"""Slot caps preserve quiet channels and bound lookback/classification work."""

from datetime import timedelta
from unittest.mock import Mock
import unittest

import test_channel_fair_sync
from test_channel_order import A, B, C
from youtube_playlist_builder.limits import prepare_limits, equal_slots
from youtube_playlist_builder.topics import edit_topic


class LimitPolicyTests(unittest.TestCase):
    def test_equal_allocation_and_custom_total(self):
        fields = dict(channels=[A, B, C], max_videos=10)
        prepare_limits({}, fields)
        self.assertEqual(fields["channel_limits"], {A: 4, B: 3, C: 3})
        topic = dict(fields)
        edits = {"channel_limits": {B: 1}}
        prepare_limits(topic, edits)
        self.assertEqual(edits["max_videos"], 8)
        self.assertEqual(edits["channel_limits"], {A: 4, B: 1, C: 3})
        reset = {"max_videos": 6}
        prepare_limits(dict(topic, **edits), reset)
        self.assertEqual(reset["channel_limits"], {A: 2, B: 2, C: 2})
        unlimited = {"max_videos": None}
        prepare_limits(topic, unlimited)
        self.assertEqual(unlimited["channel_limits"], {})


class CappedSyncTests(unittest.TestCase):
    setUp = test_channel_fair_sync.ChannelFairSyncTests.setUp
    sync = test_channel_fair_sync.ChannelFairSyncTests.sync
    inserted = test_channel_fair_sync.ChannelFairSyncTests.inserted

    def cap(self, slots):
        edit_topic(
            self.state,
            self.state.topic("news"),
            dict(max_videos=sum(slots.values()), channel_limits=slots),
        )

    def test_channel_cap_and_topic_total_keep_no_borrowing(self):
        self.cap({A: 4, B: 4, C: 4})
        self.server.video_data = {
            key: row
            for key, row in self.server.video_data.items()
            if row["snippet"]["channelId"] != C or key == "c0"
        }
        self.assertEqual(self.sync().run(), 0)
        ids = [row["video_id"] for row in self.state.items(self.topic["id"])]
        self.assertEqual(len(ids), 9)
        self.assertEqual(sum(key.startswith("c") for key in ids), 1)
        self.assertEqual(sum(key.startswith("a") for key in ids), 4)
        self.assertTrue(set(["a0", "a1", "a2", "a3", "b0", "c0"]) <= set(ids))

    def test_newer_video_replaces_oldest_only_inside_its_channel(self):
        self.cap({A: 2, B: 2, C: 2})
        self.sync().run()
        self.server.video_data["breaking"] = {
            "id": "breaking",
            "snippet": {
                "title": "New",
                "channelId": A,
                "publishedAt": (
                    test_channel_fair_sync.NOW - timedelta(seconds=1)
                ).isoformat(),
            },
            "status": {"privacyStatus": "public"},
        }
        self.server.calls.clear()
        self.assertEqual(self.sync().run(topic_name="news"), 0)
        writes = [(r, m, p) for r, m, p in self.server.calls if m != "list"]
        self.assertEqual([m for r, m, p in writes], ["delete", "insert"])
        ids = {row["video_id"] for row in self.state.items(self.topic["id"])}
        self.assertIn("breaking", ids)
        self.assertNotIn("a1", ids)
        self.assertTrue({"b0", "b1", "c0", "c1"} <= ids)

    def test_preview_does_not_trim_and_partial_cleanup_never_adds_over_cap(self):
        self.sync().run()
        self.cap({A: 1, B: 1, C: 1})
        before = self.state.items(self.topic["id"])
        self.assertEqual(self.sync().run(preview=True), 0)
        self.assertEqual(self.state.items(self.topic["id"]), before)
        self.server.calls.clear()
        self.assertEqual(self.sync(105).run(), 2)
        self.assertTrue(
            all(method != "insert" for resource, method, p in self.server.calls)
        )
        self.assertIsNone(self.state.topic("news")["next_due"])
        self.assertEqual(self.sync().run(), 0)
        self.assertEqual(len(self.state.items(self.topic["id"])), 3)

    def test_six_month_busy_channel_stops_after_first_page_and_one_model_batch(self):
        self.cap({A: 4, B: 0, C: 0})
        self.state.update(self.topic["id"], max_age_days=180)
        self.server.video_data.clear()
        for i in range(1800):
            self.server.video_data[str(i)] = {
                "id": str(i),
                "snippet": {
                    "title": "Video",
                    "channelId": A,
                    "publishedAt": (
                        test_channel_fair_sync.NOW - timedelta(hours=i * 2.4)
                    ).isoformat(),
                },
                "status": {"privacyStatus": "public"},
            }
        original = self.server.handle

        def handle(resource, method, p):
            if (
                resource == "playlistItems"
                and method == "list"
                and p["playlistId"] == A
            ):
                offset = int(p.get("pageToken", "0"))
                page = {
                    "items": [
                        {
                            "contentDetails": {
                                "videoId": str(i),
                                "videoPublishedAt": self.server.video_data[str(i)][
                                    "snippet"
                                ]["publishedAt"],
                            }
                        }
                        for i in range(offset, min(offset + 50, 1800))
                    ]
                }
                if offset + 50 < 1800:
                    page["nextPageToken"] = str(offset + 50)
                return page
            return original(resource, method, p)

        self.server.handle = handle
        classifier = Mock()
        classifier.match.side_effect = lambda topic, videos: dict(videos)
        sync = self.sync()
        sync.classifier = classifier
        self.assertEqual(sync.run(), 0)
        self.assertEqual(len(self.state.items(self.topic["id"])), 4)
        self.assertEqual(classifier.match.call_count, 1)
        self.assertEqual(
            len(
                [
                    1
                    for r, m, p in self.server.calls
                    if r == "playlistItems" and m == "list"
                ]
            ),
            1,
        )
        self.assertEqual(len([1 for r, m, p in self.server.calls if r == "videos"]), 1)

    def test_channel_age_override_works_in_bounded_and_unlimited_modes(self):
        for row in self.server.video_data.values():
            from youtube_playlist_builder.schedule import parse_time

            row["snippet"]["publishedAt"] = (
                parse_time(row["snippet"]["publishedAt"]) - timedelta(days=100)
            ).isoformat()
        self.state.update(self.topic["id"], channel_max_age_days={C: 180})
        topic = self.state.topic("news")
        self.assertEqual(
            set(self.sync().recent_videos(topic, test_channel_fair_sync.NOW)),
            {f"c{i}" for i in range(8)},
        )
        self.cap({A: 2, B: 2, C: 2})
        self.assertEqual(self.sync().run(), 0)
        self.assertEqual(
            {row["video_id"] for row in self.state.items(self.topic["id"])},
            {"c0", "c1"},
        )
