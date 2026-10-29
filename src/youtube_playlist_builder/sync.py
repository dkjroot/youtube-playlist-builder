"""Incremental playlist synchronisation, with quota-aware discovery and recovery.

Sync snapshots the enabled queue, checks account ownership, and shares writes
between topics in bounded rounds. Discovery batches metadata across channels and shares reads
within the run. The local item ledger supplies deletion IDs; write intents and
returned IDs make partial updates recoverable without duplicating remote work.
Do not remove content after a partial discovery or uncertain model response.
"""

from collections import deque
from datetime import timedelta
from itertools import zip_longest, islice

from .limits import channel_age_days
from .videos import duration_seconds
from .channel_order import addition_turns
from .terminal import progress
from .api import ApiFailure, ConnectionFailure, QuotaStop
from .schedule import staggered_due, initialise_staggering, due_topics, parse_time, playlist_title, utcnow


def batches(values, size=50):
    """Split IDs at YouTube’s 50-ID read limit; HTTP batching would not reduce per-call quota."""
    for start in range(0, len(values), size):
        yield values[start:start + size]


class Sync:
    """Reconcile desired topic contents against durable local playlist-item records.

    Upload discovery and availability refresh each run. Subscription membership
    and stable uploads-playlist addresses have bounded persistent caches. Additions
    and cleanup share turns after complete discovery and valid model decisions.
    Each write is recorded before yielding to another topic.
    """
    def __init__(self, api, state, *, now=utcnow, report=print, classifier=None, refresh_subscriptions=False):
        """Start a run with independent in-memory feed/metadata caches and optional classifier."""
        self.api, self.state, self.now, self.report = api, state, now, report
        self.classifier = classifier
        self.subscribed = None
        self.owner = None
        self.refresh_subscriptions = refresh_subscriptions
        self.uploads = {}
        self.cached_upload_ids = set()
        # Feed-page and video-detail caches expire with this run; cached addresses
        # never stand in for a fresh feed read, so newly published news stays visible.
        self.upload_pages = {}
        self.video_details = {}

    def identify(self):
        """Bind/check database ownership before trusting saved playlist IDs for remote writes."""
        channels = self.api.call('channels', part='id', mine=True).get('items', [])
        if len(channels) != 1:
            raise ValueError('Could not identify exactly one signed-in YouTube channel.')
        owner = channels[0]['id']
        previous = self.state.meta('owner_channel_id')
        if previous and previous != owner:
            raise ValueError('This database belongs to a different YouTube account. Use a separate --state file for this account.')
        if not previous:
            self.state.set_meta('owner_channel_id', owner)
        self.owner = owner
        return owner

    def subscriptions(self):
        """Reuse a bounded account-scoped channel list; upload feeds always refresh per run.

        YouTube has no subscription delta endpoint. Only complete successful lists
        replace the snapshot; a failed refresh never falls back to expired data.
        identify() must verify the account before a persistent snapshot is trusted.
        """
        if self.subscribed is not None:
            return self.subscribed
        hours = self.state.meta('subscription_cache_hours', 24)
        cached = self.state.meta('subscription_cache')
        if self.owner and hours and not self.refresh_subscriptions and isinstance(cached, dict):
            try:
                age = self.now() - parse_time(cached['fetched_at'])
                if (cached['owner'] == self.owner and timedelta(0) <= age < timedelta(hours=hours)
                        and isinstance(cached['channels'], list)):
                    channel_ids = {channel['channel_id'] for channel in cached['channels']}
                    if all(isinstance(channel, str) for channel in channel_ids):
                        self.subscribed = channel_ids
                        progress(f'Using cached subscription list ({len(channel_ids)} channels); checking uploads live…')
                        return self.subscribed
            except (KeyError, TypeError, ValueError, AttributeError):
                pass  # An old/malformed cache is disposable; fetch a complete replacement.
        entries = self.api.all_items('subscriptions', part='snippet', mine=True, maxResults=50)
        channels = [dict(channel_id=item['snippet']['resourceId']['channelId'],
                         title=item['snippet']['title'], description=item['snippet'].get('description', ''))
                    for item in entries]
        self.subscribed = {channel['channel_id'] for channel in channels}
        self.state.set_meta('subscription_cache', dict(owner=self.owner, fetched_at=self.now().isoformat(), channels=channels))
        return self.subscribed

    def details(self, video_ids):
        """Fetch uncached video IDs in full batches and retain available/unavailable results.

        Live/upcoming and private videos are not eligible. Missing IDs are cached
        only after a successful response; request failures never become negative results.
        """
        requested = set(video_ids)
        for group in batches(sorted(requested - self.video_details.keys())):
            response = self.api.call('videos', part='snippet,status,contentDetails', id=','.join(group))
            # Remember unavailable IDs too; overlapping topics needn't ask twice.
            self.video_details.update({video_id: None for video_id in group})
            for item in response.get('items', []):
                snippet = item['snippet']
                if (item.get('status', {}).get('privacyStatus') == 'private'
                        or snippet.get('liveBroadcastContent') in ('upcoming', 'live')):
                    continue
                self.video_details[item['id']] = dict(video_id=item['id'], title=snippet['title'],
                                          published_at=snippet['publishedAt'], channel_id=snippet['channelId'],
                                          description=snippet.get('description', ''),
                                          duration_seconds=duration_seconds(item.get('contentDetails', {}).get('duration')))
        known = {key: self.video_details[key] for key in requested if self.video_details.get(key)}
        self.state.remember_channel_videos(known.values())
        return known

    def resolve_uploads(self, channel_ids, *, refresh=False):
        """Resolve stable upload-playlist addresses without caching their video contents.

        The API supplies these identifiers; don't derive them from channel IDs.
        A seven-day cache avoids repeated address lookups while every command
        still reads the actual feeds. Only a complete lookup updates the cache.
        """
        records = self.state.meta('upload_playlist_ids', {})
        if not isinstance(records, dict):
            records = {}
        if not refresh:
            for channel_id in channel_ids:
                if channel_id in self.uploads:
                    continue
                record = records.get(channel_id)
                if not isinstance(record, dict):
                    continue
                try:
                    age = self.now() - parse_time(record['fetched_at'])
                    if (timedelta(0) <= age < timedelta(days=7)
                            and isinstance(record['playlist_id'], str) and record['playlist_id']):
                        self.uploads[channel_id] = record['playlist_id']
                        self.cached_upload_ids.add(channel_id)
                except (KeyError, TypeError, ValueError, AttributeError):
                    continue
        missing = sorted(set(channel_ids) if refresh else set(channel_ids) - self.uploads.keys())
        for group in batches(missing):
            entries = self.api.call('channels', part='contentDetails', id=','.join(group)).get('items', [])
            resolved = {item['id']: item['contentDetails']['relatedPlaylists']['uploads'] for item in entries}
            if set(group) - resolved.keys():
                raise ValueError('Some selected channels could not be read. No content-based removals were made.')
            for channel_id in group:
                self.uploads[channel_id] = resolved[channel_id]
                self.cached_upload_ids.discard(channel_id)
                records[channel_id] = dict(playlist_id=resolved[channel_id], fetched_at=self.now().isoformat())
            self.state.set_meta('upload_playlist_ids', records)

    def upload_ids(self, channel_id, cutoff):
        """Reuse upload pages, extending the snapshot only for wider topic age windows."""
        pages = self.upload_pages.setdefault(channel_id, [])
        index = 0
        while True:
            if index == len(pages):
                parameters = dict(part='contentDetails', playlistId=self.uploads[channel_id], maxResults=50)
                if pages:
                    parameters['pageToken'] = pages[-1]['nextPageToken']
                # Only complete responses enter the cache; failures never imply an empty feed.
                try:
                    page = self.api.call('playlistItems', **parameters)
                except ApiFailure as error:
                    if error.status != 404 or channel_id not in self.cached_upload_ids:
                        raise
                    previous = self.uploads[channel_id]
                    self.resolve_uploads([channel_id], refresh=True)
                    if self.uploads[channel_id] == previous:
                        raise  # Same address: another feed request would just waste quota.
                    pages.clear()
                    # IDs yielded earlier belong to the old snapshot; force the
                    # caller to rebuild its set from the replacement, not merge both.
                    raise ValueError('A channel upload-playlist address changed. Retry this topic to use its refreshed address.') from None
                pages.append(page)
            page = pages[index]
            entries = page.get('items', [])
            dates = [item['contentDetails'].get('videoPublishedAt') for item in entries]
            for item, date in zip(entries, dates):
                # Unknown dates must still be resolved by videos.list.
                if not date or parse_time(date) >= cutoff:
                    yield item['contentDetails']['videoId']
            # Check the whole page: one out-of-order old upload isn't a stopping point.
            if (dates and all(date and parse_time(date) < cutoff for date in dates)) or not page.get('nextPageToken'):
                break
            index += 1

    def recent_videos(self, topic, now):
        """Gather eligible uploads across subscribed selected channels before batching metadata.

        Reuse earlier upload pages for overlapping topics, extending pagination
        when a later topic has a wider age window. Apply age and title filters only
        after complete discovery, so a partial API failure cannot empty a playlist.
        """
        cutoff = now - timedelta(days=topic['max_age_days'])
        selected = sorted(set(topic['channels']) & self.subscriptions())
        self.resolve_uploads(selected)
        ids = set()
        for index, channel_id in enumerate(selected, 1):
            progress(f"{topic['name']}: reading channel {index}/{len(selected)}…")
            ids.update(self.upload_ids(channel_id, now - timedelta(days=channel_age_days(topic, channel_id))))
        # Fill batches across channels: 40 quiet channels can share one metadata read,
        # rather than paying for a mostly empty request for each individual channel.
        videos = self.details(ids)
        include = [word.casefold() for word in topic['include_words']]
        exclude = [word.casefold() for word in topic['exclude_words']]
        return {key: video for key, video in videos.items()
                if now - timedelta(days=channel_age_days(topic, video['channel_id'])) <= parse_time(video['published_at']) <= now
                and video['channel_id'] in selected
                and (not include or any(word in video['title'].casefold() for word in include))
                and not any(word in video['title'].casefold() for word in exclude)}

    def admit(self, topic, desired):
        """Apply duration/model eligibility before a video can occupy a channel slot."""
        minimum = self.state.meta('minimum_video_seconds', 0)
        retained = {}
        if minimum:
            existing = {item['video_id'] for item in self.state.items(topic['id'])}
            excluded = {key: video for key, video in desired.items()
                        if video['duration_seconds'] is None or video['duration_seconds'] < minimum}
            retained = {key: video for key, video in excluded.items() if key in existing}
            desired = {key: video for key, video in desired.items() if key not in excluded}
            self.report(f'Minimum length {minimum}s: skipping {len(excluded) - len(retained)} new videos '
                        f'(too short or duration unavailable); keeping {len(retained)} existing entries.')
        if self.classifier:
            desired = self.classifier.match(topic, desired)
        return {**desired, **retained}

    def limited_videos(self, topic, now):
        """Scan each channel lazily until its hard allocation has enough matches.

        A six-month age window remains cheap for a quiet channel. Busy channels
        stop after filling their slots instead of classifying their whole history.
        If filters reject many uploads, scanning may still need the full age window.
        No partial selection is returned after a read/model failure.
        """
        cutoff = now - timedelta(days=topic['max_age_days'])
        if not any(topic['channel_limits'].values()):
            return {}
        selected = sorted(set(topic['channels']) & self.subscriptions())
        selected = [cid for cid in selected if topic['channel_limits'].get(cid, 0) > 0]
        self.resolve_uploads(selected)
        desired = {}
        for index, cid in enumerate(selected, 1):
            cutoff = now - timedelta(days=channel_age_days(topic, cid))
            limit = topic['channel_limits'][cid]
            progress(f"{topic['name']}: channel {index}/{len(selected)}, filling up to {limit} slots…")
            iterator = iter(self.upload_ids(cid, cutoff))
            matches = {}
            while len(matches) < limit:
                ids = list(islice(iterator, 50))
                if not ids:
                    break
                videos = self.details(ids)
                include = [word.casefold() for word in topic['include_words']]
                exclude = [word.casefold() for word in topic['exclude_words']]
                eligible = sorted((video for video in videos.values()
                    if video['channel_id'] == cid and cutoff <= parse_time(video['published_at']) <= now
                    and (not include or any(word in video['title'].casefold() for word in include))
                    and not any(word in video['title'].casefold() for word in exclude)),
                    key=lambda row: (parse_time(row['published_at']), row['video_id']), reverse=True)
                # Keep model batches bounded and stop once enough matches are known.
                for group in batches(eligible, 20):
                    accepted = self.admit(topic, {video['video_id']: video for video in group})
                    for video in group:
                        if video['video_id'] in accepted:
                            matches[video['video_id']] = video
                            if len(matches) == limit:
                                break
                    if len(matches) == limit:
                        break
            desired.update(matches)
        return desired

    def write(self, topic, kind, payload, resource, method, **parameters):
        """Persist intent, dispatch once, and retain uncertain outcomes for reconciliation.

        A definite client/quota rejection clears intent; a transport failure or
        ambiguous server response may have applied the write and must not be retried.
        """
        self.api.check(50)
        self.state.begin_write(topic['id'], kind, payload)
        try:
            return self.api.call(resource, method, **parameters)
        except QuotaStop:
            with self.state.db:
                self.state.clear_pending(topic['id'])
            raise
        except ApiFailure as error:
            if error.status < 500 and error.status != 408:
                # A definite rejection hasn't applied the operation. Unknown
                # transport/server outcomes deliberately keep the recovery record.
                with self.state.db:
                    self.state.clear_pending(topic['id'])
            raise

    def reconcile(self, topic):
        """Recover uncertain operations or manual edits from a complete owned-playlist snapshot.

        Unknown creations are found by their unique marker. Never replace local
        records until all pages/details succeed, or a failed read could lose item IDs.
        """
        pending = self.state.pending(topic['id'])
        marker = f"youtube-playlist-builder topic:{topic['marker']}"
        playlist_id = topic['playlist_id']
        if not playlist_id:
            if not pending:
                return topic
            playlists = self.api.all_items('playlists', part='snippet,status', mine=True, maxResults=50)
            matches = [item for item in playlists if marker in item['snippet'].get('description', '')]
            if len(matches) != 1:
                raise ValueError('An interrupted playlist creation has no unique remote match yet. Run reconcile again later; no duplicate will be created.')
            remote = matches[0]
            playlist_id = remote['id']
        else:
            results = self.api.call('playlists', part='snippet,status', id=playlist_id).get('items', [])
            if not results:
                raise ValueError('The managed playlist is missing or inaccessible. Restore it or remove and re-add the topic to create a new one.')
            remote = results[0]
        if remote['snippet']['channelId'] != self.state.meta('owner_channel_id'):
            raise ValueError('The managed playlist is not owned by the signed-in YouTube channel.')
        entries = self.api.all_items('playlistItems', part='snippet,contentDetails', playlistId=playlist_id, maxResults=50)
        # Playlist items already carry the original video publication date. For
        # older/missing API fields, keep a known date or resolve only unknown IDs;
        # don't read every video's description/status merely to rebuild item IDs.
        saved = {item['video_id']: item for item in self.state.items(topic['id'])}
        unresolved = [item['snippet']['resourceId']['videoId'] for item in entries
                      if not item.get('contentDetails', {}).get('videoPublishedAt')
                      and not saved.get(item['snippet']['resourceId']['videoId'], {}).get('published_at')]
        videos = self.details(unresolved)
        # Only replace local contents after every page and metadata request succeeds.
        with self.state.db:
            self.state.db.execute('DELETE FROM items WHERE topic_id=?', (topic['id'],))
            for item in entries:
                video_id = item['snippet']['resourceId']['videoId']
                video = videos.get(video_id, {})
                self.state.put_item(topic['id'], dict(item_id=item['id'], video_id=video_id,
                    title=item['snippet'].get('title', video.get('title', saved.get(video_id, {}).get('title', ''))),
                    channel_id=(item['snippet'].get('videoOwnerChannelId') or saved.get(video_id,{}).get('channel_id') or video.get('channel_id')),
                    published_at=(item.get('contentDetails', {}).get('videoPublishedAt')
                                  or saved.get(video_id, {}).get('published_at') or video.get('published_at'))))
            self.state._update(topic['id'], dict(playlist_id=playlist_id,
                remote_title=remote['snippet']['title'] if remote['status']['privacyStatus'] == 'private' else None,
                reconciled_at=self.now().isoformat()))
            if (pending and pending['kind'] == 'insert'
                    and 'channel_rotation' in pending['payload']
                    and any(item['snippet']['resourceId']['videoId'] == pending['payload']['video_id'] for item in entries)):
                # A lost reply must grant the completed channel its turn exactly
                # once. If the insertion never landed, leave its cursor unchanged.
                self.state._update(topic['id'], {'channel_rotation': pending['payload']['channel_rotation']})
            self.state.clear_pending(topic['id'])
        return self.state.by_id(topic['id'])

    def remove_item(self, topic, item):
        """Delete by saved item ID and atomically update state; an already-missing item is success."""
        try:
            self.write(topic, 'delete', item, 'playlistItems', 'delete', id=item['item_id'])
        except ApiFailure as error:
            if error.status != 404 or error.reason != 'playlistItemNotFound':
                raise
        with self.state.db:
            self.state.db.execute('DELETE FROM items WHERE topic_id=? AND item_id=?', (topic['id'], item['item_id']))
            self.state.clear_pending(topic['id'])

    def update_topic(self, topic, *, preview=False):
        """Drain one topic's work for explicit updates and read-only previews."""
        for _ in self.topic_steps(topic, preview=preview):
            pass

    def topic_steps(self, topic, *, preview=False):
        """Discover, classify and yield after each durable mutation in one topic’s plan.

        Preview reads/caches but never writes playlists or advances success dates.
        Persist each successful mutation immediately so a later budget pause keeps
        useful work. Yields True on the final mutation; only complete updates earn
        a new scheduled date. The generator is reused between automatic rounds.
        """
        if not topic['channels']:
            raise ValueError('No channels are selected. Use topics set NAME --channel CHANNEL_ID or edit the topic in the menu.')
        now = self.now()
        if self.state.pending(topic['id']) or (topic['playlist_id'] and
                (not topic['reconciled_at'] or parse_time(topic['reconciled_at']) <= now - timedelta(days=7))):
            topic = self.reconcile(topic)
        if topic.get('max_videos') is None:
            desired = self.admit(topic, self.recent_videos(topic, now))
        else:
            desired = self.limited_videos(topic, now)
        current = self.state.items(topic['id'])
        if not preview:
            # Upgrade legacy item attribution using metadata already fetched;
            # future forecasts can use real channel ages/slots without extra calls.
            with self.state.db:
                for item in current:
                    video = desired.get(item['video_id'])
                    if video and not item.get('channel_id'):
                        self.state.db.execute('UPDATE items SET channel_id=? WHERE topic_id=? AND item_id=?',
                                              (video['channel_id'],topic['id'],item['item_id']))
        seen, remove = set(), []
        for item in current:
            if item['video_id'] not in desired or item['video_id'] in seen:
                remove.append(item)
            else:
                seen.add(item['video_id'])
        add = [video for key, video in desired.items() if key not in seen]
        rename = bool(topic['playlist_id'] and topic['remote_title'] != playlist_title(topic))
        cost = 50 * (len(remove) + len(add) + int(not topic['playlist_id']) + int(rename))
        self.report(f"{topic['name']}: {len(desired)} recent videos; add {len(add)}, "
                    f"remove {len(remove)}; "
                    f"{'estimated ' if preview else 'remaining '}write cost {cost} units.")
        if preview:
            for video in sorted(desired.values(), key=lambda item: item['published_at'], reverse=True):
                self.report(f"  {video['published_at'][:10]}  {video['title']}  https://youtu.be/{video['video_id']}")
            return
        remaining = cost // 50
        completed = False

        def record_step():
            """Mark completion before yielding the final write, even at a budget boundary."""
            nonlocal remaining, completed
            remaining -= 1
            if remaining == 0:
                self.finish_topic(topic)
                completed = True
            return completed

        title = playlist_title(topic)
        snippet = {'title': title, 'description': f"Recent videos from my subscriptions.\nyoutube-playlist-builder topic:{topic['marker']}"}
        if not topic['playlist_id']:
            result = self.write(topic, 'create', {}, 'playlists', 'insert', part='snippet,status',
                                body={'snippet': snippet, 'status': {'privacyStatus': 'private'}})
            with self.state.db:
                self.state._update(topic['id'], dict(playlist_id=result['id'], remote_title=title, reconciled_at=now.isoformat()))
                self.state.clear_pending(topic['id'])
            topic = self.state.by_id(topic['id'])
            yield record_step()
        elif rename:
            self.write(topic, 'rename', {}, 'playlists', 'update', part='snippet,status',
                       body={'id': topic['playlist_id'], 'snippet': snippet, 'status': {'privacyStatus': 'private'}})
            with self.state.db:
                self.state._update(topic['id'], dict(remote_title=title))
                self.state.clear_pending(topic['id'])
            yield record_step()
        # Channel fairness chooses additions; topic fairness limits how many steps
        # are drained per turn. Alternating cleanup keeps either backlog from
        # blocking the other, and removals never consume a channel insertion slot.
        if topic.get('max_videos') is not None:
            for item in remove:
                progress(f"Freeing a slot in {topic['name']}…")
                self.remove_item(topic, item)
                yield record_step()
            remove = []
        turns = addition_turns(topic, add)
        for turn, item in zip_longest(turns, remove):
            if turn is not None:
                video, rotation = turn
                # A long-running round must not insert a video that aged out while waiting.
                if parse_time(video['published_at']) >= self.now() - timedelta(days=channel_age_days(topic, video['channel_id'])):
                    progress(f"Adding a video to {topic['name']}…")
                    # Insert older backlog behind newer saved entries without moving them.
                    position = sum(1 for saved in self.state.items(topic['id'])
                                   if saved['published_at'] and parse_time(saved['published_at']) > parse_time(video['published_at']))
                    payload = dict(video, channel_rotation=rotation)
                    result = self.write(topic, 'insert', payload, 'playlistItems', 'insert', part='snippet',
                        body={'snippet': {'playlistId': topic['playlist_id'], 'position': position,
                            'resourceId': {'kind': 'youtube#video', 'videoId': video['video_id']}}})
                    with self.state.db:
                        self.state.put_item(topic['id'], dict(item_id=result['id'], **video))
                        self.state._update(topic['id'], {'channel_rotation': rotation})
                        self.state.clear_pending(topic['id'])
                    yield record_step()
                else:
                    record_step()
            if item is not None:
                progress(f"Removing a video from {topic['name']}…")
                self.remove_item(topic, item)
                yield record_step()
        if not completed:
            self.finish_topic(topic)

    def finish_topic(self, topic):
        """Record full completion once; partial turns deliberately remain due."""
        finished = self.now()
        self.state.update(topic['id'], last_success=finished.isoformat(),
                          next_due=staggered_due(self.state, topic, finished).isoformat(), last_error=None)
        self.report(f"Updated {topic['name']}: https://www.youtube.com/playlist?list={topic['playlist_id']}")

    def run(self, *, topic_name=None, preview=False, reconcile_only=False, all_topics=False):
        """Visit a fixed eligible queue in adaptive rounds, persisting the cursor each turn.

        Manual updates may target disabled topics; auto/all exclude them. Budget
        exhaustion pauses the run, while recoverable per-topic errors allow later
        topics a turn. No queue means no authentication-related API reads here.
        """
        if not preview and not reconcile_only:
            initialise_staggering(self.state, self.now())
        if topic_name:
            topics = [self.state.topic(topic_name)]
        else:
            topics = due_topics(self.state.topics(), self.state.meta('cursor', 0), self.now(), all_topics=all_topics)
        if not topics:
            self.report('No enabled topics are due. Nothing to update.')
            return 0
        try:
            self.identify()
        except QuotaStop as error:
            self.report(str(error))
            return 2
        failed = False
        # A generator holds the completed discovery/model plan between rounds.
        # Its successful writes are durable, but the plan is never trusted after restart.
        queue = deque((topic['id'], None) for topic in topics)
        automatic = not topic_name and not preview and not reconcile_only
        while queue:
            topic_id, steps = queue.popleft()
            topic = self.state.by_id(topic_id)
            allowance = max(1, min(5, self.api.remaining() // (50 * (len(queue) + 1)))) if automatic else None
            progress(f"{topic['name']}: up to {allowance} changes this turn; {len(queue)} other playlists waiting…"
                     if automatic else f"Updating {topic['name']}…")
            if not preview and not reconcile_only:
                # Advance before discovery/writes too: even an interrupted slow topic
                # must give the following topic a chance when the user resumes.
                with self.state.db:
                    self.state._update(topic_id, {'last_attempt': self.now().isoformat(), 'next_due': None})
                    if not topic_name:
                        self.state.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('cursor', str(topic_id)))
            try:
                if reconcile_only:
                    self.reconcile(topic)
                    self.report(f"Reconciled {topic['name']} with YouTube.")
                    continue
                if steps is None:
                    steps = self.topic_steps(topic, preview=preview)
                if not automatic:
                    for _ in steps:
                        pass
                    continue
                completed = False
                for _ in range(allowance):
                    if next(steps):
                        completed = True
                        steps.close()
                        break
                if not completed:
                    queue.append((topic_id, steps))
            except StopIteration:
                pass
            except QuotaStop as error:
                if not preview:
                    self.state.update(topic_id, last_error=str(error))
                self.report(f"Paused at {topic['name']}: {error}")
                return 2
            except (ApiFailure, ConnectionFailure, ValueError) as error:
                failed = True
                if not preview:
                    self.state.update(topic_id, last_error=str(error))
                self.report(f"Could not update {topic['name']}: {error}")
                if isinstance(error, ConnectionFailure):
                    return 1
        self.report(f"API units used this run: {self.api.spent}.")
        return 1 if failed else 0
