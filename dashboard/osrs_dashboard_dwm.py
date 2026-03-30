import sys
from dataclasses import dataclass

import ctypes
from ctypes import wintypes

import psutil
import win32con
import win32gui
import win32api
import win32process

from PySide6 import QtCore, QtGui, QtWidgets


# --------------------
# Config
# --------------------
MAX_WINDOWS = 40

# Process names (lowercase)
EXE_OFFICIAL = "osclient.exe"
EXE_RUNELITE = "runelite.exe"

LAYOUT_PRESETS = {
    "Overview (40) 8×5": (8, 5),
    "Paged (15) 5×3": (5, 3),
    "Paged (12) 4×3": (4, 3),
    "Paged (9) 3×3": (3, 3),
}


# --------------------
# DWM Thumbnail plumbing (ctypes)
# --------------------
# Note: DWM thumbnails are "live" surfaces; no FPS/timer capture loop required.

dwmapi = ctypes.windll.dwmapi

# Define function prototypes (critical on 64-bit; prevents HWND/HANDLE truncation)
# ctypes.wintypes doesn't always expose HRESULT, so define it.
HRESULT = ctypes.c_long
dwmapi.DwmRegisterThumbnail.argtypes = [
    wintypes.HWND,
    wintypes.HWND,
    ctypes.POINTER(wintypes.HANDLE),
]
dwmapi.DwmRegisterThumbnail.restype = HRESULT

dwmapi.DwmUnregisterThumbnail.argtypes = [wintypes.HANDLE]
dwmapi.DwmUnregisterThumbnail.restype = HRESULT

# We'll set DwmUpdateThumbnailProperties argtypes after DWM_THUMBNAIL_PROPERTIES is defined.
dwmapi.DwmUpdateThumbnailProperties.restype = HRESULT

# DWM_THUMBNAIL_PROPERTIES flags
DWM_TNP_RECTDESTINATION = 0x00000001
DWM_TNP_RECTSOURCE = 0x00000002
DWM_TNP_OPACITY = 0x00000004
DWM_TNP_VISIBLE = 0x00000008
DWM_TNP_SOURCECLIENTAREAONLY = 0x00000010


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class DWM_THUMBNAIL_PROPERTIES(ctypes.Structure):
    _fields_ = [
        ("dwFlags", wintypes.DWORD),
        ("rcDestination", RECT),
        ("rcSource", RECT),
        ("opacity", wintypes.BYTE),
        ("fVisible", wintypes.BOOL),
        ("fSourceClientAreaOnly", wintypes.BOOL),
    ]


# Now that DWM_THUMBNAIL_PROPERTIES exists, set correct argtypes.
dwmapi.DwmUpdateThumbnailProperties.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(DWM_THUMBNAIL_PROPERTIES),
]


def _ok(hr: int) -> bool:
    return hr >= 0


def dwm_register_thumbnail(dest_hwnd: int, src_hwnd: int):
    thumb = wintypes.HANDLE()
    hr = dwmapi.DwmRegisterThumbnail(
        wintypes.HWND(dest_hwnd), wintypes.HWND(src_hwnd), ctypes.byref(thumb)
    )
    if not _ok(hr):
        raise OSError(f"DwmRegisterThumbnail failed: 0x{hr & 0xFFFFFFFF:08X}")
    return thumb


def dwm_unregister_thumbnail(thumb):
    if not thumb:
        return
    try:
        dwmapi.DwmUnregisterThumbnail(thumb)
    except Exception:
        pass


def dwm_update_thumbnail(thumb, dest_rect: RECT, visible: bool = True, opacity: int = 255):
    props = DWM_THUMBNAIL_PROPERTIES()
    props.dwFlags = (
        DWM_TNP_RECTDESTINATION
        | DWM_TNP_VISIBLE
        | DWM_TNP_OPACITY
        | DWM_TNP_SOURCECLIENTAREAONLY
    )
    props.rcDestination = dest_rect
    props.fVisible = bool(visible)
    props.opacity = max(0, min(255, int(opacity)))
    props.fSourceClientAreaOnly = True

    hr = dwmapi.DwmUpdateThumbnailProperties(thumb, ctypes.byref(props))
    if not _ok(hr):
        raise OSError(f"DwmUpdateThumbnailProperties failed: 0x{hr & 0xFFFFFFFF:08X}")


# --------------------
# Window enumeration + focus
# --------------------
@dataclass
class TrackedWindow:
    hwnd: int
    title: str
    exe: str  # lowercase


def get_exe_name_from_hwnd(hwnd: int) -> str | None:
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if not pid:
            return None
        return (psutil.Process(pid).name() or "").lower()
    except Exception:
        return None


def enum_osrs_windows(show_official: bool, show_runelite: bool) -> list[TrackedWindow]:
    wanted = set()
    if show_official:
        wanted.add(EXE_OFFICIAL)
    if show_runelite:
        wanted.add(EXE_RUNELITE)

    out: list[TrackedWindow] = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True

        title = (win32gui.GetWindowText(hwnd) or "").strip()
        if not title:
            return True

        exe = get_exe_name_from_hwnd(hwnd)
        if not exe or exe not in wanted:
            return True

        out.append(TrackedWindow(hwnd=hwnd, title=title, exe=exe))
        return True

    win32gui.EnumWindows(cb, None)

    def sort_key(w: TrackedWindow):
        # RuneLite first
        client_rank = 0 if w.exe == EXE_RUNELITE else 1
        return (client_rank, w.title.lower(), w.hwnd)

    out.sort(key=sort_key)
    return out[:MAX_WINDOWS]


def focus_window(hwnd: int):
    """Bring a target OSRS window to the foreground.

    Windows is often hostile to focus-stealing; this uses a few common tricks:
    - restore if minimized
    - ALT-key "permission" nudge
    - TOPMOST toggle to force Z-order
    - best-effort SetForegroundWindow

    Intentionally does NOT FlashWindow on failure (avoids dashboard blinking).
    """
    if not win32gui.IsWindow(hwnd):
        return

    # Restore if minimized
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    except win32gui.error:
        pass

    # ALT trick (often helps SetForegroundWindow succeed)
    try:
        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
    except Exception:
        pass

    try:
        fg = win32gui.GetForegroundWindow()
        fg_tid, _ = win32process.GetWindowThreadProcessId(fg)
        this_tid = win32api.GetCurrentThreadId()
        target_tid, _ = win32process.GetWindowThreadProcessId(hwnd)

        win32process.AttachThreadInput(this_tid, fg_tid, True)
        win32process.AttachThreadInput(this_tid, target_tid, True)

        # TOPMOST toggle: forces window to front, then back to normal
        win32gui.BringWindowToTop(hwnd)
        win32gui.SetWindowPos(
            hwnd,
            win32con.HWND_TOPMOST,
            0, 0, 0, 0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
        )
        win32gui.SetWindowPos(
            hwnd,
            win32con.HWND_NOTOPMOST,
            0, 0, 0, 0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
        )

        # Best-effort foreground
        try:
            win32gui.SetForegroundWindow(hwnd)
        except win32gui.error:
            pass
        try:
            win32gui.SetActiveWindow(hwnd)
        except win32gui.error:
            pass

    except win32gui.error:
        return
    finally:
        try:
            win32process.AttachThreadInput(win32api.GetCurrentThreadId(), fg_tid, False)
        except Exception:
            pass
        try:
            win32process.AttachThreadInput(win32api.GetCurrentThreadId(), target_tid, False)
        except Exception:
            pass


# --------------------
# UI widgets
# --------------------
class ThumbnailView(QtWidgets.QWidget):
    """A native QWidget that DWM can render a live thumbnail into."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(10, 10)

        # Must be native so it has an HWND
        self.setAttribute(QtCore.Qt.WA_NativeWindow, True)
        self.setAttribute(QtCore.Qt.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)

        self._src_hwnd: int | None = None
        self._thumb = None

        self.setStyleSheet("background: #111;")

    def set_source(self, src_hwnd: int | None):
        if src_hwnd == self._src_hwnd:
            return
        self._src_hwnd = src_hwnd
        self._recreate_thumbnail()

    def clear(self):
        self._src_hwnd = None
        self._destroy_thumbnail()

    def _destroy_thumbnail(self):
        if self._thumb:
            try:
                dwm_unregister_thumbnail(self._thumb)
            finally:
                self._thumb = None

    def _recreate_thumbnail(self):
        self._destroy_thumbnail()
        if not self._src_hwnd:
            return
        if not win32gui.IsWindow(self._src_hwnd):
            return

        dest_hwnd = int(self.winId())
        try:
            self._thumb = dwm_register_thumbnail(dest_hwnd, self._src_hwnd)
            self._update_thumb_geometry()
        except Exception as e:
            # Helpful when running from a terminal: shows why thumbnails are blank.
            print("Thumbnail error:", repr(e), "src=", self._src_hwnd, "dest=", dest_hwnd)
            self._destroy_thumbnail()

    def _update_thumb_geometry(self):
        if not self._thumb:
            return
        w = max(1, self.width())
        h = max(1, self.height())
        dest = RECT(0, 0, w, h)
        dwm_update_thumbnail(self._thumb, dest_rect=dest, visible=True, opacity=255)

    def resizeEvent(self, e: QtGui.QResizeEvent):
        super().resizeEvent(e)
        try:
            self._update_thumb_geometry()
        except Exception:
            pass

    def showEvent(self, e: QtGui.QShowEvent):
        super().showEvent(e)
        try:
            self._recreate_thumbnail()
        except Exception:
            pass

    def closeEvent(self, e: QtGui.QCloseEvent):
        self._destroy_thumbnail()
        super().closeEvent(e)


class WindowTile(QtWidgets.QFrame):
    clicked = QtCore.Signal(int)

    def __init__(self, hwnd: int, title: str, subtitle: str):
        super().__init__()
        self.hwnd = hwnd

        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.setLineWidth(1)
        self.setCursor(QtCore.Qt.PointingHandCursor)

        self.thumb = ThumbnailView(self)

        self.title_label = QtWidgets.QLabel(title)
        self.title_label.setStyleSheet("font-size: 10px; padding: 2px;")
        self.title_label.setToolTip(title)
        self.title_label.setWordWrap(False)

        self.sub_label = QtWidgets.QLabel(subtitle)
        self.sub_label.setStyleSheet(
            "font-size: 9px; color: #666; padding: 0 2px 2px 2px;"
        )
        self.sub_label.setWordWrap(False)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)
        layout.addWidget(self.thumb, 1)
        layout.addWidget(self.title_label, 0)
        layout.addWidget(self.sub_label, 0)

        self.thumb.set_source(hwnd)

    def mousePressEvent(self, e: QtGui.QMouseEvent):
        if e.button() == QtCore.Qt.LeftButton:
            self.clicked.emit(self.hwnd)
        super().mousePressEvent(e)

    def cleanup(self):
        self.thumb.clear()


class Dashboard(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("OSRS Dashboard (DWM thumbnails) — click to focus")

        self.tiles_by_hwnd: dict[int, WindowTile] = {}
        self._last_windows: list[TrackedWindow] = []

        self.page = 0
        self.cols, self.rows = LAYOUT_PRESETS["Overview (40) 8×5"]

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)

        outer = QtWidgets.QVBoxLayout(central)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(6)

        controls = QtWidgets.QHBoxLayout()
        controls.setSpacing(12)

        self.cb_runelite = QtWidgets.QCheckBox("RuneLite")
        self.cb_runelite.setChecked(True)

        self.cb_official = QtWidgets.QCheckBox("Official (osclient.exe)")
        self.cb_official.setChecked(True)

        self.layout_box = QtWidgets.QComboBox()
        self.layout_box.addItems(LAYOUT_PRESETS.keys())
        self.layout_box.setCurrentText("Overview (40) 8×5")

        self.btn_prev = QtWidgets.QPushButton("◀ Prev")
        self.btn_next = QtWidgets.QPushButton("Next ▶")
        self.page_label = QtWidgets.QLabel("Page 1/1")

        self.btn_refresh = QtWidgets.QPushButton("Refresh list")

        controls.addWidget(self.cb_runelite)
        controls.addWidget(self.cb_official)
        controls.addSpacing(12)
        controls.addWidget(self.layout_box)
        controls.addWidget(self.btn_prev)
        controls.addWidget(self.btn_next)
        controls.addWidget(self.page_label)
        controls.addStretch(1)
        controls.addWidget(self.btn_refresh)

        outer.addLayout(controls)

        self.grid_host = QtWidgets.QWidget()
        self.grid = QtWidgets.QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(6)
        outer.addWidget(self.grid_host, 1)

        # signals
        self.btn_refresh.clicked.connect(self.refresh_window_list)
        self.cb_runelite.stateChanged.connect(self.refresh_window_list)
        self.cb_official.stateChanged.connect(self.refresh_window_list)
        self.layout_box.currentTextChanged.connect(self.on_layout_changed)
        self.btn_prev.clicked.connect(lambda: self.change_page(-1))
        self.btn_next.clicked.connect(lambda: self.change_page(+1))

        # timer (re-enumerate windows)
        self.refresh_timer = QtCore.QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_window_list)
        self.refresh_timer.start(2000)

        self.refresh_window_list()

    def per_page(self) -> int:
        return self.cols * self.rows

    def is_overview_mode(self) -> bool:
        return self.layout_box.currentText().startswith("Overview")

    def on_layout_changed(self, name: str):
        self.cols, self.rows = LAYOUT_PRESETS[name]
        self.page = 0
        self.refresh_window_list()

    def change_page(self, delta: int):
        total = len(self._last_windows)
        pages = max(1, (total + self.per_page() - 1) // self.per_page())
        self.page = max(0, min(self.page + delta, pages - 1))
        self.refresh_window_list()

    def visible_slice(self):
        start = self.page * self.per_page()
        end = start + self.per_page()
        return start, end

    def _tile_subtitle(self, exe: str) -> str:
        return "RuneLite.exe" if exe == EXE_RUNELITE else "osclient.exe"

    def refresh_window_list(self):
        windows = enum_osrs_windows(
            show_official=self.cb_official.isChecked(),
            show_runelite=self.cb_runelite.isChecked(),
        )
        self._last_windows = windows

        alive_hwnds = {w.hwnd for w in windows}

        # remove dead tiles
        for hwnd in list(self.tiles_by_hwnd.keys()):
            if hwnd not in alive_hwnds or not win32gui.IsWindow(hwnd):
                tile = self.tiles_by_hwnd.pop(hwnd)
                tile.cleanup()
                tile.setParent(None)
                tile.deleteLater()

        # add new tiles
        for w in windows:
            if w.hwnd not in self.tiles_by_hwnd:
                tile = WindowTile(w.hwnd, w.title, self._tile_subtitle(w.exe))
                tile.clicked.connect(self.on_tile_clicked)
                self.tiles_by_hwnd[w.hwnd] = tile

        # paging
        total = len(windows)
        pages = max(1, (total + self.per_page() - 1) // self.per_page())
        if self.page >= pages:
            self.page = pages - 1
        if self.is_overview_mode():
            self.page = 0

        start, end = self.visible_slice()
        page_windows = windows[start:end]

        self.page_label.setText(f"Page {self.page + 1}/{pages}")
        self.btn_prev.setEnabled((not self.is_overview_mode()) and self.page > 0)
        self.btn_next.setEnabled((not self.is_overview_mode()) and self.page < pages - 1)

        # relayout grid
        for i in reversed(range(self.grid.count())):
            w = self.grid.itemAt(i).widget()
            if w:
                w.setParent(None)

        for idx, w in enumerate(page_windows):
            r = idx // self.cols
            c = idx % self.cols
            self.grid.addWidget(self.tiles_by_hwnd[w.hwnd], r, c)

        # in paged modes, free GPU by unregistering thumbnails for hidden tiles
        if not self.is_overview_mode():
            visible_hwnds = {w.hwnd for w in page_windows}
            for hwnd, tile in self.tiles_by_hwnd.items():
                if hwnd in visible_hwnds:
                    tile.thumb.set_source(hwnd)
                else:
                    tile.thumb.clear()

    def on_tile_clicked(self, hwnd: int):
        focus_window(hwnd)


def main():
    app = QtWidgets.QApplication(sys.argv)
    win = Dashboard()
    win.resize(1920, 1080)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
