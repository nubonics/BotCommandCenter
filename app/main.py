from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
import subprocess
import threading
import uuid
from urllib.parse import quote_plus

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
from .models import Account, AccountExpense, AccountGoal, AccountProgress, AccountRevenue, Item, MoneyMaker, MoneyMakerComponent
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
def client_wall_page(request: Request, session: Session = Depends(get_session)):
    rows, summary, app_health = _account_health_snapshot(session)
    wall_snapshot = _wall_window_snapshot(
        session,
        [row["account"] for row in rows],
        rows,
    )
    return templates.TemplateResponse(
        request,
        "client_wall.html",
        {
            "request": request,
            "summary": summary,
            "app_health": app_health,
            "top_accounts": rows[:6],
            "wall_snapshot": wall_snapshot,
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
templates.env.filters["urlencode"] = lambda value: quote_plus(str(value or ""))


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


def _split_tags(raw_tags: str | None) -> list[str]:
    if not raw_tags:
        return []

    seen: set[str] = set()
    ordered: list[str] = []
    for part in str(raw_tags).replace("\n", ",").split(","):
        tag = part.strip().lower()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        ordered.append(tag)
    return ordered


def _normalize_tags_text(raw_tags: str | None) -> str | None:
    tags = _split_tags(raw_tags)
    return ", ".join(tags) or None


def _all_account_tags(accounts: list[Account]) -> list[str]:
    tags: set[str] = set()
    for account in accounts:
        tags.update(_split_tags(account.tags))
    return sorted(tags)


def _merge_tags(existing_tags: str | None, add_tags: str | None = None, remove_tags: str | None = None) -> str | None:
    current = _split_tags(existing_tags)
    current_set = set(current)

    for tag in _split_tags(add_tags):
        if tag not in current_set:
            current.append(tag)
            current_set.add(tag)

    remove_set = set(_split_tags(remove_tags))
    if remove_set:
        current = [tag for tag in current if tag not in remove_set]

    return ", ".join(current) or None


def _accounts_for_global_allocation(session: Session, allocation_tag: str | None = None) -> list[Account]:
    accounts = session.scalars(select(Account).order_by(Account.id)).all()
    normalized_tag = (allocation_tag or "").strip().lower()
    if normalized_tag:
        accounts = [account for account in accounts if normalized_tag in _split_tags(account.tags)]
    return accounts


def _account_health_snapshot(
    session: Session,
    accounts: list[Account] | None = None,
) -> tuple[list[dict[str, object]], dict[str, int], dict[str, object]]:
    if accounts is None:
        accounts = session.scalars(select(Account).order_by(Account.label)).all()

    progress_account_ids = set(session.scalars(select(AccountProgress.account_id)).all())
    active_goal_account_ids = set(
        session.scalars(select(AccountGoal.account_id).where(AccountGoal.is_active.is_(True))).all()
    )
    expense_account_ids = set(session.scalars(select(AccountExpense.account_id)).all())
    revenue_account_ids = set(session.scalars(select(AccountRevenue.account_id)).all())
    money_maker_count = len(session.scalars(select(MoneyMaker.id)).all())
    global_cost_group_count = len(_global_expense_groups(session))

    from .osclient_wall.router import manager as wall_manager

    rows: list[dict[str, object]] = []
    summary = {
        "accounts_with_issues": 0,
        "missing_rsn": 0,
        "missing_proxy": 0,
        "missing_progress": 0,
        "missing_goal": 0,
        "missing_costs": 0,
        "missing_revenue": 0,
        "missing_status": 0,
        "banned": 0,
    }

    for account in accounts:
        issues: list[str] = []
        if not (account.rsn or "").strip():
            issues.append("Missing RSN")
            summary["missing_rsn"] += 1
        if not (account.proxy_ip or "").strip():
            issues.append("Missing proxy")
            summary["missing_proxy"] += 1
        if account.id not in progress_account_ids:
            issues.append("No progress state")
            summary["missing_progress"] += 1
        if account.id not in active_goal_account_ids:
            issues.append("No active goal")
            summary["missing_goal"] += 1
        if account.id not in expense_account_ids:
            issues.append("No costs tracked")
            summary["missing_costs"] += 1
        if account.id not in revenue_account_ids:
            issues.append("No revenue tracked")
            summary["missing_revenue"] += 1
        if not (account.status or "").strip():
            issues.append("No status")
            summary["missing_status"] += 1
        if account.banned:
            issues.append("Banned")
            summary["banned"] += 1

        issue_count = len(issues)
        if issue_count:
            summary["accounts_with_issues"] += 1

        if account.banned:
            health_label = "Banned"
        elif issue_count == 0:
            health_label = "Clean"
        elif issue_count <= 2:
            health_label = "Watch"
        else:
            health_label = "Needs work"

        rows.append(
            {
                "account": account,
                "issues": issues,
                "issue_count": issue_count,
                "health_label": health_label,
            }
        )

    rows.sort(key=lambda row: (-int(row["issue_count"]), str(row["account"].label).lower()))

    wall_windows = wall_manager.get_windows()
    app_health = {
        "account_count": len(accounts),
        "money_maker_count": money_maker_count,
        "global_cost_group_count": global_cost_group_count,
        "wall_window_count": len(wall_windows),
        "spreader_running": get_spreader().is_running(),
        "watchdog_running": bool(WATCHDOG_STATUS.running),
    }

    return rows, summary, app_health


def _money_row_summary(rows: list[object]) -> dict[str, float]:
    one_time_total = 0.0
    monthly_total = 0.0
    tracked_total = 0.0

    for row in rows:
        try:
            amount = float(getattr(row, "amount_usd", 0) or 0)
        except Exception:
            amount = 0.0

        if getattr(row, "kind", None) == "monthly":
            tracked_total += amount
            if bool(getattr(row, "is_active", False)):
                monthly_total += amount
        else:
            one_time_total += amount
            tracked_total += amount

    return {
        "one_time_total": one_time_total,
        "monthly_total": monthly_total,
        "tracked_total": tracked_total,
    }


def _financial_summary(expenses: list[AccountExpense], revenues: list[AccountRevenue]) -> dict[str, float]:
    expense_summary = _money_row_summary(expenses)
    revenue_summary = _money_row_summary(revenues)
    return {
        "tracked_revenue": revenue_summary["tracked_total"],
        "monthly_revenue": revenue_summary["monthly_total"],
        "tracked_cost": expense_summary["tracked_total"],
        "monthly_cost": expense_summary["monthly_total"],
        "tracked_net": revenue_summary["tracked_total"] - expense_summary["tracked_total"],
        "monthly_net": revenue_summary["monthly_total"] - expense_summary["monthly_total"],
    }


def _normalize_match_text(value: str | None) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
    return " ".join(text.split())


def _score_window_account_match(window_title: str, account: Account) -> tuple[int, str | None]:
    raw_title = str(window_title or "").strip().lower()
    norm_title = _normalize_match_text(window_title)
    best_score = 0
    best_reason: str | None = None

    rsn = (account.rsn or "").strip().lower()
    norm_rsn = _normalize_match_text(account.rsn)
    if rsn and len(rsn) >= 3:
        if raw_title == rsn or norm_title == norm_rsn:
            return 120, "RSN exact"
        if rsn in raw_title or (norm_rsn and norm_rsn in norm_title):
            best_score = 100 + min(len(rsn), 20)
            best_reason = "RSN match"

    label = (account.label or "").strip().lower()
    norm_label = _normalize_match_text(account.label)
    if label and len(label) >= 4:
        if raw_title == label or norm_title == norm_label:
            return max(best_score, 95), "Label exact"
        if label in raw_title or (norm_label and norm_label in norm_title):
            score = 70 + min(len(label), 20)
            if score > best_score:
                best_score = score
                best_reason = "Label match"

    return best_score, best_reason


def _wall_window_snapshot(
    session: Session,
    accounts: list[Account] | None = None,
    health_rows: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    if accounts is None:
        accounts = session.scalars(select(Account).order_by(Account.label)).all()
    if health_rows is None:
        health_rows, _summary, _app_health = _account_health_snapshot(session, accounts)

    from .osclient_wall.router import manager as wall_manager

    health_by_id = {int(row["account"].id): row for row in health_rows}
    window_rows: list[dict[str, object]] = []
    matched_count = 0

    for window in wall_manager.get_windows():
        best_account = None
        best_reason = None
        best_score = 0
        for account in accounts:
            score, reason = _score_window_account_match(str(window.get("title") or ""), account)
            if score > best_score:
                best_score = score
                best_reason = reason
                best_account = account

        matched = best_account is not None and best_score >= 80
        health = health_by_id.get(int(best_account.id)) if matched and best_account else None
        if matched:
            matched_count += 1

        window_rows.append(
            {
                "hwnd": int(window.get("hwnd") or 0),
                "title": str(window.get("title") or ""),
                "process_name": str(window.get("process_name") or ""),
                "size": f"{int(window.get('width') or 0)}×{int(window.get('height') or 0)}",
                "matched": matched,
                "match_reason": best_reason if matched else None,
                "account": (
                    {
                        "id": int(best_account.id),
                        "label": best_account.label,
                        "rsn": best_account.rsn,
                        "health_label": health.get("health_label") if health else None,
                        "issue_count": health.get("issue_count") if health else 0,
                    }
                    if matched and best_account
                    else None
                ),
            }
        )

    window_rows.sort(key=lambda row: (0 if row["matched"] else 1, str(row["title"]).lower()))
    return {
        "windows": window_rows,
        "matched_count": matched_count,
        "unmatched_count": max(0, len(window_rows) - matched_count),
    }


def _add_global_expense(
    session: Session,
    *,
    name: str,
    total_amount: Decimal,
    kind: str,
    start_date,
    notes: str | None,
    allocation_tag: str | None = None,
) -> tuple[str, int]:
    normalized_tag = (allocation_tag or "").strip().lower() or None
    accounts = _accounts_for_global_allocation(session, normalized_tag)
    if not accounts:
        if normalized_tag:
            raise HTTPException(status_code=400, detail=f"No accounts found with tag '{normalized_tag}'")
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
                allocation_tag=normalized_tag,
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
                "allocation_tag": row.allocation_tag,
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


@app.get("/action-center", response_class=HTMLResponse)
def action_center_page(request: Request, session: Session = Depends(get_session)):
    accounts = session.scalars(select(Account).order_by(Account.label)).all()
    rows, summary, app_health = _account_health_snapshot(session, accounts)

    return templates.TemplateResponse(
        request,
        "action_center.html",
        {
            "request": request,
            "rows": rows,
            "summary": summary,
            "app_health": app_health,
            "message": request.query_params.get("message"),
        },
    )


@app.get("/api/ops/summary")
def ops_summary(session: Session = Depends(get_session)) -> JSONResponse:
    rows, summary, app_health = _account_health_snapshot(session)
    top_accounts = [
        {
            "id": row["account"].id,
            "label": row["account"].label,
            "issue_count": row["issue_count"],
            "health_label": row["health_label"],
            "issues": row["issues"],
        }
        for row in rows[:6]
    ]
    return JSONResponse(
        {
            "summary": summary,
            "app_health": app_health,
            "top_accounts": top_accounts,
        }
    )


@app.get("/api/ops/wall-windows")
def ops_wall_windows(session: Session = Depends(get_session)) -> JSONResponse:
    accounts = session.scalars(select(Account).order_by(Account.label)).all()
    health_rows, _summary, _app_health = _account_health_snapshot(session, accounts)
    return JSONResponse(_wall_window_snapshot(session, accounts, health_rows))


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
def list_accounts(request: Request, q: str = "", tag: str = "", session: Session = Depends(get_session)):
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
    health_rows, _summary, _app_health = _account_health_snapshot(session, accounts)
    health_by_id = {int(row["account"].id): row for row in health_rows}
    selected_tag = (tag or "").strip().lower()
    if selected_tag:
        accounts = [account for account in accounts if selected_tag in _split_tags(account.tags)]

    return templates.TemplateResponse(
        request,
        "accounts.html",
        {
            "request": request,
            "accounts": accounts,
            "health_by_id": health_by_id,
            "all_tags": _all_account_tags(session.scalars(select(Account).order_by(Account.label)).all()),
            "q": q,
            "selected_tag": selected_tag,
            "message": request.query_params.get("message"),
        },
    )


@app.get("/accounts/pnl", response_class=HTMLResponse)
def accounts_pnl_page(request: Request, session: Session = Depends(get_session)):
    accounts = session.scalars(select(Account).order_by(Account.label)).all()
    expenses = session.scalars(select(AccountExpense).order_by(AccountExpense.created_at)).all()
    revenues = session.scalars(select(AccountRevenue).order_by(AccountRevenue.created_at)).all()
    health_rows, _summary, _app_health = _account_health_snapshot(session, accounts)
    health_by_id = {int(row["account"].id): row for row in health_rows}

    expenses_by_account: dict[int, list[AccountExpense]] = {}
    for row in expenses:
        expenses_by_account.setdefault(int(row.account_id), []).append(row)

    revenues_by_account: dict[int, list[AccountRevenue]] = {}
    for row in revenues:
        revenues_by_account.setdefault(int(row.account_id), []).append(row)

    rows: list[dict[str, object]] = []
    totals = {
        "tracked_revenue": 0.0,
        "monthly_revenue": 0.0,
        "tracked_cost": 0.0,
        "monthly_cost": 0.0,
        "tracked_net": 0.0,
        "monthly_net": 0.0,
    }
    for account in accounts:
        financials = _financial_summary(
            expenses_by_account.get(int(account.id), []),
            revenues_by_account.get(int(account.id), []),
        )
        for key in totals:
            totals[key] += float(financials[key])
        rows.append(
            {
                "account": account,
                "financials": financials,
                "health": health_by_id.get(int(account.id)),
            }
        )

    rows.sort(key=lambda row: (float(row["financials"]["monthly_net"]), float(row["financials"]["tracked_net"])), reverse=True)

    return templates.TemplateResponse(
        request,
        "accounts_pnl.html",
        {
            "request": request,
            "rows": rows,
            "totals": totals,
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


@app.post("/accounts/bulk-update")
def bulk_update_accounts(
    account_ids: list[int] = Form([]),
    bulk_status: str = Form(""),
    bulk_tags_add: str = Form(""),
    bulk_tags_remove: str = Form(""),
    banned_state: str = Form("keep"),
    q: str = Form(""),
    tag: str = Form(""),
    session: Session = Depends(get_session),
):
    ids = sorted({int(account_id) for account_id in account_ids})
    if not ids:
        raise HTTPException(status_code=400, detail="No accounts selected")

    accounts = session.scalars(select(Account).where(Account.id.in_(ids))).all()
    if not accounts:
        raise HTTPException(status_code=404, detail="No matching accounts found")

    cleaned_status = (bulk_status or "").strip()
    add_tags = bulk_tags_add or ""
    remove_tags = bulk_tags_remove or ""

    changed = 0
    for account in accounts:
        touched = False

        if cleaned_status:
            new_status = None if cleaned_status == "__clear__" else cleaned_status
            if account.status != new_status:
                account.status = new_status
                touched = True

        if banned_state == "banned" and not account.banned:
            account.banned = True
            touched = True
        elif banned_state == "not_banned" and account.banned:
            account.banned = False
            touched = True

        merged_tags = _merge_tags(account.tags, add_tags=add_tags, remove_tags=remove_tags)
        if merged_tags != account.tags:
            account.tags = merged_tags
            touched = True

        if touched:
            changed += 1

    if changed == 0:
        message = f"No bulk changes applied across {len(accounts)} selected accounts"
    else:
        session.commit()
        message = f"Updated {changed} of {len(accounts)} selected accounts"

    params = []
    if q.strip():
        params.append(f"q={quote_plus(q.strip())}")
    if tag.strip():
        params.append(f"tag={quote_plus(tag.strip())}")
    params.append(f"message={quote_plus(message)}")
    query = "?" + "&".join(params) if params else ""
    return RedirectResponse(url=f"/accounts{query}", status_code=303)


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
    tags: str = Form(""),
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
        tags=_normalize_tags_text(tags),
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
    revenues = session.scalars(
        select(AccountRevenue)
        .where(AccountRevenue.account_id == account_id)
        .order_by(AccountRevenue.created_at.desc())
    ).all()

    financials = _financial_summary(expenses, revenues)

    return templates.TemplateResponse(
        request,
        "account_detail.html",
        {
            "request": request,
            "account": account,
            "account_tags": _split_tags(account.tags),
            "all_tags": _all_account_tags(session.scalars(select(Account).order_by(Account.label)).all()),
            "message": request.query_params.get("message"),
            "expenses": expenses,
            "revenues": revenues,
            "financials": financials,
        },
    )


@app.post("/accounts/{account_id}/expenses/new")
def add_account_expense(
    account_id: int,
    name: str = Form(...),
    amount_usd: str = Form("0"),
    kind: str = Form("one_time"),
    allocation_scope: str = Form("account"),
    allocation_tag: str = Form(""),
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
            allocation_tag=allocation_tag,
        )
        target = (allocation_tag or "").strip().lower()
        if target:
            message = f"Global expense added across {account_count} '{target}' accounts (${float(total_amount):.2f} total)"
        else:
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


@app.post("/accounts/{account_id}/revenues/new")
def add_account_revenue(
    account_id: int,
    name: str = Form(...),
    amount_usd: str = Form("0"),
    kind: str = Form("one_time"),
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
        amount = Decimal(str(amount_usd).strip() or "0").quantize(Decimal("0.01"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid amount")

    if amount < 0:
        raise HTTPException(status_code=400, detail="Amount must be non-negative")

    kind = (kind or "one_time").strip().lower()
    if kind not in {"one_time", "monthly"}:
        raise HTTPException(status_code=400, detail="Invalid kind")

    row = AccountRevenue(
        account_id=account_id,
        name=cleaned_name,
        amount_usd=amount,
        kind=kind,
        start_date=_parse_optional_date(start_date),
        notes=notes.strip() or None,
        is_active=True,
    )
    session.add(row)
    session.commit()

    return RedirectResponse(url=f"/accounts/{account_id}?message=Revenue added", status_code=303)


@app.post("/accounts/{account_id}/revenues/{revenue_id}/toggle")
def toggle_account_revenue(
    account_id: int,
    revenue_id: int,
    session: Session = Depends(get_session),
):
    row = session.get(AccountRevenue, revenue_id)
    if not row or row.account_id != account_id:
        raise HTTPException(status_code=404, detail="Revenue not found")

    row.is_active = not bool(row.is_active)
    session.commit()
    return RedirectResponse(url=f"/accounts/{account_id}?message=Revenue updated", status_code=303)


@app.post("/accounts/{account_id}/revenues/{revenue_id}/delete")
def delete_account_revenue(
    account_id: int,
    revenue_id: int,
    session: Session = Depends(get_session),
):
    row = session.get(AccountRevenue, revenue_id)
    if not row or row.account_id != account_id:
        raise HTTPException(status_code=404, detail="Revenue not found")

    session.delete(row)
    session.commit()
    return RedirectResponse(url=f"/accounts/{account_id}?message=Revenue deleted", status_code=303)


@app.get("/expenses/global", response_class=HTMLResponse)
def global_expenses_page(request: Request, session: Session = Depends(get_session)):
    groups = _global_expense_groups(session)
    all_accounts = session.scalars(select(Account).order_by(Account.label)).all()

    total_one_time = 0.0
    monthly_burn = 0.0
    avg_per_account_total = 0.0
    avg_per_account_monthly = 0.0
    avg_per_account_one_time = 0.0
    for g in groups:
        amount = float(g.get("source_amount_usd") or 0)
        per_account_amount = float(g.get("per_account_amount_usd") or 0)
        if g.get("kind") == "monthly":
            if g.get("is_active"):
                monthly_burn += amount
                avg_per_account_monthly += per_account_amount
        else:
            total_one_time += amount
            avg_per_account_one_time += per_account_amount
        avg_per_account_total += per_account_amount

    return templates.TemplateResponse(
        request,
        "global_expenses.html",
        {
            "request": request,
            "message": request.query_params.get("message"),
            "groups": groups,
            "all_tags": _all_account_tags(all_accounts),
            "global_total_spent": total_one_time + sum(
                float(g.get("source_amount_usd") or 0) for g in groups if g.get("kind") == "monthly"
            ),
            "global_monthly_burn": monthly_burn,
            "global_one_time_total": total_one_time,
            "avg_per_account_total": avg_per_account_total,
            "avg_per_account_monthly": avg_per_account_monthly,
            "avg_per_account_one_time": avg_per_account_one_time,
        },
    )


@app.post("/expenses/global/new")
def add_global_expense(
    name: str = Form(...),
    amount_usd: str = Form("0"),
    kind: str = Form("one_time"),
    allocation_tag: str = Form(""),
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
        allocation_tag=allocation_tag,
    )
    target = (allocation_tag or "").strip().lower()
    if target:
        message = f"Global expense added across {account_count} '{target}' accounts (${float(total_amount):.2f} total)"
    else:
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
    tags: str = Form(""),
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
    account.tags = _normalize_tags_text(tags)
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
