from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = "sqlite:///./osrs_money_makers.db"


class Base(DeclarativeBase):
    pass


engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_db_and_tables():
    Base.metadata.create_all(bind=engine)
    _ensure_account_schema()
    _ensure_account_expense_schema()


def _ensure_account_schema() -> None:
    with engine.begin() as conn:
        try:
            rows = conn.exec_driver_sql("PRAGMA table_info(account)").mappings().all()
        except Exception:
            return

        if not rows:
            return

        columns = {str(row.get("name")) for row in rows}

        if "tags" not in columns:
            conn.exec_driver_sql(
                "ALTER TABLE account ADD COLUMN tags VARCHAR(500)"
            )


def _ensure_account_expense_schema() -> None:
    """Best-effort additive schema updates for existing SQLite installs.

    This project currently boots straight from SQLAlchemy metadata without
    automatically running Alembic migrations, so additive columns for older
    databases need a lightweight compatibility path.
    """
    with engine.begin() as conn:
        try:
            rows = conn.exec_driver_sql("PRAGMA table_info(account_expense)").mappings().all()
        except Exception:
            return

        if not rows:
            return

        columns = {str(row.get("name")) for row in rows}

        if "allocation_scope" not in columns:
            conn.exec_driver_sql(
                "ALTER TABLE account_expense ADD COLUMN allocation_scope VARCHAR(20) NOT NULL DEFAULT 'account'"
            )
        if "allocation_group" not in columns:
            conn.exec_driver_sql(
                "ALTER TABLE account_expense ADD COLUMN allocation_group VARCHAR(64)"
            )
        if "allocation_tag" not in columns:
            conn.exec_driver_sql(
                "ALTER TABLE account_expense ADD COLUMN allocation_tag VARCHAR(255)"
            )
        if "source_amount_usd" not in columns:
            conn.exec_driver_sql(
                "ALTER TABLE account_expense ADD COLUMN source_amount_usd NUMERIC(10, 2)"
            )
        if "allocated_account_count" not in columns:
            conn.exec_driver_sql(
                "ALTER TABLE account_expense ADD COLUMN allocated_account_count INTEGER"
            )

        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_account_expense_allocation_group ON account_expense (allocation_group)"
        )

        conn.exec_driver_sql(
            "UPDATE account_expense "
            "SET allocation_scope = COALESCE(allocation_scope, 'account'), "
            "source_amount_usd = COALESCE(source_amount_usd, amount_usd), "
            "allocated_account_count = COALESCE(allocated_account_count, 1)"
        )
