from datetime import UTC, datetime

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from . import config

engine = create_engine(config.DB_URL, connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.close()


SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    # SQLite has no timezone support: store naive UTC, re-attach tz when serializing.
    return datetime.now(UTC).replace(tzinfo=None)


def get_session():
    with SessionLocal() as session:
        yield session
