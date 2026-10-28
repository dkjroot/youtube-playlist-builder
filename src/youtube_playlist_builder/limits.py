"""Local slot allocation; a finite topic is the sum of firm channel capacities.

No cross-cutting discovery pool is included. Equal division happens only when
an overall size is explicitly set. Editing individual slots recalculates that
size; unused slots never spill into another channel's allocation.
"""


def equal_slots(channels, total):
    """Split integer capacity deterministically, including zero slots when needed."""
    channels = sorted(set(channels))
    if not channels:
        return {}
    base, remainder = divmod(total, len(channels))
    return {cid: base + int(index < remainder) for index, cid in enumerate(channels)}


def prepare_limits(topic, fields):
    """Normalise a creation/edit before validation, without mutating stored state.

    Explicit total changes redistribute evenly. Channel edits preserve retained
    allocations, give a new channel an average-sized allocation, and adjust the
    total. Custom allocation edits then override that distribution and sum up.
    None means unlimited; a finite zero total is an intentionally empty allocation.
    """
    channels = fields.get("channels", topic.get("channels", []))
    maximum = fields.get("max_videos", topic.get("max_videos"))
    if maximum is not None and (type(maximum) is not int or maximum < 0):
        raise ValueError(
            "Maximum playlist size must be a whole number, zero or greater."
        )
    edits = fields.get("channel_limits")
    if "max_videos" in fields:
        limits = {} if maximum is None else equal_slots(channels, maximum)
    else:
        limits = dict(topic.get("channel_limits", {}))
        if maximum is not None and "channels" in fields:
            average = max(1, maximum // max(1, len(topic.get("channels", []))))
            limits = {cid: limits.get(cid, average) for cid in channels}
    if edits is not None:
        if maximum is None:
            raise ValueError("Set an overall maximum before assigning channel slots.")
        if not isinstance(edits, dict) or edits.keys() - set(channels):
            raise ValueError("Assign slots only to channels selected in this topic.")
        limits.update(edits)
    if maximum is not None:
        if any(type(value) is not int or value < 0 for value in limits.values()):
            raise ValueError("Channel slots must be whole numbers, zero or greater.")
        fields["channel_limits"] = limits
        fields["max_videos"] = sum(limits.values())
    elif "max_videos" in fields:
        fields["channel_limits"] = {}
    return fields


def channel_age_days(topic, channel_id):
    """Inherit the topic age unless this selected channel has an explicit override."""
    return topic.get("channel_max_age_days", {}).get(channel_id, topic["max_age_days"])
