"""End-of-run counts must describe completed work, never attempted API writes."""

from datetime import timedelta
import json
import unittest
from unittest.mock import patch

import test_sync
from test_sync import http_error
from youtube_playlist_builder.run_summary import RunSummary


class RunSummaryTests(unittest.TestCase):
    setUp = test_sync.SyncTests.setUp
    topic = test_sync.SyncTests.topic
    sync = test_sync.SyncTests.sync

    def text(self):
        return '\n'.join(self.messages)

    def test_complete_run_reports_counts_and_saves_them_in_history(self):
        self.topic()
        sync = self.sync()
        self.assertEqual(sync.run(), 0)
        self.assertIn('Run summary: Completed.', self.text())
        self.assertIn('1 of 1 completed; 0 unfinished; 0 failed', self.text())
        self.assertIn('videos added: 1; entries removed: 0; playlists created: 1', self.text())
        data = json.loads(self.state.db.execute('SELECT payload FROM run_diagnostics').fetchone()[0])
        self.assertEqual(data['summary']['completed'], 1)
        self.assertEqual(data['summary']['added'], 1)
        self.assertEqual(data['summary']['units'], sync.api.spent)

    def test_budget_pause_reports_twenty_three_units_left_and_no_failed_topic(self):
        self.topic()
        sync = self.sync(daily=10000)
        self.state.charge(sync.api.day(), 9922)
        self.assertEqual(sync.run(), 2)
        self.assertIn('Run summary: Paused; progress saved.', self.text())
        self.assertIn('needs 50 units, but only 23 remain in the daily budget', self.text())
        self.assertIn('0 of 1 completed; 1 unfinished; 0 failed', self.text())
        self.assertIn('videos added: 0; entries removed: 0; playlists created: 1', self.text())
        self.assertIn('9,977 / 10,000', self.text())
        self.assertIn('23 units left', self.text())

    def test_unvisited_topics_are_not_claimed_to_have_been_checked(self):
        for name in ('first', 'second', 'third'):
            self.topic(name)
        sync = self.sync(budget=1)
        self.assertEqual(sync.run(), 2)
        counts = sync.summary.snapshot()
        self.assertEqual(counts['topics'], 3)
        self.assertEqual(counts['completed'], 0)
        self.assertEqual(counts['not_reached'], 2)
        self.assertEqual(counts['unfinished'], 3)

    def test_definite_write_error_counts_failed_topic_not_added_video(self):
        self.topic()
        self.server.failure = ('playlistItems', 'insert', False, http_error(400, 'invalidValue'))
        sync = self.sync()
        self.assertEqual(sync.run(), 1)
        self.assertIn('Finished with errors', self.text())
        self.assertIn('0 of 1 completed; 0 unfinished; 1 failed', self.text())
        self.assertEqual(sync.summary.snapshot()['added'], 0)
        self.assertEqual(sync.api.run_usage['playlistItems.insert']['calls'], 1)

    def test_recovered_conflict_counts_only_successful_retry(self):
        self.topic()
        self.server.failure = ('playlistItems', 'insert', False, http_error(409, 'ABORTED'))
        sync = self.sync()
        with patch('youtube_playlist_builder.sync.time.sleep'):
            self.assertEqual(sync.run(), 0)
        self.assertEqual(sync.api.run_usage['playlistItems.insert']['calls'], 2)
        self.assertEqual(sync.summary.snapshot()['added'], 1)
        self.assertEqual(sync.summary.snapshot()['failed'], 0)

    def test_preview_counts_topics_previewed_without_claiming_changes(self):
        self.topic()
        sync = self.sync()
        self.assertEqual(sync.run(preview=True), 0)
        self.assertIn('1 of 1 previewed', self.text())
        self.assertEqual(sync.summary.snapshot()['created'], 0)
        self.assertEqual(sync.summary.snapshot()['added'], 0)

    def test_summary_survives_optional_diagnostics_failure(self):
        self.topic()
        sync = self.sync()
        with patch('youtube_playlist_builder.sync.RunDiagnostics', side_effect=OSError('busy')):
            self.assertEqual(sync.run(), 0)
        self.assertIn('1 of 1 completed', self.text())

    def test_empty_queue_reports_zero_work_and_zero_quota(self):
        sync = self.sync()
        self.assertEqual(sync.run(), 0)
        self.assertIn('No topics needed checking.', self.text())
        self.assertIn('0 of 0 completed', self.text())
        self.assertEqual(sync.summary.snapshot()['units'], 0)

    def test_summary_keeps_original_quota_day_after_reset(self):
        sync = self.sync(daily=10000)
        summary = RunSummary(sync.api)
        self.state.charge(sync.api.day(), 9977)
        self.now += timedelta(days=1)
        self.state.charge(sync.api.day(), 500)
        self.assertEqual(summary.snapshot()['daily_used'], 9977)
        self.assertEqual(summary.snapshot()['daily_remaining'], 23)
