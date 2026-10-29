"""Public feeds improve categorisation without credentials, quota, or sync discovery changes."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import URLError

from youtube_playlist_builder.organiser import fetch_channel_videos, channel_detail
from youtube_playlist_builder.state import State

A = "UC" + "a" * 22


class OrganiserFeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.args = SimpleNamespace(state=Path(self.temp.name) / "state.db")

    def feed(self):
        return (
            '<feed xmlns="http://www.w3.org/2005/Atom" '
            'xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
            'xmlns:media="http://search.yahoo.com/mrss/">'
            + "".join(
                f"<entry><yt:videoId>abcdefghij{i}</yt:videoId><yt:channelId>{A}</yt:channelId>"
                f"<title>Video {i}</title><published>2026-10-{i+1:02d}T00:00:00Z</published>"
                "<media:group><media:description>Details</media:description></media:group></entry>"
                for i in range(7)
            )
            + "</feed>"
        ).encode()

    @patch("urllib.request.build_opener")
    def test_feed_saves_newest_five_and_revisits_do_not_refetch(self, build):
        response = build.return_value.open.return_value.__enter__.return_value
        response.read.return_value = self.feed()
        detail = fetch_channel_videos(self.args, A)
        self.assertEqual(
            [v["title"] for v in detail["videos"]],
            ["Video 6", "Video 5", "Video 4", "Video 3", "Video 2"],
        )
        self.assertEqual(detail["videos"][0]["description"], "Details")
        self.assertTrue(detail["feed_fetched_at"])
        self.assertEqual(fetch_channel_videos(self.args, A), detail)
        self.assertEqual(build.return_value.open.call_count, 1)
        fetch_channel_videos(self.args, A, refresh=True)
        self.assertEqual(build.return_value.open.call_count, 2)
        args, kwargs = build.return_value.open.call_args
        self.assertEqual(
            args, ("https://www.youtube.com/feeds/videos.xml?channel_id=" + A,)
        )
        self.assertEqual(kwargs, {"timeout": 15})
        state = State(self.args.state)
        try:
            self.assertEqual(
                state.db.execute("SELECT COUNT(*) FROM quota").fetchone()[0], 0
            )
        finally:
            state.close()

    @patch("urllib.request.build_opener")
    def test_failed_or_invalid_feeds_preserve_saved_history(self, build):
        response = build.return_value.open.return_value.__enter__.return_value
        response.read.return_value = self.feed()
        original = fetch_channel_videos(self.args, A)
        build.return_value.open.side_effect = URLError("temporarily unavailable")
        with self.assertRaisesRegex(ValueError, "No API quota"):
            fetch_channel_videos(self.args, A, refresh=True)
        self.assertEqual(channel_detail(self.args, A), original)
        build.return_value.open.side_effect = None
        for bad in (
            b"<html/>",
            b"<!DOCTYPE feed><feed/>",
            b"not xml",
            b"x" * 2_000_001,
        ):
            response.read.return_value = bad
            with self.assertRaises(ValueError):
                fetch_channel_videos(self.args, A, refresh=True)
            self.assertEqual(channel_detail(self.args, A), original)

    @patch("urllib.request.build_opener")
    def test_invalid_channel_cannot_control_request_destination(self, build):
        with self.assertRaises(ValueError):
            fetch_channel_videos(self.args, "https://elsewhere.example")
        build.assert_not_called()
