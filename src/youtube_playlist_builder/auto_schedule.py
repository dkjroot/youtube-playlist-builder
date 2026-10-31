"""Daily server-side update times, checked once every five minutes.

Only the current server session's last five minutes are eligible. There is no
persistent catch-up queue, and overlapping jobs are skipped rather than delayed.
"""

from datetime import timedelta
import logging
import re
import threading
from zoneinfo import ZoneInfo

from .schedule import utcnow
from .state import State

logger = logging.getLogger(__name__)
CHECK_SECONDS = 300


def validate_times(times):
    """Return sorted, unique 24-hour clock times without imposing a list limit."""
    if not isinstance(times, list) or any(
        not isinstance(value, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value)
        for value in times
    ):
        raise ValueError('Use a list of times in 24-hour HH:MM format.')
    return sorted(set(times))


class AutoSchedule:
    """Wake at five-minute intervals while the HTTP server is serving requests."""

    def __init__(self, args, jobs, *, now=utcnow):
        self.args = args
        self.jobs = jobs
        self.now = now
        self.last_check = None
        self.seen = {}
        self.stopped = threading.Event()
        self.thread = None

    def start(self):
        """Start from now, so starting the server never catches up missed times."""
        self.last_check = self.now()
        self.thread = threading.Thread(target=self._run, daemon=True, name='update-schedule')
        self.thread.start()

    def close(self):
        """Interrupt the five-minute wait immediately during server shutdown."""
        self.stopped.set()
        if self.thread:
            self.thread.join()

    def _run(self):
        while not self.stopped.wait(CHECK_SECONDS):
            try:
                self.check()
            except Exception:
                # A failed local read must not kill the server's scheduling loop.
                logger.exception('Scheduled update check failed; this window was skipped')

    def check(self):
        """Coalesce eligible times into one normal update-due job, without retries."""
        end = self.now()
        previous = self.last_check
        self.last_check = end
        if previous is None or end <= previous:
            return
        start = max(previous, end - timedelta(seconds=CHECK_SECONDS))
        state = State(self.args.state)
        try:
            times = validate_times(state.meta('schedule_times', []))
            zone_name = state.meta('timezone', 'UTC')
        finally:
            state.close()
        if not times:
            return
        zone = ZoneInfo(zone_name)
        self.seen = {key: stamp for key, stamp in self.seen.items()
                     if stamp >= end - timedelta(days=2)}
        # Walk at most five UTC minute boundaries; local conversion handles
        # midnight and DST without inventing nonexistent local clock times.
        minute = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
        matched = False
        while minute <= end:
            local = minute.astimezone(zone)
            clock = local.strftime('%H:%M')
            key = (zone_name, local.date().isoformat(), clock)
            if clock in times and key not in self.seen:
                self.seen[key] = end
                matched = True
            minute += timedelta(minutes=1)
        if matched and not self.stopped.is_set():
            job_id = self.jobs.start('auto', scheduled=True)
            logger.info('Scheduled update %s', 'started' if job_id else 'skipped: another job is running or server is closing')
