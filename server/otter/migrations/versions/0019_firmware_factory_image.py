"""firmware factory image

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-01 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0019'
down_revision: str | None = '0018'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('firmwares', schema=None) as batch_op:
        batch_op.add_column(sa.Column('factory_sha256', sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('firmwares', schema=None) as batch_op:
        batch_op.drop_column('factory_sha256')
