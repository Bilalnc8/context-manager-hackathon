"""Engine and session helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from emailctx.store.models import Base


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def make_engine(db_path: Path) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}")

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        # Many users share one database: WAL lets reads run during a write, and a writer
        # waits up to 30s for another (e.g. a CLI ingest while the server runs) instead
        # of failing at once. Inside the server, writes also queue on Runtime.write_lock.
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    Base.metadata.create_all(engine)
    _add_missing_columns(engine)
    return engine


# Columns added after a table first shipped: create_all doesn't alter existing tables.
_ADDED_COLUMNS = {
    "requirements": {
        "delivery": "VARCHAR NOT NULL DEFAULT 'open'",
        "delivery_note": "TEXT",
    },
    "messages": {
        "sender": "VARCHAR",
        "body": "TEXT",
    },
}


def _add_missing_columns(engine: Engine) -> None:
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            have = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            for name, ddl in columns.items():
                if name not in have:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
