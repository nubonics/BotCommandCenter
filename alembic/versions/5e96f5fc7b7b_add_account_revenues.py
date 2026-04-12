"""add account revenues

Revision ID: 5e96f5fc7b7b
Revises: f3d20e9b6d41
Create Date: 2026-04-12

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "5e96f5fc7b7b"
down_revision: Union[str, Sequence[str], None] = "f3d20e9b6d41"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "account_revenue",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("amount_usd", sa.Numeric(10, 2), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="one_time"),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_account_revenue_account_created", "account_revenue", ["account_id", "created_at"], unique=False)
    op.create_index("ix_account_revenue_account_active", "account_revenue", ["account_id", "is_active"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_account_revenue_account_active", table_name="account_revenue")
    op.drop_index("ix_account_revenue_account_created", table_name="account_revenue")
    op.drop_table("account_revenue")
