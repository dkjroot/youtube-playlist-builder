"""Budgeted YouTube transport for the managed playlist workflow.

Sync sends every managed read/write through YouTube here. Charge the persistent
Pacific-day ledger before dispatch, check the per-run allowance, and translate
quota, HTTP and uncertain network failures into distinct recovery decisions.
The standalone setup proof and subscription export use their own small paths.
"""

import json
import time
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError
from google.auth.exceptions import GoogleAuthError
import httplib2
from requests.exceptions import RequestException

from .terminal import waiting
from .schedule import utcnow
from .diagnostics import logger, safe_text


class QuotaStop(Exception):
    """A resumable budget/rate-limit pause, mapped to exit code 2 by the CLI."""
    pass


class ApiFailure(Exception):
    """A definite HTTP failure with a safe status/reason, not a raw credential-bearing response."""
    def __init__(self, status, reason):
        """Keep structured status for recovery decisions and a short user-facing message."""
        self.status, self.reason = status, reason
        token = ''.join(c for c in reason.lower() if c.isalnum())
        self.uncertain = status >= 500 or status == 408 or token in (
            'serviceunavailable', 'backenderror', 'internalerror', 'internal', 'unavailable'
        )
        self.stop_run = self.uncertain or status == 401
        super().__init__(f'YouTube returned HTTP {status}: {reason}.')


class ConnectionFailure(Exception):
    """An uncertain transport outcome; pending writes must be reconciled before retrying."""
    pass


def error_reasons(content):
    """Read allowlisted structured reason fields from old and Google RPC errors.

    Do not infer daily exhaustion merely from RESOURCE_EXHAUSTED: that can also
    mean a temporary rate limit. Malformed error bodies still preserve HTTP status.
    """
    try:
        body = json.loads(content)
        error = body.get('error', {}) if isinstance(body, dict) else {}
        if not isinstance(error, dict):
            return []
        reasons = []
        for field in ('errors', 'details'):
            entries = error.get(field, [])
            if isinstance(entries, list):
                reasons.extend(entry['reason'] for entry in entries
                               if isinstance(entry, dict) and isinstance(entry.get('reason'), str))
        reasons.extend(error[field] for field in ('reason', 'status') if isinstance(error.get(field), str))
        return reasons
    except (ValueError, TypeError):
        return []


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
            logger.warning('YouTube call blocked: confirmed daily exhaustion day=%s', self.day())
            raise QuotaStop('YouTube reported the daily quota exhausted. Resume after midnight Pacific time.')
        if ledger['used'] + cost > self.daily_budget:
            logger.warning('YouTube call blocked: local daily budget used=%s limit=%s next_cost=%s', ledger['used'], self.daily_budget, cost)
            raise QuotaStop('The local daily API budget is used up. Progress has been saved.')
        if self.spent + cost > self.run_budget:
            logger.warning('YouTube call blocked: local run budget used=%s limit=%s next_cost=%s', self.spent, self.run_budget, cost)
            raise QuotaStop('The API budget for this run is used up. Progress has been saved.')

    def call(self, resource, method='list', **parameters):
        """Charge before dispatch and translate failures without automatic write retries.

        Each list page costs one unit; the supported playlist mutations cost 50.
        Charge failed requests conservatively, since the server may have received them.
        """
        cost = 1 if method == 'list' else 50
        self.check(cost)
        day = self.day()
        self.state.charge(day, cost, resource=resource, method=method)
        self.spent += cost
        started = time.monotonic()
        logger.info('YouTube request resource=%s method=%s cost=%s run_spent=%s daily_spent=%s',
                    resource, method, cost, self.spent, self.state.quota(day)['used'])
        try:
            with waiting(f'YouTube: {resource}.{method}…'):
                result = getattr(getattr(self.service, resource)(), method)(**parameters).execute(num_retries=0)
            logger.info('YouTube response resource=%s method=%s elapsed=%.2fs outcome=success',
                        resource, method, time.monotonic() - started)
            return result
        except HttpError as error:
            reasons = error_reasons(error.content)
            tokens = {''.join(c for c in reason.lower() if c.isalnum()) for reason in reasons}
            logger.warning('YouTube response resource=%s method=%s HTTP=%s elapsed=%.2fs reasons=%s',
                           resource, method, error.resp.status, time.monotonic() - started,
                           safe_text(','.join(reasons)) or 'unavailable')
            if tokens & {'quotaexceeded', 'dailylimitexceeded', 'dailylimitexceededunreg'}:
                self.state.exhaust(day)
                raise QuotaStop('YouTube daily quota exhausted. Progress saved; resume after midnight Pacific time.') from None
            if error.resp.status == 429 or tokens & {'ratelimitexceeded', 'userratelimitexceeded', 'resourceexhausted'}:
                raise QuotaStop('YouTube is temporarily limiting requests. Progress saved; try again later.') from None
            # A transient reason may appear after an ordinary reason or under a
            # nonstandard HTTP status (e.g. 409 SERVICE_UNAVAILABLE).
            transient = next((reason for reason in reasons if ''.join(c for c in reason.lower() if c.isalnum()) in
                              {'serviceunavailable', 'backenderror', 'internalerror', 'internal', 'unavailable'}), None)
            raise ApiFailure(error.resp.status, safe_text(transient or (reasons[0] if reasons else 'request failed'))) from None
        except (OSError, httplib2.HttpLib2Error, RequestException, GoogleAuthError):
            logger.warning('YouTube response resource=%s method=%s elapsed=%.2fs outcome=connection_failure pending_write_may_need_reconciliation',
                           resource, method, time.monotonic() - started)
            raise ConnectionFailure('The connection to Google failed or timed out. Progress saved; uncertain writes will be reconciled before retrying.') from None

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
