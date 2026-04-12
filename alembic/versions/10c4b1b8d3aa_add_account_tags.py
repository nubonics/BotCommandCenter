"""add account tags

Revision ID: 10c4b1b8d3aa
Revises: c8f43f3e2a11
Create Date: 2026-04-12

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "10c4b1b8d3aa"
down_revision: Union[str, Sequence[str], None] = "c8f43f3e2a11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("account", sa.Column("tags", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("account", "tags")
