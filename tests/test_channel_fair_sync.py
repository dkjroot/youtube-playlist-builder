"""Exercise nested channel/topic fairness through real sync state and lost replies."""
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from youtube_playlist_builder.api import YouTube
from youtube_playlist_builder.state import State
from youtube_playlist_builder.sync import Sync
from test_sync import FakeYouTube, NOW
from test_channel_order import A, B, C


class ChannelServer(FakeYouTube):
    """Three busy channels, with globally newer A uploads that must not swamp B/C."""
    def __init__(self):
        super().__init__()
        self.channel_ids = [A, B, C]
        self.video_data = {f'{cid[-1]}{i}': {'id':f'{cid[-1]}{i}',
            'snippet':{'title':'Video', 'channelId':cid,
                       'publishedAt':(NOW - timedelta(minutes=i + offset * 60)).isoformat()},
            'status':{'privacyStatus':'public'}}
            for offset,cid in enumerate(self.channel_ids) for i in range(8)}

    def handle(self,resource,method,p):
        if resource == 'channels' and not p.get('mine'):
            return {'items':[{'id':cid,'contentDetails':{'relatedPlaylists':{'uploads':cid}}} for cid in p['id'].split(',')]}
        if resource == 'subscriptions':
            return {'items':[{'snippet':{'title':cid,'resourceId':{'channelId':cid}}} for cid in self.channel_ids]}
        if resource == 'playlistItems' and method == 'list' and p['playlistId'] in self.channel_ids:
            return {'items':[{'contentDetails':{'videoId':vid,'videoPublishedAt':row['snippet']['publishedAt']}}
                              for vid,row in self.video_data.items() if row['snippet']['channelId'] == p['playlistId']]}
        return super().handle(resource,method,p)


class ChannelFairSyncTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)/'state.db'
        self.state = State(self.path)
        self.addCleanup(lambda:self.state.close())
        self.topic = self.state.add('news',channels=[A,B,C])
        self.server = ChannelServer()

    def sync(self,budget=9500):
        return Sync(YouTube(self.server,self.state,now=lambda:NOW,run_budget=budget),self.state,
                    now=lambda:NOW,report=lambda _:None)

    def inserted(self):
        return [p['body']['snippet']['resourceId']['videoId'] for r,m,p in self.server.calls if r=='playlistItems' and m=='insert']

    def test_partial_run_restart_continues_channel_turn_without_duplicates(self):
        self.assertEqual(self.sync(157).run(),2)  # 7 reads + creation + two videos.
        self.assertEqual(self.inserted(),['a0','b0'])
        rotation = self.state.topic('news')['channel_rotation']
        self.assertTrue(rotation)
        self.state.close()
        self.state = State(self.path)
        self.server.calls.clear()
        self.assertEqual(self.sync(55).run(),2)  # 5 fresh reads + next channel's insertion.
        self.assertEqual(self.inserted(),['c0'])
        self.assertEqual(len(self.state.items(self.topic['id'])),3)
        self.assertNotEqual(rotation,self.state.topic('news')['channel_rotation'])
        self.assertIsNone(self.state.topic('news')['next_due'])

    def test_preview_does_not_consume_channel_turns(self):
        before = self.state.topic('news')['channel_rotation']
        self.assertEqual(self.sync().run(preview=True),0)
        self.assertEqual(self.state.topic('news')['channel_rotation'],before)
        self.assertEqual(self.inserted(),[])

    def test_lost_successful_reply_restores_rotation_from_pending_intent(self):
        self.server.failure = ('playlistItems','insert',True,TimeoutError())
        self.assertEqual(self.sync().run(),1)
        self.assertEqual(self.state.topic('news')['channel_rotation'],{})
        intent = self.state.pending(self.topic['id'])
        self.assertIn('channel_rotation',intent['payload'])
        self.assertEqual(self.sync().run(topic_name='news',reconcile_only=True),0)
        self.assertEqual(self.state.topic('news')['channel_rotation'],intent['payload']['channel_rotation'])
        self.server.calls.clear()
        self.assertEqual(self.sync(55).run(topic_name='news'),2)
        self.assertEqual(self.inserted(),['b0'])
        self.assertEqual(len(self.state.items(self.topic['id'])),2)

    def test_lost_unapplied_reply_does_not_grant_a_channel_turn(self):
        self.server.failure = ('playlistItems','insert',False,TimeoutError())
        self.assertEqual(self.sync().run(),1)
        self.assertEqual(self.sync().run(topic_name='news',reconcile_only=True),0)
        self.assertEqual(self.state.topic('news')['channel_rotation'],{})
        self.server.calls.clear()
        self.assertEqual(self.sync(55).run(topic_name='news'),2)
        self.assertEqual(self.inserted(),['a0'])

    def test_topic_turns_and_channel_turns_compose(self):
        self.state.add('other',channels=[A,B,C])
        self.assertEqual(self.sync(557).run(),2)
        for topic in self.state.topics():
            ids = {item['video_id'] for item in self.state.items(topic['id'])}
            self.assertTrue({'a0','b0','c0'} <= ids)
            self.assertIsNone(topic['next_due'])

    def test_priority_changes_order_without_rewriting_existing_items(self):
        self.state.update(self.topic['id'],channel_priorities={C:'high',A:'low'})
        self.assertEqual(self.sync().run(),0)
        self.assertEqual(self.inserted()[:6],['c0','b0','a0','c1','b1','c2'])
        topic = self.state.topic('news')
        playlist_items = self.server.items[topic['playlist_id']]
        # Choice of next admission is weighted; the displayed playlist stays newest-first.
        dates = [self.server.video_data[item['snippet']['resourceId']['videoId']]['snippet']['publishedAt'] for item in playlist_items]
        self.assertEqual(dates,sorted(dates,reverse=True))
        self.server.calls.clear()
        self.state.update(topic['id'],channel_priorities={A:'high',C:'low'},channel_rotation={})
        self.assertEqual(self.sync().run(topic_name='news'),0)
        self.assertFalse(any(method!='list' for _,method,_ in self.server.calls))
