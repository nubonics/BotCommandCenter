"""add account_expense table

Revision ID: 7c9c3c1bd4a2
Revises: 6f2b4a0fd0d1
Create Date: 2026-04-05

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "7c9c3c1bd4a2"
down_revision: Union[str, Sequence[str], None] = "6f2b4a0fd0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "account_expense",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("account.id"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("amount_usd", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("kind", sa.String(length=20), nullable=False, server_default=sa.text("'one_time'")),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_account_expense_account_created", "account_expense", ["account_id", "created_at"], unique=False)
    op.create_index("ix_account_expense_account_active", "account_expense", ["account_id", "is_active"], unique=False)



def downgrade() -> None:
    op.drop_index("ix_account_expense_account_active", table_name="account_expense")
    op.drop_index("ix_account_expense_account_created", table_name="account_expense")
    op.drop_table("account_expense")
