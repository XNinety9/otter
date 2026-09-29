"""device tokens

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-29 23:32:14.658847
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0010'
down_revision: str | None = '0009'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.add_column(sa.Column('token_hash', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('token_used_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('revoked_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('approved', sa.Boolean(), server_default='1', nullable=False))
        batch_op.create_index(batch_op.f('ix_devices_token_hash'), ['token_hash'], unique=True)


def downgrade() -> None:
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_devices_token_hash'))
        batch_op.drop_column('approved')
        batch_op.drop_column('revoked_at')
        batch_op.drop_column('token_used_at')
        batch_op.drop_column('token_hash')

