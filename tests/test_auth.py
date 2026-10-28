import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from google.auth.exceptions import RefreshError
from google_auth_oauthlib.flow import WSGITimeoutError
from oauthlib.oauth2 import AccessDeniedError

from youtube_playlist_builder.auth import SCOPES, WRITE_SCOPES, authenticate, save_token


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.token = self.root / "token.json"
        self.client = self.root / "client.json"

    def test_token_is_saved_with_owner_only_permissions(self):
        credentials = MagicMock()
        credentials.to_json.return_value = '{"token": "fake"}'
        save_token(self.token, credentials)
        self.assertEqual(json.loads(self.token.read_text()), {"token": "fake"})
        self.assertEqual(stat.S_IMODE(self.token.stat().st_mode), 0o600)
        self.assertEqual(list(self.root.iterdir()), [self.token])

    def test_missing_client_file_has_actionable_error(self):
        with self.assertRaisesRegex(ValueError, "Missing OAuth client file"):
            authenticate(self.client, self.token)

    @patch('youtube_playlist_builder.auth.InstalledAppFlow')
    def test_unattended_mode_never_opens_browser(self, flow):
        with self.assertRaisesRegex(ValueError, 'auth --write-access'):
            authenticate(self.client, self.token, write_access=True, interactive=False)
        flow.from_client_secrets_file.assert_not_called()

    @patch("youtube_playlist_builder.auth.InstalledAppFlow")
    @patch("youtube_playlist_builder.auth.Credentials.from_authorized_user_file")
    def test_write_access_requires_new_consent_for_readonly_token(self, load, flow):
        self.token.write_text("old token")
        self.client.write_text("{}")
        load.return_value.has_scopes.return_value = False
        credentials = flow.from_client_secrets_file.return_value.run_local_server.return_value
        credentials.to_json.return_value = "{}"
        self.assertIs(authenticate(self.client, self.token, write_access=True), credentials)
        flow.from_client_secrets_file.assert_called_once_with(
            str(self.client), WRITE_SCOPES, autogenerate_code_verifier=True)

    @patch("youtube_playlist_builder.auth.InstalledAppFlow")
    def test_new_sign_in_uses_readonly_scope_and_pkce(self, flow_class):
        self.client.write_text("{}")
        credentials = flow_class.from_client_secrets_file.return_value.run_local_server.return_value
        credentials.to_json.return_value = "{}"
        authenticate(self.client, self.token)
        flow_class.from_client_secrets_file.assert_called_once_with(
            str(self.client), SCOPES, autogenerate_code_verifier=True
        )
        options = flow_class.from_client_secrets_file.return_value.run_local_server.call_args.kwargs
        self.assertEqual(options["port"], 0)
        self.assertEqual(options["timeout_seconds"], 180)
        self.assertEqual(SCOPES, ["https://www.googleapis.com/auth/youtube.readonly"])

    @patch("youtube_playlist_builder.auth.InstalledAppFlow")
    @patch("youtube_playlist_builder.auth.Credentials.from_authorized_user_file")
    def test_expired_token_is_refreshed_without_browser(self, load, flow):
        self.token.write_text("{}")
        credentials = load.return_value
        credentials.expired = True
        credentials.valid = True
        credentials.to_json.return_value = "{}"
        self.assertIs(authenticate(self.client, self.token), credentials)
        credentials.refresh.assert_called_once()
        flow.from_client_secrets_file.assert_not_called()

    @patch("youtube_playlist_builder.auth.InstalledAppFlow")
    def test_failed_login_is_explained_without_saving_token(self, flow):
        self.client.write_text("{}")
        for error, message in [
            (WSGITimeoutError("timeout"), "Sign-in timed out"),
            (AccessDeniedError(), "Google sign-in was declined"),
        ]:
            with self.subTest(error=type(error).__name__):
                flow.from_client_secrets_file.return_value.run_local_server.side_effect = error
                with self.assertRaisesRegex(ValueError, message):
                    authenticate(self.client, self.token)
                self.assertFalse(self.token.exists())

    @patch("youtube_playlist_builder.auth.Credentials.from_authorized_user_file")
    def test_revoked_token_prompts_for_new_sign_in(self, load):
        self.token.write_text("{}")
        load.return_value.expired = True
        load.return_value.refresh.side_effect = RefreshError("revoked")
        with self.assertRaisesRegex(ValueError, "Remove .* and sign in again"):
            authenticate(self.client, self.token)

