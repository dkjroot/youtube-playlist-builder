"""Cache channel membership across commands without caching away new uploads."""
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from youtube_playlist_builder.api import YouTube, ConnectionFailure
from youtube_playlist_builder.state import State
from youtube_playlist_builder.sync import Sync
from test_sync import FakeYouTube, NOW, CHANNEL


class SubscriptionCacheTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = State(Path(directory.name)/'state.db')
        self.addCleanup(self.state.close)
        self.server = FakeYouTube()
        self.now = NOW
        self.topic = self.state.add('news', channels=[CHANNEL])

    def sync(self, force=False):
        api = YouTube(self.server, self.state, now=lambda: self.now)
        return Sync(api, self.state, now=lambda: self.now, report=lambda _: None, refresh_subscriptions=force)

    def reads(self, resource):
        return sum(r == resource and m == 'list' for r, m, p in self.server.calls)

    def test_cached_membership_still_sees_new_upload_on_next_command(self):
        self.assertEqual(self.sync().run(topic_name='news'), 0)
        self.now += timedelta(minutes=10)
        self.server.video_data['breaking'] = {'id': 'breaking', 'snippet': {'title': 'Breaking news',
            'channelId': CHANNEL, 'publishedAt': self.now.isoformat()}, 'status': {'privacyStatus': 'public'}}
        self.server.calls.clear()
        self.assertEqual(self.sync().run(topic_name='news'), 0)
        self.assertEqual(self.reads('subscriptions'), 0)
        self.assertEqual(self.reads('channels'), 1)  # Account fresh; cached uploads address still reads the live feed.
        self.assertGreater(self.reads('playlistItems'), 0)
        self.assertIn('breaking', {item['video_id'] for item in self.state.items(self.topic['id'])})

    def test_expiry_forcing_and_disabled_cache_refresh(self):
        sync = self.sync(); sync.identify(); sync.subscriptions()
        self.now += timedelta(hours=24)
        self.server.calls.clear()
        sync = self.sync(); sync.identify(); sync.subscriptions()
        self.assertEqual(self.reads('subscriptions'), 1)
        self.server.calls.clear()
        sync = self.sync(True); sync.identify(); sync.subscriptions(); sync.subscriptions()
        self.assertEqual(self.reads('subscriptions'), 1)  # Force once, shared across this run.
        self.state.set_meta('subscription_cache_hours', 0)
        self.server.calls.clear()
        sync = self.sync(); sync.identify(); sync.subscriptions()
        self.assertEqual(self.reads('subscriptions'), 1)

    def test_mismatched_account_cannot_use_saved_cache(self):
        sync = self.sync(); sync.identify(); sync.subscriptions()
        self.server.owner = 'other-account'
        with self.assertRaisesRegex(ValueError, 'different YouTube account'):
            self.sync().identify()

    def test_empty_snapshot_is_a_cache_hit_and_future_timestamp_is_not(self):
        self.state.set_meta('subscription_cache', dict(owner='owner', fetched_at=self.now.isoformat(), channels=[]))
        sync = self.sync(); sync.identify()
        self.assertEqual(sync.subscriptions(), set())
        self.assertEqual(self.reads('subscriptions'), 0)
        self.now -= timedelta(hours=1)
        sync = self.sync(); sync.identify()
        self.assertEqual(sync.subscriptions(), {CHANNEL})

    def test_partial_refresh_does_not_replace_snapshot_or_use_stale_fallback(self):
        sync = self.sync(); sync.identify(); sync.subscriptions()
        before = self.state.meta('subscription_cache')
        self.now += timedelta(hours=25)
        self.server.failure = ('subscriptions', 'list', False, OSError('offline'))
        sync = self.sync(); sync.identify()
        with self.assertRaises(ConnectionFailure):
            sync.subscriptions()
        self.assertEqual(self.state.meta('subscription_cache'), before)
        self.assertIsNone(sync.subscribed)

    def test_cache_owner_mismatch_and_corruption_trigger_refresh(self):
        for cached in [dict(owner='wrong', fetched_at=self.now.isoformat(), channels=[]),
                       dict(owner='owner', fetched_at='invalid', channels=[]),
                       dict(owner='owner', fetched_at=self.now.isoformat(), channels=[{}])]:
            self.state.set_meta('subscription_cache', cached)
            self.server.calls.clear()
            sync = self.sync(); sync.identify()
            self.assertEqual(sync.subscriptions(), {CHANNEL})
            self.assertEqual(self.reads('subscriptions'), 1)
