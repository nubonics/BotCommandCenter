from __future__ import annotations

from dataclasses import dataclass

from .models import AccountState, Activity
from .xp import xp_to_level


@dataclass(frozen=True)
class AvailabilityResult:
    available: bool
    reasons: list[str]


def check_activity_available(activity: Activity, state: AccountState) -> AvailabilityResult:
    """Explain *why* an activity is (not) available.

    This is intentionally more verbose than Activity.is_available(), which is a fast
    boolean check used in the planner.
    """

    reasons: list[str] = []

    req = activity.requirements

    for skill, level in (req.min_levels or {}).items():
        cur_lvl = xp_to_level(state.skills_xp.get(skill, 0))
        if cur_lvl < level:
            reasons.append(f"Requires {skill} level {level} (have {cur_lvl})")

    for skill, xp_needed in (req.min_xp or {}).items():
        cur_xp = int(state.skills_xp.get(skill, 0))
        if cur_xp < int(xp_needed):
            reasons.append(f"Requires {skill} XP {int(xp_needed)} (have {cur_xp})")

    if int(req.min_gp or 0) > 0 and int(state.gp) < int(req.min_gp or 0):
        reasons.append(f"Requires GP {int(req.min_gp)} (have {int(state.gp)})")

    missing_unlocks = set(req.required_unlocks or set()) - set(state.unlocks)
    if missing_unlocks:
        reasons.append(f"Missing unlocks: {', '.join(sorted(missing_unlocks))}")

    blocked_by = set(req.forbidden_unlocks or set()).intersection(state.unlocks)
    if blocked_by:
        reasons.append(f"Blocked by unlocks: {', '.join(sorted(blocked_by))}")

    if activity.quest_name and activity.quest_name in state.completed_quests:
        reasons.append(f"Quest already completed: {activity.quest_name}")

    if activity.one_time and f"done:{activity.activity_id}" in state.unlocks:
        reasons.append("One-time activity already completed")

    return AvailabilityResult(available=(len(reasons) == 0), reasons=reasons)


def available_activities(activities: list[Activity], state: AccountState) -> list[Activity]:
    return [a for a in activities if a.is_available(state)]
