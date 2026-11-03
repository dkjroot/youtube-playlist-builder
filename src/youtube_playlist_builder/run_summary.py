"""Human-readable outcomes counted from actual topic work and confirmed API replies."""

from .diagnostics import safe_text


class RunSummary:
    def __init__(self, api, *, preview=False, reconcile=False):
        self.api = api
        self.preview = preview
        self.reconcile = reconcile
        self.day = api.day()
        self.start_spent = api.spent
        self.start_successes = dict(api.successful_calls)
        self.selected = set()
        self.attempted = set()
        self.completed = set()
        self.failed = set()
        self.reason = None

    def snapshot(self):
        """Keep unfinished, failed and unvisited counts distinct from completion."""
        def changes(endpoint):
            return self.api.successful_calls.get(endpoint, 0) - self.start_successes.get(endpoint, 0)

        ledger = self.api.state.quota(self.day)
        return dict(
            topics=len(self.selected), completed=len(self.completed),
            unfinished=len(self.selected - self.completed - self.failed),
            failed=len(self.failed), not_reached=len(self.selected - self.attempted),
            added=changes('playlistItems.insert'), removed=changes('playlistItems.delete'),
            created=changes('playlists.insert'), renamed=changes('playlists.update'),
            units=self.api.spent - self.start_spent, day=self.day,
            daily_used=ledger['used'], daily_budget=self.api.daily_budget,
            daily_remaining=max(0, self.api.daily_budget - ledger['used']),
            reason=self.reason,
        )

    def lines(self, code):
        data = self.snapshot()
        outcome = ('Preview completed' if self.preview else 'Reconciliation completed' if self.reconcile else 'Completed') if code == 0 else (
            'Paused; progress saved' if code == 2 else 'Stopped; confirmed changes saved' if code == 130 else 'Finished with errors; confirmed changes saved')
        reason = safe_text(self.reason) if self.reason else ('No topics needed checking.' if not self.selected else '')
        if code == 2:
            reason = reason.removesuffix(' Progress has been saved.')
        yield f"Run summary: {outcome}." + (f" {reason}" if reason else '')
        action = 'previewed' if self.preview else 'reconciled' if self.reconcile else 'completed'
        yield (f"Topics: {data['completed']} of {data['topics']} {action}; "
               f"{data['unfinished']} unfinished; {data['failed']} failed. "
               f"{data['not_reached']} topics were not reached.")
        yield (f"Changes confirmed: videos added: {data['added']}; entries removed: {data['removed']}; "
               f"playlists created: {data['created']}; playlists renamed: {data['renamed']}.")
        yield (f"YouTube quota: {data['units']:,} units used this run; "
               f"{data['daily_used']:,} / {data['daily_budget']:,} used for Pacific day {data['day']}; "
               f"{data['daily_remaining']:,} units left in that day's local allowance.")
