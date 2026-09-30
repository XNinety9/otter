"""compressed images

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-30 02:33:20.035307
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0015'
down_revision: str | None = '0014'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('firmwares', schema=None) as batch_op:
        batch_op.add_column(sa.Column('compressed_size', sa.Integer(), nullable=True))

    # Images already there get their compressed copy too.
    from otter import storage

    bind = op.get_bind()
    for firmware_id, sha256, size in bind.execute(sa.text("SELECT id, sha256, size FROM firmwares")).all():
        if storage.firmware_path(sha256).exists() and (packed := storage.compress_firmware(sha256, size)):
            bind.execute(
                sa.text("UPDATE firmwares SET compressed_size = :packed WHERE id = :id"), {"packed": packed, "id": firmware_id}
            )


def downgrade() -> None:
    with op.batch_alter_table('firmwares', schema=None) as batch_op:
        batch_op.drop_column('compressed_size')

