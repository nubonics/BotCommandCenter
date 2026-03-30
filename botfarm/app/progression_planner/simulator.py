from __future__ import annotations

from dataclasses import dataclass

from .models import AccountState, Activity, PlanStep


@dataclass(frozen=True)
class SimulationChunk:
    """A single time chunk of simulation output."""

    step: PlanStep
    before: AccountState
    after: AccountState


def simulate_activity(state: AccountState, activity: Activity, *, total_hours: float, chunk_hours: float = 1.0) -> list[SimulationChunk]:
    """Apply an activity to an account state over time in fixed chunks.

    This is useful for:
    - generating progress graphs
    - verifying that rewards/unlocks apply at the right time
    - running "what-if" sims independent of the planner

    For one-time / quest activities, the simulator applies the activity once using
    its own duration_hours (matching planner behavior).
    """

    chunks: list[SimulationChunk] = []
    if total_hours <= 0:
        return chunks

    if activity.quest_name or activity.one_time or not activity.repeatable:
        before = state.clone()
        hours = float(activity.duration_hours)
        activity.apply(state, hours)
        chunks.append(
            SimulationChunk(
                step=PlanStep(
                    activity_id=activity.activity_id,
                    activity_name=activity.name,
                    category=activity.category,
                    hours=hours,
                    xp_gained={k: state.skills_xp.get(k, 0) - before.skills_xp.get(k, 0) for k in set(before.skills_xp) | set(state.skills_xp)},
                    gp_gained=int(state.gp - before.gp),
                    unlocks_gained=set(state.unlocks) - set(before.unlocks),
                    quest_completed=activity.quest_name,
                    notes=activity.notes,
                ),
                before=before,
                after=state.clone(),
            )
        )
        return chunks

    remaining = float(total_hours)

    while remaining > 1e-9:
        hours = min(float(chunk_hours), remaining)
        before = state.clone()
        activity.apply(state, hours)

        # Keep only non-zero XP gains.
        xp_gained: dict[str, int] = {}
        for skill in set(before.skills_xp) | set(state.skills_xp):
            diff = state.skills_xp.get(skill, 0) - before.skills_xp.get(skill, 0)
            if diff:
                xp_gained[skill] = diff

        chunks.append(
            SimulationChunk(
                step=PlanStep(
                    activity_id=activity.activity_id,
                    activity_name=activity.name,
                    category=activity.category,
                    hours=hours,
                    xp_gained=xp_gained,
                    gp_gained=int(state.gp - before.gp),
                    unlocks_gained=set(state.unlocks) - set(before.unlocks),
                    quest_completed=None,
                    notes=activity.notes,
                ),
                before=before,
                after=state.clone(),
            )
        )
        remaining -= hours

    return chunks
