"""Cost-weighted local schedule redistribution, preserving unfinished work.

Candidate phases are evaluated over at least 56 days so fortnightly work cannot simply
be pushed beyond the visible ten-day chart. Local forecast assumptions supply
costs; no remote reads or success timestamps are manufactured. The caller holds
the same cross-process lock as normal topic edits.
"""

import json

from .forecast import project, planning_days
from .schedule import DAYS, parse_time, slot_due, utcnow
from zoneinfo import ZoneInfo


def redistribute(state, *, rate=1.0, now=None):
    """Choose phases that reduce estimated peaks, then squared daily load.

    Start from current schedules and accept only strict improvements. Equal-cost
    alternatives stay put. Pending/partial updates retain their first due time;
    daily topics and disabled topics retain their dates. Future repeats use the
    chosen slots. A three-pass coordinate search bounds work for large accounts.
    """
    now = now or utcnow()
    zone = state.meta("timezone", "UTC")
    topics = [row for row in state.topics() if row["enabled"] and row["channels"]]
    slots = dict(state.meta("schedule_slots", {}))
    visible_days = planning_days(topics, now=now, timezone_name=zone)
    horizon = max(56, visible_days)
    before = project(state, rate=rate, now=now, days=visible_days)
    choices = {}
    current = {}
    preserved = 0
    for topic in topics:
        tid = topic["id"]
        frequency = topic["frequency"]
        fixed_due = (
            frequency == "daily"
            or state.pending(tid)
            or (not topic["next_due"] and topic["last_attempt"])
        )
        if fixed_due and frequency != "daily":
            preserved += 1
        original = slots.get(str(tid))
        if not original or original["frequency"] != frequency:
            date = parse_time(topic["next_due"]) if topic["next_due"] else now
            date = date.astimezone(ZoneInfo(zone)).date()
            original = dict(
                frequency=frequency,
                slot=(
                    date.day
                    if frequency == "monthly"
                    else date.toordinal() % DAYS[frequency]
                ),
            )

        def option(entry, due):
            """Precompute one topic's candidate workload once, without shared-run read overhead."""
            candidate = dict(topic, next_due=due)
            rows = project(
                state,
                rate=rate,
                days=horizon,
                now=now,
                topics=[candidate],
                schedule_slots={str(tid): entry},
                shared_reads=False,
            )["days"]
            return dict(entry=entry, due=due, cost=[row["units"] for row in rows])

        first = option(original, topic["next_due"])
        current[tid] = first
        choices[tid] = [first]
        if frequency == "daily":
            continue
        phases = range(1, 32) if frequency == "monthly" else range(DAYS[frequency])
        already_today = (
            topic["last_success"]
            and parse_time(topic["last_success"]).astimezone(ZoneInfo(zone)).date()
            == now.astimezone(ZoneInfo(zone)).date()
        )
        for phase in phases:
            entry = dict(frequency=frequency, slot=phase)
            due = (
                topic["next_due"]
                if fixed_due
                else slot_due(
                    now, frequency, zone, entry, include_today=not already_today
                ).isoformat()
            )
            if entry != original or due != topic["next_due"]:
                choices[tid].append(option(entry, due))
    total = [
        sum(current[tid]["cost"][day] for tid in current) for day in range(horizon)
    ]

    def score(costs):
        """Prefer lower worst-day cost, using variance-like energy to break peak ties."""
        return (max(costs, default=0), sum(cost * cost for cost in costs))

    order = sorted(current, key=lambda tid: (-max(current[tid]["cost"]), tid))
    for _ in range(3):
        improved = False
        for tid in order:
            previous = current[tid]
            base = [total[day] - previous["cost"][day] for day in range(horizon)]
            best, best_total, best_score = previous, total, score(total)
            for candidate in choices[tid]:
                trial = [base[day] + candidate["cost"][day] for day in range(horizon)]
                candidate_score = score(trial)
                if candidate_score < best_score:
                    best, best_total, best_score = candidate, trial, candidate_score
            if best is not previous:
                current[tid], total = best, best_total
                improved = True
        if not improved:
            break
    proposed_slots = dict(slots)
    proposed_topics = []
    for topic in topics:
        chosen = current[topic["id"]]
        proposed_slots[str(topic["id"])] = chosen["entry"]
        proposed_topics.append(dict(topic, next_due=chosen["due"]))
    baseline = project(state, rate=rate, now=now, days=horizon)
    proposed = project(
        state,
        rate=rate,
        now=now,
        days=horizon,
        topics=proposed_topics,
        schedule_slots=proposed_slots,
    )
    # Shared run reads can break an otherwise equal per-topic score. Verify the
    # combined estimate before committing; redistribution must not worsen it.
    accept = score([row["units"] for row in proposed["days"]]) < score(
        [row["units"] for row in baseline["days"]]
    )
    changed = []
    if accept:
        with state.db:
            for topic in topics:
                chosen = current[topic["id"]]
                key = str(topic["id"])
                if (
                    slots.get(key) != chosen["entry"]
                    or topic["next_due"] != chosen["due"]
                ):
                    changed.append(
                        dict(
                            name=topic["name"],
                            next_due=chosen["due"],
                            frequency=topic["frequency"],
                        )
                    )
                state._update(topic["id"], {"next_due": chosen["due"]})
            state.db.execute(
                "INSERT OR REPLACE INTO meta VALUES (?,?)",
                ("schedule_slots", json.dumps(proposed_slots)),
            )
    after = project(state, rate=rate, now=now, days=visible_days)
    return dict(
        changed=changed,
        preserved_due=preserved,
        before=before,
        after=after,
        message=(
            f"Redistributed {len(changed)} topic schedules. "
            if changed
            else "No better schedule found under the current assumptions. "
        )
        + f"{preserved} unfinished topics kept their due dates. No API calls made.",
    )
