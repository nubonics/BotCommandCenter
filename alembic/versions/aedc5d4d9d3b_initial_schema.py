"""initial schema

Revision ID: aedc5d4d9d3b
Revises: 
Create Date: 2026-03-15 03:08:09.509470

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'aedc5d4d9d3b'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "account",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("email_address", sa.String(length=255)),
        sa.Column("email_password", sa.String(length=255)),
        sa.Column("rs_email", sa.String(length=255)),
        sa.Column("rs_password", sa.String(length=255)),
        sa.Column("proxy_ip", sa.String(length=255)),
        sa.Column("proxy_port", sa.String(length=50)),
        sa.Column("proxy_username", sa.String(length=255)),
        sa.Column("proxy_password", sa.String(length=255)),
        sa.Column("notes", sa.Text()),
        sa.Column("botting_hub_config_id", sa.Integer()),
        sa.Column("botting_hub_account_id", sa.Integer()),
        sa.Column("botting_hub_proxy_id", sa.Integer()),
        sa.Column("world", sa.Integer()),
        sa.Column("status", sa.String(length=50)),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("rsn", sa.String(length=255)),
        sa.Column("banned", sa.Boolean(), server_default=sa.text("'0'"), nullable=False),
    )

    op.create_table(
        "item",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("osrs_id", sa.Integer()),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("high_alch_value", sa.Integer()),
        sa.Column("ge_buy_price", sa.Integer()),
        sa.Column("ge_sell_price", sa.Integer()),
        sa.Column("updated_at", sa.DateTime()),
    )

    op.create_table(
        "money_maker",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column("units_per_hour", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("is_members", sa.Boolean(), server_default=sa.text("1"), nullable=False),
    )

    op.create_table(
        "planner_task",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column("members_only", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )

    op.create_table(
        "plan_template",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("is_generated", sa.Boolean(), nullable=False),
        sa.Column("target_active_minutes", sa.Integer()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )

    op.create_table(
        "account_goal",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("account.id"), nullable=False),
        sa.Column("name", sa.String(length=255)),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("baseline_skills_xp_json", sa.Text(), nullable=False),
        sa.Column("baseline_gp", sa.Integer(), nullable=False),
        sa.Column("target_skills_xp_json", sa.Text(), nullable=False),
        sa.Column("target_gp", sa.Integer()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("planner_weights_json", sa.Text(), server_default=sa.text("'{}'"), nullable=False),
    )

    op.create_table(
        "account_progress",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("account.id"), nullable=False),
        sa.Column("skills_xp_json", sa.Text(), nullable=False),
        sa.Column("gp", sa.Integer(), nullable=False),
        sa.Column("unlocks_json", sa.Text(), nullable=False),
        sa.Column("completed_quests_json", sa.Text(), nullable=False),
        sa.Column("quest_points", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )

    op.create_table(
        "account_plan_assignment",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("account.id"), nullable=False),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("plan_template.id"), nullable=False),
        sa.Column("assigned_for_date", sa.Date()),
        sa.Column("planned_start_time", sa.String(length=20)),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )

    op.create_table(
        "plan_step",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("plan_template.id"), nullable=False),
        sa.Column("step_order", sa.Integer(), nullable=False),
        sa.Column("step_type", sa.String(length=20), nullable=False),
        sa.Column("planner_task_id", sa.Integer(), sa.ForeignKey("planner_task.id")),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text()),
    )

    op.create_table(
        "money_maker_component",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("money_maker_id", sa.Integer(), sa.ForeignKey("money_maker.id"), nullable=False),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("item.id"), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("quantity_per_hour", sa.Integer(), nullable=False),
        sa.Column("valuation_mode", sa.String(length=20), nullable=False),
        sa.Column("notes", sa.Text()),
    )


def downgrade() -> None:
    op.drop_table("money_maker_component")
    op.drop_table("plan_step")
    op.drop_table("account_plan_assignment")
    op.drop_table("account_progress")
    op.drop_table("account_expense")
    op.drop_table("account_goal")
    op.drop_table("plan_template")
    op.drop_table("planner_task")
    op.drop_table("money_maker")
    op.drop_table("item")
    op.drop_table("account")
