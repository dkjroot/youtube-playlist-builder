from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock

from youtube_playlist_builder.playlists import create_test_playlist


class PlaylistTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.receipt = Path(directory.name) / "test.json"
        self.youtube = MagicMock()
        self.youtube.videos.return_value.list.return_value.execute.return_value = {
            "items": [{"snippet": {"title": "Chess", "publishedAt": "2026-10-28T09:00:00Z"}}]}
        self.youtube.channels.return_value.list.return_value.execute.return_value = {"items": [{"id": "owner"}]}
        self.youtube.playlists.return_value.insert.return_value.execute.return_value = {"id": "playlist"}
        self.youtube.playlistItems.return_value.insert.return_value.execute.return_value = {"id": "item"}
        self.youtube.playlists.return_value.list.return_value.execute.return_value = {"items": [{"status": {"privacyStatus": "private"}}]}
        self.youtube.playlistItems.return_value.list.return_value.execute.return_value = {"items": [{"snippet": {"resourceId": {"videoId": "video"}}}]}

    def run_test(self):
        with redirect_stdout(io.StringIO()):
            return create_test_playlist(self.youtube, "video", self.receipt)

    def test_private_playlist_and_item_ids_saved_and_repeat_blocked(self):
        result = self.run_test()
        self.assertEqual(result["phase"], "complete")
        self.assertEqual(json.loads(self.receipt.read_text())["playlist_item_id"], "item")
        body = self.youtube.playlists.return_value.insert.call_args.kwargs["body"]
        self.assertEqual(body["status"]["privacyStatus"], "private")
        self.youtube.playlists.return_value.insert.return_value.execute.assert_called_once_with(num_retries=0)
        with self.assertRaisesRegex(ValueError, "already recorded"):
            self.run_test()
        self.youtube.playlists.return_value.insert.assert_called_once()

    def test_failed_insertion_preserves_created_playlist_id(self):
        def fail(**kwargs):
            record = json.loads(self.receipt.read_text())
            self.assertEqual(record["playlist_id"], "playlist")
            self.assertEqual(record["phase"], "adding_video")
            raise TimeoutError("Uncertain outcome")
        self.youtube.playlistItems.return_value.insert.return_value.execute.side_effect = fail
        with self.assertRaises(TimeoutError):
            self.run_test()
        with self.assertRaisesRegex(ValueError, "already recorded"):
            self.run_test()

    def test_missing_video_does_not_create_playlist(self):
        self.youtube.videos.return_value.list.return_value.execute.return_value = {"items": []}
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.run_test()
        self.youtube.playlists.return_value.insert.assert_not_called()
        self.assertFalse(self.receipt.exists())
