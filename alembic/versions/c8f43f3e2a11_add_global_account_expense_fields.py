"""add global account_expense allocation fields

Revision ID: c8f43f3e2a11
Revises: 7c9c3c1bd4a2
Create Date: 2026-04-11

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c8f43f3e2a11"
down_revision: Union[str, Sequence[str], None] = "7c9c3c1bd4a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "account_expense",
        sa.Column("allocation_scope", sa.String(length=20), nullable=False, server_default=sa.text("'account'")),
    )
    op.add_column(
        "account_expense",
        sa.Column("allocation_group", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "account_expense",
        sa.Column("source_amount_usd", sa.Numeric(10, 2), nullable=True),
    )
    op.add_column(
        "account_expense",
        sa.Column("allocated_account_count", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_account_expense_allocation_group",
        "account_expense",
        ["allocation_group"],
        unique=False,
    )

    op.execute(
        "UPDATE account_expense "
        "SET allocation_scope = 'account', "
        "source_amount_usd = amount_usd, "
        "allocated_account_count = 1 "
        "WHERE allocation_scope IS NULL OR source_amount_usd IS NULL OR allocated_account_count IS NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_account_expense_allocation_group", table_name="account_expense")
    op.drop_column("account_expense", "allocated_account_count")
    op.drop_column("account_expense", "source_amount_usd")
    op.drop_column("account_expense", "allocation_group")
    op.drop_column("account_expense", "allocation_scope")
