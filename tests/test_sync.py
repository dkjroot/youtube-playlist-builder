"""Exercise real state, scheduling and budget code against a fake YouTube server."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from googleapiclient.errors import HttpError
from httplib2 import Response

from youtube_playlist_builder.api import YouTube, ApiFailure, QuotaStop
from youtube_playlist_builder.state import State
from youtube_playlist_builder.sync import Sync
from youtube_playlist_builder.schedule import next_due, due_topics

CHANNEL = 'UC' + 'a' * 22
NOW = datetime(2026, 10, 28, 15, tzinfo=timezone.utc)


def http_error(status, reason):
    return HttpError(Response({'status': str(status)}), json.dumps({'error': {'errors': [{'reason': reason}]}}).encode())


class FakeYouTube:
    def __init__(self):
        self.owner = 'owner'
        self.video_data = {'v1': {'id': 'v1', 'snippet': {'title': 'Chess lesson', 'channelId': CHANNEL,
                       'publishedAt': (NOW - timedelta(days=1)).isoformat()}, 'status': {'privacyStatus': 'public'}}}
        self.playlist_data, self.items, self.calls = {}, {}, []
        self.failure = None
        self.upload_pages = None
        self.count = 0

    def __getattr__(self, resource):
        def make_resource():
            def request(method, **parameters):
                return SimpleNamespace(execute=lambda num_retries=0: self.execute(resource, method, parameters))
            return SimpleNamespace(**{method: (lambda method=method, **parameters: request(method, **parameters))
                                      for method in ('list', 'insert', 'update', 'delete')})
        return make_resource

    def execute(self, resource, method, parameters):
        self.calls.append((resource, method, parameters))
        failure = self.failure if self.failure and self.failure[:2] == (resource, method) else None
        if failure:
            self.failure = None
            if not failure[2]:
                raise failure[3]
        result = self.handle(resource, method, parameters)
        if failure:
            raise failure[3]
        return result

    def handle(self, resource, method, p):
        if resource == 'channels':
            if p.get('mine'):
                return {'items': [{'id': self.owner}]}
            return {'items': [{'id': CHANNEL, 'contentDetails': {'relatedPlaylists': {'uploads': 'uploads'}}}]}
        if resource == 'subscriptions':
            return {'items': [{'snippet': {'title': 'Chess channel', 'resourceId': {'channelId': CHANNEL}}}]}
        if resource == 'videos':
            return {'items': [self.video_data[key] for key in p['id'].split(',') if key in self.video_data]}
        if resource == 'playlists':
            if method == 'list':
                return {'items': list(self.playlist_data.values()) if p.get('mine') else
                        [self.playlist_data[p['id']]] if p['id'] in self.playlist_data else []}
            if method == 'insert':
                self.count += 1
                key = f'PL{self.count}'
                result = dict(p['body'], id=key)
                result['snippet'] = dict(result['snippet'], channelId=self.owner)
                self.playlist_data[key] = result
                self.items[key] = []
                return result
            if method == 'update':
                result = dict(p['body'])
                result['snippet'] = dict(result['snippet'], channelId=self.owner)
                self.playlist_data[result['id']] = result
                return result
        if resource == 'playlistItems':
            if method == 'list':
                if p['playlistId'] == 'uploads':
                    if self.upload_pages is not None:
                        return self.upload_pages[int(p.get('pageToken', '0'))]
                    return {'items': [{'contentDetails': {'videoId': key, 'videoPublishedAt': item['snippet']['publishedAt']}}
                                      for key, item in self.video_data.items()]}
                return {'items': list(self.items[p['playlistId']])}
            if method == 'insert':
                self.count += 1
                item = {'id': f'item{self.count}', 'snippet': p['body']['snippet']}
                self.items[item['snippet']['playlistId']].insert(item['snippet'].get('position', 0), item)
                return item
            if method == 'delete':
                for items in self.items.values():
                    for item in items:
                        if item['id'] == p['id']:
                            items.remove(item)
                            return {}
                raise http_error(404, 'playlistItemNotFound')
        raise AssertionError((resource, method, p))


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'state.sqlite3'
        self.state = State(self.path)
        self.addCleanup(lambda: self.state.close())
        self.server = FakeYouTube()
        self.now = NOW
        self.messages = []

    def topic(self, name='chess', **kwargs):
        return self.state.add(name, channels=[CHANNEL], **kwargs)

    def sync(self, budget=9500, daily=9500):
        api = YouTube(self.server, self.state, run_budget=budget, daily_budget=daily, now=lambda: self.now)
        return Sync(api, self.state, now=lambda: self.now, report=self.messages.append)

    def writes(self):
        return [call for call in self.server.calls if call[1] != 'list']

    def test_every_time_runs_once_per_invocation_and_finds_same_day_uploads(self):
        topic = self.topic('news', frequency='every-time')
        self.topic('daily')
        self.topic('disabled', frequency='every-time', enabled=False)
        self.assertEqual(self.sync().run(), 0)
        self.assertEqual(len(self.state.items(topic['id'])), 1)
        self.assertEqual(sum(m.startswith('Updated news:') for m in self.messages), 1)
        self.assertIsNone(self.state.topic('disabled')['last_success'])
        self.assertNotIn(str(topic['id']), self.state.meta('schedule_slots', {}))
        self.server.calls.clear()
        self.messages.clear()
        self.now += timedelta(hours=2)
        self.server.video_data['v2'] = {
            'id': 'v2', 'snippet': {'title': 'Breaking news', 'channelId': CHANNEL,
                                  'publishedAt': self.now.isoformat()},
            'status': {'privacyStatus': 'public'}}
        self.assertEqual(self.sync().run(), 0)
        self.assertEqual(len(self.state.items(topic['id'])), 2)
        self.assertEqual(sum(m.startswith('Updated news:') for m in self.messages), 1)
        self.assertEqual(self.state.topic('daily')['last_success'], NOW.isoformat())
        self.assertEqual(len(self.writes()), 1)
        self.server.calls.clear()
        self.assertEqual(self.sync().run(), 0)
        self.assertTrue(self.server.calls)
        self.assertEqual(self.writes(), [])

    def test_every_time_still_pauses_for_quota_and_preview_preserves_state(self):
        self.topic('news', frequency='every-time')
        self.assertEqual(self.sync().run(), 0)
        snapshot = self.state.topics()
        self.assertEqual(self.sync().run(preview=True), 0)
        self.assertEqual(self.state.topics(), snapshot)
        self.assertEqual(self.sync(budget=1).run(), 2)

    def test_stagger_ten_two_day_topics_and_forced_all_retains_phases(self):
        for index in range(10):
            self.topic(f'topic {index}', frequency='every-2-days')
        self.assertEqual(self.sync().run(), 0)
        from collections import Counter
        before = {row['id']: row['next_due'] for row in self.state.topics()}
        self.assertEqual(sorted(Counter(before.values()).values()), [5, 5])
        self.server.calls.clear()
        self.assertEqual(self.sync().run(), 0)
        self.assertEqual(self.server.calls, [])
        self.assertEqual(self.sync().run(all_topics=True), 0)
        self.assertTrue(self.server.calls)
        self.assertEqual(before, {row['id']: row['next_due'] for row in self.state.topics()})
        self.now += timedelta(days=1)
        self.assertEqual(self.sync().run(), 0)
        tomorrow = self.now + timedelta(days=1)
        self.assertEqual(len(due_topics(self.state.topics(), 0, tomorrow)), 5)

    def test_all_ignores_frequency_but_not_disabled_and_preview_does_not_reschedule(self):
        self.topic('enabled', frequency='weekly')
        disabled = self.topic('disabled', enabled=False)
        self.sync().run()
        snapshot = self.state.topics()
        slots = self.state.meta('schedule_slots')
        self.assertEqual(self.sync().run(all_topics=True, preview=True), 0)
        self.assertEqual(self.state.topics(), snapshot)
        self.assertEqual(self.state.meta('schedule_slots'), slots)
        self.now += timedelta(hours=1)
        self.assertEqual(self.sync().run(all_topics=True), 0)
        self.assertEqual(self.state.topic('enabled')['last_success'], self.now.isoformat())
        self.assertIsNone(self.state.by_id(disabled['id'])['last_success'])

    def test_all_quota_pause_preserves_rotation_and_incomplete_due_state(self):
        for name in ('first', 'second', 'third'):
            self.topic(name, frequency='weekly')
        self.assertEqual(self.sync(budget=155).run(all_topics=True), 2)
        self.assertIsNone(self.state.topic('first')['last_success'])
        self.assertIsNone(self.state.topic('second')['next_due'])
        self.assertEqual(self.state.meta('cursor'), self.state.topic('first')['id'])
        self.assertEqual(self.sync().run(all_topics=True), 0)
        self.assertTrue(all(row['last_success'] for row in self.state.topics()))

    def test_minimum_length_filters_new_videos_before_llm_and_retains_existing(self):
        from copy import deepcopy
        from unittest.mock import Mock
        self.topic()
        self.server.video_data['v1']['contentDetails'] = {'duration': 'PT30S'}
        self.assertEqual(self.sync().run(), 0)
        for vid, duration in [('short', 'PT3M'), ('boundary', 'PT3M1S'), ('long', 'PT20M'), ('unknown', None)]:
            self.server.video_data[vid] = deepcopy(self.server.video_data['v1'])
            self.server.video_data[vid]['id'] = vid
            self.server.video_data[vid]['contentDetails'] = {'duration': duration}
        self.state.set_meta('minimum_video_seconds', 181)
        classifier = Mock()
        classifier.match.side_effect = lambda topic, videos: videos
        sync = self.sync()
        sync.classifier = classifier
        self.assertEqual(sync.run(topic_name='chess'), 0)
        self.assertEqual(set(classifier.match.call_args.args[1]), {'boundary', 'long'})
        items = self.state.items(self.state.topic('chess')['id'])
        self.assertEqual({item['video_id'] for item in items}, {'v1', 'boundary', 'long'})
        self.now += timedelta(days=8)
        self.assertEqual(self.sync().run(), 0)
        self.assertEqual(self.state.items(self.state.topic('chess')['id']), [])

    def test_minimum_length_preview_and_disabled_filter(self):
        self.topic()
        self.state.set_meta('minimum_video_seconds', 181)
        self.server.video_data['v1']['contentDetails'] = {'duration': 'PT1M'}
        self.assertEqual(self.sync().run(preview=True), 0)
        self.assertEqual(self.writes(), [])
        self.assertTrue(any('add 0' in message for message in self.messages))
        self.state.set_meta('minimum_video_seconds', 0)
        self.assertEqual(self.sync().run(), 0)
        self.assertEqual(len(self.state.items(self.state.topic('chess')['id'])), 1)

    def test_create_private_once_and_repeat_without_writes_or_playlist_reads(self):
        self.topic()
        self.assertEqual(self.sync().run(), 0)
        self.assertEqual(len(self.writes()), 2)
        self.assertEqual(next(iter(self.server.playlist_data.values()))['status']['privacyStatus'], 'private')
        self.assertEqual(next(iter(self.server.playlist_data.values()))['snippet']['title'], 'sub_chess')
        self.server.calls.clear()
        self.assertEqual(self.sync().run(topic_name='chess'), 0)
        self.assertEqual(self.writes(), [])
        self.assertFalse(any(call[0] == 'playlists' for call in self.server.calls))
        self.assertEqual(len(self.state.items(self.state.topic('chess')['id'])), 1)

    def test_rotation_resumes_other_topics_after_partial_update_and_restart(self):
        for name in ('chess', 'engineering', 'piano'):
            self.topic(name)
        self.assertEqual(self.sync(budget=155).run(), 2)
        self.assertIsNone(self.state.topic('chess')['last_success'])
        self.assertIsNone(self.state.topic('engineering')['last_success'])
        self.assertIsNotNone(self.state.topic('engineering')['playlist_id'])
        self.assertEqual(self.state.meta('cursor'), 1)
        self.state.close()
        self.state = State(self.path)
        queue = due_topics(self.state.topics(), self.state.meta('cursor'), self.now)
        self.assertEqual([topic['name'] for topic in queue], ['engineering', 'piano', 'chess'])
        self.assertEqual(self.sync(budget=200).run(), 0)
        self.assertEqual(len(self.server.playlist_data), 3)
        self.assertTrue(all(topic['last_success'] for topic in self.state.topics()))

    def test_expiration_uses_saved_item_id_without_reading_playlist(self):
        topic = self.topic()
        self.sync().run()
        self.now += timedelta(days=7)
        # Keep reconciliation recent to isolate the normal expiry path.
        self.state.update(topic['id'], reconciled_at=self.now.isoformat())
        self.server.calls.clear()
        self.assertEqual(self.sync().run(), 0)
        self.assertEqual(self.state.items(topic['id']), [])
        self.assertEqual([call[1] for call in self.writes()], ['delete'])
        self.assertFalse(any(call[0] == 'playlists' for call in self.server.calls))

    def test_lost_insert_response_recovers_without_duplicate(self):
        topic = self.topic()
        self.server.failure = ('playlistItems', 'insert', True, TimeoutError())
        self.assertEqual(self.sync().run(), 1)
        self.assertIsNotNone(self.state.pending(topic['id']))
        self.assertEqual(self.sync().run(), 0)
        self.assertEqual(len(next(iter(self.server.items.values()))), 1)
        self.assertIsNone(self.state.pending(topic['id']))

    def test_lost_creation_response_finds_unique_marker(self):
        topic = self.topic()
        self.server.failure = ('playlists', 'insert', True, TimeoutError())
        self.assertEqual(self.sync().run(), 1)
        self.assertIsNone(self.state.topic('chess')['playlist_id'])
        self.assertEqual(self.sync().run(), 0)
        self.assertEqual(len(self.server.playlist_data), 1)
        self.assertIsNone(self.state.pending(topic['id']))

    def test_no_match_for_uncertain_creation_does_not_retry_create(self):
        self.topic()
        self.server.failure = ('playlists', 'insert', False, TimeoutError())
        self.assertEqual(self.sync().run(), 1)
        self.assertEqual(self.sync().run(), 1)
        self.assertEqual(len([call for call in self.writes() if call[0] == 'playlists']), 1)

    def test_real_quota_error_stops_until_next_pacific_day(self):
        topic = self.topic()
        self.server.failure = ('playlists', 'insert', False, http_error(403, 'quotaExceeded'))
        self.assertEqual(self.sync().run(), 2)
        self.assertIsNone(self.state.pending(topic['id']))
        previous = len(self.server.calls)
        self.assertEqual(self.sync().run(), 2)
        self.assertEqual(len(self.server.calls), previous)
        self.now += timedelta(days=1)
        self.assertEqual(self.sync().run(), 0)

    def test_nonquota_403_is_reported_without_exhausting_daily_budget(self):
        self.topic()
        self.server.failure = ('playlists', 'insert', False, http_error(403, 'forbidden'))
        self.assertEqual(self.sync().run(), 1)
        self.assertEqual(self.state.quota('2026-10-28')['exhausted'], 0)

    def test_preview_and_disabled_auto_do_not_write_or_advance_queue(self):
        self.topic(enabled=False)
        self.assertEqual(self.sync().run(), 0)
        self.assertEqual(self.server.calls, [])
        self.assertEqual(self.sync().run(topic_name='chess', preview=True), 0)
        self.assertEqual(self.writes(), [])
        self.assertIsNone(self.state.topic('chess')['last_attempt'])
        self.assertEqual(self.state.meta('cursor', 0), 0)

    def test_explicit_update_runs_disabled_not_due_topic(self):
        self.topic(enabled=False, next_due=(NOW + timedelta(days=30)).isoformat())
        self.assertEqual(self.sync().run(topic_name='chess'), 0)
        self.assertEqual(len(self.server.playlist_data), 1)

    def test_different_account_stops_before_writes(self):
        self.topic()
        self.state.set_meta('owner_channel_id', 'different')
        with self.assertRaisesRegex(ValueError, 'different YouTube account'):
            self.sync().run()
        self.assertEqual(self.writes(), [])

    def test_failed_video_read_does_not_remove_current_nonexpired_items(self):
        self.topic()
        self.sync().run()
        self.server.calls.clear()
        self.server.failure = ('videos', 'list', False, http_error(500, 'backendError'))
        self.assertEqual(self.sync().run(topic_name='chess'), 1)
        self.assertEqual(self.writes(), [])

    def test_title_filters_and_old_video_in_first_page(self):
        self.topic(include_words=['lesson'], exclude_words=['short'])
        self.server.video_data['old'] = {'id': 'old', 'snippet': {'title': 'old lesson', 'channelId': CHANNEL,
                                  'publishedAt': (NOW - timedelta(days=30)).isoformat()}, 'status': {}}
        self.server.upload_pages = [
            {'items': [{'contentDetails': {'videoId': 'old', 'videoPublishedAt': (NOW-timedelta(days=30)).isoformat()}},
                       {'contentDetails': {'videoId': 'v1', 'videoPublishedAt': NOW.isoformat()}}], 'nextPageToken': '1'},
            {'items': []},
        ]
        self.assertEqual(self.sync().run(), 0)
        self.assertEqual(len([call for call in self.server.calls if call[0] == 'playlistItems' and call[1] == 'list']), 2)
        self.assertEqual([item['video_id'] for item in self.state.items(1)], ['v1'])

    def test_rename_updates_only_managed_playlist(self):
        topic = self.topic()
        self.sync().run()
        self.state.update(topic['id'], playlist_name='My favourites')
        self.server.calls.clear()
        self.assertEqual(self.sync().run(topic_name='chess'), 0)
        self.assertEqual([call[1] for call in self.writes()], ['update'])
        self.assertEqual(next(iter(self.server.playlist_data.values()))['snippet']['title'], 'My favourites')

    def test_reconcile_repairs_manually_removed_item(self):
        self.topic()
        self.sync().run()
        next(iter(self.server.items.values())).clear()
        self.assertEqual(self.sync().run(topic_name='chess', reconcile_only=True), 0)
        self.assertEqual(self.state.items(1), [])
        self.assertEqual(self.sync().run(topic_name='chess'), 0)
        self.assertEqual(len(self.state.items(1)), 1)

    def test_local_daily_budget_persists_across_runs(self):
        self.topic()
        self.assertEqual(self.sync(daily=105).run(), 0)
        self.assertEqual(self.sync(daily=105).run(topic_name='chess'), 2)


class ScheduleTests(unittest.TestCase):
    def test_intervals(self):
        for frequency, days in [('daily', 1), ('every-2-days', 2), ('every-4-days', 4), ('weekly', 7), ('biweekly', 14)]:
            self.assertEqual(next_due(NOW, frequency), (NOW + timedelta(days=days)).replace(hour=0))

    def test_daily_is_due_next_calendar_day_even_if_yesterdays_run_finished_later(self):
        finished = NOW.replace(hour=10, second=23)
        tomorrow_start = (NOW + timedelta(days=1)).replace(hour=10)
        self.assertLess(next_due(finished, 'daily'), tomorrow_start)

    def test_local_midnight_handles_daylight_saving(self):
        summer = datetime(2026, 7, 1, 20, tzinfo=timezone.utc)
        self.assertEqual(next_due(summer, 'daily', 'Europe/London'), datetime(2026, 7, 1, 23, tzinfo=timezone.utc))

    def test_calendar_month_and_leap_year(self):
        self.assertEqual(next_due(datetime(2028, 1, 31, tzinfo=timezone.utc), 'monthly').day, 29)
        self.assertEqual(next_due(datetime(2027, 1, 31, tzinfo=timezone.utc), 'monthly').day, 28)
        self.assertEqual(next_due(datetime(2026, 12, 28, tzinfo=timezone.utc), 'monthly').year, 2027)

    def test_rotation_skips_disabled_and_not_due_and_wraps(self):
        topics = [dict(id=i, enabled=i != 2, next_due=None if i != 3 else (NOW+timedelta(days=1)).isoformat()) for i in range(1, 6)]
        self.assertEqual([t['id'] for t in due_topics(topics, 4, NOW)], [5, 1, 4])
