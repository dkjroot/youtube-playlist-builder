"""Quota-fill uses real sync and accounting against the offline YouTube fixture."""

from contextlib import redirect_stdout
from datetime import timedelta
import io
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import test_sync
from youtube_playlist_builder.api import QuotaStop
from youtube_playlist_builder.cli import main
from youtube_playlist_builder.diagnostics import schedule_notice
from youtube_playlist_builder.schedule import parse_time
from youtube_playlist_builder.web import Jobs


class QuotaFillTests(unittest.TestCase):
    setUp = test_sync.SyncTests.setUp
    topic = test_sync.SyncTests.topic
    sync = test_sync.SyncTests.sync

    def test_remaining_daily_allowance_used_even_for_future_topics(self):
        self.topic('future', next_due=(self.now + timedelta(days=5)).isoformat())
        self.topic('disabled', enabled=False)
        sync = self.sync(budget=10000, daily=10000)
        sync.api.stop_day = sync.api.day()
        self.state.charge(sync.api.day(), 9895)
        self.assertEqual(sync.run(all_topics=True), 0)
        self.assertEqual(sync.api.spent, 105)
        self.assertEqual(sync.api.remaining(), 0)
        self.assertTrue(self.state.items(self.state.topic('future')['id']))
        self.assertIsNone(self.state.topic('disabled')['last_attempt'])

    def test_no_work_stops_without_repeated_discovery(self):
        self.topic()
        self.assertEqual(self.sync().run(all_topics=True), 0)
        sync = self.sync()
        self.assertEqual(sync.run(all_topics=True), 0)
        self.assertLess(sync.api.spent, 50)
        self.assertIn('API units used this run:', self.messages[-1])

    def test_pacific_reset_blocks_requests_without_charging_new_day(self):
        self.now = parse_time('2026-11-02T07:59:59Z')
        sync = self.sync()
        sync.api.stop_day = sync.api.day()
        self.now += timedelta(seconds=2)
        self.assertEqual(sync.api.remaining(), 0)
        with self.assertRaisesRegex(QuotaStop, 'Pacific reset'):
            sync.api.call('channels', mine=True)
        self.assertEqual(self.server.calls, [])
        self.assertEqual(self.state.quota(sync.api.day())['used'], 0)

    def test_cli_uses_special_ceiling_and_saved_budget_takes_precedence(self):
        self.topic(next_due=(self.now + timedelta(days=100)).isoformat())
        for saved, expected in ((None, 10000), (1234, 1234)):
            if saved:
                self.state.set_meta('daily_budget', saved)
            with patch('youtube_playlist_builder.cli.authenticate'), \
                 patch('youtube_playlist_builder.cli.build'), \
                 patch('youtube_playlist_builder.cli.Sync') as sync, \
                 redirect_stdout(io.StringIO()):
                sync.return_value.run.return_value = 0
                self.assertEqual(main(['--state', str(self.path), 'auto', '--quota-fill']), 0)
                api = sync.call_args.args[0]
                self.assertEqual(api.daily_budget, expected)
                self.assertEqual(api.stop_day, api.day())
                self.assertTrue(sync.return_value.run.call_args.kwargs['all_topics'])

    def test_console_notice_is_timestamped_and_redacts_credentials(self):
        output = io.StringIO()
        with redirect_stdout(output):
            schedule_notice('Scheduled auto started api_key=private-value')
        self.assertRegex(output.getvalue(), r'^\[\d{4}-\d\d-\d\dT')
        self.assertIn('Scheduled auto started', output.getvalue())
        self.assertNotIn('private-value', output.getvalue())

    def test_scheduled_worker_failure_logs_completion(self):
        jobs = Jobs(None)
        job = dict(id='test', action='quota-fill', scheduled=True, running=True, code=None, lines=[])
        with patch.object(jobs, 'base', side_effect=OSError('unavailable')), \
             patch('youtube_playlist_builder.web.schedule_notice') as notice:
            jobs._work(job, ['auto', '--quota-fill'])
        self.assertFalse(job['running'])
        self.assertEqual(job['code'], 1)
        self.assertIn('finished job test; exit code 1', notice.call_args.args[0])

    def test_scheduled_worker_mirrors_usage_and_success_to_console(self):
        jobs = Jobs(None)
        job = dict(id='test', action='quota-fill', scheduled=True, running=True,
                   code=None, lines=[], forecast=None)
        process = SimpleNamespace(stdout=io.StringIO('API units used this run: 105.\n'),
                                  stderr=io.StringIO(''), wait=lambda: 0)
        with patch.object(jobs, 'base', return_value=[]), \
             patch('youtube_playlist_builder.web.subprocess.Popen') as popen, \
             patch('youtube_playlist_builder.web.schedule_notice') as notice:
            popen.return_value = process
            jobs._work(job, ['auto', '--quota-fill'])
        self.assertFalse(job['running'])
        self.assertEqual(job['code'], 0)
        self.assertEqual(notice.call_args_list[0].args[0], 'API units used this run: 105.')
        self.assertIn('finished job test; exit code 0', notice.call_args.args[0])
