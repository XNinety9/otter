"""users sessions and api tokens

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-29 13:18:17.973951
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('username', sa.String(length=64), nullable=False),
    sa.Column('password_hash', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('username', name=op.f('uq_users_username'))
    )
    op.create_table('api_tokens',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('last_used_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_api_tokens_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_api_tokens')),
    sa.UniqueConstraint('name', name=op.f('uq_api_tokens_name')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_api_tokens_token_hash'))
    )
    op.create_table('auth_sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_auth_sessions_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_auth_sessions')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_auth_sessions_token_hash'))
    )


def downgrade() -> None:
    op.drop_table('auth_sessions')
    op.drop_table('api_tokens')
    op.drop_table('users')
