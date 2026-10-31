"""Learn uncertain arrivals per topic without discounting known work or recorded quota."""
from datetime import timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from zoneinfo import ZoneInfo

from youtube_playlist_builder.forecast import project
from youtube_playlist_builder.forecast_calibration import topic_calibration, signature
from youtube_playlist_builder.state import State
from test_sync import NOW, CHANNEL

B = 'UC' + 'b' * 22


class CalibrationTests(TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.state=State(Path(self.temp.name)/'state.db');self.addCleanup(self.state.close)
        self.topic=self.state.add('quiet',channels=[CHANNEL,B],frequency='daily',max_age_days=7,
                                  max_videos=2,channel_limits={CHANNEL:1,B:1},playlist_id='PL',
                                  last_success=(NOW-timedelta(days=1)).isoformat())
        with self.state.db:
            for i,cid in enumerate((CHANNEL,B)):
                self.state.put_item(self.topic['id'],dict(item_id=str(i),video_id=str(i),channel_id=cid,
                                                         published_at=(NOW-timedelta(hours=20)).isoformat()))

    def sample(self, actual=1, predicted=2, status='completed', rate=1, raw=None, **extra):
        tid=str(self.topic['id']);estimate={'adds':predicted,'reads':4}
        if raw is not None: estimate['raw_adds']=raw
        data=dict(operation='due',preview=False,completed_topics=[self.topic['id']],
                  forecast_settings={'rate':rate},topics={tid:dict(topic='quiet',signature=signature(self.state,self.topic),estimate=estimate)},
                  plans={tid:dict(adds=actual,creates=0,renames=0)},usage_by_topic={tid:{'playlistItems.list':{'units':6,'calls':6}}})
        data.update(extra)
        self.state.db.execute('INSERT INTO run_diagnostics VALUES (?,?,?,?,?)',
                              (str(self.state.db.total_changes), (NOW-timedelta(hours=1)).isoformat(),NOW.isoformat(),status,json.dumps(data)))
        self.state.db.commit()

    def test_additions_and_reads_learn_separately_and_no_history_keeps_original(self):
        before=project(self.state,now=NOW,days=1)
        self.assertEqual(project(self.state,now=NOW,days=1,calibrate=True)['days'],before['days'])
        self.sample()
        after=project(self.state,now=NOW,days=1,calibrate=True)
        row=after['days'][0]
        self.assertEqual(row['raw_adds'],2)
        self.assertEqual(row['adds'],1)
        self.assertEqual(row['removes'],1)
        self.assertEqual(row['reads'],7) # Six calibrated discovery reads plus live identity.
        self.assertEqual(row['units'],107) # Every actual estimated mutation still costs fifty.
        self.assertEqual(after['calibration']['topics'],1)
        self.assertEqual(project(self.state,now=NOW,days=1)['days'],before['days'])

    def test_known_expirations_and_recorded_usage_are_never_halved(self):
        self.state.update(self.topic['id'],max_age_days=1)
        self.topic=self.state.topic('quiet')
        self.state.db.execute('UPDATE items SET published_at=?',((NOW-timedelta(days=2)).isoformat(),));self.state.db.commit()
        self.sample(actual=0,rate=0)
        self.state.charge(NOW.astimezone(ZoneInfo('America/Los_Angeles')).date().isoformat(),4340)
        result=project(self.state,rate=0,days=1,now=NOW,calibrate=True,mode='budget')
        row=result['days'][0]
        self.assertEqual(row['removes'],2)
        self.assertEqual(row['recorded_units'],4340)
        self.assertGreaterEqual(row['additional_units'],100)
        self.assertEqual(row['units'],4340+row['additional_units'])

    def test_creation_keeps_full_price_and_quiet_rounding_still_predicts_arrivals(self):
        self.sample(actual=0)
        topic=dict(self.topic,playlist_id=None)
        forecast=project(self.state,topics=[topic],now=NOW,days=12,calibrate=True)
        first=forecast['days'][0]
        self.assertEqual(first['units'],first['reads']+50*(first['adds']+first['removes'])+50)
        self.assertGreater(sum(row['adds'] for row in forecast['days']),0)
        self.assertTrue(all(row['adds']<=2 for row in forecast['days']))

    def test_partial_failed_reconcile_and_recovery_samples_are_excluded(self):
        self.sample(status='paused');self.sample(status='failed');self.sample(preview=True)
        self.sample(operation='reconcile');self.sample(conflict_recoveries=[{'topic_id':self.topic['id']}])
        self.assertEqual(topic_calibration(self.state,[self.topic],now=NOW),{})

    def test_configuration_and_scenario_changes_invalidate_learning(self):
        self.sample()
        self.assertEqual(topic_calibration(self.state,[self.topic],now=NOW,rate=.5),{})
        self.state.update(self.topic['id'],description='Changed matching rules')
        self.assertEqual(topic_calibration(self.state,self.state.topics(),now=NOW),{})

    def test_learning_uses_raw_predictions_instead_of_compounding_adjustments(self):
        self.sample(actual=1,predicted=2,raw=4)
        learned=topic_calibration(self.state,[self.topic],now=NOW)
        self.assertEqual(learned[str(self.topic['id'])]['additions'],.25)

    def test_sample_count_is_bounded_and_zero_is_not_a_permanent_zero_rate(self):
        for _ in range(6):self.sample(actual=0)
        learned=topic_calibration(self.state,[self.topic],now=NOW)[str(self.topic['id'])]
        self.assertEqual(learned['samples'],5)
        self.assertEqual(learned['additions'],.1)

    def test_legacy_records_bootstrap_from_available_configuration(self):
        self.sample()
        row=self.state.db.execute('SELECT run_id,payload FROM run_diagnostics').fetchone()
        data=json.loads(row['payload']);saved=data['topics'][str(self.topic['id'])]
        saved.pop('signature');saved.update(frequency='daily',channels=2,max_videos=2,max_age_days=7)
        self.state.db.execute('UPDATE run_diagnostics SET payload=? WHERE run_id=?',(json.dumps(data),row['run_id']));self.state.db.commit()
        self.assertEqual(len(topic_calibration(self.state,[self.topic],now=NOW)),1)
