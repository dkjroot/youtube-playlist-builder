"""Backups preserve personal data, include WAL commits and restore without overwrites."""

from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from youtube_playlist_builder.backup import create_backup, restore_backup, inspect_database
from youtube_playlist_builder.cli import main
from youtube_playlist_builder.state import State


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'live' / 'custom.sqlite3'
        self.state = State(self.source)
        self.addCleanup(self.state.close)
        self.state.add('Photographie É', channels=['UC1', 'UC2'], enabled=False, max_videos=6)
        self.state.add('Engineering', channels=['UC2'])
        self.state.set_meta('single_topic_channels', True)
        self.state.set_meta('subscription_cache', {'channels':[{'channel_id':'UC1', 'title':'Échecs'}]})
        self.state.set_meta('owner', 'owner-channel')
        self.state.begin_write(self.state.topic('Engineering')['id'], 'insert', {'video_id':'video'})
        self.state.charge('2026-10-29', 50)
        self.export = self.source.parent / 'subscriptions.json'
        self.export.write_text(json.dumps([{'channel_id':'UC1', 'title':'Échecs'}]))
        (self.source.parent / 'token.json').write_text('secret-token')
        (self.source.parent / 'llm.json').write_text('secret-key')

    def backup(self):
        return create_backup(self.source, self.root / 'saved.zip')

    def rewrite(self, name, replacement):
        path, manifest = self.backup()
        altered = self.root / 'altered.zip'
        with zipfile.ZipFile(path) as original, zipfile.ZipFile(altered, 'w') as result:
            for member in original.namelist():
                result.writestr(member, replacement if member == name else original.read(member))
        return altered

    def test_round_trip_preserves_every_table_and_credentials_are_excluded(self):
        before = list(self.state.db.iterdump())
        path, manifest = self.backup()
        self.assertEqual(list(self.state.db.iterdump()), before)
        self.assertEqual(manifest['counts'], {'topics':2, 'assigned_channels':2, 'memberships':3, 'pending_writes':1})
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(set(archive.namelist()), {'manifest.json', 'playlists.sqlite3', 'subscriptions.json'})
        restored, result = restore_backup(path, self.root / 'restored')
        with closing(sqlite3.connect(restored / 'playlists.sqlite3')) as db:
            self.assertEqual(list(db.iterdump()), before)
        self.assertEqual(result, manifest)
        self.assertEqual((restored / 'subscriptions.json').read_bytes(), self.export.read_bytes())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(restored.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in restored.iterdir()))

    def test_wal_commits_included_and_uncommitted_changes_excluded(self):
        self.state.db.execute('PRAGMA journal_mode=WAL')
        self.state.set_meta('wal-test', 'committed')
        # An open write transaction must not block an online backup of committed data.
        self.state.db.execute("UPDATE meta SET value='\"not committed\"' WHERE key='wal-test'")
        path, _ = self.backup()
        restored, _ = restore_backup(path, self.root / 'restored')
        with closing(sqlite3.connect(restored / 'playlists.sqlite3')) as db:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='wal-test'").fetchone()[0], '"committed"')
        self.state.db.rollback()

    def test_optional_export_and_timestamped_defaults(self):
        self.export.unlink()
        first, _ = create_backup(self.source)
        second, _ = create_backup(self.source)
        self.assertNotEqual(first, second)
        self.assertEqual(first.parent, self.source.parent / 'backups')
        restored, _ = restore_backup(first, self.root / 'restored')
        self.assertFalse((restored / 'subscriptions.json').exists())
        self.assertEqual(inspect_database(restored / 'playlists.sqlite3')['topics'], 2)

    def test_existing_destinations_are_never_replaced(self):
        path, _ = self.backup()
        original = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'already exists'):
            create_backup(self.source, path)
        self.assertEqual(path.read_bytes(), original)
        folder = self.root / 'existing'
        folder.mkdir()
        with self.assertRaisesRegex(ValueError, 'already exists'):
            restore_backup(path, folder)
        self.assertEqual(list(folder.iterdir()), [])
        link = self.root / 'broken-link'
        link.symlink_to(self.root / 'missing')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            create_backup(self.source, link)
        with self.assertRaisesRegex(ValueError, 'already exists'):
            restore_backup(path, link)

    def test_checksum_failure_leaves_no_restore_folder(self):
        path = self.rewrite('subscriptions.json', b'[]')
        with self.assertRaisesRegex(ValueError, 'size|checksum'):
            restore_backup(path, self.root / 'restored')
        self.assertFalse((self.root / 'restored').exists())

    def test_manifest_version_and_counts_are_checked(self):
        path, manifest = self.backup()
        for field, value in [('version', 999), ('counts', {})]:
            changed = dict(manifest, **{field:value})
            other = self.root / (field + '.zip')
            with zipfile.ZipFile(path) as original, zipfile.ZipFile(other, 'w') as archive:
                for name in original.namelist():
                    archive.writestr(name, json.dumps(changed) if name == 'manifest.json' else original.read(name))
            with self.assertRaises(ValueError):
                restore_backup(other, self.root / 'restored')
            self.assertFalse((self.root / 'restored').exists())

    def test_unexpected_archive_paths_and_nonzip_rejected(self):
        path, _ = self.backup()
        with zipfile.ZipFile(path, 'a') as archive:
            archive.writestr('../escape', 'bad')
        with self.assertRaisesRegex(ValueError, 'Unexpected'):
            restore_backup(path, self.root / 'restored')
        bad = self.root / 'bad.zip'
        bad.write_text('not a zip')
        with self.assertRaisesRegex(ValueError, 'damaged'):
            restore_backup(bad, self.root / 'restored')
        self.assertFalse((self.root / 'restored').exists())
        self.assertFalse((self.root / 'escape').exists())

    def test_missing_and_nonapplication_database_do_not_create_backup(self):
        missing = self.root / 'missing.sqlite3'
        with self.assertRaisesRegex(ValueError, 'No database'):
            create_backup(missing)
        self.assertFalse(missing.exists())
        empty = self.root / 'empty.sqlite3'
        with closing(sqlite3.connect(empty)):
            pass
        with self.assertRaisesRegex(ValueError, 'supported'):
            create_backup(empty, self.root / 'saved.zip')
        self.assertFalse((self.root / 'saved.zip').exists())

    @patch('youtube_playlist_builder.cli.authenticate')
    @patch('youtube_playlist_builder.cli.build')
    def test_commands_are_offline_and_do_not_initialise_schedules(self, build, authenticate):
        archive = self.root / 'saved.zip'
        before = list(self.state.db.iterdump())
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--state', str(self.source), 'backup', '--output', str(archive)]), 0)
            self.assertEqual(main(['restore', str(archive), '--to', str(self.root / 'restored')]), 0)
            self.assertEqual(main(['restore', str(archive), '--to', str(self.root / 'restored')]), 1)
        authenticate.assert_not_called()
        build.assert_not_called()
        self.assertEqual(list(self.state.db.iterdump()), before)
