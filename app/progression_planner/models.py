from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .xp import normalize_skill, xp_to_level


@dataclass(frozen=True)
class Requirement:
    min_levels: dict[str, int] = field(default_factory=dict)
    min_xp: dict[str, int] = field(default_factory=dict)
    min_gp: int = 0
    required_unlocks: set[str] = field(default_factory=set)
    forbidden_unlocks: set[str] = field(default_factory=set)

    def is_met(self, state: "AccountState") -> bool:
        for skill, level in self.min_levels.items():
            if xp_to_level(state.skills_xp.get(skill, 0)) < level:
                return False

        for skill, xp_needed in self.min_xp.items():
            if state.skills_xp.get(skill, 0) < xp_needed:
                return False

        # Interpret min_gp <= 0 as "no GP requirement".
        if self.min_gp > 0 and state.gp < self.min_gp:
            return False

        if not self.required_unlocks.issubset(state.unlocks):
            return False

        if self.forbidden_unlocks.intersection(state.unlocks):
            return False

        return True


@dataclass(frozen=True)
class Reward:
    xp: dict[str, float] = field(default_factory=dict)
    gp: float = 0.0
    unlocks: set[str] = field(default_factory=set)
    quest_points: int = 0


@dataclass
class AccountState:
    skills_xp: dict[str, int] = field(default_factory=dict)
    gp: int = 0
    unlocks: set[str] = field(default_factory=set)
    completed_quests: set[str] = field(default_factory=set)
    quest_points: int = 0

    def clone(self) -> "AccountState":
        return AccountState(
            skills_xp=dict(self.skills_xp),
            gp=self.gp,
            unlocks=set(self.unlocks),
            completed_quests=set(self.completed_quests),
            quest_points=self.quest_points,
        )

    def apply_reward(self, reward: Reward) -> None:
        for skill, xp_gain in reward.xp.items():
            key = normalize_skill(skill)
            self.skills_xp[key] = int(self.skills_xp.get(key, 0) + xp_gain)
        self.gp = int(self.gp + reward.gp)
        self.unlocks.update(reward.unlocks)
        self.quest_points += reward.quest_points


@dataclass(frozen=True)
class Goal:
    target_xp: dict[str, int] = field(default_factory=dict)
    target_gp: Optional[int] = None

    def remaining_xp(self, state: AccountState) -> dict[str, int]:
        return {s: max(0, t - state.skills_xp.get(s, 0)) for s, t in self.target_xp.items()}

    def remaining_gp(self, state: AccountState) -> int:
        if self.target_gp is None:
            return 0
        return max(0, self.target_gp - state.gp)

    def is_complete(self, state: AccountState) -> bool:
        return all(v <= 0 for v in self.remaining_xp(state).values()) and self.remaining_gp(state) <= 0


@dataclass(frozen=True)
class Activity:
    activity_id: str
    name: str
    category: str

    # If true, this activity is considered manual (human-driven).
    # Quests are effectively manual too, but we do not penalize quest completion.
    is_manual: bool = False

    requirements: Requirement = field(default_factory=Requirement)
    xp_rates: dict[str, float] = field(default_factory=dict)  # XP/hour
    gp_per_hour: float = 0.0

    reward: Reward = field(default_factory=Reward)

    repeatable: bool = True
    one_time: bool = False
    duration_hours: float = 1.0

    quest_name: Optional[str] = None
    notes: str = ""

    def is_available(self, state: AccountState) -> bool:
        if not self.requirements.is_met(state):
            return False
        if self.quest_name and self.quest_name in state.completed_quests:
            return False
        if self.one_time and f"done:{self.activity_id}" in state.unlocks:
            return False
        return True

    def apply(self, state: AccountState, hours: float) -> None:
        if self.repeatable:
            for skill, rate in self.xp_rates.items():
                key = normalize_skill(skill)
                state.skills_xp[key] = int(state.skills_xp.get(key, 0) + rate * hours)
            state.gp = int(state.gp + self.gp_per_hour * hours)

        state.apply_reward(self.reward)

        if self.quest_name:
            state.completed_quests.add(self.quest_name)
            state.unlocks.add(f"quest:{self.quest_name}")

        if self.one_time:
            state.unlocks.add(f"done:{self.activity_id}")


@dataclass(frozen=True)
class PlannerWeights:
    xp_weight: float = 1.0
    gp_weight: float = 1.0
    unlock_weight: float = 0.25
    quest_weight: float = 0.0


@dataclass(frozen=True)
class PlannerConfig:
    chunk_hours: float = 1.0
    max_steps: int = 500
    weights: PlannerWeights = PlannerWeights()


@dataclass
class PlanStep:
    activity_id: str
    activity_name: str
    category: str
    hours: float
    xp_gained: dict[str, int]
    gp_gained: int
    unlocks_gained: set[str]
    quest_completed: Optional[str] = None
    notes: str = ""


@dataclass
class PlanResult:
    steps: list[PlanStep]
    final_state: AccountState
    success: bool
    reason: str
