"""add planner weights to account_goal

Revision ID: 3b7a1d9c2c19
Revises: 7e3a98542cc5
Create Date: 2026-03-15

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "3b7a1d9c2c19"
down_revision: Union[str, Sequence[str], None] = "7e3a98542cc5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("account_goal", schema=None) as batch_op:
        batch_op.add_column(sa.Column("planner_weights_json", sa.Text(), nullable=False, server_default=sa.text("'{}'")))


def downgrade() -> None:
    with op.batch_alter_table("account_goal", schema=None) as batch_op:
        batch_op.drop_column("planner_weights_json")
