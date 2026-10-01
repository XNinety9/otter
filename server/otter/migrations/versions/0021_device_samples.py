"""device samples

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-01 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0021'
down_revision: str | None = '0020'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'device_samples',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('device_id', sa.Integer(), nullable=False),
        sa.Column('at', sa.DateTime(), nullable=False),
        sa.Column('rssi', sa.Integer(), nullable=True),
        sa.Column('free_heap', sa.Integer(), nullable=True),
        sa.Column('restart', sa.String(), nullable=True),
        sa.ForeignKeyConstraint(['device_id'], ['devices.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_device_samples_at', 'device_samples', ['at'])
    op.create_index('ix_device_samples_device_at', 'device_samples', ['device_id', 'at'])


def downgrade() -> None:
    op.drop_index('ix_device_samples_device_at', table_name='device_samples')
    op.drop_index('ix_device_samples_at', table_name='device_samples')
    op.drop_table('device_samples')
