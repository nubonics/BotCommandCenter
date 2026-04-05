from __future__ import annotations

import unittest

from app.progression_planner.activities import load_activities
from app.progression_planner.data_quests import load_quests
from app.progression_planner.models import AccountState, Goal, PlannerConfig, PlannerWeights
from app.progression_planner.planner import GreedyProgressionPlanner
from app.progression_planner.xp import level_to_xp


class TestProgressionPlanner(unittest.TestCase):
    def setUp(self) -> None:
        self.activities = load_activities()
        self.quests = load_quests()
        self.planner = GreedyProgressionPlanner(
            [*self.quests, *self.activities],
            config=PlannerConfig(
                chunk_hours=1.0,
                max_steps=50,
                weights=PlannerWeights(unlock_weight=0.25),
            ),
        )

    def test_waterfall_quest_selected_for_attack_and_strength(self) -> None:
        state = AccountState(skills_xp={"attack": 0, "strength": 0}, gp=0, unlocks={"members:p2p"})
        goal = Goal(target_xp={"attack": level_to_xp(30), "strength": level_to_xp(30)})

        result = self.planner.plan(state, goal)
        self.assertTrue(result.success, result.reason)
        self.assertGreaterEqual(len(result.steps), 1)
        self.assertEqual(result.steps[0].quest_completed, "Waterfall Quest")

    def test_feud_unlocks_blackjacking(self) -> None:
        state = AccountState(skills_xp={"thieving": level_to_xp(45)}, gp=0, unlocks={"members:p2p"})
        goal = Goal(target_xp={"thieving": level_to_xp(55)})

        result = self.planner.plan(state, goal)
        self.assertTrue(result.success, result.reason)

        names = [s.activity_name for s in result.steps]
        self.assertIn("The Feud", names)
        self.assertTrue(any("Blackjacking" in n for n in names), names)
        feud_i = next(i for i, n in enumerate(names) if n == "The Feud")
        bj_i = next(i for i, n in enumerate(names) if "Blackjacking" in n)
        self.assertLess(feud_i, bj_i)

    def test_ds2_unlocks_vorkath_for_gp_goal(self) -> None:
        state = AccountState(
            skills_xp={"ranged": level_to_xp(75), "attack": level_to_xp(50)},
            gp=0,
            unlocks={"members:p2p", "quest:Dragon Slayer", "quest:Legends' Quest"},
        )
        goal = Goal(target_gp=5_000_000)

        result = self.planner.plan(state, goal)
        self.assertTrue(result.success, result.reason)

        names = [s.activity_name for s in result.steps]
        self.assertIn("Dragon Slayer II", names)
        self.assertTrue(any("Vorkath" in n for n in names), names)


if __name__ == "__main__":
    unittest.main()
