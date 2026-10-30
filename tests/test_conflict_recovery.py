"""Bounded 409 recovery rebuilds plans after reconciliation and never blindly repeats writes."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from youtube_playlist_builder.api import YouTube
from youtube_playlist_builder.state import State
from youtube_playlist_builder.sync import Sync
from test_sync import FakeYouTube, CHANNEL, NOW, http_error


class ConflictRecoveryTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = State(Path(self.temp.name) / 'state.db')
        self.addCleanup(self.state.close)
        self.server = FakeYouTube()
        self.messages = []
        self.sleep = patch('youtube_playlist_builder.sync.time.sleep').start()
        self.addCleanup(patch.stopall)

    def run_sync(self, budget=9500, **kwargs):
        api = YouTube(self.server, self.state, now=lambda:NOW, run_budget=budget)
        return Sync(api, self.state, now=lambda:NOW, report=self.messages.append).run(**kwargs)

    def topic(self, name):
        return self.state.add(name, channels=[CHANNEL])

    def test_first_insert_after_creation_recovers_without_creating_another_playlist(self):
        topic = self.topic('robotics')
        self.server.failure = ('playlistItems','insert',False,http_error(409,'SERVICE_UNAVAILABLE'))
        self.assertEqual(self.run_sync(),0)
        self.assertEqual(len(self.server.playlist_data),1)
        self.assertEqual(len(self.state.items(topic['id'])),1)
        self.assertIsNone(self.state.pending(topic['id']))
        writes=[c for c in self.server.calls if c[1]=='insert']
        self.assertEqual([c[0] for c in writes],['playlists','playlistItems','playlistItems'])
        retry_index=max(i for i,c in enumerate(self.server.calls) if c[0:2]==('playlistItems','insert'))
        first_index=min(i for i,c in enumerate(self.server.calls) if c[0:2]==('playlistItems','insert'))
        self.assertTrue(any(c[0:2]==('playlists','list') for c in self.server.calls[first_index+1:retry_index]))
        self.assertTrue(any(c[0:2]==('playlistItems','list') for c in self.server.calls[first_index+1:retry_index]))
        self.sleep.assert_called_once_with(1)
        data=json.loads(self.state.db.execute('SELECT payload FROM run_diagnostics').fetchone()[0])
        self.assertEqual(len(data['conflict_recoveries']),1)
        self.assertEqual(sum(c['units'] for c in data['usage'].values()),data['actual_units'])

    def test_insert_that_landed_is_not_inserted_twice_even_for_manual_update(self):
        topic=self.topic('film')
        self.server.failure=('playlistItems','insert',True,http_error(409,'ABORTED'))
        self.assertEqual(self.run_sync(topic_name='film'),0)
        self.assertEqual(len([c for c in self.server.calls if c[0:2]==('playlistItems','insert')]),1)
        self.assertEqual(len(self.state.items(topic['id'])),1)
        self.assertIsNone(self.state.pending(topic['id']))

    def test_repeated_conflict_is_bounded_and_other_topics_finish(self):
        bad=self.topic('bad')
        good=self.topic('good')
        execute=self.server.execute
        def conflict(resource,method,parameters):
            if resource=='playlistItems' and method=='insert' and parameters['body']['snippet']['playlistId']==self.state.topic('bad')['playlist_id']:
                self.server.failure=(resource,method,False,http_error(409,'SERVICE_UNAVAILABLE'))
            return execute(resource,method,parameters)
        self.server.execute=conflict
        self.assertEqual(self.run_sync(),1)
        bad_id=self.state.topic('bad')['playlist_id']
        attempts=[c for c in self.server.calls if c[0:2]==('playlistItems','insert') and c[2]['body']['snippet']['playlistId']==bad_id]
        self.assertEqual(len(attempts),3)  # Initial request plus two bounded recoveries.
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list],[1,3])
        self.assertIsNotNone(self.state.pending(bad['id']))
        self.assertIsNone(self.state.topic('bad')['last_success'])
        self.assertIsNotNone(self.state.topic('good')['last_success'])
        self.assertEqual(len(self.state.items(good['id'])),1)

    def test_budget_pause_during_recovery_does_not_retry_insert(self):
        topic=self.topic('robotics')
        self.server.failure=('playlistItems','insert',False,http_error(409,'ABORTED'))
        self.assertEqual(self.run_sync(budget=105),2)
        self.assertEqual(len([c for c in self.server.calls if c[0:2]==('playlistItems','insert')]),1)
        self.assertIsNotNone(self.state.pending(topic['id']))
        self.assertIsNone(self.state.topic('robotics')['last_success'])

    def test_unknown_creation_is_never_created_again_automatically(self):
        topic=self.topic('new')
        self.server.failure=('playlists','insert',False,http_error(409,'ABORTED'))
        self.assertEqual(self.run_sync(),1)
        self.assertEqual(len([c for c in self.server.calls if c[0:2]==('playlists','insert')]),1)
        self.assertIsNotNone(self.state.pending(topic['id']))
