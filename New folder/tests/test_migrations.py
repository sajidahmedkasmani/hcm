"""Finding 3: schema is managed by Alembic migrations, not create_all."""

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

from hcm_lite import models  # noqa: F401
from hcm_lite.config import Settings
from hcm_lite.db import MIGRATIONS_DIR, Base, make_engine, run_migrations


def _cfg(conn):
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["connection"] = conn
    return cfg


def test_migrations_match_models(tmp_path):
    """Drift guard: after `upgrade head` the database equals the ORM models exactly."""
    engine = make_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    run_migrations(engine)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert diff == [], f"Models and migrations differ — run `alembic revision --autogenerate`: {diff}"
    assert version == "0002"


def test_v1_database_created_with_create_all_is_adopted_and_upgraded(tmp_path):
    """A v1.0 deployment (tables from create_all, no alembic_version) keeps its data and gains new tables."""
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    engine = make_engine(url)
    with engine.begin() as conn:
        command.upgrade(_cfg(conn), "0001")          # exactly the v1.0 schema
        conn.execute(text("DROP TABLE alembic_version"))  # ...as create_all would have left it
        conn.execute(text("INSERT INTO company (name, office_start, grace_minutes, half_day_hours, weekend_days, "
                          "office_radius_m, late_penalty_enabled, created_at) VALUES "
                          "('Legacy Co', '09:00', 15, 5, '5,6', 150, false, '2026-09-01 00:00:00')"))
    assert "login_attempts" not in inspect(engine).get_table_names()

    from hcm_lite.main import create_app

    create_app(Settings(database_url=url, jwt_secret="x" * 40, bcrypt_rounds=4))  # startup migrates
    tables = set(inspect(engine).get_table_names())
    assert {"login_attempts", "revoked_tokens", "alembic_version"} <= tables
    with engine.connect() as conn:
        assert conn.execute(text("SELECT name FROM company")).scalar() == "Legacy Co"
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0002"


def test_latest_migration_is_reversible(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'down.db'}")
    run_migrations(engine)
    with engine.begin() as conn:
        command.downgrade(_cfg(conn), "0001")
    assert "revoked_tokens" not in inspect(engine).get_table_names()
    run_migrations(engine)
    assert "revoked_tokens" in inspect(engine).get_table_names()


def test_auto_migrate_disabled_refuses_an_unmigrated_database(tmp_path):
    import pytest

    from hcm_lite.main import create_app

    url = f"sqlite:///{tmp_path / 'manual.db'}"
    settings = Settings(database_url=url, jwt_secret="x" * 40, auto_migrate=False)
    with pytest.raises(RuntimeError, match="alembic upgrade head"):
        create_app(settings)
    run_migrations(make_engine(url))   # what the operator's pre-deploy step does
    assert create_app(settings) is not None
