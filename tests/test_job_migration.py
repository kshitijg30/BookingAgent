"""Schema version 3 replaces the tables of the earlier design and creates the new ones."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa

from booking_agent.store import Store

OLD_TABLES = ("sessions", "messages", "approvals", "user_memory")
NEW_TABLES = {
    "conversations",
    "searches",
    "quotes",
    "bookings",
    "jobs",
    "user_profiles",
    "app_settings",
}
NOW = datetime(2026, 1, 1, tzinfo=UTC)

VERSION_2_DDL = (
    "create table schema_versions (component text primary key, version integer not null)",
    "insert into schema_versions values ('storage', 2)",
    "create table sessions (session_id text primary key, user_id text not null, state text, data_store text,"
    " version integer, quarantined boolean, title text, updated_at datetime)",
    "create table messages (id integer primary key, session_id text, role text, client_msg_id text, content text,"
    " created_at datetime)",
    "create table approvals (id text primary key, user_id text, state text)",
    "create table bookings (booking_id text primary key, user_id text, session_id text, provider_id text,"
    " slot_start datetime, status text, request text, created_at datetime)",
    "create unique index bookings_one_active_slot on bookings (user_id, provider_id, slot_start)"
    " where status = 'active'",
    "create table jobs (id text primary key, kind text, dedupe_key text unique, payload text, status text,"
    " attempts integer, max_attempts integer, run_at datetime, locked_until datetime, last_error text,"
    " created_at datetime, updated_at datetime)",
    "create table app_settings (key text primary key, value text not null, updated_at datetime not null)",
    "insert into sessions values ('s1', 'u1', '{}', '{}', 1, 0, 'demo', '2026-01-01 00:00:00')",
    "insert into bookings values ('b1', 'u1', 's1', 'p1', '2026-01-02 00:00:00', 'active', '{}', '2026-01-01 00:00:00')",
    "insert into jobs values ('j1', 'booking.write_requested', 'k1', '{}', 'pending', 0, 5, '2026-01-01 00:00:00',"
    " null, null, '2026-01-01 00:00:00', '2026-01-01 00:00:00')",
    "insert into app_settings values ('runtime', '{\"keep\": true}', '2026-01-01 00:00:00')",
)


def stored_version(engine: sa.Engine) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(sa.text("select version from schema_versions where component='storage'")).scalar_one()
        )


def column_names(engine: sa.Engine, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(engine).get_columns(table)}


def make_version_2_database(path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        for statement in VERSION_2_DDL:
            connection.exec_driver_sql(statement)
    engine.dispose()


def test_opening_a_version_2_database_drops_the_old_tables_and_creates_the_new_ones(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    make_version_2_database(path)

    store = Store(path)
    try:
        engine = store.db.engine
        tables = set(sa.inspect(engine).get_table_names())
        assert not tables.intersection(OLD_TABLES)
        assert tables >= NEW_TABLES
        assert stored_version(engine) == 3
        assert {"quote_id", "payload"} <= column_names(engine, "jobs")
        assert "dedupe_key" not in column_names(engine, "jobs")
        assert {"start", "end", "provider_booking_id", "status"} <= column_names(engine, "bookings")
        assert "slot_start" not in column_names(engine, "bookings")
        assert "memory" in column_names(engine, "user_profiles")
        assert {"summary", "summarized_count"} <= column_names(engine, "conversations")
        assert store.recent_jobs() == []
        assert store.booked_all() == []
        assert store.get_setting("runtime") == {"keep": True}
    finally:
        store.close()


def test_the_migrated_database_works_and_opens_again_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    make_version_2_database(path)
    first = Store(path)
    first.put_setting("after", {"ok": 1}, NOW)
    first.close()

    second = Store(path)
    try:
        assert stored_version(second.db.engine) == 3
        assert second.get_setting("after") == {"ok": 1}
        assert second.counts()["conversations"] == 0
    finally:
        second.close()


def test_a_fresh_database_gets_the_new_tables_at_version_3(tmp_path: Path) -> None:
    store = Store(tmp_path / "fresh.sqlite3")
    try:
        tables = set(sa.inspect(store.db.engine).get_table_names())
        assert tables >= NEW_TABLES
        assert not tables.intersection(OLD_TABLES)
        assert stored_version(store.db.engine) == 3
    finally:
        store.close()


def test_the_indexes_exist(tmp_path: Path) -> None:
    store = Store(tmp_path / "fresh.sqlite3")
    try:
        inspector = sa.inspect(store.db.engine)

        def indexed(table: str) -> set[tuple[str, ...]]:
            return {tuple(str(name) for name in index["column_names"]) for index in inspector.get_indexes(table)}

        assert {("conversation_id", "created_at"), ("user_id", "status")} <= indexed("quotes")
        assert {("user_id", "start"), ("user_id", "provider_id", "start")} <= indexed("bookings")
        assert ("conversation_id", "created_at") in indexed("searches")
        assert {("status", "run_at"), ("quote_id",)} <= indexed("jobs")
        unique = [index for index in inspector.get_indexes("bookings") if index["unique"]]
        assert [index["column_names"] for index in unique] == [["user_id", "provider_id", "start"]]
    finally:
        store.close()
