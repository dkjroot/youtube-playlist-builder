"""Run snapshots compare forecast assumptions and realised work without extra API requests."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from youtube_playlist_builder.api import YouTube
from youtube_playlist_builder.state import State
from youtube_playlist_builder.sync import Sync
from youtube_playlist_builder.run_diagnostics import RunDiagnostics
from test_sync import FakeYouTube, CHANNEL, NOW, http_error


class RunDiagnosticsTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = State(Path(self.temp.name) / 'state.db')
        self.addCleanup(self.state.close)
        self.topic = self.state.add('chess', channels=[CHANNEL])
        self.server = FakeYouTube()

    def run_sync(self, budget=9500, **options):
        api = YouTube(self.server, self.state, now=lambda:NOW, run_budget=budget)
        sync = Sync(api, self.state, now=lambda:NOW, report=lambda _:None)
        return sync.run(**options), api

    def record(self):
        row = dict(self.state.db.execute('SELECT * FROM run_diagnostics ORDER BY rowid DESC LIMIT 1').fetchone())
        row['payload'] = json.loads(row['payload'])
        return row

    def test_success_contains_pre_run_forecast_plan_and_exact_usage(self):
        with patch.dict(os.environ, {'YT_PLAYLISTS_FORECAST':json.dumps({'rate':.25,'mode':'budget','days':'10'})}):
            result, api = self.run_sync()
        self.assertEqual(result, 0)
        row = self.record()
        data = row['payload']
        self.assertEqual(row['status'], 'completed')
        self.assertIsNotNone(row['finished_at'])
        self.assertEqual(data['forecast_settings'], {'rate':.25,'mode':'budget','days':10})
        self.assertEqual(data['forecast_source'], 'web_controls')
        self.assertIsNone(data['topics'][str(self.topic['id'])]['last_success'])
        self.assertIsNotNone(self.state.topic('chess')['last_success'])
        self.assertEqual(data['plans'][str(self.topic['id'])]['adds'], 1)
        self.assertEqual(data['plans'][str(self.topic['id'])]['creates'], 1)
        self.assertEqual(data['completed_topics'], [self.topic['id']])
        self.assertEqual(data['actual_units'], api.spent)
        self.assertEqual(sum(c['units'] for c in data['usage'].values()), api.spent)
        self.assertEqual(sum(c['units'] for usage in data['usage_by_topic'].values() for c in usage.values()), api.spent)
        self.assertEqual(data['usage']['channels.list']['calls'], 2)
        self.assertEqual(len(self.server.calls), sum(c['calls'] for c in data['usage'].values()))
        self.assertNotIn('v1', json.dumps(data))  # Counts/dates only, not video payloads.

    def test_partial_run_and_later_same_day_run_are_separate(self):
        result, api = self.run_sync(budget=55)
        self.assertEqual(result, 2)
        first = self.record()
        self.assertEqual(first['status'], 'paused')
        self.assertEqual(first['payload']['completed_topics'], [])
        result, second_api = self.run_sync()
        self.assertEqual(result, 0)
        second = self.record()
        self.assertNotEqual(first['run_id'], second['run_id'])
        self.assertEqual(second['payload']['actual_units'], second_api.spent)
        self.assertEqual(self.state.quota(second_api.day())['used'], api.spent + second_api.spent)

    def test_all_records_scope_separately_from_not_due_chart(self):
        self.run_sync()
        result, _ = self.run_sync(all_topics=True)
        self.assertEqual(result, 0)
        data = self.record()['payload']
        self.assertEqual(data['operation'], 'all')
        self.assertEqual(data['graph_forecast']['days'][0]['topics'], 0)
        self.assertEqual(data['scope_forecast']['topics'], 1)
        self.assertIn(str(self.topic['id']), data['topics'])

    def test_failed_identification_and_invalid_scenario_still_get_summary(self):
        self.server.failure = ('channels','list',False,http_error(403,'forbidden'))
        with patch.dict(os.environ, {'YT_PLAYLISTS_FORECAST':'not JSON'}):
            with self.assertRaises(Exception):
                self.run_sync()
        row = self.record()
        self.assertEqual(row['status'], 'failed')
        self.assertIn('forecast_error', row['payload'])
        self.assertEqual(row['payload']['actual_units'], 1)

    def test_optional_diagnostics_failure_does_not_block_sync(self):
        with patch.object(RunDiagnostics, 'plan', side_effect=OSError('disk problem')):
            result, _ = self.run_sync()
        self.assertEqual(result, 0)
        self.assertTrue(self.state.topic('chess')['playlist_id'])

    def test_retention_is_bounded(self):
        for i in range(125):
            self.state.db.execute('INSERT INTO run_diagnostics VALUES (?,?,?,?,?)',
                                  (str(i), f'2025-01-01T00:{i:03}:00', None, 'running', '{}'))
        self.state.db.commit()
        self.run_sync()
        self.assertEqual(self.state.db.execute('SELECT count(*) FROM run_diagnostics').fetchone()[0],120)
