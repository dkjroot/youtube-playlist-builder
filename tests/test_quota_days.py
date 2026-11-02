"""Recurring work, forecasts and migration share Google's Pacific quota day."""

from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import redirect_stdout
import io
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from youtube_playlist_builder.forecast import project
from youtube_playlist_builder.schedule import (
    QUOTA_TIMEZONE, due_topics, initialise_staggering, migrate_quota_days,
    next_due, parse_time, staggered_due,
)
from youtube_playlist_builder.state import State
from youtube_playlist_builder.cli import main
from test_sync import CHANNEL


class QuotaDayTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = State(Path(temp.name) / 'state.db')
        self.addCleanup(self.state.close)
        self.state.set_meta('timezone', 'Europe/London')

    def test_daily_run_before_reset_does_not_advance_to_following_uk_day(self):
        before = parse_time('2026-11-02T05:30:00Z')
        self.assertEqual(next_due(before, 'daily'), parse_time('2026-11-02T08:00:00Z'))
        self.assertEqual(next_due(parse_time('2026-11-02T08:00:00Z'), 'daily'),
                         parse_time('2026-11-03T08:00:00Z'))

    def test_reset_tracks_both_countries_dst_and_month_end(self):
        cases = (
            ('2027-01-02T12:00:00Z', '2027-01-03T08:00:00Z', 8),
            ('2027-07-02T12:00:00Z', '2027-07-03T07:00:00Z', 8),
            ('2027-03-20T12:00:00Z', '2027-03-21T07:00:00Z', 7),
            ('2027-10-31T12:00:00Z', '2027-11-01T07:00:00Z', 7),
        )
        for stamp, expected, uk_hour in cases:
            with self.subTest(stamp=stamp):
                result = next_due(parse_time(stamp), 'daily')
                self.assertEqual(result, parse_time(expected))
                self.assertEqual(result.astimezone(ZoneInfo('Europe/London')).hour, uk_hour)
        self.assertEqual(next_due(parse_time('2028-01-31T12:00:00Z'), 'monthly'),
                         parse_time('2028-02-29T08:00:00Z'))

    def test_existing_midnight_work_waits_for_reset_but_unfinished_work_does_not(self):
        due = self.state.add('daily', frequency='daily', next_due='2026-11-02T00:00:00Z')
        unfinished = self.state.add('unfinished', last_attempt='2026-11-01T15:00:00Z')
        news = self.state.add('news', frequency='every-time')
        pending = self.state.add('pending', next_due='2026-11-02T00:00:00Z')
        self.state.begin_write(pending['id'], 'insert', {'video_id': 'unknown'})
        before = parse_time('2026-11-02T05:30:00Z')
        initialise_staggering(self.state, before)
        self.assertEqual(parse_time(self.state.by_id(due['id'])['next_due']),
                         parse_time('2026-11-02T08:00:00Z'))
        self.assertEqual({t['id'] for t in due_topics(self.state.topics(), 0, before)},
                         {unfinished['id'], news['id'], pending['id']})
        after = parse_time('2026-11-02T08:00:00Z')
        self.assertEqual(len(due_topics(self.state.topics(), 0, after)), 4)

    def test_migration_preserves_assigned_dates_slots_history_and_quota(self):
        self.state.set_meta('schedule_slots', {'1': {'frequency': 'weekly', 'slot': 4}})
        topic = self.state.add('weekly', frequency='weekly',
                               next_due='2027-07-02T23:00:00Z',
                               last_success='2027-06-26T12:00:00Z')
        self.state.charge('2027-07-02', 900)
        migrate_quota_days(self.state)
        self.assertEqual(self.state.by_id(topic['id'])['next_due'], '2027-07-03T07:00:00+00:00')
        self.assertEqual(self.state.by_id(topic['id'])['last_success'], topic['last_success'])
        self.assertEqual(self.state.meta('schedule_slots')['1']['slot'], 4)
        self.assertEqual(self.state.quota('2027-07-02')['used'], 900)
        rows = self.state.topics()
        self.state.set_meta('timezone', 'Asia/Tokyo')
        migrate_quota_days(self.state)
        self.assertEqual(self.state.topics(), rows)

    def test_staggering_uses_quota_day_despite_clock_timezone(self):
        before = parse_time('2026-11-02T05:30:00Z')
        topic = self.state.add('daily', frequency='daily')
        self.assertEqual(staggered_due(self.state, topic, before),
                         parse_time('2026-11-02T08:00:00Z'))
        self.state.set_meta('timezone', 'Asia/Tokyo')
        self.assertEqual(staggered_due(self.state, topic, before),
                         parse_time('2026-11-02T08:00:00Z'))

    def test_forecast_before_reset_labels_previous_quota_day_and_waits(self):
        self.state.add('daily', channels=[CHANNEL], next_due='2026-11-02T08:00:00Z')
        self.state.charge('2026-11-01', 900)
        before = parse_time('2026-11-02T05:30:00Z')
        graph = project(self.state, now=before, days=2, mode='budget')
        self.assertEqual(graph['day_timezone'], QUOTA_TIMEZONE)
        self.assertEqual([row['date'] for row in graph['days']], ['2026-11-01', '2026-11-02'])
        self.assertEqual([row['topics'] for row in graph['days']], [0, 1])
        self.assertEqual(graph['days'][0]['recorded_units'], 900)
        after = project(self.state, now=parse_time('2026-11-02T08:00:00Z'), days=1, mode='budget')
        self.assertEqual(after['days'][0]['date'], '2026-11-02')
        self.assertEqual(after['days'][0]['recorded_units'], 0)
        self.assertEqual(after['days'][0]['topics'], 1)

    def test_cli_migrates_old_due_dates_before_eligibility_or_authentication(self):
        self.state.add('daily', channels=[CHANNEL], frequency='daily',
                       last_success='2026-11-01T12:00:00Z', next_due='2026-11-02T00:00:00Z')
        self.state.set_meta('schedule_slots', {'1': {'frequency': 'daily', 'slot': 0}})
        with patch('youtube_playlist_builder.cli.utcnow', return_value=parse_time('2026-11-02T05:30:00Z')), \
             patch('youtube_playlist_builder.cli.authenticate') as auth, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--state', str(self.state.path), 'auto']), 0)
            auth.assert_not_called()
        self.assertEqual(self.state.topic('daily')['next_due'], '2026-11-02T08:00:00+00:00')

    def test_clock_timezone_change_does_not_move_due_dates(self):
        self.state.set_meta('schedule_day_basis', QUOTA_TIMEZONE)
        self.state.add('daily', channels=[CHANNEL], frequency='daily',
                       last_success='2026-11-01T12:00:00Z', next_due='2026-11-02T08:00:00Z')
        self.state.set_meta('schedule_slots', {'1': {'frequency': 'daily', 'slot': 0}})
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--state', str(self.state.path), 'settings', '--timezone', 'Asia/Tokyo']), 0)
        self.assertEqual(self.state.topic('daily')['next_due'], '2026-11-02T08:00:00Z')
