"""No network or paid model calls: exercise the real Iris client via HTTP mocks."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from youtube_playlist_builder.llm import Classifier, settings
from youtube_playlist_builder.state import State
from youtube_playlist_builder.api import YouTube, QuotaStop
from youtube_playlist_builder.sync import Sync
from youtube_playlist_builder.cli import main
from test_sync import FakeYouTube, CHANNEL, NOW


class LLMTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / 'llm.json'
        self.path.write_text(json.dumps({'base_url': 'http://localhost:5001/v1', 'model': 'test', 'max_calls': 2}))
        self.config = settings(self.path)
        self.state = State(self.root / 'state.sqlite3')
        self.addCleanup(self.state.close)
        self.topic = self.state.add('chess', channels=[CHANNEL])
        self.requests = []
        self.responses = []
        def respond(request):
            self.requests.append(json.loads(request.content))
            result = self.responses.pop(0)
            if isinstance(result, int):
                return httpx.Response(result, text='secret-provider-body')
            return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': json.dumps(result)}}]})
        self.client = httpx.Client(transport=httpx.MockTransport(respond))
        self.addCleanup(self.client.close)
        self.classifier = Classifier(self.state, self.config, self.client)
        self.videos = {'v1': dict(video_id='v1', title='A chess lesson', description='Opening principles')}

    def decision(self, value='yes', vid='v1'):
        return {'decisions': [{'id': vid, 'match': value}]}

    def test_cache_survives_new_client_and_metadata_changes_invalidate(self):
        self.responses = [self.decision(), self.decision('no')]
        self.assertEqual(self.classifier.match(self.topic, self.videos), self.videos)
        restarted = Classifier(self.state, self.config, self.client)
        self.assertEqual(restarted.match(self.topic, self.videos), self.videos)
        self.assertEqual(restarted.calls, 0)
        self.videos['v1']['description'] = 'Actually a cooking lesson'
        self.assertEqual(restarted.match(self.topic, self.videos), {})
        self.assertEqual(len(self.requests), 2)

    def test_topic_definition_and_model_invalidate(self):
        self.responses = [self.decision()] * 3
        self.classifier.match(self.topic, self.videos)
        changed = dict(self.topic, description='Chess tournaments only')
        self.classifier.match(changed, self.videos)
        self.config.model = 'different-model'
        restarted = Classifier(self.state, self.config, self.client)
        restarted.match(changed, self.videos)
        self.assertEqual(len(self.requests), 3)

    def test_new_video_only_sends_uncached_metadata(self):
        self.responses = [self.decision(), self.decision(vid='v2')]
        self.classifier.match(self.topic, self.videos)
        self.videos['v2'] = dict(video_id='v2', title='More chess')
        self.classifier.match(self.topic, self.videos)
        payload = self.requests[-1]['messages'][-1]['content'].split('DATA:\n')[1]
        self.assertEqual([row['id'] for row in json.loads(payload)['videos']], ['v2'])

    def test_limit_stops_without_retry_and_resumes_cached_batches(self):
        self.config.max_calls = 1
        channels = [dict(channel_id=f'channel{i:03}', title='Pottery') for i in range(41)]
        self.responses = [{'topics': [{'name': 'pottery', 'channel_ids': ['channel000']}]},
                          {'topics': [{'name': 'pottery', 'channel_ids': ['channel040']}]}]
        with self.assertRaises(QuotaStop):
            self.classifier.suggest(channels)
        restarted = Classifier(self.state, self.config, self.client)
        result = restarted.suggest(channels)
        self.assertEqual(len(result[0]['channels']), 2)
        self.assertEqual(restarted.calls, 1)

    def test_daily_limit_persists_across_clients_and_cache_is_still_free(self):
        self.config.max_daily_calls = 1
        self.responses = [self.decision()]
        self.classifier.match(self.topic, self.videos)
        restarted = Classifier(self.state, self.config, self.client)
        self.assertEqual(restarted.match(self.topic, self.videos), self.videos)
        changed = dict(self.topic, description='Something new')
        with self.assertRaisesRegex(QuotaStop, 'daily LLM'):
            restarted.match(changed, self.videos)
        self.assertEqual(len(self.requests), 1)

    def test_zero_and_default_limits_allow_calls_but_keep_accounting(self):
        from datetime import datetime, timezone
        self.path.write_text(json.dumps({'base_url':'http://localhost:5001/v1', 'model':'test'}))
        config = settings(self.path)
        self.assertEqual((config.max_calls, config.max_daily_calls), (0, 0))
        unlimited = Classifier(self.state, config, self.client)
        unlimited.calls = 2000
        key = 'llm_calls:' + datetime.now(timezone.utc).date().isoformat()
        self.state.set_meta(key, 10000)
        self.responses = [self.decision()]
        unlimited.match(self.topic, self.videos)
        self.assertEqual(unlimited.calls, 2001)
        self.assertEqual(self.state.meta(key), 10001)

    def test_invalid_call_limits_still_rejected(self):
        for field, value in [('max_calls', -1), ('max_daily_calls', -1), ('max_calls', False)]:
            self.path.write_text(json.dumps({'base_url':'http://localhost:5001/v1', 'model':'test', field:value}))
            with self.assertRaises(ValueError):
                settings(self.path)

    def test_invalid_decisions_are_not_cached(self):
        self.responses = [self.decision(vid='invented'), self.decision()]
        with self.assertRaises(ValueError):
            self.classifier.match(self.topic, self.videos)
        self.assertEqual(self.classifier.match(self.topic, self.videos), self.videos)
        self.assertEqual(self.classifier.calls, 2)

    def test_the_is_not_a_topic(self):
        self.responses = [{'topics': [{'name': 'the', 'channel_ids': ['c']}]}]
        with self.assertRaises(ValueError):
            self.classifier.suggest([{'channel_id': 'c', 'title': 'The pottery channel'}])

    def test_uncertain_preserves_existing_but_does_not_add_new(self):
        self.responses = [self.decision('uncertain')]
        self.assertEqual(self.classifier.match(self.topic, self.videos), {})
        with self.state.db:
            self.state.put_item(self.topic['id'], dict(item_id='i1', video_id='v1'))
        self.assertEqual(self.classifier.match(self.topic, self.videos), self.videos)

    def test_provider_error_is_safe_and_no_automatic_retry(self):
        self.responses = [401]
        with self.assertRaisesRegex(ValueError, 'HTTP 401') as caught:
            self.classifier.match(self.topic, self.videos)
        self.assertNotIn('secret-provider-body', str(caught.exception))
        self.assertEqual(len(self.requests), 1)

    def test_missing_gemini_key_fails_before_network(self):
        self.path.write_text(json.dumps({'base_url': 'https://generativelanguage.googleapis.com/v1beta/openai',
            'model': 'test', 'allow_remote': True, 'api_key_env': 'GEMINI_API_KEY'}))
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'Gemini API key missing'):
                settings(self.path)

    def test_remote_requires_explicit_https_opt_in(self):
        self.path.write_text(json.dumps({'base_url': 'https://example.com/v1', 'model': 'test'}))
        with self.assertRaises(ValueError):
            settings(self.path)
        self.path.write_text(json.dumps({'base_url': 'https://example.com/v1', 'model': 'test', 'allow_remote': True}))
        self.assertEqual(settings(self.path).model, 'test')

    def test_sync_uses_model_and_invalid_response_keeps_existing(self):
        service = FakeYouTube()
        api = YouTube(service, self.state, now=lambda: NOW)
        self.assertEqual(Sync(api, self.state, now=lambda: NOW, report=lambda _: None).run(topic_name='chess'), 0)
        self.responses = [{'decisions': []}]
        sync = Sync(api, self.state, now=lambda: NOW, report=lambda _: None, classifier=self.classifier)
        self.assertEqual(sync.run(topic_name='chess'), 1)
        self.assertEqual(len(self.state.items(self.topic['id'])), 1)
        self.responses = [self.decision('no')]
        self.assertEqual(sync.run(topic_name='chess'), 0)
        self.assertEqual(self.state.items(self.topic['id']), [])

    def test_enabled_mode_routes_suggestions_through_llm(self):
        self.state.set_meta('mode', 'llm')
        export = self.root / 'subs.json'
        export.write_text(json.dumps([dict(channel_id=CHANNEL, title='Pottery')]))
        with patch('youtube_playlist_builder.cli.Classifier', return_value=self.classifier):
            self.responses = [{'topics': [{'name': 'pottery', 'channel_ids': [CHANNEL]}]}]
            self.assertEqual(main(['--state', str(self.state.path), '--llm-config', str(self.path),
                                   'suggest-topics', '--subscriptions', str(export)]), 0)
        self.assertEqual(self.classifier.calls, 1)
