"""crash reports

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-30 02:11:49.213329
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0014'
down_revision: str | None = '0013'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('crashes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('device_id', sa.Integer(), nullable=False),
    sa.Column('firmware_id', sa.Integer(), nullable=True),
    sa.Column('elf_sha256', sa.String(), nullable=False),
    sa.Column('fw_version', sa.String(), nullable=True),
    sa.Column('task', sa.String(), nullable=True),
    sa.Column('reason', sa.String(), nullable=True),
    sa.Column('report', sa.String(), nullable=False),
    sa.Column('frames', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['device_id'], ['devices.id'], name=op.f('fk_crashes_device_id_devices'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['firmware_id'], ['firmwares.id'], name=op.f('fk_crashes_firmware_id_firmwares'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_crashes'))
    )
    with op.batch_alter_table('crashes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_crashes_device_id'), ['device_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_crashes_firmware_id'), ['firmware_id'], unique=False)

    with op.batch_alter_table('firmwares', schema=None) as batch_op:
        batch_op.add_column(sa.Column('elf_sha256', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('has_elf', sa.Boolean(), server_default='0', nullable=False))
        batch_op.create_index(batch_op.f('ix_firmwares_elf_sha256'), ['elf_sha256'], unique=False)

    # Images already there: read the hash of their ELF file from their app description.
    from otter import storage

    bind = op.get_bind()
    for firmware_id, sha256 in bind.execute(sa.text("SELECT id, sha256 FROM firmwares")).all():
        path = storage.firmware_path(sha256)
        if path.exists() and (elf_sha256 := storage.image_elf_sha256(path)):
            bind.execute(
                sa.text("UPDATE firmwares SET elf_sha256 = :elf WHERE id = :id"), {"elf": elf_sha256, "id": firmware_id}
            )


def downgrade() -> None:
    with op.batch_alter_table('firmwares', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_firmwares_elf_sha256'))
        batch_op.drop_column('has_elf')
        batch_op.drop_column('elf_sha256')

    with op.batch_alter_table('crashes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_crashes_firmware_id'))
        batch_op.drop_index(batch_op.f('ix_crashes_device_id'))

    op.drop_table('crashes')
