import ctypes
from ctypes import wintypes
import keyboard

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi

DWMWA_EXTENDED_FRAME_BOUNDS = 9


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND

user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.restype = wintypes.BOOL

user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int

user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int

user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int

dwmapi.DwmGetWindowAttribute.argtypes = [
    wintypes.HWND,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
]
dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long


def get_window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def get_class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def get_window_rect(hwnd: int):
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise ctypes.WinError()
    return rect.left, rect.top, rect.right, rect.bottom


def get_dwm_frame_bounds(hwnd: int):
    rect = RECT()
    result = dwmapi.DwmGetWindowAttribute(
        hwnd,
        DWMWA_EXTENDED_FRAME_BOUNDS,
        ctypes.byref(rect),
        ctypes.sizeof(rect),
    )
    if result != 0:
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def print_rect(label: str, rect):
    left, top, right, bottom = rect
    print(f"{label}:")
    print(f"  Left:   {left}")
    print(f"  Top:    {top}")
    print(f"  Right:  {right}")
    print(f"  Bottom: {bottom}")
    print(f"  Width:  {right - left}")
    print(f"  Height: {bottom - top}")


def capture_active_window():
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        print("No active window found.")
        return

    print("\nActive window:")
    print(f"Handle: {hwnd}")
    print(f"Title: {get_window_text(hwnd) or '[No Title]'}")
    print(f"Class: {get_class_name(hwnd)}")

    try:
        rect = get_window_rect(hwnd)
        print_rect("GetWindowRect", rect)
    except Exception as e:
        print(f"GetWindowRect failed: {e}")

    dwm_rect = get_dwm_frame_bounds(hwnd)
    if dwm_rect:
        print_rect("DWM Extended Frame Bounds", dwm_rect)
    else:
        print("DWM Extended Frame Bounds: unavailable")


def main():
    print("Focus the target window.")
    print("Press F8 to capture its dimensions.")
    print("Press ESC to quit.")

    keyboard.add_hotkey("F8", capture_active_window)
    keyboard.wait("esc")


if __name__ == "__main__":
    main()