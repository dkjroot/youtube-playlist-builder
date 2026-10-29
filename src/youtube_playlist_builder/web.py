"""Loopback-only management UI over the same state, validation and CLI jobs.

Opening the page only reads local data. Updates/previews/discovery run as explicit
child CLI jobs with the existing state lock, quota ledger and recovery records.
The server never returns credential files or arbitrary filesystem paths. A per-
launch token and same-origin checks protect mutations from unrelated browser tabs.
"""

import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
import uuid
import webbrowser
from zoneinfo import ZoneInfo

from .organiser import channel_detail, assign_channel, fetch_channel_videos
from .limits import prepare_limits
from .forecast import project, planning_days
from .rebalance import redistribute
from .schedule import FREQUENCIES, utcnow, due_topics, initialise_staggering
from .state import State
from .topics import validate, edit_topic, load_subscriptions

ASSETS = Path(__file__).with_name("web_assets")


class Jobs:
    """One explicit background CLI job, with bounded, pollable progress output."""

    def __init__(self, args):
        """Carry the launching command's paths and environment into every child."""
        self.args = args
        self.lock = threading.Lock()
        self.job = None
        self.process = None
        self.closed = False

    def base(self):
        """Build an argv list, never shell text or user-supplied executable names."""
        a = self.args
        return [
            sys.executable,
            "-u",
            "-m",
            "youtube_playlist_builder",
            "--state",
            str(a.state.absolute()),
            "--token",
            str(a.token.absolute()),
            "--client-secrets",
            str(a.client_secrets.absolute()),
            "--llm-config",
            str(a.llm_config.absolute()),
        ]

    def start(self, action, name=None):
        """Validate the requested job and start a worker without blocking HTTP progress polls."""
        commands = {
            "auto": ["auto"],
            "all": ["auto", "--all"],
            "update": ["update", "--", name],
            "preview": ["update", "--preview", "--", name],
            "reconcile": ["reconcile", "--", name],
            "refresh": [
                "subscriptions",
                "--output",
                str(self.args.state.parent.absolute() / "subscriptions.json"),
            ],
            "suggest": [
                "suggest-topics",
                "--json",
                "--subscriptions",
                str(self.args.state.parent.absolute() / "subscriptions.json"),
            ],
        }
        if action not in commands or (
            action in ("update", "preview", "reconcile") and not isinstance(name, str)
        ):
            raise ValueError("Choose a supported update action and topic.")
        with self.lock:
            if self.closed:
                raise ValueError("The local server is shutting down.")
            if self.job and self.job["running"]:
                raise ValueError(
                    "An operation is already running. Wait for it to finish."
                )
            self.job = dict(
                id=uuid.uuid4().hex,
                action=action,
                running=True,
                code=None,
                lines=[],
                result=None,
            )
            job = self.job
        threading.Thread(
            target=self._work, args=(job, commands[action]), daemon=True
        ).start()
        return job["id"]

    def _work(self, job, words):
        """Collect both streams without pipe deadlock; successful suggestion JSON becomes UI data."""
        output = []
        output_size = 0
        try:
            env = dict(os.environ, YT_PLAYLISTS_PROGRESS="1")
            with self.lock:
                if self.closed:
                    job["code"] = 130
                    return
                process = subprocess.Popen(
                    self.base() + words,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )
                self.process = process

            def collect(stream, stdout=False):
                """Bound displayed logs; keep a bounded JSON stdout for topic suggestions."""
                nonlocal output_size
                for line in stream:
                    with self.lock:
                        job["lines"].append(line.rstrip())
                        job["lines"] = job["lines"][-2000:]
                        if stdout and output_size < 5_000_000:
                            output.append(line)
                            output_size += len(line)
                stream.close()

            readers = [
                threading.Thread(target=collect, args=(process.stdout, True)),
                threading.Thread(target=collect, args=(process.stderr, False)),
            ]
            for reader in readers:
                reader.start()
            code = process.wait()
            for reader in readers:
                reader.join()
            if code == 0 and job["action"] == "refresh":
                state = State(self.args.state)
                try:
                    with state.locked():
                        state.set_meta("subscription_cache", None)
                finally:
                    state.close()
            result = (
                json.loads("".join(output))
                if code == 0 and job["action"] == "suggest"
                else None
            )
            with self.lock:
                job.update(code=code, result=result)
        except (OSError, ValueError) as error:
            with self.lock:
                job["lines"].append("Operation failed: " + str(error))
                job["code"] = 1
        finally:
            with self.lock:
                job["running"] = False
                self.process = None

    def snapshot(self):
        """Copy mutable worker data while holding its lock for a consistent JSON response."""
        with self.lock:
            return json.loads(json.dumps(self.job))

    def close(self):
        """Stop a child on server shutdown; CLI write intents preserve uncertain outcomes."""
        with self.lock:
            self.closed = True
            process = self.process
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()


def snapshot(args):
    """Read local management data without making API/model requests or waiting on the writer lock."""
    state = State(args.state)
    try:
        topics = state.topics()
        for topic in topics:
            topic["item_count"] = len(state.items(topic["id"]))
            topic["pending"] = bool(state.pending(topic["id"]))
        export = args.state.parent / "subscriptions.json"
        channels = (
            load_subscriptions(export)
            if export.exists()
            else state.meta("subscription_cache", {}) or {}
        )
        if isinstance(channels, dict):
            channels = channels.get("channels", [])
        day = utcnow().astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()
        return dict(
            topics=topics,
            channels=channels,
            frequencies=FREQUENCIES,
            settings={
                key: state.meta(key, default)
                for key, default in [
                    ("daily_budget", 9500),
                    ("single_topic_channels", False),
                    ("timezone", "UTC"),
                    ("mode", "text"),
                    ("minimum_video_seconds", 0),
                    ("subscription_cache_hours", 24),
                ]
            },
            quota=dict(day=day, **state.quota(day)),
            usage=state.api_usage(day),
            queue=[
                topic["name"]
                for topic in due_topics(topics, state.meta("cursor", 0), utcnow())
            ],
        )
    finally:
        state.close()


def save_topic(args, body):
    """Apply local-only edits with the same validator and lock as the CLI; remote changes need an update."""
    name = body.get("name")
    if not isinstance(name, str):
        raise ValueError("Enter a topic name.")
    name = name.strip()
    action = body.get("action", "edit")
    fields = body.get("fields", {})
    allowed = {
        "description",
        "channels",
        "channel_priorities",
        "playlist_name",
        "frequency",
        "max_age_days",
        "include_words",
        "exclude_words",
        "enabled",
        "max_videos",
        "channel_limits",
        "channel_max_age_days",
    }
    if not isinstance(fields, dict) or fields.keys() - allowed:
        raise ValueError("Unknown topic fields.")
    for key in ("channels", "include_words", "exclude_words"):
        if key in fields and (
            not isinstance(fields[key], list)
            or any(not isinstance(value, str) for value in fields[key])
        ):
            raise ValueError(key + " must be a list of text values.")
    for key in ("channel_priorities", "channel_limits", "channel_max_age_days"):
        if key in fields and not isinstance(fields[key], dict):
            raise ValueError(key + " must be a mapping.")
    if "enabled" in fields and type(fields["enabled"]) is not bool:
        raise ValueError("Enabled must be true or false.")
    if "max_age_days" in fields and (
        type(fields["max_age_days"]) is not int or fields["max_age_days"] < 1
    ):
        raise ValueError("Maximum age must be a positive whole number of days.")
    if "description" in fields and not isinstance(fields["description"], str):
        raise ValueError("Description must be text.")
    if (
        "playlist_name" in fields
        and fields["playlist_name"] is not None
        and not isinstance(fields["playlist_name"], str)
    ):
        raise ValueError("Playlist name must be text.")
    state = State(args.state)
    try:
        with state.locked():
            if action == "delete":
                state.remove(state.topic(name)["id"])
            elif action == "add":
                prepare_limits({}, fields)
                validate(name, fields)
                state.add(name, **fields)
            elif action == "edit":
                edit_topic(state, state.topic(name), fields)
            else:
                raise ValueError("Unknown topic action.")
            initialise_staggering(state, utcnow())
    finally:
        state.close()


def make_server(args, port, *, session_token=None):
    """Build an IPv4 loopback server; a launch token protects all API requests."""
    jobs = Jobs(args)
    if session_token is not None and (
        not isinstance(session_token, str) or len(session_token) < 32
    ):
        raise ValueError("A reused local session token must be at least 32 characters.")
    # Embedders can retain an existing browser session during a controlled restart.
    # Normal CLI launches always generate a fresh token.
    token = session_token or secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        """Serve fixed assets and a small allowlisted JSON API; never accept arbitrary commands."""

        def log_message(self, *args):
            """Suppress request URLs and personal topic names in generic HTTP access logs."""
            pass

        def reply(self, status, value, content_type="application/json"):
            """Write a bounded same-origin response and prevent caching private management data."""
            data = (
                json.dumps(value).encode()
                if content_type == "application/json"
                else value
            )
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https://i.ytimg.com; frame-ancestors 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(data)

        def authorised(self):
            """Check Host plus the per-launch token, rejecting cross-origin browser requests."""
            authority = f"127.0.0.1:{self.server.server_port}"
            origin = self.headers.get("Origin")
            return (
                self.headers.get("Host") == authority
                and (not origin or origin == "http://" + authority)
                and secrets.compare_digest(
                    self.headers.get("X-YT-Local", "").encode(), token.encode()
                )
            )

        def do_GET(self):
            """Read local state/jobs or serve packaged management assets."""
            path = urlsplit(self.path).path
            if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                return self.reply(403, {"error": "Use the printed loopback URL."})
            if path in ("/", "/organiser"):
                page = (
                    (
                        ASSETS
                        / ("organiser.html" if path == "/organiser" else "index.html")
                    )
                    .read_text()
                    .replace("__TOKEN__", token)
                )
                return self.reply(200, page.encode(), "text/html")
            if path in (
                "/app.js", "/app.css", "/organiser.js", "/organiser.css",
                "/organiser-voice.js",
            ):
                return self.reply(
                    200,
                    (ASSETS / path[1:]).read_bytes(),
                    "text/javascript" if path.endswith(".js") else "text/css",
                )
            if not self.authorised():
                return self.reply(403, {"error": "Open the management page again."})
            try:
                if path == "/api/state":
                    return self.reply(200, snapshot(args))
                if path == "/api/channel":
                    cid = parse_qs(urlsplit(self.path).query).get("id", [""])[0]
                    return self.reply(200, channel_detail(args, cid))
                if path == "/api/forecast":
                    rate = float(
                        parse_qs(urlsplit(self.path).query).get("rate", ["1"])[0]
                    )
                    state = State(args.state)
                    try:
                        mode = parse_qs(urlsplit(self.path).query).get(
                            "mode", ["budget"]
                        )[0]
                        requested = parse_qs(urlsplit(self.path).query).get(
                            "days", ["auto"]
                        )[0]
                        days = (
                            planning_days(
                                state.topics(),
                                timezone_name=state.meta("timezone", "UTC"),
                            )
                            if requested == "auto"
                            else int(requested)
                        )
                        if not 1 <= days <= 366:
                            raise ValueError(
                                "Choose a forecast span between 1 and 366 days."
                            )
                        return self.reply(
                            200, project(state, rate=rate, mode=mode, days=days)
                        )
                    finally:
                        state.close()
                if path == "/api/job":
                    return self.reply(200, jobs.snapshot())
                return self.reply(404, {"error": "Not found."})
            except sqlite3.Error:
                return self.reply(
                    409,
                    {
                        "error": "The database is busy or unavailable; try again after the operation finishes."
                    },
                )
            except (ValueError, OSError) as error:
                return self.reply(400, {"error": str(error)})

        def do_POST(self):
            """Mutate local configuration or explicitly launch a supported network operation."""
            if not self.authorised():
                return self.reply(403, {"error": "Open the management page again."})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1_000_000:
                    raise ValueError("Invalid request size.")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("Use a JSON object.")
                path = urlsplit(self.path).path
                if path == "/api/channel-fetch":
                    return self.reply(
                        200,
                        fetch_channel_videos(
                            args,
                            body.get("channel_id"),
                            refresh=body.get("refresh", False),
                        ),
                    )
                if path == "/api/assignment":
                    assign_channel(args, body)
                    return self.reply(200, {"ok": True})
                if path == "/api/topic":
                    save_topic(args, body)
                    return self.reply(200, {"ok": True})
                if path == "/api/jobs":
                    return self.reply(
                        202, {"id": jobs.start(body.get("action"), body.get("name"))}
                    )
                if path == "/api/redistribute":
                    state = State(args.state)
                    try:
                        with state.locked():
                            result = redistribute(
                                state, rate=float(body.get("rate", 1))
                            )
                    finally:
                        state.close()
                    return self.reply(200, result)
                if path == "/api/settings":
                    flags = {
                        "single_topic_channels": "--single-topic-channels",
                        "daily_budget": "--daily-budget",
                        "timezone": "--timezone",
                        "mode": "--mode",
                        "minimum_video_seconds": "--minimum-video-length",
                        "subscription_cache_hours": "--subscription-cache-hours",
                    }
                    if body.keys() - flags.keys():
                        raise ValueError("Unknown setting.")
                    words = ["settings"]
                    for key, value in body.items():
                        if key == "single_topic_channels":
                            if type(value) is not bool:
                                raise ValueError(
                                    "One topic per channel must be true or false."
                                )
                            value = "on" if value else "off"
                        words += [flags[key], str(value)]
                    result = subprocess.run(
                        jobs.base() + words, capture_output=True, text=True
                    )
                    if result.returncode:
                        raise ValueError(result.stderr.strip() or result.stdout.strip())
                    return self.reply(200, {"ok": True})
                return self.reply(404, {"error": "Not found."})
            except sqlite3.Error:
                return self.reply(
                    409,
                    {
                        "error": "The database is busy or unavailable; try again after the operation finishes."
                    },
                )
            except (ValueError, TypeError, OSError) as error:
                return self.reply(400, {"error": str(error)})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.jobs = jobs
    return server


def serve(args):
    """Run the local UI until Ctrl-C; only button-triggered jobs use YouTube quota."""
    server = make_server(args, args.port)
    url = f"http://127.0.0.1:{server.server_port}"
    print("YouTube playlist manager: " + url, flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.jobs.close()
        server.server_close()
    return 0
