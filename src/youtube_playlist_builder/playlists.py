"""Standalone one-video playlist proof used during account setup.

This deliberately small path verifies write permission and privacy before the
user configures topics. Its exclusive JSON receipt prevents accidental repeat
creation. Ordinary updates use Sync/State instead, with richer reconciliation
and quota accounting; this proof never modifies those managed playlists.
"""

import json
import os
from pathlib import Path
import tempfile


def create_test_playlist(youtube, video_id: str, receipt: Path) -> dict:
    """Prove OAuth writes with one private playlist and a durable, exclusive receipt.

    This standalone setup check predates the managed topic engine and does not use
    its quota ledger. Refuse repeats, because a lost response could hide a completed
    creation; the receipt must be inspected rather than blindly retrying writes.
    """
    if receipt.exists():
        raise ValueError(f"A test run is already recorded at {receipt}. Inspect it before creating another playlist.")
    videos = youtube.videos().list(part="snippet,status", id=video_id).execute()
    if not videos.get("items"):
        raise ValueError("The selected video is unavailable. No playlist was created.")
    video = videos["items"][0]
    owners = youtube.channels().list(part="id", mine=True).execute().get("items", [])
    if len(owners) != 1:
        raise ValueError("Could not identify your YouTube channel. No playlist was created.")
    record = {
        "owner_channel_id": owners[0]["id"], "video_id": video_id,
        "video_title": video["snippet"]["title"],
        "video_published_at": video["snippet"]["publishedAt"],
        "phase": "creating_playlist",
    }
    receipt.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation blocks concurrent/repeated runs before either writes remotely.
    with receipt.open("x", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2)

    def save():
        """Atomically persist the returned IDs and next phase before another remote operation."""
        fd, temporary = tempfile.mkstemp(dir=receipt.parent, prefix=".playlist-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, receipt)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    playlist = youtube.playlists().insert(
        part="snippet,status",
        body={"snippet": {
            "title": "YouTube playlist builder — test",
            "description": "A one-video test of my subscription playlist builder.",
        }, "status": {"privacyStatus": "private"}},
    ).execute(num_retries=0)
    record.update(playlist_id=playlist["id"], phase="adding_video")
    save()
    print(f"Created private playlist: https://www.youtube.com/playlist?list={playlist['id']}", flush=True)
    item = youtube.playlistItems().insert(
        part="snippet",
        body={"snippet": {"playlistId": playlist["id"],
                          "resourceId": {"kind": "youtube#video", "videoId": video_id}}},
    ).execute(num_retries=0)
    record.update(playlist_item_id=item["id"], phase="verifying")
    save()
    actual = youtube.playlists().list(part="status", id=playlist["id"]).execute()
    items = youtube.playlistItems().list(
        part="snippet", playlistId=playlist["id"], maxResults=50
    ).execute()
    if (not actual.get("items")
            or actual["items"][0]["status"]["privacyStatus"] != "private"
            or len(items.get("items", [])) != 1
            or items.get("nextPageToken")
            or items["items"][0]["snippet"]["resourceId"]["videoId"] != video_id):
        raise ValueError(f"The playlist was created, but verification did not match. Inspect {receipt} before retrying.")
    record["phase"] = "complete"
    save()
    return record
