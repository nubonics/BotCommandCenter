"""add global expense target tag

Revision ID: f3d20e9b6d41
Revises: 10c4b1b8d3aa
Create Date: 2026-04-12

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f3d20e9b6d41"
down_revision: Union[str, Sequence[str], None] = "10c4b1b8d3aa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("account_expense", sa.Column("allocation_tag", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("account_expense", "allocation_tag")
