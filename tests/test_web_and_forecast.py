"""The local UI must not spend quota on page loads or expose credentials/cross-origin writes."""

import http.client
import json
from pathlib import Path
from html.parser import HTMLParser
from tempfile import TemporaryDirectory
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from youtube_playlist_builder.web import make_server
from youtube_playlist_builder.state import State
from youtube_playlist_builder.forecast import project
from test_channel_order import A, B
from test_sync import NOW


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.args = SimpleNamespace(
            state=root / "state.db",
            token=root / "token.json",
            client_secrets=root / "client.json",
            llm_config=root / "llm.json",
        )
        self.args.token.write_text("private-token-do-not-expose")
        self.server = make_server(self.args, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown)
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)

        class TokenParser(HTMLParser):
            def handle_starttag(parser, tag, attributes):
                values = dict(attributes)
                if tag == "meta" and values.get("name") == "yt-local":
                    parser.token = values["content"]

        parser = TokenParser()
        parser.feed(body)
        self.token = parser.token

    def shutdown(self):
        self.server.shutdown()
        self.server.jobs.close()
        self.server.server_close()
        self.thread.join()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        data = None if body is None else json.dumps(body)
        try:
            conn.request(method, path, data, headers or {})
            response = conn.getresponse()
            return response.status, response.read().decode()
        finally:
            conn.close()

    def api(self, path, body=None, extra=None):
        headers = {"X-YT-Local": self.token, "Content-Type": "application/json"}
        headers.update(extra or {})
        return self.request("GET" if body is None else "POST", path, body, headers)

    def test_state_and_forecast_are_local_only_and_credentials_not_served(self):
        with patch("youtube_playlist_builder.web.subprocess.Popen") as process:
            for path in ["/api/state", "/api/forecast?rate=0.25"]:
                status, body = self.api(path)
                self.assertEqual(status, 200)
                self.assertNotIn("private-token", body)
            process.assert_not_called()
        self.assertNotEqual(self.request("GET", "/token.json")[0], 200)
        self.assertEqual(self.request("GET", "/api/state")[0], 403)

    def test_same_origin_token_required_for_edits(self):
        self.assertEqual(
            self.api(
                "/api/topic",
                {"action": "add", "name": "bad", "fields": {}},
                {"Origin": "https://elsewhere.example"},
            )[0],
            403,
        )
        self.assertEqual(
            self.request(
                "POST", "/api/topic", {"action": "add", "name": "bad", "fields": {}}
            )[0],
            403,
        )
        self.assertEqual(
            self.api("/api/state", extra={"Host": "hostile.example"})[0], 403
        )

    def test_topic_allocations_save_and_change_total_without_api_calls(self):
        body = {
            "action": "add",
            "name": "news",
            "fields": {"channels": [A, B], "max_videos": 10, "max_age_days": 7},
        }
        self.assertEqual(self.api("/api/topic", body)[0], 200)
        self.assertEqual(
            self.api(
                "/api/topic",
                {
                    "action": "edit",
                    "name": "news",
                    "fields": {
                        "channel_limits": {A: 2},
                        "channel_max_age_days": {B: 180},
                    },
                },
            )[0],
            200,
        )
        data = json.loads(self.api("/api/state")[1])
        topic = data["topics"][0]
        self.assertEqual(topic["max_videos"], 7)
        self.assertEqual(topic["channel_limits"], {A: 2, B: 5})
        self.assertEqual(topic["channel_max_age_days"], {B: 180})
        self.assertEqual(data["quota"]["used"], 0)

    def test_empty_auto_job_completes_without_signin_and_streams_output(self):
        import time

        status, body = self.api("/api/jobs", {"action": "auto"})
        self.assertEqual(status, 202)
        deadline = time.monotonic() + 10
        job = None
        while time.monotonic() < deadline:
            job = json.loads(self.api("/api/job")[1])
            if not job["running"]:
                break
            time.sleep(0.02)
        self.assertFalse(job["running"])
        self.assertEqual(job["code"], 0)
        self.assertIn("Nothing to update", "\n".join(job["lines"]))
        self.assertFalse(self.args.token.read_text() != "private-token-do-not-expose")

    def test_redistribution_and_both_forecasts_are_available_without_remote_jobs(self):
        for i in range(3):
            self.assertEqual(
                self.api(
                    "/api/topic",
                    {
                        "action": "add",
                        "name": "weekly" + str(i),
                        "fields": {
                            "channels": [A],
                            "frequency": "weekly",
                            "max_videos": 4,
                            "max_age_days": 30,
                        },
                    },
                )[0],
                200,
            )
        with patch("youtube_playlist_builder.web.subprocess.Popen") as process:
            status, body = self.api("/api/redistribute", {"rate": 1})
            self.assertEqual(status, 200)
            self.assertIn("changed", json.loads(body))
            for mode in ("demand", "budget"):
                status, body = self.api("/api/forecast?rate=1&mode=" + mode)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["mode"], mode)
            status, body = self.api("/api/forecast?days=62")
            self.assertEqual(status, 200)
            self.assertEqual(len(json.loads(body)["days"]), 62)
            process.assert_not_called()
        self.assertEqual(self.api("/api/forecast?mode=invalid")[0], 400)
        self.assertEqual(self.api("/api/forecast?days=367")[0], 400)
        self.assertEqual(json.loads(self.api("/api/state")[1])["quota"]["used"], 0)

    def test_organiser_assignments_preserve_topic_settings_and_make_no_jobs(self):
        with patch("youtube_playlist_builder.web.subprocess.Popen") as process:
            self.assertEqual(self.request("GET", "/organiser")[0], 200)
            self.assertEqual(self.request("GET", "/organiser.js")[0], 200)
            self.assertEqual(self.request("GET", "/organiser-voice.js")[0], 200)
            self.assertEqual(self.request("GET", "/api/channel?id=" + A)[0], 403)
            self.assertEqual(
                self.api(
                    "/api/topic",
                    {
                        "action": "add",
                        "name": "support",
                        "fields": {
                            "enabled": False,
                            "channels": [B],
                            "max_videos": 4,
                            "frequency": "biweekly",
                        },
                    },
                )[0],
                200,
            )
            self.assertEqual(
                self.api(
                    "/api/assignment",
                    {"channel_id": A, "topic": "support", "assigned": True},
                )[0],
                200,
            )
            data = json.loads(self.api("/api/state")[1])
            topic = data["topics"][0]
            self.assertEqual(set(topic["channels"]), {A, B})
            self.assertEqual(topic["channel_limits"], {A: 4, B: 4})
            self.assertFalse(topic["enabled"])
            self.assertEqual(topic["frequency"], "biweekly")
            self.assertEqual(
                self.api(
                    "/api/assignment",
                    {"channel_id": A, "topic": "support", "assigned": False},
                )[0],
                200,
            )
            self.assertEqual(self.api("/api/channel?id=invalid")[0], 400)
            self.assertEqual(
                self.api(
                    "/api/assignment",
                    {"channel_id": A, "topic": "support", "assigned": "yes"},
                )[0],
                400,
            )
            process.assert_not_called()
            self.assertEqual(data["quota"]["used"], 0)

    def test_channel_history_retains_newest_five_without_mixing_channels(self):
        state = State(self.args.state)
        try:
            state.remember_channel_videos(
                [
                    {
                        "video_id": str(i),
                        "channel_id": A,
                        "title": "Video " + str(i),
                        "published_at": f"2026-10-{i+1:02d}T00:00:00+00:00",
                        "description": "Description",
                    }
                    for i in range(9)
                ]
            )
            state.remember_channel_videos(
                [
                    {
                        "video_id": "other",
                        "channel_id": B,
                        "title": "Other",
                        "published_at": "2026-10-29T00:00:00+00:00",
                    }
                ]
            )
        finally:
            state.close()
        videos = json.loads(self.api("/api/channel?id=" + A)[1])["videos"]
        self.assertEqual([v["video_id"] for v in videos], ["8", "7", "6", "5", "4"])
        self.assertEqual(videos[0]["description"], "Description")
        self.assertEqual(
            json.loads(self.api("/api/channel?id=" + B)[1])["videos"][0]["video_id"],
            "other",
        )

    def test_feed_endpoint_requires_session_and_never_launches_cli_jobs(self):
        with patch(
            "youtube_playlist_builder.web.fetch_channel_videos",
            return_value={"videos": []},
        ) as fetch:
            with patch("youtube_playlist_builder.web.subprocess.Popen") as process:
                self.assertEqual(
                    self.request("POST", "/api/channel-fetch", {"channel_id": A})[0],
                    403,
                )
                fetch.assert_not_called()
                self.assertEqual(
                    self.api("/api/channel-fetch", {"channel_id": A})[0], 200
                )
                fetch.assert_called_once_with(self.args, A, refresh=False)
                process.assert_not_called()

    def test_jobs_allowlist_rejects_arbitrary_command(self):
        self.assertEqual(self.api("/api/jobs", {"action": "rm -rf /"})[0], 400)
        self.assertEqual(
            self.api(
                "/api/topic",
                {"action": "edit", "name": "missing", "fields": {"token": "secret"}},
            )[0],
            400,
        )


class ForecastTests(unittest.TestCase):
    def test_ten_days_and_caps_reduce_initial_and_recurring_writes(self):
        with TemporaryDirectory() as directory:
            state = State(Path(directory) / "state.db")
            try:
                topic = state.add("news", channels=[A], max_age_days=180)
                unlimited = project(state, rate=10, now=NOW)
                state.update(topic["id"], max_videos=4, channel_limits={A: 4})
                bounded = project(state, rate=10, now=NOW)
                self.assertEqual(len(bounded["days"]), 10)
                self.assertLess(
                    bounded["days"][0]["units"], unlimited["days"][0]["units"]
                )
                self.assertEqual(bounded["days"][0]["adds"], 4)
                self.assertTrue(all(day["adds"] <= 4 for day in bounded["days"]))
                self.assertIsNone(state.topic("news")["last_success"])
                self.assertEqual(state.quota("2026-10-28")["used"], 0)
                with self.assertRaises(ValueError):
                    project(state, rate=float("inf"), now=NOW)
            finally:
                state.close()
