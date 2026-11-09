"""Control the iPhone from the PC (mouse + keyboard) through WebDriverAgent.

The Controller runs on its own thread: it finds or starts WebDriverAgent (already
running on the phone over Wi-Fi, or brought up over USB) and then executes input
commands from a queue. Positions are passed as fractions of the mirrored screen
(0..1), so they do not depend on the window size.

    ctrl = Controller(on_state=lambda state, message: ...)  # called on the control thread
    ctrl.device_ip = "192.168.1.20"   # optional: also look for WDA on the phone's Wi-Fi address
    ctrl.screen_aspect = 1179 / 2556  # optional: width / height of the mirrored video
    ctrl.start()                      # off -> starting -> ready (or error)
    ctrl.tap(0.5, 0.25); ctrl.scroll(0.5, 0.5, 1); ctrl.type_text("hi\\n")
    ctrl.stop()                       # -> off; also stops the USB helper processes

stop() announces OFF on the calling thread (every time, even when already off). on_state
is called with an internal lock held, so it must not block waiting for another thread
(emitting a queued Qt signal is fine). Notifications from the control thread can arrive
late at a listener that queues them: a listener should read snapshot() rather than trust
the arguments of an old notification.
"""
import collections
import logging
import queue
import threading
import time

from .usb import WDA_PORT, Cancelled, UsbError, UsbSession, wda_ready
from .wda import WDA, WDAError

log = logging.getLogger(__name__)

OFF, STARTING, READY, ERROR = "off", "starting", "ready", "error"

MSG_LOST = "Lost connection to WebDriverAgent. Enable control again."

SCROLL_PER_NOTCH = 0.12  # fraction of the screen height one wheel notch scrolls
SCROLL_MAX_STEP = 0.45  # longest single drag, fraction of the height
SCROLL_MAX_STEPS = 3  # drags per scroll command; more notches than that are dropped
SCROLL_MIN_PT = 24  # shorter drags would be taken as taps: accumulate small wheel deltas
ROTATION_RECHECK_S = 0.5  # while the video and WDA disagree on the orientation, re-read the size this often


class _Run:
    """One start() .. stop() cycle. A stale run (stopped, or replaced by a newer start())
    can never change the controller's state."""

    def __init__(self):
        self.stop = threading.Event()
        self.q = queue.Queue()
        self.usb = None
        self._lock = threading.Lock()

    def attach(self, usb):
        with self._lock:
            if self.stop.is_set():
                return False
            self.usb = usb
            return True

    def close(self):
        self.stop.set()
        with self._lock:
            usb, self.usb = self.usb, None
        if usb is not None:
            usb.close()


class Controller:
    IDLE_CHECK = 4.0  # seconds without input after which the connection and screen size are re-checked

    def __init__(self, on_state=lambda state, message: None):
        self.on_state = on_state
        self.state = OFF
        self.message = ""
        self.device_ip = None  # learned from the AirPlay connection (WDA also listens on Wi-Fi)
        # width / height of the mirrored video, set by the UI; None = unknown. When the iPhone
        # rotates, the video turns at once, while WDA's cached screen size would only be
        # re-read after IDLE_CHECK seconds without input.
        self.screen_aspect = None
        self.wda = None
        self._lock = threading.RLock()
        self._run = None
        self._thread = None
        self._scroll_carry = (0.0, 0.0)  # (notches not scrolled yet, time)
        self._size_checked = 0.0  # time of the last orientation-driven size refresh
        self._serial = 0  # number of announced state changes

    @property
    def active(self):
        return self.state in (STARTING, READY)

    def snapshot(self):
        """(state, message, serial) read together. serial goes up with every announced change,
        so a listener can tell a new error from one it has already shown."""
        with self._lock:
            return self.state, self.message, self._serial

    def start(self):
        with self._lock:
            if self._run is not None and not self._run.stop.is_set():
                return  # already starting or ready
            run = self._run = _Run()
            # visible at once to callers; the control thread announces it through on_state
            self.state, self.message = STARTING, "Looking for WebDriverAgent..."
            self._thread = threading.Thread(target=self._main, args=(run,), name="control", daemon=True)
            self._thread.start()

    def stop(self):
        with self._lock:
            run, self._run = self._run, None
            self.wda = None
            # always announced, also when already off, so a listener that applied a late
            # (queued) notification gets back in sync
            self._set(OFF, "")
        if run is not None:
            run.q.put(None)
            run.close()  # kills the USB helper processes, also while bring-up is in progress

    def _set(self, state, message, run=None):
        """Change state and notify; ignored for a stale run. Returns whether it applied."""
        with self._lock:
            if run is not None and run is not self._run:
                return False
            self.state, self.message = state, message
            self._serial += 1
            log.info("control %s: %s", state, message.replace("\n", " | "))
            try:
                self.on_state(state, message)
            except Exception:
                log.exception("on_state failed")
            return True

    def _fail(self, run, message):
        if run.stop.is_set():
            return  # stopped by the user: not an error
        run.close()
        self._set(ERROR, message, run)

    # ---- control thread
    def _main(self, run):
        wda = None
        try:
            self._set(STARTING, "Looking for WebDriverAgent...", run)
            self._scroll_carry = (0.0, 0.0)
            self._size_checked = 0.0
            url = self._find_running()
            if not url:
                usb = UsbSession()
                if not run.attach(usb):
                    return
                url = usb.bring_up(progress=lambda m: self._set(STARTING, m, run))
            wda = WDA(url)
            wda.new_session()
            w, h = wda.window_size()
            with self._lock:
                if run is not self._run or run.stop.is_set():
                    return
                self.wda = wda
                self._set(READY, f"Control ready ({w:.0f}x{h:.0f} pt)", run)
            self._loop(run, wda)
        except Cancelled:
            pass
        except (UsbError, WDAError) as e:
            self._fail(run, str(e))
        except Exception as e:
            log.exception("control thread failed")
            self._fail(run, f"Unexpected error: {e}")
        finally:
            run.close()
            if wda is not None:
                wda.close()
            with self._lock:
                if self.wda is wda and wda is not None:
                    self.wda = None

    def _loop(self, run, wda):
        held = collections.deque()  # commands taken out of the queue while coalescing
        while True:
            if held:
                cmd = held.popleft()
            else:
                try:
                    cmd = run.q.get(timeout=self.IDLE_CHECK)
                except queue.Empty:
                    self._idle_check(run, wda)
                    continue
            if cmd is None or run.stop.is_set():
                return
            cmd = self._coalesce(run.q, cmd, held)
            if cmd[0] == "swipe" and run.q.qsize() > 8:
                continue  # falling behind: drop stale drags
            try:
                self._execute(wda, *cmd)
            except WDAError as e:
                if run.stop.is_set():
                    return
                log.warning("command %s failed: %s", cmd[0], e)
                if not wda_ready(wda.base):
                    raise WDAError(MSG_LOST) from None

    def _idle_check(self, run, wda):
        """No input for a while: make sure WDA is still there and re-read the screen size
        (it swaps when the phone rotates)."""
        usb = run.usb
        if usb is not None:
            usb.check()  # UsbError if the WDA runner or the port forwarder exited
        try:
            wda.window_size(refresh=True)
        except WDAError as e:
            if run.stop.is_set():
                return
            if not wda_ready(wda.base):
                raise WDAError(MSG_LOST) from None
            log.info("screen size refresh failed: %s", e)

    @staticmethod
    def _coalesce(q, cmd, held):
        """Merge queued key presses / wheel notches into one WDA request."""
        kind = cmd[0]
        if kind not in ("type", "scroll"):
            return cmd
        while True:
            try:
                nxt = q.get_nowait()
            except queue.Empty:
                return cmd
            if nxt is not None and nxt[0] == kind:
                if kind == "type":
                    cmd = ("type", cmd[1] + nxt[1])
                    continue
                if abs(nxt[1] - cmd[1]) < 0.05 and abs(nxt[2] - cmd[2]) < 0.05:
                    cmd = ("scroll", cmd[1], cmd[2], cmd[3] + nxt[3])
                    continue
            held.append(nxt)
            return cmd

    def _find_running(self):
        candidates = [f"http://127.0.0.1:{WDA_PORT}"]
        if self.device_ip and self.device_ip not in ("127.0.0.1", "localhost"):
            candidates.append(f"http://{self.device_ip}:{WDA_PORT}")
        for url in candidates:
            if wda_ready(url, timeout=1.5):
                log.info("WebDriverAgent already running at %s", url)
                return url
        return None

    # ---- commands (callable from any thread; ignored unless READY)
    def _put(self, *cmd):
        run = self._run
        if run is not None and self.state == READY:
            run.q.put(cmd)

    def tap(self, xf, yf):
        self._put("tap", xf, yf)

    def double_tap(self, xf, yf):
        self._put("double", xf, yf)

    def long_press(self, xf, yf):
        self._put("long", xf, yf)

    def swipe(self, x1f, y1f, x2f, y2f, seconds):
        self._put("swipe", x1f, y1f, x2f, y2f, seconds)

    def scroll(self, xf, yf, amount):
        """amount in wheel notches; > 0 = wheel away from you = content moves down (finger drags down)."""
        self._put("scroll", xf, yf, amount)

    def type_text(self, text):
        """'\\b' = backspace, '\\n' = return ('\\r' and '\\r\\n' are taken as return too)."""
        if text:
            self._put("type", text.replace("\r\n", "\n").replace("\r", "\n"))

    def home(self):
        self._put("home")

    def app_switcher(self):
        self._put("switcher")

    def button(self, name):
        """name: volumeUp, volumeDown, home."""
        self._put("button", name)

    # ---- execution (control thread)
    def _screen_size(self, wda):
        """Screen size in points. The cached size is re-read when it does not match the
        orientation of the mirrored video (the iPhone has just rotated)."""
        w, h = wda.window_size()
        aspect = self.screen_aspect
        if aspect and (w > h) != (aspect > 1):
            now = time.monotonic()
            if now - self._size_checked >= ROTATION_RECHECK_S:
                self._size_checked = now
                w, h = wda.window_size(refresh=True)
                log.info("screen size re-read after rotation: %.0fx%.0f pt", w, h)
        return w, h

    def _execute(self, wda, kind, *a):
        w, h = self._screen_size(wda)

        def px(f):  # WDA rejects points outside the screen (a drag may end outside the window)
            return min(max(f * w, 0.0), w - 1)

        def py(f):
            return min(max(f * h, 0.0), h - 1)

        if kind == "tap":
            wda.tap(px(a[0]), py(a[1]))
        elif kind == "double":
            wda.double_tap(px(a[0]), py(a[1]))
        elif kind == "long":
            wda.long_press(px(a[0]), py(a[1]))
        elif kind == "swipe":
            ms = max(60, min(1500, a[4] * 1000))
            wda.swipe(px(a[0]), py(a[1]), px(a[2]), py(a[3]), duration_ms=ms)
        elif kind == "scroll":
            self._scroll(wda, w, h, *a)
        elif kind == "type":
            wda.type_text(a[0])
        elif kind == "home":
            wda.home()
        elif kind == "switcher":
            wda.app_switcher()
        elif kind == "button":
            wda.press_button(a[0])
        else:
            log.warning("unknown command %s", kind)

    def _scroll(self, wda, w, h, xf, yf, notches):
        now = time.monotonic()
        carry, when = self._scroll_carry
        if now - when > 1.0 or carry * notches < 0:
            carry = 0.0  # stale, or the wheel changed direction
        notches += carry
        per_notch = SCROLL_PER_NOTCH * h
        total = notches * per_notch  # points; > 0 = finger drags down
        if abs(total) < SCROLL_MIN_PT:
            self._scroll_carry = (notches, now)
            return
        x = min(max(xf * w, w * 0.08), w * 0.92)  # away from the edges (edge swipe = "back")
        y = yf * h
        for _ in range(SCROLL_MAX_STEPS):
            if abs(total) < SCROLL_MIN_PT:
                break
            dy = max(-SCROLL_MAX_STEP * h, min(SCROLL_MAX_STEP * h, total))
            y1 = min(max(y, h * 0.15 + max(0, -dy)), h * 0.85 - max(0, dy))
            # the short pause before lifting the finger stops the scroll view from flinging,
            # so one notch always scrolls the same distance
            wda.swipe(x, y1, x, y1 + dy, duration_ms=150, hold_end_ms=60)
            total -= dy
        rest = total / per_notch if abs(total) < SCROLL_MIN_PT else 0.0
        self._scroll_carry = (rest, time.monotonic())
