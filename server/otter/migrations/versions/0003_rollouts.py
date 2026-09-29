"""rollouts

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-29 12:58:55.536168
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('rollouts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('firmware_id', sa.Integer(), nullable=False),
    sa.Column('tags', sa.String(), nullable=False),
    sa.Column('stages', sa.String(), nullable=False),
    sa.Column('soak_s', sa.Integer(), nullable=False),
    sa.Column('max_failure_rate', sa.Double(), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('current_stage', sa.Integer(), nullable=False),
    sa.Column('stage_done_at', sa.DateTime(), nullable=True),
    sa.Column('message', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['firmware_id'], ['firmwares.id'], name=op.f('fk_rollouts_firmware_id_firmwares'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_rollouts'))
    )
    with op.batch_alter_table('deployments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('rollout_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('stage', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(batch_op.f('fk_deployments_rollout_id_rollouts'), 'rollouts', ['rollout_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    with op.batch_alter_table('deployments', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_deployments_rollout_id_rollouts'), type_='foreignkey')
        batch_op.drop_column('stage')
        batch_op.drop_column('rollout_id')

    op.drop_table('rollouts')
