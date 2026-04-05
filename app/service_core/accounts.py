import json

from sqlalchemy.orm import Session

from app.models import Account, AccountProgress
from app.planner_core.schemas import DEFAULT_SKILLS_XP, dumps_json


def create_account(
    db: Session,
    *,
    label: str,
    email_address: str | None = None,
    email_password: str | None = None,
    rs_email: str | None = None,
    rs_password: str | None = None,
    rsn: str | None = None,
    proxy_ip: str | None = None,
    proxy_port: str | None = None,
    proxy_username: str | None = None,
    proxy_password: str | None = None,
    notes: str | None = None,
) -> Account:
    account = Account(
        label=label,
        email_address=email_address,
        email_password=email_password,
        rs_email=rs_email,
        rs_password=rs_password,
        rsn=rsn,
        proxy_ip=proxy_ip,
        proxy_port=proxy_port,
        proxy_username=proxy_username,
        proxy_password=proxy_password,
        notes=notes,
    )
    db.add(account)
    db.flush()

    progress = AccountProgress(
        account_id=account.id,
        skills_xp_json=dumps_json(DEFAULT_SKILLS_XP),
        gp=0,
        unlocks_json="[]",
        completed_quests_json="[]",
        quest_points=0,
    )
    db.add(progress)
    db.commit()
    db.refresh(account)

    return account