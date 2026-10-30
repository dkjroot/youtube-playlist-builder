"""Durable state used throughout the CLI, scheduler and synchronisation workflow.

SQLite stores topic definitions, owned playlist/item IDs, unresolved remote write
intent, quota ledgers, settings and model caches. The external file lock spans
network operations; database transactions group only the corresponding local
state transitions. Keep this database across runs: deleting it loses recovery
and accounting information, not merely disposable caches.
"""

from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import sqlite3
import uuid


class State:
    """SQLite persistence shared by CLI, scheduler, sync and classification caches.

    Callers hold locked() across operations involving remote writes. Methods that
    commit are explicit; put_item, clear_pending and _update deliberately participate
    in the caller’s transaction so remote results and recovery intent change together.
    """
    def __init__(self, path: Path):
        """Open/create local tables and apply additive migrations without replacing user data."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                marker TEXT NOT NULL UNIQUE,
                playlist_name TEXT,
                frequency TEXT NOT NULL DEFAULT 'daily',
                max_age_days INTEGER NOT NULL DEFAULT 7,
                enabled INTEGER NOT NULL DEFAULT 1,
                channels TEXT NOT NULL DEFAULT '[]',
                include_words TEXT NOT NULL DEFAULT '[]',
                exclude_words TEXT NOT NULL DEFAULT '[]',
                playlist_id TEXT,
                remote_title TEXT,
                last_success TEXT,
                next_due TEXT,
                last_attempt TEXT,
                last_error TEXT,
                reconciled_at TEXT
            );
            CREATE TABLE IF NOT EXISTS voice_matches (
                phrase TEXT NOT NULL,
                topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
                uses INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(phrase, topic_id)
            );
            CREATE TABLE IF NOT EXISTS items (
                topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
                item_id TEXT NOT NULL,
                video_id TEXT NOT NULL,
                published_at TEXT,
                title TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(topic_id, item_id)
            );
            CREATE TABLE IF NOT EXISTS channel_videos (
                video_id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS channel_videos_channel ON channel_videos(channel_id, published_at);
            CREATE TABLE IF NOT EXISTS pending (
                topic_id INTEGER PRIMARY KEY REFERENCES topics(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_diagnostics (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS quota (
                day TEXT PRIMARY KEY, used INTEGER NOT NULL DEFAULT 0,
                exhausted INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS api_usage (
                day TEXT NOT NULL, resource TEXT NOT NULL, method TEXT NOT NULL,
                calls INTEGER NOT NULL DEFAULT 0, units INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(day, resource, method)
            );
        """)

        if 'description' not in {row[1] for row in self.db.execute('PRAGMA table_info(topics)')}:
            with self.db:
                self.db.execute("ALTER TABLE topics ADD COLUMN description TEXT NOT NULL DEFAULT ''")
        # Existing installations give every channel medium priority. Rotation is
        # stored on the topic so its successful turn position survives restarts.
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(topics)')}
        with self.db:
            for column in ('channel_priorities', 'channel_rotation', 'channel_limits', 'channel_max_age_days'):
                if column not in columns:
                    self.db.execute(f"ALTER TABLE topics ADD COLUMN {column} TEXT NOT NULL DEFAULT '{{}}'")

        if 'max_videos' not in columns:
            with self.db:
                self.db.execute('ALTER TABLE topics ADD COLUMN max_videos INTEGER')

        if 'channel_id' not in {row[1] for row in self.db.execute('PRAGMA table_info(items)')}:
            with self.db:
                self.db.execute('ALTER TABLE items ADD COLUMN channel_id TEXT')

    def close(self):
        """Release the connection after the CLI operation, even if an API request failed."""
        self.db.close()

    @contextmanager
    def locked(self):
        # Held across network calls too: two cron/menu processes must not both insert.
        """Exclude other updater processes for the entire operation, including network waits."""
        with self.path.with_suffix(self.path.suffix + '.lock').open('a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('Another process is using this topic database. Try again when it finishes.') from None
            try:
                yield self
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def remember_channel_videos(self, videos):
        """Keep five newest observed uploads per channel for offline organisation.

        Sync has already paid for these details. This cache is deliberately not
        used to decide fresh playlist contents or whether videos remain available.
        """
        videos = list(videos)
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO channel_videos VALUES (?,?,?,?,?)",
                [(v['video_id'], v['channel_id'], v['title'], v.get('description', ''),
                  v['published_at']) for v in videos])
            for cid in {v['channel_id'] for v in videos}:
                self.db.execute(
                    "DELETE FROM channel_videos WHERE channel_id=? AND video_id NOT IN "
                    "(SELECT video_id FROM channel_videos WHERE channel_id=? ORDER BY published_at DESC, video_id DESC LIMIT 5)",
                    (cid, cid))

    def meta(self, key, default=None):
        """Read a JSON setting/cache value, falling back only when no row exists."""
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        """Persist one JSON setting or cache entry immediately for restart-safe reuse."""
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value)))

    @staticmethod
    def decode(row):
        """Convert a topic row into application values and report missing topics consistently."""
        if row is None:
            raise ValueError('Topic not found.')
        topic = dict(row)
        for key in ('channels', 'include_words', 'exclude_words', 'channel_priorities', 'channel_rotation', 'channel_limits', 'channel_max_age_days'):
            topic[key] = json.loads(topic[key])
        return topic

    def topics(self):
        """Return topics in stable creation order, the basis of the round-robin cursor."""
        return [self.decode(row) for row in self.db.execute('SELECT * FROM topics ORDER BY id')]

    def topic(self, name):
        """Find a topic by its case-insensitive display name."""
        return self.decode(self.db.execute('SELECT * FROM topics WHERE name=?', (name,)).fetchone())

    def by_id(self, topic_id):
        """Reload current topic state after a write/reconciliation changed saved IDs or dates."""
        return self.decode(self.db.execute('SELECT * FROM topics WHERE id=?', (topic_id,)).fetchone())

    def add(self, name, **fields):
        """Create a topic and unique remote recovery marker in one transaction."""
        with self.db:
            try:
                cursor = self.db.execute('INSERT INTO topics(name,marker) VALUES (?,?)', (name, str(uuid.uuid4())))
            except sqlite3.IntegrityError:
                raise ValueError(f'Topic {name!r} already exists.') from None
            self._update(cursor.lastrowid, fields)
        return self.topic(name)

    def _update(self, topic_id, fields):
        """Apply allowlisted fields without committing; callers can group related state changes."""
        allowed = {'description', 'name', 'playlist_name', 'frequency', 'max_age_days', 'enabled', 'channels',
                   'include_words', 'exclude_words', 'playlist_id', 'remote_title', 'last_success',
                   'next_due', 'last_attempt', 'last_error', 'reconciled_at', 'channel_priorities', 'channel_rotation', 'channel_limits', 'max_videos', 'channel_max_age_days'}
        if fields.keys() - allowed:
            raise ValueError('Unknown topic field.')
        values = {key: json.dumps(value) if key in ('channels', 'include_words', 'exclude_words', 'channel_priorities', 'channel_rotation', 'channel_limits', 'channel_max_age_days')
                  else value for key, value in fields.items()}
        if values:
            self.db.execute(f"UPDATE topics SET {', '.join(key + '=?' for key in values)} WHERE id=?",
                            (*values.values(), topic_id))

    def update(self, topic_id, **fields):
        """Commit an isolated topic edit; use _update when grouping recovery changes."""
        with self.db:
            self._update(topic_id, fields)

    def remove(self, topic_id):
        """Forget a topic locally, retaining its remote playlist and refusing uncertain writes."""
        if self.pending(topic_id):
            raise ValueError('This topic has an uncertain write. Reconcile it before removing it.')
        with self.db:
            self.db.execute('DELETE FROM topics WHERE id=?', (topic_id,))

    def items(self, topic_id):
        """Return saved playlist-item IDs so deletions do not require a remote playlist read."""
        return [dict(row) for row in self.db.execute('SELECT * FROM items WHERE topic_id=?', (topic_id,))]

    def put_item(self, topic_id, item):
        """Record one returned item ID inside the caller’s success/intent-clearing transaction."""
        self.db.execute('INSERT OR REPLACE INTO items(topic_id,item_id,video_id,published_at,title,channel_id) VALUES (?,?,?,?,?,?)',
                        (topic_id, item['item_id'], item['video_id'], item.get('published_at'), item.get('title', ''), item.get('channel_id')))

    def pending(self, topic_id):
        """Return the unresolved remote write intent, or None when state is unambiguous."""
        row = self.db.execute('SELECT * FROM pending WHERE topic_id=?', (topic_id,)).fetchone()
        return {'kind': row['kind'], 'payload': json.loads(row['payload'])} if row else None

    def begin_write(self, topic_id, kind, payload):
        """Commit intent before dispatch; a crash or lost reply must leave recovery evidence."""
        with self.db:
            self.db.execute('INSERT INTO pending VALUES (?,?,?)', (topic_id, kind, json.dumps(payload)))

    def clear_pending(self, topic_id):
        """Clear intent within the same transaction that records the successful remote outcome."""
        self.db.execute('DELETE FROM pending WHERE topic_id=?', (topic_id,))

    def quota(self, day):
        """Read the Pacific-day quota ledger without manufacturing a stored row on read."""
        row = self.db.execute('SELECT used,exhausted FROM quota WHERE day=?', (day,)).fetchone()
        return dict(row) if row else {'used': 0, 'exhausted': 0}

    def charge(self, day, cost, *, resource=None, method=None):
        """Charge quota and optional endpoint statistics atomically before dispatch.

        Failed requests count too. Statistics start when this instrumentation is
        installed; older aggregate quota rows cannot be broken down retrospectively.
        """
        with self.db:
            self.db.execute('INSERT INTO quota(day,used) VALUES (?,?) ON CONFLICT(day) DO UPDATE SET used=used+excluded.used', (day, cost))
            if resource and method:
                self.db.execute('''INSERT INTO api_usage(day,resource,method,calls,units) VALUES (?,?,?,1,?)
                    ON CONFLICT(day,resource,method) DO UPDATE SET calls=calls+1,units=units+excluded.units''',
                    (day, resource, method, cost))

    def api_usage(self, day):
        """Return the locally measured endpoint breakdown for a Pacific quota day."""
        return [dict(row) for row in self.db.execute(
            'SELECT resource,method,calls,units FROM api_usage WHERE day=? ORDER BY units DESC,resource,method', (day,))]

    def exhaust(self, day):
        """Remember a server quota refusal until the next Pacific day, avoiding repeated failures."""
        with self.db:
            self.db.execute('INSERT INTO quota(day,exhausted) VALUES (?,1) ON CONFLICT(day) DO UPDATE SET exhausted=1', (day,))
