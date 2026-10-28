"""Explicit subscription export for discovery and interactive channel selection.

The CLI uses this module to write a complete, sorted local export. Sync separately
refreshes the current subscription set once per update run so changes in the
account are respected even if the human-facing export has not been refreshed.
"""

from .terminal import waiting

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Subscription:
    """The minimal channel metadata used by the export and both topic-discovery modes."""
    channel_id: str
    title: str
    description: str

    def to_dict(self) -> dict[str, str]:
        """Produce the stable JSON export record consumed by topic discovery and the menu."""
        return asdict(self)


def list_subscriptions(youtube) -> list[Subscription]:
    """Read every subscription page, deduplicate by ID and sort for human review.

    This explicit export is separate from Sync’s once-per-run subscription check;
    an interrupted export raises instead of replacing the file with a partial list.
    """
    subscriptions = {}
    page_token = None
    while True:
        parameters = dict(part="snippet", mine=True, maxResults=50, order="alphabetical")
        if page_token:
            parameters["pageToken"] = page_token
        with waiting(f'Reading subscriptions ({len(subscriptions)} channels so far)…'):
            response = youtube.subscriptions().list(**parameters).execute()
        for item in response.get("items", []):
            snippet = item["snippet"]
            channel_id = snippet["resourceId"]["channelId"]
            subscriptions[channel_id] = Subscription(
                channel_id, snippet["title"], snippet.get("description", "")
            )
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return sorted(subscriptions.values(), key=lambda channel: channel.title.casefold())

