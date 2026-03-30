from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .models import Activity, AccountState, Goal, PlannerConfig
from .xp import SKILLS


@dataclass
class ActivityIndex:
    """Precomputed arrays for fast direct-score evaluation with NumPy.

    This only accelerates the *cheap pass* (direct XP/GP toward goal) used for
    Top-K shortlisting. Unlock-benefit and detailed simulation remain in Python.
    """

    activities: list[Activity]
    skill_index: dict[str, int]

    xp_rates: np.ndarray  # (N, S)
    reward_xp: np.ndarray  # (N, S)
    gp_per_hour: np.ndarray  # (N,)
    reward_gp: np.ndarray  # (N,)
    duration_hours: np.ndarray  # (N,)
    is_quest: np.ndarray  # (N,)

    @classmethod
    def build(cls, activities: list[Activity], config: PlannerConfig) -> "ActivityIndex":
        skill_index = {s: i for i, s in enumerate(SKILLS)}
        n = len(activities)
        s = len(SKILLS)

        xp_rates = np.zeros((n, s), dtype=np.float64)
        reward_xp = np.zeros((n, s), dtype=np.float64)
        gp_per_hour = np.zeros((n,), dtype=np.float64)
        reward_gp = np.zeros((n,), dtype=np.float64)
        duration_hours = np.zeros((n,), dtype=np.float64)
        is_quest = np.zeros((n,), dtype=np.int8)

        for i, a in enumerate(activities):
            gp_per_hour[i] = float(a.gp_per_hour or 0.0)
            reward_gp[i] = float(a.reward.gp or 0.0)
            isq = 1 if a.quest_name else 0
            is_quest[i] = isq

            # Duration in the planner for this activity.
            dur = a.duration_hours if (a.quest_name or a.one_time or not a.repeatable) else config.chunk_hours
            duration_hours[i] = max(float(dur), 0.01)

            for skill, rate in (a.xp_rates or {}).items():
                j = skill_index.get(skill)
                if j is not None:
                    xp_rates[i, j] = float(rate)

            for skill, xp in (a.reward.xp or {}).items():
                j = skill_index.get(skill)
                if j is not None:
                    reward_xp[i, j] = float(xp)

        return cls(
            activities=list(activities),
            skill_index=skill_index,
            xp_rates=xp_rates,
            reward_xp=reward_xp,
            gp_per_hour=gp_per_hour,
            reward_gp=reward_gp,
            duration_hours=duration_hours,
            is_quest=is_quest,
        )

    def direct_scores(self, state: AccountState, goal: Goal, config: PlannerConfig) -> np.ndarray:
        """Compute direct score/hr for all indexed activities (ignores availability).

        Uses remaining XP/GP toward goal only; ignores unlock-benefit.
        """

        # Remaining XP vector over full skill set.
        rem = np.zeros((len(SKILLS),), dtype=np.float64)
        for skill, target in (goal.target_xp or {}).items():
            j = self.skill_index.get(skill)
            if j is None:
                continue
            rem[j] = max(0.0, float(target) - float(state.skills_xp.get(skill, 0)))

        remaining_gp = float(goal.remaining_gp(state))

        dur = self.duration_hours

        # XP/hour * hours -> XP gained, then cap by remaining.
        xp_gain = self.xp_rates * dur[:, None]
        useful_xp = np.minimum(xp_gain, rem[None, :]).sum(axis=1)

        useful_reward_xp = np.minimum(self.reward_xp, rem[None, :]).sum(axis=1)

        # GP gained: clamp negative to 0 in the "useful" sense (goal is to reach target_gp).
        gp_gain = np.maximum(self.gp_per_hour * dur, 0.0)
        useful_gp = np.minimum(gp_gain, remaining_gp)

        reward_gp = np.maximum(self.reward_gp, 0.0)
        useful_reward_gp = np.minimum(reward_gp, remaining_gp)

        w = config.weights
        total = (w.xp_weight * (useful_xp + useful_reward_xp)) + (w.gp_weight * (useful_gp + useful_reward_gp))

        if w.quest_weight:
            total = total + (w.quest_weight * self.is_quest.astype(np.float64))

        return total / dur
