"""
Database setup.

SQLite for the demo (zero config, single file, easy to inspect/reset).
Swapping to Postgres is a one-line change: replace SQLALCHEMY_DATABASE_URL
with a postgres:// DSN — nothing else in the codebase references SQLite
directly, since all access goes through SQLAlchemy's ORM layer.
"""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base

SQLALCHEMY_DATABASE_URL = "sqlite:///./watchlist.db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={
        "check_same_thread": False,  # needed because the feed simulator runs on a
                                      # background thread and shares the same engine
        "timeout": 15,  # seconds to wait on a locked db before erroring, rather than
                         # failing immediately, if two writers briefly overlap
    },
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):
    """
    WAL (write-ahead log) mode is the right fix here, not just a bigger timeout:
    this app has a background thread (the market feed) writing continuously
    while API requests read and occasionally write concurrently. In SQLite's
    default journal mode, writers block readers and vice versa, which is what
    produced intermittent "database is locked" / "readonly database" errors
    under concurrent access. WAL mode lets readers and a writer proceed at the
    same time (only writer-vs-writer still serializes), which matches this
    app's actual access pattern.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
