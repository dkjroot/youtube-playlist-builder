"""Redistribution smooths recurring demand without erasing outstanding work or quota."""

from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from youtube_playlist_builder.state import State
from youtube_playlist_builder.forecast import project, budget_carryover, planning_days
from youtube_playlist_builder.rebalance import redistribute
from youtube_playlist_builder.schedule import slot_due, parse_time
from test_sync import NOW, CHANNEL


class RedistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = State(Path(self.temp.name) / "state.db")
        self.addCleanup(self.state.close)

    def add(self, name, frequency="weekly", **fields):
        return self.state.add(
            name,
            channels=[CHANNEL],
            frequency=frequency,
            max_age_days=180,
            max_videos=4,
            channel_limits={CHANNEL: 4},
            playlist_id="PL-" + name,
            last_success=(NOW - timedelta(days=7)).isoformat(),
            next_due=(NOW + timedelta(days=1)).isoformat(),
            **fields,
        )

    def test_cost_weighted_redistribution_reduces_peaks_and_preserves_real_history(
        self,
    ):
        for i in range(10):
            self.add(str(i))
        before = project(self.state, now=NOW, days=56)
        history = {row["id"]: row["last_success"] for row in self.state.topics()}
        self.state.set_meta("cursor", 7)
        result = redistribute(self.state, now=NOW)
        after = project(self.state, now=NOW, days=56)
        self.assertTrue(result["changed"])
        self.assertLess(
            max(row["units"] for row in after["days"]),
            max(row["units"] for row in before["days"]),
        )
        self.assertEqual(
            {row["id"]: row["last_success"] for row in self.state.topics()}, history
        )
        self.assertTrue(
            all(row["frequency"] == "weekly" for row in self.state.topics())
        )
        self.assertEqual(self.state.meta("cursor"), 7)
        self.assertEqual(self.state.quota("2026-10-28")["used"], 0)
        for row in self.state.topics():
            entry = self.state.meta("schedule_slots")[str(row["id"])]
            following = slot_due(
                parse_time(row["next_due"]), row["frequency"], "UTC", entry
            )
            self.assertEqual(
                (following.date() - parse_time(row["next_due"]).date()).days, 7
            )

    def test_disabled_daily_and_unfinished_dates_are_not_hidden(self):
        disabled = self.add("off", enabled=False)
        daily = self.add("daily", frequency="daily")
        partial = self.add("partial", last_attempt=NOW.isoformat())
        self.state.update(partial["id"], next_due=None)
        pending = self.add("pending")
        self.state.begin_write(pending["id"], "insert", {"video_id": "unknown"})
        for i in range(4):
            self.add("other" + str(i), frequency="every-2-days")
        redistribute(self.state, now=NOW)
        self.assertEqual(
            self.state.by_id(disabled["id"])["next_due"], disabled["next_due"]
        )
        self.assertEqual(self.state.by_id(daily["id"])["next_due"], daily["next_due"])
        self.assertIsNone(self.state.by_id(partial["id"])["next_due"])
        self.assertEqual(
            self.state.by_id(pending["id"])["next_due"], pending["next_due"]
        )
        self.assertIsNotNone(self.state.pending(pending["id"]))

    def test_every_time_forecasts_each_day_and_redistribution_preserves_it(self):
        topic = self.add('news', frequency='every-time')
        before = self.state.by_id(topic['id'])
        forecast = project(self.state, now=NOW, days=3)
        self.assertEqual([row['topics'] for row in forecast['days']], [1, 1, 1])
        self.assertIn('one update run per day', forecast['assumptions'])
        self.assertEqual(planning_days(self.state.topics(), now=NOW), 28)
        for i in range(8):
            self.add('weekly' + str(i))
        result = redistribute(self.state, now=NOW)
        self.assertTrue(result['changed'])
        self.assertEqual(self.state.by_id(topic['id']), before)
        self.assertNotIn(str(topic['id']), self.state.meta('schedule_slots', {}))

    def test_automatic_span_covers_monthly_and_fortnightly_cadences(self):
        self.add("fortnight", frequency="biweekly")
        self.assertGreaterEqual(planning_days(self.state.topics(), now=NOW), 28)
        self.add("monthly", frequency="monthly")
        self.assertGreaterEqual(planning_days(self.state.topics(), now=NOW), 62)

    def test_no_work_and_invalid_rate_leave_state_unchanged(self):
        self.assertEqual(redistribute(self.state, now=NOW)["changed"], [])
        self.add("topic")
        before = self.state.topics()
        with self.assertRaises(ValueError):
            redistribute(self.state, rate=-1, now=NOW)
        self.assertEqual(self.state.topics(), before)


class CarryoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = State(Path(self.temp.name) / "state.db")
        self.addCleanup(self.state.close)
        self.state.set_meta("daily_budget", 100)

    def rows(self):
        return [
            dict(
                date=str(i),
                units=150 if i == 0 else 0,
                reads=0,
                adds=3 if i == 0 else 0,
                removes=0,
                topics=1 if i == 0 else 0,
            )
            for i in range(3)
        ]

    def test_excess_requested_work_carries_to_next_day(self):
        rows = budget_carryover(self.state, self.rows(), NOW)
        self.assertEqual([row["units"] for row in rows], [100, 50, 0])
        self.assertEqual([row["carried_units"] for row in rows], [50, 0, 0])
        self.assertEqual(self.state.quota("2026-10-28")["used"], 0)

    def test_today_honours_recorded_usage_and_confirmed_server_exhaustion(self):
        self.state.charge("2026-10-28", 75)
        rows = budget_carryover(self.state, self.rows(), NOW)
        self.assertEqual(
            rows[0]["units"], 75
        )  # Remaining25 cannot fund a50-unit insertion.
        self.assertEqual(rows[0]["carried_units"], 150)
        self.state.exhaust("2026-10-28")
        rows = budget_carryover(self.state, self.rows(), NOW)
        self.assertEqual(rows[0]["additional_units"], 0)
        self.assertEqual(rows[1]["carried_units"], 50)

    def test_both_modes_available_without_consuming_quota(self):
        self.state.add("news", channels=[CHANNEL], max_age_days=30)
        demand = project(self.state, rate=1, now=NOW, mode="demand")
        budget = project(self.state, rate=1, now=NOW, mode="budget")
        self.assertGreater(demand["days"][0]["units"], 100)
        self.assertLessEqual(budget["days"][0]["units"], 100)
        self.assertGreater(budget["days"][0]["carried_units"], 0)
        self.assertEqual(self.state.quota("2026-10-28")["used"], 0)
