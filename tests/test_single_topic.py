"""Channel moves preserve topic policy and cannot leave half of an assignment saved."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from youtube_playlist_builder.organiser import assign_channel
from youtube_playlist_builder.state import State

A, B = "UC" + "a" * 22, "UC" + "b" * 22


class SingleTopicTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.args = SimpleNamespace(state=Path(self.temp.name) / "state.db")
        self.state = State(self.args.state)
        self.addCleanup(self.state.close)
        self.state.add(
            "source",
            channels=[A, B],
            max_videos=6,
            channel_limits={A: 4, B: 2},
            channel_priorities={A: "high"},
            channel_max_age_days={A: 90},
            enabled=False,
        )
        self.state.add(
            "target",
            channels=[B],
            max_videos=3,
            channel_limits={B: 3},
            frequency="biweekly",
        )

    def assign(self, topic, assigned=True):
        assign_channel(
            self.args, {"channel_id": A, "topic": topic, "assigned": assigned}
        )

    def test_default_preserves_multiple_memberships(self):
        self.assign("target")
        self.assertIn(A, self.state.topic("source")["channels"])
        self.assertIn(A, self.state.topic("target")["channels"])

    def test_enabled_moves_and_preserves_unrelated_policy(self):
        self.state.set_meta("single_topic_channels", True)
        self.assign("target")
        source, target = self.state.topic("source"), self.state.topic("target")
        self.assertEqual(source["channels"], [B])
        self.assertEqual(source["channel_limits"], {B: 2})
        self.assertNotIn(A, source["channel_priorities"])
        self.assertNotIn(A, source["channel_max_age_days"])
        self.assertFalse(source["enabled"])
        self.assertEqual(target["channel_limits"], {A: 3, B: 3})
        self.assertEqual(target["frequency"], "biweekly")
        self.assign("target", False)
        self.assertNotIn(A, self.state.topic("target")["channels"])

    def test_existing_overlaps_are_left_for_manual_resolution(self):
        self.assign("target")
        self.state.add("third", channels=[])
        self.state.set_meta("single_topic_channels", True)
        self.assign("third")
        for topic in self.state.topics():
            self.assertIn(A, topic["channels"])

    def test_pending_source_rolls_back_target_move(self):
        self.state.set_meta("single_topic_channels", True)
        before = self.state.topics()
        self.state.begin_write(self.state.topic("source")["id"], "insert", {})
        with self.assertRaisesRegex(ValueError, "Reconcile"):
            self.assign("target")
        self.assertEqual(self.state.topics(), before)
