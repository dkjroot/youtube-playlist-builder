import unittest
from unittest.mock import MagicMock, call

from youtube_playlist_builder.subscriptions import list_subscriptions


def channel(channel_id, title):
    return {"snippet": {"resourceId": {"channelId": channel_id}, "title": title}}


class SubscriptionsTests(unittest.TestCase):
    def test_all_pages_are_read_and_channels_deduplicated(self):
        youtube = MagicMock()
        youtube.subscriptions.return_value.list.return_value.execute.side_effect = [
            {"items": [channel("UC2", "Zebra"), channel("UC1", "alpha")], "nextPageToken": "page2"},
            {"items": [channel("UC1", "alpha"), channel("UC3", "Engineering")]},
        ]
        result = list_subscriptions(youtube)
        self.assertEqual([item.channel_id for item in result], ["UC1", "UC3", "UC2"])
        self.assertEqual(result[0].to_dict(), {"channel_id": "UC1", "title": "alpha", "description": ""})
        self.assertEqual(youtube.subscriptions.return_value.list.call_args_list, [
            call(part="snippet", mine=True, maxResults=50, order="alphabetical"),
            call(part="snippet", mine=True, maxResults=50, order="alphabetical", pageToken="page2"),
        ])

    def test_empty_account(self):
        youtube = MagicMock()
        youtube.subscriptions.return_value.list.return_value.execute.return_value = {}
        self.assertEqual(list_subscriptions(youtube), [])

    def test_failure_on_later_page_does_not_return_partial_results(self):
        youtube = MagicMock()
        youtube.subscriptions.return_value.list.return_value.execute.side_effect = [
            {"items": [channel("UC1", "Chess")], "nextPageToken": "page2"},
            RuntimeError("quota exhausted"),
        ]
        with self.assertRaisesRegex(RuntimeError, "quota exhausted"):
            list_subscriptions(youtube)

