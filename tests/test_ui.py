"""Regression tests for the Windows window controls using Qt offscreen."""
import os
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import av
from PySide6.QtCore import QEvent, QPoint, QPointF, QProcess, QRectF, Qt
from PySide6.QtGui import QContextMenuEvent, QImage, QKeyEvent
from PySide6.QtWidgets import QApplication, QDialog, QPlainTextEdit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from icast import dialogs, ui  # noqa: E402
from icast.airplay import Connection, Receiver, Request, Response  # noqa: E402
from icast.config import Settings  # noqa: E402


class WindowControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.settings = Settings(Path(folder.name) / "settings.json")
        with mock.patch.object(ui.PhoneWindow, "_setup_tray"):
            self.window = ui.PhoneWindow(self.settings, None)
        self.addCleanup(self.window.close)

    def choose_menu_option(self, title, label):
        class TestMenu(ui.QMenu):
            def exec(self, _):
                submenu, = (action.menu() for action in self.actions() if action.text() == title)
                option, = (action for action in submenu.actions() if action.text() == label)
                option.trigger()

        point = QPoint(20, 20)
        event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, point)
        with mock.patch.object(ui, "QMenu", TestMenu):
            self.window.contextMenuEvent(event)

    def test_resolution_menu_preserves_custom_frame_rate(self):
        self.settings["fps"] = 45
        self.choose_menu_option("Resolution", "1440p")
        saved = Settings(self.settings.path)
        self.assertEqual((saved["width"], saved["height"], saved["fps"]), (2560, 1440, 45))

    def test_experimental_frame_rate_is_advertised_without_changing_resolution(self):
        self.settings["width"], self.settings["height"] = 1280, 720
        self.choose_menu_option("Frame Rate", "120 FPS (Experimental)")
        saved = Settings(self.settings.path)
        self.assertEqual((saved["width"], saved["height"], saved["fps"]), (1280, 720, 120))

        receiver = Receiver(saved, mock.Mock(), mock.Mock())
        writer = mock.Mock()
        writer.get_extra_info.return_value = ("127.0.0.1", 0)
        response = Response()
        Connection(receiver, None, writer).on_info(Request("GET", "/info", "RTSP/1.0", {"cseq": "1"}, b""), response)
        display = plistlib.loads(response.body)["displays"][0]
        self.assertEqual((display["width"], display["height"], display["maxFPS"]), (1280, 720, 120))
        self.assertAlmostEqual(display["refreshRate"], 1 / 120)

    def test_stream_info_distinguishes_received_video_and_display(self):
        self.assertIn("Waiting for a frame", self.window._stream_info())
        self.window.image = QImage()
        self.window.src_size = (590, 1280)
        self.window.state = "mirroring"
        self.window.fps = 57.9
        with mock.patch.object(self.window, "_display_size", return_value=(460, 1000)):
            info = self.window._stream_info()
        self.assertIn("Received video: 590 × 1280 px", info)
        self.assertIn("Display area: 460 × 1000 px", info)
        self.assertIn("Displayed frame rate: 58 fps", info)

    def test_fullscreen_fits_video_and_escape_restores_window(self):
        self.window.move(45, 66)
        self.window.show()
        self.app.processEvents()
        normal = self.window.geometry()
        portrait = self.window.aspect
        self.window.rx = mock.Mock()

        self.window.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_F11, Qt.NoModifier))
        self.app.processEvents()
        self.assertTrue(self.window.isFullScreen())
        full = self.window.geometry()
        for aspect in (portrait, 16 / 9):
            self.window.aspect = aspect
            self.window._relayout()
            video = self.window.phone_rect()
            bounds = QRectF(self.window.rect())
            self.assertEqual(self.window.geometry(), full)
            self.assertTrue(bounds.contains(video))
            self.assertEqual(video.center(), bounds.center())
            self.assertAlmostEqual(video.width() / video.height(), aspect, delta=0.005)
            self.assertEqual(self.window.rx.decoder.target, self.window._display_size())
            point = QPointF(video.x() + 0.25 * video.width(), video.y() + 0.75 * video.height())
            self.assertEqual(ui.to_fraction(video, point), (0.25, 0.75))
        self.window._save_pos()
        self.assertEqual((self.settings["x"], self.settings["y"]), (normal.x(), normal.y()))

        self.window.aspect = portrait
        self.window.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        self.app.processEvents()
        self.assertFalse(self.window.isFullScreen())
        self.assertEqual(self.window.geometry(), normal)

    def test_copy_screenshot_uses_full_resolution_frame(self):
        frame = av.VideoFrame(16, 24, "bgra")
        plane = frame.planes[0]
        plane.update(bytes((10, 20, 30, 255)) * (plane.buffer_size // 4))
        self.window.rx = SimpleNamespace(decoder=SimpleNamespace(last_frame=frame))
        self.window.image = QImage(8, 12, QImage.Format_RGB32)
        key = QKeyEvent(QEvent.KeyPress, Qt.Key_C, Qt.ControlModifier | Qt.ShiftModifier)

        with mock.patch.object(ui, "QApplication") as app:
            self.window.keyPressEvent(key)
            copy = app.clipboard.return_value.setImage
            copy.assert_called_once()
        image, = copy.call_args.args
        self.window.rx.decoder.last_frame = None
        del frame, plane
        self.assertEqual((image.width(), image.height()), (16, 24))
        self.assertEqual(image.pixelColor(0, 0).getRgb(), (30, 20, 10, 255))

    def test_actual_pixels_renders_original_pixels_at_current_dpi(self):
        width, height = 137, 241
        frame = av.VideoFrame(width, height, "bgra")
        plane = frame.planes[0]
        dark, light = bytes((30, 10, 10, 255)), bytes((10, 10, 240, 255))
        row = b"".join((dark, light)[x % 2] for x in range(width))
        plane.update((row + bytes(plane.line_size - len(row))) * height)
        self.window.rx = SimpleNamespace(decoder=SimpleNamespace(last_frame=frame))
        self.window.show()
        self.window.on_frame(frame.reformat(width=67, height=119, format="bgra"), width, height)
        self.window._zoom_acc = 120
        self.window._zoom_timer.start()
        self.window.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_1, Qt.ControlModifier))
        self.app.processEvents()
        self.assertTrue(self.window.actual_pixels)
        self.assertFalse(self.window._zoom_timer.isActive())
        self.assertIn("View: Actual Pixels (1:1)", self.window._stream_info())

        for fullscreen in (False, True):
            with self.subTest(fullscreen=fullscreen):
                if fullscreen:
                    self.window._toggle_fullscreen()
                    self.app.processEvents()
                self.assertEqual(self.window.rx.decoder.target, (width, height))
                rect = self.window.phone_rect()
                dpr = self.window.devicePixelRatioF()
                self.assertAlmostEqual(rect.width() * dpr, width)
                self.assertAlmostEqual(rect.height() * dpr, height)
                self.assertAlmostEqual(rect.x() * dpr, round(rect.x() * dpr))
                self.assertAlmostEqual(rect.y() * dpr, round(rect.y() * dpr))
                rendered = self.window.grab().toImage()
                x0, y0 = round(rect.x() * dpr), round(rect.y() * dpr)
                self.assertEqual(rendered.pixelColor(x0 + 60, y0 + 100).getRgb(), (10, 10, 30, 255))
                self.assertEqual(rendered.pixelColor(x0 + 61, y0 + 100).getRgb(), (240, 10, 10, 255))

    def test_actual_pixels_keeps_fit_view_for_oversized_source(self):
        self.window.src_size = (4000, 8000)
        self.window.rx = SimpleNamespace(decoder=SimpleNamespace(last_frame=None))
        original = self.window.size()
        self.window._toggle_actual_pixels()
        self.assertEqual(self.window.size(), original)
        self.assertEqual(self.window.rx.decoder.target, self.window._display_size())
        self.assertIn("View: Fit (source is too large for 1:1)", self.window._stream_info())

    def test_windows_pin_does_not_hide_or_reopen_window(self):
        with mock.patch.object(ui, "QApplication") as app, \
                mock.patch.object(self.window, "_set_windows_topmost", return_value=True) as set_topmost, \
                mock.patch.object(self.window, "_apply_flags") as apply_flags, \
                mock.patch.object(self.window, "show") as show:
            app.platformName.return_value = "windows"
            self.window._button("pin")
            self.window._button("pin")
        self.assertEqual(set_topmost.call_args_list, [mock.call(False), mock.call(True)])
        apply_flags.assert_not_called()
        show.assert_not_called()

    def test_pin_and_quit_shortcuts_trigger_once_per_press(self):
        with mock.patch.object(self.window, "_button") as button:
            for key in (Qt.Key_T, Qt.Key_Q):
                self.window.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, Qt.ControlModifier))
                self.window.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, Qt.ControlModifier, "", True))
                self.window.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, Qt.ControlModifier | Qt.ShiftModifier))
        self.assertEqual(button.call_args_list, [mock.call("pin"), mock.call("close")])

    def test_paste_reads_clipboard_once_and_only_while_control_is_ready(self):
        self.window.ctrl = mock.Mock()
        press = QKeyEvent(QEvent.KeyPress, Qt.Key_V, Qt.ControlModifier)
        repeat = QKeyEvent(QEvent.KeyPress, Qt.Key_V, Qt.ControlModifier, "", True)
        with mock.patch.object(ui, "QApplication") as app:
            app.clipboard.return_value.text.return_value = "Text to paste"
            self.window.keyPressEvent(press)
            app.clipboard.assert_not_called()
            self.window.control_state = ui.READY
            self.window.keyPressEvent(press)
            self.window.keyPressEvent(repeat)
            app.clipboard.assert_called_once_with()
        self.window._flush_type()
        self.window.ctrl.type_text.assert_called_once_with("Text to paste")

    def test_diagnostics_runs_doctor_and_displays_output(self):
        case = self
        payload = "USB check: thiết bị".encode("utf-8")
        split = payload.index(0xE1) + 1

        class TestProcess(QProcess):
            def start(self, program, arguments):
                case.assertEqual(arguments[-3:], ["-m", "icast.control", "doctor"])
                code = (f"import os, time; os.write(1, {payload[:split]!r}); "
                        f"time.sleep(0.05); os.write(1, {payload[split:]!r})")
                super().start(program, ["-E", "-c", code])

        class InspectDialog(QDialog):
            def exec(self):
                process = self.findChild(TestProcess)
                case.assertTrue(process.waitForFinished(5000))
                text = self.findChild(QPlainTextEdit).toPlainText()
                case.assertIn("AirPlay: Waiting", text)
                case.assertIn(payload.decode("utf-8"), text)
                return QDialog.Rejected

        self.window.state = "waiting"
        with mock.patch.object(dialogs, "QProcess", TestProcess), mock.patch.object(dialogs, "QDialog", InspectDialog):
            self.window._show_diagnostics()


if __name__ == "__main__":
    unittest.main()
