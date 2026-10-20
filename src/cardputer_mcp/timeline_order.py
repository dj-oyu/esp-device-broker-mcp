"""Persistent first-registration order, independent of mutable job outcomes."""

import sqlite3


def initialize_timeline_order(db: sqlite3.Connection) -> None:
    # Add display metadata only: never alter a plan hash, outcome or review flag.
    # Old unexecuted plans have no wall-clock creation time. Preserve their
    # insertion order as an explicitly undated legacy prefix; do not invent dates.
    with db:
        db.execute("""CREATE TABLE IF NOT EXISTS timeline_order (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL UNIQUE, created TEXT
        )""")
        if (
            db.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name IN "
                "('timeline_plan_registered','timeline_job_registered')"
            ).fetchone()[0]
            == 2
        ):
            return
        db.execute("""INSERT OR IGNORE INTO timeline_order(job_id,created)
            SELECT CASE WHEN length(CAST(p.payload AS BLOB))<=16384 AND json_valid(p.payload)
                THEN json_extract(p.payload,'$.job_id') END, NULL
            FROM plans p
            WHERE CASE WHEN length(CAST(p.payload AS BLOB))<=16384 AND json_valid(p.payload)
                THEN json_type(p.payload,'$.job_id')='text'
                    AND length(json_extract(p.payload,'$.job_id')) BETWEEN 1 AND 64
                    AND NOT EXISTS (SELECT 1 FROM jobs j
                        WHERE j.id=json_extract(p.payload,'$.job_id')) ELSE 0 END
            ORDER BY p.rowid""")
        db.execute("""INSERT OR IGNORE INTO timeline_order(job_id,created)
            SELECT id,created FROM jobs ORDER BY created,rowid""")
        db.execute("""
            CREATE TRIGGER IF NOT EXISTS timeline_plan_registered AFTER INSERT ON plans
            WHEN CASE WHEN length(CAST(NEW.payload AS BLOB))<=16384 AND json_valid(NEW.payload)
                THEN json_type(NEW.payload,'$.job_id')='text'
                    AND length(json_extract(NEW.payload,'$.job_id')) BETWEEN 1 AND 64
                ELSE 0 END
            BEGIN
                INSERT OR IGNORE INTO timeline_order(job_id,created)
                    VALUES(json_extract(NEW.payload,'$.job_id'),CURRENT_TIMESTAMP);
            END;
        """)
        db.execute("""
            CREATE TRIGGER IF NOT EXISTS timeline_job_registered AFTER INSERT ON jobs
            BEGIN
                INSERT OR IGNORE INTO timeline_order(job_id,created) VALUES(NEW.id,NEW.created);
            END;
        """)
