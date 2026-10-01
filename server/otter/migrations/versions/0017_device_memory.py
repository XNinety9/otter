"""device memory sizes

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-01 16:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0017'
down_revision: str | None = '0016'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.add_column(sa.Column('flash_size', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('psram_size', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.drop_column('psram_size')
        batch_op.drop_column('flash_size')
