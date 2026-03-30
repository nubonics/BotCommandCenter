from __future__ import annotations

import json
from pathlib import Path

from .activities import _parse_activity


DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_PATH = DATA_DIR / "quests.wiki.json"


from functools import lru_cache


def load_quests(path: str | Path | None = None) -> list:
    """Load quest activities.

    We default to a large, wiki-derived requirements dataset (quests.wiki.json) and
    then apply a small set of hand-maintained overrides (quests.sample.json) to
    provide rewards/unlocks/durations for important quests.

    Uses caching when path is None.
    """

    if path is None:
        return list(_load_quests_cached())

    raw = _load_quests_raw(Path(path))
    return [_parse_activity(item) for item in raw]


def load_quests_raw(path: str | Path | None = None) -> list[dict]:
    """Load raw quest dicts (for UI/meta)."""

    p = Path(path) if path else DEFAULT_PATH
    return _load_quests_raw(p)


@lru_cache(maxsize=8)
def _load_quests_raw(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("Quests JSON must be a list")

    # Apply overrides (if present) to keep names/ids in sync.
    override_path = DATA_DIR / "quests.sample.json"
    if override_path.exists():
        overrides = json.loads(override_path.read_text(encoding="utf-8"))
        if isinstance(overrides, list):
            by_name = {str(item.get("name")): item for item in raw if isinstance(item, dict) and item.get("name")}
            for ov in overrides:
                if not isinstance(ov, dict) or not ov.get("name"):
                    continue
                name = str(ov["name"])
                base = by_name.get(name)
                if base is None:
                    raw.append(ov)
                    by_name[name] = ov
                else:
                    merged = dict(base)
                    merged.update(ov)
                    idx = raw.index(base)
                    raw[idx] = merged

    # Ensure dict type
    cleaned = [x for x in raw if isinstance(x, dict)]

    # Mark all quests as manual activities.
    for item in cleaned:
        item.setdefault("manual", True)

    return cleaned


def load_quests_overrides() -> list:
    """Load only the hand-maintained quest overrides (fast, curated set).

    This is useful for planning, where the full wiki-derived quest list is too
    large/slow and mostly has no modeled XP/unlock rewards.
    """

    override_path = DATA_DIR / "quests.sample.json"
    if not override_path.exists():
        return []

    raw = json.loads(override_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("quests.sample.json must be a list")

    # Ensure override quests are marked manual.
    for item in raw:
        if isinstance(item, dict):
            item.setdefault("manual", True)

    return [_parse_activity(item) for item in raw if isinstance(item, dict)]


@lru_cache(maxsize=2)
def _load_quests_cached() -> tuple:
    raw = _load_quests_raw(DEFAULT_PATH)
    return tuple(_parse_activity(item) for item in raw)

