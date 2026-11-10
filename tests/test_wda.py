"""Tests for the WebDriverAgent client, the Controller and the USB helpers.

No iPhone needed: a mock WebDriverAgent server runs on 127.0.0.1 and pymobiledevice3
calls are replaced by fakes. Run:  python tests/test_wda.py
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import plistlib
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from icast import control  # noqa: E402
from icast.control import ERROR, OFF, READY, STARTING, Controller, usb  # noqa: E402
from icast.control import __main__ as cli  # noqa: E402
from icast.control.wda import WDA, WDAError  # noqa: E402


# ---------------------------------------------------------------------- mock WebDriverAgent
class MockWDA:
    """Answers like WebDriverAgent and records every request as (method, path, body)."""

    def __init__(self, size=(390, 844)):
        self.requests = []
        self.lock = threading.Lock()
        self.size = size
        self.session = None
        self.sessions = 0
        self.expire_next = False  # answer the next session request with 404 "invalid session id"
        self.fail = {}  # session sub-path -> (HTTP status, W3C error), answered once
        self.drop_next = False  # close the connection without answering, once
        self.status_value = {"ready": True, "ios": {"ip": "10.0.0.2"}}
        self.dead = False
        self.client_ports = set()
        mock_wda = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"  # keep-alive, like the real WDA

            def log_message(self, *args):
                pass

            def do_GET(self):
                self._handle()

            do_POST = do_DELETE = do_GET

            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                with mock_wda.lock:
                    mock_wda.client_ports.add(self.client_address[1])
                    drop = mock_wda.dead or mock_wda.drop_next
                    mock_wda.drop_next = False
                if drop:
                    self.close_connection = True
                    return
                status, payload = mock_wda.route(self.command, self.path, json.loads(raw) if raw else None)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def route(self, method, path, body):
        with self.lock:
            self.requests.append((method, path, body))
            if path == "/status":
                return 200, {"value": self.status_value, "sessionId": self.session}
            if method == "POST" and path == "/session":
                self.sessions += 1
                self.session = f"S{self.sessions}"
                return 200, {"value": {"sessionId": self.session, "capabilities": {}}, "sessionId": self.session}
            if method == "POST" and path == "/wda/homescreen":
                return 200, {"value": None}
            m = re.match(r"^/session/([^/]+)(/.*)$", path)
            if m:
                sid, rest = m.groups()
                if sid != self.session or self.expire_next:
                    self.expire_next = False
                    self.session = None  # WDA forgot the session (e.g. it was restarted)
                    return 404, {"value": {"error": "invalid session id", "message": "Session does not exist"}}
                if rest in self.fail:
                    status, err = self.fail.pop(rest)
                    return status, {"value": {"error": err, "message": "mock failure"}}
                if rest == "/window/size":
                    return 200, {"value": {"width": self.size[0], "height": self.size[1]}}
                return 200, {"value": None, "sessionId": sid}
            return 404, {"value": {"error": "unknown command", "message": f"unhandled {method} {path}"}}

    # ---- inspection helpers
    def snapshot(self):
        with self.lock:
            return list(self.requests)

    def find(self, method=None, suffix=None):
        return [r for r in self.snapshot() if (method is None or r[0] == method) and (suffix is None or r[1].endswith(suffix))]

    def pointer_actions(self):
        """The W3C pointer action sequences that were posted, in order."""
        return [body["actions"][0] for _, _, body in self.find("POST", "/actions")]

    def typed(self):
        return "".join("".join(body["value"]) for _, _, body in self.find("POST", "/wda/keys"))

    def kill(self):
        """Simulate WebDriverAgent dying: open connections are dropped, new ones refused."""
        self.dead = True
        self.httpd.shutdown()
        self.httpd.server_close()

    def close(self):
        if not self.dead:
            self.kill()


def wait_for(predicate, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------- WDA client
class WDAClientTests(unittest.TestCase):
    def setUp(self):
        self.mock = MockWDA()
        self.wda = WDA(self.mock.url, timeout=5)

    def tearDown(self):
        self.wda.close()
        self.mock.close()

    def test_session_and_window_size(self):
        self.assertEqual(self.wda.new_session(), "S1")
        self.assertEqual(self.wda.window_size(), (390.0, 844.0))
        self.assertEqual(self.wda.window_size(), (390.0, 844.0))  # cached
        self.assertEqual(len(self.mock.find("GET", "/window/size")), 1)
        self.wda.window_size(refresh=True)
        self.assertEqual(len(self.mock.find("GET", "/window/size")), 2)
        body = self.mock.find("POST", "/session")[0][2]
        self.assertIn("alwaysMatch", body["capabilities"])

    def test_tap_payload(self):
        self.wda.tap(100.4, 200.6)
        method, path, body = self.mock.find("POST", "/actions")[-1]
        self.assertEqual(path, "/session/S1/actions")
        seq = body["actions"][0]
        self.assertEqual(seq["type"], "pointer")
        self.assertEqual(seq["parameters"], {"pointerType": "touch"})
        steps = seq["actions"]
        self.assertEqual([s["type"] for s in steps], ["pointerMove", "pointerDown", "pause", "pointerUp"])
        self.assertEqual((steps[0]["x"], steps[0]["y"]), (100, 201))
        self.assertLess(steps[2]["duration"], 200)

    def test_long_press_payload(self):
        self.wda.long_press(50, 60)
        steps = self.mock.pointer_actions()[-1]["actions"]
        self.assertEqual([s["type"] for s in steps], ["pointerMove", "pointerDown", "pause", "pointerUp"])
        self.assertEqual((steps[0]["x"], steps[0]["y"]), (50, 60))
        self.assertGreaterEqual(steps[2]["duration"], 500)

    def test_swipe_payload(self):
        self.wda.swipe(10, 700, 12, 200, duration_ms=300, hold_end_ms=50)
        steps = self.mock.pointer_actions()[-1]["actions"]
        self.assertEqual([s["type"] for s in steps], ["pointerMove", "pointerDown", "pointerMove", "pause", "pointerUp"])
        self.assertEqual((steps[0]["x"], steps[0]["y"]), (10, 700))
        self.assertEqual((steps[2]["x"], steps[2]["y"], steps[2]["duration"]), (12, 200, 300))
        self.assertEqual(steps[3]["duration"], 50)
        self.wda.swipe(1, 2, 3, 4)  # no hold: no pause step
        self.assertNotIn("pause", [s["type"] for s in self.mock.pointer_actions()[-1]["actions"]])

    def test_double_tap_is_one_action_sequence(self):
        self.wda.double_tap(100.4, 200.6)
        acts = self.mock.pointer_actions()
        self.assertEqual(len(acts), 1)
        steps = acts[0]["actions"]
        self.assertEqual(
            [s["type"] for s in steps],
            ["pointerMove", "pointerDown", "pause", "pointerUp", "pause", "pointerDown", "pause", "pointerUp"],
        )
        self.assertEqual((steps[0]["x"], steps[0]["y"]), (100, 201))
        self.assertLess(steps[2]["duration"] + steps[4]["duration"] + steps[6]["duration"], 300)

    def test_app_switcher_face_id_sends_no_swipe(self):
        self.assertFalse(self.wda.app_switcher())
        self.assertFalse(self.mock.pointer_actions())
        self.assertFalse(self.mock.find("POST", "/wda/performIoHidEvent"))

    def test_app_switcher_home_button_double_presses_home(self):
        self.mock.size = (375, 667)  # iPhone SE / 8
        self.assertTrue(self.wda.has_home_button())
        self.wda.app_switcher()
        hid = self.mock.find("POST", "/wda/performIoHidEvent")
        self.assertEqual([body for _, _, body in hid], [{"page": 0x0C, "usage": 0x40, "duration": 0.005}] * 2)
        self.assertFalse(self.mock.pointer_actions())  # no swipe: it would open Control Center

    def test_app_switcher_home_button_without_hid_support(self):
        self.mock.size = (414, 736)  # iPhone 8 Plus
        self.mock.fail["/wda/performIoHidEvent"] = (404, "unknown command")
        self.wda.app_switcher()
        self.assertEqual(len(self.mock.find("POST", "/wda/performIoHidEvent")), 1)
        presses = self.mock.find("POST", "/wda/pressButton")
        self.assertEqual([body for _, _, body in presses], [{"name": "home"}] * 2)
        self.assertEqual(self.mock.sessions, 1)  # "unknown command" is not a lost session

    def test_has_home_button(self):
        for size, expected in (((390, 844), False), ((844, 390), False), ((320, 568), True), ((667, 375), True)):
            self.mock.size = size
            self.wda.window_size(refresh=True)
            self.assertEqual(self.wda.has_home_button(), expected, size)

    def test_type_text(self):
        self.wda.type_text("hi\b\n")
        method, path, body = self.mock.find("POST", "/wda/keys")[-1]
        self.assertEqual(path, "/session/S1/wda/keys")
        self.assertEqual(body, {"value": ["h", "i", "\b", "\n"]})

    def test_home_needs_no_session(self):
        self.wda.home()
        self.assertEqual(self.mock.snapshot(), [("POST", "/wda/homescreen", {})])

    def test_press_button(self):
        self.wda.press_button("volumeUp")
        method, path, body = self.mock.find("POST", "/wda/pressButton")[-1]
        self.assertEqual((path, body), ("/session/S1/wda/pressButton", {"name": "volumeUp"}))

    def test_session_recreated_after_invalid_session(self):
        self.wda.tap(1, 1)
        self.mock.expire_next = True
        self.wda.tap(2, 2)
        self.assertEqual(self.wda.session_id, "S2")
        self.assertEqual(self.mock.sessions, 2)
        paths = [p for _, p, _ in self.mock.snapshot()]
        i = paths.index("/session/S1/actions", paths.index("/session/S1/actions") + 1)  # the rejected one
        self.assertEqual(paths[i + 1], "/session")
        self.assertEqual(paths[-1], "/session/S2/actions")
        self.assertEqual(self.mock.pointer_actions()[-1]["actions"][0]["x"], 2)

    def test_other_errors_are_not_retried(self):
        self.wda.new_session()
        self.mock.fail["/wda/keys"] = (500, "unknown error")
        with self.assertRaises(WDAError) as cm:
            self.wda.type_text("x")
        self.assertEqual(cm.exception.status, 500)
        self.assertEqual(cm.exception.error, "unknown error")
        self.assertFalse(cm.exception.invalid_session)
        self.assertEqual(self.mock.sessions, 1)

    def test_keep_alive_and_stale_connection_retry(self):
        self.wda.status()
        self.wda.status()
        self.wda.home()
        self.assertEqual(len(self.mock.client_ports), 1)  # one connection for all requests
        self.mock.drop_next = True  # the server drops the idle connection
        self.assertTrue(self.wda.status()["ready"])  # retried on a fresh connection
        self.assertEqual(len(self.mock.client_ports), 2)

    def test_unreachable_raises_wda_error(self):
        with WDA(f"http://127.0.0.1:{closed_port()}", timeout=3) as wda:
            with self.assertRaises(WDAError):
                wda.status()

    def test_wda_ready_ignores_system_proxy(self):
        bogus = "http://127.0.0.1:9"
        with mock.patch.dict(os.environ, {"HTTP_PROXY": bogus, "http_proxy": bogus, "NO_PROXY": "", "no_proxy": ""}):
            self.assertTrue(usb.wda_ready(self.mock.url))

    def test_wda_ready_rejects_non_wda_answers(self):
        self.mock.status_value = None
        self.assertFalse(usb.wda_ready(self.mock.url))
        self.mock.status_value = {"ready": False}
        self.assertFalse(usb.wda_ready(self.mock.url))

    def test_oversized_wda_response_closes_connection(self):
        self.mock.status_value = {"ready": True, "padding": "x" * 1024}
        with mock.patch("icast.control.wda.MAX_RESPONSE_BYTES", 128), mock.patch.object(self.mock.httpd, "handle_error"):
            with self.assertRaisesRegex(WDAError, "size limit"):
                self.wda.status()
        self.assertIsNone(self.wda._conn)

    def test_revoked_control_sends_no_more_http_commands(self):
        revoked = threading.Event()
        with WDA(self.mock.url, cancelled=revoked.is_set) as wda:
            self.assertTrue(wda.status()["ready"])
            revoked.set()
            with self.assertRaisesRegex(WDAError, "session ended"):
                wda.type_text("must not be sent")
        self.assertFalse(self.mock.find("POST", "/wda/keys"))


# ---------------------------------------------------------------------- Controller
class FakeUsb:
    """Stands in for UsbSession: no subprocesses, no USB."""

    instances = []
    error = None  # raise this from bring_up
    block = False  # wait in bring_up until close()

    def __init__(self):
        self.closed = threading.Event()
        FakeUsb.instances.append(self)

    def bring_up(self, progress=lambda m: None, wait=90):
        progress("fake bring-up")
        if FakeUsb.block:
            self.closed.wait(10)
            raise usb.Cancelled()
        if FakeUsb.error:
            raise FakeUsb.error
        raise AssertionError("unexpected")

    def check(self):
        pass

    def close(self):
        self.closed.set()


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.mock = MockWDA()
        self.states = []
        self.patches = [mock.patch.object(Controller, "_find_running", return_value=self.mock.url)]
        for p in self.patches:
            p.start()
        FakeUsb.instances, FakeUsb.error, FakeUsb.block = [], None, False

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.mock.close()

    def controller(self):
        ctrl = Controller(on_state=lambda s, m: self.states.append((s, m)))
        self.addCleanup(ctrl.stop)
        return ctrl

    def wait_state(self, ctrl, state, timeout=5.0):
        self.assertTrue(wait_for(lambda: ctrl.state == state, timeout), f"state {ctrl.state!r}, wanted {state!r}")

    def test_mirror_control_uses_usb_and_rejects_mismatched_phone(self):
        ctrl = self.controller()
        usb_session = mock.Mock()
        usb_session.bring_up.return_value = self.mock.url
        with mock.patch.object(control, "UsbSession", return_value=usb_session), \
                mock.patch.object(Controller, "_find_running") as discover:
            ctrl.start(expected_peer="10.0.0.3")
            self.wait_state(ctrl, ERROR)
            self.assertEqual(self.mock.sessions, 0)
            ctrl.start(expected_peer="10.0.0.2")
            self.wait_state(ctrl, READY)
            discover.assert_not_called()
            self.assertEqual(self.mock.sessions, 1)

    def next_pointer_action(self, before, timeout=5.0):
        acts = wait_for(lambda: self.mock.pointer_actions()[before:], timeout)
        self.assertTrue(acts, "no pointer action was sent")
        return acts[0]["actions"]

    def test_app_switcher_face_id_shows_hint_without_disabling_control(self):
        ctrl = self.controller()
        ctrl.start()
        self.wait_state(ctrl, READY)
        ctrl.app_switcher()
        self.assertTrue(wait_for(lambda: (READY, control.MSG_SWITCHER_ASSISTIVE) in self.states))
        self.assertFalse(self.mock.pointer_actions())
        ctrl.tap(0.5, 0.5)
        self.next_pointer_action(0)
        self.assertEqual(ctrl.state, READY)

    def test_end_to_end(self):
        ctrl = self.controller()
        ctrl.start()
        self.assertTrue(ctrl.active)  # STARTING right away
        self.wait_state(ctrl, READY)
        self.assertEqual(self.states[0][0], STARTING)
        self.assertEqual(self.states[-1][0], READY)
        self.assertIn("390x844", ctrl.message)
        self.assertEqual(ctrl.wda.base, self.mock.url)

        n = len(self.mock.pointer_actions())
        ctrl.tap(0.5, 0.25)
        steps = self.next_pointer_action(n)
        self.assertEqual((steps[0]["x"], steps[0]["y"]), (195, 211))
        self.assertEqual(steps[1]["type"], "pointerDown")

        for notches, sign in ((1, 1), (-1, -1)):  # wheel away from the user = finger drags down
            n = len(self.mock.pointer_actions())
            ctrl.scroll(0.5, 0.5, notches)
            moves = [s for s in self.next_pointer_action(n) if s["type"] == "pointerMove"]
            dy = moves[1]["y"] - moves[0]["y"]
            self.assertAlmostEqual(dy, sign * 0.12 * 844, delta=1.5)
            self.assertEqual(moves[0]["x"], moves[1]["x"])

        n = len(self.mock.pointer_actions())
        ctrl.long_press(0.1, 0.1)
        steps = self.next_pointer_action(n)
        self.assertEqual((steps[0]["x"], steps[0]["y"]), (39, 84))
        self.assertGreaterEqual(steps[2]["duration"], 500)

        n = len(self.mock.pointer_actions())
        ctrl.swipe(0.5, 0.8, 0.5, 0.2, 0.3)
        steps = self.next_pointer_action(n)
        self.assertEqual((steps[0]["x"], steps[0]["y"]), (195, 675))
        self.assertEqual((steps[2]["x"], steps[2]["y"], steps[2]["duration"]), (195, 169, 300))

        n = len(self.mock.pointer_actions())
        ctrl.swipe(0.5, 0.5, 1.3, -0.2, 0.2)  # dragged out of the window: clamped to the screen
        steps = self.next_pointer_action(n)
        self.assertEqual((steps[2]["x"], steps[2]["y"]), (389, 0))

        ctrl.type_text("hel")
        ctrl.type_text("lo\r")
        self.assertEqual(wait_for(lambda: self.mock.typed() == "hello\n" and self.mock.typed()), "hello\n")

        ctrl.home()
        self.assertTrue(wait_for(lambda: self.mock.find("POST", "/wda/homescreen")))
        ctrl.button("volumeUp")
        self.assertTrue(wait_for(lambda: self.mock.find("POST", "/wda/pressButton")))
        self.assertEqual(self.mock.find("POST", "/wda/pressButton")[-1][2], {"name": "volumeUp"})

        thread = ctrl._thread
        ctrl.stop()
        self.assertEqual(ctrl.state, OFF)
        self.assertFalse(ctrl.active)
        self.assertEqual(self.states[-1], (OFF, ""))
        thread.join(3)
        self.assertFalse(thread.is_alive())
        n = len(self.mock.pointer_actions())
        ctrl.tap(0.5, 0.5)  # ignored when not READY
        time.sleep(0.3)
        self.assertEqual(len(self.mock.pointer_actions()), n)

    def test_double_tap(self):
        ctrl = self.controller()
        ctrl.start()
        self.wait_state(ctrl, READY)
        n = len(self.mock.pointer_actions())
        ctrl.double_tap(0.5, 0.25)
        steps = self.next_pointer_action(n)
        self.assertEqual([s["type"] for s in steps].count("pointerDown"), 2)
        self.assertEqual((steps[0]["x"], steps[0]["y"]), (195, 211))
        time.sleep(0.2)
        self.assertEqual(len(self.mock.pointer_actions()), n + 1)  # one request for both taps

    def test_rotation_is_picked_up_before_the_idle_check(self):
        ctrl = self.controller()
        ctrl.screen_aspect = 390 / 844
        ctrl.start()
        self.wait_state(ctrl, READY)
        n = len(self.mock.pointer_actions())
        ctrl.tap(0.9, 0.5)
        self.assertEqual(self.next_pointer_action(n)[0]["x"], 351)  # portrait: 0.9 * 390
        sizes = len(self.mock.find("GET", "/window/size"))

        self.mock.size = (844, 390)  # the iPhone turns to landscape ...
        ctrl.screen_aspect = 844 / 390  # ... and so does the video
        n = len(self.mock.pointer_actions())
        ctrl.tap(0.9, 0.5)
        steps = self.next_pointer_action(n)
        self.assertEqual((steps[0]["x"], steps[0]["y"]), (760, 195))  # not clamped to the portrait size
        self.assertEqual(len(self.mock.find("GET", "/window/size")), sizes + 1)

        n = len(self.mock.pointer_actions())
        ctrl.tap(0.1, 0.1)  # orientations agree again: the cached size is used
        self.next_pointer_action(n)
        self.assertEqual(len(self.mock.find("GET", "/window/size")), sizes + 1)

    def test_stop_always_announces_off(self):
        ctrl = self.controller()
        self.assertEqual(ctrl.snapshot(), (OFF, "", 0))
        ctrl.stop()  # already off: listeners still hear about it, to resync after a late notification
        self.assertEqual(self.states, [(OFF, "")])
        state, message, serial = ctrl.snapshot()
        self.assertEqual((state, message), (OFF, ""))
        self.assertEqual(serial, 1)
        ctrl.start()
        self.wait_state(ctrl, READY)
        self.assertGreater(ctrl.snapshot()[2], serial)

    def test_small_wheel_deltas_accumulate(self):
        ctrl = self.controller()
        ctrl.start()
        self.wait_state(ctrl, READY)
        n = len(self.mock.pointer_actions())
        ctrl.scroll(0.5, 0.5, 0.1)  # ~10 pt: would be a tap, so it is held back
        time.sleep(0.3)
        self.assertEqual(len(self.mock.pointer_actions()), n)
        ctrl.scroll(0.5, 0.5, 0.3)  # together 0.4 notch = ~40 pt
        moves = [s for s in self.next_pointer_action(n) if s["type"] == "pointerMove"]
        self.assertAlmostEqual(moves[1]["y"] - moves[0]["y"], 0.4 * 0.12 * 844, delta=1.5)
        time.sleep(0.3)
        self.assertEqual(len(self.mock.pointer_actions()), n + 1)

    def test_restart_after_stop(self):
        ctrl = self.controller()
        ctrl.start()
        self.wait_state(ctrl, READY)
        ctrl.stop()
        ctrl.start()
        self.wait_state(ctrl, READY)
        self.assertEqual(self.mock.sessions, 2)

    def test_usb_error_then_restart(self):
        self.patches[0].stop()
        self.patches = [
            mock.patch.object(Controller, "_find_running", return_value=None),
            mock.patch.object(control, "UsbSession", FakeUsb),
        ]
        for p in self.patches:
            p.start()
        FakeUsb.error = usb.UsbError("no phone here")
        ctrl = self.controller()
        ctrl.start()
        self.wait_state(ctrl, ERROR)
        self.assertEqual(ctrl.message, "no phone here")
        self.assertFalse(ctrl.active)
        self.assertTrue(FakeUsb.instances[0].closed.is_set())
        self.assertIn((STARTING, "fake bring-up"), self.states)
        ctrl.start()  # a new attempt is possible right away
        self.assertTrue(wait_for(lambda: len(FakeUsb.instances) == 2))
        self.wait_state(ctrl, ERROR)
        self.assertEqual([s for s, _ in self.states].count(ERROR), 2)

    def test_stop_during_usb_bring_up(self):
        self.patches[0].stop()
        self.patches = [
            mock.patch.object(Controller, "_find_running", return_value=None),
            mock.patch.object(control, "UsbSession", FakeUsb),
        ]
        for p in self.patches:
            p.start()
        FakeUsb.block = True
        ctrl = self.controller()
        ctrl.start()
        self.assertTrue(wait_for(lambda: (STARTING, "fake bring-up") in self.states))
        first_thread = ctrl._thread
        ctrl.stop()
        self.assertEqual(ctrl.state, OFF)
        self.assertTrue(FakeUsb.instances[0].closed.is_set())  # helper processes are stopped
        ctrl.start()  # not blocked by the old thread
        self.assertTrue(wait_for(lambda: len(FakeUsb.instances) == 2))
        self.assertEqual(ctrl.state, STARTING)
        ctrl.stop()
        first_thread.join(3)
        self.assertFalse(first_thread.is_alive())
        self.assertNotIn(ERROR, [s for s, _ in self.states])
        self.assertEqual(ctrl.state, OFF)

    def test_lost_connection_is_detected_when_idle(self):
        with mock.patch.object(Controller, "IDLE_CHECK", 0.2):
            ctrl = self.controller()
            ctrl.start()
            self.wait_state(ctrl, READY)
            self.mock.kill()
            self.wait_state(ctrl, ERROR, timeout=20)
            self.assertEqual(ctrl.message, control.MSG_LOST)


# ---------------------------------------------------------------------- USB helpers
def _zip_bytes(entries):
    """entries: (name, data or None for a directory, unix mode or None)"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data, mode in entries:
            zi = zipfile.ZipInfo(name, (2024, 1, 2, 3, 4, 6))
            if mode is not None:
                zi.create_system = 3
                zi.external_attr = mode << 16
            z.writestr(zi, b"" if data is None else data)
    return buf.getvalue()


class UsbTests(unittest.TestCase):
    APP = "build/Release-iphoneos/WebDriverAgentRunner-Runner.app/"
    PAYLOAD = "Payload/WebDriverAgentRunner-Runner.app/"
    EXE = "WebDriverAgentRunner-Runner"

    def download(self, data, dest, digest=None):
        with (
            mock.patch("urllib.request.urlopen", side_effect=lambda *args, **kwargs: io.BytesIO(data)),
            mock.patch.object(usb, "WDA_RELEASE_SHA256", digest or hashlib.sha256(data).hexdigest()),
        ):
            return usb.download_wda_ipa(dest, progress=lambda m: None)

    def app_entries(self):
        plist = plistlib.dumps({
            "CFBundleExecutable": self.EXE,
            "CFBundleIdentifier": "com.facebook.WebDriverAgentRunner.xctrunner",
        })
        return [
            (self.APP, None, 0o40755),
            (self.APP + "Info.plist", plist, 0o100644),
            (self.APP + self.EXE, b"\xcf\xfa\xed\xfe binary", 0o100755),
        ]

    def test_download_wda_ipa_repackages_app(self):
        app = self.APP
        xctest = "PlugIns/WebDriverAgentRunner.xctest/"
        framework = "Frameworks/Test.framework/"
        debug = "PlugIns/WebDriverAgentRunner.xctest.dSYM/"
        data = _zip_bytes(self.app_entries() + [
            ("build/", None, None),
            (app + xctest, None, 0o40755),
            (app + xctest + "Info.plist", plistlib.dumps({"CFBundleExecutable": "WebDriverAgentRunner"}), 0o100644),
            (app + xctest + "WebDriverAgentRunner", b"XCTest binary", 0o100755),
            (app + "Frameworks/", None, 0o40755),
            (app + framework + "Test", b"Framework binary", 0o100755),
            (app + framework + "Current", b"Test", 0o120777),
            (app + "Resources/debug.dSYM.txt", b"ordinary resource", None),
            (app + debug, None, 0o40755),
            (app + debug + "Contents/Info.plist", b"debug plist", None),
            (app + debug + "Contents/Resources/DWARF/WebDriverAgentRunner", b"debug binary", None),
            (app + framework + "nested/Framework.DsYm/Contents/Resources/DWARF/Test", b"debug binary", None),
            (app + ".DS_Store", b"junk", None),
            (app + xctest + "._Info.plist", b"resource fork", None),
            (app + "__MACOSX/resources/junk", b"metadata", None),
            ("build/README.txt", b"not part of the app", None),
            ("__MACOSX/" + app + "._Info.plist", b"resource fork", None),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out" / "WebDriverAgentRunner.ipa"
            path = self.download(data, dest)
            self.assertEqual(path, dest)
            self.assertEqual(sorted(p.name for p in dest.parent.iterdir()), ["WebDriverAgentRunner.ipa"])
            with zipfile.ZipFile(path) as ipa:
                self.assertIsNone(ipa.testzip())
                self.assertEqual(set(ipa.namelist()), {self.PAYLOAD + n for n in (
                    "", "Info.plist", self.EXE, xctest, xctest + "Info.plist",
                    xctest + "WebDriverAgentRunner", "Frameworks/", framework + "Test",
                    framework + "Current", "Resources/debug.dSYM.txt",
                )})
                exe = ipa.getinfo(self.PAYLOAD + self.EXE)
                self.assertEqual(ipa.read(exe), b"\xcf\xfa\xed\xfe binary")
                self.assertEqual(exe.create_system, 3)
                self.assertEqual((exe.external_attr >> 16) & 0o777, 0o755)  # still executable
                self.assertEqual(exe.date_time, (2024, 1, 2, 3, 4, 6))
                self.assertEqual(ipa.read(self.PAYLOAD + xctest + "WebDriverAgentRunner"), b"XCTest binary")
                link = ipa.getinfo(self.PAYLOAD + framework + "Current")
                self.assertEqual(link.external_attr >> 16, 0o120777)
                self.assertEqual(ipa.read(link), b"Test")

    def test_download_rejects_unsafe_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "WDA.ipa"
            for name in ("../unsafe", "/absolute", "C:/drive", self.APP + "../escape",
                         self.APP + "./file", self.APP + "a//b", self.APP + "file:stream"):
                with self.subTest(name=name), self.assertRaisesRegex(usb.UsbError, "Unsafe path"):
                    self.download(_zip_bytes(self.app_entries() + [(name, b"bad", None)]), dest)
            for marker in (b"\\", b"\0"):
                data = _zip_bytes(self.app_entries() + [(self.APP + "unsafe_path", b"bad", None)])
                with self.subTest(marker=marker), self.assertRaisesRegex(usb.UsbError, "Unsafe path"):
                    self.download(data.replace(b"unsafe_path", b"unsafe" + marker + b"path"), dest)
            with self.assertRaisesRegex(usb.UsbError, "Unsafe path"):
                self.download(_zip_bytes(self.app_entries() + [(self.APP + "Escape", b"../../outside", 0o120777)]), dest)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_download_rejects_invalid_zip_or_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "WDA.ipa"
            with self.assertRaisesRegex(usb.UsbError, "Could not package"):
                self.download(b"not a ZIP", dest)
            with self.assertRaisesRegex(usb.UsbError, "SHA-256"):
                self.download(_zip_bytes(self.app_entries()), dest, digest="0" * 64)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_download_retries_gateway_error_via_release_url(self):
        data = _zip_bytes(self.app_entries())
        error = urllib.error.HTTPError(usb.WDA_RELEASE_ASSET_URL, 504, "Gateway Timeout", {}, None)
        progress = []
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "urllib.request.urlopen", side_effect=[error, io.BytesIO(data)]
        ) as urlopen, mock.patch.object(usb, "WDA_RELEASE_SHA256", hashlib.sha256(data).hexdigest()):
            dest = Path(tmp) / "WDA.ipa"
            self.assertEqual(usb.download_wda_ipa(dest, progress.append), dest)
            with zipfile.ZipFile(dest) as ipa:
                self.assertIsNone(ipa.testzip())
                self.assertIn(self.PAYLOAD + self.EXE, ipa.namelist())
        requests = [call.args[0] for call in urlopen.call_args_list]
        self.assertEqual([req.full_url for req in requests], [usb.WDA_RELEASE_ASSET_URL, usb.WDA_RELEASE_URL])
        self.assertEqual(requests[0].get_header("Accept"), "application/octet-stream")
        self.assertTrue(any("alternate" in message for message in progress))
        self.assertFalse(any("HTTP 504" in message for message in progress))

    def test_download_retries_when_api_returns_metadata_instead_of_the_asset(self):
        data = _zip_bytes(self.app_entries())
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "urllib.request.urlopen", side_effect=[io.BytesIO(b'{"id":648705675}'), io.BytesIO(data)]
        ), mock.patch.object(usb, "WDA_RELEASE_SHA256", hashlib.sha256(data).hexdigest()):
            dest = Path(tmp) / "WDA.ipa"
            self.assertEqual(usb.download_wda_ipa(dest, progress=lambda m: None), dest)
            with zipfile.ZipFile(dest) as ipa:
                self.assertIsNone(ipa.testzip())

    def test_download_rejects_wrong_digest_from_fallback(self):
        data = _zip_bytes(self.app_entries())
        error = urllib.error.HTTPError(usb.WDA_RELEASE_ASSET_URL, 504, "Gateway Timeout", {}, None)
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "WDA.ipa"
            original = self.download(data, dest).read_bytes()
            with mock.patch("urllib.request.urlopen", side_effect=[error, io.BytesIO(b"unverified content")]):
                with self.assertRaisesRegex(usb.UsbError, "HTTP 504.*SHA-256"):
                    usb.download_wda_ipa(dest, progress=lambda m: None)
            self.assertEqual(dest.read_bytes(), original)

    def test_download_rejects_ambiguous_bundle_or_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "WDA.ipa"
            with self.assertRaisesRegex(usb.UsbError, "expected one outer .app"):
                self.download(_zip_bytes(self.app_entries() + [("Other.app/Info.plist", b"other", None)]), dest)
            with self.assertRaisesRegex(usb.UsbError, "Duplicate path"):
                self.download(_zip_bytes(self.app_entries() + [(self.APP + "info.plist", b"duplicate", None)]), dest)

    def test_failed_packaging_preserves_existing_ipa(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "WDA.ipa"
            original = self.download(_zip_bytes(self.app_entries()), dest).read_bytes()
            for data, error in (
                (_zip_bytes(self.app_entries()[:-1]), "executable"),
                (_zip_bytes([(self.APP + self.EXE, b"binary", None)]), "Info.plist"),
                (_zip_bytes(self.app_entries()).replace(b"\xcf\xfa\xed\xfe binary", b"\xcf\xfa\xed\xfe BROKEN"), "CRC"),
            ):
                with self.subTest(error=error), self.assertRaisesRegex(usb.UsbError, error):
                    self.download(data, dest)
                self.assertEqual(dest.read_bytes(), original)
                self.assertEqual(list(Path(tmp).iterdir()), [dest])

    def test_download_without_app_bundle_fails(self):
        data = _zip_bytes([("readme.txt", b"hello", None)])
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "WDA.ipa"
            with self.assertRaises(usb.UsbError):
                self.download(data, dest)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_children_never_use_pythonw(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("python.exe", "pythonw.exe"):
                (Path(tmp) / name).write_bytes(b"")
            with mock.patch.object(sys, "executable", str(Path(tmp) / "pythonw.exe")):
                self.assertEqual(usb._python(), str(Path(tmp) / "python.exe"))

    def test_list_devices(self):
        with mock.patch.object(usb, "usbmuxd_reachable", return_value=False):
            with self.assertRaises(usb.UsbError) as cm:
                usb.list_devices()
            self.assertEqual(str(cm.exception), usb.MSG_NO_USBMUXD)
        err = "2026-11-08 18:38:07 PC __main__[1] ERROR Failed to connect to usbmuxd socket. Make sure it's running.\n"
        with mock.patch.object(usb, "usbmuxd_reachable", return_value=True), \
                mock.patch.object(usb, "pmd3", return_value=(1, "", err)):
            with self.assertRaises(usb.UsbError) as cm:
                usb.list_devices()
            self.assertEqual(str(cm.exception), usb.MSG_NO_USBMUXD)
        dev = {"Identifier": "00008110-AAAA", "DeviceName": "Test iPhone", "ProductVersion": "17.5.1"}
        with mock.patch.object(usb, "usbmuxd_reachable", return_value=True), \
                mock.patch.object(usb, "pmd3", return_value=(0, json.dumps([dev], indent=4), "")):
            self.assertEqual(usb.list_devices(), [dev])

    def test_list_devices_untrusted(self):
        def fake(*args, timeout=60, cancel=None):
            if "--simple" in args:
                return 0, json.dumps(["00008110-AAAA"]), ""
            return 1, "", "ERROR LockdownError: something the hints do not know\n"

        with mock.patch.object(usb, "usbmuxd_reachable", return_value=True), mock.patch.object(usb, "pmd3", fake):
            with self.assertRaises(usb.UsbError) as cm:
                usb.list_devices()
            self.assertEqual(str(cm.exception), usb.MSG_NOT_TRUSTED)

    def test_find_wda_bundle(self):
        apps = {
            "com.example.notes": {"CFBundleName": "Notes"},
            "com.example.WebDriverAgentRunner": {"CFBundleName": "WebDriverAgentRunner"},
            "com.example.WebDriverAgentRunner.xctrunner": {"CFBundleName": "WebDriverAgentRunner-Runner"},
        }
        with mock.patch.object(usb, "pmd3", return_value=(0, json.dumps(apps), "")) as pm:
            self.assertEqual(usb.find_wda_bundle("UDID1"), "com.example.WebDriverAgentRunner.xctrunner")
            self.assertEqual(pm.call_args[0], ("apps", "list", "--type", "User", "--udid", "UDID1"))
        renamed = {"io.sideloaded.abc": {"CFBundleExecutable": "WebDriverAgentRunner-Runner"}}
        with mock.patch.object(usb, "pmd3", return_value=(0, json.dumps(renamed), "")):
            self.assertEqual(usb.find_wda_bundle("UDID1"), "io.sideloaded.abc")
        with mock.patch.object(usb, "pmd3", return_value=(0, json.dumps({"com.example.notes": {}}), "")):
            self.assertIsNone(usb.find_wda_bundle("UDID1"))

    def test_explain(self):
        self.assertEqual(usb.explain("x ERROR Failed to connect to usbmuxd socket. Make sure it's running."),
                         usb.MSG_NO_USBMUXD)
        self.assertEqual(usb.explain("x ERROR Waiting for the user to answer the Trust dialog on the device."),
                         usb.MSG_NOT_TRUSTED)
        self.assertEqual(usb.explain("x ERROR Developer Mode is disabled. You can try ..."), usb.MSG_DEV_MODE)
        self.assertEqual(usb.explain("ERROR ... its profile has not been explicitly trusted by the user"),
                         usb.MSG_UNTRUSTED_DEV)
        self.assertIsNone(usb.explain("x INFO Trying again over a no-root userspace tunnel (tunneld)"))
        self.assertIsNone(usb.explain(""))

    def _bring_up(self, version, wda_version=usb.WDA_RUNNER_VERSION, ready=True):
        calls, spawned = [], []

        def fake_pmd3(*args, timeout=60, cancel=None):
            calls.append(list(args))
            if args[:2] == ("usbmux", "list"):
                dev = {"Identifier": "UDID1", "DeviceName": "Test iPhone", "ProductVersion": version}
                return 0, json.dumps([dev]), ""
            if args[0] == "amfi":
                return 0, "true\n", ""
            if args[0] == "apps":
                return 0, json.dumps({"com.facebook.WebDriverAgentRunner.xctrunner": {}}), ""
            return 0, "", ""

        def fake_spawn(session, name, args):
            spawned.append((name, list(args)))

        with mock.patch.object(usb, "pmd3", fake_pmd3), \
                mock.patch.object(usb, "usbmuxd_reachable", return_value=True), \
                mock.patch.object(usb, "probe_wda", return_value=({"build": {"version": wda_version}}, ready)), \
                mock.patch.object(usb.UsbSession, "read_log", return_value="ICAST_USB_FORWARD_PORT=18100\n"), \
                mock.patch.object(usb.UsbSession, "_spawn", fake_spawn):
            session = usb.UsbSession()
            url = session.bring_up()
        return url, calls, spawned

    def test_bring_up_command_lines(self):
        for version, userspace, dev_mode in (("17.5.1", True, True), ("17.2", False, True), ("15.8", False, False)):
            url, calls, spawned = self._bring_up(version)
            self.assertEqual(url, "http://127.0.0.1:18100")
            self.assertEqual([c[0] for c in calls], ["usbmux"] + (["amfi"] if dev_mode else []) + ["mounter", "apps"])
            self.assertIn(["mounter", "auto-mount", "--udid", "UDID1"], calls)
            runner = ["developer", "dvt", "xcuitest", "com.facebook.WebDriverAgentRunner.xctrunner",
                      "--udid", "UDID1", "--env", "USE_IP=127.0.0.1", "--env", "USE_PORT=8100"]
            self.assertEqual(spawned, [
                ("wda_runner", runner + (["--userspace"] if userspace else [])),
                ("wda_forward", ["UDID1", "8100"]),
            ])

    def test_bring_up_rejects_unsupported_runner(self):
        for ready in (True, False):
            with self.subTest(ready=ready), self.assertRaises(usb.UsbError) as cm:
                self._bring_up("17.5.1", wda_version="1.0", ready=ready)
            self.assertIn("com.facebook.WebDriverAgentRunner.xctrunner", str(cm.exception))
            self.assertIn("Port 8100 reported version '1.0'", str(cm.exception))
            self.assertIn("If the selected bundle is an old copy, remove it", str(cm.exception))

    def test_expired_runner_error_names_the_selected_bundle(self):
        session = usb.UsbSession()
        session.bundle = "com.example.WebDriverAgentRunner.old"
        process = mock.Mock(poll=mock.Mock(return_value=1))
        session.procs.append(("wda_runner", process, None))
        with self.assertRaises(usb.UsbError) as cm:
            with mock.patch.object(session, "read_log", return_value="The application signature has expired"):
                session.check()
        self.assertIn(session.bundle, str(cm.exception))
        self.assertIn(usb.MSG_EXPIRED, str(cm.exception))

    def test_forward_readiness_is_isolated_between_sessions(self):
        def spawn(args, *, script, stdout, stderr):
            stdout.write(f"ICAST_USB_FORWARD_PORT={args[0]}\n")
            stdout.flush()
            return mock.Mock(poll=mock.Mock(return_value=0))

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(usb, "APP_DIR", Path(tmp)), \
                mock.patch.object(usb, "_popen", side_effect=spawn):
            archive = Path(tmp) / "wda_forward.log"
            archive.write_text("ICAST_USB_FORWARD_PORT=18000\n", encoding="utf-8")
            first, second = usb.UsbSession(), usb.UsbSession()
            try:
                self.assertEqual(first.read_log("wda_forward"), "")
                first._spawn("wda_forward", ["18001"])
                second._spawn("wda_forward", ["18002"])
                self.assertIn("ICAST_USB_FORWARD_PORT=18001", first.read_log("wda_forward"))
                self.assertIn("ICAST_USB_FORWARD_PORT=18002", second.read_log("wda_forward"))
                first.close()
                self.assertEqual(first.read_log("wda_forward"), "")
                self.assertIn("ICAST_USB_FORWARD_PORT=18002", second.read_log("wda_forward"))
            finally:
                first.close()
                second.close()
            self.assertIn("ICAST_USB_FORWARD_PORT=18002", archive.read_text(encoding="utf-8"))
            self.assertEqual(list(Path(tmp).iterdir()), [archive])

    def test_close_cancels_bring_up(self):
        def slow_pmd3(*args, timeout=60, cancel=None):
            if cancel.wait(10):
                raise usb.Cancelled()
            return 0, "[]", ""

        session = usb.UsbSession()
        result = []
        with mock.patch.object(usb, "pmd3", slow_pmd3), mock.patch.object(usb, "usbmuxd_reachable", return_value=True):
            t = threading.Thread(target=lambda: result.append(self._catch(session.bring_up)))
            t.start()
            time.sleep(0.2)
            session.close()
            t.join(3)
        self.assertFalse(t.is_alive())
        self.assertIsInstance(result[0], usb.Cancelled)
        with self.assertRaises(usb.Cancelled):
            session._spawn("x", ["--help"])  # nothing can be started after close()

    @staticmethod
    def _catch(fn):
        try:
            return fn()
        except Exception as e:
            return e

    @unittest.skipUnless(sys.platform == "win32", "Windows job objects")
    def test_children_are_tied_to_a_kill_on_close_job(self):
        import ctypes
        from ctypes import wintypes

        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"],
                             creationflags=usb.NO_WINDOW)
        try:
            usb._tie_to_job(p)
            self.assertIsNotNone(usb._job)
            k32 = ctypes.WinDLL("kernel32")
            k32.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
            result = wintypes.BOOL()
            self.assertTrue(k32.IsProcessInJob(int(p._handle), usb._job, ctypes.byref(result)))
            self.assertTrue(result.value)
        finally:
            p.kill()
            p.wait()

    @unittest.skipUnless(importlib.util.find_spec("pymobiledevice3"), "pymobiledevice3 not installed")
    def test_command_lines_match_installed_pymobiledevice3(self):
        """Every command we run must parse: pymobiledevice3 rejects unknown options (exit 2)
        before it handles --help (exit 0)."""
        _, calls, spawned = self._bring_up("17.5.1")
        commands = calls + [args for name, args in spawned if name == "wda_runner"] + [
            ["usbmux", "list", "--usb", "--simple"],
            ["amfi", "reveal-developer-mode", "--udid", "UDID1"],
        ]
        procs = [
            (args, subprocess.Popen([usb._python(), "-m", "pymobiledevice3", *args, "--help"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    creationflags=usb.NO_WINDOW, env=usb._child_env()))
            for args in commands
        ]
        for args, p in procs:
            _, err = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0, f"pymobiledevice3 {' '.join(args)}: {err.decode(errors='replace')[-400:]}")


# ---------------------------------------------------------------------- command line
class CliTests(unittest.TestCase):
    def setUp(self):
        self.mock = MockWDA()

    def tearDown(self):
        self.mock.close()

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(argv))
        text = out.getvalue()
        text.encode("ascii")  # console output stays ASCII
        return code, text

    def test_debug_commands(self):
        code, out = self.run_cli("tap", "10", "20.4", "--url", self.mock.url)
        self.assertEqual(code, 0, out)
        self.assertIn("[OK]", out)
        steps = self.mock.pointer_actions()[-1]["actions"]
        self.assertEqual((steps[0]["x"], steps[0]["y"]), (10, 20))
        code, out = self.run_cli("type", "ab\\n", "--url", self.mock.url)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.mock.typed(), "ab\n")
        code, out = self.run_cli("home", "--url", self.mock.url)
        self.assertEqual(code, 0, out)
        self.assertTrue(self.mock.find("POST", "/wda/homescreen"))

    def test_debug_command_without_wda(self):
        code, out = self.run_cli("home", "--url", f"http://127.0.0.1:{closed_port()}")
        self.assertEqual(code, 1)
        self.assertIn("[FAIL]", out)

    def test_start_runs_until_ctrl_c(self):
        import _thread

        real_say = cli.say

        def say(text=""):
            real_say(text)
            if "Running." in text:
                _thread.interrupt_main()  # like pressing Ctrl+C once control is ready

        with mock.patch.object(Controller, "_find_running", return_value=self.mock.url), \
                mock.patch.object(cli, "say", say):
            code, out = self.run_cli("start")
        self.assertEqual(code, 0, out)
        self.assertIn("[OK]   Control ready (390x844 pt)", out)
        self.assertIn(f"WebDriverAgent: {self.mock.url}", out)
        self.assertIn("Stopped", out)

    def test_start_reports_errors(self):
        FakeUsb.instances, FakeUsb.error, FakeUsb.block = [], usb.UsbError(usb.MSG_NO_DEVICE), False
        with mock.patch.object(Controller, "_find_running", return_value=None), \
                mock.patch.object(control, "UsbSession", FakeUsb):
            code, out = self.run_cli("start")
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] Control stopped", out)
        self.assertIn("No iPhone found on USB", out)
        self.assertTrue(FakeUsb.instances[0].closed.is_set())

    def test_doctor_without_usbmuxd(self):
        with mock.patch.object(usb, "usbmuxd_reachable", return_value=False), \
                mock.patch.object(cli, "DEFAULT_URL", self.mock.url):
            code, out = self.run_cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("[OK]   pymobiledevice3", out)
        self.assertIn("[FAIL] Apple's USB service", out)
        self.assertIn("Apple Devices", out)
        self.assertIn(f"[OK]   WebDriverAgent answers at {self.mock.url} (iPhone Wi-Fi address 10.0.0.2)", out)

    def test_doctor_reports_wda_that_answers_but_is_not_ready(self):
        self.mock.status_value = {"ready": False}
        with mock.patch.object(cli, "_check_usb", return_value=False), \
                mock.patch.object(cli, "DEFAULT_URL", self.mock.url):
            code, out = self.run_cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("reports it is unavailable for control", out)
        self.assertNotIn("so control works", out)

    def test_doctor_all_good(self):
        dev = {"Identifier": "UDID1", "DeviceName": "Test iPhone", "ProductVersion": "17.5.1", "ProductType": "iPhone15,2"}
        with mock.patch.object(usb, "usbmuxd_reachable", return_value=True), \
                mock.patch.object(usb, "list_devices", return_value=[dev]), \
                mock.patch.object(usb, "developer_mode_enabled", return_value=True), \
                mock.patch.object(usb, "find_wda_bundle", return_value="com.facebook.WebDriverAgentRunner.xctrunner"), \
                mock.patch.object(cli, "DEFAULT_URL", self.mock.url):
            code, out = self.run_cli("doctor")
        self.assertEqual(code, 0, out)
        self.assertIn("Test iPhone (iPhone15,2), iOS 17.5.1, UDID UDID1", out)
        self.assertIn("Developer Mode is on", out)
        self.assertIn("com.facebook.WebDriverAgentRunner.xctrunner", out)
        self.assertNotIn("[FAIL]", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
