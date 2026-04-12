from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Numeric,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Item(Base):
    __tablename__ = "item"

    id: Mapped[int] = mapped_column(primary_key=True)
    osrs_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)

    high_alch_value: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ge_buy_price: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ge_sell_price: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    components: Mapped[list["MoneyMakerComponent"]] = relationship(
        back_populates="item",
        cascade="all, delete-orphan",
    )


class MoneyMaker(Base):
    __tablename__ = "money_maker"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    category: Mapped[str] = mapped_column(String(100), default="processing")
    is_members: Mapped[bool] = mapped_column(default=True)
    units_per_hour: Mapped[int] = mapped_column(Integer, default=1)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    components: Mapped[list["MoneyMakerComponent"]] = relationship(
        back_populates="money_maker",
        cascade="all, delete-orphan",
    )


class MoneyMakerComponent(Base):
    __tablename__ = "money_maker_component"

    id: Mapped[int] = mapped_column(primary_key=True)

    money_maker_id: Mapped[int] = mapped_column(ForeignKey("money_maker.id"))
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id"))

    role: Mapped[str] = mapped_column(String(20))
    quantity_per_hour: Mapped[int] = mapped_column(Integer, default=1)
    valuation_mode: Mapped[str] = mapped_column(String(20), default="market")
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    money_maker: Mapped["MoneyMaker"] = relationship(back_populates="components")
    item: Mapped["Item"] = relationship(back_populates="components")


class Account(Base):
    __tablename__ = "account"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(255), index=True)
    email_address: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    email_password: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    rs_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    rs_password: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    rsn: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    proxy_ip: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    proxy_port: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    proxy_username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    proxy_password: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    botting_hub_config_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, unique=True, index=True)
    botting_hub_account_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    botting_hub_proxy_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    world: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    tags: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    banned: Mapped[bool] = mapped_column(default=False, server_default="0")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    plan_assignments: Mapped[list["AccountPlanAssignment"]] = relationship(
        back_populates="account",
        cascade="all, delete-orphan",
    )

    # NEW RELATIONSHIPS
    progress: Mapped[Optional["AccountProgress"]] = relationship(
        back_populates="account",
        cascade="all, delete-orphan",
        uselist=False,
    )

    goals: Mapped[list["AccountGoal"]] = relationship(
        back_populates="account",
        cascade="all, delete-orphan",
    )

    expenses: Mapped[list["AccountExpense"]] = relationship(
        back_populates="account",
        cascade="all, delete-orphan",
    )


class AccountExpense(Base):
    __tablename__ = "account_expense"
    __table_args__ = (
        Index("ix_account_expense_account_created", "account_id", "created_at"),
        Index("ix_account_expense_account_active", "account_id", "is_active"),
        Index("ix_account_expense_allocation_group", "allocation_group"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"), index=True)

    name: Mapped[str] = mapped_column(String(255))
    # Always store as USD for now.
    amount_usd: Mapped[float] = mapped_column(Numeric(10, 2), default=0)

    # one_time or monthly
    kind: Mapped[str] = mapped_column(String(20), default="one_time")
    # account or global
    allocation_scope: Mapped[str] = mapped_column(String(20), default="account", server_default="account")
    allocation_group: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    allocation_tag: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    source_amount_usd: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    allocated_account_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    start_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    account: Mapped["Account"] = relationship(back_populates="expenses")


class AccountProgress(Base):
    __tablename__ = "account_progress"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"), unique=True, index=True)

    skills_xp_json: Mapped[str] = mapped_column(Text, default="{}")
    gp: Mapped[int] = mapped_column(Integer, default=0)
    unlocks_json: Mapped[str] = mapped_column(Text, default="[]")
    completed_quests_json: Mapped[str] = mapped_column(Text, default="[]")
    quest_points: Mapped[int] = mapped_column(Integer, default=0)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    account: Mapped["Account"] = relationship(back_populates="progress")


class AccountGoal(Base):
    __tablename__ = "account_goal"
    __table_args__ = (
        Index("ix_account_goal_account_active", "account_id", "is_active"),
        Index("ix_account_goal_account_created", "account_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"), index=True)

    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True)

    baseline_skills_xp_json: Mapped[str] = mapped_column(Text, default="{}")
    baseline_gp: Mapped[int] = mapped_column(Integer, default=0)

    # Optional quest targets (manual): list of quest/miniquest names.
    # These names should match the wiki dataset names used in completed_quests_json.
    target_quests_json: Mapped[str] = mapped_column(Text, default="[]")

    target_skills_xp_json: Mapped[str] = mapped_column(Text, default="{}")
    target_gp: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    planner_weights_json: Mapped[str] = mapped_column(Text, default="{}")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    account: Mapped["Account"] = relationship(back_populates="goals")


class PlannerTask(Base):
    __tablename__ = "planner_task"
    __table_args__ = (
        Index("ix_planner_task_enabled_category", "enabled", "category"),
        Index("ix_planner_task_enabled_members", "enabled", "members_only"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    category: Mapped[str] = mapped_column(String(100), default="general")
    members_only: Mapped[bool] = mapped_column(default=False)
    enabled: Mapped[bool] = mapped_column(default=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    plan_steps: Mapped[list["PlanStep"]] = relationship(back_populates="planner_task")


class PlanTemplate(Base):
    __tablename__ = "plan_template"
    __table_args__ = (
        Index("ix_plan_template_generated", "is_generated"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_generated: Mapped[bool] = mapped_column(default=False)
    target_active_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    steps: Mapped[list["PlanStep"]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="PlanStep.step_order",
    )

    assignments: Mapped[list["AccountPlanAssignment"]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
    )


class PlanStep(Base):
    __tablename__ = "plan_step"
    __table_args__ = (
        Index("ix_plan_step_plan_order", "plan_id", "step_order"),
        Index("ix_plan_step_planner_task_id", "planner_task_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plan_template.id"))
    step_order: Mapped[int] = mapped_column(Integer)
    step_type: Mapped[str] = mapped_column(String(20))
    planner_task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("planner_task.id"),
        nullable=True,
    )
    duration_minutes: Mapped[int] = mapped_column(Integer)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    plan: Mapped["PlanTemplate"] = relationship(back_populates="steps")
    planner_task: Mapped[Optional["PlannerTask"]] = relationship(back_populates="plan_steps")


class AccountPlanAssignment(Base):
    __tablename__ = "account_plan_assignment"
    __table_args__ = (
        Index("ix_account_plan_assignment_account_active", "account_id", "is_active"),
        Index("ix_account_plan_assignment_account_date", "account_id", "assigned_for_date"),
        Index("ix_account_plan_assignment_plan_date", "plan_id", "assigned_for_date"),
        Index("ix_account_plan_assignment_date_active", "assigned_for_date", "is_active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"))
    plan_id: Mapped[int] = mapped_column(ForeignKey("plan_template.id"))
    assigned_for_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    planned_start_time: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    account: Mapped["Account"] = relationship(back_populates="plan_assignments")
    plan: Mapped["PlanTemplate"] = relationship(back_populates="assignments")
