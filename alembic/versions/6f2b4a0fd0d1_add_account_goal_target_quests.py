"""add target quests to account_goal

Revision ID: 6f2b4a0fd0d1
Revises: 3b7a1d9c2c19
Create Date: 2026-04-05

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "6f2b4a0fd0d1"
down_revision: Union[str, Sequence[str], None] = "3b7a1d9c2c19"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("account_goal", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "target_quests_json",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("account_goal", schema=None) as batch_op:
        batch_op.drop_column("target_quests_json")
