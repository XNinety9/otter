"""Applies database migrations at startup."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, inspect, text

from .db import engine

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Schema of databases created before migrations existed (with metadata.create_all).
BASELINE_REVISION = "0001"
# Tables of that schema, parents before children.
BASELINE_TABLES = ("devices", "firmwares", "deployments")


def alembic_config(connection: Connection | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["connection"] = connection
    return cfg


def upgrade_database() -> None:
    with engine.begin() as connection:
        cfg = alembic_config(connection)
        tables = set(inspect(connection).get_table_names())
        if "devices" in tables and "alembic_version" not in tables:
            adopt_pre_migration_database(connection, cfg)
        command.upgrade(cfg, "head")


def adopt_pre_migration_database(connection: Connection, cfg: Config) -> None:
    """Rebuilds tables created before migrations existed as the baseline revision.

    Their columns already match, but their constraints are unnamed, which later
    migrations can't alter. Runs inside the caller's transaction: all or nothing.
    """
    for table in BASELINE_TABLES:
        connection.execute(text(f"ALTER TABLE {table} RENAME TO {table}_legacy"))
    command.upgrade(cfg, BASELINE_REVISION)
    for table in BASELINE_TABLES:
        columns = ", ".join(c["name"] for c in inspect(connection).get_columns(table))
        connection.execute(text(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM {table}_legacy"))
    for table in reversed(BASELINE_TABLES):
        connection.execute(text(f"DROP TABLE {table}_legacy"))
