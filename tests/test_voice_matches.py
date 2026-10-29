"""Chosen voice phrases persist atomically with assignments and survive backups."""

from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from youtube_playlist_builder.backup import create_backup, restore_backup
from youtube_playlist_builder.organiser import assign_channel
from youtube_playlist_builder.state import State
from youtube_playlist_builder.web import snapshot

A = 'UC' + 'a' * 22


class VoiceMatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.args = SimpleNamespace(state=self.root / 'state.sqlite3')
        self.state = State(self.args.state)
        self.addCleanup(self.state.close)
        self.topic = self.state.add('Synth', channels=[])

    def choose(self, phrase='', topic='Synth', assigned=True):
        assign_channel(self.args, {'channel_id':A, 'topic':topic, 'assigned':assigned, 'voice_phrase':phrase})

    def matches(self):
        return [dict(row) for row in self.state.db.execute('SELECT * FROM voice_matches')]

    def test_normalised_phrase_is_persistent_and_repeated_choices_are_counted(self):
        self.choose(' Since! ')
        self.choose('SINCE')  # Existing membership still teaches without changing topic state.
        self.assertEqual(self.matches(), [{'phrase':'since', 'topic_id':self.topic['id'], 'uses':2}])
        self.assertEqual(snapshot(self.args)['voice_matches'], self.matches())
        with closing(sqlite3.connect(self.args.state)) as db:
            self.assertEqual(db.execute('SELECT phrase,uses FROM voice_matches').fetchone(), ('since', 2))

    def test_normal_checkbox_and_removal_do_not_teach_or_forget(self):
        self.choose()
        self.assertEqual(self.matches(), [])
        self.choose('since')
        self.choose(assigned=False)
        self.assertEqual(self.matches()[0]['phrase'], 'since')

    def test_empty_whitespace_and_punctuation_do_not_teach(self):
        for phrase in ('', '   ', '...!?', '\u0301'):
            with self.subTest(phrase=phrase):
                self.choose(phrase)
                self.assertEqual(self.matches(), [])
        self.assertEqual(self.state.topic('Synth')['channels'], [A])

    def test_same_phrase_can_suggest_several_topics_and_tracks_topic_identity(self):
        self.choose('since')
        other = self.state.add('Synthesis', channels=[])
        self.choose('since', 'Synthesis')
        self.state.update(self.topic['id'], name='Synths and music')
        self.assertEqual({m['topic_id'] for m in self.matches()}, {self.topic['id'], other['id']})
        self.state.remove(self.topic['id'])
        self.assertEqual([m['topic_id'] for m in self.matches()], [other['id']])

    def test_failed_move_does_not_learn(self):
        source = self.state.add('Source', channels=[A])
        self.state.set_meta('single_topic_channels', True)
        self.state.begin_write(source['id'], 'insert', {})
        with self.assertRaisesRegex(ValueError, 'Reconcile'):
            self.choose('since')
        self.assertEqual(self.matches(), [])
        self.assertEqual(self.state.topic('Synth')['channels'], [])
        self.assertEqual(self.state.topic('Source')['channels'], [A])

    def test_invalid_phrase_rejected_before_assignment(self):
        for phrase, assigned in [(None, True), (123, True), ('x' * 121, True), ('since', False)]:
            with self.subTest(phrase=phrase):
                with self.assertRaisesRegex(ValueError, 'voice phrase'):
                    self.choose(phrase, assigned=assigned)
        self.assertEqual(self.state.topic('Synth')['channels'], [])
        self.assertEqual(self.matches(), [])

    def test_backup_round_trip_includes_learning(self):
        self.choose('since')
        path, _ = create_backup(self.args.state)
        restored, _ = restore_backup(path, self.root / 'restored')
        with closing(sqlite3.connect(restored / 'playlists.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT phrase,topic_id,uses FROM voice_matches').fetchone(), ('since', self.topic['id'], 1))
