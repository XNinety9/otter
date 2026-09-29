"""release channels

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-29 15:10:21.702892
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.add_column(sa.Column('channel', sa.String(), nullable=True))

    with op.batch_alter_table('firmwares', schema=None) as batch_op:
        batch_op.add_column(sa.Column('channel', sa.String(), nullable=True))

    with op.batch_alter_table('rollouts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('channel', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('rollouts', schema=None) as batch_op:
        batch_op.drop_column('channel')

    with op.batch_alter_table('firmwares', schema=None) as batch_op:
        batch_op.drop_column('channel')

    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.drop_column('channel')

