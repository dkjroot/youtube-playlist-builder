"""Calendar scheduling and fair queue order, independent of network operations.

The CLI assigns stable staggered slots as topics are created/edited; Sync advances
success dates only after complete updates. Due times are stored in UTC but slots
use local calendar days. A persisted cursor gives other topics a turn after a
quota pause; explicit all mode bypasses frequency without enabling disabled topics.
"""

import calendar
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

FREQUENCIES = ('daily', 'every-2-days', 'every-4-days', 'weekly', 'biweekly', 'monthly')
DAYS = dict(zip(FREQUENCIES[:-1], (1, 2, 4, 7, 14)))


def utcnow():
    """Return an aware clock value; tests inject clocks into API and sync instead of sleeping."""
    return datetime.now(timezone.utc)


def parse_time(value):
    """Normalise stored/API ISO timestamps to aware UTC datetimes for comparisons."""
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)


def next_due(now, frequency, timezone_name='UTC'):
    # Calendar days, not 24 hours after completion: a daily cron job must not
    # miss tomorrow just because today's update took a few seconds to finish.
    """Calculate the full interval’s local midnight before assigning a staggered slot."""
    now = now.astimezone(ZoneInfo(timezone_name))
    if frequency == 'monthly':
        year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        day = min(now.day, calendar.monthrange(year, month)[1])
        result = now.replace(year=year, month=month, day=day)
    else:
        result = now + timedelta(days=DAYS[frequency])
    return result.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def due_topics(topics, after_id, now, *, all_topics=False):
    """Take one enabled-topic snapshot in cursor order; all mode bypasses only due dates."""
    ordered = [topic for topic in topics if topic['id'] > after_id]
    ordered += [topic for topic in topics if topic['id'] <= after_id]
    return [topic for topic in ordered if topic['enabled'] and
            (all_topics or not topic['next_due'] or parse_time(topic['next_due']) <= now)]


def playlist_title(topic):
    """Apply the user override or derive the predictable sub_<topic> default."""
    return topic['playlist_name'] or 'sub_' + topic['name'].strip().lower().replace(' ', '_')


def staggered_due(state, topic, completed):
    """Keep a stable calendar slot, choosing a less crowded one on first use.

    The first interval may be shorter to establish the phase. Subsequent on-time
    runs retain that phase, instead of shortening every interval to fill gaps.
    """
    zone = state.meta('timezone', 'UTC')
    local = completed.astimezone(ZoneInfo(zone))
    nominal = next_due(completed, topic['frequency'], zone).astimezone(ZoneInfo(zone))
    frequency = topic['frequency']
    slots = state.meta('schedule_slots', {})
    entry = slots.get(str(topic['id']))
    monthly = frequency == 'monthly'
    def slot(date):
        """Represent a repeating phase as a day of month or an ordinal-day remainder."""
        return date.day if monthly else date.toordinal() % DAYS[frequency]
    candidates = [nominal - timedelta(days=i) for i in range((nominal.date() - local.date()).days)]
    if not entry or entry['frequency'] != frequency:
        peers = {str(row['id']) for row in state.topics() if row['enabled'] and row['id'] != topic['id']}
        counts = {}
        for key, value in slots.items():
            if key in peers and value['frequency'] == frequency:
                counts[value['slot']] = counts.get(value['slot'], 0) + 1
        chosen = min(candidates, key=lambda date: counts.get(slot(date), 0))
        entry = {'frequency': frequency, 'slot': slot(chosen)}
        slots[str(topic['id'])] = entry
        state.set_meta('schedule_slots', slots)
    return slot_due(completed, frequency, timezone_name=zone, entry=entry)


def slot_due(completed, frequency, timezone_name, entry, *, include_today=False):
    """Return the next local midnight matching a persisted phase, without modifying state."""
    local = completed.astimezone(ZoneInfo(timezone_name))
    for offset in range(0 if include_today else 1, 63):
        candidate = (local + timedelta(days=offset)).replace(hour=0,minute=0,second=0,microsecond=0)
        if frequency == 'monthly':
            match = candidate.day == min(entry['slot'],calendar.monthrange(candidate.year,candidate.month)[1])
        else:
            match = candidate.date().toordinal() % DAYS[frequency] == entry['slot']
        if match: return candidate.astimezone(timezone.utc)
    raise ValueError('Could not assign a calendar slot.')


def initialise_staggering(state, now):
    """Allocate new/changed slots, migrating future schedules without hiding due work."""
    slots = state.meta('schedule_slots', {})
    for topic in state.topics():
        entry = slots.get(str(topic['id']))
        if not topic['enabled'] or (entry and entry['frequency'] == topic['frequency']):
            continue
        anchor = parse_time(topic['last_success']) if topic['last_success'] else now
        scheduled = staggered_due(state, topic, anchor)
        if topic['next_due'] and parse_time(topic['next_due']) > now:
            state.update(topic['id'], next_due=scheduled.isoformat())
