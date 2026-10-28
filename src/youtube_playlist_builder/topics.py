"""Offline topic definitions and the optional text-mode suggestion rules.

The menu/CLI validate edits here before saving them in State. Suggestions inspect
the user's exported subscriptions against known subject rules; they never create
topics automatically. LLM discovery lives in llm.py, selected by the CLI's saved
mode. Content-changing edits make topics due for a fresh synchronisation.
"""

import json
from pathlib import Path
import re

from .schedule import staggered_due, FREQUENCIES, next_due, parse_time, playlist_title
from .channel_order import PRIORITIES
from .limits import prepare_limits

# These are suggestions, not a claim to understand every channel. The extra
# names catch channels whose titles don't describe their subject.
RULES = {
    'chess': (r'\bchess\b|gothamchess|powerplaychess|chessnetwork|chessdojo|chessfactor',
              ['Anna Cramling', 'Daniel Naroditsky', 'Eric Rosen', 'GMBenjaminFinegold', 'IM Alex Banzea', 'John Bartholomew', 'Magnus Carlsen', 'Take Take Take']),
    'mechanical engineering': (r'\b(machining|metalworking|mechanical|lathe|milling|blacksmith|welding)\b',
              ['This Old Tony', 'Inheritance Machining', 'Blondihacks', 'AvE', 'Jeremy Fielding', 'Fireball Tool', 'Chronova Engineering', 'Tom Stanton', 'Breaking Taps', 'Stuff Made Here', 'Allen Millyard']),
    'electronics': (r'\b(electronics|electronic circuits|microcontroller|arduino|soldering)\b',
              ['Ben Eater', 'ElectroBOOM', 'bigclivedotcom', 'Phil’s Lab', 'w2aew', 'Sam Zeloof', 'Afrotechmods', 'AddOhms']),
    'programming': (r'\b(programming|coding|software development|python|javascript)\b',
              ['Fireship', 'The Art of Code', 'CodeParade', 'Sebastian Lague', 'Freya Holmér', 'sentdex']),
    'ai and machine learning': (r'\b(artificial intelligence|machine learning|deep learning|large language models|llms)\b',
              ['AI Explained', 'AI Search', 'Andrej Karpathy', 'OpenAI', 'Two Minute Papers', 'Matthew Berman', 'Yannic Kilcher', 'TensorFlow', 'Kaggle']),
    'physics and space': (r'\b(physics|astronomy|astrophysics|cosmology|space exploration)\b',
              ['PBS Space Time', 'Astrum Extra', 'Fermilab', 'Cool Worlds', 'Sean Carroll', 'NASA', 'Scott Manley', 'Anton Petrov']),
    'science': (r'\b(science experiments|science communication|scientific discoveries)\b',
              ['Veritasium', 'SmarterEveryDay', 'Steve Mould', 'Applied Science', 'The Royal Institution', 'Vsauce', 'Be Smart', 'The Rest Is Science', 'AlphaPhoenix']),
    'chemistry': (r'\b(chemistry|chemical experiments)\b',
              ['NileRed', 'NileBlue', 'NileRed Extra', 'NurdRage', 'Explosions&Fire', 'Extractions&Ire', "Cody'sLab"]),
    'mathematics': (r'\b(mathematics|maths|calculus|linear algebra)\b',
              ['3Blue1Brown', 'Mathologer', 'Tibees', 'Welch Labs', 'Hannah Fry', 'MySecretMathTutor']),
    'aviation': (r'\b(aviation|flight training|pilot training|aircraft|airliner)\b',
              ['FlightChops', 'FlightInsight', 'A330 Driver', 'The Finer Points', 'Mentour Now!', 'Taking Off']),
    'flight simulation': (r'\b(flight sim|flight simulator|msfs|x-plane)\b',
              ['A2A Simulations', 'Flight Sim School', 'Aus Flight Simmer', 'twotonemurphy', 'P Gatcomb']),
    'drones and fpv': (r'\b(fpv|drones|quadcopters)\b',
              ['Joshua Bardwell', 'Joshua Bardwell Livestream Clips', 'Rotor Riot', 'Mr Steele', 'CPSdrone', 'Chris Rosser']),
    'piano': (r'\b(piano|pianist|pianists)\b',
              ['Pianote', 'PianoPig', 'Nahre Sol', 'Tiffany Poon', 'Heart of the Keys', 'MangoldProject']),
    'guitar': (r'\b(guitar|guitarist|guitars)\b',
              ['JustinGuitar', 'Paul Davids', 'That Pedal Show', 'Rick Beato', 'guitaro5000']),
    'synths and music production': (r'\b(synthesizer|synthesizers|synths|music production|sound design|eurorack)\b',
              ['loopop', 'HAINBACH', 'BoBeats', 'AudioPilz', 'Benn Jordan', 'Venus Theory', 'Red Means Recording', 'ANDREW HUANG']),
    'art and painting': (r'\b(painting|drawing|illustration|portrait artist)\b',
              ['Proko', 'James Gurney', 'Marco Bucci', 'Cesar Santos', 'Draw Mix Paint', 'Draftsmen']),
    'photography': (r'\b(photography|photographer|film cameras|cinematography)\b',
              ['Camera Conspiracies', 'Media Division', 'Pictorial Planet', 'Shoot Film Like a Boss']),
    'writing': (r'\b(creative writing|writing fiction|novelist|storytelling|screenwriting)\b',
              ['Brandon Sanderson', 'Abbie Emmons', 'Jed Herne', 'Just Write', 'Lessons from the Screenplay']),
    'history': (r'\b(history|historian|archaeology|ancient civilizations)\b',
              ['The Rest Is History', 'History Hit', 'World of Antiquity', 'Modern History TV', 'OverSimplified']),
    'climbing': (r'\b(climbing|bouldering|climbers)\b',
              ['Magnus Midtbø', 'Adam Ondra', "Hooper's Beta", 'Lattice Training']),
    'fitness and yoga': (r'\b(fitness|strength training|calisthenics|yoga|bodybuilding)\b',
              ['Jeff Nippard', 'Tom Merrick', 'Yoga With Adriene', 'ATHLEAN-X™']),
    'board games': (r'\b(board games|boardgames|tabletop games)\b',
              ['Shut Up & Sit Down', 'The Dice Tower', 'Watch It Played', 'Man vs Meeple']),
    'game development': (r'\b(game development|gamedev|game dev|godot|unreal engine)\b',
              ['Brackeys', 'Gamefromscratch', 'Mix and Jam', 'iHeartGameDev']),
    'retro computing': (r'\b(retro computing|retro computers|commodore|vintage computers)\b',
              ['The 8-Bit Guy', '8-Bit Show And Tell', "Adrian's Digital Basement", "Noel's Retro Lab", 'Nostalgia Nerd']),
    'coffee': (r'\b(coffee|espresso|barista)\b', ['James Hoffmann', 'Lance Hedrick', 'ARAMSE']),
    'sewing and tailoring': (r'\b(sewing|tailoring|dressmaking|costume making)\b',
              ['Bernadette Banner', 'Evelyn Wood', 'Cornelius Quiring', 'withwendy']),
    'woodworking': (r'\b(woodworking|carpentry|woodturning)\b', ['Matt Estlea', 'Marius Hornberger', 'Darbin Orvar']),
}


def load_subscriptions(path: Path):
    """Read and validate a local JSON export; never contact YouTube implicitly."""
    if not path.exists():
        raise ValueError(f'No subscription export at {path}. Run subscriptions --output {path} first.')
    channels = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(channels, list) or any(not isinstance(item, dict) or
            not isinstance(item.get('channel_id'), str) or not isinstance(item.get('title'), str) for item in channels):
        raise ValueError('The subscription file must be a JSON list of channels with channel_id and title.')
    return channels


def suggest(channels):
    """Suggest known subjects using channel names and descriptions, ignoring common sponsor footers."""
    result = []
    for name, (pattern, known) in RULES.items():
        names = {title.casefold() for title in known}
        matches = []
        for channel in channels:
            title = channel['title']
            # Channel footers often contain sponsor links, patron names, or a
            # "buy me a coffee" link unrelated to what the channel covers.
            intro = (channel.get('description') or '').split('\n\n', 1)[0][:600]
            intro = '\n'.join(line for line in intro.splitlines()
                              if not re.search(r'https?://|buy me a coffee|patreon|donat', line, re.IGNORECASE))
            text = title + '\n' + intro
            found = re.search(pattern, text, re.IGNORECASE)
            if title.casefold() in names or found:
                matches.append(dict(channel, reason='recognised channel name' if title.casefold() in names
                                    else f'matched “{found.group()}” in channel title/description'))
        if matches:
            result.append({'name': name, 'channels': matches})
    return sorted(result, key=lambda row: (-len(row['channels']), row['name']))


def validate(name, fields):
    """Reject invalid topic values before committing state or invoking a remote operation."""
    if not name.strip() or len(name) > 80 or any(ord(char) < 32 for char in name):
        raise ValueError('Use a topic name of 1–80 characters without control characters.')
    if len(fields.get('description', '')) > 2000:
        raise ValueError('Keep the topic description within 2000 characters.')
    if fields.get('frequency', 'daily') not in FREQUENCIES:
        raise ValueError('Unknown update frequency.')
    if fields.get('max_age_days', 7) < 1:
        raise ValueError('The maximum video age must be at least one day.')
    if fields.get('playlist_name') is not None and (not fields['playlist_name'].strip() or len(fields['playlist_name']) > 150):
        raise ValueError('Use a playlist name of 1–150 characters.')
    if any(not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', channel) for channel in fields.get('channels', [])):
        raise ValueError('Use YouTube channel IDs (UC followed by 22 characters). Find them with the channels command.')
    priorities = fields.get('channel_priorities', {})
    if not isinstance(priorities, dict) or any(
            not isinstance(cid, str) or not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', cid) or level not in PRIORITIES
            for cid, level in priorities.items()):
        raise ValueError('Channel priorities must map YouTube channel IDs to high, medium or low.')
    if 'channels' in fields and priorities.keys() - set(fields['channels']):
        raise ValueError('Set priorities only for channels selected in this topic; add the channel first or in the same edit.')
    ages = fields.get('channel_max_age_days', {})
    if not isinstance(ages, dict) or any(type(n) is not int or n < 1 for n in ages.values()):
        raise ValueError('Channel ages must be positive whole numbers of days.')
    if 'channels' in fields and ages.keys() - set(fields['channels']):
        raise ValueError('Set age overrides only for selected channels.')
    maximum = fields.get('max_videos')
    if maximum is not None and (type(maximum) is not int or maximum < 0):
        raise ValueError('Maximum playlist size must be a whole number, zero or greater.')
    limits = fields.get('channel_limits', {})
    if not isinstance(limits, dict) or any(type(n) is not int or n < 0 for n in limits.values()):
        raise ValueError('Channel slots must be whole numbers, zero or greater.')
    if 'channels' in fields and limits.keys() - set(fields['channels']):
        raise ValueError('Assign slots only to channels selected in this topic.')
    for key in ('include_words', 'exclude_words'):
        if any(not word.strip() for word in fields.get(key, [])):
            raise ValueError('Title filters cannot be empty.')


def edit_topic(state, topic, fields):
    """Save validated changes, refuse unresolved writes and recalculate or invalidate due times."""
    prepare_limits(topic, fields)
    if 'channel_max_age_days' in fields and fields['channel_max_age_days'].keys() - set(fields.get('channels', topic['channels'])):
        raise ValueError('Set age overrides only for selected channels.')
    if 'channels' in fields:
        fields['channel_max_age_days'] = {cid:age for cid,age in fields.get('channel_max_age_days',topic.get('channel_max_age_days',{})).items() if cid in fields['channels']}
        # Dropped channels must not retain a hidden priority if they are re-added.
        fields['channel_priorities'] = {cid: level for cid, level in
            fields.get('channel_priorities', topic['channel_priorities']).items() if cid in fields['channels']}
    if 'channel_priorities' in fields:
        validate(topic['name'], dict(topic, **fields))
        fields['channel_rotation'] = {}
    validate(topic['name'], fields)
    if state.pending(topic['id']):
        raise ValueError('Reconcile the interrupted update before editing this topic.')
    if 'frequency' in fields and topic['last_success'] and topic['next_due']:
        fields['next_due'] = staggered_due(state, dict(topic, **fields), parse_time(topic['last_success'])).isoformat()
    if any(key in fields for key in ('max_videos', 'channel_limits', 'channel_max_age_days', 'description', 'channels', 'channel_priorities', 'include_words', 'exclude_words', 'max_age_days', 'playlist_name')):
        fields['next_due'] = None
    state.update(topic['id'], **fields)


def describe(topic):
    """Format one topic for menu/CLI display without performing network requests."""
    lines = [f"{topic['name']} — {'enabled' if topic['enabled'] else 'disabled'}; {topic['frequency']}; "
             f"max age {topic['max_age_days']} days; {len(topic['channels'])} channels",
             f"  Playlist: {playlist_title(topic)}",
             f"  Last completed: {topic['last_success'] or 'never'}; next due: {topic['next_due'] or 'now'}"]
    lines.append(f"  Maximum videos: {topic.get('max_videos') if topic.get('max_videos') is not None else 'unlimited'}")
    if topic['playlist_id']:
        lines.append(f"  https://www.youtube.com/playlist?list={topic['playlist_id']}")
    if topic['last_error']:
        lines.append(f"  Last issue: {topic['last_error']}")
    return '\n'.join(lines)
