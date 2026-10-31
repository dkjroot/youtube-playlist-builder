"""Multiweek, local-only quota scenarios, never claims of known future uploads.

Known playlist publication dates and schedules anchor the simulation. The chosen
matching-video rate supplies unknown arrivals; unavailable channel IDs in legacy
records are spread across assigned channels. Reads are approximate, especially
when many uploads fail filters. No API/model calls are made by this module.
"""

from datetime import timedelta
import math
from collections import deque
from zoneinfo import ZoneInfo

from .limits import channel_age_days
from .forecast_calibration import topic_calibration
from .schedule import DAYS, parse_time, next_due, slot_due, utcnow


def planning_days(topics, *, now=None, timezone_name="UTC"):
    """Cover two cycles of the longest active cadence, including its first due date."""
    now = now or utcnow()
    active = [topic for topic in topics if topic["enabled"] and topic["channels"]]
    longest = max(
        (
            31 if topic["frequency"] == "monthly" else (1 if topic["frequency"] == "every-time" else DAYS[topic["frequency"]])
            for topic in active
        ),
        default=14,
    )
    first = max(
        (
            max(
                0,
                (
                    parse_time(topic["next_due"])
                    .astimezone(ZoneInfo(timezone_name))
                    .date()
                    - now.astimezone(ZoneInfo(timezone_name)).date()
                ).days,
            )
            for topic in active
            if topic["next_due"] and topic["frequency"] != "every-time"
        ),
        default=0,
    )
    return min(366, max(28, 2 * longest, first + longest + 1))


def project(
    state,
    *,
    rate=1.0,
    days=10,
    now=None,
    topics=None,
    schedule_slots=None,
    shared_reads=True,
    mode="demand",
    calibrate=False,
    calibration=None,
):
    """Simulate scheduled updates, firm slots, expiry and replacements under an explicit rate."""
    if not math.isfinite(rate) or not 0 <= rate <= 1000:
        raise ValueError(
            "Use an expected rate between 0 and 1000 matching videos per channel per day."
        )
    if mode not in ("demand", "budget"):
        raise ValueError("Choose demand or budget forecast mode.")
    now = now or utcnow()
    zone = state.meta("timezone", "UTC")
    today = now.astimezone(ZoneInfo(zone)).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    topics = [
        dict(row)
        for row in (state.topics() if topics is None else topics)
        if row["enabled"] and row["channels"]
    ]
    calibration = (topic_calibration(state, topics, rate=rate, now=now) if calibration is None else calibration) if calibrate else {}
    calibration = {tid: value for tid, value in calibration.items() if tid in {str(t['id']) for t in topics}}
    calibrated_fractional = {}
    schedule_slots = (
        state.meta("schedule_slots", {}) if schedule_slots is None else schedule_slots
    )
    cache = state.meta("subscription_cache") or {}
    if not isinstance(cache, dict):
        cache = {}
    subscription_pages = math.ceil(len(cache.get("channels", [])) / 50)
    cache_hours = state.meta("subscription_cache_hours", 24)
    try:
        subscription_fetched = parse_time(cache["fetched_at"])
        if subscription_fetched > now:
            subscription_fetched = None
    except (KeyError, ValueError, TypeError, AttributeError):
        subscription_fetched = None
    inventory = {}
    fractional = {}
    for topic in topics:
        groups = {cid: [] for cid in topic["channels"]}
        for index, item in enumerate(state.items(topic["id"])):
            cid = (
                item.get("channel_id")
                or topic["channels"][index % len(topic["channels"])]
            )
            if cid not in groups:
                cid = "__removed__"
            groups.setdefault(cid, []).append(
                parse_time(item["published_at"])
                if item["published_at"]
                else now - timedelta(days=3650)
            )
        inventory[topic["id"]] = groups
    rows = []
    for offset in range(days):
        date = today + timedelta(days=offset)
        row = dict(
            date=date.date().isoformat(), topics=0, reads=0, adds=0, removes=0, units=0, raw_adds=0, raw_reads=0
        )
        for topic in topics:
            due = parse_time(topic["next_due"]) if topic["next_due"] else now
            if topic["frequency"] != "every-time" and due.astimezone(ZoneInfo(zone)).date() > date.date():
                continue
            row["topics"] += 1
            topic_reads = 0
            correction = calibration.get(str(topic['id']), {})
            if not topic["playlist_id"]:
                row["units"] += 50
                topic["playlist_id"] = "forecast-only"
            row["removes"] += len(inventory[topic["id"]].pop("__removed__", []))
            for cid in topic["channels"]:
                slots = (
                    topic["channel_limits"].get(cid, 0)
                    if topic.get("max_videos") is not None
                    else None
                )
                if slots == 0:
                    row["removes"] += len(inventory[topic["id"]][cid])
                    inventory[topic["id"]][cid] = []
                    continue
                cutoff = date - timedelta(days=channel_age_days(topic, cid))
                old = inventory[topic["id"]][cid]
                kept = [published for published in old if published >= cutoff]
                row["removes"] += len(old) - len(kept)
                since = max(
                    1,
                    (
                        date
                        - (
                            parse_time(topic["last_success"])
                            if topic["last_success"]
                            else date - timedelta(days=channel_age_days(topic, cid))
                        )
                    ).total_seconds()
                    / 86400,
                )
                elapsed = min(since, channel_age_days(topic, cid))
                key = (topic["id"], cid)
                expected = elapsed * rate + fractional.get(key, 0)
                incoming = math.floor(expected)
                fractional[key] = expected - incoming
                additions = incoming if slots is None else min(incoming, slots)
                row['raw_adds'] += additions
                if correction:
                    # Apply bias to uncertain arrivals, not to the 50-unit price
                    # of mutations or known expiry. Share fractional rounding
                    # across channels to avoid all quiet channels jumping together.
                    cap = elapsed * rate if slots is None else min(elapsed * rate, slots)
                    calibrated = cap * correction['additions'] + calibrated_fractional.get(topic['id'], 0)
                    additions = math.floor(calibrated)
                    calibrated_fractional[topic['id']] = calibrated - additions
                    if slots is not None:
                        additions = min(additions, slots)
                overflow = (
                    max(0, len(kept) + additions - slots) if slots is not None else 0
                )
                row["removes"] += overflow
                kept = sorted(kept, reverse=True)[: max(0, len(kept) - overflow)]
                kept.extend([date] * additions)
                inventory[topic["id"]][cid] = kept
                row["adds"] += additions
                # Minimum discovery scenario; rejected uploads can require more pages.
                candidates = max(1, math.ceil(rate * channel_age_days(topic, cid)))
                if slots is not None:
                    candidates = max(1, min(candidates, max(slots, 50)))
                topic_reads += max(1, math.ceil(candidates / 50)) + math.ceil(
                    candidates / 50
                )
            row['raw_reads'] += topic_reads
            row['reads'] += math.ceil(topic_reads * correction.get('reads', 1))
            topic["last_success"] = date.isoformat()
            entry = schedule_slots.get(str(topic["id"]))
            topic["next_due"] = (
                slot_due(date, topic["frequency"], zone, entry)
                if entry and entry["frequency"] == topic["frequency"]
                else next_due(date, topic["frequency"], zone)
            ).isoformat()
        if shared_reads and row["topics"]:
            row["reads"] += 1  # Live account identity check per scheduled run.
            row["raw_reads"] += 1
            if (
                not cache_hours
                or subscription_fetched is None
                or date >= subscription_fetched + timedelta(hours=cache_hours)
            ):
                row["reads"] += subscription_pages
                row["raw_reads"] += subscription_pages
                subscription_fetched = date
        row["units"] += row["reads"] + 50 * (row["adds"] + row["removes"])
        rows.append(row)
    demand_rows = [dict(row) for row in rows]
    if mode == "budget":
        rows = budget_carryover(state, rows, now)
    return dict(
        days=rows,
        demand_days=demand_rows,
        mode=mode,
        budget=state.meta("daily_budget", 9500),
        rate=rate,
        calibration=dict(enabled=calibrate, topics=len(calibration), factors=calibration),
        assumptions=(
            (f"Adjusted using completed updates for {len(calibration)} topics; other topics use the assumed rate. " if calibrate else "Unadjusted scenario using your assumed matching-upload rate. ")
            + (
                "Requested work is queued into later days; expired or superseded work may need less effort in practice. "
                if mode == "budget"
                else "Requested work assumes scheduled updates finish; it does not carry budget delays forward. "
            )
            + ("Every time topics assume one update run per day; additional runs use more quota. " if any(t["frequency"] == "every-time" for t in topics) else "")
            + "Reads are approximate. This does not know future uploads or Google’s actual remaining quota. Legacy entries without channel IDs are distributed evenly."
        ),
    )


def budget_carryover(state, demand_rows, now):
    """Queue estimated work against daily capacity, preserving 50-unit mutation granularity.

    This is an aggregate work-queue scenario, not a second sync implementation.
    It avoids claiming exact completed videos: delayed demand can expire, merge
    with newer desired contents, or need fresh discovery on the real run.
    Existing recorded usage and a confirmed server exhaustion constrain today.
    No forecast writes quota, schedules, success dates, or remote state.
    """
    day = now.astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()
    quota = state.quota(day)
    ceiling = state.meta("daily_budget", 9500)
    queue = deque()
    result = []
    for index, demand in enumerate(demand_rows):
        reads = demand["reads"]
        writes = max(0, (demand["units"] - reads) // 50)
        queue.append([reads, writes])
        recorded = quota["used"] if index == 0 else 0
        available = max(0, ceiling - recorded)
        if index == 0 and quota["exhausted"]:
            available = 0
        started_with = available
        # Reads cost one and mutations fifty; never pretend a partially funded
        # insertion happened just to draw a bar exactly up to the daily ceiling.
        for work in queue:
            read_count = min(work[0], available)
            work[0] -= read_count
            available -= read_count
            if work[0]:
                break
            write_count = min(work[1], available // 50)
            work[1] -= write_count
            available -= 50 * write_count
        queue = deque(work for work in queue if any(work))
        carried = sum(reads + 50 * writes for reads, writes in queue)
        spent = started_with - available
        result.append(
            dict(
                demand,
                units=recorded + spent,
                requested_units=demand["units"],
                additional_units=spent,
                recorded_units=recorded,
                carried_units=carried,
            )
        )
    return result
