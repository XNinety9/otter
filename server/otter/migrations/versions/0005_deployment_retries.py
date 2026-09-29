"""deployment retries

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-29 15:04:07.409872
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('deployments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('attempts', sa.Integer(), server_default='1', nullable=False))
        batch_op.add_column(sa.Column('retry_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('deployments', schema=None) as batch_op:
        batch_op.drop_column('retry_at')
        batch_op.drop_column('attempts')

