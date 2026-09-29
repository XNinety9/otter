"""remote configuration

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-30 00:01:38.437025
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0012'
down_revision: str | None = '0011'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('config_values',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('device_id', sa.Integer(), nullable=True),
    sa.Column('tag', sa.String(length=32), nullable=True),
    sa.Column('key', sa.String(length=32), nullable=False),
    sa.Column('value', sa.String(), nullable=False),
    sa.ForeignKeyConstraint(['device_id'], ['devices.id'], name=op.f('fk_config_values_device_id_devices'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_config_values'))
    )
    with op.batch_alter_table('config_values', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_config_values_device_id'), ['device_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_config_values_tag'), ['tag'], unique=False)

    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.add_column(sa.Column('config_version', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.drop_column('config_version')

    with op.batch_alter_table('config_values', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_config_values_tag'))
        batch_op.drop_index(batch_op.f('ix_config_values_device_id'))

    op.drop_table('config_values')
