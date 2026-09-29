"""device health

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-29 22:50:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0008'
down_revision: str | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.add_column(sa.Column('free_heap', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('min_free_heap', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('reset_reason', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('boot_count', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.drop_column('boot_count')
        batch_op.drop_column('reset_reason')
        batch_op.drop_column('min_free_heap')
        batch_op.drop_column('free_heap')
