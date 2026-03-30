import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import math

import keyboard
from screeninfo import get_monitors


EXACT_WINDOW_TITLES = {
    "Old School RuneScape",
    # "RuneLite",
}

VISIBLE_PER_PAGE = 6
ROWS_PER_PAGE = 2
ROW_STEP = 450
START_X_PADDING = 0
START_Y_PADDING = 0

# Hotkeys
NEXT_PAGE_HOTKEY = "f8"
PREV_PAGE_HOTKEY = "f7"
REFRESH_HOTKEY = "f6"

# Off-screen parking location for hidden windows
HIDE_X = -32000
HIDE_Y = -32000

RESTORE_WINDOWS = True
SORT_LEFT_TO_RIGHT_TOP_TO_BOTTOM = True


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


@dataclass
class ClientWindow:
    hwnd: int
    title: str
    left: int
    top: int
    width: int
    height: int


EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL

user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL

user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int

user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int

user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.restype = wintypes.BOOL

user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL

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

user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long


def get_window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value.strip()


def get_window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise ctypes.WinError()
    return rect.left, rect.top, rect.right, rect.bottom


def is_visible(hwnd: int) -> bool:
    return bool(user32.IsWindowVisible(hwnd))


def is_minimized(hwnd: int) -> bool:
    style = user32.GetWindowLongW(hwnd, GWL_STYLE)
    return bool(style & WS_MINIMIZE)


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
    return title in EXACT_WINDOW_TITLES


def get_osrs_windows() -> list[ClientWindow]:
    found: list[ClientWindow] = []
    seen = set()

    @EnumWindowsProc
    def enum_proc(hwnd, lparam):
        try:
            if hwnd in seen:
                return True
            if not is_visible(hwnd):
                return True

            title = get_window_title(hwnd)
            if not title_matches(title):
                return True

            left, top, right, bottom = get_window_rect(hwnd)
            found.append(
                ClientWindow(
                    hwnd=hwnd,
                    title=title,
                    left=left,
                    top=top,
                    width=right - left,
                    height=bottom - top,
                )
            )
            seen.add(hwnd)
        except Exception:
            pass
        return True

    user32.EnumWindows(enum_proc, 0)
    return found


def print_monitors(monitors) -> None:
    print("Detected monitors:")
    for i, m in enumerate(monitors):
        print(
            f"  [{i}] x={m.x}, y={m.y}, width={m.width}, height={m.height}, "
            f"is_primary={getattr(m, 'is_primary', False)}"
        )


def ask_int(prompt: str, min_value: int, max_value: int) -> int:
    while True:
        raw = input(prompt).strip()
        try:
            value = int(raw)
            if min_value <= value <= max_value:
                return value
        except ValueError:
            pass
        print("Invalid input.")


def choose_monitor(monitors, index: int):
    if index < 0 or index >= len(monitors):
        raise IndexError("Invalid monitor index.")
    return monitors[index]


def sort_windows(windows: list[ClientWindow]) -> list[ClientWindow]:
    if SORT_LEFT_TO_RIGHT_TOP_TO_BOTTOM:
        return sorted(windows, key=lambda w: (w.top, w.left, w.hwnd))
    return windows


def compute_row_x_positions(row_windows: list[ClientWindow], monitor) -> list[int] | None:
    count = len(row_windows)
    if count == 0:
        return []

    usable_left = monitor.x + START_X_PADDING
    usable_width = monitor.width - START_X_PADDING
    right_edge = monitor.x + monitor.width

    if count == 1:
        x_positions = [usable_left]
    else:
        max_start = usable_left + usable_width - row_windows[-1].width
        if max_start < usable_left:
            return None
        step = (max_start - usable_left) / (count - 1)
        x_positions = [round(usable_left + i * step) for i in range(count)]

    if len(set(x_positions)) != len(x_positions):
        return None

    for client, x in zip(row_windows, x_positions):
        if x + client.width > right_edge:
            return None

    return x_positions


def build_page_positions(page_windows: list[ClientWindow], monitor) -> list[tuple[ClientWindow, int, int]]:
    if not page_windows:
        return []

    usable_top = monitor.y + START_Y_PADDING
    monitor_bottom = monitor.y + monitor.height

    rows = []
    per_row = math.ceil(len(page_windows) / ROWS_PER_PAGE)

    start = 0
    for _ in range(ROWS_PER_PAGE):
        row = page_windows[start:start + per_row]
        if row:
            rows.append(row)
        start += per_row

    positions: list[tuple[ClientWindow, int, int]] = []

    for row_index, row_windows in enumerate(rows):
        y = usable_top + row_index * ROW_STEP
        row_height = max(w.height for w in row_windows)

        if y + row_height > monitor_bottom:
            raise RuntimeError(
                f"Row {row_index + 1} does not fit on screen. "
                f"Try a smaller ROW_STEP."
            )

        x_positions = compute_row_x_positions(row_windows, monitor)
        if x_positions is None:
            raise RuntimeError(
                f"Row {row_index + 1} does not fit horizontally. "
                f"Too many windows in the row for current sizes."
            )

        for client, x in zip(row_windows, x_positions):
            positions.append((client, x, y))

    return positions


class WindowPager:
    def __init__(self, monitor):
        self.monitor = monitor
        self.windows: list[ClientWindow] = []
        self.current_page = 0

    def refresh(self):
        previous_hwnds = [w.hwnd for w in self.windows]
        self.windows = sort_windows(get_osrs_windows())

        if not self.windows:
            self.current_page = 0
            print("\nNo OSRS windows found.")
            return

        if previous_hwnds != [w.hwnd for w in self.windows]:
            print(f"\nRefreshed. Found {len(self.windows)} OSRS windows.")

        max_page = self.page_count() - 1
        if self.current_page > max_page:
            self.current_page = max_page

    def page_count(self) -> int:
        if not self.windows:
            return 0
        return math.ceil(len(self.windows) / VISIBLE_PER_PAGE)

    def get_page_windows(self, page_index: int) -> list[ClientWindow]:
        start = page_index * VISIBLE_PER_PAGE
        end = start + VISIBLE_PER_PAGE
        return self.windows[start:end]

    def show_page(self, page_index: int):
        self.refresh()
        if not self.windows:
            return

        total_pages = self.page_count()
        self.current_page = max(0, min(page_index, total_pages - 1))

        visible_hwnds = {w.hwnd for w in self.get_page_windows(self.current_page)}

        # Hide non-page windows
        for client in self.windows:
            if client.hwnd not in visible_hwnds:
                move_window_only(client.hwnd, HIDE_X, HIDE_Y)

        # Show page windows
        page_windows = self.get_page_windows(self.current_page)
        positions = build_page_positions(page_windows, self.monitor)

        for client, x, y in positions:
            if RESTORE_WINDOWS and is_minimized(client.hwnd):
                restore_window(client.hwnd)
            move_window_only(client.hwnd, x, y)

        print(f"\nShowing page {self.current_page + 1}/{total_pages}")
        for idx, client in enumerate(page_windows, 1):
            print(f"  {idx}. hwnd={client.hwnd} | {client.title} | {client.width}x{client.height}")

    def next_page(self):
        if not self.windows:
            self.refresh()
        if not self.windows:
            return
        self.show_page((self.current_page + 1) % self.page_count())

    def prev_page(self):
        if not self.windows:
            self.refresh()
        if not self.windows:
            return
        self.show_page((self.current_page - 1) % self.page_count())


def main():
    monitors = get_monitors()
    if not monitors:
        print("No monitors detected.")
        return

    print_monitors(monitors)

    monitor_index = ask_int(
        f"\nWhich monitor index do you want to use? (0-{len(monitors) - 1}): ",
        0,
        len(monitors) - 1,
    )
    monitor = choose_monitor(monitors, monitor_index)

    pager = WindowPager(monitor)
    pager.refresh()

    if not pager.windows:
        print("\nNo OSRS client windows found.")
        print("Current exact matches are:", EXACT_WINDOW_TITLES)
        return

    print(f"\nFound {len(pager.windows)} OSRS client windows.")
    print(f"Pages: {pager.page_count()}")
    print(f"Visible per page: {VISIBLE_PER_PAGE}")
    print(f"Rows per page: {ROWS_PER_PAGE}")
    print(f"Row step: {ROW_STEP}")

    pager.show_page(0)

    keyboard.add_hotkey(NEXT_PAGE_HOTKEY, pager.next_page)
    keyboard.add_hotkey(PREV_PAGE_HOTKEY, pager.prev_page)
    keyboard.add_hotkey(REFRESH_HOTKEY, lambda: pager.show_page(pager.current_page))

    print(f"\nHotkeys:")
    print(f"  {REFRESH_HOTKEY.upper()} = refresh current page")
    print(f"  {PREV_PAGE_HOTKEY.upper()} = previous page")
    print(f"  {NEXT_PAGE_HOTKEY.upper()} = next page")
    print("Press ESC to quit.")

    keyboard.wait("esc")


if __name__ == "__main__":
    main()