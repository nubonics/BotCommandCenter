import sys
from dataclasses import dataclass

import psutil
import win32con
import win32gui
import win32api
import win32process
import win32ui

from PySide6 import QtCore, QtGui, QtWidgets


MAX_WINDOWS = 40

EXE_OFFICIAL = "osclient.exe"
EXE_RUNELITE = "runelite.exe"

LAYOUT_PRESETS = {
    "Overview (40) 8×5": (8, 5),
    "Paged (15) 5×3": (5, 3),
    "Paged (12) 4×3": (4, 3),
    "Paged (9) 3×3": (3, 3),
}

CAPTURE_FPS = 2
CAPTURE_INTERVAL_MS = int(1000 / CAPTURE_FPS)


@dataclass
class TrackedWindow:
    hwnd: int
    title: str
    exe: str


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
        client_rank = 0 if w.exe == EXE_RUNELITE else 1
        return (client_rank, w.title.lower(), w.hwnd)

    out.sort(key=sort_key)
    return out[:MAX_WINDOWS]


def focus_window(hwnd: int):
    if not win32gui.IsWindow(hwnd):
        return

    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    except win32gui.error:
        pass

    try:
        # ALT nudge
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

        try:
            win32gui.SetForegroundWindow(hwnd)
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


def printwindow_capture(hwnd: int) -> QtGui.QImage | None:
    """Attempt to capture the window even if it's covered (stacked)."""
    if not win32gui.IsWindow(hwnd):
        return None

    try:
        left, top, right, bottom = win32gui.GetClientRect(hwnd)
        width = right - left
        height = bottom - top
        if width <= 0 or height <= 0:
            return None

        hwnd_dc = win32gui.GetWindowDC(hwnd)
        srcdc = win32ui.CreateDCFromHandle(hwnd_dc)
        memdc = srcdc.CreateCompatibleDC()

        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(srcdc, width, height)
        memdc.SelectObject(bmp)

        # PrintWindow into our memory DC
        # 0 = basic, 2 = PW_RENDERFULLCONTENT (not supported everywhere)
        ok = win32gui.PrintWindow(hwnd, memdc.GetSafeHdc(), 2)
        if ok != 1:
            # fallback flag
            ok = win32gui.PrintWindow(hwnd, memdc.GetSafeHdc(), 0)
            if ok != 1:
                return None

        bmpinfo = bmp.GetInfo()
        bmpstr = bmp.GetBitmapBits(True)

        # BGRA -> QImage
        img = QtGui.QImage(bmpstr, bmpinfo['bmWidth'], bmpinfo['bmHeight'], QtGui.QImage.Format_ARGB32)
        return img.copy()  # detach from buffer
    except Exception:
        return None
    finally:
        try:
            win32gui.ReleaseDC(hwnd, hwnd_dc)
        except Exception:
            pass
        try:
            memdc.DeleteDC()
        except Exception:
            pass
        try:
            srcdc.DeleteDC()
        except Exception:
            pass
        try:
            win32gui.DeleteObject(bmp.GetHandle())
        except Exception:
            pass


class WindowTile(QtWidgets.QFrame):
    clicked = QtCore.Signal(int)

    def __init__(self, hwnd: int, title: str, subtitle: str):
        super().__init__()
        self.hwnd = hwnd

        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.setLineWidth(1)
        self.setCursor(QtCore.Qt.PointingHandCursor)

        self.image_label = QtWidgets.QLabel(alignment=QtCore.Qt.AlignCenter)
        self.image_label.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.image_label.setStyleSheet("background:#111;")

        self.title_label = QtWidgets.QLabel(title)
        self.title_label.setStyleSheet("font-size: 10px; padding: 2px;")
        self.title_label.setToolTip(title)
        self.title_label.setWordWrap(False)

        self.sub_label = QtWidgets.QLabel(subtitle)
        self.sub_label.setStyleSheet("font-size: 9px; color: #666; padding: 0 2px 2px 2px;")
        self.sub_label.setWordWrap(False)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)
        layout.addWidget(self.image_label, 1)
        layout.addWidget(self.title_label, 0)
        layout.addWidget(self.sub_label, 0)

    def mousePressEvent(self, e: QtGui.QMouseEvent):
        if e.button() == QtCore.Qt.LeftButton:
            self.clicked.emit(self.hwnd)
        super().mousePressEvent(e)

    def set_preview(self, img: QtGui.QImage | None):
        if not img or img.isNull():
            return
        pm = QtGui.QPixmap.fromImage(img)
        self.image_label.setPixmap(pm.scaled(self.image_label.size(), QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation))


class Dashboard(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("OSRS Dashboard (PrintWindow fallback) — click to focus")

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

        self.btn_refresh.clicked.connect(self.refresh_window_list)
        self.cb_runelite.stateChanged.connect(self.refresh_window_list)
        self.cb_official.stateChanged.connect(self.refresh_window_list)
        self.layout_box.currentTextChanged.connect(self.on_layout_changed)
        self.btn_prev.clicked.connect(lambda: self.change_page(-1))
        self.btn_next.clicked.connect(lambda: self.change_page(+1))

        self.refresh_timer = QtCore.QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_window_list)
        self.refresh_timer.start(2000)

        self.capture_timer = QtCore.QTimer(self)
        self.capture_timer.timeout.connect(self.capture_visible)
        self.capture_timer.start(CAPTURE_INTERVAL_MS)

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
        windows = enum_osrs_windows(self.cb_official.isChecked(), self.cb_runelite.isChecked())
        self._last_windows = windows

        alive_hwnds = {w.hwnd for w in windows}
        for hwnd in list(self.tiles_by_hwnd.keys()):
            if hwnd not in alive_hwnds or not win32gui.IsWindow(hwnd):
                tile = self.tiles_by_hwnd.pop(hwnd)
                tile.setParent(None)
                tile.deleteLater()

        for w in windows:
            if w.hwnd not in self.tiles_by_hwnd:
                tile = WindowTile(w.hwnd, w.title, self._tile_subtitle(w.exe))
                tile.clicked.connect(self.on_tile_clicked)
                self.tiles_by_hwnd[w.hwnd] = tile

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

        for i in reversed(range(self.grid.count())):
            w = self.grid.itemAt(i).widget()
            if w:
                w.setParent(None)

        for idx, w in enumerate(page_windows):
            r = idx // self.cols
            c = idx % self.cols
            self.grid.addWidget(self.tiles_by_hwnd[w.hwnd], r, c)

    def on_tile_clicked(self, hwnd: int):
        focus_window(hwnd)

    def capture_visible(self):
        start, end = self.visible_slice()
        targets = self._last_windows[start:end]
        for w in targets:
            tile = self.tiles_by_hwnd.get(w.hwnd)
            if not tile:
                continue
            img = printwindow_capture(w.hwnd)
            tile.set_preview(img)


def main():
    app = QtWidgets.QApplication(sys.argv)
    win = Dashboard()
    win.resize(1920, 1080)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
