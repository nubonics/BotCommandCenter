"""add account wall hint

Revision ID: c1d7e5d74884
Revises: 5e96f5fc7b7b
Create Date: 2026-04-12

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c1d7e5d74884"
down_revision: Union[str, Sequence[str], None] = "5e96f5fc7b7b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("account", sa.Column("wall_hint", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("account", "wall_hint")
