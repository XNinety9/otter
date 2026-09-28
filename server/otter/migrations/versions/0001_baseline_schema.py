"""baseline schema

Revision ID: 0001
Revises:
Create Date: 2026-09-28 21:31:12.618184
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('devices',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('mac', sa.String(length=17), nullable=False),
    sa.Column('name', sa.String(), nullable=True),
    sa.Column('hw', sa.String(), nullable=False),
    sa.Column('app', sa.String(), nullable=False),
    sa.Column('fw_version', sa.String(), nullable=False),
    sa.Column('ip', sa.String(), nullable=True),
    sa.Column('rssi', sa.Integer(), nullable=True),
    sa.Column('uptime_s', sa.Integer(), nullable=True),
    sa.Column('first_seen', sa.DateTime(), nullable=False),
    sa.Column('last_seen', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_devices')),
    sa.UniqueConstraint('mac', name=op.f('uq_devices_mac'))
    )
    op.create_table('firmwares',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('app', sa.String(), nullable=False),
    sa.Column('hw', sa.String(), nullable=False),
    sa.Column('version', sa.String(), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('notes', sa.String(), nullable=True),
    sa.Column('uploaded_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_firmwares')),
    sa.UniqueConstraint('app', 'hw', 'version', name=op.f('uq_firmwares_app_hw_version'))
    )
    op.create_table('deployments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('device_id', sa.Integer(), nullable=False),
    sa.Column('firmware_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('progress', sa.Integer(), nullable=False),
    sa.Column('error', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['device_id'], ['devices.id'], name=op.f('fk_deployments_device_id_devices'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['firmware_id'], ['firmwares.id'], name=op.f('fk_deployments_firmware_id_firmwares'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_deployments'))
    )


def downgrade() -> None:
    op.drop_table('deployments')
    op.drop_table('firmwares')
    op.drop_table('devices')
