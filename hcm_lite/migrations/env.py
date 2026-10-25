"""Alembic environment.

Two ways in:
* CLI: `alembic upgrade head` (reads DATABASE_URL via Settings).
* Programmatic: `hcm_lite.db.run_migrations(engine)` passes an open connection in
  `config.attributes["connection"]` — used on app startup and in tests.
"""

from __future__ import annotations

from alembic import context

from hcm_lite import models  # noqa: F401  (registers tables on Base.metadata)
from hcm_lite.config import Settings
from hcm_lite.db import Base, make_engine

config = context.config
target_metadata = Base.metadata


def _configure_and_run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,  # lets SQLite emulate ALTER TABLE
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url") or Settings.from_env().database_url
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, render_as_batch=True,
                      dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure_and_run(connection)
        return
    url = config.get_main_option("sqlalchemy.url") or Settings.from_env().database_url
    engine = make_engine(url)
    with engine.begin() as conn:
        _configure_and_run(conn)
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
