from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import get_session
from .models import (
    Account,
    AccountGoal,
    AccountPlanAssignment,
    AccountProgress,
    PlanStep,
    PlanTemplate,
    PlannerTask,
)

from .progression_planner.activities import load_activities
from .progression_planner.data_quests import load_quests, load_quests_overrides
from .progression_planner.models import AccountState, Goal, PlannerConfig, PlannerWeights
from .progression_planner.planner import GreedyProgressionPlanner
from .progression_planner.xp import SKILLS, level_to_xp, xp_to_level


# --- Runtime from Botting Hub script logs (sara*.txt) ---
# One file per account; filename includes the RS login email.
LOG_TS_RE = re.compile(r"^\[(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})\]\s*(.*)$")


def _is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except Exception:
        return False


def _win_to_wsl_path(win_path: str) -> Path:
    m = re.match(r"^([a-zA-Z]):\\(.*)$", win_path)
    if not m:
        return Path(win_path)
    drive = m.group(1).lower()
    rest = m.group(2).replace("\\", "/")
    return Path(f"/mnt/{drive}/{rest}")


def _parse_log_ts(line: str) -> datetime | None:
    m = LOG_TS_RE.match(line.rstrip("\n"))
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y/%m/%d %H:%M:%S")
    except Exception:
        return None


def _runtime_hours_from_log(path: Path, max_gap_seconds: int = 300) -> float:
    """Compute active runtime hours from an action log.

    We sum deltas between consecutive timestamps, but cap each delta to
    max_gap_seconds so long idle gaps don't inflate runtime.
    """
    if not path.exists() or not path.is_file():
        return 0.0

    last: datetime | None = None
    total = 0.0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                ts = _parse_log_ts(line)
                if not ts:
                    continue
                if last is not None:
                    dt = (ts - last).total_seconds()
                    if dt > 0:
                        total += min(dt, float(max_gap_seconds))
                last = ts
    except Exception:
        return 0.0

    return total / 3600.0


router = APIRouter(tags=["progression"])


def get_templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _safe_json_loads(text: str, default: Any) -> Any:
    if not text or not text.strip():
        return default
    return json.loads(text)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _get_or_create_progress(session: Session, account_id: int) -> AccountProgress:
    row = session.scalars(select(AccountProgress).where(AccountProgress.account_id == account_id)).first()
    if row:
        return row
    row = AccountProgress(account_id=account_id)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _get_active_goal(session: Session, account_id: int) -> AccountGoal | None:
    return session.scalars(
        select(AccountGoal)
        .where(AccountGoal.account_id == account_id, AccountGoal.is_active.is_(True))
        .order_by(AccountGoal.created_at.desc())
    ).first()


def _deactivate_goals(session: Session, account_id: int) -> None:
    goals = session.scalars(
        select(AccountGoal).where(AccountGoal.account_id == account_id, AccountGoal.is_active.is_(True))
    ).all()
    for g in goals:
        g.is_active = False


def _compute_progress_rows(progress: AccountProgress, goal: AccountGoal) -> tuple[list[dict], float]:
    cur_xp = dict(_safe_json_loads(progress.skills_xp_json, {}))
    base_xp = dict(_safe_json_loads(goal.baseline_skills_xp_json, {}))
    tgt_xp = dict(_safe_json_loads(goal.target_skills_xp_json, {}))

    rows: list[dict] = []
    percents: list[float] = []

    for skill, tgt in sorted(tgt_xp.items()):
        b = int(base_xp.get(skill, 0))
        c = int(cur_xp.get(skill, b))
        t = int(tgt)
        denom = max(1, t - b)
        pct = max(0.0, min(1.0, (c - b) / float(denom)))
        percents.append(pct)
        rows.append(
            {
                "kind": "skill",
                "skill": skill,
                "baseline_xp": b,
                "current_xp": c,
                "target_xp": t,
                "baseline_level": xp_to_level(b),
                "current_level": xp_to_level(c),
                "target_level": xp_to_level(t),
                "pct": pct,
            }
        )

    if goal.target_gp is not None:
        b = int(goal.baseline_gp or 0)
        c = int(progress.gp or 0)
        t = int(goal.target_gp)
        denom = max(1, t - b)
        pct = max(0.0, min(1.0, (c - b) / float(denom)))
        percents.append(pct)
        rows.append({"kind": "gp", "pct": pct, "baseline_gp": b, "current_gp": c, "target_gp": t})

    overall = sum(percents) / float(len(percents)) if percents else 0.0
    return rows, overall


def _top_blockers(state: AccountState, candidates) -> list[dict[str, object]]:
    """Return a small list of common reasons activities are unavailable."""

    from .progression_planner.availability import check_activity_available

    reason_counts: dict[str, int] = {}
    for a in list(candidates)[:200]:
        res = check_activity_available(a, state)
        if res.available:
            continue
        for r in res.reasons:
            reason_counts[r] = reason_counts.get(r, 0) + 1

    top = sorted(reason_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    return [{"reason": reason, "count": count} for reason, count in top]


def _plan_preview(progress: AccountProgress, goal: AccountGoal):
    state = AccountState(
        skills_xp={k: int(v) for k, v in dict(_safe_json_loads(progress.skills_xp_json, {})).items()},
        gp=int(progress.gp or 0),
        unlocks=set(_safe_json_loads(progress.unlocks_json, [])),
        completed_quests=set(_safe_json_loads(progress.completed_quests_json, [])),
        quest_points=int(progress.quest_points or 0),
    )
    planner_goal = Goal(
        target_xp={k: int(v) for k, v in dict(_safe_json_loads(goal.target_skills_xp_json, {})).items()},
        target_gp=(int(goal.target_gp) if goal.target_gp is not None else None),
    )

    activities = load_activities()
    # Planning uses only the curated quest overrides for performance.
    quests = load_quests_overrides()

    weights_raw = _safe_json_loads(getattr(goal, "planner_weights_json", "{}"), {})
    if not isinstance(weights_raw, dict):
        weights_raw = {}

    allow_manual_flag = bool(weights_raw.get("allow_manual", False))
    if not allow_manual_flag:
        activities = [a for a in activities if not a.is_manual]

    weights = PlannerWeights(
        xp_weight=float(weights_raw.get("xp_weight", 1.0)),
        gp_weight=float(weights_raw.get("gp_weight", 1.0)),
        unlock_weight=float(weights_raw.get("unlock_weight", 0.25)),
        quest_weight=float(weights_raw.get("quest_weight", 0.0)),
    )

    config = PlannerConfig(
        chunk_hours=4.0,
        max_steps=2000,
        weights=weights,
    )
    top_k = int(weights_raw.get("top_k", 60) or 60)
    planner = GreedyProgressionPlanner([*quests, *activities], config=config, top_k=top_k)
    result = planner.plan(state, planner_goal)
    total_hours = sum(s.hours for s in result.steps)

    blockers = _top_blockers(state, [*quests, *activities]) if not result.success else []

    # Verbose scoring debug for each chosen step (requested).
    plan_step_debug: list[dict[str, object]] = []
    if result.steps:
        from .progression_planner.scoring import score_activity_breakdown

        by_id = {a.activity_id: a for a in [*quests, *activities]}
        sim_state = state.clone()

        for idx, step in enumerate(result.steps, start=1):
            activity = by_id.get(step.activity_id)
            if activity is None:
                plan_step_debug.append({"index": idx, "activity_id": step.activity_id, "score": None})
                continue

            breakdown = score_activity_breakdown(activity, [*quests, *activities], sim_state, planner_goal, config)
            plan_step_debug.append({"index": idx, "activity_id": step.activity_id, "score": breakdown})
            activity.apply(sim_state, step.hours)

    return result, total_hours, blockers, plan_step_debug


def _ensure_task(session: Session, name: str, category: str) -> PlannerTask:
    task = session.scalars(select(PlannerTask).where(PlannerTask.name == name)).first()
    if task:
        return task
    task = PlannerTask(
        name=name,
        category=category or "progression",
        enabled=True,
        members_only=False,
        notes="Auto-created from progression planner_core",
    )
    session.add(task)
    session.flush()
    return task


def _deactivate_assignments(session: Session, account_id: int) -> None:
    assigns = session.scalars(
        select(AccountPlanAssignment).where(
            AccountPlanAssignment.account_id == account_id,
            AccountPlanAssignment.is_active.is_(True),
        )
    ).all()
    for a in assigns:
        a.is_active = False


@router.get("/accounts/{account_id}/progress", response_class=HTMLResponse)
def progress_page(request: Request, account_id: int, session: Session = Depends(get_session)):
    templates = get_templates(request)

    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    progress = _get_or_create_progress(session, account_id)
    goal = _get_active_goal(session, account_id)

    rows: list[dict] = []
    overall = 0.0
    plan = None
    plan_hours = 0.0
    plan_blockers: list[dict[str, object]] = []
    plan_step_debug: list[dict[str, object]] = []
    show_plan = False

    if goal:
        rows, overall = _compute_progress_rows(progress, goal)

        show_plan = request.query_params.get("plan") == "1"
        if show_plan:
            plan, plan_hours, plan_blockers, plan_step_debug = _plan_preview(progress, goal)

    active_assignment = session.scalars(
        select(AccountPlanAssignment)
        .where(AccountPlanAssignment.account_id == account_id, AccountPlanAssignment.is_active.is_(True))
        .order_by(AccountPlanAssignment.created_at.desc())
    ).first()

    # UI helpers for skill grids.
    skills_xp = dict(_safe_json_loads(progress.skills_xp_json, {}))
    current_levels = {s: xp_to_level(int(skills_xp.get(s, 0))) for s in SKILLS}

    completed_set = set(_safe_json_loads(progress.completed_quests_json, []))

    # Quest/miniquest meta comes from the wiki-generated dataset.
    miniquests: set[str] = set()
    members_by_name: dict[str, bool] = {}
    try:
        from .progression_planner.data_quests import load_quests_raw

        for item in load_quests_raw():
            name = item.get("name")
            if not name:
                continue
            name = str(name)
            if item.get("is_miniquest"):
                miniquests.add(name)
            if "is_members" in item:
                members_by_name[name] = bool(item.get("is_members"))
    except Exception:
        miniquests = set()
        members_by_name = {}

    quest_rows: list[dict[str, object]] = []
    for q in load_quests():
        prereq_quests = sorted(
            [u.removeprefix("quest:") for u in (q.requirements.required_unlocks or set()) if u.startswith("quest:")]
        )
        unmet = [p for p in prereq_quests if p not in completed_set]
        is_mini = (q.name in miniquests)
        is_members = members_by_name.get(q.name, True)
        membership = "p2p" if is_members else "f2p"

        quest_rows.append(
            {
                "name": q.name,
                "prereqs": prereq_quests,
                "unmet": unmet,
                "is_miniquest": is_mini,
                "membership": membership,
                "qp": int(getattr(q.reward, "quest_points", 0) or 0),
            }
        )

    # Stable sort: quests first, then miniquests, then name.
    quest_rows.sort(key=lambda r: (bool(r.get("is_miniquest")), str(r.get("name")).lower()))

    goal_levels: dict[str, int] = {}
    baseline_mode = "true"
    weights_ui = {"xp_weight": 1.0, "gp_weight": 1.0, "unlock_weight": 0.25, "quest_weight": 0.0, "allow_manual": False, "top_k": 60}
    if goal:
        tgt_xp = dict(_safe_json_loads(goal.target_skills_xp_json, {}))
        goal_levels = {s: xp_to_level(int(tgt_xp.get(s, 0))) for s in SKILLS}

        base_xp = _safe_json_loads(goal.baseline_skills_xp_json, {})
        has_nonzero_baseline = bool(base_xp) or bool(int(goal.baseline_gp or 0))
        baseline_mode = "true" if has_nonzero_baseline else "false"

        wraw = _safe_json_loads(getattr(goal, "planner_weights_json", "{}"), {})
        if isinstance(wraw, dict):
            for k in ["xp_weight", "gp_weight", "unlock_weight", "quest_weight"]:
                if k in wraw:
                    try:
                        weights_ui[k] = float(wraw[k])
                    except Exception:
                        pass
            if "allow_manual" in wraw:
                weights_ui["allow_manual"] = bool(wraw.get("allow_manual"))
            if "top_k" in wraw:
                try:
                    weights_ui["top_k"] = int(wraw.get("top_k") or 60)
                except Exception:
                    pass

    return templates.TemplateResponse(
        request,
        "account_progress.html",
        {
            "request": request,
            "account": account,
            "progress": progress,
            "goal": goal,
            "skills": SKILLS,
            "current_levels": current_levels,
            "goal_levels": goal_levels,
            "baseline_mode": baseline_mode,
            "weights_ui": weights_ui,
            "quest_rows": quest_rows,
            "completed_set": completed_set,
            "rows": rows,
            "overall": overall,
            "plan": plan,
            "plan_hours": plan_hours,
            "show_plan": show_plan,
            "plan_blockers": plan_blockers,
            "plan_step_debug": plan_step_debug,
            "active_assignment": active_assignment,
            "message": request.query_params.get("message"),
        },
    )


@router.get("/accounts/progress", response_class=HTMLResponse)
def progress_all_accounts_page(request: Request, session: Session = Depends(get_session)):
    """Aggregate progress page for all accounts.

    Computes the same "overall completion" score shown on the per-account page,
    for each account that has an active goal.
    """
    templates = get_templates(request)

    accounts = session.scalars(select(Account).order_by(Account.label)).all()

    # Optional stacking skill filters (current level).
    skills_q = list(request.query_params.getlist("skill"))
    mins_q = list(request.query_params.getlist("min_level"))

    filters: list[tuple[str, int]] = []
    for i, s in enumerate(skills_q):
        s = (s or "").strip()
        if not s or s not in SKILLS:
            continue
        raw = (mins_q[i] if i < len(mins_q) else "").strip()
        try:
            mn = int(raw) if raw else 1
        except Exception:
            mn = 1
        filters.append((s, max(1, mn)))

    # Runtime from logs.
    logs_dir = (request.query_params.get("logs_dir") or r"C:\Users\nubonix\Botting Hub\Client\Logs\Script").strip()
    max_gap_raw = (request.query_params.get("max_gap_seconds") or "300").strip()
    try:
        max_gap_seconds = int(max_gap_raw)
    except Exception:
        max_gap_seconds = 300
    if max_gap_seconds < 1:
        max_gap_seconds = 300

    logs_dir_path = _win_to_wsl_path(logs_dir) if _is_wsl() else Path(logs_dir)

    rows: list[dict[str, object]] = []
    for account in accounts:
        progress = _get_or_create_progress(session, account.id)
        goal = _get_active_goal(session, account.id)

        overall = 0.0
        goal_name = None
        goal_created_at = None
        baseline_gp = None

        if goal:
            _rows, overall = _compute_progress_rows(progress, goal)
            goal_name = getattr(goal, "name", None)
            goal_created_at = getattr(goal, "created_at", None)
            baseline_gp = getattr(goal, "baseline_gp", None)

        # Prefer quest points tracked on the progress object.
        quest_points = int(getattr(progress, "quest_points", 0) or 0)

        # Skill filters (AND): compute requested skill levels from XP json.
        skill_levels: dict[str, int] = {}
        if filters:
            skills_xp = dict(_safe_json_loads(progress.skills_xp_json, {}))
            ok = True
            for s, mn in filters:
                try:
                    lvl = xp_to_level(int(skills_xp.get(s, 0) or 0))
                except Exception:
                    lvl = 1
                skill_levels[s] = lvl
                if lvl < mn:
                    ok = False
                    break
            if not ok:
                continue

        # Runtime (hours) from a per-account action log.
        runtime_hours = 0.0
        log_path = None
        if account.rs_email and "@" in account.rs_email:
            # Prefer the convention: <anything><email>.txt, e.g.
            #   sarahernandez1628+she69@gmail.com.txt
            # Also supports prefixes like sara<email>.txt.
            email = account.rs_email.strip()
            patterns = [
                f"*{email}.txt",
            ]

            matches: list[Path] = []
            for pat in patterns:
                try:
                    matches.extend(list(logs_dir_path.glob(pat)))
                except Exception:
                    pass

            # If multiple, pick most recently modified.
            matches = [p for p in matches if p.is_file()]
            if matches:
                matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                log_path = matches[0]

            if log_path is not None:
                runtime_hours = _runtime_hours_from_log(log_path, max_gap_seconds=max_gap_seconds)

        rows.append(
            {
                "account": account,
                "goal": goal,
                "goal_name": goal_name,
                "goal_created_at": goal_created_at,
                "baseline_gp": baseline_gp,
                "quest_points": quest_points,
                "overall": float(overall or 0.0),
                "filters": filters,
                "skill_levels": skill_levels,
                "runtime_hours": runtime_hours,
            }
        )

    # Highest completion first, then name.
    rows.sort(key=lambda r: (-float(r.get("overall") or 0.0), str(getattr(r["account"], "label", "")).lower()))

    active_count = sum(1 for r in rows if r.get("goal") is not None)
    avg_overall = 0.0
    if active_count:
        avg_overall = sum(float(r.get("overall") or 0.0) for r in rows if r.get("goal") is not None) / active_count

    avg_runtime = 0.0
    if rows:
        avg_runtime = sum(float(r.get("runtime_hours") or 0.0) for r in rows) / len(rows)

    return templates.TemplateResponse(
        request,
        "accounts_progress.html",
        {
            "request": request,
            "rows": rows,
            "active_count": active_count,
            "avg_overall": avg_overall,
            "avg_runtime": avg_runtime,
            "skills": SKILLS,
            "filters": filters,
            "logs_dir": logs_dir,
            "max_gap_seconds": max_gap_seconds,
            "message": request.query_params.get("message"),
        },
    )


@router.get("/accounts/{account_id}/progress/simulate", response_class=HTMLResponse)
def simulate_progress_plan(request: Request, account_id: int, session: Session = Depends(get_session)):
    templates = get_templates(request)

    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    progress = _get_or_create_progress(session, account_id)
    goal = _get_active_goal(session, account_id)
    if not goal:
        raise HTTPException(status_code=400, detail="Set a goal first")

    plan, plan_hours, _blockers, _debug = _plan_preview(progress, goal)
    if not plan.steps:
        raise HTTPException(status_code=400, detail=f"No plan steps to simulate: {plan.reason}")

    # Rebuild initial state (same as preview) and simulate chunk-by-chunk.
    from .progression_planner.simulator import simulate_activity

    state = AccountState(
        skills_xp={k: int(v) for k, v in dict(_safe_json_loads(progress.skills_xp_json, {})).items()},
        gp=int(progress.gp or 0),
        unlocks=set(_safe_json_loads(progress.unlocks_json, [])),
        completed_quests=set(_safe_json_loads(progress.completed_quests_json, [])),
        quest_points=int(progress.quest_points or 0),
    )

    activities = load_activities()
    quests = load_quests_overrides()
    by_id = {a.activity_id: a for a in [*quests, *activities]}

    chunks_out: list[dict[str, object]] = []
    t = 0.0

    # Use the same chunk size as the planner preview config.
    chunk_hours = 1.0

    for step in plan.steps:
        activity = by_id.get(step.activity_id)
        if activity is None:
            continue

        sim_chunks = simulate_activity(state, activity, total_hours=float(step.hours), chunk_hours=chunk_hours)
        for c in sim_chunks:
            chunks_out.append(
                {
                    "t_start": t,
                    "t_end": t + float(c.step.hours),
                    "activity_name": c.step.activity_name,
                    "hours": float(c.step.hours),
                    "xp_gained": c.step.xp_gained,
                    "gp_gained": int(c.step.gp_gained),
                    "unlocks_gained": sorted(list(c.step.unlocks_gained)),
                }
            )
            t += float(c.step.hours)

    return templates.TemplateResponse(
        request,
        "account_progress_sim.html",
        {
            "request": request,
            "account": account,
            "chunks": chunks_out,
            "total_hours": t,
            "steps_count": len(plan.steps),
            "final_state": state,
            "message": request.query_params.get("message"),
        },
    )


@router.post("/accounts/{account_id}/progress/import-hiscores")
async def import_hiscores(
    account_id: int,
    hiscores_rsn: str = Form(""),
    session: Session = Depends(get_session),
):
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    rsn = hiscores_rsn.strip()
    if not rsn:
        raise HTTPException(status_code=400, detail="RuneScape username is required")

    progress = _get_or_create_progress(session, account_id)

    import urllib.parse

    url = "https://secure.runescape.com/m=hiscore_oldschool/index_lite.ws?player=" + urllib.parse.quote(rsn)

    import httpx

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            raise HTTPException(status_code=400, detail=f"Hiscores lookup failed ({resp.status_code})")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Hiscores lookup error: {e}")

    text = resp.text.strip()
    lines = [ln for ln in text.splitlines() if ln.strip()]

    # OSRS lite hiscores skills order (overall first, then skills).
    order = [
        "overall",
        "attack",
        "defence",
        "strength",
        "hitpoints",
        "ranged",
        "prayer",
        "magic",
        "cooking",
        "woodcutting",
        "fletching",
        "fishing",
        "firemaking",
        "crafting",
        "smithing",
        "mining",
        "herblore",
        "agility",
        "thieving",
        "slayer",
        "farming",
        "runecraft",
        "hunter",
        "construction",
    ]

    xp_by_skill: dict[str, int] = {}
    for idx, skill in enumerate(order):
        if idx >= len(lines):
            break
        parts = lines[idx].split(",")
        if len(parts) < 3:
            continue

        # rank, level, xp
        try:
            xp = int(parts[2])
        except Exception:
            xp = 0

        if skill != "overall":
            xp_by_skill[skill] = max(0, xp)

    # Preserve sailing if present (not on lite hiscores yet).
    try:
        existing = dict(_safe_json_loads(progress.skills_xp_json, {}))
        if "sailing" in existing and "sailing" not in xp_by_skill:
            xp_by_skill["sailing"] = int(existing.get("sailing") or 0)
    except Exception:
        pass

    progress.skills_xp_json = _json_dumps(xp_by_skill)
    session.commit()

    return RedirectResponse(
        url=f"/accounts/{account_id}/progress?message=Imported skills from hiscores for {rsn}",
        status_code=303,
    )


@router.post("/accounts/{account_id}/progress/state")
async def update_state(
    account_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    progress = _get_or_create_progress(session, account_id)

    form = await request.form()

    gp = int(form.get("gp") or 0)
    skills_mode = str(form.get("skills_mode") or "levels")

    # Skills grid: overwrite skills entirely from the form.
    skills_xp: dict[str, int] = {}
    for s in SKILLS:
        raw = form.get(f"skill_{s}")
        if raw is None or str(raw).strip() == "":
            continue
        n = int(raw)
        skills_xp[s] = level_to_xp(n) if skills_mode == "levels" else n

    unlocks_json = str(form.get("unlocks_json") or "[]")
    quest_points = int(form.get("quest_points") or 0)

    unlocks_list = _safe_json_loads(unlocks_json, [])
    completed_list = [str(x) for x in form.getlist("completed_quests")]
    if not isinstance(unlocks_list, list):
        raise HTTPException(status_code=400, detail="Unlocks must be a JSON array")

    progress.gp = gp
    progress.skills_xp_json = _json_dumps(skills_xp)
    progress.unlocks_json = _json_dumps([str(x) for x in unlocks_list])
    progress.completed_quests_json = _json_dumps([str(x) for x in completed_list])
    progress.quest_points = quest_points
    session.commit()

    return RedirectResponse(url=f"/accounts/{account_id}/progress?message=Progress state updated", status_code=303)


@router.post("/accounts/{account_id}/progress/goal")
async def set_goal(
    account_id: int,
    request: Request,
    name: str = Form(""),
    target_gp: str = Form(""),
    target_skills_mode: str = Form("levels"),
    use_current_as_baseline: str = Form("true"),
    xp_weight: float = Form(1.0),
    gp_weight: float = Form(1.0),
    unlock_weight: float = Form(0.25),
    quest_weight: float = Form(0.0),
    allow_manual: str = Form("false"),
    top_k: int = Form(60),
    session: Session = Depends(get_session),
):
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    progress = _get_or_create_progress(session, account_id)

    form = await request.form()

    # Target skills grid.
    target_xp: dict[str, int] = {}
    for s in SKILLS:
        raw = form.get(f"goal_{s}")
        if raw is None or str(raw).strip() == "":
            continue
        n = int(raw)
        target_xp[s] = level_to_xp(n) if target_skills_mode == "levels" else n

    parsed_target_gp = int(target_gp) if target_gp.strip() else None

    weights_obj = {
        "xp_weight": float(xp_weight),
        "gp_weight": float(gp_weight),
        "unlock_weight": float(unlock_weight),
        "quest_weight": float(quest_weight),
        "allow_manual": (allow_manual == "true"),
        "top_k": int(top_k or 60),
    }
    weights_json = _json_dumps(weights_obj)

    baseline_xp = {}
    baseline_gp = 0
    if use_current_as_baseline == "true":
        baseline_xp = _safe_json_loads(progress.skills_xp_json, {})
        baseline_gp = int(progress.gp or 0)

    _deactivate_goals(session, account_id)

    goal = AccountGoal(
        account_id=account_id,
        name=name.strip() or None,
        is_active=True,
        baseline_skills_xp_json=_json_dumps(baseline_xp),
        baseline_gp=baseline_gp,
        target_skills_xp_json=_json_dumps(target_xp),
        target_gp=parsed_target_gp,
        planner_weights_json=weights_json,
    )
    session.add(goal)
    session.commit()

    return RedirectResponse(url=f"/accounts/{account_id}/progress?message=Goal saved", status_code=303)


@router.post("/accounts/{account_id}/progress/push-plan")
def push_plan(account_id: int, session: Session = Depends(get_session)):
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    progress = _get_or_create_progress(session, account_id)
    goal = _get_active_goal(session, account_id)
    if not goal:
        raise HTTPException(status_code=400, detail="Set a goal first")

    plan_result, total_hours, _plan_blockers, _plan_step_debug = _plan_preview(progress, goal)
    if not plan_result.steps:
        raise HTTPException(status_code=400, detail="Planner produced zero steps")

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    plan = PlanTemplate(
        name=f"Progression Plan - {account.label} - {timestamp}",
        description="Auto-generated from progression goals",
        is_generated=True,
        target_active_minutes=int(round(total_hours * 60)),
    )
    session.add(plan)
    session.flush()

    step_order = 1
    for step in plan_result.steps:
        task = _ensure_task(session, step.activity_name, step.category)
        minutes = max(1, int(round(step.hours * 60)))
        session.add(
            PlanStep(
                plan_id=plan.id,
                step_order=step_order,
                step_type="task",
                planner_task_id=task.id,
                duration_minutes=minutes,
                notes=f"From progression planner_core: {step.activity_id}",
            )
        )
        step_order += 1

    _deactivate_assignments(session, account_id)
    session.add(
        AccountPlanAssignment(
            account_id=account_id,
            plan_id=plan.id,
            assigned_for_date=None,
            planned_start_time=None,
            is_active=True,
            notes="Auto-assigned from progression planner_core",
        )
    )
    session.commit()

    return RedirectResponse(url=f"/planner_core/plans/{plan.id}?message=Progression plan created and assigned", status_code=303)
