"""Regression tests for the Windows window controls using Qt offscreen."""
import os
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QEvent, QPoint, QProcess, Qt
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
