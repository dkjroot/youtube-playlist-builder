"""Bounded, private pre-run forecast versus actual usage records, with no extra API calls."""
import json
import os
import uuid

from .diagnostics import logger, safe_text
from .forecast import project, planning_days
from .forecast_calibration import topic_calibration, signature
from .schedule import due_topics, parse_time


class RunDiagnostics:
    """Capture comparable run inputs and results independently of rotating request logs."""
    def __init__(self, sync, *, topic_name=None, preview=False, reconcile_only=False, all_topics=False):
        """Save forecasts before account reads or mutations; explicit Update all gets its own scope."""
        self.sync = sync
        self.run_id = uuid.uuid4().hex
        self.started = sync.now().isoformat()
        self.forecasts = {}
        self.plans = {}
        self.completed = set()
        self.data = dict(operation='preview' if preview else 'reconcile' if reconcile_only else 'update' if topic_name else 'all' if all_topics else 'due',
                         preview=preview, run_budget=sync.api.run_budget, daily_budget=sync.api.daily_budget)
        state, now = sync.state, sync.now()
        try:
            options = json.loads(os.environ.get('YT_PLAYLISTS_FORECAST', '{}'))
            rate = float(options.get('rate', 1))
            mode = options.get('mode', 'budget')
            calibrate = options.get('calibrate', False)
            if type(calibrate) is not bool:
                raise ValueError('Invalid calibration setting')
            corrections = topic_calibration(state, state.topics(), rate=rate, now=now) if calibrate else {}
            days = options.get('days', 'auto')
            horizon = planning_days(state.topics(), now=now, timezone_name=state.meta('timezone', 'UTC')) if days == 'auto' else int(days)
            if not 1 <= horizon <= 366:
                raise ValueError('Invalid forecast period')
            graph = project(state, rate=rate, days=horizon, mode=mode, now=now, calibrate=calibrate, calibration=corrections)
            self.data['forecast_source'] = 'web_controls' if 'YT_PLAYLISTS_FORECAST' in os.environ else 'default_scenario'
            self.data['graph_forecast'] = graph
            selected = [state.topic(topic_name)] if topic_name else due_topics(state.topics(), state.meta('cursor', 0), now, all_topics=all_topics)
            self.data['scope_forecast'] = project(
                state, topics=[dict(t, enabled=True, next_due=None) for t in selected],
                rate=rate, days=1, now=now, calibrate=calibrate, calibration=corrections
            )['days'][0]
            for topic in selected:
                # A manual/all run ignores due dates; keep a separate estimate for
                # its actual scope rather than confusing it with today's chart bar.
                forced = dict(topic, enabled=True, next_due=None)
                self.forecasts[str(topic['id'])] = dict(
                    topic=safe_text(topic['name']), frequency=topic['frequency'],
                    signature=signature(state, topic),
                    channels=len(topic['channels']), max_videos=topic['max_videos'],
                    max_age_days=topic['max_age_days'], last_success=topic['last_success'],
                    next_due=topic['next_due'],
                    estimate=project(state, topics=[forced], shared_reads=False, rate=rate, days=1, now=now, calibrate=calibrate, calibration=corrections)['days'][0],
                )
            self.data['forecast_settings'] = dict(rate=rate, mode=mode, days=horizon)
            if 'calibrate' in options:
                self.data['forecast_settings']['calibrate'] = calibrate
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            # Diagnostics must not turn an invalid optional scenario into a failed sync.
            self.data['forecast_error'] = safe_text(error)
        self.save('running')
        logger.info('Run diagnostics started run_id=%s operation=%s forecast_source=%s', self.run_id, self.data['operation'], self.data.get('forecast_source', 'unavailable'))

    def plan(self, topic, desired, additions, removals, creates, renames):
        """Keep a complete discovery plan apart from partially executed mutations."""
        counts = {}
        new_counts = {}
        last = parse_time(topic['last_success']) if topic['last_success'] else None
        for video in desired.values():
            cid = video['channel_id']
            counts[cid] = counts.get(cid, 0) + 1
            if last and parse_time(video['published_at']) > last:
                new_counts[cid] = new_counts.get(cid, 0) + 1
        if str(topic['id']) in self.plans:
            self.data.setdefault('previous_plans', {}).setdefault(str(topic['id']), []).append(self.plans[str(topic['id'])])
        self.plans[str(topic['id'])] = dict(
            selected=len(desired), adds=len(additions), removes=len(removals),
            creates=int(creates), renames=int(renames), selected_by_channel=counts,
            selected_new_since_success=new_counts,
            selection_is_capped=topic.get('max_videos') is not None,
            planned_write_units=50 * (len(additions) + len(removals) + int(creates) + int(renames)),
        )
        self.save('running')

    def save(self, status, finished=None):
        """Persist summaries in SQLite so log rotation cannot lose tomorrow's comparison."""
        payload = dict(self.data, topics=self.forecasts, plans=self.plans,
                       completed_topics=sorted(self.completed),
                       usage=self.sync.api.run_usage, usage_by_topic=self.sync.api.topic_usage,
                       actual_units=self.sync.api.spent,
                       llm_calls=self.sync.classifier.calls if self.sync.classifier is not None and hasattr(self.sync.classifier, 'calls') else None)
        # Mock classifiers may not expose an integer counter.
        if not isinstance(payload['llm_calls'], (int, type(None))):
            payload['llm_calls'] = None
        with self.sync.state.db:
            self.sync.state.db.execute(
                'INSERT OR REPLACE INTO run_diagnostics VALUES (?,?,?,?,?)',
                (self.run_id, self.started, finished, status, json.dumps(payload)),
            )
            self.sync.state.db.execute(
                'DELETE FROM run_diagnostics WHERE run_id NOT IN '
                '(SELECT run_id FROM run_diagnostics ORDER BY started_at DESC,run_id DESC LIMIT 120)'
            )

    def finish(self, code, error=None):
        """Record partial/failed runs too, preserving the original outcome and error context."""
        status = 'completed' if code == 0 else 'paused' if code == 2 else 'interrupted' if code == 130 else 'failed'
        if error is not None:
            self.data['error'] = safe_text(error)
        self.save(status, self.sync.now().isoformat())
        logger.info('Run diagnostics finished run_id=%s status=%s actual_units=%s', self.run_id, status, self.sync.api.spent)
