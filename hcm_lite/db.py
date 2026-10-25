"""Database engine, session factory and custom column types (SQLAlchemy 2.x).

Any SQL database works: SQLite for local/dev, PostgreSQL/MySQL in production via DATABASE_URL.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import BigInteger, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

CENT = Decimal("0.01")
MIGRATIONS_DIR = Path(__file__).parent / "migrations"
BASELINE_REVISION = "0001"  # schema of v1.0, which created tables with create_all


def to_money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


class Money(TypeDecorator):
    """Stores money as integer minor units (paisa) and exposes `Decimal` in Python.

    Avoids float rounding and SQLite's lack of a native DECIMAL type.
    """

    impl = BigInteger
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> int | None:
        if value is None:
            return None
        return int((to_money(value) * 100).to_integral_value(rounding=ROUND_HALF_UP))

    def process_result_value(self, value: Any, dialect: Any) -> Decimal | None:
        if value is None:
            return None
        return (Decimal(value) / 100).quantize(CENT)


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str) -> Engine:
    kwargs: dict[str, Any] = {"future": True}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if database_url in {"sqlite://", "sqlite:///:memory:"}:
            kwargs["poolclass"] = StaticPool
    engine = create_engine(database_url, **kwargs)
    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver hook
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def run_migrations(engine: Engine, target: str = "head") -> None:
    """Apply Alembic migrations on one connection (works for SQLite in-memory and PostgreSQL).

    A database created by v1.0 (tables but no `alembic_version`) is adopted by stamping the
    baseline revision first, so later revisions upgrade it instead of failing on existing tables.
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        tables = set(inspect(conn).get_table_names())
        if "users" in tables and "alembic_version" not in tables:
            command.stamp(cfg, BASELINE_REVISION)
        command.upgrade(cfg, target)


def schema_revision(engine: Engine) -> tuple[str | None, str]:
    """(current database revision, latest revision in the code)."""
    from alembic.config import Config
    from alembic.migration import MigrationContext
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    with engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()
    return current, head


def init_db(engine: Engine, *, auto_migrate: bool = True) -> None:
    """Bring the schema to the latest revision. With AUTO_MIGRATE=false the operator runs
    `alembic upgrade head` (e.g. as a pre-deploy step) and startup refuses an out-of-date schema."""
    from . import models  # noqa: F401  (register mappers)

    if auto_migrate:
        run_migrations(engine)
        return
    current, head = schema_revision(engine)
    if current != head:
        raise RuntimeError(f"Database schema is at revision {current!r} but the code needs {head!r}. "
                           "Run `alembic upgrade head` or set AUTO_MIGRATE=true.")
