from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import httpx
from bs4 import BeautifulSoup

ROOT = "https://oldschool.runescape.wiki"

URLS = {
    "by_skill": f"{ROOT}/w/Quests/Requirements_by_skill",
    "by_quest": f"{ROOT}/w/Quests/Requirements_by_quest",
    "list": f"{ROOT}/w/Quests/List",
}


UA = {"User-Agent": "BotFarmPlanner quest builder"}


def fetch(url: str) -> str:
    print(f"fetch: {url}")
    r = httpx.get(url, timeout=60, headers=UA)
    r.raise_for_status()
    return r.text


def fetch_wikitext_qp(client: httpx.Client, quest_name: str) -> int:
    """Fetch quest points from the quest page wikitext via MediaWiki API."""

    import urllib.parse

    page = quest_name.replace(" ", "_")
    url = (
        f"{ROOT}/api.php?action=parse&page={urllib.parse.quote(page)}&prop=wikitext&format=json"
    )

    r = client.get(url, timeout=60, headers=UA)
    r.raise_for_status()
    text = r.text

    # Most quest pages use |qp = N
    m = re.search(r"\|\s*qp\s*=\s*(\d+)", text, re.I)
    if m:
        return int(m.group(1))

    # Some pages might use quest_points.
    m = re.search(r"\|\s*quest_points\s*=\s*(\d+)", text, re.I)
    if m:
        return int(m.group(1))

    return 0


def slugify(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


SKILL_NAMES = {
    "attack",
    "strength",
    "defence",
    "hitpoints",
    "ranged",
    "prayer",
    "magic",
    "runecraft",
    "construction",
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
}

SKILL_ALIASES = {
    "runecrafting": "runecraft",
}


def normalize_skill(name: str) -> str | None:
    key = name.strip().lower().replace(" ", "_")
    key = SKILL_ALIASES.get(key, key)
    return key if key in SKILL_NAMES else None


def parse_requirements_by_skill(html: str) -> dict[str, dict[str, int]]:
    """Return quest_name -> {skill: level}."""

    soup = BeautifulSoup(html, "lxml")

    out: dict[str, dict[str, int]] = {}

    # The page is organized into skill sections with an <h3> heading followed by a wikitable.
    for h3 in soup.find_all("h3"):
        skill = normalize_skill(h3.get_text(" ", strip=True))
        if not skill:
            continue

        table = h3.find_next("table", class_=lambda c: c and "wikitable" in c)
        if not table:
            continue

        # Expect rows: Quest | Level (sometimes notes)
        for tr in table.select("tr"):
            tds = tr.find_all(["td", "th"])
            if len(tds) < 2:
                continue

            quest_name = tds[0].get_text(" ", strip=True)
            if quest_name.lower() in {"quest", "questname"}:
                continue

            level_text = tds[1].get_text(" ", strip=True)
            m = re.search(r"(\d+)", level_text)
            if not m:
                continue
            level = int(m.group(1))

            out.setdefault(quest_name, {})
            prev = out[quest_name].get(skill, 0)
            out[quest_name][skill] = max(prev, level)

    return out


def parse_requirements_by_quest_prereqs(html: str) -> dict[str, set[str]]:
    """Return quest_name -> set(prereq_quest_names)."""

    soup = BeautifulSoup(html, "lxml")

    prereqs: dict[str, set[str]] = {}

    # There are multiple tables with header Questname / Requirements
    for table in soup.select("table.wikitable"):
        headers = [th.get_text(" ", strip=True).lower() for th in table.select("tr th")]
        if not any("quest" in h for h in headers) or not any("require" in h for h in headers):
            continue

        for tr in table.select("tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue

            quest_name = tds[0].get_text(" ", strip=True)
            req_cell = tds[1]

            # Collect linked page titles from requirements cell; filter out skills and obvious non-quests.
            names: set[str] = set()
            for a in req_cell.select("a"):
                title = (a.get("title") or "").strip()
                if not title:
                    continue
                tnorm = title.strip()

                # Drop skills.
                if normalize_skill(tnorm):
                    continue

                # Drop generic pages.
                if tnorm.lower() in {"quest point", "quest points", "combat"}:
                    continue

                # Heuristic: keep only things that look like quest/miniquest names.
                # (Wiki uses lots of links; this isn't perfect but is good enough to get a working graph.)
                if ":" in tnorm:
                    continue

                names.add(tnorm)

            if names:
                prereqs.setdefault(quest_name, set()).update(names)

    return prereqs


def parse_quest_list(html: str) -> dict[str, dict[str, object]]:
    """Return quest_name -> {is_miniquest: bool} from Quests/List."""

    soup = BeautifulSoup(html, "lxml")
    out: dict[str, dict[str, object]] = {}

    mode = "p2p"  # f2p|p2p|miniquest

    # Walk nodes in document order; switch mode when we enter sections.
    content = soup.select_one("div.mw-parser-output") or soup
    for node in content.select("h2, h3, table.wikitable"):
        if node.name in {"h2", "h3"}:
            title = node.get_text(" ", strip=True).lower()
            if "miniquest" in title:
                mode = "miniquest"
            elif "free-to-play" in title:
                mode = "f2p"
            elif "members" in title:
                mode = "p2p"
            continue

        header_row = node.select_one("tr")
        if not header_row:
            continue
        headers = [th.get_text(" ", strip=True).lower() for th in header_row.select("th")]
        if "name" not in headers:
            continue

        name_idx = headers.index("name")
        for tr in node.select("tr")[1:]:
            tds = tr.select("td")
            if len(tds) <= name_idx:
                continue
            nm = tds[name_idx].get_text(" ", strip=True)
            if not nm:
                continue
            out[nm] = {
                "is_miniquest": (mode == "miniquest"),
                "is_members": (mode != "f2p"),
            }

    # Fallback: if the above missed tables, do a broad pass.
    if not out:
        for table in soup.select("table.wikitable"):
            header_row = table.select_one("tr")
            if not header_row:
                continue
            headers = [th.get_text(" ", strip=True).lower() for th in header_row.select("th")]
            if "name" not in headers:
                continue
            name_idx = headers.index("name")
            for tr in table.select("tr")[1:]:
                tds = tr.select("td")
                if len(tds) <= name_idx:
                    continue
                nm = tds[name_idx].get_text(" ", strip=True)
                if nm and nm not in out:
                    out[nm] = {"is_miniquest": False, "is_members": False}

    return out


def build_quests_json(
    skill_reqs: dict[str, dict[str, int]],
    quest_reqs: dict[str, set[str]],
    quest_meta: dict[str, dict[str, object]],
) -> list[dict]:
    all_quests = sorted(set(skill_reqs) | set(quest_reqs) | set(quest_meta))

    out: list[dict] = []
    for name in all_quests:
        min_levels = skill_reqs.get(name, {})
        prereq_quests = sorted(quest_reqs.get(name, set()))

        required_unlocks = [f"quest:{q}" for q in prereq_quests]

        meta = quest_meta.get(name) or {}
        is_miniquest = bool(meta.get("is_miniquest", False))
        is_members = bool(meta.get("is_members", True))

        out.append(
            {
                "id": f"quest_{slugify(name)}",
                "name": name,
                "is_miniquest": is_miniquest,
                "is_members": is_members,
                "category": "quest",
                "repeatable": False,
                "one_time": True,
                "duration_hours": 1.0,
                "quest_name": name,
                "requirements": {
                    "min_levels": min_levels,
                    "required_unlocks": required_unlocks,
                }
                if (min_levels or required_unlocks)
                else {},
                "reward": {
                    "quest_points": 0,
                },
                "notes": "Generated from OSRS Wiki requirements tables.",
            }
        )

    return out


def main() -> None:
    by_skill_html = fetch(URLS["by_skill"])
    by_quest_html = fetch(URLS["by_quest"])
    list_html = fetch(URLS["list"])

    skill_reqs = parse_requirements_by_skill(by_skill_html)
    quest_prereqs = parse_requirements_by_quest_prereqs(by_quest_html)
    quest_meta = parse_quest_list(list_html)

    quests = build_quests_json(skill_reqs, quest_prereqs, quest_meta)

    data_dir = Path(__file__).resolve().parents[1] / "app" / "progression_planner" / "data"
    qp_cache_path = data_dir / "quest_points.json"

    qp_cache: dict[str, int] = {}
    if qp_cache_path.exists():
        try:
            qp_cache = {str(k): int(v) for k, v in json.loads(qp_cache_path.read_text(encoding="utf-8")).items()}
        except Exception:
            qp_cache = {}

    # Fill quest points for quests (miniquests remain 0).
    # This can take a bit on first run; we cache results.
    with httpx.Client() as client:
        for q in quests:
            if q.get("is_miniquest"):
                continue
            name = str(q.get("name") or "")
            if not name:
                continue

            if name not in qp_cache:
                try:
                    qp_cache[name] = fetch_wikitext_qp(client, name)
                except Exception:
                    qp_cache[name] = 0

            qp = int(qp_cache.get(name, 0) or 0)
            q.setdefault("reward", {})
            if isinstance(q["reward"], dict):
                q["reward"]["quest_points"] = qp

    qp_cache_path.write_text(json.dumps(qp_cache, indent=2, sort_keys=True), encoding="utf-8")

    out_path = data_dir / "quests.wiki.json"
    out_path.write_text(json.dumps(quests, indent=2, sort_keys=False), encoding="utf-8")

    print(f"wrote {out_path} ({len(quests)} quests)")
    print(f"wrote {qp_cache_path} ({len(qp_cache)} qp entries)")


if __name__ == "__main__":
    main()
