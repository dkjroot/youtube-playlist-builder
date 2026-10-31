"""Application entry point and shared command dispatcher.

Both shell commands and menu actions arrive at main(). It validates paths,
configures diagnostics, locks state, selects local versus remote work, and wires
OAuth, budgeted YouTube transport and optional classification into Sync. Exit
codes distinguish completion, error, and a resumable quota/budget pause.
"""

from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
import sys
import sqlite3
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google.auth.exceptions import GoogleAuthError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import httplib2
from requests.exceptions import RequestException

from .auth import authenticate
from .subscriptions import list_subscriptions
from .playlists import create_test_playlist
from .api import YouTube, ApiFailure, ConnectionFailure, QuotaStop
from .state import State
from .schedule import initialise_staggering, staggered_due, FREQUENCIES, utcnow, due_topics, next_due, parse_time
from .topics import load_subscriptions, suggest, validate, edit_topic, describe
from .diagnostics import configure as configure_logging, logger
from .limits import prepare_limits
from .rebalance import redistribute
from .sync import Sync
from .llm import Classifier, settings as llm_settings


def parser() -> argparse.ArgumentParser:
    """Define the public CLI contract shared by terminal commands and the menu."""
    result = argparse.ArgumentParser(description="Organise your YouTube subscriptions by subject.")
    result.add_argument("--client-secrets", type=Path, default=Path("client_secret.json"))
    result.add_argument("--token", type=Path, default=Path(".local/token.json"))
    result.add_argument("--state", type=Path, default=Path(".local/playlists.sqlite3"))
    result.add_argument("--llm-config", type=Path, default=Path(".local/llm.json"))
    commands = result.add_subparsers(dest="command")
    web = commands.add_parser('web', help='Open local browser management at 127.0.0.1.')
    web.add_argument('--port', type=positive, default=8765)
    web.add_argument('--no-browser', action='store_true')
    backup = commands.add_parser('backup', help='Save a consistent, restorable local data backup; no sign-in needed.')
    backup.add_argument('--output', type=Path, help='New ZIP filename; default: timestamped file in the state folder/backups/.')
    restore = commands.add_parser('restore', help='Verify a backup and restore into a new folder without overwriting live data.')
    restore.add_argument('archive', type=Path)
    restore.add_argument('--to', type=Path, required=True, help='New destination folder (must not already exist).')
    commands.add_parser('menu', help='Open the interactive menu (also the default in a terminal).')
    auth = commands.add_parser("auth", help="Sign in with Google.")
    auth.add_argument("--write-access", action="store_true", help="Also request permission to manage playlists.")
    test = commands.add_parser("create-test-playlist", help="Create a private playlist containing one video.")
    test.add_argument("--video-id", required=True, help="YouTube video ID to add.")
    subscriptions = commands.add_parser("subscriptions", help="List all your subscribed channels.")
    subscriptions.add_argument("--output", type=Path, help="Also save a JSON channel list.")
    for command in ('channels', 'suggest-topics'):
        local = commands.add_parser(command, help='Review the local subscription export.' if command == 'channels'
                                    else 'Suggest topics using the selected text or LLM mode.')
        local.add_argument('--subscriptions', type=Path, default=Path('.local/subscriptions.json'))
        if command == 'channels':
            local.add_argument('query', nargs='?', default='')
        else:
            local.add_argument('--details', action='store_true')
            local.add_argument('--json', action='store_true')
    topics = commands.add_parser('topics', help='Manage topic settings locally.')
    actions = topics.add_subparsers(dest='action', required=True)
    actions.add_parser('list')
    for action in ('show', 'remove', 'enable', 'disable'):
        actions.add_parser(action).add_argument('name')
    for action in ('add', 'set'):
        change = actions.add_parser(action)
        change.add_argument('name')
        change.add_argument('--max-videos', type=nonnegative, help='Overall playlist capacity; 0 restores unlimited. Redistributes slots evenly.')
        change.add_argument('--channel-max-age', action='append', default=[], type=channel_slots, metavar='CHANNEL_ID=DAYS', help='Override a channel age; 0 restores topic default.')
        change.add_argument('--channel-slots', action='append', default=[], type=channel_slots, metavar='CHANNEL_ID=COUNT', help='Override a channel allocation and recalculate the total.')
        change.add_argument('--description', help='Describe what belongs in this topic for the LLM.')
        change.add_argument('--frequency', choices=FREQUENCIES)
        change.add_argument('--max-age', type=positive, help='Maximum video age in days.')
        change.add_argument('--playlist-name', help='Override the default sub_<topic> name.')
        change.add_argument('--channel', action='append', help='Channel ID; repeat to select several. Replaces the selection.')
        change.add_argument('--channel-priority', action='append', type=channel_priority, default=[], metavar='CHANNEL_ID=LEVEL',
                            help='Set high, medium or low for a selected channel (default medium); repeat as needed.')
        change.add_argument('--include', action='append', help='Require any of these title substrings; repeat as needed.')
        change.add_argument('--exclude', action='append', help='Exclude titles matching any of these substrings.')
        if action == 'add':
            change.add_argument('--suggested', action='store_true', help='Use the matching suggestion from your export.')
            change.add_argument('--subscriptions', type=Path, default=Path('.local/subscriptions.json'))
            change.add_argument('--disabled', action='store_true')
        else:
            change.add_argument('--add-channel', action='append', default=[])
            change.add_argument('--remove-channel', action='append', default=[])
            change.add_argument('--default-name', action='store_true')
            change.add_argument('--clear-filters', action='store_true')
    for command in ('auto', 'update', 'reconcile'):
        operation = commands.add_parser(command, help={
            'auto': 'Update enabled, due topics in the saved rotation.',
            'update': 'Update one topic now, even if disabled or not due.',
            'reconcile': 'Read a managed playlist to repair local state after manual edits or interruptions.',
        }[command])
        if command == 'auto':
            operation.add_argument('--all', action='store_true', help='Update every enabled topic, ignoring due dates.')
        if command != 'auto':
            operation.add_argument('name')
        operation.add_argument('--budget', type=positive, help='Maximum API units for this run.')
        operation.add_argument('--daily-budget', type=positive, help='Override the saved daily unit budget for this run.')
        if command != 'reconcile':
            operation.add_argument('--refresh-subscriptions', action='store_true',
                                   help='Refresh subscribed channels now; uploads are always checked live.')
            operation.add_argument('--preview', action='store_true', help='Read and show proposed changes without writing to YouTube.')
    rebalance = commands.add_parser('redistribute', help='Spread enabled topic schedules using local forecast costs, with no API calls.')
    rebalance.add_argument('--calibrate', action='store_true', help='Learn per-topic forecast corrections from completed update runs.')
    rebalance.add_argument('--rate', type=float, default=1.0, help='Assumed matching videos per channel per day.')
    commands.add_parser('status', help='Show the queue, schedules, and local daily quota usage.')
    settings = commands.add_parser('settings', help='View or set the local daily API budget (default 9500 units).')
    settings.add_argument('--single-topic-channels', choices=('on', 'off'),
                          help='In the web organiser, selecting a new topic moves channels that have one topic (default off).')
    settings.add_argument('--subscription-cache-hours', type=nonnegative, metavar='HOURS',
                          help='Cache subscribed channel list for up to 720 hours; 0 always refreshes (default 24).')
    settings.add_argument('--refresh-subscriptions', action='store_true',
                          help='Discard the channel-list cache so the next update fetches it again.')
    settings.add_argument('--minimum-video-length', type=nonnegative, metavar='SECONDS',
                          help='Minimum duration for new additions; 0 disables the filter.')
    settings.add_argument('--mode', choices=('text', 'llm'), help='Select topic discovery and video matching together.')
    settings.add_argument('--daily-budget', type=positive)
    settings.add_argument('--timezone', help='Calendar scheduling timezone, such as Europe/London (default UTC).')
    return result


def channel_slots(value):
    """Parse a channel capacity override; membership is checked against the final selection."""
    channel_id, separator, count = value.partition('=')
    if not separator or not channel_id:
        raise argparse.ArgumentTypeError('Use CHANNEL_ID=COUNT.')
    return channel_id, nonnegative(count)


def channel_priority(value):
    """Parse a repeatable channel-priority edit before any network work or state changes."""
    channel_id, separator, level = value.partition('=')
    if not separator or level not in ('high', 'medium', 'low') or not channel_id:
        raise argparse.ArgumentTypeError('Use CHANNEL_ID=high, CHANNEL_ID=medium or CHANNEL_ID=low.')
    return channel_id, level


def nonnegative(value):
    """Parse duration settings; zero explicitly disables the minimum-length filter."""
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('Use a whole number of seconds, zero or greater.') from None
    if number < 0:
        raise argparse.ArgumentTypeError('Use a whole number of seconds, zero or greater.')
    return number


def positive(value):
    """Parse budgets and age limits, rejecting zero before any state or API work."""
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('Use a positive integer.') from None
    if number < 1:
        raise argparse.ArgumentTypeError('Use a positive integer.')
    return number


def suggestions(args, state):
    """Select text or LLM discovery from saved mode, closing the model transport afterwards."""
    channels = load_subscriptions(args.subscriptions)
    if state.meta('mode', 'text') == 'text':
        return suggest(channels)
    classifier = Classifier(state, llm_settings(args.llm_config))
    try:
        return classifier.suggest(channels)
    finally:
        classifier.close()


def local_command(args, state):
    """Handle settings, topic CRUD and export inspection under the caller’s state lock.

    These commands never modify YouTube; LLM suggestions may contact the model.
    Topic edits invalidate due times where the desired playlist contents change.
    """
    if args.command == 'redistribute':
        result = redistribute(state,rate=args.rate,calibrate=args.calibrate)
        print(result['message'])
        return 0
    if args.command in ('channels', 'suggest-topics'):
        channels = load_subscriptions(args.subscriptions)
        if args.command == 'channels':
            query = args.query.casefold()
            for channel in channels:
                if query in (channel['title'] + ' ' + (channel.get('description') or '')).casefold():
                    print(f"{channel['channel_id']}  {channel['title']}")
        else:
            found = suggestions(args, state)
            if args.json:
                print(json.dumps(found, indent=2, ensure_ascii=False))
            else:
                print('Suggestions from channel names/descriptions. Review the matches before adding a topic.')
                for suggestion in found:
                    matches = suggestion['channels']
                    print(f"\n{suggestion['name']} ({len(matches)} channels)")
                    for channel in matches if args.details else matches[:3]:
                        print(f"  {channel['channel_id']}  {channel['title']} — {channel['reason']}")
                if not found:
                    print('No matches found. Add a topic and select its channels manually.')
        return 0
    if args.command == 'settings':
        if args.single_topic_channels is not None:
            state.set_meta('single_topic_channels', args.single_topic_channels == 'on')
        print(f"One topic per channel in organiser: {'on' if state.meta('single_topic_channels', False) else 'off'}.")
        if args.subscription_cache_hours is not None:
            if args.subscription_cache_hours > 720:
                raise ValueError('Subscription cache lifetime must be between 0 and 720 hours.')
            state.set_meta('subscription_cache_hours', args.subscription_cache_hours)
        if args.refresh_subscriptions:
            state.set_meta('subscription_cache', None)
            print('Subscription channel cache cleared; the next update will refresh it.')
        print(f"Subscription channel cache: {state.meta('subscription_cache_hours', 24)} hours; upload feeds refresh every run.")
        if args.minimum_video_length is not None:
            state.set_meta('minimum_video_seconds', args.minimum_video_length)
        print(f"Minimum video length: {state.meta('minimum_video_seconds', 0)} seconds (0 means off).")
        if args.mode:
            if args.mode == 'llm':
                llm_settings(args.llm_config)
            if args.mode != state.meta('mode', 'text'):
                for topic in state.topics():
                    state.update(topic['id'], next_due=None)
            state.set_meta('mode', args.mode)
        print(f"Topic discovery and video matching: {state.meta('mode', 'text')} mode.")
        if args.timezone:
            try:
                ZoneInfo(args.timezone)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError('Unknown timezone. Use a name such as Europe/London or UTC.') from None
            state.set_meta('timezone', args.timezone)
            for topic in state.topics():
                if topic['last_success'] and topic['next_due']:
                    state.update(topic['id'], next_due=staggered_due(state, topic, parse_time(topic['last_success'])).isoformat())
        if args.daily_budget:
            state.set_meta('daily_budget', args.daily_budget)
        print(f"Local daily budget: {state.meta('daily_budget', 9500)} API units.")
        print(f"Calendar scheduling timezone: {state.meta('timezone', 'UTC')}.")
        return 0
    if args.command in ('status', 'topics'):
        if args.command == 'status' or args.action == 'list':
            topics = state.topics()
            for topic in topics:
                print(describe(topic))
            if not topics:
                print('No topics yet. Try suggest-topics, then topics add NAME --suggested.')
            if args.command == 'status':
                day = utcnow().astimezone(ZoneInfo('America/Los_Angeles')).date().isoformat()
                quota = state.quota(day)
                print(f"\nTracked quota for {day} (Pacific): {quota['used']}/{state.meta('daily_budget', 9500)} units"
                      + ('; YouTube quota exhausted.' if quota['exhausted'] else '.'))
                usage = state.api_usage(day)
                if usage:
                    print('Request breakdown (since this feature was installed; includes failed attempts):')
                    for row in usage:
                        print(f"  {row['resource']}.{row['method']}: {row['calls']} calls, {row['units']} estimated units")
                queue = due_topics(topics, state.meta('cursor', 0), utcnow())
                llm_day = datetime.now(timezone.utc).date().isoformat()
                print(f"LLM calls today (UTC): {state.meta('llm_calls:' + llm_day, 0)}.")
                print('Due queue: ' + (' → '.join(topic['name'] for topic in queue) or 'none'))
            return 0
        name = args.name.strip()
        if args.action == 'add':
            fields = dict(description=args.description or '', frequency=args.frequency or 'daily', max_age_days=args.max_age or 7,
                          enabled=not args.disabled, channels=args.channel or [],
                          channel_priorities=dict(args.channel_priority),
                          include_words=args.include or [], exclude_words=args.exclude or [], playlist_name=args.playlist_name)
            if args.suggested:
                matches = [item for item in suggestions(args, state) if item['name'].casefold() == name.casefold()]
                if not matches:
                    raise ValueError('No matching suggestion. Use suggest-topics to see the names, or supply --channel IDs.')
                fields['channels'] = sorted(set(fields['channels']) | {item['channel_id'] for item in matches[0]['channels']})
            if args.channel_max_age:
                fields['channel_max_age_days'] = {cid:days for cid,days in args.channel_max_age if days}
            if args.max_videos is not None:
                fields['max_videos'] = args.max_videos or None
            if args.channel_slots:
                fields['channel_limits'] = dict(args.channel_slots)
            prepare_limits({}, fields)
            validate(name, fields)
            added = state.add(name, **fields)
            initialise_staggering(state, utcnow())
            print(describe(added))
            return 0
        topic = state.topic(name)
        if args.action == 'show':
            print(describe(topic))
            print('Description: ' + topic['description'])
            print('Channels: ' + ', '.join(topic['channels']))
            print('Channel priorities (default medium; weighted high:medium:low = 3:2:1):')
            for channel_id in topic['channels']:
                print(f"  {channel_id}: {topic['channel_priorities'].get(channel_id, 'medium')}")
            print('Channel age overrides: ' + str(topic.get('channel_max_age_days', {})))
            print('Channel slots: ' + str(topic.get('channel_limits', {})))
            print('Include title phrases: ' + ', '.join(topic['include_words']))
            print('Exclude title phrases: ' + ', '.join(topic['exclude_words']))
        elif args.action == 'remove':
            state.remove(topic['id'])
            print(f"Removed topic {name}. Its YouTube playlist has been kept.")
        elif args.action in ('enable', 'disable'):
            state.update(topic['id'], enabled=args.action == 'enable')
            print(describe(state.by_id(topic['id'])))
        else:
            fields = {key: value for key, value in dict(description=args.description, frequency=args.frequency, max_age_days=args.max_age,
                      playlist_name=args.playlist_name, include_words=args.include, exclude_words=args.exclude).items() if value is not None}
            if args.default_name:
                fields['playlist_name'] = None
            if args.clear_filters:
                fields.update(include_words=args.include or [], exclude_words=args.exclude or [])
            if args.channel is not None or args.add_channel or args.remove_channel:
                fields['channels'] = sorted((set(args.channel if args.channel is not None else topic['channels'])
                                             | set(args.add_channel)) - set(args.remove_channel))
            if args.channel_priority:
                selected = fields.get('channels', topic['channels'])
                if set(dict(args.channel_priority)) - set(selected):
                    raise ValueError('Set priorities only for channels selected in this topic; add the channel first or in the same edit.')
                fields['channel_priorities'] = dict(topic['channel_priorities'], **dict(args.channel_priority))
            if args.max_videos is not None:
                fields['max_videos'] = args.max_videos or None
            if args.channel_slots:
                fields['channel_limits'] = dict(args.channel_slots)
            if args.channel_max_age:
                ages = dict(topic.get('channel_max_age_days', {}))
                for cid,days in args.channel_max_age:
                    if cid not in fields.get('channels',topic['channels']):
                        raise ValueError('Set age overrides only for selected channels.')
                    if days: ages[cid] = days
                    else: ages.pop(cid,None)
                fields['channel_max_age_days'] = ages
            edit_topic(state, topic, fields)
            print(describe(state.by_id(topic['id'])))
        return 0
    raise ValueError('Unknown local command.')


def main(argv=None) -> int:
    """Dispatch one command and return a stable shell exit code.

    The menu re-enters this function so both interfaces use identical validation,
    locks, budgets and recovery. Managed updates authenticate without opening a
    browser; the explicit auth/proof commands retain their interactive workflow.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if '--auto' in argv:
        argv[argv.index('--auto')] = 'auto'
    args = parser().parse_args(argv)
    try:
        configure_logging(args.state.parent / 'runtime.log')
        logger.info('Command=%s preview=%s all=%s', args.command or 'menu',
                    getattr(args, 'preview', False), getattr(args, 'all', False))
        if args.command == 'web':
            if args.port > 65535:
                raise ValueError('Choose a port between 1 and 65535.')
            if args.state.resolve() in {args.token.resolve(),args.client_secrets.resolve()}:
                raise ValueError('The topic database must not use a credential path.')
            from .web import serve
            return serve(args)
        if args.command == 'menu' or args.command is None:
            if args.command is None and not sys.stdin.isatty():
                parser().print_help()
                return 0
            from .menu import run_menu
            return run_menu(args, main)
        if args.state.resolve() in {args.token.resolve(), args.client_secrets.resolve()}:
            raise ValueError('The topic database must not use the credentials or token path.')
        if args.command == 'backup':
            from .backup import create_backup
            path, manifest = create_backup(args.state, args.output)
            counts = manifest['counts']
            print(f"Backup saved to {path.resolve()}.")
            print(f"{counts['topics']} topics, {counts['assigned_channels']} assigned channels, {counts['memberships']} memberships.")
            print('Includes local settings and playlist state; excludes sign-in credentials and model keys.')
            print('Copy this ZIP to another drive or your usual backed-up storage for protection against disk loss.')
            return 0
        if args.command == 'restore':
            from .backup import restore_backup
            path, manifest = restore_backup(args.archive, args.to)
            print(f"Backup verified and restored to {path.resolve()}.")
            print('Your current database has not been replaced. Stop the web server before switching to the restored database.')
            print(f'Restored database: {(path / "playlists.sqlite3").resolve()}')
            print('Restore does not undo YouTube changes. Reconcile managed playlists before resuming updates from an older backup.')
            return 0
        if args.command in ('topics', 'status', 'settings', 'channels', 'suggest-topics', 'auto', 'update', 'reconcile', 'redistribute'):
            state = State(args.state)
            try:
                with state.locked():
                    if not getattr(args, 'preview', False):
                        initialise_staggering(state, utcnow())
                    if args.command not in ('auto', 'update', 'reconcile'):
                        return local_command(args, state)
                    if args.command == 'auto' and not due_topics(state.topics(), state.meta('cursor', 0), utcnow(), all_topics=args.all):
                        print('No enabled topics are due. Nothing to update.')
                        return 0
                    if args.command != 'auto':
                        state.topic(args.name)  # Validate before attempting authentication.
                    preview = getattr(args, 'preview', False)
                    credentials = authenticate(args.client_secrets, args.token,
                        write_access=not preview and args.command != 'reconcile', interactive=False)
                    service = build('youtube', 'v3', credentials=credentials, cache_discovery=False)
                    daily = args.daily_budget or state.meta('daily_budget', 9500)
                    api = YouTube(service, state, daily_budget=daily, run_budget=args.budget or daily)
                    classifier = (Classifier(state, llm_settings(args.llm_config))
                                  if state.meta('mode', 'text') == 'llm' and args.command != 'reconcile' else None)
                    try:
                        return Sync(api, state, classifier=classifier,
                                    refresh_subscriptions=getattr(args, 'refresh_subscriptions', False)).run(topic_name=getattr(args, 'name', None),
                            preview=preview, reconcile_only=args.command == 'reconcile',
                            all_topics=getattr(args, 'all', False))
                    finally:
                        if classifier:
                            classifier.close()
            finally:
                state.close()
        if args.command == "subscriptions" and args.output:
            protected = {args.client_secrets.resolve(), args.token.resolve(), args.state.resolve()}
            if args.output.resolve() in protected:
                raise ValueError("Choose an output path other than your OAuth client, token, or topic database.")
        write_access = args.command == "create-test-playlist" or getattr(args, "write_access", False)
        credentials = (authenticate(args.client_secrets, args.token, write_access=True)
                       if write_access else authenticate(args.client_secrets, args.token))
        if args.command == "auth":
            print("Signed in with YouTube playlist management access." if write_access
                  else "Signed in with YouTube read access.")
            return 0
        youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
        if args.command == "create-test-playlist":
            receipt = args.token.parent / "test-playlist.json"
            result = create_test_playlist(youtube, args.video_id, receipt)
            print(f"Verified private playlist with one video: {result['video_title']}")
            print(f"Saved playlist and item IDs to {receipt}.")
            return 0
        channels = list_subscriptions(youtube)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps([channel.to_dict() for channel in channels], indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        for channel in channels:
            print(f"{channel.title}  https://www.youtube.com/channel/{channel.channel_id}")
        print(f"\n{len(channels)} subscribed channels.")
        if args.output:
            print(f"Saved to {args.output}.")
        return 0
    except HttpError as error:
        print(
            f"YouTube API request failed (HTTP {error.resp.status}). "
            "Check that YouTube Data API v3 is enabled, that you chose the right "
            "YouTube account, and that the project has quota remaining.",
            file=sys.stderr,
        )
    except QuotaStop as error:
        print(str(error), file=sys.stderr)
        return 2
    except (ApiFailure, ConnectionFailure, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
    except (ValueError, OSError, GoogleAuthError, RequestException, httplib2.HttpLib2Error) as error:
        # OAuth exceptions can contain response data. Keep token material out of logs.
        if isinstance(error, (GoogleAuthError, RequestException, httplib2.HttpLib2Error)):
            print("Google sign-in or connection failed. Check your connection and try again.", file=sys.stderr)
        else:
            print(str(error), file=sys.stderr)
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    return 1

