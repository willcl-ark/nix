"""Transactional, bounded change cursors for derived statistics."""

import re
import uuid


def install(db, table, key):
    """Track the latest mutation of each key, including deleted keys.

    Install while initializing a source store. Existing rows are deliberately
    not entered: a consumer seeing a new identity must read a full snapshot.
    Installation joins an existing transaction with a savepoint, or obtains
    the write lock before reading schema in an autocommit connection.
    """
    if not all(re.fullmatch(r"[a-z_]+", name) for name in (table, key)):
        raise ValueError("invalid statistics source identifier")
    own_transaction = not db.in_transaction
    db.execute(
        "BEGIN IMMEDIATE" if own_transaction else "SAVEPOINT install_stats_changes"
    )
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS stats_sources (
            table_name TEXT PRIMARY KEY,
            identity TEXT NOT NULL,
            sequence INTEGER NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS stats_changes (
            table_name TEXT NOT NULL,
            record_key TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            PRIMARY KEY (table_name, record_key)
        )""")
        db.execute("""CREATE INDEX IF NOT EXISTS stats_changes_sequence
            ON stats_changes(table_name, sequence)""")
        db.execute(
            "INSERT OR IGNORE INTO stats_sources VALUES (?, ?, 0)",
            (table, str(uuid.uuid4())),
        )
        for operation, versions in (
            ("INSERT", ("NEW",)),
            ("UPDATE", ("OLD", "NEW")),
            ("DELETE", ("OLD",)),
        ):
            changes = "\n".join(
                f"""INSERT INTO stats_changes(table_name, record_key, sequence)
                    SELECT '{table}', CAST({version}.{key} AS TEXT), sequence
                    FROM stats_sources WHERE table_name = '{table}'
                    ON CONFLICT(table_name, record_key)
                    DO UPDATE SET sequence = excluded.sequence;"""
                for version in versions
            )
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS stats_{table}_{operation.lower()}
                AFTER {operation} ON {table}
                BEGIN
                    UPDATE stats_sources SET sequence = sequence + 1
                        WHERE table_name = '{table}';
                    {changes}
                END""")
        if own_transaction:
            db.commit()
        else:
            db.execute("RELEASE install_stats_changes")
    except BaseException:
        if own_transaction:
            db.rollback()
        else:
            db.execute("ROLLBACK TO install_stats_changes")
            db.execute("RELEASE install_stats_changes")
        raise
