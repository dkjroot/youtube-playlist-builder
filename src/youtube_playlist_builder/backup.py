"""Offline, consistent state snapshots and non-destructive restoration.

SQLite's backup API includes committed WAL data even while the organiser is open.
Archives contain application data only, never adjacent OAuth or model credentials.
Restoration validates before creating a new destination and never replaces live state.
"""

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import zipfile


DATABASE = 'playlists.sqlite3'
MANIFEST = 'manifest.json'
SUBSCRIPTIONS = 'subscriptions.json'
MAX_FILE_BYTES = 1024 * 1024 * 1024


def read_database(path):
    """Open an existing database read-only without migrations or accidental creation."""
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)


def inspect_database(path):
    """Check SQLite integrity, application tables and references; return useful counts."""
    with closing(read_database(path)) as db:
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('The backup database failed its SQLite integrity check.')
        required = {
            'topics': {'id', 'name', 'channels', 'marker'},
            'meta': {'key', 'value'},
            'items': {'topic_id', 'item_id', 'video_id'},
            'pending': {'topic_id', 'kind', 'payload'},
            'quota': {'day', 'used'},
        }
        for table, columns in required.items():
            found = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
            if not columns <= found:
                raise ValueError('This is not a supported playlist-builder database.')
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('The backup database has broken topic references.')
        channels = [json.loads(row[0]) for row in db.execute('SELECT channels FROM topics')]
        if any(not isinstance(group, list) or any(not isinstance(cid, str) for cid in group) for group in channels):
            raise ValueError('The backup database has invalid channel assignments.')
        return {
            'topics': len(channels),
            'assigned_channels': len({cid for group in channels for cid in group}),
            'memberships': sum(map(len, channels)),
            'pending_writes': db.execute('SELECT COUNT(*) FROM pending').fetchone()[0],
        }


def digest(path):
    """Hash file contents in bounded chunks for archive verification."""
    value = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def create_backup(state_path, output=None):
    """Publish a private ZIP snapshot without overwriting any existing file.

    Snapshot the whole database, preserving settings, owned playlist IDs, pending
    recovery and caches. Copy only the sibling subscription export if present.
    The backup API, rather than a file copy, safely handles active SQLite writers.
    """
    state_path = Path(state_path)
    timestamp = datetime.now(timezone.utc)
    output = Path(output) if output is not None else state_path.parent / 'backups' / (
        'youtube-playlists-' + timestamp.strftime('%Y%m%dT%H%M%S.%fZ') + '.zip'
    )
    if not state_path.is_file():
        raise ValueError(f'No database at {state_path}. Choose the existing --state file.')
    if output.exists() or output.is_symlink():
        raise ValueError(f'Backup destination already exists: {output}. Choose a new filename.')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='yt-backup-') as temporary:
        root = Path(temporary)
        database = root / DATABASE
        with closing(read_database(state_path)) as source, closing(sqlite3.connect(database)) as target:
            source.backup(target)
        counts = inspect_database(database)
        files = [DATABASE]
        export = state_path.parent / SUBSCRIPTIONS
        if export.exists():
            # Read one opened file so an atomic export replacement cannot mix versions.
            data = export.read_bytes()
            _validate_subscriptions(data)
            (root / SUBSCRIPTIONS).write_bytes(data)
            files.append(SUBSCRIPTIONS)
        if any((root / name).stat().st_size > MAX_FILE_BYTES for name in files):
            raise ValueError('Backup files exceed the supported 1 GiB size per file.')
        manifest = {
            'format': 'youtube-playlist-builder-backup', 'version': 1,
            'created_at': timestamp.isoformat(), 'counts': counts,
            'files': {name: {'sha256': digest(root / name), 'bytes': (root / name).stat().st_size} for name in files},
        }
        fd, temporary_name = tempfile.mkstemp(prefix='.yt-backup-', dir=output.parent)
        try:
            with os.fdopen(fd, 'w+b') as handle:
                with zipfile.ZipFile(handle, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                    for name in files:
                        archive.write(root / name, name)
                    archive.writestr(MANIFEST, json.dumps(manifest, indent=2) + '\n')
                handle.flush()
                os.fsync(handle.fileno())
            # Atomic publication with no overwrite, even if another process won a race.
            os.link(temporary_name, output)
        finally:
            Path(temporary_name).unlink(missing_ok=True)
    return output, manifest


def _validate_subscriptions(data):
    """Reject a malformed channel export before promising a usable restoration."""
    channels = json.loads(data)
    if not isinstance(channels, list) or any(
        not isinstance(item, dict) or not isinstance(item.get('channel_id'), str)
        or not isinstance(item.get('title'), str) for item in channels
    ):
        raise ValueError('The backup subscription export is not a channel list.')


def restore_backup(archive_path, destination):
    """Verify an archive, then restore to a new private folder; never replace live data.

    Allowlisted filenames are streamed explicitly, never extractall(), and checked
    against sizes/checksums before SQLite validation. Failed validation leaves no
    destination. A live web server must be stopped before switching to restored state.
    """
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError(f'Restore destination already exists: {destination}. Choose a new folder.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix='.yt-restore-', dir=destination.parent) as temporary:
            root = Path(temporary)
            with zipfile.ZipFile(archive_path) as archive:
                entries = archive.infolist()
                names = [entry.filename for entry in entries]
                if any(entry.flag_bits & 1 for entry in entries):
                    raise ValueError('Encrypted ZIP archives are not supported.')
                if len(names) != len(set(names)) or set(names) not in (
                    {MANIFEST, DATABASE}, {MANIFEST, DATABASE, SUBSCRIPTIONS}
                ):
                    raise ValueError('Unexpected or missing files in the backup archive.')
                if archive.getinfo(MANIFEST).file_size > 65536:
                    raise ValueError('Backup manifest is too large.')
                manifest = json.loads(archive.read(MANIFEST))
                if not isinstance(manifest, dict) or manifest.get('format') != 'youtube-playlist-builder-backup' or manifest.get('version') != 1:
                    raise ValueError('Unsupported backup format or version.')
                files = manifest.get('files')
                if not isinstance(files, dict) or set(files) != set(names) - {MANIFEST}:
                    raise ValueError('Backup manifest does not match the archive contents.')
                for name, expected in files.items():
                    info = archive.getinfo(name)
                    if not isinstance(expected, dict) or info.file_size > MAX_FILE_BYTES or expected.get('bytes') != info.file_size:
                        raise ValueError('Backup file size is invalid or exceeds 1 GiB.')
                    with archive.open(name) as source, (root / name).open('xb') as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
                    (root / name).chmod(0o600)
                    if digest(root / name) != expected.get('sha256'):
                        raise ValueError(f'Backup checksum failed for {name}.')
            counts = inspect_database(root / DATABASE)
            if counts != manifest.get('counts'):
                raise ValueError('Backup counts do not match the database.')
            if (root / SUBSCRIPTIONS).exists():
                _validate_subscriptions((root / SUBSCRIPTIONS).read_bytes())
            (root / MANIFEST).write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
            (root / MANIFEST).chmod(0o600)
            # mkdir is exclusive: a race cannot replace someone else's directory.
            destination.mkdir(mode=0o700)
            try:
                for name in names:
                    os.replace(root / name, destination / name)
            except BaseException:
                shutil.rmtree(destination)
                raise
        return destination, manifest
    except (zipfile.BadZipFile, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError('The backup archive is damaged or is not a supported backup.') from error
