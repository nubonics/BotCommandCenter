from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import math
import os
import threading
import time
from dataclasses import asdict, dataclass
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import psutil
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from mss import mss
from PIL import Image, ImageDraw, ImageFont


BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

TARGET_PROCESS = os.getenv("TARGET_PROCESS", "osclient.exe").lower()
BOARD_WIDTH = int(os.getenv("BOARD_WIDTH", "1920"))
BOARD_HEIGHT = int(os.getenv("BOARD_HEIGHT", "1080"))
CAPTURE_FPS = max(1, int(os.getenv("CAPTURE_FPS", "5")))
DISCOVERY_INTERVAL = float(os.getenv("DISCOVERY_INTERVAL", "1.0"))
JPEG_QUALITY = max(30, min(95, int(os.getenv("JPEG_QUALITY", "70"))))
TILE_GAP = max(0, int(os.getenv("TILE_GAP", "0")))
SHOW_TITLES = os.getenv("SHOW_TITLES", "0") == "1"
TITLE_BAR_HEIGHT = int(os.getenv("TITLE_BAR_HEIGHT", "18")) if SHOW_TITLES else 0
ALLOW_SCREEN_FALLBACK = os.getenv("SCREEN_FALLBACK", "0") == "1"
ALLOW_PRINTWINDOW_FALLBACK = os.getenv("PRINTWINDOW_FALLBACK", "0") == "1"
KEEP_LAST_GOOD_TILE = os.getenv("KEEP_LAST_GOOD_TILE", "1") == "1"
BLANK_FRAME_THRESHOLD = max(0, min(30, int(os.getenv("BLANK_FRAME_THRESHOLD", "2"))))
GRID_COLS = max(1, int(os.getenv("GRID_COLS", "8")))
COLS_SLIDER_MIN = max(1, int(os.getenv("COLS_SLIDER_MIN", "1")))
COLS_SLIDER_MAX = max(COLS_SLIDER_MIN, int(os.getenv("COLS_SLIDER_MAX", "10")))
FPS_SLIDER_MIN = max(1, int(os.getenv("FPS_SLIDER_MIN", "1")))
FPS_SLIDER_MAX = max(FPS_SLIDER_MIN, int(os.getenv("FPS_SLIDER_MAX", "15")))
FILL_MODE = os.getenv("FILL_MODE", "cover").strip().lower()
FLASH_DURATION = max(0.25, float(os.getenv("FLASH_DURATION", "3.0")))
FLASH_INTERVAL = max(0.05, float(os.getenv("FLASH_INTERVAL", "0.08")))
FLASH_ALPHA = max(1, min(255, int(os.getenv("FLASH_ALPHA", "110"))))

PW_CLIENTONLY = 0x00000001
BI_RGB = 0
DIB_RGB_COLORS = 0
SRCCOPY = 0x00CC0020
SW_RESTORE = 9
SW_SHOW = 5
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040
LWA_ALPHA = 0x00000002
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_POPUP = 0x80000000
FLASH_CLASS_NAME = "OSClientWallFlashOverlay"
COLORREF_GREEN = 0x0000FF00

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

# Some Python builds do not expose these aliases.
if not hasattr(wintypes, "HINSTANCE"):
    wintypes.HINSTANCE = wintypes.HANDLE
if not hasattr(wintypes, "HICON"):
    wintypes.HICON = wintypes.HANDLE
if not hasattr(wintypes, "HCURSOR"):
    wintypes.HCURSOR = wintypes.HANDLE
if not hasattr(wintypes, "HBRUSH"):
    wintypes.HBRUSH = wintypes.HANDLE

WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
LRESULT = ctypes.c_ssize_t

EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HCURSOR),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", ctypes.c_ushort),
        ("biBitCount", ctypes.c_ushort),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class RGBQUAD(ctypes.Structure):
    _fields_ = [
        ("rgbBlue", ctypes.c_ubyte),
        ("rgbGreen", ctypes.c_ubyte),
        ("rgbRed", ctypes.c_ubyte),
        ("rgbReserved", ctypes.c_ubyte),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", RGBQUAD * 1)]


user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
user32.DefWindowProcW.restype = LRESULT

try:
    user32.SetProcessDPIAware()
except Exception:
    pass


@dataclass
class WindowInfo:
    hwnd: int
    pid: int
    title: str
    process_name: str
    left: int
    top: int
    right: int
    bottom: int
    client_width: int
    client_height: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)


_flash_class_registered = False
_flash_class_brush = None
_flash_wndproc_ref = None


def _overlay_wndproc(hwnd, msg, wparam, lparam):
    try:
        return int(user32.DefWindowProcW(hwnd, msg, wparam, lparam))
    except Exception:
        return 0


def ensure_flash_overlay_class() -> bool:
    global _flash_class_registered, _flash_class_brush, _flash_wndproc_ref
    if _flash_class_registered:
        return True

    _flash_wndproc_ref = WNDPROC(_overlay_wndproc)
    if _flash_class_brush is None:
        _flash_class_brush = gdi32.CreateSolidBrush(COLORREF_GREEN)

    wc = WNDCLASSW()
    wc.lpfnWndProc = _flash_wndproc_ref
    wc.hInstance = kernel32.GetModuleHandleW(None)
    wc.lpszClassName = FLASH_CLASS_NAME
    wc.hbrBackground = _flash_class_brush

    atom = user32.RegisterClassW(ctypes.byref(wc))
    if atom == 0:
        err = kernel32.GetLastError()
        if err != 1410:  # class already exists
            return False

    _flash_class_registered = True
    return True


def get_window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value.strip()


def get_window_pid(hwnd: int) -> int:
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def get_window_rect(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def get_client_size(hwnd: int) -> Optional[Tuple[int, int]]:
    rect = RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    width = max(0, rect.right - rect.left)
    height = max(0, rect.bottom - rect.top)
    if width <= 0 or height <= 0:
        return None
    return width, height


def get_client_screen_rect(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    rect = RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None

    top_left = POINT(rect.left, rect.top)
    bottom_right = POINT(rect.right, rect.bottom)
    if not user32.ClientToScreen(hwnd, ctypes.byref(top_left)):
        return None
    if not user32.ClientToScreen(hwnd, ctypes.byref(bottom_right)):
        return None

    left = top_left.x
    top = top_left.y
    right = bottom_right.x
    bottom = bottom_right.y
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def create_or_update_flash_overlay(target_hwnd: int, overlay_hwnd: Optional[int] = None) -> Optional[int]:
    if not ensure_flash_overlay_class():
        return None

    rect = get_client_screen_rect(target_hwnd)
    if rect is None:
        return None
    left, top, right, bottom = rect
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        return None

    if overlay_hwnd and not user32.IsWindow(overlay_hwnd):
        overlay_hwnd = None

    if overlay_hwnd is None:
        overlay_hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            FLASH_CLASS_NAME,
            None,
            WS_POPUP,
            left,
            top,
            width,
            height,
            None,
            None,
            kernel32.GetModuleHandleW(None),
            None,
        )
        if not overlay_hwnd:
            return None
        user32.SetLayeredWindowAttributes(overlay_hwnd, 0, FLASH_ALPHA, LWA_ALPHA)

    user32.SetWindowPos(overlay_hwnd, HWND_TOPMOST, left, top, width, height, SWP_SHOWWINDOW)
    user32.ShowWindow(overlay_hwnd, SW_SHOW)
    user32.UpdateWindow(overlay_hwnd)
    return overlay_hwnd


def destroy_flash_overlay(overlay_hwnd: Optional[int]) -> None:
    if overlay_hwnd and user32.IsWindow(overlay_hwnd):
        user32.DestroyWindow(overlay_hwnd)


class FlashManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._until_by_hwnd: Dict[int, float] = {}
        self._overlay_by_hwnd: Dict[int, int] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.5)
        with self._lock:
            overlays = list(self._overlay_by_hwnd.values())
            self._overlay_by_hwnd.clear()
            self._until_by_hwnd.clear()
        for overlay_hwnd in overlays:
            destroy_flash_overlay(overlay_hwnd)

    def request_flash(self, hwnd: int, duration: float = FLASH_DURATION) -> None:
        until = time.perf_counter() + max(0.1, duration)
        with self._lock:
            self._until_by_hwnd[hwnd] = max(self._until_by_hwnd.get(hwnd, 0.0), until)

    def is_flashing(self, hwnd: int) -> bool:
        now = time.perf_counter()
        with self._lock:
            return self._until_by_hwnd.get(hwnd, 0.0) > now

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            now = time.perf_counter()
            with self._lock:
                active = dict(self._until_by_hwnd)
                overlay_cache = dict(self._overlay_by_hwnd)

            stale_targets: List[int] = []
            refreshed: Dict[int, int] = {}

            for hwnd, until in active.items():
                if until <= now or not user32.IsWindow(hwnd):
                    stale_targets.append(hwnd)
                    continue
                overlay_hwnd = create_or_update_flash_overlay(hwnd, overlay_cache.get(hwnd))
                if overlay_hwnd:
                    refreshed[hwnd] = overlay_hwnd
                else:
                    stale_targets.append(hwnd)

            overlays_to_destroy: List[int] = []
            with self._lock:
                self._overlay_by_hwnd.update(refreshed)
                for hwnd in stale_targets:
                    self._until_by_hwnd.pop(hwnd, None)
                    overlay_hwnd = self._overlay_by_hwnd.pop(hwnd, None)
                    if overlay_hwnd:
                        overlays_to_destroy.append(overlay_hwnd)

            for overlay_hwnd in overlays_to_destroy:
                destroy_flash_overlay(overlay_hwnd)

            self._stop_event.wait(FLASH_INTERVAL)


class CaptureManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._capture_thread: Optional[threading.Thread] = None
        self._discover_thread: Optional[threading.Thread] = None
        self._latest_jpeg: bytes = b""
        self._latest_frame_id = 0
        self._target_fps = CAPTURE_FPS
        self._grid_cols = GRID_COLS
        self._windows: List[WindowInfo] = []
        self._tile_layout: List[Dict[str, object]] = []
        self._last_good_by_hwnd: Dict[int, Image.Image] = {}
        self._stats: Dict[str, object] = {
            "target_process": TARGET_PROCESS,
            "target_fps": CAPTURE_FPS,
            "actual_fps": 0.0,
            "window_count": 0,
            "board_width": BOARD_WIDTH,
            "board_height": BOARD_HEIGHT,
            "jpeg_quality": JPEG_QUALITY,
            "last_frame_ms": 0.0,
            "updated_at": 0.0,
            "capture_mode": "gdi-client-dc",
            "printwindow_fallback": ALLOW_PRINTWINDOW_FALLBACK,
            "screen_fallback": ALLOW_SCREEN_FALLBACK,
            "keep_last_good_tile": KEEP_LAST_GOOD_TILE,
            "fill_mode": FILL_MODE,
            "flash_duration": FLASH_DURATION,
            "fps_slider_min": FPS_SLIDER_MIN,
            "fps_slider_max": FPS_SLIDER_MAX,
            "cols_slider_min": COLS_SLIDER_MIN,
            "cols_slider_max": COLS_SLIDER_MAX,
            "grid_cols": GRID_COLS,
        }
        try:
            self._font = ImageFont.load_default()
        except Exception:
            self._font = None

    def start(self) -> None:
        if self._capture_thread and self._capture_thread.is_alive():
            return
        self._stop_event.clear()
        self._discover_thread = threading.Thread(target=self._discover_loop, daemon=True)
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._discover_thread.start()
        self._capture_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._discover_thread:
            self._discover_thread.join(timeout=2)
        if self._capture_thread:
            self._capture_thread.join(timeout=2)

    def get_stats(self) -> Dict[str, object]:
        with self._lock:
            return dict(self._stats)

    def get_windows(self) -> List[Dict[str, object]]:
        with self._lock:
            return [asdict(w) | {"width": w.width, "height": w.height} for w in self._windows]

    def get_layout(self) -> List[Dict[str, object]]:
        with self._lock:
            return [dict(item) for item in self._tile_layout]

    def get_latest_frame(self) -> Tuple[int, bytes]:
        with self._lock:
            return self._latest_frame_id, self._latest_jpeg

    def set_target_fps(self, fps: int) -> int:
        applied = max(FPS_SLIDER_MIN, min(FPS_SLIDER_MAX, int(fps)))
        with self._lock:
            self._target_fps = applied
            self._stats["target_fps"] = applied
        return applied

    def set_grid_cols(self, cols: int) -> int:
        applied = max(COLS_SLIDER_MIN, min(COLS_SLIDER_MAX, int(cols)))
        with self._lock:
            self._grid_cols = applied
            self._stats["grid_cols"] = applied
        return applied

    def _discover_loop(self) -> None:
        while not self._stop_event.is_set():
            windows = enumerate_target_windows(TARGET_PROCESS)
            current_hwnds = {w.hwnd for w in windows}
            with self._lock:
                self._windows = windows
                self._stats["window_count"] = len(windows)
                stale = [hwnd for hwnd in self._last_good_by_hwnd if hwnd not in current_hwnds]
                for hwnd in stale:
                    self._last_good_by_hwnd.pop(hwnd, None)
            self._stop_event.wait(DISCOVERY_INTERVAL)

    def _capture_loop(self) -> None:
        frame_times: List[float] = []
        sct = mss() if ALLOW_SCREEN_FALLBACK else None
        try:
            while not self._stop_event.is_set():
                started = time.perf_counter()
                with self._lock:
                    windows = list(self._windows)
                    current_target_fps = self._target_fps
                    current_grid_cols = self._grid_cols

                board, layout = self._render_board(sct, windows, current_grid_cols)
                buffer = BytesIO()
                board.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=False)
                jpeg = buffer.getvalue()

                finished = time.perf_counter()
                elapsed = finished - started
                frame_times.append(finished)
                cutoff = finished - 1.0
                while frame_times and frame_times[0] < cutoff:
                    frame_times.pop(0)
                actual_fps = float(len(frame_times))

                with self._lock:
                    self._latest_jpeg = jpeg
                    self._latest_frame_id += 1
                    self._tile_layout = layout
                    self._stats.update(
                        {
                            "actual_fps": round(actual_fps, 2),
                            "last_frame_ms": round(elapsed * 1000.0, 2),
                            "updated_at": time.time(),
                            "target_fps": current_target_fps,
                            "grid_cols": current_grid_cols,
                            "board_width": board.width,
                            "board_height": board.height,
                        }
                    )

                delay = max(0.0, (1.0 / max(1, current_target_fps)) - elapsed)
                self._stop_event.wait(delay)
        finally:
            if sct is not None:
                sct.close()

    def _render_board(self, sct: Optional[mss], windows: List[WindowInfo], grid_cols: int) -> Tuple[Image.Image, List[Dict[str, object]]]:
        layout: List[Dict[str, object]] = []

        if not windows:
            board = Image.new("RGB", (BOARD_WIDTH, BOARD_HEIGHT), (8, 8, 8))
            draw = ImageDraw.Draw(board)
            draw.text((20, 20), f"No visible {TARGET_PROCESS} windows found.", fill=(220, 220, 220), font=self._font)
            draw.text((20, 48), "Windows must be visible and not minimized.", fill=(180, 180, 180), font=self._font)
            return board, layout

        avg_window_aspect = max(0.1, average_window_aspect(windows))
        cols = max(1, min(len(windows), grid_cols))
        rows = math.ceil(len(windows) / cols)
        cell_w = max(1, BOARD_WIDTH // cols)
        thumb_w = max(1, cell_w - TILE_GAP * 2)
        usable_h = max(1, int(round(thumb_w / avg_window_aspect)))
        cell_h = usable_h + TITLE_BAR_HEIGHT + TILE_GAP * 2
        board_height = max(1, rows * cell_h)

        board = Image.new("RGB", (BOARD_WIDTH, board_height), (8, 8, 8))
        draw = ImageDraw.Draw(board)

        for index, win in enumerate(windows):
            col = index % cols
            row = index // cols
            x = col * cell_w + TILE_GAP
            y = row * cell_h + TILE_GAP

            source = self._capture_best_image(sct, win.hwnd)
            status = "live"
            if source is not None:
                self._last_good_by_hwnd[win.hwnd] = source.copy()
                thumb = resize_tile_image(source, thumb_w, usable_h)
            else:
                cached = self._last_good_by_hwnd.get(win.hwnd)
                if KEEP_LAST_GOOD_TILE and cached is not None:
                    thumb = resize_tile_image(cached, thumb_w, usable_h)
                    status = "cached"
                else:
                    thumb = self._make_error_tile(thumb_w, usable_h, win.title or "Untitled")
                    status = "missing"

            if flash_manager.is_flashing(win.hwnd):
                thumb = apply_green_tint(thumb, FLASH_ALPHA)
                status += "-flash"

            if SHOW_TITLES:
                title = truncate_text(f"{index + 1:02d}. {win.title or 'Untitled'}", 34)
                draw.rectangle([x, y, x + thumb_w - 1, y + TITLE_BAR_HEIGHT], fill=(28, 28, 28))
                draw.text((x + 4, y + 3), title, fill=(235, 235, 235), font=self._font)
                board.paste(thumb, (x, y + TITLE_BAR_HEIGHT))
                tile_top = y + TITLE_BAR_HEIGHT
            else:
                board.paste(thumb, (x, y))
                tile_top = y

            layout.append(
                {
                    "index": index,
                    "hwnd": win.hwnd,
                    "title": win.title,
                    "x": x,
                    "y": y,
                    "tile_top": tile_top,
                    "width": thumb_w,
                    "height": usable_h,
                    "status": status,
                }
            )

        return board, layout

    def _capture_best_image(self, sct: Optional[mss], hwnd: int) -> Optional[Image.Image]:
        img = capture_window_client_image_gdi(hwnd)
        if img is not None and not is_probably_blank(img):
            return img

        if ALLOW_PRINTWINDOW_FALLBACK:
            img = capture_window_client_image_printwindow(hwnd)
            if img is not None and not is_probably_blank(img):
                return img

        if ALLOW_SCREEN_FALLBACK and sct is not None:
            img = capture_window_client_from_screen(sct, hwnd)
            if img is not None and not is_probably_blank(img):
                return img

        return None

    def _make_error_tile(self, width: int, height: int, text: str) -> Image.Image:
        img = Image.new("RGB", (max(1, width), max(1, height)), (16, 16, 16))
        draw = ImageDraw.Draw(img)
        draw.text((8, 8), truncate_text(text or "Capture unavailable", 28), fill=(180, 180, 180), font=self._font)
        return img


def truncate_text(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def average_window_aspect(windows: List[WindowInfo]) -> float:
    aspects = [w.client_width / max(1, w.client_height) for w in windows if w.client_width > 0 and w.client_height > 0]
    if not aspects:
        return 1.0
    return sum(aspects) / len(aspects)


def fit_image(img: Image.Image, max_w: int, max_h: int) -> Image.Image:
    max_w = max(1, max_w)
    max_h = max(1, max_h)
    src_w, src_h = img.size
    scale = min(max_w / src_w, max_h / src_h)
    new_w = max(1, int(round(src_w * scale)))
    new_h = max(1, int(round(src_h * scale)))
    resized = img.resize((new_w, new_h), Image.Resampling.BILINEAR)
    canvas = Image.new("RGB", (max_w, max_h), (0, 0, 0))
    x = (max_w - new_w) // 2
    y = (max_h - new_h) // 2
    canvas.paste(resized, (x, y))
    return canvas


def cover_image(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    target_w = max(1, target_w)
    target_h = max(1, target_h)
    src_w, src_h = img.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w = max(1, int(round(src_w * scale)))
    new_h = max(1, int(round(src_h * scale)))
    resized = img.resize((new_w, new_h), Image.Resampling.BILINEAR)
    left = max(0, (new_w - target_w) // 2)
    top = max(0, (new_h - target_h) // 2)
    return resized.crop((left, top, left + target_w, top + target_h))


def resize_tile_image(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    if FILL_MODE == "fit":
        return fit_image(img, target_w, target_h)
    return cover_image(img, target_w, target_h)


def apply_green_tint(img: Image.Image, alpha: int) -> Image.Image:
    overlay = Image.new("RGBA", img.size, (0, 255, 0, max(0, min(255, alpha))))
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def is_probably_blank(img: Image.Image) -> bool:
    try:
        sample = img if img.width <= 64 and img.height <= 64 else img.resize((64, 64), Image.Resampling.BILINEAR)
        grayscale = sample.convert("L")
        lo, hi = grayscale.getextrema()
        return hi <= BLANK_FRAME_THRESHOLD and lo <= BLANK_FRAME_THRESHOLD
    except Exception:
        return False


def is_alt_tab_candidate(hwnd: int) -> bool:
    if not user32.IsWindowVisible(hwnd):
        return False
    if user32.IsIconic(hwnd):
        return False
    title = get_window_text(hwnd)
    if not title:
        return False
    rect = get_window_rect(hwnd)
    if rect is None:
        return False
    left, top, right, bottom = rect
    if right - left <= 0 or bottom - top <= 0:
        return False
    if get_client_size(hwnd) is None:
        return False
    return True


def enumerate_target_windows(target_process: str) -> List[WindowInfo]:
    windows: List[WindowInfo] = []
    pid_name_cache: Dict[int, str] = {}

    @EnumWindowsProc
    def callback(hwnd: int, _lparam: int) -> bool:
        try:
            if not is_alt_tab_candidate(hwnd):
                return True

            pid = get_window_pid(hwnd)
            if pid <= 0:
                return True

            process_name = pid_name_cache.get(pid)
            if process_name is None:
                try:
                    process_name = psutil.Process(pid).name().lower()
                except Exception:
                    process_name = ""
                pid_name_cache[pid] = process_name
            if process_name != target_process:
                return True

            rect = get_window_rect(hwnd)
            if rect is None:
                return True
            client_size = get_client_size(hwnd)
            if client_size is None:
                return True

            left, top, right, bottom = rect
            client_width, client_height = client_size
            windows.append(
                WindowInfo(
                    hwnd=int(hwnd),
                    pid=pid,
                    title=get_window_text(hwnd),
                    process_name=process_name,
                    left=left,
                    top=top,
                    right=right,
                    bottom=bottom,
                    client_width=client_width,
                    client_height=client_height,
                )
            )
        except Exception:
            return True
        return True

    user32.EnumWindows(callback, 0)
    windows.sort(key=lambda w: (w.top, w.left, w.hwnd))
    return windows


def _capture_dib_from_dc(src_dc: int, width: int, height: int) -> Optional[Image.Image]:
    mem_dc = None
    dib = None
    old_obj = None
    bits_ptr = ctypes.c_void_p()

    try:
        mem_dc = gdi32.CreateCompatibleDC(src_dc)
        if not mem_dc:
            return None

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        bmi.bmiHeader.biSizeImage = width * height * 4

        dib = gdi32.CreateDIBSection(src_dc, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits_ptr), None, 0)
        if not dib or not bits_ptr:
            return None

        old_obj = gdi32.SelectObject(mem_dc, dib)
        if not old_obj:
            return None

        if not gdi32.BitBlt(mem_dc, 0, 0, width, height, src_dc, 0, 0, SRCCOPY):
            return None

        size = width * height * 4
        raw = ctypes.string_at(bits_ptr, size)
        return Image.frombuffer("RGBA", (width, height), raw, "raw", "BGRA", 0, 1).convert("RGB").copy()
    except Exception:
        return None
    finally:
        if old_obj and mem_dc:
            gdi32.SelectObject(mem_dc, old_obj)
        if dib:
            gdi32.DeleteObject(dib)
        if mem_dc:
            gdi32.DeleteDC(mem_dc)


def capture_window_client_image_gdi(hwnd: int) -> Optional[Image.Image]:
    client_size = get_client_size(hwnd)
    if client_size is None:
        return None
    width, height = client_size
    if width <= 0 or height <= 0:
        return None

    window_dc = user32.GetDC(hwnd)
    if not window_dc:
        return None
    try:
        return _capture_dib_from_dc(window_dc, width, height)
    finally:
        user32.ReleaseDC(hwnd, window_dc)


def capture_window_client_image_printwindow(hwnd: int) -> Optional[Image.Image]:
    client_size = get_client_size(hwnd)
    if client_size is None:
        return None
    width, height = client_size
    if width <= 0 or height <= 0:
        return None

    window_dc = user32.GetDC(hwnd)
    if not window_dc:
        return None

    mem_dc = None
    dib = None
    old_obj = None
    bits_ptr = ctypes.c_void_p()

    try:
        mem_dc = gdi32.CreateCompatibleDC(window_dc)
        if not mem_dc:
            return None

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        bmi.bmiHeader.biSizeImage = width * height * 4

        dib = gdi32.CreateDIBSection(window_dc, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits_ptr), None, 0)
        if not dib or not bits_ptr:
            return None

        old_obj = gdi32.SelectObject(mem_dc, dib)
        if not old_obj:
            return None

        if not user32.PrintWindow(hwnd, mem_dc, PW_CLIENTONLY):
            return None

        size = width * height * 4
        raw = ctypes.string_at(bits_ptr, size)
        return Image.frombuffer("RGBA", (width, height), raw, "raw", "BGRA", 0, 1).convert("RGB").copy()
    except Exception:
        return None
    finally:
        if old_obj and mem_dc:
            gdi32.SelectObject(mem_dc, old_obj)
        if dib:
            gdi32.DeleteObject(dib)
        if mem_dc:
            gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, window_dc)


def capture_window_client_from_screen(sct: mss, hwnd: int) -> Optional[Image.Image]:
    rect = get_client_screen_rect(hwnd)
    if rect is None:
        return None
    left, top, right, bottom = rect
    if right <= left or bottom <= top:
        return None

    try:
        shot = sct.grab({"left": left, "top": top, "width": right - left, "height": bottom - top})
        return Image.frombytes("RGB", shot.size, shot.rgb)
    except Exception:
        return None


def focus_window(hwnd: int) -> bool:
    if not user32.IsWindow(hwnd):
        return False

    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        else:
            user32.ShowWindow(hwnd, SW_SHOW)

        foreground = user32.GetForegroundWindow()
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        foreground_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        current_thread = kernel32.GetCurrentThreadId()

        attached_to_target = False
        attached_to_foreground = False

        if target_thread and target_thread != current_thread:
            attached_to_target = bool(user32.AttachThreadInput(current_thread, target_thread, True))
        if foreground_thread and foreground_thread != current_thread and foreground_thread != target_thread:
            attached_to_foreground = bool(user32.AttachThreadInput(current_thread, foreground_thread, True))

        try:
            user32.BringWindowToTop(hwnd)
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            user32.SetForegroundWindow(hwnd)
            user32.SetFocus(hwnd)
            user32.SetActiveWindow(hwnd)
            return bool(user32.GetForegroundWindow() == hwnd)
        finally:
            if attached_to_foreground and foreground_thread:
                user32.AttachThreadInput(current_thread, foreground_thread, False)
            if attached_to_target and target_thread:
                user32.AttachThreadInput(current_thread, target_thread, False)
    except Exception:
        return False


app = FastAPI(title="OSClient Wall")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
manager = CaptureManager()
flash_manager = FlashManager()


@app.on_event("startup")
def on_startup() -> None:
    manager.start()
    flash_manager.start()


@app.on_event("shutdown")
def on_shutdown() -> None:
    flash_manager.stop()
    manager.stop()


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "board_width": BOARD_WIDTH,
            "board_height": BOARD_HEIGHT,
            "target_process": TARGET_PROCESS,
            "target_fps": CAPTURE_FPS,
            "fps_slider_min": FPS_SLIDER_MIN,
            "fps_slider_max": FPS_SLIDER_MAX,
            "grid_cols": GRID_COLS,
            "cols_slider_min": COLS_SLIDER_MIN,
            "cols_slider_max": COLS_SLIDER_MAX,
        },
    )


@app.get("/api/stats")
def api_stats() -> JSONResponse:
    return JSONResponse(manager.get_stats())


@app.get("/api/windows")
def api_windows() -> JSONResponse:
    return JSONResponse(manager.get_windows())


@app.get("/api/layout")
def api_layout() -> JSONResponse:
    return JSONResponse(manager.get_layout())


@app.post("/api/focus/{hwnd}")
def api_focus(hwnd: int) -> JSONResponse:
    windows = {item["hwnd"] for item in manager.get_windows()}
    if hwnd not in windows:
        raise HTTPException(status_code=404, detail="Window not found")
    ok = focus_window(hwnd)
    flash_manager.request_flash(hwnd)
    return JSONResponse({"ok": ok, "hwnd": hwnd, "flashing": True, "flash_duration": FLASH_DURATION})


@app.post("/api/settings/fps")
def api_set_fps(payload: dict) -> JSONResponse:
    fps = int(payload.get("fps", CAPTURE_FPS))
    applied = manager.set_target_fps(fps)
    return JSONResponse({"ok": True, "target_fps": applied})


@app.post("/api/settings/cols")
def api_set_cols(payload: dict) -> JSONResponse:
    cols = int(payload.get("cols", GRID_COLS))
    applied = manager.set_grid_cols(cols)
    return JSONResponse({"ok": True, "grid_cols": applied})


@app.get("/stream.mjpg")
def stream_mjpg() -> StreamingResponse:
    boundary = "frame"

    def generate():
        last_id = -1
        while True:
            frame_id, jpeg = manager.get_latest_frame()
            if frame_id != last_id and jpeg:
                last_id = frame_id
                yield (
                    b"--" + boundary.encode() + b"\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n"
                )
            else:
                time.sleep(0.005)

    return StreamingResponse(
        generate(),
        media_type=f"multipart/x-mixed-replace; boundary={boundary}",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8005, reload=False)
