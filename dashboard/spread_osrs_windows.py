import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import math

from screeninfo import get_monitors


EXACT_WINDOW_TITLES = {
    "Old School RuneScape",
    # "RuneLite",
}

ROWS_PER_MONITOR = 2
ROW_STEP = 260
START_X_PADDING = 0
START_Y_PADDING = 0
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


def choose_monitors(monitors, count: int):
    primary = next((m for m in monitors if getattr(m, "is_primary", False)), monitors[0])
    ordered = [primary] + [m for m in monitors if m is not primary]
    return ordered[:count]


def sort_windows(windows: list[ClientWindow]) -> list[ClientWindow]:
    if SORT_LEFT_TO_RIGHT_TOP_TO_BOTTOM:
        return sorted(windows, key=lambda w: (w.top, w.left))
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
        step = (max_start - usable_left) / (count - 1)
        x_positions = [round(usable_left + i * step) for i in range(count)]

    if len(set(x_positions)) != len(x_positions):
        return None

    for client, x in zip(row_windows, x_positions):
        if x + client.width > right_edge:
            return None

    return x_positions


def build_positions_for_monitor(
    windows: list[ClientWindow],
    monitor,
    rows_per_monitor: int,
    row_step: int,
) -> tuple[list[tuple[ClientWindow, int, int]], list[ClientWindow]]:
    positions = []
    leftovers = windows[:]

    if not leftovers:
        return positions, leftovers

    usable_top = monitor.y + START_Y_PADDING
    monitor_bottom = monitor.y + monitor.height

    while leftovers:
        remaining_count = len(leftovers)
        per_row = math.ceil(remaining_count / rows_per_monitor)

        rows = []
        start = 0
        for _ in range(rows_per_monitor):
            row = leftovers[start:start + per_row]
            if row:
                rows.append(row)
            start += per_row

        if not rows:
            break

        can_place_all_rows = True

        for row_index, row_windows in enumerate(rows):
            y = usable_top + row_index * row_step
            row_height = max(w.height for w in row_windows)

            if y + row_height > monitor_bottom:
                can_place_all_rows = False
                break

            x_positions = compute_row_x_positions(row_windows, monitor)
            if x_positions is None:
                can_place_all_rows = False
                break

            for client, x in zip(row_windows, x_positions):
                positions.append((client, x, y))

        if can_place_all_rows:
            leftovers = leftovers[len(sum(rows, [])):]
            break

        # If all remaining windows don't fit in this monitor using the target split,
        # try fewer windows on this monitor by peeling one off the end.
        leftovers = leftovers[:-1]
        positions.clear()

        if not leftovers:
            break

    placed_hwnds = {client.hwnd for client, _, _ in positions}
    true_leftovers = [w for w in windows if w.hwnd not in placed_hwnds]

    return positions, true_leftovers


def arrange_windows_across_monitors(
    windows: list[ClientWindow],
    selected_monitors,
    rows_per_monitor: int,
    row_step: int,
) -> None:
    remaining = windows[:]
    all_positions: list[tuple[ClientWindow, int, int]] = []

    for mi, monitor in enumerate(selected_monitors):
        if not remaining:
            break

        positions, remaining = build_positions_for_monitor(
            remaining,
            monitor,
            rows_per_monitor,
            row_step,
        )
        all_positions.extend(positions)
        print(
            f"Monitor {mi}: placed {len(positions)} window(s), "
            f"{len(remaining)} remaining"
        )

    for client, x, y in all_positions:
        if RESTORE_WINDOWS and is_minimized(client.hwnd):
            restore_window(client.hwnd)

        move_window_only(client.hwnd, x, y)
        print(
            f"Moved: {client.title}\n"
            f"  hwnd={client.hwnd}\n"
            f"  x={x}, y={y}, width={client.width}, height={client.height}"
        )

    if remaining:
        print("\nThese windows could not be placed:")
        for client in remaining:
            print(f"  hwnd={client.hwnd} | {client.title} | {client.width}x{client.height}")


def main() -> None:
    monitors = get_monitors()
    if not monitors:
        print("No monitors detected.")
        return

    print_monitors(monitors)

    windows = get_osrs_windows()
    if not windows:
        print("\nNo OSRS client windows found.")
        print("Current exact matches are:", EXACT_WINDOW_TITLES)
        return

    windows = sort_windows(windows)

    print(f"\nFound {len(windows)} OSRS client window(s):")
    for i, w in enumerate(windows, 1):
        print(f"  {i}. hwnd={w.hwnd} | {w.title} | {w.width}x{w.height} | ({w.left}, {w.top})")

    monitor_count = ask_int(
        f"\nHow many monitors do you want to use? (1-{len(monitors)}): ",
        1,
        len(monitors),
    )

    row_step = ask_int(
        "\nRow step in pixels (try 240-280): ",
        1,
        2000,
    )

    selected_monitors = choose_monitors(monitors, monitor_count)

    print("\nUsing monitors:")
    for i, m in enumerate(selected_monitors):
        print(f"  [{i}] x={m.x}, y={m.y}, width={m.width}, height={m.height}")

    print(f"\nUsing {ROWS_PER_MONITOR} rows per monitor")
    print(f"Using row step: {row_step}")

    arrange_windows_across_monitors(
        windows,
        selected_monitors,
        ROWS_PER_MONITOR,
        row_step,
    )
    print("\nDone.")


if __name__ == "__main__":
    main()