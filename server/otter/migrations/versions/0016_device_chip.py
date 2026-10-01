"""device chip model

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-01 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0016'
down_revision: str | None = '0015'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.add_column(sa.Column('chip', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('chip_rev', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.drop_column('chip_rev')
        batch_op.drop_column('chip')
