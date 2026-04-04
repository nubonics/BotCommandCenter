from __future__ import annotations

import ctypes
import os
import threading
import time
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple

try:
    from screeninfo import get_monitors
except Exception:  # pragma: no cover
    get_monitors = None

user32 = ctypes.windll.user32

SW_RESTORE = 9
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_SHOWWINDOW = 0x0040
GWL_STYLE = -16
WS_MINIMIZE = 0x20000000


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

user32.EnumWindows.argtypes = [EnumWindowsProc, ctypes.c_void_p]
user32.EnumWindows.restype = ctypes.c_bool

user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
user32.IsWindowVisible.restype = ctypes.c_bool

user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
user32.GetWindowTextLengthW.restype = ctypes.c_int

user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int

user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(RECT)]
user32.GetWindowRect.restype = ctypes.c_bool

user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.ShowWindow.restype = ctypes.c_bool

user32.SetWindowPos.argtypes = [
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_uint,
]
user32.SetWindowPos.restype = ctypes.c_bool

user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long


@dataclass
class Slot:
    slot_index: int
    monitor_index: int
    x: int
    y: int

    occupied: bool = False
    hwnd: Optional[int] = None
    last_vacated_at: Optional[float] = None  # epoch seconds


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    left: int
    top: int
    width: int
    height: int


def _get_window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value.strip()


def _get_window_rect(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def _is_visible(hwnd: int) -> bool:
    return bool(user32.IsWindowVisible(hwnd))


def _is_minimized(hwnd: int) -> bool:
    style = user32.GetWindowLongW(hwnd, GWL_STYLE)
    return bool(style & WS_MINIMIZE)


def _restore_window(hwnd: int) -> None:
    user32.ShowWindow(hwnd, SW_RESTORE)


def _move_window_only(hwnd: int, x: int, y: int) -> None:
    ok = user32.SetWindowPos(
        hwnd,
        None,
        x,
        y,
        0,
        0,
        SWP_NOSIZE | SWP_NOZORDER | SWP_SHOWWINDOW,
    )
    if not ok:
        raise ctypes.WinError()


def enumerate_osclient_windows(exact_titles: List[str]) -> List[WindowInfo]:
    # Match the simple, reliable approach used in osrs-dwm-dashboard:
    # exact window title match on visible windows.
    titles = set(exact_titles)
    found: List[WindowInfo] = []
    seen = set()

    @EnumWindowsProc
    def enum_proc(hwnd, _lparam):
        try:
            ihwnd = int(hwnd)
            if ihwnd in seen:
                return True
            if not _is_visible(ihwnd):
                return True

            title = _get_window_title(ihwnd)
            if title not in titles:
                return True

            rect = _get_window_rect(ihwnd)
            if rect is None:
                return True
            left, top, right, bottom = rect
            found.append(
                WindowInfo(
                    hwnd=ihwnd,
                    title=title,
                    left=left,
                    top=top,
                    width=right - left,
                    height=bottom - top,
                )
            )
            seen.add(ihwnd)
        except Exception:
            pass
        return True

    user32.EnumWindows(enum_proc, 0)
    found.sort(key=lambda w: (w.top, w.left, w.hwnd))
    return found


class WindowSpreader:
    def __init__(
        self,
        poll_seconds: float = 5.0,
        reuse_cooldown_seconds: float = 120.0,
        exact_window_titles: Optional[List[str]] = None,
    ) -> None:
        self.poll_seconds = float(poll_seconds)
        self.reuse_cooldown_seconds = float(reuse_cooldown_seconds)
        self.exact_window_titles = exact_window_titles or ["Old School RuneScape"]

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._running = False
        self._slots: List[Slot] = []
        self._last_action: str = ""

        self._init_default_slots()

    def _init_default_slots(self) -> None:
        """Default mapping:
        Monitor 1 (index 0): 3 slots across top row => #1-#3
        Monitor 2 (index 1): 3 slots across top row => #7-#9

        This matches your example (slot 7 = top-left of monitor 2).
        Slots 4-6 and 10-12 can be added later for second-row, etc.
        """
        if get_monitors is None:
            self._slots = []
            return

        monitors = get_monitors() or []
        if not monitors:
            self._slots = []
            return

        def top_row_three(m, monitor_index: int, slot_start: int) -> List[Slot]:
            # left, mid, right positions
            x1 = m.x
            x2 = m.x + (m.width // 2)
            x3 = m.x + m.width
            # We don't know window width; we align by left edge.
            # For middle/right we will subtract window width at placement time.
            y = m.y
            return [
                Slot(slot_index=slot_start + 0, monitor_index=monitor_index, x=x1, y=y),
                Slot(slot_index=slot_start + 1, monitor_index=monitor_index, x=x2, y=y),
                Slot(slot_index=slot_start + 2, monitor_index=monitor_index, x=x3, y=y),
            ]

        slots: List[Slot] = []
        # Monitor 1 -> slots 1-3
        slots.extend(top_row_three(monitors[0], 0, 1))
        # Monitor 2 -> slots 7-9 if present
        if len(monitors) > 1:
            slots.extend(top_row_three(monitors[1], 1, 7))

        self._slots = slots

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._running = True
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
            self._last_action = f"Started window spreader (poll={self.poll_seconds}s, cooldown={self.reuse_cooldown_seconds}s)"

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            self._running = False
            self._last_action = "Stopped window spreader"
        if self._thread:
            self._thread.join(timeout=2.0)

    def is_running(self) -> bool:
        with self._lock:
            return bool(self._running)

    def last_action(self) -> str:
        with self._lock:
            return self._last_action

    def get_slots(self) -> List[dict]:
        now = time.time()
        with self._lock:
            out = []
            for s in self._slots:
                cooldown_remaining = 0
                if (not s.occupied) and s.last_vacated_at is not None:
                    cooldown_remaining = int(max(0, (s.last_vacated_at + self.reuse_cooldown_seconds) - now))
                out.append(
                    {
                        **asdict(s),
                        "cooldown_remaining_s": cooldown_remaining,
                    }
                )
            return out

    def tick(self) -> None:
        """One pass: discover windows, update occupancy, then place any unassigned windows."""
        windows = enumerate_osclient_windows(self.exact_window_titles)
        now = time.time()

        with self._lock:
            hwnds = {w.hwnd for w in windows}

            # Mark vacated slots.
            for s in self._slots:
                if s.occupied and (s.hwnd is not None) and (s.hwnd not in hwnds):
                    s.occupied = False
                    s.hwnd = None
                    s.last_vacated_at = now

            assigned = {s.hwnd for s in self._slots if s.occupied and s.hwnd is not None}

            # Windows that are not currently assigned to any slot.
            unassigned = [w for w in windows if w.hwnd not in assigned]

            # Place them in next available slots, but respect cooldown.
            moves = []
            for w in unassigned:
                slot = self._next_available_slot_locked(now)
                if slot is None:
                    break
                try:
                    # Slot x positions: left=exact; mid/right are anchors; adjust by width.
                    x = slot.x
                    if slot.slot_index % 3 == 2:  # "middle" (2,8,...) anchor at center
                        x = int(slot.x - (w.width // 2))
                    elif slot.slot_index % 3 == 0:  # "right" (3,9,...) anchor at right edge
                        x = int(slot.x - w.width)

                    if _is_minimized(w.hwnd):
                        _restore_window(w.hwnd)
                    _move_window_only(w.hwnd, x, slot.y)

                    slot.occupied = True
                    slot.hwnd = w.hwnd
                    slot.last_vacated_at = None
                    moves.append(f"slot #{slot.slot_index} <= hwnd={w.hwnd} @ ({x},{slot.y})")
                except Exception as e:
                    moves.append(f"FAILED placing hwnd={w.hwnd} into slot #{slot.slot_index}: {e}")

            if moves:
                self._last_action = "\n".join(moves)
            else:
                self._last_action = f"Tick ok. windows={len(windows)} assigned={len(assigned)} unassigned={len(unassigned)}"

    def _next_available_slot_locked(self, now: float) -> Optional[Slot]:
        for s in sorted(self._slots, key=lambda x: x.slot_index):
            if s.occupied:
                continue
            if s.last_vacated_at is not None and (now - s.last_vacated_at) < self.reuse_cooldown_seconds:
                continue
            return s
        return None

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception as e:
                with self._lock:
                    self._last_action = f"Loop error: {e}"
            self._stop_event.wait(self.poll_seconds)


# Singleton used by the web app.
_spreader = WindowSpreader(
    poll_seconds=float(os.getenv("WINDOW_SPREADER_POLL_SECONDS", "5")),
    reuse_cooldown_seconds=float(os.getenv("WINDOW_SPREADER_SLOT_COOLDOWN_SECONDS", "120")),
    exact_window_titles=[t.strip() for t in os.getenv("WINDOW_SPREADER_TITLES", "Old School RuneScape").split("|") if t.strip()],
)


def get_spreader() -> WindowSpreader:
    return _spreader
