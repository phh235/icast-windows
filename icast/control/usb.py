"""Bring up WebDriverAgent on a USB-connected iPhone with pymobiledevice3.

Steps (each is a pymobiledevice3 CLI call, run in the background without a console):
  1. find the iPhone through Apple's usbmuxd (installed by the "Apple Devices" app)
  2. check Developer Mode
  3. mount the Developer Disk Image (downloaded/personalised automatically)
  4. find the installed WebDriverAgent runner and launch it as an XCUITest
     (iOS 17.4+: through a userspace tunnel, no admin rights needed)
  5. forward a local TCP port to WDA's port 8100 on the phone
"""
import hashlib
import io
import json
import logging
import os
import plistlib
import re
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from contextlib import suppress
from http.client import HTTPException
from pathlib import Path
from xml.parsers.expat import ExpatError

from ..config import APP_DIR
from .wda import WDA, WDAError, describe_error

log = logging.getLogger(__name__)

WDA_PORT = 8100
USB_FORWARD_SCRIPT = Path(__file__).with_name("_usb_forward.py")
WDA_RELEASE_TAG = "v16.15.3"
# The release asset's WebDriverAgentLib.framework reports this version in /status.
WDA_RUNNER_VERSION = "16.15.2"
WDA_RELEASE_URL = (
    f"https://github.com/appium/WebDriverAgent/releases/download/{WDA_RELEASE_TAG}/WebDriverAgentRunner-Runner.zip"
)
# Update the tag, asset ID and digest together using the official release asset.
WDA_RELEASE_ASSET_URL = "https://api.github.com/repos/appium/WebDriverAgent/releases/assets/648705675"
WDA_RELEASE_SHA256 = "2176793538d902f6b1e61291e9997f68d306541f222ea715d256a7c6ce9d4fc5"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
USBMUXD_WINDOWS = ("127.0.0.1", 27015)  # Apple Mobile Device Service

MSG_NO_USBMUXD = (
    "Apple's USB service (usbmuxd) is not running.\n"
    "Install \"Apple Devices\" from the Microsoft Store (or iTunes), connect the iPhone "
    "with a cable and tap \"Trust\" on the phone."
)
MSG_NO_DEVICE = "No iPhone found on USB. Connect it with a cable, unlock it and tap \"Trust\"."
MSG_NOT_TRUSTED = (
    "The iPhone does not trust this PC yet. Unlock it, tap \"Trust\" and enter the passcode, then try again."
)
MSG_DENIED = "The iPhone refused to trust this PC. Unplug it, plug it back in and tap \"Trust\"."
MSG_LOCKED = "The iPhone is locked. Unlock it and try again."
MSG_NO_PMD3 = "pymobiledevice3 is not installed. Run install.bat again."
MSG_DEV_MODE = (
    "Developer Mode is off.\n"
    "Run:  python -m icast.control reveal-dev-mode\n"
    "then on the iPhone: Settings > Privacy & Security > Developer Mode > On (the phone restarts)."
)
MSG_NO_WDA = (
    "WebDriverAgent is not installed on the iPhone.\n"
    "Run:  python -m icast.control get-wda\n"
    "and install the resulting WebDriverAgentRunner.ipa with Sideloadly using your Apple ID "
    "(see README, section \"USB control\")."
)
MSG_UNTRUSTED_DEV = (
    "The iPhone does not trust the developer of WebDriverAgent yet.\n"
    "On the iPhone open Settings > General > VPN & Device Management, tap your Apple ID, "
    "tap \"Trust\", then try again."
)
MSG_EXPIRED = (
    "The WebDriverAgent signature has expired (apps signed with a free Apple ID last 7 days).\n"
    "Install WebDriverAgentRunner.ipa again with Sideloadly."
)
MSG_IOS_17_EARLY = (
    "iOS 17.0-17.3 can only be controlled with administrator rights.\n"
    "Update the iPhone to iOS 17.4 or later, or run \"pymobiledevice3 remote tunneld\" as administrator."
)
MSG_WDA_TIMEOUT = (
    "WebDriverAgent did not start. If the phone shows an \"Untrusted Developer\" message, trust it in "
    "Settings > General > VPN & Device Management, then try again."
)

# known pymobiledevice3 / XCTest error texts -> advice for the user (checked in order)
_HINTS = [
    (("connect to usbmuxd",), MSG_NO_USBMUXD),
    (("password protected", "unlock and retry"), MSG_LOCKED),
    (("refused to trust",), MSG_DENIED),
    (("trust dialog", "not paired", "does not trust this computer", "pairing with the device failed"), MSG_NOT_TRUSTED),
    (("developer mode is disabled",), MSG_DEV_MODE),
    (("cannot serve this device", "tunneld"), MSG_IOS_17_EARLY),
    (("explicitly trusted", "untrusted developer", "invalid code signature"), MSG_UNTRUSTED_DEV),
    (("has expired",), MSG_EXPIRED),
    (("device is not connected", "no device with the requested udid"), MSG_NO_DEVICE),
]


class UsbError(Exception):
    pass


class Cancelled(UsbError):
    """The USB session was closed while it was being brought up."""

    def __init__(self, message="cancelled"):
        super().__init__(message)


# ---------------------------------------------------------------------- child processes
def _python():
    """Interpreter for child processes: python.exe, never pythonw.exe (pythonw has no usable
    stdout/stderr); CREATE_NO_WINDOW keeps python.exe from opening a console window."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        console = exe.with_name("python.exe")
        if console.exists():
            return str(console)
    return str(exe)


def _child_env():
    env = dict(os.environ)
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1", NO_COLOR="1")
    return env


_job = None
_job_lock = threading.Lock()


def _tie_to_job(proc):
    """Windows: put the child in a kill-on-close job object owned by this process, so the
    background pymobiledevice3 processes never outlive a crashed or killed app."""
    global _job
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        with _job_lock:
            if _job is None:

                class Basic(ctypes.Structure):
                    _fields_ = [
                        ("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD),
                    ]

                class Extended(ctypes.Structure):
                    _fields_ = [
                        ("BasicLimitInformation", Basic),
                        ("IoInfo", ctypes.c_uint64 * 6),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t),
                    ]

                job = k32.CreateJobObjectW(None, None)
                if not job:
                    return
                info = Extended()
                info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                    k32.CloseHandle(job)
                    return
                _job = job  # never closed: it lives as long as this process
        if not k32.AssignProcessToJobObject(_job, int(proc._handle)):
            log.debug("AssignProcessToJobObject failed: %s", ctypes.get_last_error())
    except Exception as e:  # best effort only
        log.debug("could not tie child process to a job: %s", e)


def _popen(args, *, script=None, **kw):
    entrypoint = [str(script)] if script else ["-m", "pymobiledevice3"]
    proc = subprocess.Popen(
        [_python(), "-I", "-X", "utf8", "-u", *entrypoint, *args],
        stdin=subprocess.DEVNULL, creationflags=NO_WINDOW, env=_child_env(), **kw,
    )
    _tie_to_job(proc)
    return proc


def _kill(proc):
    try:
        proc.kill()
    except OSError:
        pass


def pmd3(*args, timeout=60, cancel=None):
    """Run a pymobiledevice3 command; returns (exit code, stdout, stderr).
    `cancel` (a threading.Event) kills the command early and raises Cancelled."""
    log.info("pymobiledevice3 %s", " ".join(args))
    try:
        p = _popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    except OSError as e:
        raise UsbError(f"Could not start pymobiledevice3: {describe_error(e)}") from None
    deadline = time.monotonic() + timeout
    while True:
        try:
            out, err = p.communicate(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            cancelled = cancel is not None and cancel.is_set()
            if cancelled or time.monotonic() > deadline:
                _kill(p)
                p.communicate()
                if cancelled:
                    raise Cancelled() from None
                raise UsbError(f"pymobiledevice3 {' '.join(args)} timed out") from None
    if p.returncode:
        if "No module named pymobiledevice3" in err:
            raise UsbError(MSG_NO_PMD3)
        log.warning("pymobiledevice3 %s failed (exit %s): %s", " ".join(args[:3]), p.returncode, _tail(err, 3))
    return p.returncode, out, err


# ---------------------------------------------------------------------- helpers
def _json(text):
    starts = [i for i in ((text or "").find("["), (text or "").find("{")) if i >= 0]
    if not starts:
        return None
    try:
        return json.loads(text[min(starts):])
    except ValueError:
        return None


def _tail(text, n=8):
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    return "\n".join(lines[-n:])


def explain(text):
    """Advice for a known pymobiledevice3 failure in `text` (stderr or a log), else None."""
    lines = [l for l in (text or "").splitlines() if " INFO " not in l and " DEBUG " not in l]
    low = "\n".join(lines).lower()
    for needles, message in _HINTS:
        if any(n in low for n in needles):
            return message
    return None


def failure_text(text, what):
    return explain(text) or f"{what}:\n{_tail(text, 6)}"


def parse_version(version):
    """'17.4.1' -> (17, 4); anything unparsable -> (0, 0)."""
    parts = (str(version or "").split(".") + ["0", "0"])[:2]
    try:
        return int(parts[0] or 0), int(parts[1] or 0)
    except ValueError:
        return 0, 0


def usbmuxd_address():
    """(host, port) of usbmuxd, or a unix socket path."""
    addr = os.environ.get("PYMOBILEDEVICE3_USBMUX") or os.environ.get("USBMUXD_SOCKET_ADDRESS")
    if addr:
        if addr.upper().startswith("UNIX:"):
            return addr[5:]
        host, sep, port = addr.rpartition(":")
        if sep and port.isdigit() and "/" not in addr:
            return host or "127.0.0.1", int(port)
        return addr
    if sys.platform == "win32":
        return USBMUXD_WINDOWS
    return "/var/run/usbmuxd"


def usbmuxd_reachable(timeout=1.5):
    addr = usbmuxd_address()
    if isinstance(addr, tuple):
        try:
            with socket.create_connection(addr, timeout=timeout):
                return True
        except OSError:
            return False
    return os.path.exists(addr)


# ---------------------------------------------------------------------- device queries
def list_devices(cancel=None):
    """iPhones on USB: dicts with Identifier, DeviceName, ProductVersion, ProductType, ..."""
    if not usbmuxd_reachable():
        raise UsbError(MSG_NO_USBMUXD)
    code, out, err = pmd3("usbmux", "list", "--usb", timeout=30, cancel=cancel)
    if code == 0:
        devices = _json(out)
        return [d for d in devices if isinstance(d, dict)] if isinstance(devices, list) else []
    hint = explain(err)
    if hint is None:
        # reading the device details needs lockdown; is there a device at all?
        code2, out2, _ = pmd3("usbmux", "list", "--usb", "--simple", timeout=30, cancel=cancel)
        udids = _json(out2) if code2 == 0 else None
        if isinstance(udids, list) and any(isinstance(u, str) for u in udids):
            hint = MSG_NOT_TRUSTED
    if hint:
        raise UsbError(hint)
    raise UsbError("Could not list the USB devices:\n" + _tail(err, 6))


def select_device(devices):
    """Return the only connected iPhone and its usable UDID."""
    if not devices:
        raise UsbError(MSG_NO_DEVICE)
    if len(devices) > 1:
        raise UsbError("Connect only the iPhone you want to control by USB.")
    device = devices[0]
    udid = device.get("Identifier") or device.get("UniqueDeviceID")
    if not isinstance(udid, str) or not udid.strip():
        raise UsbError("usbmuxd reported an iPhone without a UDID. Reconnect it and try again.")
    return device, udid


def developer_mode_enabled(udid, cancel=None):
    code, out, err = pmd3("amfi", "developer-mode-status", "--udid", udid, timeout=30, cancel=cancel)
    if code:
        raise UsbError(failure_text(err or out, "Could not read the Developer Mode status"))
    return out.strip().lower().startswith("true")


def find_wda_bundle(udid, cancel=None):
    """Bundle id of the installed WebDriverAgent runner, or None."""
    code, out, err = pmd3("apps", "list", "--type", "User", "--udid", udid, timeout=60, cancel=cancel)
    if code:
        raise UsbError(failure_text(err or out, "Could not list the apps on the iPhone"))
    apps = _json(out) or {}
    if isinstance(apps, list):
        apps = {a.get("CFBundleIdentifier", ""): a for a in apps if isinstance(a, dict)}
    if not isinstance(apps, dict):
        return None

    def is_wda(bundle, info):
        names = [bundle]
        if isinstance(info, dict):  # re-signing tools may rename the bundle id
            names += [str(info.get(k) or "") for k in ("CFBundleName", "CFBundleExecutable", "CFBundleDisplayName")]
        return any("webdriveragent" in n.lower() for n in names)

    candidates = [b for b, info in apps.items() if b and is_wda(b, info)]
    candidates.sort(key=lambda b: ("xctrunner" not in b.lower(), len(b)))
    return candidates[0] if candidates else None


def probe_wda(base_url, timeout=2.0):
    """Return the status payload and whether WDA is ready to accept commands."""
    if not base_url:
        return {}, False
    try:
        with WDA(base_url, timeout=timeout) as wda:
            value = wda.status(timeout=timeout)
    except (WDAError, ValueError):
        return {}, False
    return value, bool(value) and bool(value.get("ready", True))


def wda_ready(base_url, timeout=2.0):
    return probe_wda(base_url, timeout)[1]


# ---------------------------------------------------------------------- session
class UsbSession:
    """Owns the background pymobiledevice3 processes (WDA runner + port forwarder).

    close() may be called from any thread at any time; a bring_up() in progress then stops
    within a fraction of a second with Cancelled, and nothing is left running."""

    def __init__(self):
        self.procs = []  # (name, Popen, log file)
        self.udid = None
        self.device = {}
        self.ios = (0, 0)
        self.bundle = None
        self.local_port = None
        self._forward_log = None
        self._closed = threading.Event()
        self._lock = threading.Lock()

    @property
    def base_url(self):
        port = self.local_port
        return f"http://127.0.0.1:{port}" if port else None

    @property
    def closed(self):
        return self._closed.is_set()

    def bring_up(self, progress=lambda msg: None, wait=90):
        cancel = self._closed
        progress("Looking for the iPhone on USB...")
        self.device, self.udid = select_device(list_devices(cancel=cancel))
        version = str(self.device.get("ProductVersion") or "0")
        self.ios = parse_version(version)
        name = self.device.get("DeviceName") or "iPhone"
        log.info("USB device %s (%s, iOS %s)", name, self.udid, version)

        if self.ios[0] >= 16:
            progress(f"{name} (iOS {version}): checking Developer Mode...")
            if not developer_mode_enabled(self.udid, cancel=cancel):
                raise UsbError(MSG_DEV_MODE)

        progress("Mounting the Developer Disk Image (downloaded the first time)...")
        code, out, err = pmd3("mounter", "auto-mount", "--udid", self.udid, timeout=600, cancel=cancel)
        if code:
            raise UsbError(failure_text(err or out, "Could not mount the Developer Disk Image"))

        progress("Looking for WebDriverAgent...")
        self.bundle = find_wda_bundle(self.udid, cancel=cancel)
        if not self.bundle:
            raise UsbError(MSG_NO_WDA)

        progress("Starting WebDriverAgent on the iPhone...")
        self._spawn("wda_runner", xcuitest_args(self.bundle, self.udid, self.ios))
        self._spawn("wda_forward", [self.udid, str(WDA_PORT)])

        progress("Waiting for WebDriverAgent to answer...")
        deadline = time.monotonic() + wait
        while True:
            self.check()
            if self.local_port is None:
                ready = re.search(r"^ICAST_USB_FORWARD_PORT=([0-9]{1,5})$", self.read_log("wda_forward"), re.M)
                if ready and 1 <= int(ready[1]) <= 65535:
                    self.local_port = int(ready[1])
            url = self.base_url
            if url:
                status, ready = probe_wda(url)
                build = status.get("build")
                version = build.get("version") if isinstance(build, dict) else None
                if (ready or version is not None) and version != WDA_RUNNER_VERSION:
                    reported = repr(version[:80]) if isinstance(version, str) else "unknown"
                    raise UsbError(
                        f"Selected WDA bundle {self.bundle!r}. Port {WDA_PORT} reported version {reported} "
                        f"(expected {WDA_RUNNER_VERSION}).\n"
                        "If the selected bundle is an old copy, remove it and retry with the supported USB-only build. "
                        "If the selected bundle is current, stop any other running WDA. "
                        "To get the supported IPA, run control.bat get-wda."
                    )
                if ready:
                    return url
            if time.monotonic() > deadline:
                break
            if cancel.wait(1):
                raise Cancelled()
        text = self.read_log("wda_runner")
        raise UsbError(f"Selected WDA bundle {self.bundle!r}.\n" + (explain(text) or MSG_WDA_TIMEOUT + "\n" + _tail(text)))

    def check(self):
        """Raise UsbError if the session was closed or one of its processes has exited."""
        if self._closed.is_set():
            raise Cancelled()
        for name, p, _ in list(self.procs):
            if p.poll() is not None:
                text = self.read_log(name)
                if name == "wda_runner":
                    what = f"WebDriverAgent bundle {self.bundle!r} stopped"
                else:
                    what = "The USB port forwarder stopped"
                hint = explain(text)
                if hint:
                    raise UsbError(f"{what}.\n{hint}")
                raise UsbError(f"{what} (exit code {p.returncode}):\n{_tail(text)}")

    def read_log(self, name):
        with self._lock:
            path = self._forward_log if name == "wda_forward" else APP_DIR / f"{name}.log"
            if path:
                with suppress(OSError):
                    return path.read_text("utf-8", "replace")
        return ""

    def _spawn(self, name, args):
        with self._lock:
            if self._closed.is_set():
                raise Cancelled()
            APP_DIR.mkdir(parents=True, exist_ok=True)
            if name == "wda_forward":
                logf = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", errors="replace", dir=APP_DIR,
                                                  prefix=".wda_forward-", suffix=".log", delete=False)
                self._forward_log = Path(logf.name)
            else:
                logf = open(APP_DIR / f"{name}.log", "w", encoding="utf-8", errors="replace")
            logf.write("pymobiledevice3 " + " ".join(args) + "\n")
            logf.flush()
            log.info("spawn %s: %s", name, " ".join(args))
            try:
                script = USB_FORWARD_SCRIPT if name == "wda_forward" else None
                p = _popen(args, script=script, stdout=logf, stderr=subprocess.STDOUT)
            except OSError as e:
                logf.close()
                raise UsbError(f"Could not start pymobiledevice3: {describe_error(e)}") from None
            self.procs.append((name, p, logf))

    def close(self):
        with self._lock:
            self._closed.set()
            procs, self.procs = self.procs, []
            forward_log, self._forward_log = self._forward_log, None
        for _, p, _ in procs:
            if p.poll() is None:
                try:
                    p.terminate()
                except OSError:
                    pass
        for _, p, logf in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _kill(p)
            logf.close()
        if forward_log:
            # Keep the last completed log for troubleshooting, never for readiness checks.
            try:
                forward_log.replace(APP_DIR / "wda_forward.log")
            except OSError:
                with suppress(OSError):
                    forward_log.unlink(missing_ok=True)
        self.local_port = None


def xcuitest_args(bundle, udid, ios=(0, 0)):
    args = ["developer", "dvt", "xcuitest", bundle, "--udid", udid,
            "--env", "USE_IP=127.0.0.1", "--env", f"USE_PORT={WDA_PORT}"]
    if tuple(ios) >= (17, 4):
        # in-process userspace tunnel: no administrator rights needed. iOS 17.0-17.3 has no
        # CoreDeviceProxy, so there pymobiledevice3 falls back to a tunneld started as admin.
        args.append("--userspace")
    return args


# ---------------------------------------------------------------------- WebDriverAgent download
def _zip_path(name):
    parts = tuple(name.removesuffix("/").split("/"))
    if any(
        not part or part in (".", "..") or part.endswith((".", " "))
        or any(c in '\\:<>"|?*' or ord(c) < 32 for c in part)
        for part in parts
    ):
        raise UsbError(f"Unsafe path in WebDriverAgent archive: {name!r}")
    return parts


def _ipa_member(name):
    """Return safe ZIP path components, or None for metadata and debug symbols."""
    parts = _zip_path(name)
    if any(
        part in ("__macosx", ".ds_store") or part.startswith("._") or part.endswith(".dsym")
        for part in map(str.casefold, parts)
    ):
        return None
    return parts


def _ipa_members(src):
    root = None
    members = {}
    paths = {}
    for info in src.infolist():
        # zipfile can truncate NULs and normalize backslashes in filename.
        parts = _ipa_member(info.orig_filename)
        if parts is None:
            continue
        index = next((i for i, part in enumerate(parts) if part.casefold().endswith(".app")), None)
        if index is None:
            continue
        bundle = parts[:index + 1]
        if root is not None and bundle != root:
            raise UsbError("Unexpected archive layout: expected one outer .app bundle from Appium")
        root = bundle
        relative = parts[index:]
        name = "Payload/" + "/".join(relative) + ("/" if info.is_dir() else "")
        key = tuple(part.casefold() for part in relative)
        if key in paths:
            raise UsbError(f"Duplicate path in WebDriverAgent archive: {name!r}")
        paths[key] = info
        members[name] = info
    if root is None:
        raise UsbError("Unexpected archive layout: no .app bundle from Appium")
    for key in paths:
        for length in range(1, len(key)):
            parent = paths.get(key[:length])
            if parent is not None and not parent.is_dir():
                raise UsbError(f"File used as a directory in WebDriverAgent archive: {parent.filename!r}")
    return "Payload/" + root[-1] + "/", members


def _validate_ipa(path, app, members):
    with zipfile.ZipFile(path) as ipa:
        if sorted(ipa.namelist()) != sorted(members):
            raise UsbError("IPA validation failed: unexpected bundle contents")
        bad_file = ipa.testzip()
        if bad_file is not None:
            raise UsbError(f"IPA validation failed: {bad_file!r} did not pass the ZIP integrity check")
        try:
            metadata = plistlib.loads(ipa.read(app + "Info.plist"))
        except (KeyError, ValueError, ExpatError):
            raise UsbError("IPA validation failed: missing or invalid application Info.plist") from None
        executable = metadata.get("CFBundleExecutable") if isinstance(metadata, dict) else None
        identifier = metadata.get("CFBundleIdentifier") if isinstance(metadata, dict) else None
        if not isinstance(identifier, str) or not identifier or not isinstance(executable, str) or not executable:
            raise UsbError("IPA validation failed: Info.plist must identify the app and its executable")
        if len(_zip_path(executable)) != 1 or app + executable not in members or members[app + executable].is_dir():
            raise UsbError("IPA validation failed: the executable from Info.plist is missing or invalid")


def _download_wda(url, progress):
    sources = (WDA_RELEASE_ASSET_URL, url) if url == WDA_RELEASE_URL else (url,)
    pinned = url in (WDA_RELEASE_URL, WDA_RELEASE_ASSET_URL)
    errors = []
    for source in sources:
        if errors:
            progress("Trying the alternate GitHub download for the pinned release...")
        progress(f"Downloading {source}")
        headers = {"User-Agent": "icast-windows"}
        if source == WDA_RELEASE_ASSET_URL:
            headers["Accept"] = "application/octet-stream"
        req = urllib.request.Request(source, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=25 if pinned else 120) as response:
                data = response.read()
        except urllib.error.HTTPError as e:
            errors.append(f"HTTP {e.code}")
            e.close()
        except urllib.error.URLError as e:
            reason = describe_error(e.reason) if isinstance(e.reason, OSError) else str(e.reason)
            errors.append(reason)
        except (OSError, HTTPException) as e:
            errors.append(describe_error(e))
        else:
            if pinned and hashlib.sha256(data).hexdigest() != WDA_RELEASE_SHA256:
                errors.append("SHA-256 mismatch for the pinned release. Retry or update iCast.")
                continue
            return data
    raise UsbError("Download failed: " + " / ".join(errors))


def download_wda_ipa(dest: Path = None, progress=print, url: str = WDA_RELEASE_URL) -> Path:
    """Fetch Appium's unsigned real-device WDA build and repackage it as an .ipa
    (Payload/<name>.app/...), ready to be signed and installed with Sideloadly."""
    dest = Path(dest) if dest else Path.home() / "Downloads" / "WebDriverAgentRunner.ipa"
    data = _download_wda(url, progress)
    progress(f"Downloaded {len(data) / 1e6:.1f} MB, building the .ipa...")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as src:
            app, members = _ipa_members(src)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=dest.parent) as staging:
                tmp = Path(staging) / dest.name
                with zipfile.ZipFile(tmp, "w") as ipa:
                    for name, info in members.items():
                        content = src.read(info)
                        if info.is_dir() and content:
                            raise UsbError(f"Unexpected data in ZIP directory: {info.filename!r}")
                        if stat.S_ISLNK(info.external_attr >> 16):
                            # A link may point down into the bundle (e.g. Versions/Current).
                            # Never pass absolute or parent-relative links to the signing tool.
                            _zip_path(content.decode("utf-8"))
                        zi = zipfile.ZipInfo(name, info.date_time)
                        zi.create_system = info.create_system
                        zi.external_attr = info.external_attr
                        zi.internal_attr = info.internal_attr
                        zi.comment = info.comment
                        zi.compress_type = zipfile.ZIP_STORED if info.is_dir() else zipfile.ZIP_DEFLATED
                        ipa.writestr(zi, content)
                progress("Checking the IPA layout and ZIP integrity...")
                _validate_ipa(tmp, app, members)
                os.replace(tmp, dest)
    except (zipfile.BadZipFile, RuntimeError, UnicodeError, zlib.error) as e:
        raise UsbError(f"Could not package the WebDriverAgent ZIP: {e}. Download it again or update iCast.") from None
    return dest
