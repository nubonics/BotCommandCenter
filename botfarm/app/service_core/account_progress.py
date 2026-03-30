from sqlalchemy.orm import Session

from app.models import AccountProgress
from app.planner_core.schemas import DEFAULT_SKILLS_XP, dumps_json


def get_or_create_account_progress(db: Session, account_id: int) -> AccountProgress:
    progress = (
        db.query(AccountProgress)
        .filter(AccountProgress.account_id == account_id)
        .first()
    )
    if progress:
        return progress

    progress = AccountProgress(
        account_id=account_id,
        skills_xp_json=dumps_json(DEFAULT_SKILLS_XP),
        gp=0,
        unlocks_json="[]",
        completed_quests_json="[]",
        quest_points=0,
    )
    db.add(progress)
    db.commit()
    db.refresh(progress)
    return progress