"""device tags

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-29 12:46:43.384336
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('tags',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=32), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tags')),
    sa.UniqueConstraint('name', name=op.f('uq_tags_name'))
    )
    op.create_table('device_tags',
    sa.Column('device_id', sa.Integer(), nullable=False),
    sa.Column('tag_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['device_id'], ['devices.id'], name=op.f('fk_device_tags_device_id_devices'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tag_id'], ['tags.id'], name=op.f('fk_device_tags_tag_id_tags'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('device_id', 'tag_id', name=op.f('pk_device_tags'))
    )


def downgrade() -> None:
    op.drop_table('device_tags')
    op.drop_table('tags')
