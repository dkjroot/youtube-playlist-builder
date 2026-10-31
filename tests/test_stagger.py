from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from youtube_playlist_builder.schedule import initialise_staggering, staggered_due, next_due, parse_time, due_topics
from youtube_playlist_builder.state import State
from youtube_playlist_builder.cli import main
from test_sync import NOW


class StaggerTests(unittest.TestCase):
    def test_existing_synchronised_topics_migrate_without_delaying_due_work(self):
        with TemporaryDirectory() as directory:
            state = State(Path(directory) / 'state.db')
            self.addCleanup(state.close)
            for i in range(4):
                state.add(str(i), frequency='every-2-days', last_success=NOW.isoformat(),
                          next_due=next_due(NOW, 'every-2-days').isoformat())
            due = state.add('unfinished', frequency='every-2-days', last_success=NOW.isoformat())
            initialise_staggering(state, NOW)
            dates = [state.topic(str(i))['next_due'] for i in range(4)]
            self.assertEqual(len(set(dates)), 2)
            self.assertTrue(all(dates.count(date) == 2 for date in dates))
            self.assertIsNone(state.by_id(due['id'])['next_due'])
            initial = state.topics()
            initialise_staggering(state, NOW)
            self.assertEqual(state.topics(), initial)

    def test_every_time_ignores_future_due_and_can_switch_back_to_calendar(self):
        from youtube_playlist_builder.topics import edit_topic
        with TemporaryDirectory() as directory:
            state = State(Path(directory) / 'state.db')
            self.addCleanup(state.close)
            topic = state.add('news', last_success=NOW.isoformat(),
                              next_due=next_due(NOW, 'daily').isoformat())
            edit_topic(state, topic, {'frequency': 'every-time'})
            topic = state.by_id(topic['id'])
            self.assertEqual(staggered_due(state, topic, NOW), NOW)
            self.assertEqual(next_due(NOW, 'every-time'), NOW)
            state.update(topic['id'], next_due=(NOW + timedelta(days=100)).isoformat())
            self.assertEqual(len(due_topics(state.topics(), 0, NOW)), 1)
            initialise_staggering(state, NOW)
            self.assertNotIn(str(topic['id']), state.meta('schedule_slots', {}))
            edit_topic(state, state.by_id(topic['id']), {'frequency': 'weekly'})
            topic = state.by_id(topic['id'])
            self.assertGreater(parse_time(topic['next_due']), NOW)
            self.assertEqual(state.meta('schedule_slots')[str(topic['id'])]['frequency'], 'weekly')

    def test_monthly_and_dst_slots_remain_local_midnight(self):
        from zoneinfo import ZoneInfo
        with TemporaryDirectory() as directory:
            state = State(Path(directory) / 'state.db')
            self.addCleanup(state.close)
            state.set_meta('timezone', 'Europe/London')
            topic = state.add('monthly', frequency='monthly')
            jan = parse_time('2027-01-31T12:00:00Z')
            feb = staggered_due(state, topic, jan)
            self.assertEqual(feb.day, 28)
            weekly = state.add('weekly', frequency='weekly')
            march = parse_time('2027-03-25T12:00:00Z')
            result = staggered_due(state, weekly, march)
            self.assertEqual(result.astimezone(ZoneInfo('Europe/London')).hour, 0)
            self.assertEqual(result.hour, 23)

    @patch('youtube_playlist_builder.cli.Sync')
    @patch('youtube_playlist_builder.cli.build')
    @patch('youtube_playlist_builder.cli.authenticate')
    def test_cli_alias_bypasses_due_check(self, auth, build, sync):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'state.db'
            state = State(path)
            state.add('future', next_due=(NOW + timedelta(days=365)).isoformat())
            state.close()
            sync.return_value.run.return_value = 0
            self.assertEqual(main(['--state', str(path), '--auto', '--all']), 0)
            self.assertTrue(sync.return_value.run.call_args.kwargs['all_topics'])

    def test_menu_routes_all(self):
        from types import SimpleNamespace
        from youtube_playlist_builder.menu import run_menu
        with TemporaryDirectory() as directory:
            args = SimpleNamespace(state=Path(directory)/'state.db', token=Path('token'),
                                   client_secrets=Path('client'), llm_config=Path('llm'))
            calls = []
            with patch('builtins.input', side_effect=['16', '0']), patch('builtins.print'):
                self.assertEqual(run_menu(args, lambda words: calls.append(words)), 0)
            self.assertEqual(calls[0][-2:], ['auto', '--all'])
