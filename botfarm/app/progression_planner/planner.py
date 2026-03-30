from __future__ import annotations

from .models import AccountState, Activity, Goal, PlannerConfig, PlanResult, PlanStep
from .scoring import direct_score_per_hour, score_activity

try:
    from .fast_direct import ActivityIndex
except Exception:  # pragma: no cover
    ActivityIndex = None  # type: ignore


class GreedyProgressionPlanner:
    def __init__(self, activities: list[Activity], config: PlannerConfig, top_k: int = 60) -> None:
        self.activities = activities
        self.config = config
        self.top_k = int(top_k or 60)

        self._id_to_index = {a.activity_id: i for i, a in enumerate(self.activities)}
        self._fast_index = ActivityIndex.build(self.activities, config) if ActivityIndex is not None else None

    def plan(self, initial_state: AccountState, goal: Goal) -> PlanResult:
        state = initial_state.clone()
        steps: list[PlanStep] = []

        # Memoization across steps (cheap-score cache).
        goal_skills = tuple(sorted(goal.target_xp.keys()))
        direct_cache: dict[tuple, float] = {}

        for _ in range(self.config.max_steps):
            if goal.is_complete(state):
                return PlanResult(self._compress(steps), state, True, "Goals completed successfully.")

            available = [a for a in self.activities if a.is_available(state)]
            if not available:
                # Helpful debug info (common during progression data iteration).
                try:
                    from .availability import check_activity_available

                    reason_counts: dict[str, int] = {}
                    for a in self.activities[:50]:
                        res = check_activity_available(a, state)
                        if res.available:
                            continue
                        for r in res.reasons:
                            reason_counts[r] = reason_counts.get(r, 0) + 1

                    top = sorted(reason_counts.items(), key=lambda x: x[1], reverse=True)[:6]
                    extra = (" Top blockers: " + "; ".join(f"{msg} (x{count})" for msg, count in top)) if top else ""
                except Exception:
                    extra = ""

                return PlanResult(
                    self._compress(steps),
                    state,
                    False,
                    "No activities available for current requirements/unlocks." + extra,
                )

            # 1) Cheap pass: compute direct score for all available activities.
            direct: list[tuple[float, bool, Activity]] = []
            fast_scores = None
            if self._fast_index is not None:
                fast_scores = self._fast_index.direct_scores(state, goal, self.config)

            # State signature: goal-skill XP + coarse GP bucket + unlocks.
            sig_levels = tuple((k, int(state.skills_xp.get(k, 0))) for k in goal_skills)
            gp_bucket = int(state.gp // 250_000)
            unlock_sig = frozenset(state.unlocks)

            for a in available:
                cache_key = (a.activity_id, sig_levels, gp_bucket, unlock_sig)
                s = direct_cache.get(cache_key)
                if s is None:
                    if fast_scores is not None:
                        idx = self._id_to_index.get(a.activity_id)
                        s = float(fast_scores[idx]) if idx is not None else 0.0
                    else:
                        s = direct_score_per_hour(a, state, goal, self.config)
                    direct_cache[cache_key] = s

                if s <= 0.0:
                    continue
                is_p2p = "members:p2p" in (a.requirements.required_unlocks or set())
                direct.append((s, is_p2p, a))

            if not direct:
                return PlanResult(self._compress(steps), state, False, "No available activity can make useful progress.")

            # Prefer useful F2P activities before P2P.
            has_useful_f2p = any(not is_p2p for _, is_p2p, _a in direct)

            # 2) Top-K shortlist (plus always include unlockers/quests).
            TOP_K = max(10, min(int(self.top_k), 500))
            direct.sort(key=lambda x: x[0], reverse=True)

            shortlist: list[Activity] = []
            seen_ids: set[str] = set()

            for s, _is_p2p, a in direct[:TOP_K]:
                if a.activity_id in seen_ids:
                    continue
                shortlist.append(a)
                seen_ids.add(a.activity_id)

            # Always include unlockers even if their direct score is zero.
            for a in available:
                if a.activity_id in seen_ids:
                    continue
                if a.quest_name or a.one_time or a.reward.unlocks:
                    shortlist.append(a)
                    seen_ids.add(a.activity_id)

            # 3) Expensive pass: full score on the shortlist only.
            scored: list[tuple[float, Activity]] = []
            for a in shortlist:
                s = score_activity(a, self.activities, state, goal, self.config)
                if s <= 0.0:
                    continue

                is_p2p = "members:p2p" in (a.requirements.required_unlocks or set())
                if has_useful_f2p and is_p2p:
                    s = s * 0.15

                # Manual activities (non-quest) are allowed but slightly penalized.
                if getattr(a, "is_manual", False) and a.category != "quest":
                    s = s * 0.6

                scored.append((s, a))

            if not scored:
                return PlanResult(self._compress(steps), state, False, "No available activity can make useful progress.")

            scored.sort(key=lambda x: x[0], reverse=True)
            chosen = scored[0][1]

            before = state.clone()
            hours = chosen.duration_hours if (chosen.quest_name or chosen.one_time or not chosen.repeatable) else self.config.chunk_hours
            chosen.apply(state, hours)

            steps.append(self._build_step(chosen, before, state, hours))

        return PlanResult(
            self._compress(steps),
            state,
            goal.is_complete(state),
            "Reached max_steps without completing goal.",
        )

    @staticmethod
    def _build_step(activity: Activity, before: AccountState, after: AccountState, hours: float) -> PlanStep:
        xp_gained: dict[str, int] = {}
        for skill in set(before.skills_xp) | set(after.skills_xp):
            diff = after.skills_xp.get(skill, 0) - before.skills_xp.get(skill, 0)
            if diff:
                xp_gained[skill] = diff

        return PlanStep(
            activity_id=activity.activity_id,
            activity_name=activity.name,
            category=activity.category,
            hours=float(hours),
            xp_gained=xp_gained,
            gp_gained=int(after.gp - before.gp),
            unlocks_gained=set(after.unlocks) - set(before.unlocks),
            quest_completed=activity.quest_name,
            notes=activity.notes,
        )

    @staticmethod
    def _compress(steps: list[PlanStep]) -> list[PlanStep]:
        if not steps:
            return []
        out = [steps[0]]
        for s in steps[1:]:
            prev = out[-1]
            if s.activity_id == prev.activity_id and not s.quest_completed and not prev.quest_completed:
                prev.hours += s.hours
                prev.gp_gained += s.gp_gained
                prev.unlocks_gained.update(s.unlocks_gained)
                for k, v in s.xp_gained.items():
                    prev.xp_gained[k] = prev.xp_gained.get(k, 0) + v
            else:
                out.append(s)
        return out
