from alembic import context

from otter import models  # noqa: F401  (registers the tables on Base.metadata)
from otter.db import Base, engine


def run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        render_as_batch=True,  # SQLite can't ALTER most things: recreate tables instead
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("Offline (SQL script) migrations are not supported.")

# otter.migrate passes its own connection; the alembic CLI uses the app's engine.
connection = context.config.attributes.get("connection")
if connection is not None:
    run_migrations(connection)
else:
    with engine.begin() as conn:
        run_migrations(conn)
