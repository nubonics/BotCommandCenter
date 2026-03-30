from __future__ import annotations

import random
from datetime import date, datetime
from typing import Iterable

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import get_session
from .models import Account, AccountPlanAssignment, PlanStep, PlanTemplate, PlannerTask

router = APIRouter(tags=["planner_core"])


def get_templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _deactivate_current_assignments(session: Session, account_id: int) -> None:
    assignments = session.scalars(
        select(AccountPlanAssignment).where(
            AccountPlanAssignment.account_id == account_id,
            AccountPlanAssignment.is_active.is_(True),
        )
    ).all()
    for assignment in assignments:
        assignment.is_active = False


def _assign_plan_to_account(
    session: Session,
    account_id: int,
    plan_id: int,
    assigned_for_date: date | None = None,
    planned_start_time: str | None = None,
    notes: str | None = None,
) -> None:
    _deactivate_current_assignments(session, account_id)

    assignment = AccountPlanAssignment(
        account_id=account_id,
        plan_id=plan_id,
        assigned_for_date=assigned_for_date,
        planned_start_time=planned_start_time or None,
        is_active=True,
        notes=notes or None,
    )
    session.add(assignment)


def _generate_random_steps(
    allowed_tasks: list[PlannerTask],
    total_active_minutes: int,
    min_task_minutes: int,
    max_task_minutes: int,
    min_break_minutes: int,
    max_break_minutes: int,
    max_continuous_minutes: int,
    max_repeats_per_task: int,
) -> list[dict]:
    if not allowed_tasks:
        raise ValueError("No allowed tasks selected")

    if min_task_minutes > max_task_minutes:
        raise ValueError("Task min minutes cannot be greater than task max minutes")

    if min_break_minutes > max_break_minutes:
        raise ValueError("Break min minutes cannot be greater than break max minutes")

    if total_active_minutes <= 0:
        raise ValueError("Total active minutes must be greater than 0")

    steps: list[dict] = []
    active_minutes_used = 0
    continuous_minutes = 0
    last_task_id: int | None = None
    repeat_count = 0

    while active_minutes_used < total_active_minutes:
        remaining_active = total_active_minutes - active_minutes_used

        if steps and continuous_minutes >= max_continuous_minutes:
            break_duration = random.randint(min_break_minutes, max_break_minutes)
            steps.append(
                {
                    "step_type": "break",
                    "planner_task_id": None,
                    "duration_minutes": break_duration,
                    "notes": "Auto-generated break",
                }
            )
            continuous_minutes = 0
            last_task_id = None
            repeat_count = 0

        candidates = allowed_tasks
        if last_task_id is not None and repeat_count >= max_repeats_per_task:
            filtered = [task for task in allowed_tasks if task.id != last_task_id]
            if filtered:
                candidates = filtered

        task = random.choice(candidates)

        max_duration_allowed = min(max_task_minutes, remaining_active)
        if max_duration_allowed < min_task_minutes:
            task_duration = remaining_active
        else:
            task_duration = random.randint(min_task_minutes, max_duration_allowed)

        steps.append(
            {
                "step_type": "task",
                "planner_task_id": task.id,
                "duration_minutes": task_duration,
                "notes": f"Auto-generated task: {task.name}",
            }
        )

        active_minutes_used += task_duration
        continuous_minutes += task_duration

        if last_task_id == task.id:
            repeat_count += 1
        else:
            last_task_id = task.id
            repeat_count = 1

        remaining_active = total_active_minutes - active_minutes_used
        if remaining_active <= 0:
            break

        should_break = False
        if continuous_minutes >= max_continuous_minutes:
            should_break = True
        elif random.random() < 0.55:
            should_break = True

        if should_break:
            break_duration = random.randint(min_break_minutes, max_break_minutes)
            steps.append(
                {
                    "step_type": "break",
                    "planner_task_id": None,
                    "duration_minutes": break_duration,
                    "notes": "Auto-generated break",
                }
            )
            continuous_minutes = 0
            last_task_id = None
            repeat_count = 0

    return steps


@router.get("/planner_core/tasks", response_class=HTMLResponse)
def planner_tasks(request: Request, session: Session = Depends(get_session)):
    templates = get_templates(request)
    tasks = session.scalars(select(PlannerTask).order_by(PlannerTask.name)).all()
    return templates.TemplateResponse(
        request,
        "planner_tasks.html",
        {
            "request": request,
            "tasks": tasks,
            "message": request.query_params.get("message"),
        },
    )


@router.get("/planner_core/tasks/new", response_class=HTMLResponse)
def new_planner_task_form(request: Request):
    templates = get_templates(request)
    return templates.TemplateResponse(
        request,
        "planner_task_form.html",
        {
            "request": request,
            "planner_task": None,
            "message": request.query_params.get("message"),
        },
    )


@router.post("/planner_core/tasks/new")
def create_planner_task(
    name: str = Form(...),
    category: str = Form("general"),
    members_only: str = Form("f2p"),
    enabled: str = Form("true"),
    notes: str = Form(""),
    session: Session = Depends(get_session),
):
    cleaned_name = name.strip()
    if not cleaned_name:
        raise HTTPException(status_code=400, detail="Task name is required")

    task = PlannerTask(
        name=cleaned_name,
        category=category.strip() or "general",
        members_only=(members_only == "p2p"),
        enabled=(enabled == "true"),
        notes=notes.strip() or None,
    )
    session.add(task)
    session.commit()

    return RedirectResponse(url="/planner_core/tasks?message=Planner task created", status_code=303)


@router.get("/planner_core/tasks/{task_id}/edit", response_class=HTMLResponse)
def edit_planner_task_form(request: Request, task_id: int, session: Session = Depends(get_session)):
    templates = get_templates(request)
    planner_task = session.get(PlannerTask, task_id)
    if not planner_task:
        raise HTTPException(status_code=404, detail="Planner task not found")

    return templates.TemplateResponse(
        request,
        "planner_task_form.html",
        {
            "request": request,
            "planner_task": planner_task,
            "message": request.query_params.get("message"),
        },
    )


@router.post("/planner_core/tasks/{task_id}/edit")
def update_planner_task(
    task_id: int,
    name: str = Form(...),
    category: str = Form("general"),
    members_only: str = Form("f2p"),
    enabled: str = Form("true"),
    notes: str = Form(""),
    session: Session = Depends(get_session),
):
    planner_task = session.get(PlannerTask, task_id)
    if not planner_task:
        raise HTTPException(status_code=404, detail="Planner task not found")

    cleaned_name = name.strip()
    if not cleaned_name:
        raise HTTPException(status_code=400, detail="Task name is required")

    planner_task.name = cleaned_name
    planner_task.category = category.strip() or "general"
    planner_task.members_only = (members_only == "p2p")
    planner_task.enabled = (enabled == "true")
    planner_task.notes = notes.strip() or None

    session.commit()

    return RedirectResponse(url="/planner_core/tasks?message=Planner task updated", status_code=303)


@router.post("/planner_core/tasks/{task_id}/delete")
def delete_planner_task(task_id: int, session: Session = Depends(get_session)):
    planner_task = session.get(PlannerTask, task_id)
    if not planner_task:
        raise HTTPException(status_code=404, detail="Planner task not found")

    session.delete(planner_task)
    session.commit()

    return RedirectResponse(url="/planner_core/tasks?message=Planner task deleted", status_code=303)


@router.get("/planner_core/plans", response_class=HTMLResponse)
def planner_plans(request: Request, session: Session = Depends(get_session)):
    templates = get_templates(request)
    plans = session.scalars(select(PlanTemplate).order_by(PlanTemplate.name)).all()
    return templates.TemplateResponse(
        request,
        "planner_plans.html",
        {
            "request": request,
            "plans": plans,
            "message": request.query_params.get("message"),
        },
    )


@router.get("/planner_core/plans/new", response_class=HTMLResponse)
def new_plan_form(request: Request):
    templates = get_templates(request)
    return templates.TemplateResponse(
        request,
        "planner_plan_form.html",
        {
            "request": request,
            "plan": None,
            "message": request.query_params.get("message"),
        },
    )


@router.post("/planner_core/plans/new")
def create_plan(
    name: str = Form(...),
    description: str = Form(""),
    target_active_minutes: int = Form(0),
    session: Session = Depends(get_session),
):
    cleaned_name = name.strip()
    if not cleaned_name:
        raise HTTPException(status_code=400, detail="Plan name is required")

    plan = PlanTemplate(
        name=cleaned_name,
        description=description.strip() or None,
        is_generated=False,
        target_active_minutes=target_active_minutes or None,
    )
    session.add(plan)
    session.commit()
    session.refresh(plan)

    return RedirectResponse(url=f"/planner_core/plans/{plan.id}?message=Plan created", status_code=303)


@router.get("/planner_core/plans/{plan_id}", response_class=HTMLResponse)
def planner_plan_detail(request: Request, plan_id: int, session: Session = Depends(get_session)):
    templates = get_templates(request)
    plan = session.get(PlanTemplate, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    tasks = session.scalars(
        select(PlannerTask).where(PlannerTask.enabled.is_(True)).order_by(PlannerTask.name)
    ).all()
    accounts = session.scalars(select(Account).order_by(Account.label)).all()

    total_minutes = sum(step.duration_minutes for step in plan.steps)
    active_minutes = sum(step.duration_minutes for step in plan.steps if step.step_type == "task")
    break_minutes = sum(step.duration_minutes for step in plan.steps if step.step_type == "break")

    active_assignments = session.scalars(
        select(AccountPlanAssignment)
        .where(
            AccountPlanAssignment.plan_id == plan.id,
            AccountPlanAssignment.is_active.is_(True),
        )
        .order_by(AccountPlanAssignment.created_at.desc())
    ).all()

    return templates.TemplateResponse(
        request,
        "planner_plan_detail.html",
        {
            "request": request,
            "plan": plan,
            "tasks": tasks,
            "accounts": accounts,
            "total_minutes": total_minutes,
            "active_minutes": active_minutes,
            "break_minutes": break_minutes,
            "active_assignments": active_assignments,
            "message": request.query_params.get("message"),
        },
    )


@router.post("/planner_core/plans/{plan_id}/steps")
def add_plan_step(
    plan_id: int,
    step_type: str = Form(...),
    planner_task_id: int | None = Form(None),
    duration_minutes: int = Form(...),
    notes: str = Form(""),
    session: Session = Depends(get_session),
):
    plan = session.get(PlanTemplate, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    if step_type not in {"task", "break"}:
        raise HTTPException(status_code=400, detail="Invalid step type")

    if duration_minutes <= 0:
        raise HTTPException(status_code=400, detail="Duration must be greater than 0")

    if step_type == "task" and not planner_task_id:
        raise HTTPException(status_code=400, detail="Task step requires a planner_core task")

    next_order = len(plan.steps) + 1

    step = PlanStep(
        plan_id=plan_id,
        step_order=next_order,
        step_type=step_type,
        planner_task_id=planner_task_id if step_type == "task" else None,
        duration_minutes=duration_minutes,
        notes=notes.strip() or None,
    )
    session.add(step)
    session.commit()

    return RedirectResponse(url=f"/planner_core/plans/{plan_id}?message=Step added", status_code=303)


@router.post("/planner_core/steps/{step_id}/delete")
def delete_plan_step(step_id: int, session: Session = Depends(get_session)):
    step = session.get(PlanStep, step_id)
    if not step:
        raise HTTPException(status_code=404, detail="Plan step not found")

    plan_id = step.plan_id
    session.delete(step)
    session.commit()

    plan = session.get(PlanTemplate, plan_id)
    if plan:
        ordered_steps = sorted(plan.steps, key=lambda s: s.step_order)
        for index, current_step in enumerate(ordered_steps, start=1):
            current_step.step_order = index
        session.commit()

    return RedirectResponse(url=f"/planner_core/plans/{plan_id}?message=Step deleted", status_code=303)


@router.get("/planner_core/plans/{plan_id}/edit", response_class=HTMLResponse)
def edit_plan_form(request: Request, plan_id: int, session: Session = Depends(get_session)):
    templates = get_templates(request)
    plan = session.get(PlanTemplate, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    return templates.TemplateResponse(
        request,
        "planner_plan_form.html",
        {
            "request": request,
            "plan": plan,
            "message": request.query_params.get("message"),
        },
    )


@router.post("/planner_core/plans/{plan_id}/edit")
def update_plan(
    plan_id: int,
    name: str = Form(...),
    description: str = Form(""),
    target_active_minutes: int = Form(0),
    session: Session = Depends(get_session),
):
    plan = session.get(PlanTemplate, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    cleaned_name = name.strip()
    if not cleaned_name:
        raise HTTPException(status_code=400, detail="Plan name is required")

    plan.name = cleaned_name
    plan.description = description.strip() or None
    plan.target_active_minutes = target_active_minutes or None
    session.commit()

    return RedirectResponse(url=f"/planner_core/plans/{plan.id}?message=Plan updated", status_code=303)


@router.post("/planner_core/plans/{plan_id}/delete")
def delete_plan(plan_id: int, session: Session = Depends(get_session)):
    plan = session.get(PlanTemplate, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    session.delete(plan)
    session.commit()

    return RedirectResponse(url="/planner_core/plans?message=Plan deleted", status_code=303)


@router.get("/planner_core/assignments", response_class=HTMLResponse)
def planner_assignments(request: Request, session: Session = Depends(get_session)):
    templates = get_templates(request)
    accounts = session.scalars(select(Account).order_by(Account.label)).all()
    plans = session.scalars(select(PlanTemplate).order_by(PlanTemplate.name)).all()
    assignments = session.scalars(
        select(AccountPlanAssignment).order_by(AccountPlanAssignment.created_at.desc())
    ).all()

    return templates.TemplateResponse(
        request,
        "planner_assignments.html",
        {
            "request": request,
            "accounts": accounts,
            "plans": plans,
            "assignments": assignments,
            "message": request.query_params.get("message"),
        },
    )


@router.post("/planner_core/assignments")
def create_assignment(
    account_id: int = Form(...),
    plan_id: int = Form(...),
    assigned_for_date: str = Form(""),
    planned_start_time: str = Form(""),
    notes: str = Form(""),
    session: Session = Depends(get_session),
):
    account = session.get(Account, account_id)
    plan = session.get(PlanTemplate, plan_id)

    if not account or not plan:
        raise HTTPException(status_code=404, detail="Account or plan not found")

    parsed_date = date.fromisoformat(assigned_for_date) if assigned_for_date else None
    _assign_plan_to_account(
        session=session,
        account_id=account_id,
        plan_id=plan_id,
        assigned_for_date=parsed_date,
        planned_start_time=planned_start_time.strip() or None,
        notes=notes.strip() or None,
    )
    session.commit()

    return RedirectResponse(url="/planner_core/assignments?message=Plan assigned", status_code=303)


@router.post("/planner_core/assignments/{assignment_id}/deactivate")
def deactivate_assignment(assignment_id: int, session: Session = Depends(get_session)):
    assignment = session.get(AccountPlanAssignment, assignment_id)
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")

    assignment.is_active = False
    session.commit()

    return RedirectResponse(url="/planner_core/assignments?message=Assignment deactivated", status_code=303)


@router.get("/planner_core/generate", response_class=HTMLResponse)
def planner_generate_form(request: Request, session: Session = Depends(get_session)):
    templates = get_templates(request)
    accounts = session.scalars(select(Account).order_by(Account.label)).all()
    tasks = session.scalars(
        select(PlannerTask).where(PlannerTask.enabled.is_(True)).order_by(PlannerTask.name)
    ).all()

    return templates.TemplateResponse(
        request,
        "planner_generate.html",
        {
            "request": request,
            "accounts": accounts,
            "tasks": tasks,
            "message": request.query_params.get("message"),
        },
    )


@router.post("/planner_core/generate")
def generate_plans(
    account_ids: list[int] = Form(...),
    task_ids: list[int] = Form(...),
    base_name: str = Form("Generated Daily Plan"),
    total_active_minutes: int = Form(...),
    min_task_minutes: int = Form(...),
    max_task_minutes: int = Form(...),
    min_break_minutes: int = Form(...),
    max_break_minutes: int = Form(...),
    max_continuous_minutes: int = Form(...),
    max_repeats_per_task: int = Form(...),
    auto_assign: str = Form("true"),
    assign_date: str = Form(""),
    start_time: str = Form(""),
    session: Session = Depends(get_session),
):
    accounts = session.scalars(select(Account).where(Account.id.in_(account_ids)).order_by(Account.label)).all()
    tasks = session.scalars(
        select(PlannerTask)
        .where(
            PlannerTask.id.in_(task_ids),
            PlannerTask.enabled.is_(True),
        )
        .order_by(PlannerTask.name)
    ).all()

    if not accounts:
        raise HTTPException(status_code=400, detail="Select at least one account")
    if not tasks:
        raise HTTPException(status_code=400, detail="Select at least one planner_core task")

    parsed_date = date.fromisoformat(assign_date) if assign_date else None
    created_count = 0

    for account in accounts:
        steps_data = _generate_random_steps(
            allowed_tasks=tasks,
            total_active_minutes=total_active_minutes,
            min_task_minutes=min_task_minutes,
            max_task_minutes=max_task_minutes,
            min_break_minutes=min_break_minutes,
            max_break_minutes=max_break_minutes,
            max_continuous_minutes=max_continuous_minutes,
            max_repeats_per_task=max_repeats_per_task,
        )

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        plan = PlanTemplate(
            name=f"{base_name.strip() or 'Generated Plan'} - {account.label} - {timestamp}",
            description=f"Auto-generated for {account.label}",
            is_generated=True,
            target_active_minutes=total_active_minutes,
        )
        session.add(plan)
        session.flush()

        for index, step_data in enumerate(steps_data, start=1):
            session.add(
                PlanStep(
                    plan_id=plan.id,
                    step_order=index,
                    step_type=step_data["step_type"],
                    planner_task_id=step_data["planner_task_id"],
                    duration_minutes=step_data["duration_minutes"],
                    notes=step_data["notes"],
                )
            )

        if auto_assign == "true":
            _assign_plan_to_account(
                session=session,
                account_id=account.id,
                plan_id=plan.id,
                assigned_for_date=parsed_date,
                planned_start_time=start_time.strip() or None,
                notes="Auto-assigned from generator",
            )

        created_count += 1

    session.commit()

    return RedirectResponse(
        url=f"/planner_core/plans?message=Generated {created_count} plan(s)",
        status_code=303,
    )