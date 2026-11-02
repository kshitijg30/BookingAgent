"""Versioned schema upgrades for the optional SQLAlchemy persistence adapter."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

SCHEMA_VERSION = 1
versions = sa.Table(
    "schema_versions",
    sa.MetaData(),
    sa.Column("component", sa.Text, primary_key=True),
    sa.Column("version", sa.Integer, nullable=False),
)


@dataclass(frozen=True)
class Migration:
    version: int
    upgrade: Callable[[Connection], None]


def initialize_schema(
    engine: Engine, metadata: sa.MetaData, *, schema: str, migrations: tuple[Migration, ...] = ()
) -> None:
    """Create final fresh schemas; upgrade existing schemas before final DDL validation."""
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            key = int.from_bytes(hashlib.sha256(f"booking-agent-schema:{schema}".encode()).digest()[:8], signed=True)
            connection.execute(sa.text("select pg_advisory_xact_lock(:key)"), {"key": key})
        elif connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        target = max([SCHEMA_VERSION, *(migration.version for migration in migrations)])
        table_names = sa.inspect(connection).get_table_names(
            schema=schema if connection.dialect.name == "postgresql" else None
        )
        fresh = not set(table_names).difference({"schema_versions"})
        versions.create(connection, checkfirst=True)
        current = connection.execute(
            sa.select(versions.c.version).where(versions.c.component == "storage")
        ).scalar_one_or_none()
        if current is not None and current > target:
            raise RuntimeError(f"Database schema {current} is newer than supported {target}")
        if fresh:
            metadata.create_all(connection)
            if current is None:
                connection.execute(versions.insert().values(component="storage", version=target))
            else:
                connection.execute(versions.update().where(versions.c.component == "storage").values(version=target))
            return
        if current is None:
            current = SCHEMA_VERSION
            connection.execute(versions.insert().values(component="storage", version=current))
        for migration in sorted(migrations, key=lambda item: item.version):
            if migration.version > current:
                if migration.version != current + 1:
                    raise RuntimeError("Schema migration versions must be contiguous")
                migration.upgrade(connection)
                current = migration.version
                connection.execute(versions.update().where(versions.c.component == "storage").values(version=current))
        # Only after upgrades: safe for documented legacy databases with missing baseline tables.
        metadata.create_all(connection)
