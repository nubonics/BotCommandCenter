from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .activities import load_activities
from .data_quests import load_quests
from .models import AccountState, Goal, PlannerConfig, PlannerWeights
from .planner import GreedyProgressionPlanner
from .xp import levels_to_xp, normalize_skill


def _read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_state(path: str | Path) -> AccountState:
    raw = _read_json(path)
    if not isinstance(raw, dict):
        raise ValueError("State JSON must be an object")

    gp = int(raw.get("gp") or raw.get("coins") or 0)

    skills_xp: dict[str, int] = {}
    if isinstance(raw.get("skills_xp"), dict):
        skills_xp.update({normalize_skill(k): int(v) for k, v in raw["skills_xp"].items()})

    if isinstance(raw.get("skills_levels"), dict):
        skills_xp.update(levels_to_xp({normalize_skill(k): int(v) for k, v in raw["skills_levels"].items()}))

    unlocks = set(raw.get("unlocks") or [])
    completed_quests = set(raw.get("completed_quests") or [])
    quest_points = int(raw.get("quest_points") or 0)

    return AccountState(
        skills_xp=skills_xp,
        gp=gp,
        unlocks=unlocks,
        completed_quests=completed_quests,
        quest_points=quest_points,
    )


def load_goal(path: str | Path) -> Goal:
    raw = _read_json(path)
    if not isinstance(raw, dict):
        raise ValueError("Goal JSON must be an object")

    target_gp = raw.get("target_gp")
    if target_gp is None:
        target_gp = raw.get("target_coins")

    target_xp: dict[str, int] = {}
    if isinstance(raw.get("target_xp"), dict):
        target_xp.update({normalize_skill(k): int(v) for k, v in raw["target_xp"].items()})

    if isinstance(raw.get("target_levels"), dict):
        target_xp.update(levels_to_xp({normalize_skill(k): int(v) for k, v in raw["target_levels"].items()}))

    return Goal(target_xp=target_xp, target_gp=(int(target_gp) if target_gp is not None else None))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Greedy progression planner_core (skills + GP + quests).")
    ap.add_argument("--state", required=True, help="Path to state JSON")
    ap.add_argument("--goal", required=True, help="Path to goal JSON")
    ap.add_argument("--chunk-hours", type=float, default=1.0)
    ap.add_argument("--max-steps", type=int, default=200)
    ap.add_argument("--xp-weight", type=float, default=1.0)
    ap.add_argument("--gp-weight", type=float, default=1.0)
    ap.add_argument("--unlock-weight", type=float, default=0.25)
    args = ap.parse_args(argv)

    state = load_state(args.state)
    goal = load_goal(args.goal)

    activities = load_activities()
    quests = load_quests()

    config = PlannerConfig(
        chunk_hours=args.chunk_hours,
        max_steps=args.max_steps,
        weights=PlannerWeights(
            xp_weight=args.xp_weight,
            gp_weight=args.gp_weight,
            unlock_weight=args.unlock_weight,
        ),
    )

    planner = GreedyProgressionPlanner([*quests, *activities], config=config)
    result = planner.plan(state, goal)

    print(f"Success: {result.success} ({result.reason})")
    for i, step in enumerate(result.steps, start=1):
        print(f"{i:02d}. {step.activity_name} [{step.category}] {step.hours:.2f}h  gp={step.gp_gained:+d}")
    return 0 if result.success else 2


if __name__ == "__main__":
    raise SystemExit(main())
