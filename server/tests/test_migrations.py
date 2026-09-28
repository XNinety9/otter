import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from conftest import reset_database
from otter.db import engine
from otter.migrate import alembic_config, upgrade_database


def current_revision() -> str:
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def head_revision() -> str:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


@pytest.fixture(autouse=True)
def empty_database():
    reset_database()


def test_fresh_database_gets_all_tables():
    upgrade_database()
    tables = set(inspect(engine).get_table_names())
    assert {"devices", "firmwares", "deployments", "alembic_version"} <= tables


def test_migrations_match_models():
    # Fails when a model changed without a migration: run `alembic revision --autogenerate`.
    upgrade_database()
    with engine.begin() as conn:
        command.check(alembic_config(conn))


def test_upgrade_is_idempotent():
    upgrade_database()
    upgrade_database()
    with engine.begin() as conn:
        command.check(alembic_config(conn))


# Schema written by metadata.create_all() before Alembic was introduced (unnamed constraints).
PRE_MIGRATION_SCHEMA = [
    """CREATE TABLE devices (
        id INTEGER NOT NULL, mac VARCHAR(17) NOT NULL, name VARCHAR, hw VARCHAR NOT NULL,
        app VARCHAR NOT NULL, fw_version VARCHAR NOT NULL, ip VARCHAR, rssi INTEGER,
        uptime_s INTEGER, first_seen DATETIME NOT NULL, last_seen DATETIME NOT NULL,
        PRIMARY KEY (id), UNIQUE (mac))""",
    """CREATE TABLE firmwares (
        id INTEGER NOT NULL, app VARCHAR NOT NULL, hw VARCHAR NOT NULL, version VARCHAR NOT NULL,
        size INTEGER NOT NULL, sha256 VARCHAR(64) NOT NULL, notes VARCHAR,
        uploaded_at DATETIME NOT NULL, PRIMARY KEY (id), UNIQUE (app, hw, version))""",
    """CREATE TABLE deployments (
        id INTEGER NOT NULL, device_id INTEGER NOT NULL, firmware_id INTEGER NOT NULL,
        status VARCHAR NOT NULL, progress INTEGER NOT NULL, error VARCHAR,
        created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, PRIMARY KEY (id),
        FOREIGN KEY(device_id) REFERENCES devices (id) ON DELETE CASCADE,
        FOREIGN KEY(firmware_id) REFERENCES firmwares (id) ON DELETE CASCADE)""",
    "INSERT INTO devices VALUES (7, 'aa:bb:cc:00:00:01', 'desk', 'esp32', 'weather', '1.1.0', "
    "'192.168.1.20', -60, 42, '2026-01-01 10:00:00', '2026-01-02 10:00:00')",
    "INSERT INTO firmwares VALUES (3, 'weather', 'esp32', '1.1.0', 1024, "
    + "'" + "ab" * 32 + "'" + ", 'notes', '2026-01-01 09:00:00')",
    "INSERT INTO deployments VALUES (5, 7, 3, 'success', 100, NULL, "
    "'2026-01-01 11:00:00', '2026-01-01 11:01:00')",
]


def test_pre_migration_database_is_adopted_without_data_loss():
    with engine.begin() as conn:
        for statement in PRE_MIGRATION_SCHEMA:
            conn.execute(text(statement))

    upgrade_database()

    assert current_revision() == head_revision()
    assert not {t for t in inspect(engine).get_table_names() if t.endswith("_legacy")}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT id, name, fw_version FROM devices")).one() == (7, "desk", "1.1.0")
        assert conn.execute(text("SELECT id, version FROM firmwares")).one() == (3, "1.1.0")
        assert conn.execute(text("SELECT id, device_id, firmware_id, status FROM deployments")).one() == (
            5, 7, 3, "success",
        )
    # Constraints now carry the baseline's names: nothing left for autogenerate to fix.
    with engine.begin() as conn:
        command.check(alembic_config(conn))
    unique = inspect(engine).get_unique_constraints("devices")
    assert [u["name"] for u in unique] == ["uq_devices_mac"]
