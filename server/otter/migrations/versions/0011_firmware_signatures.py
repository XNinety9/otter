"""firmware signatures

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-29 23:46:42.509776
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0011'
down_revision: str | None = '0010'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('firmwares', schema=None) as batch_op:
        batch_op.add_column(sa.Column('signature', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('firmwares', schema=None) as batch_op:
        batch_op.drop_column('signature')

