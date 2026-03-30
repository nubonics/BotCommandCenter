import ctypes
from ctypes import wintypes
from dataclasses import dataclass

import pygetwindow as gw
from screeninfo import get_monitors


WINDOW_TITLE_KEYWORDS = [
    "RuneLite",
    "Old School RuneScape",
    "OSRS",
]

START_X = 50
START_Y = 50
OFFSET_X = 40
OFFSET_Y = 40
PREFER_VISIBLE_ONLY = False

user32 = ctypes.windll.user32

SW_RESTORE = 9
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_SHOWWINDOW = 0x0040


user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL

user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL

user32.SetWindowPos.argtypes = [
    wintypes.HWND,
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
user32.SetWindowPos.restype = wintypes.BOOL


@dataclass
class ClientWindow:
    hwnd: int
    title: str


def is_visible(hwnd: int) -> bool:
    return bool(user32.IsWindowVisible(hwnd))


def restore_window(hwnd: int) -> None:
    user32.ShowWindow(hwnd, SW_RESTORE)


def move_window_only(hwnd: int, x: int, y: int) -> None:
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


def title_matches(title: str) -> bool:
    if not title:
        return False
    t = title.lower()
    return any(keyword.lower() in t for keyword in WINDOW_TITLE_KEYWORDS)


def get_osrs_windows() -> list[ClientWindow]:
    results = []
    seen_hwnds = set()

    for w in gw.getAllWindows():
        try:
            hwnd = getattr(w, "_hWnd", None)
            title = w.title or ""

            if not hwnd or hwnd in seen_hwnds:
                continue
            if not title_matches(title):
                continue
            if PREFER_VISIBLE_ONLY and not is_visible(hwnd):
                continue

            seen_hwnds.add(hwnd)
            results.append(ClientWindow(hwnd=hwnd, title=title))
        except Exception:
            continue

    return results


def recover_windows() -> None:
    monitors = get_monitors()
    if not monitors:
        raise RuntimeError("No monitors detected.")

    primary = next((m for m in monitors if getattr(m, "is_primary", False)), monitors[0])

    windows = get_osrs_windows()
    if not windows:
        print("No OSRS windows found.")
        return

    print(f"Found {len(windows)} OSRS window(s).")

    base_x = primary.x + START_X
    base_y = primary.y + START_Y

    for i, client in enumerate(windows):
        x = base_x + (i * OFFSET_X)
        y = base_y + (i * OFFSET_Y)

        restore_window(client.hwnd)
        move_window_only(client.hwnd, x, y)

        print(f"Moved: {client.title}")
        print(f"  hwnd={client.hwnd}")
        print(f"  x={x}, y={y}")


if __name__ == "__main__":
    recover_windows()