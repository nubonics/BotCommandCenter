from __future__ import annotations

import ctypes
import os
import threading
import time
from dataclasses import dataclass, asdict
from typing import List, Optional, Tuple

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
    """Enumerate OSRS client windows.

    Mirrors osrs-dwm-dashboard's approach:
    - EnumWindows
    - only visible windows
    - exact title match
    - uses GetWindowRect for geometry
    """
    titles = set(exact_titles)
    found: List[WindowInfo] = []
    seen: set[int] = set()

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

        # Manual overrides: slot_index -> hwnd
        self._pinned: dict[int, int] = {}

        # Slot creation can be delayed until first tick.
        self._init_default_slots()

    def _init_default_slots(self) -> None:
        """Auto-detect monitors and create 2 rows × 3 columns of slots per monitor."""
        if get_monitors is None:
            self._slots = []
            return

        monitors = get_monitors() or []
        if not monitors:
            self._slots = []
            return

        monitors = sorted(monitors, key=lambda m: (getattr(m, "x", 0), getattr(m, "y", 0)))

        start_x_padding = int(os.getenv("WINDOW_SPREADER_START_X_PADDING", "0"))
        start_y_padding = int(os.getenv("WINDOW_SPREADER_START_Y_PADDING", "0"))
        row_step = int(os.getenv("WINDOW_SPREADER_ROW_STEP", "260"))

        def anchors_2x3(m) -> List[Tuple[int, int]]:
            left_x = int(m.x + start_x_padding)
            center_x = int(m.x + (m.width // 2))
            right_x = int(m.x + m.width - 1)

            top_y = int(m.y + start_y_padding)
            y2 = int(top_y + row_step)

            return [
                (left_x, top_y),
                (center_x, top_y),
                (right_x, top_y),
                (left_x, y2),
                (center_x, y2),
                (right_x, y2),
            ]

        slots: List[Slot] = []
        slot_index = 1
        for mi, m in enumerate(monitors):
            for (x, y) in anchors_2x3(m):
                slots.append(Slot(slot_index=slot_index, monitor_index=mi, x=x, y=y))
                slot_index += 1

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
                        "pinned": (s.slot_index in self._pinned),
                    }
                )
            return out

    def set_pinned(self, slot_index: int, hwnd: Optional[int]) -> None:
        with self._lock:
            if hwnd is None:
                self._pinned.pop(int(slot_index), None)
            else:
                self._pinned[int(slot_index)] = int(hwnd)

    def tick(self) -> None:
        """One pass: discover windows, update occupancy, enforce pinning, enforce slot positions, auto-assign remaining."""
        # Lazily initialize slots (some environments may not have monitors ready at import time).
        with self._lock:
            if not self._slots:
                self._init_default_slots()

        windows = enumerate_osclient_windows(self.exact_window_titles)
        by_hwnd = {w.hwnd: w for w in windows}
        now = time.time()

        with self._lock:
            hwnds = set(by_hwnd.keys())

            # Mark slots vacated if their window disappeared.
            for s in self._slots:
                if s.occupied and (s.hwnd is not None) and (s.hwnd not in hwnds):
                    s.occupied = False
                    s.hwnd = None
                    s.last_vacated_at = now

            # Apply pinned assignments first (slot_index -> hwnd)
            pinned_moves: list[str] = []
            for s in self._slots:
                want_hwnd = self._pinned.get(s.slot_index)
                if want_hwnd is None:
                    continue
                if want_hwnd not in hwnds:
                    pinned_moves.append(f"slot #{s.slot_index} pin missing (hwnd={want_hwnd} not found)")
                    continue

                # If that hwnd is currently occupying a different slot, free it.
                for other in self._slots:
                    if other.slot_index != s.slot_index and other.occupied and other.hwnd == want_hwnd:
                        other.occupied = False
                        other.hwnd = None
                        other.last_vacated_at = now

                s.occupied = True
                s.hwnd = want_hwnd
                s.last_vacated_at = None

                # Enforce placement for pinned window.
                w = by_hwnd.get(want_hwnd)
                if w:
                    x = self._slot_target_x(s.slot_index, s.x, w.width)
                    try:
                        if _is_minimized(w.hwnd):
                            _restore_window(w.hwnd)
                        _move_window_only(w.hwnd, x, s.y)
                        pinned_moves.append(f"pinned: slot #{s.slot_index} <= hwnd={w.hwnd} @ ({x},{s.y})")
                    except Exception as e:
                        pinned_moves.append(f"FAILED pinned move hwnd={w.hwnd} slot #{s.slot_index}: {e}")

            # Enforce slot position for assigned windows (snap back if manually moved).
            tolerance = int(os.getenv("WINDOW_SPREADER_POSITION_TOLERANCE_PX", "10"))
            snap_moves: list[str] = []
            for s in self._slots:
                if not s.occupied or s.hwnd is None:
                    continue
                w = by_hwnd.get(s.hwnd)
                if not w:
                    continue
                target_x = self._slot_target_x(s.slot_index, s.x, w.width)
                target_y = s.y
                if abs(w.left - target_x) > tolerance or abs(w.top - target_y) > tolerance:
                    try:
                        if _is_minimized(w.hwnd):
                            _restore_window(w.hwnd)
                        _move_window_only(w.hwnd, target_x, target_y)
                        snap_moves.append(f"snapback: hwnd={w.hwnd} -> slot #{s.slot_index} @ ({target_x},{target_y})")
                    except Exception as e:
                        snap_moves.append(f"FAILED snapback hwnd={w.hwnd} slot #{s.slot_index}: {e}")

            assigned = {s.hwnd for s in self._slots if s.occupied and s.hwnd is not None}

            # Windows that are not currently assigned to any slot.
            unassigned = [w for w in windows if w.hwnd not in assigned]

            # Auto-assign remaining windows to next available slots (respect cooldown).
            moves = []
            for w in unassigned:
                slot = self._next_available_slot_locked(now)
                if slot is None:
                    break
                try:
                    x = self._slot_target_x(slot.slot_index, slot.x, w.width)

                    if _is_minimized(w.hwnd):
                        _restore_window(w.hwnd)
                    _move_window_only(w.hwnd, x, slot.y)

                    slot.occupied = True
                    slot.hwnd = w.hwnd
                    slot.last_vacated_at = None
                    moves.append(f"auto: slot #{slot.slot_index} <= hwnd={w.hwnd} @ ({x},{slot.y})")
                except Exception as e:
                    moves.append(f"FAILED placing hwnd={w.hwnd} into slot #{slot.slot_index}: {e}")

            lines: list[str] = []
            lines.extend(pinned_moves)
            lines.extend(snap_moves)
            lines.extend(moves)

            if lines:
                self._last_action = "\n".join(lines)
            else:
                self._last_action = f"Tick ok. windows={len(windows)} assigned={len(assigned)} unassigned={len(unassigned)}"

    def _slot_target_x(self, slot_index: int, anchor_x: int, window_width: int) -> int:
        # Slot x positions are anchors: left/center/right.
        col = (int(slot_index) - 1) % 3  # 0=left,1=center,2=right
        if col == 1:
            return int(anchor_x - (window_width // 2))
        if col == 2:
            return int(anchor_x - window_width)
        return int(anchor_x)

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
