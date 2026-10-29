"""Channel-first organisation with local assignments and quota-free public feeds."""

import re
import unicodedata
from .state import State
from .topics import edit_topic


def channel_detail(args, channel_id):
    """Combine observed uploads and managed entries, explicitly allowing sparse history."""
    if not isinstance(channel_id, str) or not re.fullmatch(r"UC[\w-]{22}", channel_id):
        raise ValueError("Choose a valid channel.")
    state = State(args.state)
    try:
        videos = {}
        for row in state.db.execute(
            "SELECT video_id,title,published_at,channel_id FROM items WHERE channel_id=?",
            (channel_id,),
        ):
            videos[row["video_id"]] = dict(row, description="")
        for row in state.db.execute(
            "SELECT * FROM channel_videos WHERE channel_id=?", (channel_id,)
        ):
            videos[row["video_id"]] = dict(row)
        recent = sorted(
            videos.values(),
            key=lambda v: (v["published_at"] or "", v["video_id"]),
            reverse=True,
        )[:5]
        return {
            "videos": recent,
            "feed_fetched_at": state.meta("channel_feed_fetched:" + channel_id),
        }
    finally:
        state.close()


def assign_channel(args, body):
    """Change just one membership against fresh state, preserving other channel edits.

    Standard topic validation also maintains firm capacities and refuses uncertain
    pending writes. Optional single-topic mode removes all other memberships,
    including existing overlaps. All changes share a database transaction.
    A chosen non-empty voice phrase is learned in the same transaction, keyed to
    topic identity. Empty recognitions never teach. Disabled topics remain useful
    as organisation-only categories.
    """
    cid, name, assigned = (
        body.get("channel_id"),
        body.get("topic"),
        body.get("assigned"),
    )
    if not isinstance(cid, str) or not re.fullmatch(r"UC[\w-]{22}", cid):
        raise ValueError("Choose a valid channel.")
    if not isinstance(name, str) or type(assigned) is not bool:
        raise ValueError("Choose a topic and whether to include the channel.")
    phrase = body.get("voice_phrase", "")
    if not isinstance(phrase, str) or len(phrase) > 120 or (phrase and not assigned):
        raise ValueError("Use a voice phrase of at most 120 characters with an assignment.")
    phrase = unicodedata.normalize("NFKD", phrase).lower()
    phrase = "".join(c for c in phrase if not unicodedata.combining(c))
    phrase = " ".join("".join(c if c.isalnum() else " " for c in phrase).split())
    state = State(args.state)
    try:
        with state.locked():
            topic = state.topic(name)
            channels = set(topic["channels"])
            if assigned:
                channels.add(cid)
            else:
                channels.discard(cid)
            previous = [t for t in state.topics() if cid in t["channels"]]
            move_from = (
                [t for t in previous if t["id"] != topic["id"]]
                if assigned and state.meta("single_topic_channels", False)
                else []
            )
            changed = (cid in topic["channels"]) != assigned or bool(move_from)
            if not changed and not phrase:
                return
            # Both sides commit together. Pending writes or validation failures
            # in either topic leave the original membership/allocations intact.
            with state.db:
                if changed:
                    edit_topic(state, topic, {"channels": sorted(channels)}, commit=False)
                for source in move_from:
                    edit_topic(
                        state,
                        source,
                        {"channels": [c for c in source["channels"] if c != cid]},
                        commit=False,
                    )
                if phrase:
                    state.db.execute(
                        "INSERT INTO voice_matches(phrase,topic_id,uses) VALUES (?,?,1) "
                        "ON CONFLICT(phrase,topic_id) DO UPDATE SET uses=uses+1",
                        (phrase, topic["id"]),
                    )
    finally:
        state.close()


def fetch_channel_videos(args, channel_id, *, refresh=False):
    """Fetch five public uploads without credentials or Data API quota, caching for 24 hours.

    The fixed YouTube Atom endpoint is intentionally separate from playlist sync:
    feeds may be incomplete, contain Shorts/live entries, or become unavailable.
    No API fallback or retry loop spends quota unexpectedly. Bound response size
    and time, reject redirects/DTD, and persist only validated video metadata.
    """
    import urllib.request
    import urllib.error
    import xml.etree.ElementTree as ET
    from .schedule import utcnow, parse_time
    from datetime import timedelta

    if not isinstance(channel_id, str) or not re.fullmatch(
        r"UC[A-Za-z0-9_-]{22}", channel_id
    ):
        raise ValueError("Choose a valid channel.")

    if type(refresh) is not bool:
        raise ValueError("Refresh must be true or false.")
    cached = channel_detail(args, channel_id)
    if not refresh and cached["feed_fetched_at"]:
        age = utcnow() - parse_time(cached["feed_fetched_at"])
        if timedelta(0) <= age < timedelta(hours=24):
            return cached

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        """Do not follow public-feed redirects to unexpected destinations."""

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            """Surface redirects as a feed error rather than following another host."""
            return None

    url = "https://www.youtube.com/feeds/videos.xml?channel_id=" + channel_id
    try:
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(url, timeout=15) as response:
            data = response.read(2_000_001)
        if (
            len(data) > 2_000_000
            or b"<!DOCTYPE" in data.upper()
            or b"<!ENTITY" in data.upper()
        ):
            raise ValueError("Unexpected public-feed response.")
        root = ET.fromstring(data)
        atom, yt, media = (
            "{http://www.w3.org/2005/Atom}",
            "{http://www.youtube.com/xml/schemas/2015}",
            "{http://search.yahoo.com/mrss/}",
        )
        if root.tag != atom + "feed":
            raise ValueError("YouTube did not return a video feed.")
        videos = []
        for entry in root.findall(atom + "entry"):
            vid = entry.findtext(yt + "videoId", "")
            cid = entry.findtext(yt + "channelId", "")
            date = entry.findtext(atom + "published", "")
            if (
                cid != channel_id
                or not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid)
                or not date
            ):
                continue
            videos.append(
                dict(
                    video_id=vid,
                    channel_id=cid,
                    title=entry.findtext(atom + "title", ""),
                    published_at=date,
                    description=entry.findtext(
                        media + "group/" + media + "description", ""
                    ),
                )
            )
        videos.sort(key=lambda v: (v["published_at"], v["video_id"]), reverse=True)
    except (urllib.error.URLError, TimeoutError, OSError, ET.ParseError) as error:
        raise ValueError(
            "Could not read YouTube’s public feed. Try again later or open the channel on YouTube. No API quota was used."
        ) from error
    state = State(args.state)
    try:
        with state.locked():
            state.remember_channel_videos(videos[:5])
            state.set_meta("channel_feed_fetched:" + channel_id, utcnow().isoformat())
    finally:
        state.close()
    return channel_detail(args, channel_id)
