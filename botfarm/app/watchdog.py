from __future__ import annotations

import asyncio
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


TS_RE = re.compile(r"^\[(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})\]\s*(.*)$")
NONE_ARRAY_RE = re.compile(r"\[\s*None\s*,\s*None\s*,")
WITHDRAW_RE = re.compile(r"\bwithdrawing item:\s*(.+)$", re.IGNORECASE)
SANDBOX_PATH_RE = re.compile(r"C:\\Sandbox\\([^\\]+)\\", re.IGNORECASE)


@dataclass
class WatchdogConfig:
    logs_dir: Path
    pattern: str = "*@*.txt"

    window_seconds: int = 180
    threshold_none_arrays: int = 6
    threshold_withdraws: int = 6
    cooldown_seconds: int = 30

    poll_interval: float = 0.5

    # Bootstrap scan size per file (in bytes). Used to infer last PID/sandbox/item even
    # if the watchdog starts after the client already logged those lines.
    bootstrap_bytes: int = 5_000_000

    # Optional: periodically re-bootstrap from disk to correct inference if tail missed
    # lines (e.g. restarts). 0 disables.
    bootstrap_refresh_seconds: int = 60

    kill_osclient: bool = True
    terminate_sandbox: bool = True
    sandboxie_start_exe: str = r"C:\Program Files\Sandboxie-Plus\Start.exe"

    # When running under WSL, use Windows executables via /mnt/c.
    taskkill_exe: str = r"C:\Windows\System32\taskkill.exe"


@dataclass
class FileWatchStatus:
    path: str
    inferred_pid: int | None = None
    inferred_sandbox: str | None = None
    window_seconds: int = 0
    none_arrays: int = 0
    withdraws: int = 0
    last_withdraw_item: str | None = None
    last_action_at: str | None = None
    last_action: str | None = None


@dataclass
class WatchdogStatus:
    running: bool = False
    logs_dir: str = ""
    pattern: str = ""
    poll_interval: float = 0.0
    threshold_none_arrays: int = 0
    threshold_withdraws: int = 0
    cooldown_seconds: int = 0
    bootstrap_bytes: int = 0
    bootstrap_refresh_seconds: int = 0
    files: list[FileWatchStatus] = None  # type: ignore[assignment]


WATCHDOG_STATUS = WatchdogStatus(files=[])


def update_watchdog_config(patch: dict) -> None:
    """Best-effort runtime config update. Accepts a subset of WatchdogConfig fields."""
    global WATCHDOG_CONFIG
    if WATCHDOG_CONFIG is None:
        return

    allowed_int = {
        "window_seconds",
        "threshold_none_arrays",
        "threshold_withdraws",
        "cooldown_seconds",
        "bootstrap_bytes",
        "bootstrap_refresh_seconds",
    }
    allowed_float = {"poll_interval"}
    allowed_bool = {"kill_osclient", "terminate_sandbox"}

    for k, v in (patch or {}).items():
        if k in allowed_int:
            try:
                setattr(WATCHDOG_CONFIG, k, int(v))
            except Exception:
                pass
        elif k in allowed_float:
            try:
                setattr(WATCHDOG_CONFIG, k, float(v))
            except Exception:
                pass
        elif k in allowed_bool:
            if isinstance(v, bool):
                setattr(WATCHDOG_CONFIG, k, v)


WATCHDOG_CONFIG: WatchdogConfig | None = None


def _parse_ts(line: str) -> Optional[tuple[datetime, str]]:
    m = TS_RE.match(line.rstrip("\n"))
    if not m:
        return None
    try:
        ts = datetime.strptime(m.group(1), "%Y/%m/%d %H:%M:%S")
    except ValueError:
        return None
    return ts, m.group(2)


def _is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except Exception:
        return False


def _win_to_wsl_path(win_path: str) -> str:
    # Minimal conversion for absolute C:\ paths.
    m = re.match(r"^([a-zA-Z]):\\(.*)$", win_path)
    if not m:
        return win_path
    drive = m.group(1).lower()
    rest = m.group(2).replace("\\", "/")
    return f"/mnt/{drive}/{rest}"


def _run(cmd: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except Exception:
        return None


def _kill_pid(cfg: WatchdogConfig, pid: int) -> None:
    # Prefer targeted kill by PID.
    if os.name == "nt":
        _run([cfg.taskkill_exe, "/PID", str(pid), "/F"])
        return
    if _is_wsl():
        _run([_win_to_wsl_path(cfg.taskkill_exe), "/PID", str(pid), "/F"])


def _terminate_sandbox(cfg: WatchdogConfig, sandbox_name: str) -> None:
    if os.name == "nt":
        _run([cfg.sandboxie_start_exe, f"/terminate_all:{sandbox_name}"])
        return
    if _is_wsl():
        _run([_win_to_wsl_path(cfg.sandboxie_start_exe), f"/terminate_all:{sandbox_name}"])


class TailState:
    def __init__(self, path: Path):
        self.path = path
        self.f = None
        self.pos = 0
        self.recent: list[tuple[datetime, str]] = []
        self.last_action_at: datetime = datetime.min
        self.last_action: str | None = None
        self.inferred_sandbox: str | None = None
        self.inferred_pid: int | None = None
        self.last_bootstrap_at: datetime = datetime.min
        self.last_withdraw_item: str | None = None

    def close(self) -> None:
        if self.f:
            try:
                self.f.close()
            except Exception:
                pass
        self.f = None

    def open_if_needed(self) -> None:
        if self.f is not None:
            return
        self.f = self.path.open("r", encoding="utf-8", errors="replace")
        # Bootstrap inference by scanning a slice of the existing file.
        try:
            size = self.path.stat().st_size
            back = min(size, getattr(WATCHDOG_CONFIG, "bootstrap_bytes", 256_000) or 256_000)
            self.f.seek(size - back)
            bootstrap = self.f.read(back)
            for raw in bootstrap.splitlines():
                parsed = _parse_ts(raw)
                msg = parsed[1] if parsed else raw

                m_sb = SANDBOX_PATH_RE.search(msg)
                if m_sb:
                    self.inferred_sandbox = m_sb.group(1)

                m_pid = re.search(r"\bfound client pid=(\d+)", msg, re.IGNORECASE)
                if m_pid:
                    try:
                        self.inferred_pid = int(m_pid.group(1))
                    except ValueError:
                        pass

                m_w = WITHDRAW_RE.search(msg)
                if m_w:
                    self.last_withdraw_item = m_w.group(1).strip()

            self.last_bootstrap_at = datetime.now()
        except Exception:
            pass

        # Now follow from end.
        self.f.seek(0, os.SEEK_END)
        self.pos = self.f.tell()

    def read_new_lines(self) -> list[str]:
        if not self.path.exists():
            self.close()
            return []
        self.open_if_needed()
        assert self.f is not None

        self.f.seek(self.pos)
        data = self.f.read()
        self.pos = self.f.tell()
        if not data:
            return []
        return data.splitlines(True)


async def watchdog_loop(cfg: WatchdogConfig) -> None:
    tails: dict[Path, TailState] = {}

    global WATCHDOG_CONFIG
    WATCHDOG_CONFIG = cfg

    WATCHDOG_STATUS.running = True
    WATCHDOG_STATUS.logs_dir = str(cfg.logs_dir)
    WATCHDOG_STATUS.pattern = cfg.pattern
    WATCHDOG_STATUS.poll_interval = cfg.poll_interval
    WATCHDOG_STATUS.threshold_none_arrays = cfg.threshold_none_arrays
    WATCHDOG_STATUS.threshold_withdraws = cfg.threshold_withdraws
    WATCHDOG_STATUS.cooldown_seconds = cfg.cooldown_seconds
    WATCHDOG_STATUS.bootstrap_bytes = cfg.bootstrap_bytes
    WATCHDOG_STATUS.bootstrap_refresh_seconds = cfg.bootstrap_refresh_seconds

    while True:
        # Discover files
        for p in cfg.logs_dir.glob(cfg.pattern):
            if p not in tails:
                tails[p] = TailState(p)

        now = datetime.now()

        for state in list(tails.values()):
            # Periodic bootstrap refresh to catch missed PID/sandbox/item changes.
            if cfg.bootstrap_refresh_seconds and cfg.bootstrap_refresh_seconds > 0:
                if (now - state.last_bootstrap_at).total_seconds() >= cfg.bootstrap_refresh_seconds:
                    state.close()
                    state.open_if_needed()

            # If a file disappears, keep state but it will no-op.
            lines = state.read_new_lines()
            if not lines:
                continue

            for raw in lines:
                parsed = _parse_ts(raw)
                if parsed is None:
                    if state.inferred_sandbox is None:
                        m = SANDBOX_PATH_RE.search(raw)
                        if m:
                            state.inferred_sandbox = m.group(1)
                    continue
                ts, msg = parsed

                # Track the most recent OSRS client PID seen in this log.
                m_pid = re.search(r"\bfound client pid=(\d+)", msg, re.IGNORECASE)
                if m_pid:
                    try:
                        state.inferred_pid = int(m_pid.group(1))
                    except ValueError:
                        pass

                m_w = WITHDRAW_RE.search(msg)
                if m_w:
                    state.last_withdraw_item = m_w.group(1).strip()

                if state.inferred_sandbox is None:
                    m = SANDBOX_PATH_RE.search(msg)
                    if m:
                        state.inferred_sandbox = m.group(1)

                state.recent.append((ts, msg))

            # Prune window
            cutoff = now - timedelta(seconds=cfg.window_seconds)
            state.recent = [(t, m) for (t, m) in state.recent if t >= cutoff]

            none_arrays = sum(1 for _, m in state.recent if NONE_ARRAY_RE.search(m))
            withdraws = sum(1 for _, m in state.recent if WITHDRAW_RE.search(m))

            last_item = None
            for _, m in reversed(state.recent):
                m_w = WITHDRAW_RE.search(m)
                if m_w:
                    last_item = m_w.group(1).strip()
                    break

            cooldown_ok = (now - state.last_action_at).total_seconds() >= cfg.cooldown_seconds

            if cooldown_ok and none_arrays >= cfg.threshold_none_arrays and withdraws >= cfg.threshold_withdraws:
                state.last_action_at = now

                # Kill only the malfunctioning instance if we have its PID.
                if cfg.kill_osclient and state.inferred_pid is not None:
                    _kill_pid(cfg, state.inferred_pid)
                    state_last_action = f"killed pid {state.inferred_pid}"
                else:
                    state_last_action = None

                # Terminate Sandboxie sandbox if we can infer it from the log.
                if cfg.terminate_sandbox and state.inferred_sandbox:
                    _terminate_sandbox(cfg, state.inferred_sandbox)
                    if state_last_action:
                        state_last_action += f"; terminated sandbox {state.inferred_sandbox}"
                    else:
                        state_last_action = f"terminated sandbox {state.inferred_sandbox}"

                if state_last_action:
                    # store on state for UI
                    state.last_action = state_last_action

            # Update exported status snapshot (cheap; list is small).
            statuses: list[FileWatchStatus] = []
            for st in tails.values():
                last_at = None
                if st.last_action_at != datetime.min:
                    last_at = st.last_action_at.isoformat(sep=" ", timespec="seconds")
                statuses.append(
                    FileWatchStatus(
                        path=str(st.path),
                        inferred_pid=st.inferred_pid,
                        inferred_sandbox=st.inferred_sandbox,
                        window_seconds=cfg.window_seconds,
                        none_arrays=sum(1 for _, m in st.recent if NONE_ARRAY_RE.search(m)),
                        withdraws=sum(1 for _, m in st.recent if WITHDRAW_RE.search(m)),
                        last_withdraw_item=st.last_withdraw_item,
                        last_action_at=last_at,
                        last_action=getattr(st, "last_action", None),
                    )
                )
            WATCHDOG_STATUS.files = sorted(statuses, key=lambda s: s.path)

        await asyncio.sleep(cfg.poll_interval)
