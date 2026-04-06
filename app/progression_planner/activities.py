from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import Activity, Requirement, Reward
from .xp import normalize_skill


DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_PATH = DATA_DIR / "activities.f2p.json"


def _as_set(value: Any) -> set[str]:
    if not value:
        return set()
    if isinstance(value, (list, tuple, set)):
        return {str(v) for v in value if str(v).strip()}
    return {str(value)}


def _parse_requirement(obj: Any) -> Requirement:
    obj = obj or {}
    min_levels = {normalize_skill(k): int(v) for k, v in dict(obj.get("min_levels") or {}).items()}
    min_xp = {normalize_skill(k): int(v) for k, v in dict(obj.get("min_xp") or {}).items()}
    return Requirement(
        min_levels=min_levels,
        min_xp=min_xp,
        min_gp=int(obj.get("min_gp") or 0),
        required_unlocks=_as_set(obj.get("required_unlocks")),
        forbidden_unlocks=_as_set(obj.get("forbidden_unlocks")),
    )


def _parse_reward(obj: Any) -> Reward:
    obj = obj or {}
    xp = {normalize_skill(k): float(v) for k, v in dict(obj.get("xp") or {}).items()}
    gp = obj.get("gp")
    if gp is None:
        gp = obj.get("coins", 0)
    return Reward(
        xp=xp,
        gp=float(gp or 0.0),
        unlocks=_as_set(obj.get("unlocks")),
        quest_points=int(obj.get("quest_points") or 0),
    )


def _parse_activity(obj: Any) -> Activity:
    if not isinstance(obj, dict):
        raise TypeError("Activity must be a JSON object")

    name = str(obj.get("name") or "").strip()
    if not name:
        raise ValueError("Activity requires 'name'")

    activity_id = str(obj.get("id") or obj.get("activity_id") or name.lower().replace(" ", "_")).strip()

    xp_rates = {normalize_skill(k): float(v) for k, v in dict(obj.get("xp_rates") or {}).items()}

    gp_per_hour = obj.get("gp_per_hour")
    if gp_per_hour is None:
        gp_per_hour = obj.get("coins_per_hour", 0)

    return Activity(
        activity_id=activity_id,
        name=name,
        category=str(obj.get("category") or "general"),
        is_manual=bool(obj.get("manual", False)),
        requirements=_parse_requirement(obj.get("requirements")),
        xp_rates=xp_rates,
        gp_per_hour=float(gp_per_hour or 0.0),
        reward=_parse_reward(obj.get("reward")),
        repeatable=bool(obj.get("repeatable", True)),
        one_time=bool(obj.get("one_time", False)),
        duration_hours=float(obj.get("duration_hours", 1.0)),
        quest_name=(str(obj["quest_name"]).strip() if obj.get("quest_name") else None),
        notes=str(obj.get("notes") or ""),
    )


def load_activities(path: str | Path | None = None) -> list[Activity]:
    # Cache default load; callers should treat Activity objects as immutable.
    if path is None:
        return list(_load_activities_cached())

    p = Path(path)
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("Activities JSON must be a list")
    return [_parse_activity(item) for item in raw]


from functools import lru_cache


@lru_cache(maxsize=4)
def _load_activities_cached() -> tuple[Activity, ...]:
    """Load and merge activities from the data directory.

    Ordering is intentional:
    1) activities.f2p.json
    2) activities.p2p.json
    3) activities.sample.json (legacy overrides)

    Later files override earlier ones on id.
    """

    paths: list[Path] = []
    for fname in ["activities.f2p.json", "activities.p2p.json", "activities.sample.json", "activities.manual.json"]:
        p = DATA_DIR / fname
        if p.exists():
            paths.append(p)

    merged: dict[str, dict] = {}
    for p in paths:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError(f"Activities JSON must be a list: {p}")
        for item in raw:
            if not isinstance(item, dict):
                continue
            aid = str(item.get("id") or item.get("activity_id") or "").strip()
            if not aid:
                continue
            merged[aid] = item

    # Preserve ordering by re-reading in file order.
    out: list[Activity] = []
    seen: set[str] = set()
    for p in paths:
        raw = json.loads(p.read_text(encoding="utf-8"))
        for item in raw:
            if not isinstance(item, dict):
                continue
            aid = str(item.get("id") or item.get("activity_id") or "").strip()
            if not aid or aid in seen:
                continue
            out.append(_parse_activity(merged[aid]))
            seen.add(aid)

    return tuple(out)

