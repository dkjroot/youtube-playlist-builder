"""Stable addresses and existing playlist fields save reads without hiding new videos."""
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from youtube_playlist_builder.api import YouTube, ApiFailure, QuotaStop
from youtube_playlist_builder.state import State
from youtube_playlist_builder.sync import Sync
from test_sync import FakeYouTube, NOW, CHANNEL, http_error


class AddressServer(FakeYouTube):
    """An uploads address can change or disappear independently of a channel ID."""
    def __init__(self):
        super().__init__()
        self.address = 'uploads'

    def handle(self, resource, method, p):
        if resource == 'channels' and not p.get('mine'):
            return {'items': [{'id': CHANNEL, 'contentDetails': {'relatedPlaylists': {'uploads': self.address}}}]}
        if resource == 'playlistItems' and method == 'list' and p['playlistId'] in ('uploads', 'new-uploads'):
            if p['playlistId'] != self.address:
                raise http_error(404, 'playlistNotFound')
            return super().handle(resource, method, dict(p, playlistId='uploads'))
        return super().handle(resource, method, p)


class AddressCacheAndUsageTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = State(Path(directory.name)/'state.db')
        self.addCleanup(self.state.close)
        self.server = AddressServer()
        self.now = NOW
        self.topic = self.state.add('chess', channels=[CHANNEL])

    def sync(self, budget=9500):
        return Sync(YouTube(self.server, self.state, run_budget=budget, now=lambda: self.now), self.state,
                    now=lambda: self.now, report=lambda _: None)

    def test_address_cache_reuses_verified_id_but_expires(self):
        self.sync().resolve_uploads([CHANNEL])
        self.server.calls.clear()
        sync = self.sync(); sync.resolve_uploads([CHANNEL])
        self.assertEqual(sync.uploads[CHANNEL], 'uploads')
        self.assertEqual(self.server.calls, [])
        self.now += timedelta(days=7)
        self.sync().resolve_uploads([CHANNEL])
        self.assertEqual(len(self.server.calls), 1)

    def test_disappeared_cached_address_refreshes_without_blind_retry(self):
        self.sync().resolve_uploads([CHANNEL])
        self.server.failure = ('playlistItems', 'list', False, http_error(404, 'playlistNotFound'))
        self.server.calls.clear()
        sync = self.sync(); sync.resolve_uploads([CHANNEL])
        with self.assertRaises(ApiFailure):
            list(sync.upload_ids(CHANNEL, self.now - timedelta(days=7)))
        self.assertEqual([resource for resource, method, p in self.server.calls], ['playlistItems', 'channels'])

    def test_changed_address_cannot_cause_content_removals_and_recovers_next_command(self):
        self.assertEqual(self.sync().run(topic_name='chess'), 0)
        before = self.state.items(self.topic['id'])
        self.server.address = 'new-uploads'
        self.server.calls.clear()
        self.assertEqual(self.sync().run(topic_name='chess'), 1)
        self.assertEqual(self.state.items(self.topic['id']), before)
        self.assertFalse(any(method != 'list' for _, method, _ in self.server.calls))
        self.assertEqual(self.sync().run(topic_name='chess'), 0)
        self.assertEqual(self.state.items(self.topic['id']), before)

    def test_partial_mapping_lookup_does_not_replace_cache(self):
        self.sync().resolve_uploads([CHANNEL])
        before = self.state.meta('upload_playlist_ids')
        other = 'UC' + 'z' * 22
        with self.assertRaises(ValueError):
            self.sync().resolve_uploads([CHANNEL, other], refresh=True)
        self.assertEqual(self.state.meta('upload_playlist_ids'), before)

    def test_reconcile_uses_video_dates_already_present_in_playlist_response(self):
        self.assertEqual(self.sync().run(topic_name='chess'), 0)
        topic = self.state.topic('chess')
        entry = self.server.items[topic['playlist_id']][0]
        entry['contentDetails'] = dict(videoPublishedAt=self.server.video_data['v1']['snippet']['publishedAt'])
        entry['snippet']['title'] = 'Updated title'
        # Even with no previous saved metadata, the playlist response is sufficient.
        with self.state.db:
            self.state.db.execute('DELETE FROM items')
        self.server.calls.clear()
        self.assertEqual(self.sync().run(topic_name='chess', reconcile_only=True), 0)
        self.assertFalse(any(resource == 'videos' for resource, method, p in self.server.calls))
        self.assertEqual(self.state.items(topic['id'])[0]['title'], 'Updated title')
        self.assertEqual(self.state.items(topic['id'])[0]['published_at'], entry['contentDetails']['videoPublishedAt'])

    def test_reconcile_resolves_only_unknown_dates(self):
        self.assertEqual(self.sync().run(topic_name='chess'), 0)
        topic = self.state.topic('chess')
        self.server.video_data['v2'] = dict(self.server.video_data['v1'], id='v2')
        self.server.items[topic['playlist_id']].append(dict(id='manual', snippet={'resourceId': {'videoId': 'v2'}}))
        self.server.calls.clear()
        self.assertEqual(self.sync().run(topic_name='chess', reconcile_only=True), 0)
        self.assertEqual([p['id'] for r, m, p in self.server.calls if r == 'videos'], ['v2'])

    def test_breakdown_counts_failed_dispatched_calls_but_not_local_rejections(self):
        sync = self.sync(budget=51)
        sync.api.call('channels', part='id', mine=True)
        self.server.failure = ('playlists', 'insert', False, http_error(403, 'forbidden'))
        with self.assertRaises(ApiFailure):
            sync.api.call('playlists', 'insert', body={})
        with self.assertRaises(QuotaStop):
            sync.api.call('channels', part='id', mine=True)
        usage = {(row['resource'], row['method']): row for row in self.state.api_usage(sync.api.day())}
        self.assertEqual(usage[('channels', 'list')]['calls'], 1)
        self.assertEqual(usage[('playlists', 'insert')]['units'], 50)
        self.assertEqual(self.state.quota(sync.api.day())['used'], 51)
