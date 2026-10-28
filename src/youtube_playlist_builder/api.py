"""Budgeted YouTube transport for the managed playlist workflow.

Sync sends every managed read/write through YouTube here. Charge the persistent
Pacific-day ledger before dispatch, check the per-run allowance, and translate
quota, HTTP and uncertain network failures into distinct recovery decisions.
The standalone setup proof and subscription export use their own small paths.
"""

import json
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError
from google.auth.exceptions import GoogleAuthError
import httplib2
from requests.exceptions import RequestException

from .terminal import waiting
from .schedule import utcnow


class QuotaStop(Exception):
    """A resumable budget/rate-limit pause, mapped to exit code 2 by the CLI."""
    pass


class ApiFailure(Exception):
    """A definite HTTP failure with a safe status/reason, not a raw credential-bearing response."""
    def __init__(self, status, reason):
        """Keep structured status for recovery decisions and a short user-facing message."""
        self.status, self.reason = status, reason
        super().__init__(f'YouTube returned HTTP {status}: {reason}.')


class ConnectionFailure(Exception):
    """An uncertain transport outcome; pending writes must be reconciled before retrying."""
    pass


class YouTube:
    """The single quota-charging gateway used by managed topic updates."""
    def __init__(self, service, state, *, daily_budget=9500, run_budget=9500, now=utcnow):
        """Bind a Google service to a persistent daily ledger and a fresh per-run allowance."""
        self.service, self.state = service, state
        self.daily_budget, self.run_budget = daily_budget, run_budget
        self.spent = 0
        self.now = now

    def day(self):
        """Use YouTube’s Pacific reset date, independently of the user’s scheduling timezone."""
        return self.now().astimezone(ZoneInfo('America/Los_Angeles')).date().isoformat()

    def remaining(self):
        """Return conservative remaining units across the shared daily and run budgets."""
        ledger = self.state.quota(self.day())
        if ledger['exhausted']:
            return 0
        return max(0, min(self.run_budget - self.spent, self.daily_budget - ledger['used']))

    def check(self, cost):
        """Reject a request before dispatch when its full cost cannot fit either allowance."""
        ledger = self.state.quota(self.day())
        if ledger['exhausted']:
            raise QuotaStop('YouTube reported the daily quota exhausted. Resume after midnight Pacific time.')
        if ledger['used'] + cost > self.daily_budget:
            raise QuotaStop('The local daily API budget is used up. Progress has been saved.')
        if self.spent + cost > self.run_budget:
            raise QuotaStop('The API budget for this run is used up. Progress has been saved.')

    def call(self, resource, method='list', **parameters):
        """Charge before dispatch and translate failures without automatic write retries.

        Each list page costs one unit; the supported playlist mutations cost 50.
        Charge failed requests conservatively, since the server may have received them.
        """
        cost = 1 if method == 'list' else 50
        self.check(cost)
        day = self.day()
        self.state.charge(day, cost)
        self.spent += cost
        try:
            with waiting(f'YouTube: {resource}.{method}…'):
                return getattr(getattr(self.service, resource)(), method)(**parameters).execute(num_retries=0)
        except HttpError as error:
            try:
                reasons = [entry.get('reason', '') for entry in json.loads(error.content)['error'].get('errors', [])]
            except (ValueError, KeyError, TypeError):
                reasons = []
            if any(reason in ('quotaExceeded', 'dailyLimitExceeded', 'dailyLimitExceededUnreg') for reason in reasons):
                self.state.exhaust(day)
                raise QuotaStop('YouTube daily quota exhausted. Progress saved; resume after midnight Pacific time.') from None
            if error.resp.status == 429 or any(reason in ('rateLimitExceeded', 'userRateLimitExceeded') for reason in reasons):
                raise QuotaStop('YouTube is temporarily limiting requests. Progress saved; try again later.') from None
            raise ApiFailure(error.resp.status, reasons[0] if reasons else 'request failed') from None
        except (OSError, httplib2.HttpLib2Error, RequestException, GoogleAuthError):
            raise ConnectionFailure('The connection to Google failed. Saved progress will be recovered on the next run.') from None

    def pages(self, resource, **parameters):
        """Yield complete pages lazily so callers can stop scanning old uploads early."""
        while True:
            response = self.call(resource, **parameters)
            yield response
            if not response.get('nextPageToken'):
                break
            parameters['pageToken'] = response['nextPageToken']

    def all_items(self, resource, **parameters):
        """Collect every page; callers must not interpret a partial read as a complete snapshot."""
        return [item for page in self.pages(resource, **parameters) for item in page.get('items', [])]
