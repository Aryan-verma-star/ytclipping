"""Engine/session factory.

- SQLite (default, local dev + tests): WAL journal, relaxed thread checks so
  the worker thread and request handlers can share the engine.
- PostgreSQL (production, e.g. Neon): psycopg2 with pool_pre_ping so a
  suspended serverless compute resumes transparently.
"""

from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Base


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        kwargs: dict = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        self.engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover
                cursor = dbapi_connection.cursor()
                try:
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.execute("PRAGMA busy_timeout=30000")
                    cursor.execute("PRAGMA synchronous=NORMAL")
                finally:
                    cursor.close()

        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False, autoflush=False)

    def session(self) -> Session:
        return self.Session()

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    def dispose(self) -> None:
        self.engine.dispose()
