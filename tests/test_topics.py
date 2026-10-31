from contextlib import redirect_stdout, redirect_stderr
from datetime import timedelta
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from youtube_playlist_builder.cli import main
from youtube_playlist_builder.state import State
from youtube_playlist_builder.topics import suggest, edit_topic
from youtube_playlist_builder.schedule import next_due
from test_sync import CHANNEL, NOW


class TopicTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'state.sqlite3'

    def cli(self, *args):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return main(['--state', str(self.path), *args])

    @patch('youtube_playlist_builder.cli.authenticate')
    def test_topic_crud_runs_offline(self, auth):
        self.assertEqual(self.cli('topics', 'add', 'chess', '--channel', CHANNEL), 0)
        self.assertEqual(self.cli('topics', 'set', 'chess', '--frequency', 'weekly', '--max-age', '14', '--playlist-name', 'Chess time'), 0)
        self.assertEqual(self.cli('topics', 'disable', 'chess'), 0)
        state = State(self.path)
        self.addCleanup(state.close)
        topic = state.topic('chess')
        self.assertEqual((topic['frequency'], topic['max_age_days'], topic['enabled']), ('weekly', 14, 0))
        self.assertEqual(topic['playlist_name'], 'Chess time')
        self.assertEqual(self.cli('topics', 'enable', 'chess'), 0)
        self.assertEqual(self.cli('topics', 'remove', 'chess'), 0)
        self.assertEqual(state.topics(), [])
        auth.assert_not_called()

    @patch('youtube_playlist_builder.cli.authenticate')
    def test_every_time_frequency_can_be_created_and_edited_offline(self, auth):
        self.assertEqual(self.cli('topics', 'add', 'news', '--frequency', 'every-time'), 0)
        state = State(self.path)
        self.addCleanup(state.close)
        self.assertEqual(state.topic('news')['frequency'], 'every-time')
        self.assertEqual(self.cli('topics', 'set', 'news', '--frequency', 'daily'), 0)
        self.assertEqual(self.cli('topics', 'set', 'news', '--frequency', 'every-time'), 0)
        self.assertEqual(state.topic('news')['frequency'], 'every-time')
        auth.assert_not_called()

    def test_filters_can_be_replaced_and_cleared(self):
        self.cli('topics', 'add', 'chess', '--channel', CHANNEL, '--include', 'lesson')
        self.cli('topics', 'set', 'chess', '--clear-filters', '--exclude', 'short')
        state = State(self.path)
        self.addCleanup(state.close)
        self.assertEqual(state.topic('chess')['include_words'], [])
        self.assertEqual(state.topic('chess')['exclude_words'], ['short'])

    def test_invalid_channel_and_duplicate_topic_are_rejected(self):
        self.assertEqual(self.cli('topics', 'add', 'chess', '--channel', 'bad-id'), 1)
        self.assertEqual(self.cli('topics', 'add', 'chess', '--channel', CHANNEL), 0)
        self.assertEqual(self.cli('topics', 'add', 'CHESS', '--channel', CHANNEL), 1)

    def test_frequency_change_recomputes_due_and_content_change_makes_due_now(self):
        state = State(self.path)
        self.addCleanup(state.close)
        topic = state.add('chess', channels=[CHANNEL], last_success=NOW.isoformat(), next_due=next_due(NOW, 'daily').isoformat())
        edit_topic(state, topic, {'frequency': 'weekly'})
        self.assertEqual(state.topic('chess')['next_due'], next_due(NOW, 'weekly').isoformat())
        edit_topic(state, state.topic('chess'), {'max_age_days': 2})
        self.assertIsNone(state.topic('chess')['next_due'])

    def test_pending_write_cannot_be_discarded_by_edit_or_remove(self):
        state = State(self.path)
        self.addCleanup(state.close)
        topic = state.add('chess')
        state.begin_write(topic['id'], 'create', {})
        with self.assertRaisesRegex(ValueError, 'Reconcile'):
            edit_topic(state, topic, {'max_age_days': 2})
        with self.assertRaisesRegex(ValueError, 'Reconcile'):
            state.remove(topic['id'])

    def test_two_processes_cannot_update_same_database(self):
        first, second = State(self.path), State(self.path)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        with first.locked():
            with self.assertRaisesRegex(ValueError, 'Another process'):
                with second.locked():
                    self.fail('Second lock must fail')

    def test_suggestions_ignore_donation_footer_and_preserve_review_evidence(self):
        channels = [dict(channel_id=CHANNEL, title='Chess lessons', description='Learn chess.'),
                    dict(channel_id='UC'+'b'*22, title='Woodworking', description='Woodworking projects.\n\nBuy me a coffee: https://example.com')]
        result = {entry['name']: entry for entry in suggest(channels)}
        self.assertIn('chess', result)
        self.assertIn('woodworking', result)
        self.assertNotIn('coffee', result)
        self.assertIn('reason', result['chess']['channels'][0])

    def test_suggested_channels_can_be_added_from_export(self):
        export = self.path.parent / 'subscriptions.json'
        export.write_text(json.dumps([dict(channel_id=CHANNEL, title='Chess lessons')]))
        self.assertEqual(self.cli('topics', 'add', 'chess', '--suggested', '--subscriptions', str(export)), 0)
        state = State(self.path)
        self.addCleanup(state.close)
        self.assertEqual(state.topic('chess')['channels'], [CHANNEL])

    def test_timezone_and_budget_saved(self):
        self.assertEqual(self.cli('settings', '--daily-budget', '5000', '--timezone', 'Europe/London'), 0)
        self.assertEqual(self.cli('settings', '--timezone', 'not-a-zone'), 1)
        state = State(self.path)
        self.addCleanup(state.close)
        self.assertEqual(state.meta('daily_budget'), 5000)
        self.assertEqual(state.meta('timezone'), 'Europe/London')

    @patch('builtins.input', side_effect=['1', '14', '', '', '0'])
    def test_menu_can_show_topics_and_settings_then_exit(self, user_input):
        self.assertEqual(self.cli('menu'), 0)

    @patch('youtube_playlist_builder.cli.authenticate')
    def test_empty_auto_queue_needs_no_sign_in(self, auth):
        self.assertEqual(self.cli('auto'), 0)
        auth.assert_not_called()
