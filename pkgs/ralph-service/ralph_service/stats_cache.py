"""Disposable, private statistics summaries and source checkpoints."""

import fcntl
import json
import os
import sqlite3
from contextlib import closing, contextmanager
from itertools import groupby
from operator import itemgetter
from pathlib import Path
from urllib.parse import quote

TOKEN_FIELDS = ("input", "output", "cached", "cache_write", "reasoning")

# Bump whenever extraction or cached summary interpretation changes.
VERSION = 10

USAGE_KEYS = (
    "review_id",
    "repository_id",
    "number",
    "job_id",
    "generation",
    "stage",
    "provider",
    "model",
    "month",
    "day",
    "status",
    "http_status",
)
USAGE_SUMS = (
    "requests",
    "unknown_usage",
    "incomplete_usage",
    "web_search_calls",
    "unknown_web_search",
    *TOKEN_FIELDS,
    *(f"unknown_{name}" for name in TOKEN_FIELDS),
)


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
                    "DROP TABLE IF EXISTS usage;"
                )
                self.full = True
            self.db.executescript(f"""
                CREATE TABLE IF NOT EXISTS sources (
                    source TEXT PRIMARY KEY, identity TEXT, sequence INTEGER
                );
                CREATE TABLE IF NOT EXISTS records (
                    source TEXT, key TEXT, position INTEGER, summary TEXT,
                    PRIMARY KEY (source, key)
                );
                CREATE INDEX IF NOT EXISTS records_order ON records(source, position);
                CREATE TABLE IF NOT EXISTS usage (
                    source TEXT, {", ".join(USAGE_KEYS + USAGE_SUMS)},
                    requested_at, completed_at
                );
                CREATE INDEX IF NOT EXISTS usage_review ON usage(source, review_id);
            """)
            if version != VERSION:
                self.db.execute(f"PRAGMA user_version={VERSION}")
            self.db.execute("BEGIN")
            if self.full:
                self.db.execute("DELETE FROM sources")
                self.db.execute("DELETE FROM records")
                self.db.execute("DELETE FROM usage")
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

    @contextmanager
    def _source(self, path, table):
        """Yield one source snapshot and the keys changed since the last run.

        Changes are None when everything derived from the source must be rebuilt.
        """
        path = Path(path).resolve()
        source = str(path) + ":" + table
        # Only trusted module constants supply table names.
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
            changes = (
                None
                if rebuild
                else [
                    row[0]
                    for row in db.execute(
                        "SELECT record_key FROM stats_changes WHERE table_name=? AND sequence>? ORDER BY sequence",
                        (table, previous[1]),
                    )
                ]
            )
            yield source, db, changes
            if metadata and (
                previous is None or previous != (identity, metadata["sequence"])
            ):
                self.db.execute(
                    "INSERT OR REPLACE INTO sources VALUES (?, ?, ?)",
                    (source, identity, metadata["sequence"]),
                )
            elif metadata is None:
                self.db.execute("DELETE FROM sources WHERE source=?", (source,))

    def rows(self, path, table, key, summarize):
        """Read source changes and rows from one snapshot; stream cold loads."""
        with self._source(path, table) as (source, db, changes):
            if changes is None:
                self.db.execute("DELETE FROM records WHERE source=?", (source,))
                rows = db.execute(
                    f'SELECT rowid AS _position, * FROM "{table}" ORDER BY rowid'
                )
                for row in rows:
                    self._save(source, str(row[key]), row, summarize)
            else:
                for record_key in changes:
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
        return [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT summary FROM records WHERE source=? ORDER BY position",
                (source,),
            )
        ]

    def usage(self, path, record):
        """Return request totals for each review, stage, model and day.

        Changed reviews are recounted from their requests, never added to, so
        retries and late completions cannot be counted twice.
        """
        with self._source(path, "requests") as (source, db, changes):
            if changes is not None:
                reviews = {
                    row[0]
                    for token in changes
                    for row in db.execute(
                        "SELECT review_id FROM requests WHERE token=?", (token,)
                    )
                }
                for review in reviews:
                    self.db.execute(
                        "DELETE FROM usage WHERE source=? AND review_id=?",
                        (source, review),
                    )
                    self._save_usage(
                        source,
                        db.execute(
                            "SELECT * FROM requests WHERE review_id=?", (review,)
                        ),
                        record,
                    )
                counted = self.db.execute(
                    "SELECT TOTAL(requests) FROM usage WHERE source=?", (source,)
                ).fetchone()[0]
                stored = db.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
                # Deleted or renamed requests no longer identify their old review.
                if counted != stored:
                    changes = None
            if changes is None:
                self.db.execute("DELETE FROM usage WHERE source=?", (source,))
                for _, rows in groupby(
                    db.execute("SELECT * FROM requests ORDER BY review_id"),
                    key=itemgetter("review_id"),
                ):
                    self._save_usage(source, rows, record)
        rows = self.db.execute("SELECT * FROM usage WHERE source=?", (source,))
        names = [column[0] for column in rows.description]
        return [dict(zip(names, row)) for row in rows]

    def _save_usage(self, source, rows, record):
        totals = {}
        for row in rows:
            value = record(row)
            key = tuple(value[name] for name in USAGE_KEYS)
            total = totals.setdefault(key, dict.fromkeys(USAGE_SUMS))
            for name in USAGE_SUMS:
                if value[name] is not None:
                    total[name] = (total[name] or 0) + value[name]
            for name, choose in (("requested_at", min), ("completed_at", max)):
                times = [
                    time
                    for time in (total.get(name), value[name])
                    if isinstance(time, (int, float))
                ]
                total[name] = choose(times, default=None)
        columns = (*USAGE_KEYS, *USAGE_SUMS, "requested_at", "completed_at")
        self.db.executemany(
            f"INSERT INTO usage (source, {', '.join(columns)}) "
            f"VALUES ({', '.join('?' * (len(columns) + 1))})",
            (
                (source, *key, *(total[name] for name in columns[len(USAGE_KEYS) :]))
                for key, total in totals.items()
            ),
        )

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
