"""iPhone-shaped floating window (PySide6).

Control off (default): the window is a plain mirror. Drag it anywhere to move it,
double-click to switch between big and normal size.

Control on (WebDriverAgent ready, see control/): mouse and keyboard input on the
phone area goes to the iPhone. Click = tap, press and hold = long press, drag =
swipe, double-click = double tap, wheel = scroll, typing = text. The window is
then moved by dragging the top bar or with Alt+drag anywhere.
"""
import logging
import math
import os
import threading
import time
from collections import deque
from pathlib import Path

from av.error import FFmpegError
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRectF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPixmap,
    QRadialGradient,
    QTransform,
)
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon, QToolTip, QWidget

from .config import APP_NAME, LOG_FILE
from .control import ERROR, MSG_SWITCHER_ASSISTIVE, OFF, READY, STARTING
from .dialogs import show_diagnostics

log = logging.getLogger(__name__)

SHADOW = 22  # px around the phone for the drop shadow
BAR = 40  # px above the phone for the hover bar
DEFAULT_ASPECT = 1179 / 2556  # iPhone portrait
WINDOW_SIZE_MAX = (1 << 24) - 1  # Qt's unconstrained QWidget size
DPI_CHANGE = getattr(QEvent.Type, "DevicePixelRatioChange", None)
RESOLUTIONS = (("720p", (1280, 720)), ("1080p", (1920, 1080)), ("1440p", (2560, 1440)))
FRAME_RATES = (("30 FPS", (30,)), ("60 FPS", (60,)), ("120 FPS (Experimental)", (120,)))

# input mapping when iPhone control is on
LONG_PRESS_S = 0.5  # hold at least this long without moving = long press
TAP_SLOP = 6.0  # px the mouse may move before a press becomes a swipe
SWIPE_MIN_S, SWIPE_MAX_S = 0.06, 1.5  # swipe duration = real drag time, clamped
DOUBLE_TAP_MS = 200  # a click is held back this long in case a second click makes it a double tap
WHEEL_BATCH_MS = 120  # wheel notches are summed for this long, then sent as one scroll
TYPE_BATCH_MS = 80  # typed characters are buffered for this long, then sent together
HINT_S = 2.6  # how long a status hint stays in the top bar

# Ctrl+wheel resizing: per 120 units of wheel delta (one notch). Touchpad pinches and smooth
# scrolling send many small deltas, which are summed and applied at most every ZOOM_MS.
ZOOM_PER_NOTCH = 1.06
ZOOM_MS = 30

# Windows virtual-key codes: shortcuts must also work with non-Latin keyboard layouts
VK_1, VK_C, VK_H, VK_J, VK_M, VK_U, VK_V = 0x31, 0x43, 0x48, 0x4A, 0x4D, 0x55, 0x56
VK_OEM_PLUS, VK_OEM_MINUS, VK_ADD, VK_SUBTRACT = 0xBB, 0xBD, 0x6B, 0x6D

GREEN, ORANGE, RED = QColor("#30d158"), QColor("#ff9f0a"), QColor("#ff453a")


class Bridge(QObject):
    """Carries callbacks from worker threads to the GUI thread (queued signals)."""

    frame = Signal(object, int, int)
    event = Signal(str, dict)
    control = Signal(str, str)  # Controller.on_state(state, message)
    _ready = Signal()

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._pending = deque()
        self._ready.connect(self._deliver, Qt.QueuedConnection)

    def post_frame(self, frame, width, height):
        self._post(self.frame, (frame, width, height))

    def post_event(self, event, info):
        self._post(self.event, (event, info))

    def _post(self, signal, args):
        with self._lock:
            wake = not self._pending
            # Only coalesce consecutive frames so stream events retain their order.
            if not wake and signal is self.frame and self._pending[-1][0] is signal:
                self._pending[-1] = signal, args
            else:
                self._pending.append((signal, args))
        if wake:
            self._ready.emit()

    @Slot()
    def _deliver(self):
        with self._lock:
            pending, self._pending = self._pending, deque()
        for signal, args in pending:
            signal.emit(*args)


# ---------------------------------------------------------------- input helpers
def clamp01(v: float) -> float:
    return 0.0 if v < 0 else 1.0 if v > 1 else float(v)


def to_fraction(rect: QRectF, pos) -> tuple:
    """Window position -> (x, y) as fractions 0..1 of the phone screen rect, clamped."""
    w, h = rect.width() or 1.0, rect.height() or 1.0
    return clamp01((pos.x() - rect.x()) / w), clamp01((pos.y() - rect.y()) / h)


def swipe_seconds(held: float) -> float:
    return max(SWIPE_MIN_S, min(SWIPE_MAX_S, held))


def key_text(event) -> str:
    """Text a key press types on the iPhone: printable characters, '\\b' or '\\n' ('' = none)."""
    mods = event.modifiers()
    ctrl, alt = bool(mods & Qt.ControlModifier), bool(mods & Qt.AltModifier)
    if ctrl != alt:  # Ctrl+key / Alt+key are shortcuts; Ctrl+Alt is AltGr on Windows (still typing)
        return ""
    k = event.key()
    if k == Qt.Key_Backspace:
        return "\b"
    if k in (Qt.Key_Return, Qt.Key_Enter):
        return "\n"
    t = event.text()
    return t if t and all(c.isprintable() for c in t) else ""


def is_key(event, keys, vks=()) -> bool:
    return event.key() in keys or (bool(vks) and event.nativeVirtualKey() in vks)


class PhoneWindow(QWidget):
    def __init__(self, settings, receiver_ref, controller=None):
        super().__init__()
        self.settings = settings
        self.rx = receiver_ref  # set later by main
        self.ctrl = controller  # control.Controller (optional)
        self.actual_pixels = False
        self.setWindowTitle(APP_NAME)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self._apply_flags()

        self.image = None  # QImage of the current frame (device pixels)
        self._frame_buffer = None  # keeps the FFmpeg plane behind self.image alive
        self._shadow = None
        self._shadow_key = None
        self.src_size = None
        self.aspect = DEFAULT_ASPECT
        self.state = "starting"
        self.info = {}
        self.client = None
        self._mirror_peer = None
        self._mirror_revoked = None
        self.auto_control = False
        self.hover = False
        self.drag_offset = None
        self._windowed_geometry = None
        self.fps = 0.0
        self._frames = 0
        self._fps_t = time.monotonic()
        self._hide_bar = QTimer(self, singleShot=True, interval=1200, timeout=self._bar_off)
        self._hover_btn = None
        self._hint_text = ""
        self._hint_until = 0.0
        self._zoom_acc = 0  # Ctrl+wheel delta not applied yet
        self._zoom_timer = QTimer(self, singleShot=True, interval=ZOOM_MS, timeout=self._apply_zoom)

        # iPhone control
        self.control_state = OFF  # controller state as of the last on_control (GUI thread)
        self.control_message = ""
        self._control_serial = 0  # Controller.snapshot() serial of the shown state
        self._error_serial = None  # serial of the last error shown in a message box
        self._error_box = None
        self._g = None  # gesture in progress: dict(pos0, pos, t0, moved, done, double)
        self._long_timer = QTimer(self, singleShot=True, interval=int(LONG_PRESS_S * 1000), timeout=self._on_long_press)
        self._pending_tap = None  # (x, y) fractions of a click that may still become a double tap
        self._tap_timer = QTimer(self, singleShot=True, interval=DOUBLE_TAP_MS, timeout=self._flush_tap)
        self._wheel_acc = 0.0
        self._wheel_pos = (0.5, 0.5)
        self._wheel_timer = QTimer(self, singleShot=True, interval=WHEEL_BATCH_MS, timeout=self._flush_wheel)
        self._type_buf = ""
        self._type_timer = QTimer(self, singleShot=True, interval=TYPE_BATCH_MS, timeout=self._flush_type)
        self._effects = []  # touch feedback: dict(kind, a, b, t, life), positions as fractions
        self._anim = QTimer(self, interval=16, timeout=self._tick)

        screen = QApplication.primaryScreen().availableGeometry()
        ph = int(settings.get("phone_height") or 0) or int(screen.height() * 0.72)
        self.phone_h = max(320, min(ph, screen.height() - BAR - 2 * SHADOW))
        self._relayout(keep_center=False)
        x, y = settings.get("x"), settings.get("y")
        if x is not None and y is not None:
            c = QPoint(int(x) + self.width() // 2, int(y) + self.height() // 2)
            if QApplication.screenAt(c) is None:  # saved position is off-screen now
                x = y = None
        if x is None or y is None:
            x = screen.right() - self.width() - 40
            y = screen.top() + (screen.height() - self.height()) // 2
        self.move(int(x), int(y))

        self._tray = None
        self._setup_tray()

    # ------------------------------------------------------------ geometry
    def _apply_flags(self):
        flags = Qt.FramelessWindowHint | Qt.Window
        if self.settings.get("always_on_top", True):
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)

    def _native_size(self):
        if not self.actual_pixels or self.src_size is None:
            return None
        dpr = self.devicePixelRatioF()
        w, h = (pixels / dpr for pixels in self.src_size)
        if self.isFullScreen():
            available_w, available_h = self.width(), self.height()
        else:
            area = (self.screen() or QApplication.primaryScreen()).availableGeometry()
            available_w, available_h = area.width() - 2 * SHADOW, area.height() - 2 * SHADOW - BAR
        if w <= available_w and h <= available_h:
            return w, h
        return None

    def phone_size(self):
        native = self._native_size()
        if native is not None:
            return native
        if self.isFullScreen():
            h = min(self.height(), self.width() / self.aspect)
            return max(1, round(h * self.aspect)), max(1, round(h))
        if self.aspect <= 1:  # portrait: height is the reference
            h = self.phone_h
            w = round(h * self.aspect)
        else:  # landscape: keep the same long side
            w = self.phone_h
            h = round(w / self.aspect)
        return w, h

    def phone_rect(self) -> QRectF:
        w, h = self.phone_size()
        if self.isFullScreen():
            x, y = (self.width() - w) / 2, (self.height() - h) / 2
        else:
            x, y = SHADOW, SHADOW + BAR
        if self.actual_pixels:
            # Keep the image origin on the physical pixel grid at fractional DPI.
            dpr = self.devicePixelRatioF()
            x, y = round(x * dpr) / dpr, round(y * dpr) / dpr
        return QRectF(x, y, w, h)

    def corner_radius(self) -> float:
        w, h = self.phone_size()
        return min(w, h) * 0.135

    def _relayout(self, keep_center=True):
        if not self.isFullScreen():
            w, h = self.phone_size()
            center = self.geometry().center()
            self.setFixedSize(math.ceil(w) + 2 * SHADOW, math.ceil(h) + 2 * SHADOW + BAR)
            if keep_center:
                g = self.geometry()
                g.moveCenter(center)
                if self._native_size() is not None:
                    area = (self.screen() or QApplication.primaryScreen()).availableGeometry()
                    g.moveLeft(max(area.left(), min(g.x(), area.right() + 1 - g.width())))
                    g.moveTop(max(area.top(), min(g.y(), area.bottom() + 1 - g.height())))
                self.move(g.topLeft())
        self._update_target()
        self.update()

    def _reset_view_input(self):
        self._drop_tap()
        self._cancel_gesture()
        self.drag_offset = None
        self._wheel_timer.stop()
        self._wheel_acc = 0.0
        self._zoom_timer.stop()
        self._zoom_acc = 0

    def _toggle_fullscreen(self):
        self._reset_view_input()
        self._hide_bar.stop()
        self._hover_btn = None
        if self.isFullScreen():
            geometry, self._windowed_geometry = self._windowed_geometry, None
            self.showNormal()
            if geometry is not None:
                self.setGeometry(geometry)
            self.hover = self.underMouse()
        else:
            self._windowed_geometry = self.geometry()
            self.setMinimumSize(0, 0)
            self.setMaximumSize(WINDOW_SIZE_MAX, WINDOW_SIZE_MAX)
            self.showFullScreen()
            self._flash_bar(1.2)
        self._relayout()

    def _toggle_actual_pixels(self):
        self._reset_view_input()
        self.actual_pixels = not self.actual_pixels
        self._relayout()
        if not self.actual_pixels:
            self._hint("Fit view")
        elif self.src_size is None:
            self._hint("Actual Pixels enabled. Waiting for video.")
        elif self._native_size() is None:
            self._hint("Source is too large for 1:1. Using fit view.")
        else:
            # Show the cached original immediately, even if no new frame arrives.
            frame = self.rx.decoder.last_frame if self.rx else None
            if not self._has_native_image() and frame is not None and self.src_size == (frame.width, frame.height):
                try:
                    self._set_video_image(frame.reformat(format="bgra"))
                except FFmpegError:
                    log.warning("could not show the cached frame at native size")
            self._hint("Actual Pixels (1:1)" if self._has_native_image() else "Waiting for a native-size frame")

    def _has_native_image(self):
        return self.image is not None and self.src_size == (self.image.width(), self.image.height())

    def _display_size(self):
        w, h = self.phone_size()
        dpr = self.devicePixelRatioF()
        return round(w * dpr), round(h * dpr)

    def _update_target(self):
        if self.rx is not None:
            self.rx.decoder.target = self._display_size()

    def set_phone_height(self, h):
        if self.isFullScreen():
            return
        self.actual_pixels = False
        screen = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        self.phone_h = int(max(320, min(h, screen.height() + 400)))
        self._relayout()
        self.settings["phone_height"] = self.phone_h

    def _toggle_size(self):
        screen = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        target = int((screen.height() - BAR - 2 * SHADOW) * (0.97 if self.aspect <= 1 else 0.55))
        if abs(self.phone_h - target) < 20:
            target = int(screen.height() * 0.6)
        self.set_phone_height(target)

    # ------------------------------------------------------------ data from receiver / controller
    def _set_video_image(self, frame):
        plane = frame.planes[0]
        buffer = memoryview(plane)
        self.image = QImage(buffer, frame.width, frame.height, plane.line_size, QImage.Format_RGB32)
        self._frame_buffer = buffer

    def on_frame(self, frame, src_w, src_h):
        self._set_video_image(frame)
        aspect = src_w / src_h
        if self.src_size != (src_w, src_h):
            self.src_size = (src_w, src_h)
            if self.ctrl is not None:
                self.ctrl.screen_aspect = aspect  # lets the controller notice a rotation at once
            aspect_changed = abs(aspect - self.aspect) > 0.01
            if aspect_changed:
                self.aspect = aspect
            if aspect_changed or self.actual_pixels:
                self._relayout()
        now = time.monotonic()
        if self.state != "mirroring":
            self.state = "mirroring"
            self.fps, self._frames, self._fps_t = 0.0, 0, now
        if self.auto_control and self._mirror_peer:
            self.auto_control = False
            self._toggle_control()
        self._frames += 1
        if now - self._fps_t >= 1:
            self.fps = self._frames / (now - self._fps_t)
            self._frames, self._fps_t = 0, now
        self.update()

    def on_event(self, event, info):
        log.debug("ui event %s %s", event, info)
        if event == "listening":
            self.info = info
            if self.state in ("starting", "error"):
                self.state = "waiting"
        elif event == "connecting":
            if self.ctrl is not None and self.ctrl.active:
                self.ctrl.stop()
            self.client = info.get("name")
            self.state = "connecting"
            self._mirror_peer = info.get("ip")
            self._mirror_revoked = info.get("revoked")
            self.image = self._frame_buffer = None
            if self.actual_pixels:
                self.src_size = None
                self._relayout()
        elif event == "format":
            if self.state != "mirroring":
                self.state = "connecting"
        elif event == "paused":
            self.state = "paused"
        elif event == "mirror_stopped":
            self.state = "waiting"
            self._mirror_peer = None
            self._mirror_revoked = None
            self.image = None
            self._frame_buffer = None
            self.src_size = None
            if self.ctrl is not None:
                self.ctrl.screen_aspect = None
                if self.ctrl.active:
                    self.ctrl.stop()
            self._drop_tap()  # the screen it was aimed at is gone
            self._cancel_gesture()
            if self.actual_pixels or abs(self.aspect - DEFAULT_ASPECT) > 0.01:
                self.aspect = DEFAULT_ASPECT
                self._relayout()
        elif event == "decrypt_error":
            self.state = "decrypt_error"
        elif event == "error":
            self.state = "error"
            self.info = dict(self.info, message=info.get("message", ""))
        self.update()

    def on_control(self, state, message):
        """Controller state change; connected through Bridge.control, so this runs on the GUI thread.

        Notifications from the control thread are queued, while stop() on the GUI thread
        notifies directly, so they can arrive out of order. The arguments are therefore only
        a wake-up call: the controller's current state is what gets shown."""
        if self.ctrl is not None:
            state, message, serial = self.ctrl.snapshot()
        else:
            serial = self._control_serial + 1
        message = message or ""
        changed = (state, message) != (self.control_state, self.control_message)
        new_error = state == ERROR and serial != self._error_serial  # each failure once, also a repeated one
        self.control_state, self.control_message = state, message
        self._control_serial = serial
        if state != READY:
            self._drop_tap()
            self._cancel_gesture()
            self._type_buf = ""
            self._wheel_acc = 0.0
        if new_error:
            self._error_serial = serial
            self._show_control_error(self.control_message or "iPhone control failed.")
        if state == READY and message == MSG_SWITCHER_ASSISTIVE:
            QToolTip.showText(self.mapToGlobal(self.phone_rect().topLeft().toPoint()), message, self)
        elif changed or new_error:
            self._hint_text = ""  # the new control status replaces any older hint
            self._flash_bar()
        self._update_anim()
        self.update()

    def _show_control_error(self, message):
        if self._error_box is not None:
            try:
                self._error_box.close()
            except RuntimeError:  # already deleted
                pass
        box = QMessageBox(QMessageBox.Warning, "iPhone control", message, QMessageBox.Ok, self)
        box.setWindowModality(Qt.NonModal)
        box.setAttribute(Qt.WA_DeleteOnClose)
        box.setTextInteractionFlags(Qt.TextSelectableByMouse)
        box.show()
        self._error_box = box

    # ------------------------------------------------------------ painting
    def _shadow_pixmap(self, rect, radius):
        dpr = self.devicePixelRatioF()
        key = (self.width(), self.height(), dpr)
        if self._shadow_key != key:
            shadow = QPixmap(math.ceil(self.width() * dpr), math.ceil(self.height() * dpr))
            shadow.setDevicePixelRatio(dpr)
            shadow.fill(Qt.transparent)
            p = QPainter(shadow)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(Qt.NoPen)
            for i in range(SHADOW, 0, -2):
                alpha = int(20 * (1 - i / SHADOW) ** 2) + 1
                p.setBrush(QColor(0, 0, 0, alpha))
                p.drawRoundedRect(rect.adjusted(-i, -i + 6, i, i + 6), radius + i, radius + i)
            p.end()
            self._shadow, self._shadow_key = shadow, key
        return self._shadow

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        rect = self.phone_rect()
        fullscreen = self.isFullScreen()
        r = 0 if fullscreen else self.corner_radius()
        if fullscreen:
            p.fillRect(self.rect(), Qt.black)
        else:
            p.drawPixmap(0, 0, self._shadow_pixmap(rect, r))
        p.setPen(Qt.NoPen)

        if self.image is not None and self.state in ("mirroring", "paused", "connecting"):
            brush = QBrush(self.image)
            dpr = self.devicePixelRatioF()
            if self.actual_pixels and self._has_native_image() and self.src_size == (
                round(rect.width() * dpr), round(rect.height() * dpr)
            ):
                p.setRenderHint(QPainter.SmoothPixmapTransform, False)
            t = QTransform()
            t.translate(rect.x(), rect.y())
            t.scale(rect.width() / (self.image.width() / dpr) / dpr, rect.height() / (self.image.height() / dpr) / dpr)
            brush.setTransform(t)
            p.setBrush(brush)
            p.drawRoundedRect(rect, r, r)
            if self.state == "paused":
                p.setBrush(QColor(0, 0, 0, 150))
                p.drawRoundedRect(rect, r, r)
                self._center_text(p, rect, "iPhone is locked", "Unlock your iPhone to continue")
        else:
            self._paint_placeholder(p, rect, r)

        self._paint_touches(p, rect, r)

        if not fullscreen:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(255, 255, 255, 38), 1))
            p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), r - 0.5, r - 0.5)

        if self.hover:
            self._paint_bar(p)
        p.end()

    def _paint_placeholder(self, p, rect, r):
        g = QLinearGradient(rect.topLeft(), rect.bottomRight())
        g.setColorAt(0.0, QColor("#2b1d3f"))
        g.setColorAt(0.55, QColor("#16131f"))
        g.setColorAt(1.0, QColor("#0b0b10"))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawRoundedRect(rect, r, r)
        glow = QRadialGradient(rect.center() + QPointF(0, -rect.height() * 0.12), rect.width() * 0.6)
        glow.setColorAt(0, QColor(140, 110, 255, 70))
        glow.setColorAt(1, QColor(140, 110, 255, 0))
        p.setBrush(glow)
        p.drawRoundedRect(rect, r, r)

        # dynamic island
        iw, ih = rect.width() * 0.32, rect.width() * 0.09
        p.setBrush(QColor("#000000"))
        p.drawRoundedRect(QRectF(rect.center().x() - iw / 2, rect.y() + rect.width() * 0.035, iw, ih), ih / 2, ih / 2)

        self._paint_airplay_glyph(p, rect)
        name = self.info.get("name") or self.settings["name"]
        if self.state == "starting":
            title, sub = "Starting...", "Preparing the AirPlay receiver"
        elif self.state == "connecting":
            title, sub = "Connecting...", f"{self.client or 'iPhone'} is starting Screen Mirroring"
        elif self.state == "decrypt_error":
            title = "Could not decrypt the video"
            sub = (
                "Stop Screen Mirroring and connect again. If it keeps happening, "
                "send the log (right-click > Open Log)."
            )
        elif self.state == "error":
            title, sub = "Error", self.info.get("message", "")
        else:
            title = "Waiting for iPhone"
            sub = f'On your iPhone open Control Center > Screen Mirroring and choose "{name}"'
        self._center_text(p, rect, title, sub)

        addrs = self.info.get("addresses") or []
        if addrs:
            f = QFont("Segoe UI", 8)
            p.setFont(f)
            p.setPen(QColor(255, 255, 255, 90))
            p.drawText(
                QRectF(rect.x(), rect.bottom() - rect.width() * 0.16, rect.width(), 20),
                Qt.AlignHCenter,
                "PC: " + ", ".join(addrs) + "  -  iPhone and PC must be on the same Wi-Fi",
            )

    def _paint_airplay_glyph(self, p, rect):
        s = rect.width() * 0.2
        cx, cy = rect.center().x(), rect.y() + rect.height() * 0.36
        screen = QPainterPath()
        screen.addRoundedRect(QRectF(cx - s * 0.62, cy - s * 0.5, s * 1.24, s * 0.75), s * 0.08, s * 0.08)
        stroke = QPainterPathStroker()
        stroke.setWidth(max(2.0, s * 0.07))

        tri = QPainterPath()
        tri.moveTo(cx, cy + s * 0.05)
        tri.lineTo(cx + s * 0.3, cy + s * 0.45)
        tri.lineTo(cx - s * 0.3, cy + s * 0.45)
        tri.closeSubpath()
        # Fill the joined shapes once so the bottom edge meets the triangle without a seam.
        p.fillPath(stroke.createStroke(screen).united(tri), QColor(255, 255, 255, 230))

    def _center_text(self, p, rect, title, sub):
        w = rect.width()
        tf = QFont("Segoe UI Semibold", max(11, int(w * 0.055)))
        p.setFont(tf)
        p.setPen(QColor(255, 255, 255, 240))
        y = rect.y() + rect.height() * 0.5
        p.drawText(QRectF(rect.x() + w * 0.08, y, w * 0.84, w * 0.12), Qt.AlignHCenter | Qt.AlignTop, title)
        sf = QFont("Segoe UI", max(9, int(w * 0.034)))
        p.setFont(sf)
        p.setPen(QColor(255, 255, 255, 170))
        p.drawText(
            QRectF(rect.x() + w * 0.1, y + w * 0.12, w * 0.8, rect.height() * 0.3),
            Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap,
            sub,
        )

    def _paint_touches(self, p, rect, r):
        """Touch feedback: ripples for taps / long presses, fading lines for swipes."""
        if not self._effects and self._g is None:
            return
        now = time.monotonic()

        def pt(f):
            return QPointF(rect.x() + f[0] * rect.width(), rect.y() + f[1] * rect.height())

        p.save()
        clip = QPainterPath()
        clip.addRoundedRect(rect, r, r)
        p.setClipPath(clip)
        for fx in self._effects:
            k = (now - fx["t"]) / fx["life"]
            if not 0 <= k < 1:
                continue
            fade = 1 - k
            if fx["kind"] == "swipe":
                a, b = pt(fx["a"]), pt(fx["b"])
                p.setPen(QPen(QColor(255, 255, 255, int(170 * fade)), 3 + 4 * fade, Qt.SolidLine, Qt.RoundCap))
                p.drawLine(a, b)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 255, 255, int(210 * fade)))
                p.drawEllipse(b, 3 + 4 * fade, 3 + 4 * fade)
            else:
                big = fx["kind"] == "long"
                radius = 10 + (34 if big else 20) * k
                p.setPen(QPen(QColor(255, 255, 255, int(220 * fade)), 2.5 if big else 2))
                p.setBrush(QColor(255, 255, 255, int((80 if big else 60) * fade)))
                p.drawEllipse(pt(fx["a"]), radius, radius)
        g = self._g
        if g is not None:  # finger down
            if g["moved"] and not g["done"]:
                p.setPen(QPen(QColor(255, 255, 255, 110), 3, Qt.SolidLine, Qt.RoundCap))
                p.drawLine(g["pos0"], g["pos"])
            p.setPen(QPen(QColor(255, 255, 255, 150), 1.5))
            p.setBrush(QColor(255, 255, 255, 60))
            p.drawEllipse(g["pos"] if g["moved"] else g["pos0"], 11, 11)
        p.restore()

    # hover bar: [label .......... control  home  pin  -  x]
    def _bar_rect(self) -> QRectF:
        rect = self.phone_rect()
        y = rect.top() + 6 if self.isFullScreen() else SHADOW + 2
        return QRectF(rect.x() + 6, y, rect.width() - 12, BAR - 10)

    def _buttons(self):
        b = self._bar_rect()
        names = ("control", "home", "pin", "min", "close")
        s = min(b.height(), max(18.0, (b.width() - 40) / len(names)))
        y = b.center().y() - s / 2
        right = b.right() - max(0.0, (b.height() - s) / 2)
        return {n: QRectF(right - (len(names) - i) * s, y, s, s) for i, n in enumerate(names)}

    def _button_at(self, pos):
        for name, r in self._buttons().items():
            if r.contains(pos):
                return name
        return None

    def _control_status(self) -> str:
        st = self.control_state
        if self.ctrl is None or st == OFF:
            return ""
        if st == STARTING:
            return self.control_message.splitlines()[0] if self.control_message else "Starting control..."
        if st == READY:
            return "Control on"
        first = self.control_message.splitlines()[0] if self.control_message else ""
        return f"Control error: {first}" if first else "Control error"

    def _bar_labels(self) -> list:
        """Top bar label, most detailed first; the painter uses the first one that fits."""
        if self._hint_text and time.monotonic() < self._hint_until:
            return [self._hint_text]
        name = self.client or "iPhone"
        fps = f"{self.fps:.0f} fps" if self.state == "mirroring" and self.fps else ""
        status = self._control_status()
        sep = "  ·  "
        labels = [sep.join(x for x in (name, fps, status) if x)]
        if fps:
            labels.append(sep.join(x for x in (name, status) if x))
        if status:
            labels.append(status)
        return labels

    def _control_color(self) -> QColor:
        st = self.control_state
        if st == READY:
            return QColor(GREEN)
        if st == STARTING:
            c = QColor(ORANGE)
            c.setAlphaF(0.35 + 0.65 * (0.5 + 0.5 * math.sin(time.monotonic() * 2 * math.pi / 1.1)))
            return c
        if st == ERROR:
            return QColor(RED)
        return QColor(255, 255, 255, 220)

    def _paint_bar(self, p):
        b = self._bar_rect()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(30, 30, 34, 225))
        p.drawRoundedRect(b, b.height() / 2, b.height() / 2)
        btn = self._buttons()

        font = QFont("Segoe UI Semibold", 9)
        p.setFont(font)
        text_rect = QRectF(b.x() + 14, b.y(), max(0.0, btn["control"].left() - b.x() - 18), b.height())
        fm = QFontMetrics(font)
        labels = self._bar_labels()
        label = next((t for t in labels if fm.horizontalAdvance(t) <= text_rect.width()), None)
        if label is None:
            label = fm.elidedText(labels[-1], Qt.ElideRight, int(text_rect.width()))
        p.setPen(QColor(255, 255, 255, 220))
        p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, label)

        s = btn["close"].width()
        k = s / 30  # glyph scale
        if self._hover_btn in btn:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 34))
            p.drawEllipse(btn[self._hover_btn].center(), s * 0.42, s * 0.42)

        self._paint_hand(p, btn["control"].center() + QPointF(0.8 * k, 0.4 * k), k * 0.95, self._control_color())

        white = QColor(255, 255, 255, 220)
        d = 4.5 * k
        c = btn["home"].center()
        home_on = self.control_state == READY
        p.setPen(QPen(white if home_on else QColor(255, 255, 255, 110), 1.6))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(c, 5.6 * k, 5.6 * k)

        pen = QPen(white, 1.6, Qt.SolidLine, Qt.RoundCap)
        p.setPen(pen)
        c = btn["close"].center()
        p.drawLine(QPointF(c.x() - d, c.y() - d), QPointF(c.x() + d, c.y() + d))
        p.drawLine(QPointF(c.x() - d, c.y() + d), QPointF(c.x() + d, c.y() - d))
        c = btn["min"].center()
        p.drawLine(QPointF(c.x() - d, c.y()), QPointF(c.x() + d, c.y()))
        c = btn["pin"].center()
        on = self.settings.get("always_on_top", True)
        p.setBrush(white if on else Qt.NoBrush)
        p.drawEllipse(QPointF(c.x(), c.y() - 1.6 * k), 3.6 * k, 3.6 * k)
        p.drawLine(QPointF(c.x(), c.y() + 2.0 * k), QPointF(c.x(), c.y() + 6.4 * k))

    @staticmethod
    def _paint_hand(p, c, k, color):
        """Hand with a raised index finger (the 'control the iPhone' glyph)."""

        def rr(x, y, w, h, r):
            q = QPainterPath()
            q.addRoundedRect(QRectF(c.x() + x * k, c.y() + y * k, w * k, h * k), r * k, r * k)
            return q

        path = rr(-2.0, -9.0, 3.6, 10.0, 1.8)  # index finger
        path = path.united(rr(-4.4, -2.6, 10.4, 10.6, 3.2))  # palm with folded fingers
        path = path.united(rr(-7.6, 0.4, 5.0, 3.6, 1.8))  # thumb
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawPath(path)
        p.setPen(QPen(QColor(30, 30, 34, 200), 1.0 * max(1.0, k)))
        for x in (1.8, 4.0):  # gaps between the folded fingers
            p.drawLine(QPointF(c.x() + x * k, c.y() - 2.2 * k), QPointF(c.x() + x * k, c.y() + 1.4 * k))

    def _bar_off(self):
        self.hover = False
        self._hover_btn = None
        self._update_anim()
        self.update()

    def _flash_bar(self, seconds=HINT_S):
        """Show the top bar for a moment (status changes, hints) even if the mouse is elsewhere."""
        self.hover = True
        if self.isFullScreen() or not self.underMouse():
            self._hide_bar.start(int(seconds * 1000))
        self._update_anim()
        self.update()

    def _hint(self, text):
        self._hint_text = text
        self._hint_until = time.monotonic() + HINT_S
        self._flash_bar()
        QTimer.singleShot(int(HINT_S * 1000) + 50, self.update)

    # ------------------------------------------------------------ animation
    def _needs_anim(self):
        return bool(self._effects) or (self.hover and self.control_state == STARTING)

    def _update_anim(self):
        if self._needs_anim() and not self._anim.isActive():
            self._anim.start()

    def _tick(self):
        now = time.monotonic()
        self._effects = [fx for fx in self._effects if now - fx["t"] < fx["life"]]
        if not self._needs_anim():
            self._anim.stop()
        self.update()

    def _add_effect(self, kind, a, b=None):
        life = {"tap": 0.45, "long": 0.7, "swipe": 0.5}[kind]
        self._effects.append({"kind": kind, "a": a, "b": b, "t": time.monotonic(), "life": life})
        self._update_anim()
        self.update()

    # ------------------------------------------------------------ iPhone control
    def _control_ready(self):
        return self.ctrl is not None and self.control_state == READY

    def _gestures_on(self):
        """Mouse input goes to the iPhone only while control is ready and its screen is shown."""
        return self._control_ready() and self.image is not None

    def _toggle_control(self):
        if self.ctrl is None:
            self._hint("iPhone control is not available")
        elif self.ctrl.active:  # the controller's own state: the shown one may lag behind
            self.ctrl.stop()
        elif (self.image is None or not self._mirror_peer or self._mirror_revoked is None
              or self._mirror_revoked.is_set()):
            self._hint("Start mirroring the iPhone first")
        else:
            self.ctrl.start(expected_peer=self._mirror_peer, revoked=self._mirror_revoked)

    def _phone(self, method, *args):
        """Send a command (home, app_switcher, button) to the iPhone, or explain why not."""
        if not self._control_ready():
            if self.ctrl is None:
                self._hint("iPhone control is not available")
            elif self.control_state == STARTING:
                self._hint("iPhone control is still starting...")
            else:
                self._hint("Turn on iPhone control first (Ctrl+U)")
            return
        self._flush_tap()
        getattr(self.ctrl, method)(*args)

    def _gesture_begin(self, pos, double=False):
        self._g = {
            "pos0": QPointF(pos),
            "pos": QPointF(pos),
            "t0": time.monotonic(),
            "moved": False,
            "done": False,
            "double": double,  # second press of a double-click whose first click is still held back
        }
        self._long_timer.start()
        self.update()

    def _cancel_gesture(self):
        self._g = None
        self._long_timer.stop()
        if self._pending_tap is not None and not self._tap_timer.isActive():
            self._flush_tap()  # its double-click will not complete now

    def _flush_tap(self):
        """Send the held-back click as a single tap (no second click came in time)."""
        self._tap_timer.stop()
        a, self._pending_tap = self._pending_tap, None
        if a is not None and self._control_ready():
            self.ctrl.tap(*a)

    def _drop_tap(self):
        self._tap_timer.stop()
        self._pending_tap = None

    def _on_long_press(self):
        g = self._g
        if g is None or g["moved"] or g["done"] or not self._control_ready():
            return
        g["done"] = True
        self._flush_tap()  # a click before this press stays a tap of its own
        a = to_fraction(self.phone_rect(), g["pos0"])
        self.ctrl.long_press(*a)
        self._add_effect("long", a)

    def _gesture_end(self, pos):
        g, self._g = self._g, None
        self._long_timer.stop()
        if not self._control_ready():
            self._drop_tap()
            self.update()
            return
        if g["done"]:
            self.update()
            return
        rect = self.phone_rect()
        a = to_fraction(rect, g["pos0"])
        held = time.monotonic() - g["t0"]
        if g["moved"]:
            self._flush_tap()
            b = to_fraction(rect, pos)
            self.ctrl.swipe(a[0], a[1], b[0], b[1], swipe_seconds(held))
            self._add_effect("swipe", a, b)
        elif held >= LONG_PRESS_S:  # the timer did not get to run (busy event loop)
            self._flush_tap()
            self.ctrl.long_press(*a)
            self._add_effect("long", a)
        elif g["double"] and self._pending_tap is not None:
            first = self._pending_tap
            self._drop_tap()
            self.ctrl.double_tap(*first)  # both taps in one go, at the first click's position
            self._add_effect("tap", first)
        else:
            # hold the tap back briefly: a second click turns it into a double tap
            self._flush_tap()
            self._pending_tap = a
            self._tap_timer.start()
            self._add_effect("tap", a)

    def _flush_wheel(self):
        n, self._wheel_acc = self._wheel_acc, 0.0
        if abs(n) >= 0.05 and self._control_ready():
            self._flush_tap()  # keep the order: a click made before the scroll goes first
            self.ctrl.scroll(self._wheel_pos[0], self._wheel_pos[1], n)

    def _type(self, text):
        self._type_buf += text
        if not self._type_timer.isActive():
            self._type_timer.start()

    def _flush_type(self):
        text, self._type_buf = self._type_buf, ""
        if text and self._control_ready():
            self._flush_tap()  # the click that focused a text field must arrive before the text
            self.ctrl.type_text(text)

    def _update_cursor(self, pos):
        touch = (
            self._gestures_on()
            and self.phone_rect().contains(pos)
            and not (QApplication.keyboardModifiers() & Qt.AltModifier)
        )
        want = Qt.PointingHandCursor if touch else Qt.ArrowCursor
        if self.cursor().shape() != want:
            self.setCursor(want)

    # ------------------------------------------------------------ input
    def enterEvent(self, e):
        if self.isFullScreen():
            return
        self._hide_bar.stop()
        self.hover = True
        self._update_anim()
        self.update()

    def leaveEvent(self, e):
        self._hide_bar.start(1200)
        if self._hover_btn:
            self._hover_btn = None
            self.update()

    def event(self, e):
        if e.type() == DPI_CHANGE and self.actual_pixels:
            QTimer.singleShot(0, self._relayout)
        if e.type() == QEvent.ToolTip:
            name = self._button_at(QPointF(e.pos())) if self.hover else None
            if name:
                QToolTip.showText(e.globalPos(), self._button_tip(name), self)
            else:
                QToolTip.hideText()
                e.ignore()
            return True
        return super().event(e)

    def _button_tip(self, name):
        if name == "control":
            return {
                READY: "iPhone control is on - click to turn it off (Ctrl+U)",
                STARTING: "iPhone control is starting - click to cancel (Ctrl+U)",
                ERROR: "iPhone control failed - click to try again (Ctrl+U)",
            }.get(self.control_state, "Control iPhone over USB (Ctrl+U)")
        return {"home": "Home (Ctrl+H)", "pin": "Always on top", "min": "Minimize", "close": "Close"}[name]

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        pos = e.position()
        if self.hover:
            name = self._button_at(pos)
            if name:
                self._button(name)
                return
        alt = bool(e.modifiers() & Qt.AltModifier)
        if self._gestures_on() and not alt and self.phone_rect().contains(pos):
            self._gesture_begin(pos)
            return
        if not self.isFullScreen():
            self.drag_offset = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        pos = e.position()
        g = self._g
        if g is not None:
            g["pos"] = QPointF(pos)
            if not g["moved"] and not g["done"]:
                d = pos - g["pos0"]
                if math.hypot(d.x(), d.y()) > TAP_SLOP:
                    g["moved"] = True
                    self._long_timer.stop()
            self.update()
            return
        if self.drag_offset is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self.drag_offset)
            return
        if self.isFullScreen():
            if self._bar_rect().contains(pos):
                self._hide_bar.stop()
                if not self.hover:
                    self.hover = True
                    self._update_anim()
                    self.update()
            elif self.hover and not self._hide_bar.isActive():
                self._hide_bar.start()
        name = self._button_at(pos) if self.hover else None
        if name != self._hover_btn:
            self._hover_btn = name
            self.update()
        self._update_cursor(pos)

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        if self._g is not None:
            self._gesture_end(e.position())
            return
        if self.drag_offset is not None:
            self.drag_offset = None
            self._save_pos()

    def mouseDoubleClickEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        pos = e.position()
        if self.hover and self._button_at(pos):
            return  # the first click already pressed the button
        alt = bool(e.modifiers() & Qt.AltModifier)
        if self._gestures_on() and not alt and self.phone_rect().contains(pos):
            # second press of a double-click. If the first click is still held back, both are
            # sent together as a double tap on release; if it already went out, this is a tap.
            double = self._tap_timer.isActive()
            self._tap_timer.stop()  # keep the first click until this press ends
            self._gesture_begin(pos, double=double)
            return
        self._toggle_size()

    def _apply_zoom(self):
        """Resize by the Ctrl+wheel delta collected so far, then wait ZOOM_MS before the next step."""
        dy, self._zoom_acc = self._zoom_acc, 0
        if dy:
            self.set_phone_height(round(self.phone_h * ZOOM_PER_NOTCH ** (dy / 120)))
            self._zoom_timer.start()

    def wheelEvent(self, e):
        dy = e.angleDelta().y()
        if e.modifiers() & Qt.ControlModifier:
            if dy and not self.isFullScreen():
                self._zoom_acc += dy  # proportional: a touchpad pinch sends many small deltas
                if not self._zoom_timer.isActive():
                    self._apply_zoom()
            return
        if dy and self._gestures_on() and self.phone_rect().contains(e.position()):
            if not self._wheel_timer.isActive():
                self._wheel_pos = to_fraction(self.phone_rect(), e.position())
                self._wheel_timer.start()
            self._wheel_acc += dy / 120  # > 0: wheel rotated away from the user

    def keyPressEvent(self, e):
        mods = e.modifiers()
        if mods == Qt.NoModifier and (e.key() == Qt.Key_F11 or
                                       (e.key() == Qt.Key_Escape and self.isFullScreen())):
            if not e.isAutoRepeat():
                self._toggle_fullscreen()
            return
        if (mods & Qt.ControlModifier) and not (mods & Qt.AltModifier):
            rep = e.isAutoRepeat()
            if is_key(e, (Qt.Key_Plus, Qt.Key_Equal), (VK_OEM_PLUS, VK_ADD)):
                self.set_phone_height(self.phone_h * 1.1)
            elif is_key(e, (Qt.Key_Minus,), (VK_OEM_MINUS, VK_SUBTRACT)):
                self.set_phone_height(self.phone_h / 1.1)
            elif is_key(e, (Qt.Key_Up,)):
                self._phone("button", "volumeUp")
            elif is_key(e, (Qt.Key_Down,)):
                self._phone("button", "volumeDown")
            elif rep:
                pass  # Home / App Switcher / toggles: once per key press
            elif is_key(e, (Qt.Key_H,), (VK_H,)):
                self._phone("home")
            elif is_key(e, (Qt.Key_J,), (VK_J,)):
                self._phone("app_switcher")
            elif is_key(e, (Qt.Key_M,), (VK_M,)):
                self._toggle_mute()
            elif is_key(e, (Qt.Key_U,), (VK_U,)):
                self._toggle_control()
            elif is_key(e, (Qt.Key_1,), (VK_1,)):
                self._toggle_actual_pixels()
            elif is_key(e, (Qt.Key_V,), (VK_V,)):
                if self._control_ready():
                    text = QApplication.clipboard().text()
                    if text:
                        self._type(text)
                else:
                    self._hint("Enable USB control to paste")
            elif mods & Qt.ShiftModifier and is_key(e, (Qt.Key_C,), (VK_C,)):
                self.copy_screenshot()
            else:
                super().keyPressEvent(e)
            return
        if self._control_ready():
            text = key_text(e)
            if text:
                self._type(text)
                return
        super().keyPressEvent(e)

    def contextMenuEvent(self, e):
        self._cancel_gesture()
        m = QMenu(self)
        ready = self._control_ready()
        ctl = QAction(
            "Control iPhone (USB)\tCtrl+U", m, checkable=True, checked=self.control_state in (STARTING, READY)
        )
        ctl.setEnabled(self.ctrl is not None)
        ctl.triggered.connect(self._toggle_control)
        m.addAction(ctl)
        for label, method, args in (
            ("Home\tCtrl+H", "home", ()),
            ("App Switcher\tCtrl+J", "app_switcher", ()),
            ("Volume Up\tCtrl+Up", "button", ("volumeUp",)),
            ("Volume Down\tCtrl+Down", "button", ("volumeDown",)),
        ):
            a = m.addAction(label, lambda method=method, args=args: self._phone(method, *args))
            a.setEnabled(ready)
        m.addSeparator()
        top = QAction("Always on Top", m, checkable=True, checked=self.settings.get("always_on_top", True))
        top.triggered.connect(lambda: self._button("pin"))
        m.addAction(top)
        snd = QAction("Sound\tCtrl+M", m, checkable=True, checked=not (self.rx and self.rx.muted))
        snd.triggered.connect(self._toggle_mute)
        m.addAction(snd)
        size = m.addMenu("Size")
        size.setEnabled(not self.isFullScreen())
        screen = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        for label, k in (("Small", 0.5), ("Medium", 0.72), ("Large", 0.92)):
            size.addAction(label, lambda k=k: self.set_phone_height(screen.height() * k))
        fullscreen = "Exit Full Screen" if self.isFullScreen() else "Full Screen"
        m.addAction(fullscreen + "\tF11", self._toggle_fullscreen)
        pixels = QAction("Actual Pixels (1:1)\tCtrl+1", m, checkable=True, checked=self.actual_pixels)
        pixels.triggered.connect(self._toggle_actual_pixels)
        m.addAction(pixels)
        self._add_video_menu(m, "Resolution", ("width", "height"), RESOLUTIONS)
        self._add_video_menu(m, "Frame Rate", ("fps",), FRAME_RATES)
        m.addSeparator()
        m.addAction("Save Screenshot", self.screenshot)
        m.addAction("Copy Screenshot\tCtrl+Shift+C", self.copy_screenshot)
        m.addAction("Stream Info...", self._show_stream_info)
        m.addAction("Rename PC...", self._rename)
        m.addAction("Open Settings Folder", self._open_settings_folder)
        m.addAction("Connection Diagnostics...", self._show_diagnostics)
        m.addAction("Open Log", self._open_log)
        m.addSeparator()
        m.addAction("Quit", QApplication.quit)
        m.exec(e.globalPos())

    def _add_video_menu(self, menu, title, keys, choices):
        submenu = menu.addMenu(title)
        group = QActionGroup(submenu)
        current = tuple(self.settings[key] for key in keys)
        for label, values in choices:
            action = QAction(label, submenu, checkable=True, checked=values == current)
            group.addAction(action)
            submenu.addAction(action)
            action.triggered.connect(lambda checked, values=values: self._save_video_choice(keys, values))
        if group.checkedAction() is None:
            value = " × ".join(str(value) for value in current)
            submenu.addAction(f"Custom ({value})").setEnabled(False)

    def _save_video_choice(self, keys, values):
        previous = tuple(self.settings[key] for key in keys)
        if values == previous:
            return
        for key, value in zip(keys, values):
            self.settings[key] = value
        if not self.settings.save():
            for key, value in zip(keys, previous):
                self.settings[key] = value
            self._hint("Could not save video settings. See Open Log.")
        elif self.image is not None:
            self._hint("Reconnect mirroring to request the new video settings.")
        else:
            self._hint("Video settings saved")

    def _button(self, name):
        if name == "close":
            QApplication.quit()
        elif name == "min":
            self.showMinimized()
        elif name == "control":
            self._toggle_control()
        elif name == "home":
            self._phone("home")
        elif name == "pin":
            on = not self.settings.get("always_on_top", True)
            self.settings["always_on_top"] = on
            self.settings.save()
            fullscreen = self.isFullScreen()
            pos = self.pos()
            self._apply_flags()
            if fullscreen:
                self.showFullScreen()
            else:
                self.move(pos)
                self.show()
        self.update()

    def _rename(self):
        from PySide6.QtWidgets import QInputDialog

        name, ok = QInputDialog.getText(
            self, "PC Name", "Name of this PC in Screen Mirroring on the iPhone:", text=self.settings["name"]
        )
        name = name.strip()
        if ok and name and self.rx:
            self.rx.rename(name)
            self.info = dict(self.info, name=name)
            self.update()

    def _stream_info(self):
        w, h = self._display_size()
        lines = [f"Display area: {w} × {h} px"]
        if self.image is None or self.src_size is None:
            lines.append("Received video: Waiting for a frame")
        else:
            src_w, src_h = self.src_size
            lines.append(f"Received video: {src_w} × {src_h} px")
            if self.state == "mirroring":
                lines.append(f"Displayed frame rate: {self.fps:.0f} fps" if self.fps else "Measuring frame rate...")
            elif self.state == "paused":
                lines.append("Video is paused.")
        if self.actual_pixels and self.src_size is not None:
            if self._native_size() is None:
                lines.append("View: Fit (source is too large for 1:1)")
            elif self._has_native_image():
                lines.append("View: Actual Pixels (1:1)")
            else:
                lines.append("View: Waiting for a native-size frame")
        lines.append(f"Requested on connect: {self.settings['width']} × {self.settings['height']} px"
                     f" / {self.settings['fps']} fps")
        return "\n".join(lines)

    def _show_stream_info(self):
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Stream Info")
        dialog.setTextFormat(Qt.PlainText)
        dialog.setStandardButtons(QMessageBox.Close)
        dialog.setText(self._stream_info())
        timer = QTimer(dialog, interval=1000, timeout=lambda: dialog.setText(self._stream_info()))
        timer.start()
        try:
            dialog.exec()
        finally:
            timer.stop()
            dialog.deleteLater()

    def _show_diagnostics(self):
        state = self.state.replace("_", " ").capitalize()
        summary = f"AirPlay: {state}\nReceiver name: {self.settings['name']}"
        if self.info.get("addresses"):
            summary += "\nPC addresses: " + ", ".join(self.info["addresses"])
        if self.state == "error" and self.info.get("message"):
            summary += "\n" + self.info["message"]
        show_diagnostics(self, summary)

    def _toggle_mute(self):
        if self.rx:
            self.rx.set_muted(not self.rx.muted)
            self._hint("Sound off" if self.rx.muted else "Sound on")

    def _open_log(self):
        if LOG_FILE.exists():
            os.startfile(str(LOG_FILE))

    def _open_settings_folder(self):
        try:
            os.startfile(str(self.settings.path.parent))
        except OSError:
            log.exception("could not open settings folder")
            self._hint("Could not open settings folder")

    def copy_screenshot(self):
        self.screenshot(copy=True)

    def screenshot(self, *, copy=False):
        frame = getattr(self.rx.decoder, "last_frame", None) if self.rx else None
        if frame is None:
            self._hint("Nothing to copy yet" if copy else "Nothing to save yet")
            return
        try:
            frame = frame.reformat(format="bgra")
            plane = frame.planes[0]
            img = QImage(memoryview(plane), frame.width, frame.height, plane.line_size, QImage.Format_RGB32)
            if copy:
                QApplication.clipboard().setImage(img.copy())
                self._hint("Screenshot copied")
                return
            folder = Path.home() / "Pictures" / APP_NAME
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / time.strftime("iPhone %Y-%m-%d %H-%M-%S.png")
            if not img.save(str(path)):
                raise OSError(f"could not write {path}")
            if self._tray:
                self._tray.showMessage("Screenshot saved", str(path), self.windowIcon(), 3000)
            else:
                self._hint("Screenshot saved")
        except Exception:
            log.exception("screenshot failed")
            if copy:
                self._hint("Could not copy screenshot")

    def _save_pos(self):
        pos = self._windowed_geometry.topLeft() if self._windowed_geometry is not None else self.pos()
        self.settings["x"], self.settings["y"] = pos.x(), pos.y()
        self.settings.save()

    def moveEvent(self, e):
        if self.actual_pixels and not self.isFullScreen():
            self._relayout(keep_center=False)
        else:
            self._update_target()

    def resizeEvent(self, e):
        if self.isFullScreen():
            self._update_target()

    def closeEvent(self, e):
        self._save_pos()
        super().closeEvent(e)

    # ------------------------------------------------------------ tray
    def _setup_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(self.windowIcon(), self)
        tray.setToolTip(APP_NAME)
        menu = QMenu()
        menu.addAction("Show Window", self._show_front)
        menu.addAction("Quit", QApplication.quit)
        tray.setContextMenu(menu)
        tray.activated.connect(lambda reason: self._show_front() if reason == QSystemTrayIcon.Trigger else None)
        tray.show()
        self._tray = tray
        self._tray_menu = menu  # the tray does not take ownership of the menu

    def _show_front(self):
        if self._windowed_geometry is not None:
            self.showFullScreen()
        else:
            self.showNormal()
        self.raise_()
        self.activateWindow()
