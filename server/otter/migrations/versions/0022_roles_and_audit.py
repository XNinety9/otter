"""user roles and audit log

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-01 23:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0022'
down_revision: str | None = '0021'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('role', sa.String(length=16), server_default='admin', nullable=False))
    op.create_table(
        'audit_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('at', sa.DateTime(), nullable=False),
        sa.Column('username', sa.String(), nullable=False),
        sa.Column('via_token', sa.Boolean(), nullable=False),
        sa.Column('method', sa.String(length=8), nullable=False),
        sa.Column('path', sa.String(), nullable=False),
        sa.Column('text', sa.String(), nullable=False),
        sa.Column('status', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_audit_events_at', 'audit_events', ['at'])


def downgrade() -> None:
    op.drop_index('ix_audit_events_at', table_name='audit_events')
    op.drop_table('audit_events')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('role')
