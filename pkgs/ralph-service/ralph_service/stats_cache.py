"""Disposable, private statistics summaries and source checkpoints."""

import fcntl
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

# Bump whenever extraction or cached summary interpretation changes.
VERSION = 1


class Cache:
    def __init__(self, directory, *, full=False):
        self.directory = Path(directory)
        self.full = full

    def __enter__(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.lock = (self.directory / ".lock").open("a")
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        try:
            self.db = sqlite3.connect(self.directory / "cache.sqlite3")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version != VERSION:
                self.db.executescript(
                    "DROP TABLE IF EXISTS sources; DROP TABLE IF EXISTS records;"
                )
                self.full = True
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS sources (
                    source TEXT PRIMARY KEY, identity TEXT, sequence INTEGER
                );
                CREATE TABLE IF NOT EXISTS records (
                    source TEXT, key TEXT, position INTEGER, summary TEXT,
                    PRIMARY KEY (source, key)
                );
                CREATE INDEX IF NOT EXISTS records_order ON records(source, position);
            """)
            if version != VERSION:
                self.db.execute(f"PRAGMA user_version={VERSION}")
            self.db.execute("BEGIN")
            if self.full:
                self.db.execute("DELETE FROM sources")
                self.db.execute("DELETE FROM records")
            return self
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            self.lock.close()
            raise

    def __exit__(self, kind, value, traceback):
        try:
            if kind is None:
                self.db.commit()
            else:
                self.db.rollback()
        finally:
            self.db.close()
            self.lock.close()

    def rows(self, path, table, key, summarize):
        """Read source changes and rows from one snapshot; stream cold loads."""
        path = Path(path).resolve()
        source = str(path) + ":" + table
        # Only trusted module constants supply table/key identifiers.
        uri = f"file:{quote(str(path), safe='/:')}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            tracked = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stats_sources'"
            ).fetchone()
            metadata = (
                db.execute(
                    "SELECT identity, sequence FROM stats_sources WHERE table_name=?",
                    (table,),
                ).fetchone()
                if tracked
                else None
            )
            stat = path.stat()
            identity = (
                json.dumps([metadata["identity"], stat.st_dev, stat.st_ino])
                if metadata
                else None
            )
            previous = self.db.execute(
                "SELECT identity, sequence FROM sources WHERE source=?", (source,)
            ).fetchone()
            rebuild = (
                metadata is None
                or previous is None
                or previous[0] != identity
                or previous[1] > metadata["sequence"]
            )
            if rebuild:
                self.db.execute("DELETE FROM records WHERE source=?", (source,))
                rows = db.execute(
                    f'SELECT rowid AS _position, * FROM "{table}" ORDER BY rowid'
                )
                for row in rows:
                    self._save(source, str(row[key]), row, summarize)
            else:
                changes = db.execute(
                    "SELECT record_key FROM stats_changes WHERE table_name=? AND sequence>? ORDER BY sequence",
                    (table, previous[1]),
                )
                for change in changes:
                    record_key = change[0]
                    row = db.execute(
                        f'SELECT rowid AS _position, * FROM "{table}" WHERE "{key}"=?',
                        (record_key,),
                    ).fetchone()
                    self.db.execute(
                        "DELETE FROM records WHERE source=? AND key=?",
                        (source, record_key),
                    )
                    if row is not None:
                        self._save(source, record_key, row, summarize)
            # REPLACE can delete a conflicting UNIQUE row without firing a
            # DELETE trigger. Reconcile only keys, never historical payloads.
            live_keys = {
                str(row[0]) for row in db.execute(f'SELECT "{key}" FROM "{table}"')
            }
            cached_keys = {
                row[0]
                for row in self.db.execute(
                    "SELECT key FROM records WHERE source=?", (source,)
                )
            }
            self.db.executemany(
                "DELETE FROM records WHERE source=? AND key=?",
                ((source, removed) for removed in cached_keys - live_keys),
            )
            if metadata and (
                previous is None or previous != (identity, metadata["sequence"])
            ):
                self.db.execute(
                    "INSERT OR REPLACE INTO sources VALUES (?, ?, ?)",
                    (source, identity, metadata["sequence"]),
                )
            elif metadata is None:
                self.db.execute("DELETE FROM sources WHERE source=?", (source,))
        return [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT summary FROM records WHERE source=? ORDER BY position",
                (source,),
            )
        ]

    def _save(self, source, key, row, summarize):
        self.db.execute(
            "INSERT OR REPLACE INTO records VALUES (?, ?, ?, ?)",
            (
                source,
                key,
                row["_position"],
                json.dumps(summarize(dict(row)), separators=(",", ":")),
            ),
        )
