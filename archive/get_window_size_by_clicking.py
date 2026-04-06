import sys
import win32api
import win32con
import win32gui
from pynput import mouse


def get_top_level_window(hwnd: int) -> int:
    """Return the top-level/root window for a child/control handle."""
    root = win32gui.GetAncestor(hwnd, win32con.GA_ROOT)
    return root if root else hwnd


def print_window_info(hwnd: int) -> None:
    """Print title, handle, position, and size for a window."""
    if not hwnd:
        print("No window found.")
        return

    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width = right - left
        height = bottom - top
        title = win32gui.GetWindowText(hwnd).strip() or "<No Title>"

        print("\nWindow info")
        print(f"Title : {title}")
        print(f"HWND  : {hwnd}")
        print(f"Left  : {left}")
        print(f"Top   : {top}")
        print(f"Right : {right}")
        print(f"Bottom: {bottom}")
        print(f"Width : {width}")
        print(f"Height: {height}")
    except Exception as e:
        print(f"Failed to read window info: {e}")


def on_click(x, y, button, pressed):
    """Handle the first left-click and stop the listener."""
    if pressed and button == mouse.Button.left:
        try:
            hwnd = win32gui.WindowFromPoint((x, y))
            hwnd = get_top_level_window(hwnd)
            print_window_info(hwnd)
        except Exception as e:
            print(f"Error getting clicked window: {e}")
        return False  # Stop listener after first click


def main():
    print("Click any window to get its size...")
    print("Press Ctrl+C to cancel.\n")

    try:
        with mouse.Listener(on_click=on_click) as listener:
            listener.join()
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(0)


if __name__ == "__main__":
    main()