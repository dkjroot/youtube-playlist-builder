"""Weighted channel turns inside a topic's already-budgeted playlist turns.

Discovery and classification decide eligibility first. This module only decides
which eligible new video gets the next insertion. It is pure: callers persist
the returned rotation token only alongside a successful remote insertion, never
when planning, previewing, removing an item or receiving an uncertain reply.
"""

from collections import deque
import hashlib
import json

from .schedule import parse_time

PRIORITIES = ('high', 'medium', 'low')
WEIGHTS = {'high': 3, 'medium': 2, 'low': 1}


def addition_turns(topic, videos):
    """Yield (video, next_rotation) in a repeating 3:2:1 channel cycle.

    Each layer visits all channels that have that layer's weight, high priority
    first. Equal priorities rotate one video per channel; unused turns are
    skipped rather than reserving quota for an empty channel. Sorting within
    each channel favours fresh uploads. A signature invalidates the old index
    when the channel selection/priorities change, without storing a huge cycle
    with each video. An unchanged cycle resumes after the last saved insertion.
    """
    priorities = topic.get('channel_priorities', {})
    channels = sorted(set(topic['channels']), key=lambda cid: (-WEIGHTS[priorities.get(cid, 'medium')], cid))
    cycle = [cid for layer in range(3) for cid in channels if WEIGHTS[priorities.get(cid, 'medium')] > layer]
    if not cycle:
        return
    signature = hashlib.sha256(json.dumps(cycle).encode()).hexdigest()
    saved = topic.get('channel_rotation', {})
    cursor = saved.get('next', 0) if saved.get('signature') == signature else 0
    if type(cursor) is not int or not 0 <= cursor < len(cycle):
        cursor = 0
    queues = {}
    for video in sorted(videos, key=lambda row: (parse_time(row['published_at']), row['video_id']), reverse=True):
        queues.setdefault(video['channel_id'], deque()).append(video)
    remaining = sum(len(queues.get(cid, ())) for cid in channels)
    while remaining:
        cid = cycle[cursor]
        cursor = (cursor + 1) % len(cycle)
        queue = queues.get(cid)
        if queue:
            remaining -= 1
            yield queue.popleft(), {'signature': signature, 'next': cursor}
