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
    pattern: str = "sara*.txt"

    window_seconds: int = 180
    threshold_none_arrays: int = 6
    threshold_withdraws: int = 6
    cooldown_seconds: int = 30

    poll_interval: float = 0.5

    kill_osclient: bool = True
    terminate_sandbox: bool = True
    sandboxie_start_exe: str = "Start.exe"  # set full path if not in PATH


def _parse_ts(line: str) -> Optional[tuple[datetime, str]]:
    m = TS_RE.match(line.rstrip("\n"))
    if not m:
        return None
    try:
        ts = datetime.strptime(m.group(1), "%Y/%m/%d %H:%M:%S")
    except ValueError:
        return None
    return ts, m.group(2)


def _run(cmd: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except Exception:
        return None


def _taskkill_osclient() -> None:
    # Windows-only. No-op on non-Windows hosts.
    if os.name != "nt":
        return
    _run(["taskkill", "/IM", "osclient.exe", "/F"])


def _terminate_sandbox(start_exe: str, sandbox_name: str) -> None:
    if os.name != "nt":
        return
    _run([start_exe, f"/terminate_all:{sandbox_name}"])


class TailState:
    def __init__(self, path: Path):
        self.path = path
        self.f = None
        self.pos = 0
        self.recent: list[tuple[datetime, str]] = []
        self.last_action_at: datetime = datetime.min
        self.inferred_sandbox: str | None = None

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

    while True:
        # Discover files
        for p in cfg.logs_dir.glob(cfg.pattern):
            if p not in tails:
                tails[p] = TailState(p)

        now = datetime.now()

        for state in list(tails.values()):
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

            cooldown_ok = (now - state.last_action_at).total_seconds() >= cfg.cooldown_seconds

            if cooldown_ok and none_arrays >= cfg.threshold_none_arrays and withdraws >= cfg.threshold_withdraws:
                state.last_action_at = now

                # Kill osclient.exe
                if cfg.kill_osclient:
                    _taskkill_osclient()

                # Terminate Sandboxie sandbox if we can infer it from the log.
                if cfg.terminate_sandbox and state.inferred_sandbox:
                    _terminate_sandbox(cfg.sandboxie_start_exe, state.inferred_sandbox)

        await asyncio.sleep(cfg.poll_interval)

