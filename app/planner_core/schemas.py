import json
from typing import Any

DEFAULT_SKILLS_XP = {
    "attack": 0,
    "strength": 0,
    "defence": 0,
    "hitpoints": 1154,
    "ranged": 0,
    "prayer": 0,
    "magic": 0,
    "cooking": 0,
    "woodcutting": 0,
    "fletching": 0,
    "fishing": 0,
    "firemaking": 0,
    "crafting": 0,
    "smithing": 0,
    "mining": 0,
    "herblore": 0,
    "agility": 0,
    "thieving": 0,
    "slayer": 0,
    "farming": 0,
    "runecraft": 0,
    "hunter": 0,
    "construction": 0,
}

VALID_SKILL_NAMES = set(DEFAULT_SKILLS_XP.keys())


def normalize_skills_xp(data: Any) -> dict[str, int]:
    if data is None:
        return DEFAULT_SKILLS_XP.copy()

    if isinstance(data, str):
        data = json.loads(data)

    if not isinstance(data, dict):
        raise ValueError("skills_xp_json must be a dict")

    normalized = DEFAULT_SKILLS_XP.copy()

    for skill_name, xp_value in data.items():
        if skill_name not in VALID_SKILL_NAMES:
            raise ValueError(f"Invalid skill name: {skill_name}")

        if not isinstance(xp_value, int):
            raise ValueError(f"XP for {skill_name} must be an int")

        if xp_value < 0:
            raise ValueError(f"XP for {skill_name} cannot be negative")

        normalized[skill_name] = xp_value

    return normalized


def normalize_unlocks(data: Any) -> list[str]:
    if data is None:
        return []

    if isinstance(data, str):
        data = json.loads(data)

    if not isinstance(data, list):
        raise ValueError("unlocks_json must be a list")

    cleaned: list[str] = []
    seen: set[str] = set()

    for value in data:
        if not isinstance(value, str):
            raise ValueError("All unlock values must be strings")

        value = value.strip().lower()
        if not value:
            continue

        if value not in seen:
            seen.add(value)
            cleaned.append(value)

    return cleaned


def normalize_completed_quests(data: Any) -> list[str]:
    if data is None:
        return []

    if isinstance(data, str):
        data = json.loads(data)

    if not isinstance(data, list):
        raise ValueError("completed_quests_json must be a list")

    cleaned: list[str] = []
    seen: set[str] = set()

    for value in data:
        if not isinstance(value, str):
            raise ValueError("All completed quest values must be strings")

        value = value.strip().lower()
        if not value:
            continue

        if value not in seen:
            seen.add(value)
            cleaned.append(value)

    return cleaned


def dumps_json(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"), sort_keys=True)