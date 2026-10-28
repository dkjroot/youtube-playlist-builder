"""Count real gateway calls against fake feeds, including overlapping lookback windows."""
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from youtube_playlist_builder.api import YouTube
from youtube_playlist_builder.state import State
from youtube_playlist_builder.sync import Sync
from test_sync import FakeYouTube, NOW, CHANNEL


class ManyChannels(FakeYouTube):
    """Forty quiet channels, each with one recent and one expired video."""
    def __init__(self):
        super().__init__()
        self.channel_ids = ['UC' + str(i).zfill(22) for i in range(40)]
        self.video_data = {}
        for i, channel in enumerate(self.channel_ids):
            for label, age in [('recent', 1), ('old', 100)]:
                key = f'{label}{i}'
                self.video_data[key] = {'id': key, 'snippet': {'channelId': channel,
                    'title': key, 'publishedAt': (NOW - timedelta(days=age)).isoformat()},
                    'status': {'privacyStatus': 'public'}}

    def handle(self, resource, method, p):
        if resource == 'subscriptions':
            return {'items': [{'snippet': {'title': channel, 'resourceId': {'channelId': channel}}}
                              for channel in self.channel_ids]}
        if resource == 'channels' and not p.get('mine'):
            return {'items': [{'id': channel, 'contentDetails': {'relatedPlaylists': {'uploads': channel}}}
                              for channel in p['id'].split(',')]}
        if resource == 'playlistItems' and method == 'list' and p['playlistId'] in self.channel_ids:
            return {'items': [{'contentDetails': {'videoId': key, 'videoPublishedAt': video['snippet']['publishedAt']}}
                              for key, video in self.video_data.items() if video['snippet']['channelId'] == p['playlistId']]}
        return super().handle(resource, method, p)


class QuotaEfficiencyTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = State(Path(directory.name)/'state.db')
        self.addCleanup(self.state.close)

    def sync(self, server):
        api = YouTube(server, self.state, now=lambda: NOW)
        return Sync(api, self.state, now=lambda: NOW, report=lambda _: None)

    def test_cross_channel_batching_reduces_83_reads_to_44(self):
        server = ManyChannels()
        self.state.add('all', channels=server.channel_ids)
        sync = self.sync(server)
        self.assertEqual(sync.run(preview=True), 0)
        detail_reads = [p for resource, method, p in server.calls if resource == 'videos']
        self.assertEqual(len(detail_reads), 1)
        self.assertEqual(len(detail_reads[0]['id'].split(',')), 40)
        self.assertTrue(all(key.startswith('recent') for key in detail_reads[0]['id'].split(',')))
        self.assertEqual(sync.api.spent, 44)  # owner + subscriptions + channels + 40 feeds + metadata

    def test_wider_topic_extends_cached_pages_without_rereading(self):
        server = FakeYouTube()
        for i, age in enumerate((1, 5, 10), 1):
            server.video_data[f'v{i}'] = {'id': f'v{i}', 'snippet': {'title': 'Chess', 'channelId': CHANNEL,
                'publishedAt': (NOW - timedelta(days=age)).isoformat()}, 'status': {'privacyStatus': 'public'}}
        server.upload_pages = []
        for i in range(1, 4):
            page = {'items': [{'contentDetails': {'videoId': f'v{i}',
                             'videoPublishedAt': server.video_data[f'v{i}']['snippet']['publishedAt']}}]}
            if i < 3:
                page['nextPageToken'] = str(i)
            server.upload_pages.append(page)
        narrow = self.state.add('narrow', channels=[CHANNEL], max_age_days=2)
        wide = self.state.add('wide', channels=[CHANNEL], max_age_days=7)
        sync = self.sync(server)
        self.assertEqual(set(sync.recent_videos(narrow, NOW)), {'v1'})
        self.assertEqual(set(sync.recent_videos(wide, NOW)), {'v1', 'v2'})
        self.assertEqual(set(sync.recent_videos(narrow, NOW)), {'v1'})
        feed_reads = [p for resource, method, p in server.calls if resource == 'playlistItems']
        self.assertEqual(len(feed_reads), 3)
        self.assertEqual([p.get('pageToken') for p in feed_reads], [None, '1', '2'])
        self.assertEqual([p['id'] for resource, method, p in server.calls if resource == 'videos'], ['v1', 'v2'])

    def test_missing_ids_cached_only_within_run_and_unknown_dates_resolved(self):
        server = FakeYouTube()
        server.upload_pages = [{'items': [{'contentDetails': {'videoId': 'v1'}},
                                         {'contentDetails': {'videoId': 'missing'}}]}]
        topic = self.state.add('chess', channels=[CHANNEL])
        sync = self.sync(server)
        self.assertEqual(set(sync.recent_videos(topic, NOW)), {'v1'})
        self.assertEqual(sync.details(['missing']), {})
        self.assertEqual(len([call for call in server.calls if call[0] == 'videos']), 1)
        # A new invocation refreshes status, rather than keeping negative results forever.
        server.video_data['missing'] = dict(server.video_data['v1'], id='missing')
        self.assertEqual(set(self.sync(server).recent_videos(topic, NOW)), {'v1', 'missing'})

    def test_metadata_batches_respect_fifty_id_limit(self):
        server = FakeYouTube()
        original = server.video_data['v1']
        server.video_data = {f'video{i:03}': dict(original, id=f'video{i:03}') for i in range(51)}
        sync = self.sync(server)
        self.assertEqual(len(sync.details(server.video_data)), 51)
        reads = [p for resource, method, p in server.calls if resource == 'videos']
        self.assertEqual([len(p['id'].split(',')) for p in reads], [50, 1])
        self.assertEqual(sync.api.spent, 2)

    def test_expired_feed_needs_no_video_details(self):
        server = FakeYouTube()
        server.video_data['v1']['snippet']['publishedAt'] = (NOW - timedelta(days=30)).isoformat()
        topic = self.state.add('chess', channels=[CHANNEL], max_age_days=7)
        self.assertEqual(self.sync(server).recent_videos(topic, NOW), {})
        self.assertFalse(any(resource == 'videos' for resource, _, _ in server.calls))

    def test_reconciliation_metadata_is_reused_for_discovery(self):
        server = FakeYouTube()
        topic = self.state.add('chess', channels=[CHANNEL])
        self.sync(server).run()
        self.state.update(topic['id'], reconciled_at=None)
        server.calls.clear()
        self.assertEqual(self.sync(server).run(topic_name='chess'), 0)
        self.assertEqual(len([call for call in server.calls if call[0] == 'videos']), 1)
        self.assertFalse(any(method != 'list' for _, method, _ in server.calls))
