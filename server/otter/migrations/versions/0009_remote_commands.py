"""remote commands

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-29 23:17:37.182281
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0009'
down_revision: str | None = '0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('commands',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('device_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('args', sa.String(), nullable=True),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('result', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('sent_at', sa.DateTime(), nullable=True),
    sa.Column('done_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['device_id'], ['devices.id'], name=op.f('fk_commands_device_id_devices'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_commands'))
    )


def downgrade() -> None:
    op.drop_table('commands')
