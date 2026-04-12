from __future__ import annotations

import asyncio
import contextlib
import os
import time
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
import subprocess
import threading
import uuid

import psutil

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .planner import router as planner_router
from .progression_web import router as progression_router
from .osclient_wall.router import (
    include_wall as include_osclient_wall,
    start_wall as start_osclient_wall,
    stop_wall as stop_osclient_wall,
)
from .window_spreader import get_spreader
from .watchdog import WATCHDOG_STATUS, WatchdogConfig, update_watchdog_config, watchdog_loop
from . import models  # noqa: F401
from .database import create_db_and_tables, get_session
from .models import Account, AccountExpense, Item, MoneyMaker, MoneyMakerComponent
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


# --- Repo self-update (dev / git) ---
_UPDATE_LOCK = threading.Lock()


def _repo_root() -> Path:
    # app/ lives under the repo root.
    return BASE_DIR.parent


def _run_git(args: list[str], timeout: float = 8.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(_repo_root()),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return int(proc.returncode), (proc.stdout or ""), (proc.stderr or "")
    except Exception as e:
        return 1, "", str(e)


def _git_head() -> str:
    code, out, _err = _run_git(["rev-parse", "--short", "HEAD"], timeout=4.0)
    return out.strip() if code == 0 else "-"


def _git_branch() -> str:
    code, out, _err = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], timeout=4.0)
    return out.strip() if code == 0 else "-"


def _git_behind(remote_ref: str = "origin/master") -> int | None:
    # Returns how many commits local is behind remote_ref, or None if unknown.
    code, out, _err = _run_git(["rev-list", "--count", f"HEAD..{remote_ref}"])
    if code != 0:
        return None
    try:
        return int(out.strip() or "0")
    except Exception:
        return None


def _bytes_to_gb(n: float) -> float:
    return float(n) / (1024.0 ** 3)


def _try_get_nvidia_gpu() -> dict | None:
    """Best-effort NVIDIA GPU stats.

    Returns None if nvidia-smi is not available.
    """
    import shutil
    import subprocess

    exe = shutil.which("nvidia-smi")
    if not exe:
        return None

    try:
        # util.gpu, memory.used, memory.total (percent not directly provided)
        out = subprocess.check_output(
            [
                exe,
                "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,name",
                "--format=csv,noheader,nounits",
            ],
            stderr=subprocess.DEVNULL,
            timeout=2,
            text=True,
        ).strip()
        if not out:
            return None
        # Take first GPU only for now.
        line = out.splitlines()[0]
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            return None
        util = float(parts[0])
        mem_used = float(parts[1])
        mem_total = float(parts[2])
        temp = float(parts[3])
        name = parts[4]
        mem_pct = (mem_used / mem_total * 100.0) if mem_total > 0 else 0.0
        return {
            "name": name,
            "util_percent": round(util, 1),
            "mem_used_mb": round(mem_used, 0),
            "mem_total_mb": round(mem_total, 0),
            "mem_percent": round(mem_pct, 1),
            "temp_c": round(temp, 0),
        }
    except Exception:
        return None


def _try_get_windows_gpu_engine_util() -> float | None:
    """Best-effort GPU utilization using Windows performance counters.

    This is vendor-agnostic (works for AMD/Intel/NVIDIA) but provides an estimate
    based on GPU Engine utilization (similar to Task Manager).
    """
    try:
        import subprocess

        if os.name != "nt":
            return None

        # typeperf returns CSV. We sample once. Example output:
        # "(PDH-CSV 4.0)","\\GPU Engine(pid_1234_luid_0x..._engtype_3D)\\Utilization Percentage",...
        # "04/05/2026 02:59:00.123","12.345","0.000",...
        cmd = [
            "typeperf",
            "\\GPU Engine(*)\\Utilization Percentage",
            "-sc",
            "1",
        ]
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=2, text=True, encoding="utf-8", errors="ignore")
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        if len(lines) < 3:
            return None

        # Data line is last.
        data = lines[-1]
        # Very small CSV parser: values are quoted and separated by ","
        parts = [p.strip().strip('"') for p in data.split(",")]
        if len(parts) <= 1:
            return None

        # parts[0] is timestamp; remaining are counters.
        vals: list[float] = []
        for raw in parts[1:]:
            try:
                # Some locales use comma decimal; typeperf typically uses dot. Handle both.
                raw2 = raw.replace(",", ".")
                vals.append(float(raw2))
            except Exception:
                pass
        if not vals:
            return None

        # GPU Engine counters are per-engine; Task Manager shows a blended view.
        # Taking max is a decent approximation of "current bottleneck".
        util = max(vals)
        return max(0.0, min(100.0, float(util)))
    except Exception:
        return None


def register_hw_routes(app: FastAPI) -> None:
    @app.get("/api/health/hw")
    def hw_health() -> JSONResponse:
        """Hardware usage snapshot for the navbar."""

        cpu = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        sm = psutil.swap_memory()
        commit_used = None
        commit_limit = None
        commit_pct = None
        try:
            # Windows: commit = RAM + pagefile. psutil exposes pagefile via swap.
            commit_used = int(vm.total - vm.available + sm.used)
            commit_limit = int(vm.total + sm.total)
            if commit_limit > 0:
                commit_pct = float(commit_used) / float(commit_limit) * 100.0
        except Exception:
            pass

        # Pick a stable path for disk usage.
        # On Windows, this will resolve to the drive containing the current working dir.
        disk_path = str(Path.cwd().anchor or Path.cwd())
        disk = psutil.disk_usage(disk_path)

        # Estimate disk "busy" via I/O counters delta over a short window.
        # This is approximate and may not match Task Manager exactly.
        busy_pct = None
        try:
            io1 = psutil.disk_io_counters()
            t1 = time.perf_counter()
            time.sleep(0.15)
            io2 = psutil.disk_io_counters()
            t2 = time.perf_counter()
            if io1 and io2 and (t2 - t1) > 0:
                # busy time delta (ms) / wall time (ms)
                dt_ms = (t2 - t1) * 1000.0
                busy_ms = float(getattr(io2, "busy_time", 0) - getattr(io1, "busy_time", 0))
                if dt_ms > 1:
                    busy_pct = max(0.0, min(100.0, busy_ms / dt_ms * 100.0))
        except Exception:
            busy_pct = None

        gpu = _try_get_nvidia_gpu()
        gpu_util = None
        gpu_mem_pct = None
        gpu_temp_c = None
        if gpu:
            gpu_util = gpu.get("util_percent")
            gpu_mem_pct = gpu.get("mem_percent")
            gpu_temp_c = gpu.get("temp_c")
        else:
            # Vendor-agnostic Windows fallback (AMD/Intel).
            win_util = _try_get_windows_gpu_engine_util()
            if win_util is not None:
                gpu_util = round(float(win_util), 1)

        payload = {
            "cpu_percent": round(float(cpu), 1),
            "ram_percent": round(float(vm.percent), 1),
            "ram_used_gb": round(_bytes_to_gb(vm.used), 2),
            "ram_total_gb": round(_bytes_to_gb(vm.total), 2),
            "commit_percent": None if commit_pct is None else round(float(commit_pct), 1),
            "commit_used_gb": None if commit_used is None else round(_bytes_to_gb(commit_used), 2),
            "commit_limit_gb": None if commit_limit is None else round(_bytes_to_gb(commit_limit), 2),
            "disk_full_percent": round(float(disk.percent), 1),
            "disk_used_gb": round(_bytes_to_gb(disk.used), 2),
            "disk_total_gb": round(_bytes_to_gb(disk.total), 2),
            "disk_path": disk_path,
            "disk_busy_percent": None if busy_pct is None else round(float(busy_pct), 1),
            "gpu": gpu,
            "gpu_util_percent": gpu_util,
            "gpu_mem_percent": gpu_mem_pct,
            "gpu_temp_c": gpu_temp_c,
        }
        return JSONResponse(payload)


def register_update_routes(app: FastAPI) -> None:
    """Dev-only repo update helpers.

    Assumes this app is run from a git checkout.
    """

    @app.get("/api/update/status")
    def update_status() -> JSONResponse:
        # Try a fetch, but keep it non-fatal/fast.
        _run_git(["fetch", "origin", "master"], timeout=6.0)
        branch = _git_branch()
        head = _git_head()
        behind = _git_behind("origin/master")
        return JSONResponse(
            {
                "ok": True,
                "branch": branch,
                "head": head,
                "remote": "origin/master",
                "behind": behind,
                "can_update": (behind is not None and behind > 0),
            }
        )

    @app.post("/api/update/pull")
    def update_pull() -> JSONResponse:
        # Prevent concurrent pulls.
        with _UPDATE_LOCK:
            # Ensure we only fast-forward (safer for a local app).
            _run_git(["fetch", "origin", "master"], timeout=10.0)
            code, out, err = _run_git(["pull", "--ff-only", "origin", "master"], timeout=30.0)
            head = _git_head()
            behind = _git_behind("origin/master")
            return JSONResponse(
                {
                    "ok": (code == 0),
                    "head": head,
                    "behind": behind,
                    "stdout": (out or "").strip(),
                    "stderr": (err or "").strip(),
                    "restart_required": True,
                    "restart_cmd": "uvicorn app.main:app --port 8025",
                }
            )


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
    start_osclient_wall()
    spreader = get_spreader()
    spreader.start()

    # Background watchdog: tails bot log files and kills osclient.exe when a
    # "stuck in withdraw loop" pattern is detected.
    win_user = os.environ.get("USERNAME")
    userprofile = os.environ.get("USERPROFILE")
    if userprofile and "\\" in userprofile:
        try:
            win_user = userprofile.split("\\")[-1] or win_user
        except Exception:
            pass
    win_user = win_user or "nubonix"

    # Safety: disable auto-killing OSClient by default.
    # The watchdog can produce false positives and kill every client; keep the log tailing
    # running, but require an explicit opt-in to kill processes.
    cfg = WatchdogConfig(
        logs_dir=Path(rf"C:\Users\{win_user}\Botting Hub\Client\Logs\Script"),
        pattern="*@*.txt",
        kill_osclient=(os.environ.get("WATCHDOG_KILL_OSCLIENT", "0") == "1"),
        terminate_sandbox=(os.environ.get("WATCHDOG_TERMINATE_SANDBOX", "0") == "1"),
    )
    watchdog_task = asyncio.create_task(watchdog_loop(cfg))

    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            spreader.stop()
        with contextlib.suppress(Exception):
            stop_osclient_wall()
        watchdog_task.cancel()
        with contextlib.suppress(Exception):
            await watchdog_task


app = FastAPI(title="BotFarmPlanner", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["gp"] = format_gp
templates.env.filters["mask_secret"] = mask_secret

register_hw_routes(app)
register_update_routes(app)

app.state.templates = templates
app.include_router(planner_router)
app.include_router(progression_router)

# OSClient Wall (dashboard)
include_osclient_wall(app, prefix="/wall")


# --- Control-UI compatibility ---
# The OSClient Wall now runs inside the main FastAPI app under /wall.
# Some older frontend code still expects root-level /api/* routes, so
# we keep thin pass-through routes here for compatibility.


@app.get("/api/stats")
def wall_api_stats() -> JSONResponse:
    from .osclient_wall.router import manager

    return JSONResponse(manager.get_stats())


@app.get("/api/layout")
def wall_api_layout() -> JSONResponse:
    from .osclient_wall.router import manager

    return JSONResponse(manager.get_layout())


@app.get("/api/windows")
def wall_api_windows() -> JSONResponse:
    from .osclient_wall.router import manager

    return JSONResponse(manager.get_windows())


@app.post("/api/focus/{hwnd}")
def wall_api_focus(hwnd: int) -> JSONResponse:
    from .osclient_wall.router import focus_window, flash_manager, manager

    windows = {item["hwnd"] for item in manager.get_windows()}
    if hwnd not in windows:
        raise HTTPException(status_code=404, detail="Window not found")
    ok = focus_window(hwnd)
    flash_manager.request_flash(hwnd)
    return JSONResponse({"ok": ok, "hwnd": hwnd})


@app.post("/api/settings/fps")
def wall_api_set_fps(payload: dict) -> JSONResponse:
    from .osclient_wall.router import manager

    fps = int(payload.get("fps", 5))
    applied = manager.set_target_fps(fps)
    return JSONResponse({"ok": True, "target_fps": applied})


@app.post("/api/settings/cols")
def wall_api_set_cols(payload: dict) -> JSONResponse:
    from .osclient_wall.router import manager

    cols = int(payload.get("cols", 8))
    applied = manager.set_grid_cols(cols)
    return JSONResponse({"ok": True, "grid_cols": applied})


@app.get("/client-wall", response_class=HTMLResponse)
def client_wall_page(request: Request):
    return templates.TemplateResponse(
        request,
        "client_wall.html",
        {
            "request": request,
        },
    )


@app.get("/window-spreader", response_class=HTMLResponse)
def window_spreader_page(request: Request):
    return templates.TemplateResponse(
        request,
        "window_spreader.html",
        {
            "request": request,
        },
    )


@app.get("/api/window-spreader/status")
def window_spreader_status() -> JSONResponse:
    from .window_spreader import get_spreader

    s = get_spreader()
    return JSONResponse(
        {
            "running": s.is_running(),
            "poll_seconds": s.poll_seconds,
            "reuse_cooldown_seconds": s.reuse_cooldown_seconds,
            "slots": s.get_slots(),
            "last_action": s.last_action(),
        }
    )


@app.get("/api/window-spreader/windows")
def window_spreader_windows() -> JSONResponse:
    from .window_spreader import get_spreader

    s = get_spreader()
    return JSONResponse({"windows": s.list_windows()})


@app.post("/api/window-spreader/start")
def window_spreader_start() -> JSONResponse:
    from .window_spreader import get_spreader

    s = get_spreader()
    s.start()
    return JSONResponse({"ok": True, "running": s.is_running()})


@app.post("/api/window-spreader/stop")
def window_spreader_stop() -> JSONResponse:
    from .window_spreader import get_spreader

    s = get_spreader()
    s.stop()
    return JSONResponse({"ok": True, "running": s.is_running()})


@app.post("/api/window-spreader/tick")
def window_spreader_tick() -> JSONResponse:
    from .window_spreader import get_spreader

    s = get_spreader()
    s.tick()
    return JSONResponse({"ok": True, "last_action": s.last_action()})


@app.post("/api/window-spreader/pin")
async def window_spreader_pin(request: Request) -> JSONResponse:
    """Pin a specific window (hwnd) to a specific slot.

    Body JSON:
      {"slot_index": 1, "hwnd": 123456} to pin
      {"slot_index": 1, "hwnd": null} to unpin
    """
    from .window_spreader import get_spreader

    payload = await request.json()
    slot_index = int(payload.get("slot_index"))
    hwnd = payload.get("hwnd", None)
    s = get_spreader()
    s.set_pinned(slot_index, None if hwnd in (None, "", 0) else int(hwnd))
    return JSONResponse({"ok": True})


@app.get("/watchdog", response_class=HTMLResponse)
def watchdog_page(request: Request):
    return templates.TemplateResponse(
        request,
        "watchdog.html",
        {
            "request": request,
            "status": WATCHDOG_STATUS,
        },
    )


@app.get("/watchdog/status")
def watchdog_status_json():
    return JSONResponse(
        {
            "running": WATCHDOG_STATUS.running,
            "logs_dir": WATCHDOG_STATUS.logs_dir,
            "pattern": WATCHDOG_STATUS.pattern,
            "poll_interval": WATCHDOG_STATUS.poll_interval,
            "threshold_none_arrays": WATCHDOG_STATUS.threshold_none_arrays,
            "threshold_withdraws": WATCHDOG_STATUS.threshold_withdraws,
            "cooldown_seconds": WATCHDOG_STATUS.cooldown_seconds,
            "bootstrap_bytes": getattr(WATCHDOG_STATUS, "bootstrap_bytes", 0),
            "bootstrap_refresh_seconds": getattr(WATCHDOG_STATUS, "bootstrap_refresh_seconds", 0),
            "files": [
                {
                    "path": f.path,
                    "inferred_pid": f.inferred_pid,
                    "inferred_sandbox": f.inferred_sandbox,
                    "window_seconds": f.window_seconds,
                    "none_arrays": f.none_arrays,
                    "withdraws": f.withdraws,
                    "last_withdraw_item": getattr(f, "last_withdraw_item", None),
                    "last_action_at": f.last_action_at,
                    "last_action": f.last_action,
                }
                for f in (WATCHDOG_STATUS.files or [])
            ],
        }
    )


@app.post("/watchdog/config")
async def watchdog_config_update(payload: dict):
    update_watchdog_config(payload)
    return JSONResponse({"ok": True})




def format_usd(value) -> str:
    if value is None:
        return "$0.00"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"${n:,.2f}"


templates.env.filters["usd"] = format_usd


def _split_usd_evenly(total_usd: Decimal, count: int) -> list[Decimal]:
    if count <= 0:
        return []

    total_cents = int((total_usd * Decimal("100")).quantize(Decimal("1")))
    base_cents, remainder = divmod(total_cents, count)
    shares = [Decimal(base_cents) / Decimal("100") for _ in range(count)]
    for i in range(remainder):
        shares[i] += Decimal("0.01")
    return shares


def _parse_optional_date(start_date: str):
    parsed_date = None
    if (start_date or "").strip():
        try:
            from datetime import datetime

            parsed_date = datetime.strptime(start_date.strip(), "%Y-%m-%d").date()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid start_date (use YYYY-MM-DD)")
    return parsed_date


def _add_global_expense(
    session: Session,
    *,
    name: str,
    total_amount: Decimal,
    kind: str,
    start_date,
    notes: str | None,
) -> tuple[str, int]:
    accounts = session.scalars(select(Account).order_by(Account.id)).all()
    if not accounts:
        raise HTTPException(status_code=400, detail="No accounts available for global split")

    split_group = uuid.uuid4().hex
    shares = _split_usd_evenly(total_amount, len(accounts))
    for acct, share in zip(accounts, shares):
        session.add(
            AccountExpense(
                account_id=acct.id,
                name=name,
                amount_usd=share,
                kind=kind,
                allocation_scope="global",
                allocation_group=split_group,
                source_amount_usd=total_amount,
                allocated_account_count=len(accounts),
                start_date=start_date,
                notes=notes,
                is_active=True,
            )
        )
    session.commit()
    return split_group, len(accounts)


def _global_expense_groups(session: Session) -> list[dict[str, object]]:
    rows = session.scalars(
        select(AccountExpense)
        .where(AccountExpense.allocation_scope == "global")
        .order_by(AccountExpense.created_at.desc(), AccountExpense.id.desc())
    ).all()

    groups: dict[str, dict[str, object]] = {}
    ordered_keys: list[str] = []
    for row in rows:
        key = row.allocation_group or f"legacy-{row.id}"
        if key not in groups:
            ordered_keys.append(key)
            per_account_amount = 0.0
            try:
                per_account_amount = float(row.amount_usd or 0)
            except Exception:
                pass
            source_amount = None
            try:
                source_amount = float(row.source_amount_usd) if row.source_amount_usd is not None else None
            except Exception:
                source_amount = None
            groups[key] = {
                "group_key": key,
                "expense_id": row.id,
                "name": row.name,
                "kind": row.kind,
                "is_active": bool(row.is_active),
                "created_at": row.created_at,
                "start_date": row.start_date,
                "notes": row.notes,
                "account_count": int(row.allocated_account_count or 0),
                "source_amount_usd": source_amount,
                "per_account_amount_usd": per_account_amount,
            }

    return [groups[key] for key in ordered_keys]


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

    expenses = session.scalars(
        select(AccountExpense)
        .where(AccountExpense.account_id == account_id)
        .order_by(AccountExpense.created_at.desc())
    ).all()

    total_spent = 0.0
    monthly_burn = 0.0
    for e in expenses:
        try:
            amt = float(e.amount_usd or 0)
        except Exception:
            amt = 0.0
        if e.kind == "monthly":
            if e.is_active:
                monthly_burn += amt
        else:
            total_spent += amt
    total_spent_all_time = total_spent + sum(
        (float(e.amount_usd or 0) if (e.kind == "monthly") else 0.0)
        for e in expenses
    )

    return templates.TemplateResponse(
        request,
        "account_detail.html",
        {
            "request": request,
            "account": account,
            "message": request.query_params.get("message"),
            "expenses": expenses,
            "total_spent_all_time": total_spent_all_time,
            "monthly_burn": monthly_burn,
        },
    )


@app.post("/accounts/{account_id}/expenses/new")
def add_account_expense(
    account_id: int,
    name: str = Form(...),
    amount_usd: str = Form("0"),
    kind: str = Form("one_time"),
    allocation_scope: str = Form("account"),
    start_date: str = Form(""),
    notes: str = Form(""),
    session: Session = Depends(get_session),
):
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    cleaned_name = (name or "").strip()
    if not cleaned_name:
        raise HTTPException(status_code=400, detail="Name is required")

    try:
        total_amount = Decimal(str(amount_usd).strip() or "0").quantize(Decimal("0.01"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid amount")

    if total_amount < 0:
        raise HTTPException(status_code=400, detail="Amount must be non-negative")

    kind = (kind or "one_time").strip().lower()
    if kind not in {"one_time", "monthly"}:
        raise HTTPException(status_code=400, detail="Invalid kind")

    allocation_scope = (allocation_scope or "account").strip().lower()
    if allocation_scope not in {"account", "global"}:
        raise HTTPException(status_code=400, detail="Invalid expense scope")

    parsed_date = _parse_optional_date(start_date)

    cleaned_notes = notes.strip() or None

    if allocation_scope == "global":
        _split_group, account_count = _add_global_expense(
            session,
            name=cleaned_name,
            total_amount=total_amount,
            kind=kind,
            start_date=parsed_date,
            notes=cleaned_notes,
        )
        message = f"Global expense added across {account_count} accounts (${float(total_amount):.2f} total)"
        return RedirectResponse(url=f"/accounts/{account_id}?message={message}", status_code=303)

    row = AccountExpense(
        account_id=account_id,
        name=cleaned_name,
        amount_usd=total_amount,
        kind=kind,
        allocation_scope="account",
        source_amount_usd=total_amount,
        allocated_account_count=1,
        start_date=parsed_date,
        notes=cleaned_notes,
        is_active=True,
    )
    session.add(row)
    session.commit()

    return RedirectResponse(url=f"/accounts/{account_id}?message=Expense added", status_code=303)


@app.post("/accounts/{account_id}/expenses/{expense_id}/toggle")
def toggle_account_expense(
    account_id: int,
    expense_id: int,
    session: Session = Depends(get_session),
):
    row = session.get(AccountExpense, expense_id)
    if not row or row.account_id != account_id:
        raise HTTPException(status_code=404, detail="Expense not found")

    if row.allocation_scope == "global" and row.allocation_group:
        rows = session.scalars(
            select(AccountExpense).where(AccountExpense.allocation_group == row.allocation_group)
        ).all()
        new_state = not bool(row.is_active)
        for item in rows:
            item.is_active = new_state
        session.commit()
        message = f"Global expense {'enabled' if new_state else 'disabled'} across {len(rows)} accounts"
        return RedirectResponse(url=f"/accounts/{account_id}?message={message}", status_code=303)

    row.is_active = not bool(row.is_active)
    session.commit()
    return RedirectResponse(url=f"/accounts/{account_id}?message=Expense updated", status_code=303)


@app.post("/accounts/{account_id}/expenses/{expense_id}/delete")
def delete_account_expense(
    account_id: int,
    expense_id: int,
    session: Session = Depends(get_session),
):
    row = session.get(AccountExpense, expense_id)
    if not row or row.account_id != account_id:
        raise HTTPException(status_code=404, detail="Expense not found")

    if row.allocation_scope == "global" and row.allocation_group:
        rows = session.scalars(
            select(AccountExpense).where(AccountExpense.allocation_group == row.allocation_group)
        ).all()
        count = len(rows)
        for item in rows:
            session.delete(item)
        session.commit()
        return RedirectResponse(
            url=f"/accounts/{account_id}?message=Global expense deleted across {count} accounts",
            status_code=303,
        )

    session.delete(row)
    session.commit()
    return RedirectResponse(url=f"/accounts/{account_id}?message=Expense deleted", status_code=303)


@app.get("/expenses/global", response_class=HTMLResponse)
def global_expenses_page(request: Request, session: Session = Depends(get_session)):
    groups = _global_expense_groups(session)

    total_one_time = 0.0
    monthly_burn = 0.0
    for g in groups:
        amount = float(g.get("source_amount_usd") or 0)
        if g.get("kind") == "monthly":
            if g.get("is_active"):
                monthly_burn += amount
        else:
            total_one_time += amount

    return templates.TemplateResponse(
        request,
        "global_expenses.html",
        {
            "request": request,
            "message": request.query_params.get("message"),
            "groups": groups,
            "global_total_spent": total_one_time + sum(
                float(g.get("source_amount_usd") or 0) for g in groups if g.get("kind") == "monthly"
            ),
            "global_monthly_burn": monthly_burn,
            "global_one_time_total": total_one_time,
        },
    )


@app.post("/expenses/global/new")
def add_global_expense(
    name: str = Form(...),
    amount_usd: str = Form("0"),
    kind: str = Form("one_time"),
    start_date: str = Form(""),
    notes: str = Form(""),
    session: Session = Depends(get_session),
):
    cleaned_name = (name or "").strip()
    if not cleaned_name:
        raise HTTPException(status_code=400, detail="Name is required")

    try:
        total_amount = Decimal(str(amount_usd).strip() or "0").quantize(Decimal("0.01"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid amount")

    if total_amount < 0:
        raise HTTPException(status_code=400, detail="Amount must be non-negative")

    kind = (kind or "one_time").strip().lower()
    if kind not in {"one_time", "monthly"}:
        raise HTTPException(status_code=400, detail="Invalid kind")

    parsed_date = _parse_optional_date(start_date)
    _group_key, account_count = _add_global_expense(
        session,
        name=cleaned_name,
        total_amount=total_amount,
        kind=kind,
        start_date=parsed_date,
        notes=notes.strip() or None,
    )
    message = f"Global expense added across {account_count} accounts (${float(total_amount):.2f} total)"
    return RedirectResponse(url=f"/expenses/global?message={message}", status_code=303)


@app.post("/expenses/global/{group_key}/toggle")
def toggle_global_expense(group_key: str, session: Session = Depends(get_session)):
    rows = session.scalars(
        select(AccountExpense).where(
            AccountExpense.allocation_scope == "global",
            AccountExpense.allocation_group == group_key,
        )
    ).all()
    if not rows:
        raise HTTPException(status_code=404, detail="Global expense not found")

    new_state = not bool(rows[0].is_active)
    for row in rows:
        row.is_active = new_state
    session.commit()
    message = f"Global expense {'enabled' if new_state else 'disabled'} across {len(rows)} accounts"
    return RedirectResponse(url=f"/expenses/global?message={message}", status_code=303)


@app.post("/expenses/global/{group_key}/delete")
def delete_global_expense(group_key: str, session: Session = Depends(get_session)):
    rows = session.scalars(
        select(AccountExpense).where(
            AccountExpense.allocation_scope == "global",
            AccountExpense.allocation_group == group_key,
        )
    ).all()
    if not rows:
        raise HTTPException(status_code=404, detail="Global expense not found")

    count = len(rows)
    for row in rows:
        session.delete(row)
    session.commit()
    return RedirectResponse(
        url=f"/expenses/global?message=Global expense deleted across {count} accounts",
        status_code=303,
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
