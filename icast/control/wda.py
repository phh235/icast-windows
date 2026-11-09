"""Minimal WebDriverAgent (WDA) HTTP client: taps, swipes, typing, hardware buttons.

WebDriverAgent is Appium's XCTest-based automation server. Once it runs on the
iPhone it listens on port 8100 and accepts W3C WebDriver requests. Coordinates are
in iOS points (e.g. 393 x 852 on an iPhone 15), in the current interface orientation.

The client keeps one HTTP/1.1 keep-alive connection (every new TCP connection through
the USB port forwarder costs a usbmux handshake) and never uses the system proxy:
WDA is always on 127.0.0.1 or on the local network.
"""
import http.client
import json
import logging
import threading
import urllib.parse

log = logging.getLogger(__name__)

INVALID_SESSION = "invalid session id"

# HID usage of the Home button (consumer page, "Menu"), for /wda/performIoHidEvent
HID_PAGE_CONSUMER, HID_USAGE_HOME = 0x0C, 0x40
# iPhones with a Home button have a 16:9 screen (e.g. 375 x 667 pt); Face ID iPhones are
# about 19.5:9 (e.g. 393 x 852 pt).
HOME_BUTTON_MAX_ASPECT = 2.0


class WDAError(Exception):
    """A failed WDA request. `status` is the HTTP status (None if there was no answer),
    `error` the W3C error code from the response body (e.g. "invalid session id")."""

    def __init__(self, message, status=None, error=None):
        super().__init__(message)
        self.status = status
        self.error = error

    @property
    def invalid_session(self):
        # W3C WDA: HTTP 404 + "invalid session id"; a bare 404 without a JSON error means the
        # session part of the path was not recognised either.
        return self.error == INVALID_SESSION or (self.status == 404 and not self.error)


def describe_error(e):
    """Short English text for a transport error (Windows socket errors come localised)."""
    if isinstance(e, ConnectionRefusedError):
        return "connection refused"
    if isinstance(e, TimeoutError):
        return "timed out"
    if isinstance(e, http.client.RemoteDisconnected):
        return "connection closed without an answer"
    if isinstance(e, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return "connection lost"
    if isinstance(e, OSError) and getattr(e, "winerror", None):
        return f"{type(e).__name__} (WinError {e.winerror})"
    return str(e) or type(e).__name__


class WDA:
    def __init__(self, base_url: str, timeout: float = 15):
        self.base = base_url.rstrip("/")
        url = urllib.parse.urlsplit(self.base)
        if url.scheme != "http" or not url.hostname:
            raise ValueError(f"not an http:// URL: {base_url!r}")
        self._host = url.hostname
        self._port = url.port or 80
        self._prefix = url.path.rstrip("/")
        self.timeout = timeout
        self.session_id = None
        self.size = None  # (width, height) in points
        self._conn = None
        self._lock = threading.Lock()

    def __repr__(self):
        return f"WDA({self.base!r})"

    def close(self):
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ transport
    def _send(self, method, path, data, timeout):
        """One HTTP exchange on the keep-alive connection; returns (status, raw body)."""
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        with self._lock:
            for attempt in (1, 2):
                if self._conn is None:
                    self._conn = http.client.HTTPConnection(self._host, self._port, timeout=timeout)
                conn = self._conn
                reused = conn.sock is not None
                conn.timeout = timeout
                if conn.sock is not None:
                    conn.sock.settimeout(timeout)
                try:
                    conn.request(method, self._prefix + path, body=data, headers=headers)
                    resp = conn.getresponse()
                    return resp.status, resp.read()
                except (http.client.HTTPException, OSError) as e:
                    conn.close()
                    self._conn = None
                    # An idle keep-alive connection that the server already closed fails like
                    # this on first use: retry once on a fresh connection. Never retry timeouts.
                    stale = isinstance(e, (ConnectionError, http.client.BadStatusLine))
                    if attempt == 1 and reused and stale and not isinstance(e, TimeoutError):
                        continue
                    raise WDAError(f"{method} {path} -> {describe_error(e)}") from None

    def _request(self, method, path, body=None, timeout=None):
        if body is None and method == "POST":
            body = {}
        data = None if body is None else json.dumps(body).encode()
        status, raw = self._send(method, path, data, timeout or self.timeout)
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {"value": payload}
        value = payload.get("value")
        if status >= 400:
            err = value.get("error") if isinstance(value, dict) else None
            msg = value.get("message") if isinstance(value, dict) else None
            text = f"{method} {path} -> HTTP {status}"
            if err:
                text += f" {err}"
            if msg:
                text += ": " + str(msg).strip().splitlines()[0][:200]
            raise WDAError(text, status, err)
        legacy = payload.get("status")  # pre-W3C (JSONWP) WDA builds: HTTP 200 + non-zero status
        if isinstance(legacy, int) and not isinstance(legacy, bool) and legacy != 0:
            err = INVALID_SESSION if legacy == 6 else "unknown error"
            raise WDAError(f"{method} {path} -> status {legacy}: {value}", status, err)
        return payload

    def _session(self, method, path, body=None, timeout=None):
        """Request on the current session, recreating it once if WDA forgot it."""
        if self.session_id is None:
            self.new_session()
        try:
            return self._request(method, f"/session/{self.session_id}{path}", body, timeout)
        except WDAError as e:
            if not e.invalid_session:
                raise
            log.info("WebDriverAgent session %s is gone, creating a new one", self.session_id)
            self.new_session()
            return self._request(method, f"/session/{self.session_id}{path}", body, timeout)

    # ------------------------------------------------------------------ session
    def status(self, timeout=3):
        value = self._request("GET", "/status", timeout=timeout).get("value")
        return value if isinstance(value, dict) else {}

    def new_session(self):
        caps = {"shouldWaitForQuiescence": False, "waitForIdleTimeout": 0}
        res = self._request("POST", "/session", {"capabilities": {"alwaysMatch": caps}})
        value = res.get("value") if isinstance(res.get("value"), dict) else {}
        self.session_id = res.get("sessionId") or value.get("sessionId")
        if not self.session_id:
            raise WDAError("WebDriverAgent did not return a session id")
        try:  # make gestures snappy: do not wait for animations / idle
            self._request(
                "POST",
                f"/session/{self.session_id}/appium/settings",
                {"settings": {"waitForIdleTimeout": 0, "animationCoolOffTimeout": 0, "snapshotTimeout": 1}},
            )
        except WDAError:
            pass
        self.size = None
        return self.session_id

    def window_size(self, refresh=False):
        """Screen size in points for the current orientation (cached; refresh=True re-reads it)."""
        if self.size is None or refresh:
            v = self._session("GET", "/window/size").get("value")
            v = v if isinstance(v, dict) else {}
            size = (float(v.get("width") or 0), float(v.get("height") or 0))
            if not all(size):
                raise WDAError("could not read the screen size")
            self.size = size
        return self.size

    # ------------------------------------------------------------------ gestures (points)
    def _pointer(self, steps):
        body = {
            "actions": [
                {"type": "pointer", "id": "finger1", "parameters": {"pointerType": "touch"}, "actions": steps}
            ]
        }
        self._session("POST", "/actions", body, timeout=30)

    def tap(self, x, y, hold_ms=40):
        self._pointer(
            [
                {"type": "pointerMove", "duration": 0, "x": round(x), "y": round(y)},
                {"type": "pointerDown", "button": 0},
                {"type": "pause", "duration": int(hold_ms)},
                {"type": "pointerUp", "button": 0},
            ]
        )

    def double_tap(self, x, y, hold_ms=40, gap_ms=80):
        """Two taps in ONE action sequence, so iOS gets them close enough together to see a
        double tap (two separate requests are each delayed by a round trip)."""
        x, y = round(x), round(y)
        self._pointer(
            [
                {"type": "pointerMove", "duration": 0, "x": x, "y": y},
                {"type": "pointerDown", "button": 0},
                {"type": "pause", "duration": int(hold_ms)},
                {"type": "pointerUp", "button": 0},
                {"type": "pause", "duration": int(gap_ms)},
                {"type": "pointerDown", "button": 0},
                {"type": "pause", "duration": int(hold_ms)},
                {"type": "pointerUp", "button": 0},
            ]
        )

    def long_press(self, x, y, hold_ms=700):
        self.tap(x, y, hold_ms)

    def swipe(self, x1, y1, x2, y2, duration_ms=250, hold_end_ms=0):
        """Drag from (x1, y1) to (x2, y2). hold_end_ms > 0 keeps the finger still before lifting
        it, which stops scroll views from flinging."""
        steps = [
            {"type": "pointerMove", "duration": 0, "x": round(x1), "y": round(y1)},
            {"type": "pointerDown", "button": 0},
            {"type": "pointerMove", "duration": int(max(20, duration_ms)), "x": round(x2), "y": round(y2)},
        ]
        if hold_end_ms:
            steps.append({"type": "pause", "duration": int(hold_end_ms)})
        steps.append({"type": "pointerUp", "button": 0})
        self._pointer(steps)

    # ------------------------------------------------------------------ system
    def home(self):
        self._request("POST", "/wda/homescreen")

    def has_home_button(self):
        """True for iPhones with a Home button (iPhone SE, 8 and older), judged by the screen shape."""
        w, h = self.window_size()
        return max(w, h) / min(w, h) < HOME_BUTTON_MAX_ASPECT

    def app_switcher(self):
        if self.has_home_button():
            # no gesture for it there (a swipe up from the bottom opens Control Center)
            self.double_press_home()
            return
        w, h = self.window_size()
        self.swipe(w / 2, h - 2, w / 2, h * 0.62, duration_ms=280, hold_end_ms=450)

    def double_press_home(self):
        """Press the Home button twice quickly (opens the App Switcher on Home-button iPhones)."""
        try:
            self._hid_home()
        except WDAError as e:
            if e.status is None:
                raise  # no answer at all: not a missing feature
            # older WDA builds have no performIoHidEvent: use the slower pressButton instead
            log.info("performIoHidEvent failed (%s), double-pressing Home with pressButton", e)
            self.press_button("home")
            self.press_button("home")
            return
        self._hid_home()

    def _hid_home(self):
        body = {"page": HID_PAGE_CONSUMER, "usage": HID_USAGE_HOME, "duration": 0.005}
        self._session("POST", "/wda/performIoHidEvent", body)

    def type_text(self, text: str):
        """Type into the focused field. '\\b' deletes a character, '\\n' presses Return."""
        if text:
            self._session("POST", "/wda/keys", {"value": list(text)})

    def press_button(self, name: str):
        """name: home, volumeUp, volumeDown (and on newer iOS: action, camera)."""
        self._session("POST", "/wda/pressButton", {"name": name})

    def lock(self):
        self._session("POST", "/wda/lock")

    def unlock(self):
        self._session("POST", "/wda/unlock")
