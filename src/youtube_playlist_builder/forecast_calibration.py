"""Learn topic-specific forecast bias from complete, comparable local update runs."""
import hashlib
import json
from datetime import timedelta

from .schedule import parse_time, utcnow


def signature(state, topic):
    """Invalidate learning when channel choices, matching rules, limits or cadence change."""
    fields = {key: topic.get(key) for key in (
        'channels', 'frequency', 'max_age_days', 'max_videos', 'channel_limits',
        'channel_max_age_days', 'include_words', 'exclude_words', 'description')}
    fields.update(mode=state.meta('mode', 'text'), minimum_video_seconds=state.meta('minimum_video_seconds', 0))
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def topic_calibration(state, topics, *, rate=1.0, now=None):
    """Use up to five complete samples per topic from the last thirty days.

    Learn additions and discovery reads separately. Known expirations, removals
    caused by channel edits, and playlist creation retain their real unit costs.
    Older records bootstrap using their available configuration fields; new ones
    use a full matching/settings signature. Paused/failed/recovery runs don't train.
    """
    now = now or utcnow()
    current = {str(t['id']): t for t in topics}
    signatures = {tid: signature(state, topic) for tid, topic in current.items()}
    samples = {tid: [] for tid in current}
    rows = state.db.execute("SELECT started_at,payload FROM run_diagnostics WHERE status='completed' ORDER BY started_at DESC LIMIT 120")
    for row in rows:
        try:
            started = parse_time(row['started_at'])
            if started > now or started < now - timedelta(days=30):
                continue
            data = json.loads(row['payload'])
            if data.get('preview') or data.get('operation') not in ('due', 'all', 'update') or data.get('forecast_settings', {}).get('rate') != rate:
                continue
            recovered = {str(entry['topic_id']) for entry in data.get('conflict_recoveries', [])}
            for tid, saved in data.get('topics', {}).items():
                if tid not in current or tid in recovered or len(samples[tid]) >= 5 or int(tid) not in data.get('completed_topics', []):
                    continue
                topic = current[tid]
                if saved.get('signature'):
                    if saved['signature'] != signatures[tid]:
                        continue
                elif not all(saved.get(key) == value for key, value in (
                    ('topic', topic['name']), ('frequency', topic['frequency']),
                    ('channels', len(topic['channels'])), ('max_videos', topic['max_videos']),
                    ('max_age_days', topic['max_age_days']))):
                    continue
                plan = data.get('plans', {}).get(tid)
                estimate = saved.get('estimate', {})
                usage = data.get('usage_by_topic', {}).get(tid)
                if plan is None or usage is None or plan.get('creates') or plan.get('renames'):
                    continue
                raw_adds = estimate.get('raw_adds', estimate.get('adds', 0))
                raw_reads = estimate.get('raw_reads', estimate.get('reads', 0))
                actual_reads = sum(v['units'] for key, v in usage.items() if key.endswith('.list'))
                measured = (raw_adds, plan['adds'], raw_reads, actual_reads)
                if any(type(value) is not int or value < 0 for value in measured):
                    continue
                samples[tid].append(measured)
        except (ValueError, TypeError, KeyError, AttributeError):
            continue  # Diagnostic records are optional; malformed history is ignored.
    result = {}
    for tid, values in samples.items():
        if not values:
            continue
        predicted_adds = sum(v[0] for v in values)
        predicted_reads = sum(v[2] for v in values)
        result[tid] = dict(
            additions=max(.1, min(1.5, sum(v[1] for v in values) / predicted_adds)) if predicted_adds else 1.0,
            reads=max(.5, min(4.0, sum(v[3] for v in values) / predicted_reads)) if predicted_reads else 1.0,
            samples=len(values),
        )
    return result
