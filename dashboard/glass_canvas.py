import sys
import ctypes
from ctypes import wintypes
from dataclasses import dataclass

import keyboard
from PySide6.QtCore import Qt, QTimer, QPoint, Signal, QObject
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QGuiApplication, QCursor
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QInputDialog,
    QFileDialog,
    QMessageBox,
)

# =========================
# Config
# =========================
TARGET_TITLES = {
    "Old School RuneScape",
    # "RuneLite",
}
POLL_MS = 30
PEN_WIDTH = 3
PEN_COLOR = QColor(255, 0, 0, 255)
TEXT_COLOR = QColor(0, 0, 0, 255)
TEXT_SIZE = 18

# =========================
# Win32 setup
# =========================
user32 = ctypes.windll.user32

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

SW_RESTORE = 9


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


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

user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL

user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL

user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long

user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowLongW.restype = ctypes.c_long


@dataclass
class TargetWindow:
    hwnd: int
    title: str
    left: int
    top: int
    width: int
    height: int


def get_window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value.strip()


def get_window_rect(hwnd: int):
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def enum_target_windows() -> list[TargetWindow]:
    found = []
    seen = set()

    @EnumWindowsProc
    def callback(hwnd, lparam):
        try:
            if hwnd in seen:
                return True
            if not user32.IsWindowVisible(hwnd):
                return True

            title = get_window_title(hwnd)
            if title not in TARGET_TITLES:
                return True

            r = get_window_rect(hwnd)
            if not r:
                return True

            left, top, right, bottom = r
            found.append(
                TargetWindow(
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

    user32.EnumWindows(callback, 0)
    return found


def set_click_through(hwnd: int, enabled: bool) -> None:
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    base = ex | WS_EX_LAYERED | WS_EX_TOOLWINDOW
    if enabled:
        ex = base | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
    else:
        ex = (base | WS_EX_NOACTIVATE) & ~WS_EX_TRANSPARENT
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)


class HotkeyBridge(QObject):
    toggle_edit = Signal()
    refresh_windows = Signal()
    clear_all = Signal()
    add_text = Signal()


class OverlayWindow(QWidget):
    def __init__(self, target_hwnd: int, title: str):
        super().__init__(None)
        self.target_hwnd = target_hwnd
        self.target_title = title
        self.edit_mode = False

        self.setWindowTitle(f"Overlay {target_hwnd}")
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMouseTracking(True)

        self.canvas = QPixmap(1, 1)
        self.canvas.fill(Qt.transparent)

        self.last_point = None
        self.last_mouse_pos = QPoint(20, 40)
        self._resize_canvas(300, 200)

        self.show()
        self._apply_clickthrough()

    def _native_hwnd(self) -> int:
        return int(self.winId())

    def _apply_clickthrough(self):
        set_click_through(self._native_hwnd(), not self.edit_mode)

    def set_edit_mode(self, enabled: bool):
        self.edit_mode = enabled
        self._apply_clickthrough()
        self.update()

    def _resize_canvas(self, w: int, h: int):
        w = max(1, w)
        h = max(1, h)
        if self.canvas.width() == w and self.canvas.height() == h:
            return

        new_canvas = QPixmap(w, h)
        new_canvas.fill(Qt.transparent)

        painter = QPainter(new_canvas)
        painter.drawPixmap(0, 0, self.canvas)
        painter.end()

        self.canvas = new_canvas
        self.update()

    def sync_to_target(self):
        r = get_window_rect(self.target_hwnd)
        if not r:
            self.hide()
            return

        if user32.IsIconic(self.target_hwnd):
            self.hide()
            return

        left, top, right, bottom = r
        w = max(1, right - left)
        h = max(1, bottom - top)

        self._resize_canvas(w, h)
        self.setGeometry(left, top, w, h)
        self.show()

    def clear_canvas(self):
        self.canvas.fill(Qt.transparent)
        self.update()

    def add_text_at(self, pos: QPoint):
        text, ok = QInputDialog.getText(self, "Add Text", "Text:")
        if not ok or not text:
            return

        painter = QPainter(self.canvas)
        pen = QPen(TEXT_COLOR)
        painter.setPen(pen)
        font = painter.font()
        font.setPointSize(TEXT_SIZE)
        painter.setFont(font)
        painter.drawText(pos, text)
        painter.end()
        self.update()

    def add_text_at_last_position(self):
        self.add_text_at(self.last_mouse_pos)

    def add_image_at(self, pos: QPoint):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose Image",
            "",
            "Images (*.png *.jpg *.jpeg *.bmp *.webp)"
        )
        if not path:
            return

        img = QPixmap(path)
        if img.isNull():
            QMessageBox.warning(self, "Image", "Could not load image.")
            return

        painter = QPainter(self.canvas)
        painter.drawPixmap(pos, img)
        painter.end()
        self.update()

    def paste_image_at(self, pos: QPoint):
        clipboard = QGuiApplication.clipboard()
        img = clipboard.pixmap()
        if img.isNull():
            return

        painter = QPainter(self.canvas)
        painter.drawPixmap(pos, img)
        painter.end()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self.canvas)

        if self.edit_mode:
            painter.setPen(QPen(QColor(0, 255, 255, 180), 2))
            painter.drawRect(self.rect().adjusted(1, 1, -2, -2))
            painter.setPen(QPen(QColor(0, 255, 255, 220), 1))
            painter.drawText(10, 22, "EDIT MODE  |  LMB draw  |  F12 text  |  MMB image  |  Ctrl+V paste")
        painter.end()

    def mousePressEvent(self, event):
        if not self.edit_mode:
            return

        self.last_mouse_pos = event.position().toPoint()

        if event.button() == Qt.LeftButton:
            self.last_point = event.position().toPoint()
        elif event.button() == Qt.MiddleButton:
            self.add_image_at(event.position().toPoint())

    def mouseMoveEvent(self, event):
        if not self.edit_mode:
            return

        self.last_mouse_pos = event.position().toPoint()

        if event.buttons() & Qt.LeftButton and self.last_point is not None:
            current = event.position().toPoint()
            painter = QPainter(self.canvas)
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setPen(QPen(PEN_COLOR, PEN_WIDTH, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.drawLine(self.last_point, current)
            painter.end()
            self.last_point = current
            self.update()

    def mouseReleaseEvent(self, event):
        if not self.edit_mode:
            return
        if event.button() == Qt.LeftButton:
            self.last_point = None

    def keyPressEvent(self, event):
        if not self.edit_mode:
            return

        if event.key() == Qt.Key_Delete:
            self.clear_canvas()
        elif event.modifiers() & Qt.ControlModifier and event.key() == Qt.Key_V:
            self.paste_image_at(self.last_mouse_pos)


class OverlayManager(QObject):
    def __init__(self):
        super().__init__()
        self.overlays: dict[int, OverlayWindow] = {}
        self.edit_mode = False

        self.timer = QTimer()
        self.timer.timeout.connect(self.sync)
        self.timer.start(POLL_MS)

    def refresh_targets(self):
        current = {t.hwnd: t for t in enum_target_windows()}

        for hwnd in list(self.overlays.keys()):
            if hwnd not in current:
                self.overlays[hwnd].close()
                del self.overlays[hwnd]

        for hwnd, target in current.items():
            if hwnd not in self.overlays:
                ov = OverlayWindow(target.hwnd, target.title)
                ov.set_edit_mode(self.edit_mode)
                self.overlays[hwnd] = ov

        self.sync()

    def sync(self):
        current_targets = enum_target_windows()
        current_hwnds = {t.hwnd for t in current_targets}

        for hwnd in list(self.overlays.keys()):
            if hwnd not in current_hwnds:
                self.overlays[hwnd].close()
                del self.overlays[hwnd]

        for target in current_targets:
            if target.hwnd not in self.overlays:
                ov = OverlayWindow(target.hwnd, target.title)
                ov.set_edit_mode(self.edit_mode)
                self.overlays[target.hwnd] = ov

        for ov in self.overlays.values():
            ov.sync_to_target()

    def toggle_edit(self):
        self.edit_mode = not self.edit_mode
        for ov in self.overlays.values():
            ov.set_edit_mode(self.edit_mode)
        print(f"Edit mode: {'ON' if self.edit_mode else 'OFF'}")

    def clear_all(self):
        for ov in self.overlays.values():
            ov.clear_canvas()
        print("Cleared all overlays.")

    def add_text_to_overlay_under_mouse(self):
        if not self.edit_mode:
            print("Turn on edit mode first.")
            return

        global_pos = QCursor.pos()

        for ov in self.overlays.values():
            if ov.isVisible() and ov.geometry().contains(global_pos):
                local_pos = ov.mapFromGlobal(global_pos)
                ov.last_mouse_pos = local_pos
                ov.add_text_at_last_position()
                return

        print("Move your mouse over an overlay first.")


def install_hotkeys(bridge: HotkeyBridge):
    keyboard.add_hotkey("F8", bridge.toggle_edit.emit)
    keyboard.add_hotkey("F9", bridge.refresh_windows.emit)
    keyboard.add_hotkey("F10", bridge.clear_all.emit)
    keyboard.add_hotkey("F12", bridge.add_text.emit)


def main():
    app = QApplication(sys.argv)

    bridge = HotkeyBridge()
    manager = OverlayManager()

    bridge.toggle_edit.connect(manager.toggle_edit)
    bridge.refresh_windows.connect(manager.refresh_targets)
    bridge.clear_all.connect(manager.clear_all)
    bridge.add_text.connect(manager.add_text_to_overlay_under_mouse)

    install_hotkeys(bridge)
    manager.refresh_targets()

    print("F8  = toggle edit mode")
    print("F9  = rescan OSRS windows")
    print("F10 = clear all overlays")
    print("F12 = add text under mouse")
    print("In edit mode:")
    print("  Left drag     = draw")
    print("  Middle click  = add image from file")
    print("  Ctrl+V        = paste clipboard image")
    print("  Delete        = clear that overlay")

    sys.exit(app.exec())


if __name__ == "__main__":
    main()