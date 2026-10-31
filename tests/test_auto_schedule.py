"""Exercise server scheduling with injected clocks, without waiting or API calls."""

from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from youtube_playlist_builder.auto_schedule import AutoSchedule, validate_times
from youtube_playlist_builder.schedule import parse_time
from youtube_playlist_builder.state import State
from youtube_playlist_builder.web import Jobs


class AutoScheduleTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.args = SimpleNamespace(state=Path(temp.name) / 'state.db')
        self.state = State(self.args.state)
        self.addCleanup(self.state.close)
        self.now = parse_time('2026-10-31T08:00:00Z')
        self.jobs = Mock()
        self.scheduler = AutoSchedule(self.args, self.jobs, now=lambda: self.now)
        self.scheduler.last_check = self.now

    def times(self, *times, zone='UTC'):
        self.state.set_meta('schedule_times', list(times))
        self.state.set_meta('timezone', zone)

    def advance(self, minutes):
        self.now += timedelta(minutes=minutes)
        self.scheduler.check()

    def test_validation_deduplicates_and_rejects_invalid_values(self):
        self.assertEqual(validate_times(['23:59', '00:00', '23:59']), ['00:00', '23:59'])
        self.assertEqual(validate_times([]), [])
        for value in (None, '08:00', [800], ['24:00'], ['08:60'], ['8:00'],
                      ['08:00:00'], [' 08:00'], ['0١:00']):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_times(value)
        self.assertEqual(len(validate_times([f'{h:02}:{m:02}' for h in range(24) for m in range(60)])), 1440)

    def test_window_excludes_start_includes_end_and_coalesces_times(self):
        self.times('08:00', '08:01', '08:03', '08:05')
        self.advance(5)
        self.jobs.start.assert_called_once_with('auto', scheduled=True)
        self.advance(5)
        self.jobs.start.assert_called_once()

    def test_only_last_five_minutes_after_long_sleep_are_considered(self):
        self.times('08:05', '08:10')
        self.advance(60)
        self.jobs.start.assert_not_called()
        self.times('09:02')
        self.advance(5)
        self.jobs.start.assert_called_once()

    def test_first_check_after_start_does_not_catch_up_and_shutdown_is_immediate(self):
        self.times('07:59', '08:00')
        with patch('youtube_playlist_builder.auto_schedule.threading.Thread') as thread:
            self.scheduler.start()
            thread.return_value.start.assert_called_once()
            self.advance(5)
            self.jobs.start.assert_not_called()
            self.scheduler.close()
            self.assertTrue(self.scheduler.stopped.is_set())
            thread.return_value.join.assert_called_once()

    def test_midnight_in_configured_timezone(self):
        self.now = parse_time('2026-10-31T23:58:00Z')
        self.scheduler.last_check = self.now
        self.times('00:00', zone='Europe/London')
        self.advance(5)
        self.jobs.start.assert_called_once()

    def test_spring_gap_is_skipped_and_autumn_repeat_runs_once(self):
        self.times('01:30', zone='Europe/London')
        self.now = parse_time('2027-03-28T00:28:00Z')
        self.scheduler.last_check = self.now
        self.advance(5)
        self.jobs.start.assert_not_called()
        self.now = parse_time('2027-10-31T00:28:00Z')
        self.scheduler.last_check = self.now
        self.advance(5)
        self.jobs.start.assert_called_once()
        self.now = parse_time('2027-10-31T01:28:00Z')
        self.scheduler.last_check = self.now
        self.advance(5)
        self.jobs.start.assert_called_once()

    def test_backward_clock_and_repeated_checks_do_not_duplicate_jobs(self):
        self.times('08:03')
        self.advance(5)
        self.scheduler.check()
        self.advance(-4)
        self.advance(4)
        self.jobs.start.assert_called_once()

    def test_removed_times_and_empty_schedule_do_not_start_jobs(self):
        self.times('08:04')
        self.times()
        self.advance(5)
        self.jobs.start.assert_not_called()

    def test_scheduler_waits_300_seconds_and_survives_local_errors(self):
        self.scheduler.stopped = Mock()
        self.scheduler.stopped.wait.side_effect = [False, False, True]
        with patch.object(self.scheduler, 'check', side_effect=[OSError('busy'), None]) as check:
            with self.assertLogs('youtube_playlist_builder.auto_schedule', level='ERROR'):
                self.scheduler._run()
        self.assertEqual(check.call_count, 2)
        self.assertTrue(all(call.args == (300,) for call in self.scheduler.stopped.wait.call_args_list))

    def test_busy_scheduled_run_is_skipped_without_queue_or_replacement(self):
        jobs = Jobs(self.args)
        jobs.job = {'id': 'existing', 'running': True}
        self.scheduler.jobs = jobs
        self.times('08:03')
        with patch('youtube_playlist_builder.web.threading.Thread') as thread:
            self.advance(5)
            self.assertEqual(jobs.job, {'id': 'existing', 'running': True})
            jobs.job['running'] = False
            self.advance(5)
            thread.assert_not_called()
        jobs.closed = True
        self.assertIsNone(jobs.start('auto', scheduled=True))

    def test_scheduled_jobs_use_normal_auto_and_have_visible_source(self):
        jobs = Jobs(self.args)
        with patch('youtube_playlist_builder.web.threading.Thread') as thread:
            job_id = jobs.start('auto', scheduled=True)
        self.assertEqual(jobs.snapshot()['id'], job_id)
        self.assertTrue(jobs.snapshot()['scheduled'])
        self.assertEqual(thread.call_args.kwargs['args'][1], ['auto'])
