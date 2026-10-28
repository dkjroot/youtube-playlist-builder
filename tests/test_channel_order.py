"""Channel priorities apportion insertion opportunities, not playlist display order."""
from collections import Counter
from datetime import timedelta
import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from youtube_playlist_builder.channel_order import addition_turns
from youtube_playlist_builder.cli import main
from youtube_playlist_builder.state import State
from youtube_playlist_builder.topics import edit_topic
from test_sync import NOW

A, B, C = ('UC' + letter * 22 for letter in ('a', 'b', 'c'))


def videos(counts):
    """Make each channel busy enough that recency alone would pick A repeatedly."""
    return [dict(video_id=f'{cid[-1]}{i}', channel_id=cid,
                 published_at=(NOW - timedelta(minutes=i + offset)).isoformat(), title='A video')
            for offset, (cid, count) in enumerate(counts.items()) for i in range(count)]


class ChannelOrderTests(unittest.TestCase):
    def test_default_medium_is_one_per_channel_before_repeating(self):
        topic = dict(channels=[B, A], channel_priorities={}, channel_rotation={})
        turns = list(addition_turns(topic, videos({A: 5, B: 2})))
        self.assertEqual([row['channel_id'] for row, _ in turns], [A, B, A, B, A, A, A])
        self.assertEqual([row['video_id'] for row, _ in turns if row['channel_id'] == A], ['a0','a1','a2','a3','a4'])

    def test_three_two_one_weighting_still_serves_low(self):
        topic = dict(channels=[A, B, C], channel_priorities={A:'high', B:'medium', C:'low'})
        turns = list(addition_turns(topic, videos({A:20, B:20, C:20})))
        first = [row['channel_id'] for row, _ in turns[:6]]
        self.assertEqual(first, [A, B, C, A, B, A])
        self.assertEqual(Counter(first), {A:3, B:2, C:1})
        self.assertEqual([row['channel_id'] for row, _ in turns[6:12]], first)

    def test_resumes_at_saved_turn_and_changed_priorities_reset_cycle(self):
        topic = dict(channels=[A, B], channel_priorities={})
        first, rotation = next(addition_turns(topic, videos({A:4, B:4})))
        remaining = [row for row in videos({A:4, B:4}) if row['video_id'] != first['video_id']]
        restarted = list(addition_turns(dict(topic, channel_rotation=rotation), remaining))
        self.assertEqual(restarted[0][0]['channel_id'], B)
        changed = dict(topic, channel_rotation=rotation, channel_priorities={A:'high', B:'low'})
        self.assertEqual(next(addition_turns(changed, remaining))[0]['channel_id'], A)

    def test_empty_channels_skip_unused_turns(self):
        topic = dict(channels=[A, B, C], channel_priorities={A:'high', C:'low'})
        turns = list(addition_turns(topic, videos({C:3})))
        self.assertEqual([row['video_id'] for row, _ in turns], ['c0','c1','c2'])
        self.assertEqual(list(addition_turns(dict(channels=[], channel_priorities={}), [])), [])

    def test_cli_edit_persists_priorities_and_removing_channel_drops_override(self):
        with TemporaryDirectory() as directory, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            path = Path(directory)/'state.db'
            def cli(*words):
                return main(['--state',str(path),*words])
            self.assertEqual(cli('topics','add','news','--channel',A,'--channel',B,'--channel-priority',A+'=high'),0)
            self.assertEqual(cli('topics','set','news','--channel-priority',B+'=low'),0)
            state = State(path)
            self.assertEqual(state.topic('news')['channel_priorities'],{A:'high',B:'low'})
            state.update(1, channel_rotation={'signature':'old','next':1}, next_due=(NOW + timedelta(days=1)).isoformat())
            state.close()
            self.assertEqual(cli('topics','set','news','--channel-priority',A+'=medium'),0)
            state = State(path)
            self.assertEqual(state.topic('news')['channel_rotation'],{})
            self.assertIsNone(state.topic('news')['next_due'])
            state.close()
            self.assertEqual(cli('topics','set','news','--remove-channel',B),0)
            self.assertEqual(cli('topics','set','news','--channel-priority',C+'=high'),1)
            state = State(path)
            self.assertNotIn(B,state.topic('news')['channel_priorities'])
            state.close()
            with self.assertRaises(SystemExit):
                cli('topics','set','news','--channel-priority',A+'=urgent')

    def test_existing_database_migration_preserves_topics_and_defaults_to_medium(self):
        import json
        import sqlite3
        with TemporaryDirectory() as directory:
            path = Path(directory)/'legacy.db'
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE topics (id INTEGER PRIMARY KEY, name TEXT UNIQUE, marker TEXT, channels TEXT, include_words TEXT, exclude_words TEXT)")
            db.execute("INSERT INTO topics VALUES (7, 'news', 'keep-this-marker', ?, '[]', '[]')", (json.dumps([A,B]),))
            db.commit()
            db.close()
            state = State(path)
            topic = state.topic('news')
            self.assertEqual(topic['marker'],'keep-this-marker')
            self.assertEqual(topic['channel_priorities'],{})
            self.assertEqual(topic['channel_rotation'],{})
            self.assertEqual(topic['id'],7)
            turns = list(addition_turns(topic,videos({A:2,B:2})))
            self.assertEqual([row['channel_id'] for row,_ in turns],[A,B,A,B])
            state.close()

    def test_menu_priority_edit_uses_channel_names_and_can_cancel(self):
        from types import SimpleNamespace
        from youtube_playlist_builder.menu import run_menu
        with TemporaryDirectory() as directory:
            args = SimpleNamespace(state=Path(directory)/'state.db', token=Path('token'),
                                   client_secrets=Path('client'), llm_config=Path('llm'))
            state = State(args.state)
            state.add('news', channels=[A])
            state.close()
            calls = []
            with patch('youtube_playlist_builder.menu.ask',side_effect=['3','1','8','1','1','0']),patch('builtins.print'):
                self.assertEqual(run_menu(args,lambda words:calls.append(words)),0)
            self.assertEqual(calls[-1][-5:],['topics','set','news','--channel-priority',A+'=high'])
