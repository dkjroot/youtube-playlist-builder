from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from youtube_playlist_builder.cli import main
from youtube_playlist_builder.subscriptions import Subscription


class CliTests(unittest.TestCase):
    @patch("youtube_playlist_builder.cli.authenticate")
    def test_export_cannot_overwrite_token(self, authenticate):
        with redirect_stderr(io.StringIO()):
            status = main(["subscriptions", "--output", ".local/token.json"])
        self.assertEqual(status, 1)
        authenticate.assert_not_called()

    @patch("youtube_playlist_builder.cli.list_subscriptions")
    @patch("youtube_playlist_builder.cli.build")
    @patch("youtube_playlist_builder.cli.authenticate")
    def test_export_preserves_unicode(self, authenticate, build, subscriptions):
        subscriptions.return_value = [Subscription("UC1", "Échecs", "Chess")]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "channels.json"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["subscriptions", "--output", str(output)]), 0)
            self.assertEqual(json.loads(output.read_text())[0]["title"], "Échecs")
            build.assert_called_once_with(
                "youtube", "v3", credentials=authenticate.return_value, cache_discovery=False
            )

