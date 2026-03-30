from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Account, Item, MoneyMaker

MAPPING_URL = "https://prices.runescape.wiki/api/v1/osrs/mapping"
LATEST_URL = "https://prices.runescape.wiki/api/v1/osrs/latest"

HEADERS = {
    "User-Agent": "OSRS Money Maker App - local development",
}


def _get_json(url: str) -> Any:
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return response.json()


def ensure_coins_item(session: Session) -> None:
    coins = session.scalars(select(Item).where(Item.osrs_id == 995)).first()

    if not coins:
        coins = Item(
            osrs_id=995,
            name="Coins",
            ge_buy_price=1,
            ge_sell_price=1,
            high_alch_value=1,
            updated_at=datetime.now(timezone.utc),
        )
        session.add(coins)
    else:
        coins.name = "Coins"
        coins.ge_buy_price = 1
        coins.ge_sell_price = 1
        coins.high_alch_value = 1
        coins.updated_at = datetime.now(timezone.utc)


def ensure_item_catalog(session: Session) -> dict[str, int]:
    mapping = _get_json(MAPPING_URL)

    existing_items = {
        item.osrs_id: item
        for item in session.scalars(select(Item)).all()
        if item.osrs_id is not None
    }

    created = 0
    updated = 0

    for entry in mapping:
        osrs_id = entry.get("id")
        if osrs_id is None:
            continue

        name = entry.get("name", f"Item {osrs_id}")
        high_alch = entry.get("highalch")

        item = existing_items.get(osrs_id)
        if item is None:
            item = Item(
                osrs_id=osrs_id,
                name=name,
                high_alch_value=high_alch,
            )
            session.add(item)
            created += 1
        else:
            changed = False

            if item.name != name:
                item.name = name
                changed = True

            if high_alch is not None and item.high_alch_value != high_alch:
                item.high_alch_value = high_alch
                changed = True

            if changed:
                updated += 1

    ensure_coins_item(session)
    session.commit()
    return {"created": created, "updated": updated}


def refresh_latest_prices(session: Session) -> dict[str, int]:
    items = session.scalars(select(Item)).all()

    if not items:
        ensure_item_catalog(session)
        items = session.scalars(select(Item)).all()

    payload = _get_json(LATEST_URL)
    data = payload.get("data", {})

    updated = 0
    now = datetime.now(timezone.utc)

    for item in items:
        if item.osrs_id is None:
            continue

        if item.osrs_id == 995:
            item.ge_buy_price = 1
            item.ge_sell_price = 1
            item.high_alch_value = 1
            item.updated_at = now
            continue

        entry = data.get(str(item.osrs_id))
        if not entry:
            continue

        changed = False

        high_price = entry.get("high")
        low_price = entry.get("low")

        if high_price is not None and item.ge_buy_price != high_price:
            item.ge_buy_price = high_price
            changed = True

        if low_price is not None and item.ge_sell_price != low_price:
            item.ge_sell_price = low_price
            changed = True

        if changed:
            item.updated_at = now
            updated += 1

    ensure_coins_item(session)
    session.commit()
    return {"updated": updated}


def refresh_selected_items(session: Session, item_ids: list[int]) -> dict[str, int]:
    if not item_ids:
        return {"updated": 0}

    payload = _get_json(LATEST_URL)
    data = payload.get("data", {})

    items = session.scalars(select(Item).where(Item.id.in_(item_ids))).all()
    updated = 0
    now = datetime.now(timezone.utc)

    for item in items:
        if item.osrs_id is None:
            continue

        if item.osrs_id == 995:
            item.ge_buy_price = 1
            item.ge_sell_price = 1
            item.high_alch_value = 1
            item.updated_at = now
            continue

        entry = data.get(str(item.osrs_id))
        if not entry:
            continue

        changed = False

        high_price = entry.get("high")
        low_price = entry.get("low")

        if high_price is not None and item.ge_buy_price != high_price:
            item.ge_buy_price = high_price
            changed = True

        if low_price is not None and item.ge_sell_price != low_price:
            item.ge_sell_price = low_price
            changed = True

        if changed:
            item.updated_at = now
            updated += 1

    ensure_coins_item(session)
    session.commit()
    return {"updated": updated}


def evaluate_money_maker(money_maker: MoneyMaker) -> dict[str, int]:
    input_cost = 0
    output_value = 0

    for component in money_maker.components:
        item = component.item
        qty = component.quantity_per_hour or 0

        if component.role == "input":
            if component.valuation_mode == "high_alch":
                price = item.high_alch_value or 0
            else:
                price = item.ge_buy_price or item.ge_sell_price or 0
            input_cost += price * qty

        elif component.role == "output":
            if component.valuation_mode == "high_alch":
                price = item.high_alch_value or 0
            else:
                price = item.ge_sell_price or item.ge_buy_price or 0
            output_value += price * qty

    profit_per_hour = output_value - input_cost

    return {
        "input_cost": input_cost,
        "output_value": output_value,
        "profit_per_hour": profit_per_hour,
    }


def refresh_money_maker_cache(session: Session, money_maker: MoneyMaker) -> dict[str, int]:
    return evaluate_money_maker(money_maker)


def import_botting_hub_accounts(session: Session, db_path: str) -> dict[str, int]:
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"Botting Hub DB not found: {db_path}")

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row

    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                c.id AS config_id,
                c.acc_id AS bh_account_id,
                c.proxy_id AS bh_proxy_id,
                c.world,
                c.status,
                c.disabled,
                c.notes AS config_notes,

                a.email_address,
                a.email_password,
                a.rs_email,
                a.rs_password,
                a.notes AS account_notes,

                p.proxy_ip,
                p.proxy_port,
                p.proxy_username,
                p.proxy_password,
                p.status AS proxy_status,
                p.renewal_status

            FROM configurations c
            JOIN accounts a ON a.id = c.acc_id
            LEFT JOIN proxies p ON p.id = c.proxy_id
            ORDER BY c.id
            """
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    created = 0
    updated = 0

    existing_by_config_id = {
        acc.botting_hub_config_id: acc
        for acc in session.scalars(
            select(Account).where(Account.botting_hub_config_id.is_not(None))
        ).all()
    }

    for row in rows:
        config_id = row["config_id"]
        bh_account_id = row["bh_account_id"]
        bh_proxy_id = row["bh_proxy_id"]

        rs_email = row["rs_email"]
        world = row["world"]

        label = rs_email or f"BH Config {config_id}"
        if world:
            label = f"{label} - W{world}"

        merged_notes = []
        if row["account_notes"]:
            merged_notes.append(f"Account notes: {row['account_notes']}")
        if row["config_notes"]:
            merged_notes.append(f"Config notes: {row['config_notes']}")
        if row["proxy_status"]:
            merged_notes.append(f"Proxy status: {row['proxy_status']}")
        if row["renewal_status"]:
            merged_notes.append(f"Proxy renewal: {row['renewal_status']}")

        notes = "\n".join(merged_notes) if merged_notes else None

        account = existing_by_config_id.get(config_id)
        if account is None:
            account = Account(
                label=label,
                email_address=row["email_address"],
                email_password=row["email_password"],
                rs_email=row["rs_email"],
                rs_password=row["rs_password"],
                rsn=None,
                proxy_ip=row["proxy_ip"],
                proxy_port=str(row["proxy_port"]) if row["proxy_port"] is not None else None,
                proxy_username=row["proxy_username"],
                proxy_password=row["proxy_password"],
                notes=notes,
                botting_hub_config_id=config_id,
                botting_hub_account_id=bh_account_id,
                botting_hub_proxy_id=bh_proxy_id,
                world=row["world"],
                status=row["status"],
                banned=False,
            )
            session.add(account)
            created += 1
        else:
            account.label = label
            account.email_address = row["email_address"]
            account.email_password = row["email_password"]
            account.rs_email = row["rs_email"]
            account.rs_password = row["rs_password"]
            account.proxy_ip = row["proxy_ip"]
            account.proxy_port = str(row["proxy_port"]) if row["proxy_port"] is not None else None
            account.proxy_username = row["proxy_username"]
            account.proxy_password = row["proxy_password"]
            account.notes = notes
            account.botting_hub_account_id = bh_account_id
            account.botting_hub_proxy_id = bh_proxy_id
            account.world = row["world"]
            account.status = row["status"]
            updated += 1

    session.commit()
    return {"created": created, "updated": updated, "total": len(rows)}


def get_osrs_usd_per_million() -> float:
    url = "https://www.food4rs.com/swap_sell.txt"
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()

        text = response.text
        start_tag = "<rs07_sell>"
        end_tag = "</rs07_sell>"

        start = text.find(start_tag)
        end = text.find(end_tag)

        if start == -1 or end == -1:
            raise ValueError("Could not find rs07_sell in swap_sell.txt")

        start += len(start_tag)
        value = text[start:end].strip()
        return float(value)
    except Exception:
        return 0.145


def gp_per_hour_to_usd_per_hour(gp_per_hour: int | float, usd_per_million: float | None = None) -> float:
    if usd_per_million is None:
        usd_per_million = get_osrs_usd_per_million()
    return (float(gp_per_hour) / 1_000_000.0) * usd_per_million