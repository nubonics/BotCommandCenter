from __future__ import annotations

import math


SKILLS: list[str] = [
    "attack",
    "strength",
    "defence",
    "ranged",
    "prayer",
    "magic",
    "runecraft",
    "construction",
    "hitpoints",
    "agility",
    "herblore",
    "thieving",
    "crafting",
    "fletching",
    "slayer",
    "hunter",
    "mining",
    "smithing",
    "fishing",
    "cooking",
    "firemaking",
    "woodcutting",
    "farming",
    "sailing",
]

# Alias mapping for highscores / UI labels.
SKILL_ALIASES: dict[str, str] = {
    "hp": "hitpoints",
    "hitpoints": "hitpoints",
    "rc": "runecraft",
    "runecrafting": "runecraft",
}



def normalize_skill(skill: str) -> str:
    return str(skill).strip().lower().replace(" ", "_")


def level_to_xp(level: int) -> int:
    """
    OSRS-like XP curve:
      xp = floor( 1/4 * sum_{l=1..L-1} floor(l + 300*2^(l/7)) )

    Level 1 => 0 XP.
    """
    if level <= 1:
        return 0

    points = 0
    for l in range(1, level):
        points += math.floor(l + 300 * 2 ** (l / 7))
    return math.floor(points / 4)


def xp_to_level(xp: int, max_level: int = 99) -> int:
    if xp <= 0:
        return 1
    for level in range(1, max_level):
        if xp < level_to_xp(level + 1):
            return level
    return max_level


def levels_to_xp(levels: dict[str, int]) -> dict[str, int]:
    return {normalize_skill(k): level_to_xp(int(v)) for k, v in levels.items()}
