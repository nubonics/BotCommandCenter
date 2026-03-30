from __future__ import annotations

from .models import AccountState, Activity, Goal, PlannerConfig


def _best_rates(activities: list[Activity], state: AccountState) -> tuple[float, dict[str, float]]:
    best_gp = 0.0
    best_xp: dict[str, float] = {}
    for a in activities:
        if not a.is_available(state) or not a.repeatable:
            continue
        best_gp = max(best_gp, float(a.gp_per_hour))
        for skill, rate in a.xp_rates.items():
            best_xp[skill] = max(best_xp.get(skill, 0.0), float(rate))
    return best_gp, best_xp


def _useful_xp(activity: Activity, remaining_xp: dict[str, int], hours: float) -> float:
    total = 0.0
    for skill, need in remaining_xp.items():
        if need <= 0:
            continue
        total += min(float(activity.xp_rates.get(skill, 0.0)) * hours, need)
        total += min(float(activity.reward.xp.get(skill, 0.0)), need)
    return total


def _useful_gp(activity: Activity, remaining_gp: int, hours: float) -> float:
    if remaining_gp <= 0:
        return 0.0
    total = 0.0
    total += min(max(float(activity.gp_per_hour) * hours, 0.0), remaining_gp)
    total += min(max(float(activity.reward.gp), 0.0), remaining_gp)
    return total


def _unlock_benefit(
    candidate: Activity,
    activities: list[Activity],
    state: AccountState,
    goal: Goal,
    config: PlannerConfig,
) -> tuple[float, float]:
    """Estimate how much this activity improves *future* ability to make progress.

    Unlock-aware: we compare the best available XP/GP rates before vs after applying
    the candidate once, and convert that rate improvement into a heuristic value.
    """

    rem_xp = goal.remaining_xp(state)
    rem_gp = goal.remaining_gp(state)

    b_gp, b_xp = _best_rates(activities, state)

    tmp = state.clone()
    duration = candidate.duration_hours if (candidate.quest_name or candidate.one_time or not candidate.repeatable) else config.chunk_hours
    candidate.apply(tmp, duration)

    a_gp, a_xp = _best_rates(activities, tmp)

    xp_bonus = 0.0
    gp_bonus = 0.0

    if rem_gp > 0 and a_gp > b_gp and a_gp > 0:
        gp_bonus += rem_gp if b_gp <= 0 else rem_gp * (a_gp / b_gp - 1.0)

    for skill, need in rem_xp.items():
        if need <= 0:
            continue
        before = float(b_xp.get(skill, 0.0))
        after = float(a_xp.get(skill, 0.0))
        if after > before and after > 0:
            xp_bonus += need if before <= 0 else need * (after / before - 1.0)

    return xp_bonus, gp_bonus


def direct_score_per_hour(activity: Activity, state: AccountState, goal: Goal, config: PlannerConfig) -> float:
    """Cheap score that ignores unlock benefit (used for top-K shortlisting)."""

    if not activity.is_available(state):
        return 0.0

    rem_xp = goal.remaining_xp(state)
    rem_gp = goal.remaining_gp(state)

    duration = activity.duration_hours if (activity.quest_name or activity.one_time or not activity.repeatable) else config.chunk_hours
    duration = max(float(duration), 0.01)

    xp_value = _useful_xp(activity, rem_xp, duration)
    gp_value = _useful_gp(activity, rem_gp, duration)

    w = config.weights
    total = (w.xp_weight * xp_value) + (w.gp_weight * gp_value) + ((w.quest_weight) if activity.quest_name else 0.0)
    return float(total / duration)


def score_activity_breakdown(
    activity: Activity,
    activities: list[Activity],
    state: AccountState,
    goal: Goal,
    config: PlannerConfig,
) -> dict[str, float]:
    """Return a verbose breakdown of the scoring components.

    All values are in "useful units" (XP/GP toward goal), not raw totals.
    The returned `score_per_hour` matches what the greedy planner optimizes.
    """

    if not activity.is_available(state):
        return {
            "duration": 0.0,
            "xp_value": 0.0,
            "gp_value": 0.0,
            "unlock_xp": 0.0,
            "unlock_gp": 0.0,
            "score_per_hour": 0.0,
        }

    rem_xp = goal.remaining_xp(state)
    rem_gp = goal.remaining_gp(state)

    duration = activity.duration_hours if (activity.quest_name or activity.one_time or not activity.repeatable) else config.chunk_hours
    duration = max(float(duration), 0.01)

    xp_value = _useful_xp(activity, rem_xp, duration)
    gp_value = _useful_gp(activity, rem_gp, duration)
    # Unlock benefit is expensive to compute (it scans activities). Only compute it
    # for activities that can actually change future availability.
    if activity.reward.unlocks or activity.quest_name or activity.one_time:
        unlock_xp, unlock_gp = _unlock_benefit(activity, activities, state, goal, config)
    else:
        unlock_xp, unlock_gp = 0.0, 0.0

    w = config.weights
    total = (
        w.xp_weight * xp_value
        + w.gp_weight * gp_value
        + w.unlock_weight * (w.xp_weight * unlock_xp + w.gp_weight * unlock_gp)
        + (w.quest_weight if activity.quest_name else 0.0)
    )

    return {
        "duration": float(duration),
        "xp_value": float(xp_value),
        "gp_value": float(gp_value),
        "unlock_xp": float(unlock_xp),
        "unlock_gp": float(unlock_gp),
        "score_per_hour": float(total / duration),
    }


def score_activity(activity: Activity, activities: list[Activity], state: AccountState, goal: Goal, config: PlannerConfig) -> float:
    return float(score_activity_breakdown(activity, activities, state, goal, config)["score_per_hour"])
