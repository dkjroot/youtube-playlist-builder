import json
from pathlib import Path
import stat
import tempfile
import unittest
import httpx
from youtube_playlist_builder.diagnostics import configure, logger, provider_error, PrivateRotatingHandler


class DiagnosticsTests(unittest.TestCase):
    def test_error_details_redact_key_and_omit_unapproved_fields(self):
        response = httpx.Response(400, json={'error': {
            'code': 400, 'status': 'INVALID_ARGUMENT',
            'message': 'Bad setting. Bearer token123 api_key=secret123 supplied-key',
            'details': {'request': 'private prompt', 'token': 'private token'}}})
        text = provider_error(response, ['supplied-key'])
        for value in ('token123', 'secret123', 'supplied-key', 'private prompt', 'private token'):
            self.assertNotIn(value, text)
        self.assertIn('Bad setting', text)
        self.assertIn('INVALID_ARGUMENT', text)

    def test_google_array_error_is_read(self):
        response = httpx.Response(400, json=[{'error': {'code': 400, 'message': 'Missing Authorization header'}}])
        self.assertIn('Missing Authorization header', provider_error(response))

    def test_non_json_body_is_not_logged(self):
        self.assertNotIn('private', provider_error(httpx.Response(400, text='private content')))

    def test_rotated_files_are_private_and_reconfiguration_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'runtime.log'
            configure(path)
            configure(path)
            handlers = [h for h in logger.handlers if isinstance(h, PrivateRotatingHandler)]
            self.assertEqual(len(handlers), 1)
            handlers[0].maxBytes = 100
            for _ in range(10):
                logger.info('Bounded diagnostic information: %s', 'x' * 50)
            self.assertLessEqual(len(list(Path(directory).iterdir())), 4)
            for file in Path(directory).iterdir():
                self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
            for handler in handlers:
                logger.removeHandler(handler)
                handler.close()
