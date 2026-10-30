"""Every managed endpoint must stop on quota; uncertain failures retain write intent."""
import json
from unittest import TestCase
from unittest.mock import patch
from googleapiclient.errors import HttpError
from httplib2 import Response

from youtube_playlist_builder.api import ApiFailure, QuotaStop, error_reasons
from youtube_playlist_builder.diagnostics import logger
import test_sync as fixtures
from test_sync import http_error, NOW


def response_error(status, error):
    return HttpError(Response({'status':str(status)}), json.dumps({'error':error}).encode())


class ApiErrorTests(TestCase):
    setUp = fixtures.SyncTests.setUp
    topic = fixtures.SyncTests.topic
    sync = fixtures.SyncTests.sync
    writes = fixtures.SyncTests.writes
    def test_rpc_quota_errors_block_every_resource_and_an_increased_budget(self):
        for resource, method in [('channels','list'), ('subscriptions','list'), ('playlistItems','list'),
                                 ('videos','list'), ('playlists','insert'), ('playlists','update'), ('playlistItems','insert'), ('playlistItems','delete')]:
            with self.subTest(resource=resource, method=method):
                self.state.db.execute('DELETE FROM quota')
                self.state.db.commit()
                self.server.failure = (resource, method, False, response_error(403, {
                    'status':'RESOURCE_EXHAUSTED', 'details':[{'reason':'QUOTA_EXCEEDED'}]}))
                api = self.sync().api
                with self.assertRaisesRegex(QuotaStop, 'daily quota'):
                    api.call(resource, method)
                self.assertEqual(self.state.quota(api.day())['exhausted'], 1)
                previous = len(self.server.calls)
                with self.assertRaises(QuotaStop):
                    self.sync(budget=11000, daily=11000).api.call(resource, method)
                self.assertEqual(len(self.server.calls), previous)

    def test_delete_quota_stops_whole_queue_and_keeps_local_item(self):
        topic = self.topic('politics')
        other = self.topic('later')
        self.assertEqual(self.sync().run(), 0)
        before = self.state.items(topic['id'])
        self.server.video_data.clear()
        self.server.calls.clear()
        self.server.failure = ('playlistItems','delete',False,http_error(403,'quotaExceeded'))
        self.state.set_meta('cursor', 0)
        self.assertEqual(self.sync().run(all_topics=True), 2)
        self.assertEqual(self.state.items(topic['id']), before)
        self.assertIsNone(self.state.pending(topic['id']))
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.state.topic('later')['last_success'], other.get('last_success') or NOW.isoformat())

    @patch("youtube_playlist_builder.sync.time.sleep")
    def test_transient_delete_recovers_and_reconciles_before_resuming(self, sleep):
        topic = self.topic('politics')
        self.topic('later')
        self.assertEqual(self.sync().run(), 0)
        self.server.video_data.clear()
        self.server.calls.clear()
        # Simulate an applied deletion whose server replied 409 SERVICE_UNAVAILABLE.
        self.server.failure = ('playlistItems','delete',True,http_error(409,'SERVICE_UNAVAILABLE'))
        self.state.set_meta('cursor', 0)
        self.assertEqual(self.sync().run(all_topics=True), 0)
        sleep.assert_called_once_with(1)
        self.assertIsNone(self.state.pending(topic['id']))
        # Missing item from the uncertain delete is reconciled, not deleted again.
        deleted = [call[2]['id'] for call in self.writes() if call[1] == 'delete']
        self.assertEqual(len(deleted), len(set(deleted)))

    def test_delete_timeout_stops_without_retry_or_losing_intent(self):
        topic = self.topic('politics')
        self.topic('later')
        self.assertEqual(self.sync().run(), 0)
        self.server.video_data.clear()
        self.server.calls.clear()
        self.server.failure = ('playlistItems','delete',False,TimeoutError())
        self.state.set_meta('cursor', 0)
        self.assertEqual(self.sync().run(all_topics=True), 1)
        self.assertEqual(len(self.writes()), 1)
        self.assertIsNotNone(self.state.pending(topic['id']))

    def test_definite_400_is_not_retried_in_the_same_run(self):
        topic = self.topic('politics')
        self.topic('later')
        self.assertEqual(self.sync().run(), 0)
        before = self.state.items(topic['id'])
        self.server.video_data.clear()
        self.server.calls.clear()
        self.server.failure = ('playlistItems','delete',False,http_error(400,'invalidValue'))
        self.state.set_meta('cursor', 0)
        self.assertEqual(self.sync().run(all_topics=True), 1)
        self.assertEqual(self.state.items(topic['id']), before)
        self.assertIsNone(self.state.pending(topic['id']))
        self.assertEqual(len(self.writes()), 2)  # One failure, then the other playlist's delete.
        self.assertEqual(len({call[2]['id'] for call in self.writes()}), 2)

    def test_errors_have_structured_private_logs_without_request_payload(self):
        api = self.sync().api
        self.server.failure = ('playlistItems','delete',False,http_error(400,'invalidValue'))
        with self.assertLogs(logger, level='INFO') as logs:
            with self.assertRaises(ApiFailure):
                api.call('playlistItems','delete', id='private-playlist-item')
        text = '\n'.join(logs.output)
        self.assertIn('resource=playlistItems method=delete', text)
        self.assertIn('HTTP=400', text)
        self.assertNotIn('private-playlist-item', text)

    def test_rate_errors_stop_without_marking_daily_exhaustion(self):
        for error in [http_error(429,'unknown'), response_error(403,{'status':'RESOURCE_EXHAUSTED'}),
                      response_error(400,{'details':[{'reason':'RATE_LIMIT_EXCEEDED'}]})]:
            self.server.failure = ('channels','list',False,error)
            with self.assertRaises(QuotaStop):
                self.sync().api.call('channels',mine=True)
            self.assertFalse(self.state.quota(self.sync().api.day())['exhausted'])


class ErrorReasonTests(TestCase):
    def test_malformed_error_shapes_do_not_mask_http_failures(self):
        for content in [b'not json', b'[]', b'{"error":[]}', b'{"error":{"errors":[null,"x"],"details":null}}']:
            self.assertEqual(error_reasons(content), [])
        self.assertFalse(ApiFailure(400,'badRequest').uncertain)
        for status, reason in [(409,'SERVICE_UNAVAILABLE'), (408,'timeout'), (503,'unknown')]:
            self.assertTrue(ApiFailure(status,reason).uncertain)
            self.assertTrue(ApiFailure(status,reason).stop_run)
