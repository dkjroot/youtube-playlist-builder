"""Duration parsing used before text/LLM playlist admission.

Sync reads contentDetails in its existing video metadata request and uses this
parser for the minimum-length setting. Unknown durations remain unknown, allowing
the admission filter to skip them conservatively without pretending they are Shorts.
"""
import re


def duration_seconds(value):
    """Parse YouTube’s day/hour/minute/second durations; return None rather than guess on missing/invalid input."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r'P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?', value)
    if not match or not any(part is not None for part in match.groups()):
        return None
    return sum(float(part or 0) * factor for part, factor in zip(match.groups(), (86400, 3600, 60, 1)))
