from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .planner import router as planner_router
from .progression_web import router as progression_router
from .osclient_wall.app import mount as mount_osclient_wall
from .watchdog import WatchdogConfig, watchdog_loop
from . import models  # noqa: F401
from .database import create_db_and_tables, get_session
from .models import Account, Item, MoneyMaker, MoneyMakerComponent
from .services import (
    ensure_item_catalog,
    evaluate_money_maker,
    get_osrs_usd_per_million,
    gp_per_hour_to_usd_per_hour,
    import_botting_hub_accounts,
    refresh_latest_prices,
    refresh_money_maker_cache,
    refresh_selected_items,
)

BASE_DIR = Path(__file__).resolve().parent


def format_gp(value) -> str:
    if value is None:
        return "0"

    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)

    sign = "-" if n < 0 else ""
    n = abs(n)

    if n >= 1_000_000_000:
        formatted = f"{n / 1_000_000_000:.2f}".rstrip("0").rstrip(".")
        return f"{sign}{formatted}b"
    if n >= 1_000_000:
        formatted = f"{n / 1_000_000:.2f}".rstrip("0").rstrip(".")
        return f"{sign}{formatted}m"
    if n >= 1_000:
        formatted = f"{n / 1_000:.2f}".rstrip("0").rstrip(".")
        return f"{sign}{formatted}k"

    if n.is_integer():
        return f"{sign}{int(n)}"
    return f"{sign}{n:.2f}".rstrip("0").rstrip(".")


def mask_secret(value: str | None) -> str:
    if not value:
        return "-"
    if len(value) <= 3:
        return "*" * len(value)
    return value[:1] + ("*" * (len(value) - 2)) + value[-1]


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_db_and_tables()

    # Background watchdog: tails bot log files and kills osclient.exe when a
    # "stuck in withdraw loop" pattern is detected.
    cfg = WatchdogConfig(
        logs_dir=Path(r"C:\Users\nubonix\Botting Hub\Client\Logs\Script"),
        pattern="sara*.txt",
        kill_osclient=True,
        terminate_sandbox=True,
        sandboxie_start_exe="Start.exe",
    )
    watchdog_task = asyncio.create_task(watchdog_loop(cfg))

    try:
        yield
    finally:
        watchdog_task.cancel()
        with contextlib.suppress(Exception):
            await watchdog_task


app = FastAPI(title="BotFarmPlanner", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["gp"] = format_gp
templates.env.filters["mask_secret"] = mask_secret

app.state.templates = templates
app.include_router(planner_router)
app.include_router(progression_router)

# OSClient Wall (dashboard)
mount_osclient_wall(app, prefix="/wall")




def format_usd(value) -> str:
    if value is None:
        return "$0.00"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"${n:,.2f}"


templates.env.filters["usd"] = format_usd


# --- Back-compat redirects for earlier /planner/* routes ---
@app.get("/planner")
def planner_root_compat_redirect():
    return RedirectResponse(url="/planner_core/plans", status_code=307)


@app.get("/planner/plans")
def planner_plans_compat_redirect():
    return RedirectResponse(url="/planner_core/plans", status_code=307)


@app.get("/planner/plans/new")
def planner_new_plan_compat_redirect():
    return RedirectResponse(url="/planner_core/plans/new", status_code=307)


@app.get("/planner/plans/{plan_id}")
def planner_plan_detail_compat_redirect(plan_id: int):
    return RedirectResponse(url=f"/planner_core/plans/{plan_id}", status_code=307)


@app.get("/planner/plans/{plan_id}/edit")
def planner_plan_edit_compat_redirect(plan_id: int):
    return RedirectResponse(url=f"/planner_core/plans/{plan_id}/edit", status_code=307)


@app.get("/planner/tasks")
def planner_tasks_compat_redirect():
    return RedirectResponse(url="/planner_core/tasks", status_code=307)


@app.get("/planner/tasks/new")
def planner_new_task_compat_redirect():
    return RedirectResponse(url="/planner_core/tasks/new", status_code=307)


@app.get("/planner/tasks/{task_id}/edit")
def planner_task_edit_compat_redirect(task_id: int):
    return RedirectResponse(url=f"/planner_core/tasks/{task_id}/edit", status_code=307)


@app.get("/planner/assignments")
def planner_assignments_compat_redirect():
    return RedirectResponse(url="/planner_core/assignments", status_code=307)


@app.get("/planner/generate")
def planner_generate_compat_redirect():
    return RedirectResponse(url="/planner_core/generate", status_code=307)


# --- Main app ---
@app.get("/", response_class=HTMLResponse)
def dashboard(
        request: Request,
        members: str = "all",
        sort: str = "name",
        direction: str = "asc",
        session: Session = Depends(get_session),
):
    money_makers = session.scalars(select(MoneyMaker).order_by(MoneyMaker.name)).all()

    if members == "f2p":
        money_makers = [m for m in money_makers if not m.is_members]
    elif members == "p2p":
        money_makers = [m for m in money_makers if m.is_members]

    usd_per_million = get_osrs_usd_per_million()

    rows = []
    for money_maker in money_makers:
        summary = evaluate_money_maker(money_maker)
        usd_profit_per_hour = gp_per_hour_to_usd_per_hour(
            summary["profit_per_hour"],
            usd_per_million=usd_per_million,
        )
        rows.append(
            {
                "money_maker": money_maker,
                "summary": summary,
                "usd_profit_per_hour": usd_profit_per_hour,
            }
        )

    reverse = direction == "desc"

    if sort == "profit":
        rows.sort(key=lambda r: r["summary"]["profit_per_hour"], reverse=reverse)
    elif sort == "usd_profit":
        rows.sort(key=lambda r: r["usd_profit_per_hour"], reverse=reverse)
    else:
        rows.sort(key=lambda r: r["money_maker"].name.lower(), reverse=reverse)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "request": request,
            "rows": rows,
            "members": members,
            "sort": sort,
            "direction": direction,
            "usd_per_million": usd_per_million,
            "message": request.query_params.get("message"),
        },
    )


@app.get("/item-search")
def item_search(q: str = "", session: Session = Depends(get_session)):
    q = q.strip()
    if len(q) < 2:
        return []

    items = session.scalars(
        select(Item)
        .where(Item.name.ilike(f"%{q}%"))
        .order_by(Item.name)
        .limit(25)
    ).all()

    return [{"id": item.id, "name": item.name, "osrs_id": item.osrs_id} for item in items]


@app.post("/refresh-items")
def refresh_items(session: Session = Depends(get_session)):
    if not session.scalars(select(Item).limit(1)).first():
        ensure_item_catalog(session)

    result = refresh_latest_prices(session)
    message = f"Refreshed latest prices for {result['updated']} items."
    return RedirectResponse(url=f"/items?message={message}", status_code=303)


@app.get("/items", response_class=HTMLResponse)
def list_items(request: Request, q: str = "", session: Session = Depends(get_session)):
    q = (q or "").strip()

    # Performance/UX: don't load the entire catalog by default.
    # The OSRS mapping table is large; we only fetch items when searching.
    items: list[Item] = []

    if q:
        # Basic guardrails to avoid accidental huge scans.
        if len(q) < 2:
            items = []
        else:
            # Prefer prefix match for short queries; contains match for longer ones.
            if len(q) < 4:
                statement = select(Item).where(Item.name.ilike(f"{q}%")).order_by(Item.name)
            else:
                statement = select(Item).where(Item.name.ilike(f"%{q}%")).order_by(Item.name)
            items = session.scalars(statement.limit(150)).all()

    return templates.TemplateResponse(
        request,
        "items.html",
        {
            "request": request,
            "items": items,
            "q": q,
            "message": request.query_params.get("message"),
        },
    )


@app.post("/items/{item_id}/refresh")
def refresh_item(item_id: int, session: Session = Depends(get_session)):
    item = session.get(Item, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    refresh_selected_items(session, [item_id])
    return RedirectResponse(url=f"/items/{item_id}?message=Item refreshed", status_code=303)


@app.get("/items/{item_id}", response_class=HTMLResponse)
def item_detail(request: Request, item_id: int, session: Session = Depends(get_session)):
    item = session.get(Item, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    return templates.TemplateResponse(
        request,
        "item_detail.html",
        {
            "request": request,
            "item": item,
            "message": request.query_params.get("message"),
        },
    )


@app.get("/money-makers/new", response_class=HTMLResponse)
def new_money_maker_form(request: Request, session: Session = Depends(get_session)):
    if not session.scalars(select(Item).limit(1)).first():
        ensure_item_catalog(session)

    return templates.TemplateResponse(
        request,
        "money_maker_form.html",
        {
            "request": request,
            "items": [],
            "money_maker": None,
            "components": [],
            "message": request.query_params.get("message"),
        },
    )


@app.post("/money-makers/new")
def create_money_maker(
    name: str = Form(...),
    category: str = Form(...),
    is_members: str = Form("p2p"),
    units_per_hour: int = Form(...),
    notes: str = Form(""),
    session: Session = Depends(get_session),
):
    cleaned_name = name.strip()
    if not cleaned_name:
        raise HTTPException(status_code=400, detail="Name is required")

    money_maker = MoneyMaker(
        name=cleaned_name,
        category=category.strip() or "processing",
        is_members=(is_members == "p2p"),
        units_per_hour=units_per_hour,
        notes=notes.strip() or None,
    )
    session.add(money_maker)
    session.commit()
    session.refresh(money_maker)

    return RedirectResponse(
        url=f"/money-makers/{money_maker.id}?message=Money maker created",
        status_code=303,
    )


@app.get("/money-makers/{money_maker_id}", response_class=HTMLResponse)
def money_maker_detail(request: Request, money_maker_id: int, session: Session = Depends(get_session)):
    money_maker = session.get(MoneyMaker, money_maker_id)
    if not money_maker:
        raise HTTPException(status_code=404, detail="Money maker not found")

    summary = evaluate_money_maker(money_maker)

    return templates.TemplateResponse(
        request,
        "money_maker_detail.html",
        {
            "request": request,
            "money_maker": money_maker,
            "items": [],
            "summary": summary,
            "message": request.query_params.get("message"),
        },
    )


@app.post("/money-makers/{money_maker_id}/refresh")
def refresh_money_maker(money_maker_id: int, session: Session = Depends(get_session)):
    money_maker = session.get(MoneyMaker, money_maker_id)
    if not money_maker:
        raise HTTPException(status_code=404, detail="Money maker not found")

    item_ids = [component.item_id for component in money_maker.components]
    if item_ids:
        refresh_selected_items(session, item_ids)

    refresh_money_maker_cache(session, money_maker)
    return RedirectResponse(
        url=f"/money-makers/{money_maker_id}?message=Money maker refreshed",
        status_code=303,
    )


@app.post("/money-makers/{money_maker_id}/components")
def add_component(
        money_maker_id: int,
        item_id: int = Form(...),
        role: str = Form(...),
        quantity_per_hour: int = Form(...),
        valuation_mode: str = Form("market"),
        notes: str = Form(""),
        session: Session = Depends(get_session),
):
    money_maker = session.get(MoneyMaker, money_maker_id)
    item = session.get(Item, item_id)
    if not money_maker or not item:
        raise HTTPException(status_code=404, detail="Money maker or item not found")

    if role not in {"input", "output"}:
        raise HTTPException(status_code=400, detail="Invalid component role")

    if valuation_mode not in {"market", "high_alch"}:
        raise HTTPException(status_code=400, detail="Invalid valuation mode")

    component = MoneyMakerComponent(
        money_maker_id=money_maker_id,
        item_id=item_id,
        role=role,
        quantity_per_hour=quantity_per_hour,
        valuation_mode=valuation_mode,
        notes=notes.strip() or None,
    )
    session.add(component)
    session.commit()

    money_maker = session.get(MoneyMaker, money_maker_id)
    if money_maker:
        refresh_money_maker_cache(session, money_maker)

    return RedirectResponse(
        url=f"/money-makers/{money_maker_id}?message=Component added",
        status_code=303,
    )


@app.post("/components/{component_id}/delete")
def delete_component(component_id: int, session: Session = Depends(get_session)):
    component = session.get(MoneyMakerComponent, component_id)
    if not component:
        raise HTTPException(status_code=404, detail="Component not found")

    money_maker_id = component.money_maker_id
    session.delete(component)
    session.commit()

    money_maker = session.get(MoneyMaker, money_maker_id)
    if money_maker:
        refresh_money_maker_cache(session, money_maker)

    return RedirectResponse(
        url=f"/money-makers/{money_maker_id}?message=Component deleted",
        status_code=303,
    )


@app.get("/accounts", response_class=HTMLResponse)
def list_accounts(request: Request, q: str = "", session: Session = Depends(get_session)):
    statement = select(Account).order_by(Account.label)
    if q.strip():
        like = f"%{q.strip()}%"
        statement = (
            select(Account)
            .where(
                or_(
                    Account.label.ilike(like),
                    Account.email_address.ilike(like),
                    Account.rs_email.ilike(like),
                    Account.proxy_ip.ilike(like),
                )
            )
            .order_by(Account.label)
        )

    accounts = session.scalars(statement).all()
    return templates.TemplateResponse(
        request,
        "accounts.html",
        {
            "request": request,
            "accounts": accounts,
            "q": q,
            "message": request.query_params.get("message"),
        },
    )


@app.post("/accounts/import-botting-hub")
def import_accounts_from_botting_hub(
        db_path: str = Form(...),
        session: Session = Depends(get_session),
):
    result = import_botting_hub_accounts(session, db_path)
    message = (
        f"Imported Botting Hub accounts. "
        f"Created {result['created']}, updated {result['updated']}, total {result['total']}."
    )
    return RedirectResponse(url=f"/accounts?message={message}", status_code=303)


@app.get("/accounts/new", response_class=HTMLResponse)
def new_account_form(request: Request):
    return templates.TemplateResponse(
        request,
        "account_form.html",
        {
            "request": request,
            "account": None,
            "message": request.query_params.get("message"),
        },
    )


@app.post("/accounts/new")
def create_account(
    label: str = Form(...),
    email_address: str = Form(""),
    email_password: str = Form(""),
    rs_email: str = Form(""),
    rs_password: str = Form(""),
    rsn: str = Form(""),
    proxy_ip: str = Form(""),
    proxy_port: str = Form(""),
    proxy_username: str = Form(""),
    proxy_password: str = Form(""),
    banned: str = Form("false"),
    notes: str = Form(""),
    session: Session = Depends(get_session),
):
    cleaned_label = label.strip()
    if not cleaned_label:
        raise HTTPException(status_code=400, detail="Label is required")

    account = Account(
        label=cleaned_label,
        email_address=email_address.strip() or None,
        email_password=email_password.strip() or None,
        rs_email=rs_email.strip() or None,
        rs_password=rs_password.strip() or None,
        rsn=rsn.strip() or None,
        proxy_ip=proxy_ip.strip() or None,
        proxy_port=proxy_port.strip() or None,
        proxy_username=proxy_username.strip() or None,
        proxy_password=proxy_password.strip() or None,
        banned=(banned == "true"),
        notes=notes.strip() or None,
    )
    session.add(account)
    session.commit()
    session.refresh(account)

    return RedirectResponse(url=f"/accounts/{account.id}?message=Account created", status_code=303)


@app.get("/accounts/{account_id}", response_class=HTMLResponse)
def account_detail(request: Request, account_id: int, session: Session = Depends(get_session)):
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    return templates.TemplateResponse(
        request,
        "account_detail.html",
        {
            "request": request,
            "account": account,
            "message": request.query_params.get("message"),
        },
    )


@app.get("/accounts/{account_id}/edit", response_class=HTMLResponse)
def edit_account_form(request: Request, account_id: int, session: Session = Depends(get_session)):
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    return templates.TemplateResponse(
        request,
        "account_form.html",
        {
            "request": request,
            "account": account,
            "message": request.query_params.get("message"),
        },
    )


@app.post("/accounts/{account_id}/edit")
def update_account(
    account_id: int,
    label: str = Form(...),
    email_address: str = Form(""),
    email_password: str = Form(""),
    rs_email: str = Form(""),
    rs_password: str = Form(""),
    rsn: str = Form(""),
    proxy_ip: str = Form(""),
    proxy_port: str = Form(""),
    proxy_username: str = Form(""),
    proxy_password: str = Form(""),
    banned: str = Form("false"),
    notes: str = Form(""),
    session: Session = Depends(get_session),
):
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    cleaned_label = label.strip()
    if not cleaned_label:
        raise HTTPException(status_code=400, detail="Label is required")

    account.label = cleaned_label
    account.email_address = email_address.strip() or None
    account.email_password = email_password.strip() or None
    account.rs_email = rs_email.strip() or None
    account.rs_password = rs_password.strip() or None
    account.rsn = rsn.strip() or None
    account.proxy_ip = proxy_ip.strip() or None
    account.proxy_port = proxy_port.strip() or None
    account.proxy_username = proxy_username.strip() or None
    account.proxy_password = proxy_password.strip() or None
    account.banned = (banned == "true")
    account.notes = notes.strip() or None

    session.commit()

    return RedirectResponse(url=f"/accounts/{account.id}?message=Account updated", status_code=303)


@app.post("/accounts/{account_id}/delete")
def delete_account(account_id: int, session: Session = Depends(get_session)):
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    session.delete(account)
    session.commit()

    return RedirectResponse(url="/accounts?message=Account deleted", status_code=303)


@app.post("/money-makers/{money_maker_id}/edit")
def update_money_maker(
    money_maker_id: int,
    name: str = Form(...),
    category: str = Form(...),
    is_members: str = Form("p2p"),
    units_per_hour: int = Form(...),
    notes: str = Form(""),
    session: Session = Depends(get_session),
):
    money_maker = session.get(MoneyMaker, money_maker_id)
    if not money_maker:
        raise HTTPException(status_code=404, detail="Money maker not found")

    cleaned_name = name.strip()
    if not cleaned_name:
        raise HTTPException(status_code=400, detail="Name is required")

    money_maker.name = cleaned_name
    money_maker.category = category.strip() or "processing"
    money_maker.is_members = (is_members == "p2p")
    money_maker.units_per_hour = units_per_hour
    money_maker.notes = notes.strip() or None

    session.commit()
    session.refresh(money_maker)

    return RedirectResponse(
        url=f"/money-makers/{money_maker.id}?message=Money maker updated",
        status_code=303,
    )


@app.get("/money-makers/{money_maker_id}/edit", response_class=HTMLResponse)
def edit_money_maker_form(request: Request, money_maker_id: int, session: Session = Depends(get_session)):
    money_maker = session.get(MoneyMaker, money_maker_id)
    if not money_maker:
        raise HTTPException(status_code=404, detail="Money maker not found")

    return templates.TemplateResponse(
        request,
        "money_maker_form.html",
        {
            "request": request,
            "money_maker": money_maker,
            "components": money_maker.components,
            "message": request.query_params.get("message"),
        },
    )


@app.post("/money-makers/{money_maker_id}/delete")
def delete_money_maker(money_maker_id: int, session: Session = Depends(get_session)):
    money_maker = session.get(MoneyMaker, money_maker_id)
    if not money_maker:
        raise HTTPException(status_code=404, detail="Money maker not found")

    session.delete(money_maker)
    session.commit()

    return RedirectResponse(url="/?message=Money maker deleted", status_code=303)
