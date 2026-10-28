"""Fair write rounds must preserve recovery, freshness, ordering and quota boundaries."""
from datetime import timedelta
from itertools import groupby
import unittest
from unittest.mock import Mock

import test_sync
from test_sync import CHANNEL


class FairBatchTests(unittest.TestCase):
    setUp = test_sync.SyncTests.setUp
    topic = test_sync.SyncTests.topic
    sync = test_sync.SyncTests.sync
    # Reuse the fake-server fixture, not its test methods.
    def seed(self, names=('busy', 'quiet', 'weekly')):
        for name in names:
            self.topic(name, frequency='weekly')
            self.assertEqual(self.sync().run(topic_name=name), 0)
        self.now += timedelta(days=8)
        self.server.video_data.clear()
        for index in range(8):
            key = f'new{index}'
            self.server.video_data[key] = {'id': key, 'snippet': {'title': key,
                'channelId': CHANNEL, 'publishedAt': (self.now - timedelta(hours=index + 1)).isoformat()},
                'status': {'privacyStatus': 'public'}}
        self.server.calls.clear()

    def test_every_playlist_gets_new_video_before_busy_one_finishes(self):
        self.seed()
        self.assertEqual(self.sync(budget=655).run(), 2)
        for topic in self.state.topics():
            self.assertTrue(any(item['video_id'].startswith('new') for item in self.state.items(topic['id'])))
            self.assertIsNone(topic['next_due'])
        inserts = [p['body']['snippet']['playlistId'] for r, m, p in self.server.calls if r == 'playlistItems' and m == 'insert']
        self.assertEqual(len(set(inserts)), 3)
        self.assertTrue(all(len(list(group)) <= 5 for _, group in groupby(inserts)))
        # The old entry was removed during a turn, not postponed behind all additions.
        for topic in self.state.topics():
            self.assertNotIn('v1', [item['video_id'] for item in self.state.items(topic['id'])])

    def test_complete_rounds_reuse_plan_and_insert_newest_first(self):
        self.seed()
        classifier = Mock()
        classifier.match.side_effect = lambda topic, videos: dict(videos)
        sync = self.sync()
        sync.classifier = classifier
        self.assertEqual(sync.run(), 0)
        self.assertEqual(classifier.match.call_count, 3)
        for topic in self.state.topics():
            self.assertEqual(topic['last_success'], self.now.isoformat())
            ids = [item['snippet']['resourceId']['videoId'] for item in self.server.items[topic['playlist_id']]]
            self.assertEqual(ids, [f'new{i}' for i in range(8)])
        self.assertEqual(len([1 for r, m, p in self.server.calls if r == 'videos']), 2)  # Reconciliation of old ID, then new IDs.

    def test_completion_saved_at_exact_write_boundary(self):
        self.topic()
        # Five initial reads, then playlist creation and its single insertion.
        self.assertEqual(self.sync(budget=105).run(), 0)
        self.assertIsNotNone(self.state.topic('chess')['last_success'])
        self.assertIsNotNone(self.state.topic('chess')['next_due'])

    def test_one_failed_topic_is_not_retried_in_each_round(self):
        self.seed()
        classifier = Mock()
        attempts = []
        def match(topic, videos):
            attempts.append(topic['name'])
            if topic['name'] == 'busy':
                raise ValueError('bad model result')
            return dict(videos)
        classifier.match.side_effect = match
        sync = self.sync()
        sync.classifier = classifier
        self.assertEqual(sync.run(), 1)
        self.assertEqual(attempts.count('busy'), 1)
        self.assertIsNone(self.state.topic('busy')['next_due'])
        self.assertIsNotNone(self.state.topic('quiet')['next_due'])

    def test_remaining_units_uses_smaller_daily_allowance(self):
        sync = self.sync(budget=1000, daily=500)
        self.assertEqual(sync.api.remaining(), 500)
        self.state.charge(sync.api.day(), 450)
        self.assertEqual(sync.api.remaining(), 50)
        self.state.exhaust(sync.api.day())
        self.assertEqual(sync.api.remaining(), 0)
