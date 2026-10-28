"""Interactive front end that gathers inputs and calls the ordinary CLI.

There is no separate playlist implementation here: every submitted action goes
through the same command validation, state lock and quotas as scripted usage.
Escape cancels an unfinished form; it cannot undo a command already submitted.
"""

from .terminal import ask, MenuCancelled
from .channel_order import PRIORITIES
from .schedule import FREQUENCIES
from .state import State
from .api import QuotaStop
from .topics import load_subscriptions, suggest


def choose(prompt, options):
    """Return a zero-based selection, or None on blank; Escape unwinds the whole action."""
    for index, option in enumerate(options, 1):
        print(f'  {index}. {option}')
    value = ask(prompt + ' (blank to cancel): ').strip()
    if not value:
        return None
    if not value.isdigit() or not 1 <= int(value) <= len(options):
        raise ValueError('Choose one of the displayed numbers.')
    return int(value) - 1


def select_channels(path):
    """Search the local subscription export and resolve selected numbers to channel IDs."""
    channels = load_subscriptions(path)
    query = ask('Search your channels (name or description): ').strip().casefold()
    matches = [channel for channel in channels if query in
               (channel['title'] + ' ' + (channel.get('description') or '')).casefold()]
    for index, channel in enumerate(matches, 1):
        print(f"  {index}. {channel['title']} ({channel['channel_id']})")
    selected = ask('Channel numbers, separated by commas; “all” selects these matches (blank to cancel): ').strip()
    if not selected:
        return None
    if selected.casefold() == 'all':
        return [channel['channel_id'] for channel in matches]
    numbers = [int(value.strip()) for value in selected.split(',')]
    if any(number < 1 or number > len(matches) for number in numbers):
        raise ValueError('A channel number was outside the displayed list.')
    return sorted({matches[number - 1]['channel_id'] for number in numbers})


def run_menu(args, runner):
    """Collect one action’s inputs, then invoke the same CLI used by automation.

    Escape during input discards the unfinished action. It does not interrupt
    network operations or undo commands already completed. Credentials and state
    options are carried into every invocation, not silently reset to defaults.
    """
    base = ['--state', str(args.state), '--token', str(args.token), '--client-secrets', str(args.client_secrets),
            '--llm-config', str(args.llm_config)]
    export = args.state.parent / 'subscriptions.json'

    def run(*words):
        """Forward a menu action with the global paths from the launching command."""
        return runner(base + list(words))

    def topic_name():
        """Read available topics, close the connection, then ask which one to edit."""
        state = State(args.state)
        try:
            topics = state.topics()
        finally:
            state.close()
        if not topics:
            raise ValueError('Add a topic first.')
        index = choose('Topic', [topic['name'] for topic in topics])
        return topics[index]['name'] if index is not None else None

    while True:
        try:
            print('\nYouTube playlist builder\n'
                  '  1. Topics and update queue\n'
                  '  2. Add a topic\n'
                  '  3. Edit a topic\n'
                  '  4. Enable or disable a topic\n'
                  '  5. Remove a topic (keep its playlist)\n'
                  '  6. Suggest topics from subscriptions\n'
                  '  7. Find subscribed channels\n'
                  '  8. Preview a topic update\n'
                  '  9. Update one topic now\n'
                  ' 10. Auto-update due topics\n'
                  ' 11. Refresh subscriptions\n'
                  ' 12. Google sign-in\n'
                  ' 13. Reconcile a playlist with local records\n'
                  ' 14. API budget and scheduling timezone\n'
                  ' 15. Text or LLM mode (topic discovery and video matching)\n'
                  ' 16. Update all enabled topics (ignore frequency)\n'
                  ' 17. Minimum video length\n'
                  ' 18. Subscription channel cache (uploads always refresh)\n'
                  '  0. Exit\n'
                  'Press Escape at any prompt to return here.')
            action = ask('Choose: ').strip()
            if action == '0':
                return 0
            if action == '15':
                mode = choose('Matching mode', ['Text (no model calls)', 'LLM (uses .local/llm.json or --llm-config)'])
                if mode is not None:
                    run('settings', '--mode', ('text', 'llm')[mode])
            elif action == '1':
                run('status')
            elif action == '2':
                name = ask('Topic name (blank to cancel): ').strip()
                if not name:
                    continue
                from .cli import suggestions
                from types import SimpleNamespace
                state = State(args.state)
                try:
                    with state.locked():
                        candidates = suggestions(SimpleNamespace(subscriptions=export, llm_config=args.llm_config), state) if export.exists() else []
                finally:
                    state.close()
                candidate = next((entry for entry in candidates if entry['name'].casefold() == name.casefold()), None)
                selected = None
                if candidate:
                    print('Suggested channels: ' + ', '.join(channel['title'] for channel in candidate['channels']))
                    if ask('Use these channels? [y/N]: ').strip().lower() == 'y':
                        selected = [channel['channel_id'] for channel in candidate['channels']]
                if selected is None:
                    selected = select_channels(export)
                if not selected:
                    continue
                frequency = choose('Update frequency', list(FREQUENCIES))
                if frequency is None:
                    continue
                age = ask('Maximum video age in days [7]: ').strip() or '7'
                title = ask('Playlist name (blank for sub_<topic>): ').strip()
                words = ['topics', 'add', name, '--frequency', FREQUENCIES[frequency], '--max-age', age]
                if title:
                    words += ['--playlist-name', title]
                for channel_id in selected:
                    words += ['--channel', channel_id]
                run(*words)
            elif action == '3':
                name = topic_name()
                if name is None:
                    continue
                run('topics', 'show', name)
                field = choose('Change', ['Frequency', 'Maximum video age', 'Playlist name', 'Channels', 'Title filters', 'Topic description (LLM)', 'Add channels', 'Channel priorities'])
                words = ['topics', 'set', name]
                if field == 7:
                    state = State(args.state)
                    try:
                        topic = state.topic(name)
                    finally:
                        state.close()
                    titles = {channel['channel_id']: channel['title'] for channel in load_subscriptions(export)} if export.exists() else {}
                    channels = sorted(topic['channels'], key=lambda cid: titles.get(cid, cid).casefold())
                    if not channels:
                        raise ValueError('Add channels to this topic before setting priorities.')
                    index = choose('Channel', [f"{titles.get(cid, cid)} [{topic['channel_priorities'].get(cid, 'medium')}]"
                                               for cid in channels])
                    if index is None:
                        continue
                    priority = choose('Priority (weighted turns: high 3, medium 2, low 1)', list(PRIORITIES))
                    if priority is None:
                        continue
                    words += ['--channel-priority', f'{channels[index]}={PRIORITIES[priority]}']
                elif field == 5:
                    words += ['--description', ask('What belongs in this topic? ').strip()]
                elif field == 0:
                    frequency = choose('Frequency', list(FREQUENCIES))
                    if frequency is None:
                        continue
                    words += ['--frequency', FREQUENCIES[frequency]]
                elif field == 1:
                    words += ['--max-age', ask('Maximum age in days: ').strip()]
                elif field == 2:
                    title = ask('Playlist name (blank restores the default): ').strip()
                    words += ['--playlist-name', title] if title else ['--default-name']
                elif field in (3, 6):
                    selected = select_channels(export)
                    if not selected:
                        continue
                    for channel_id in selected:
                        words += ['--add-channel' if field == 6 else '--channel', channel_id]
                elif field == 4:
                    included = ask('Require any of these title phrases, separated by commas (blank for none): ').strip()
                    excluded = ask('Exclude these title phrases, separated by commas (blank for none): ').strip()
                    # Clear first and then supply the complete replacement in one edit.
                    words += ['--clear-filters']
                    for word in included.split(',') if included else []:
                        words += ['--include', word.strip()]
                    for word in excluded.split(',') if excluded else []:
                        words += ['--exclude', word.strip()]
                else:
                    continue
                run(*words)
            elif action in ('4', '5', '8', '9', '13'):
                name = topic_name()
                if name is None:
                    continue
                if action == '4':
                    selection = choose('Set topic', ['Enabled', 'Disabled'])
                    if selection is not None:
                        run('topics', 'enable' if selection == 0 else 'disable', name)
                elif action == '5':
                    if ask(f'Remove topic {name!r} and keep its YouTube playlist? [y/N]: ').strip().lower() == 'y':
                        run('topics', 'remove', name)
                elif action == '8':
                    run('update', name, '--preview')
                elif action == '9':
                    run('update', name)
                else:
                    run('reconcile', name)
            elif action == '6':
                run('suggest-topics', '--subscriptions', str(export))
            elif action == '7':
                run('channels', ask('Search: ').strip(), '--subscriptions', str(export))
            elif action == '18':
                run('settings')
                choice = choose('Subscription channel cache', ['Refresh on the next update', 'Set cache lifetime'])
                if choice == 0:
                    run('settings', '--refresh-subscriptions')
                elif choice == 1:
                    hours = ask('Cache hours (24 default; 0 always refreshes; maximum 720): ').strip()
                    if hours:
                        run('settings', '--subscription-cache-hours', hours)
            elif action == '17':
                run('settings')
                length = ask('Minimum seconds for new videos (0 turns off; 181 excludes up to 3 minutes; blank keeps it): ').strip()
                if length:
                    run('settings', '--minimum-video-length', length)
            elif action == '16':
                run('auto', '--all')
            elif action == '10':
                run('auto')
            elif action == '11':
                run('subscriptions', '--output', str(export))
            elif action == '12':
                access = choose('Access', ['Read subscriptions', 'Read and manage playlists'])
                if access is not None:
                    run('auth', *(['--write-access'] if access == 1 else []))
            elif action == '14':
                run('settings')
                budget = ask('New daily budget in units (blank keeps it): ').strip()
                zone = ask('Scheduling timezone, e.g. Europe/London (blank keeps it): ').strip()
                words = ['settings']
                if budget:
                    words += ['--daily-budget', budget]
                if zone:
                    words += ['--timezone', zone]
                if len(words) > 1:
                    run(*words)
            else:
                print('Choose a number from the menu.')
        except MenuCancelled:
            print('Cancelled. Back to the menu.')
            continue
        except (EOFError, KeyboardInterrupt):
            print('\nLeaving the menu.')
            return 0
        except (ValueError, OSError, QuotaStop) as error:
            print(str(error))
        except SystemExit as error:
            if error.code not in (0, 2):
                raise
