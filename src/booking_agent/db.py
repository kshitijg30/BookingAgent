"""Database plumbing shared by the agent store and the mock provider.

SQLite for local runs and tests, Postgres (Supabase) in production, chosen by URL. On Postgres the
tables live in a named schema via SQLAlchemy's schema_translate_map; SQLite has no schemas, so the
same table definitions run unchanged there.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection, Engine

from booking_agent.schema import Migration, initialize_schema

JSONType = sa.JSON().with_variant(JSONB(), "postgresql")


class TZDateTime(sa.types.TypeDecorator[datetime]):
    """timestamptz on Postgres; UTC text on SQLite. Always returns timezone-aware datetimes."""

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: sa.Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime")
        value = value.astimezone(UTC)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: datetime | None, dialect: sa.Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def normalize_url(url_or_path: str | Path) -> str:
    url = str(url_or_path)
    if "://" not in url:
        return f"sqlite:///{url}"
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


class Database:
    """An engine plus a thread-local transaction, so nested calls join one transaction."""

    def __init__(
        self,
        url_or_path: str | Path,
        metadata: sa.MetaData,
        *,
        schema: str,
        migrations: tuple[Migration, ...] = (),
    ) -> None:
        url = normalize_url(url_or_path)
        self.is_sqlite = url.startswith("sqlite")
        if self.is_sqlite:
            engine = sa.create_engine(url, connect_args={"check_same_thread": False})

            @sa.event.listens_for(engine, "connect")
            def _pragma(dbapi_conn: Any, _: Any) -> None:
                dbapi_conn.execute("PRAGMA journal_mode=WAL")
                dbapi_conn.execute("PRAGMA busy_timeout=5000")

            self.engine: Engine = engine
        else:
            base = sa.create_engine(url, pool_pre_ping=True, pool_recycle=1800, pool_size=5, max_overflow=5)
            with base.begin() as c:
                c.execute(sa.text(f'create schema if not exists "{schema}"'))
            self.engine = base.execution_options(schema_translate_map={None: schema})
        initialize_schema(self.engine, metadata, schema=schema, migrations=migrations)
        self._coordination_engine = None if self.is_sqlite else sa.create_engine(url, poolclass=sa.pool.NullPool)
        self._lock = threading.RLock()
        self._local = threading.local()

    def close(self) -> None:
        self.engine.dispose()
        if self._coordination_engine is not None:
            self._coordination_engine.dispose()

    @contextmanager
    def coordination_lock(self, key: str) -> Iterator[None]:
        """Serialize a whole operation without keeping its write-ahead transaction open.

        PostgreSQL uses session advisory locks on dedicated, nonpooled connections.
        SQLite uses OS locks beside the database (same-host processes, including workers).
        """
        held = getattr(self._local, "coordination", None)
        if held is None:
            held = self._local.coordination = set()
        if key in held:
            yield
            return
        digest = hashlib.sha256(key.encode()).digest()
        if self._coordination_engine is not None:
            lock_key = int.from_bytes(digest[:8], signed=True)
            with self._coordination_engine.connect() as connection:
                connection.execute(sa.text("select pg_advisory_lock(:key)"), {"key": lock_key})
                connection.commit()
                held.add(key)
                try:
                    yield
                finally:
                    held.remove(key)
                    connection.execute(sa.text("select pg_advisory_unlock(:key)"), {"key": lock_key})
                    connection.commit()
        else:
            database = self.engine.url.database
            if database is None or database == ":memory:":
                with self._lock:
                    yield
                return
            directory = Path(database).resolve().parent / ".booking-agent-locks"
            directory.mkdir(exist_ok=True)
            path = directory / (hashlib.sha256(str(Path(database).resolve()).encode() + digest).hexdigest() + ".lock")
            with path.open("a+b") as stream:
                if os.name == "nt":
                    msvcrt = importlib.import_module("msvcrt")

                    stream.seek(0)
                    if not stream.read(1):
                        stream.write(b"0")
                        stream.flush()
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                held.add(key)
                try:
                    yield
                finally:
                    held.remove(key)
                    if os.name == "nt":
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def tx(self) -> Iterator[Connection]:
        conn: Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            yield conn
            return
        with self._lock, self.engine.begin() as c:
            self._local.conn = c
            try:
                yield c
            finally:
                self._local.conn = None

    def rows(self, stmt: sa.Executable) -> list[sa.RowMapping]:
        with self.tx() as c:
            return list(c.execute(stmt).mappings())

    def one(self, stmt: sa.Executable) -> sa.RowMapping | None:
        with self.tx() as c:
            return c.execute(stmt).mappings().first()

    def exec(self, stmt: sa.Executable) -> int:
        with self.tx() as c:
            return int(c.execute(stmt).rowcount or 0)

    def upsert(self, table: sa.Table, key: dict[str, Any], values: dict[str, Any]) -> None:
        """Insert or update by primary key, in one transaction on either dialect."""
        with self.tx() as c:
            cond = sa.and_(*(table.c[k] == v for k, v in key.items()))
            if values and c.execute(table.update().where(cond).values(**values)).rowcount:
                return
            if not values and c.execute(sa.select(sa.literal(1)).select_from(table).where(cond)).first():
                return
            c.execute(table.insert().values(**key, **values))
