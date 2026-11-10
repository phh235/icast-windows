"""USB control tools: python -m icast.control <command>

  doctor            check everything USB control needs, with advice for the first problem
  get-wda           download WebDriverAgent and build WebDriverAgentRunner.ipa for Sideloadly
  reveal-dev-mode   show the Developer Mode switch in the iPhone's Settings
  start             start WebDriverAgent over USB and keep it running until Ctrl+C
  tap X Y | home | type TEXT
                    debug commands against a running WebDriverAgent (X/Y in points)
"""
import argparse
import importlib
import logging
import queue
import sys
from pathlib import Path

from ..config import APP_NAME
from . import ERROR, OFF, READY, STARTING, Controller
from . import usb
from .wda import WDA, WDAError, describe_error

DEFAULT_URL = f"http://127.0.0.1:{usb.WDA_PORT}"
MODULE = "python -m icast.control"

ADVICE_USBMUXD = (
    "Install \"Apple Devices\" from the Microsoft Store (or iTunes from apple.com).\n"
    "If it is installed already, open it once with the iPhone connected, or restart Windows.\n"
    "Then connect the iPhone with a cable, unlock it and tap \"Trust\"."
)
ADVICE_NOT_RUNNING = f"Normal until control is started: {MODULE} start (or turn on control in the app)."
ADVICE_NO_WDA_URL = f"Is WebDriverAgent running? Start it with: {MODULE} start"


# ---------------------------------------------------------------------- output
def say(text=""):
    print(text, flush=True)


def _block(tag, msg, advice=None):
    say(f"{tag:<7}{msg}")
    for line in (advice or "").strip().splitlines():
        say(f"       {line}")


def ok(msg, advice=None):
    _block("[OK]", msg, advice)


def fail(msg, advice=None):
    _block("[FAIL]", msg, advice)


def warn(msg, advice=None):
    _block("[WARN]", msg, advice)


def note(msg, advice=None):
    _block("[--]", msg, advice)


def _device_line(dev):
    name = dev.get("DeviceName") or "iPhone"
    model = dev.get("ProductType") or "?"
    return f"{name} ({model}), iOS {dev.get('ProductVersion') or '?'}, UDID {dev.get('Identifier') or '?'}"


# ---------------------------------------------------------------------- doctor
def _check_usb():
    """The USB chain; stops at the first failure. Returns True if every check passed."""
    try:
        importlib.import_module("pymobiledevice3")
        from importlib.metadata import version

        ok(f"pymobiledevice3 {version('pymobiledevice3')} is installed")
    except Exception as e:
        fail(f"pymobiledevice3 cannot be imported ({type(e).__name__})", usb.MSG_NO_PMD3)
        return False

    addr = usb.usbmuxd_address()
    where = f"{addr[0]}:{addr[1]}" if isinstance(addr, tuple) else addr
    if not usb.usbmuxd_reachable():
        fail(f"Apple's USB service (usbmuxd) is not reachable at {where}", ADVICE_USBMUXD)
        return False
    ok(f"Apple's USB service (usbmuxd) is running at {where}")

    try:
        devices = usb.list_devices()
    except usb.UsbError as e:
        fail("Could not read the iPhone over USB", str(e))
        return False
    for dev in devices:
        ok(f"iPhone on USB: {_device_line(dev)}")
    try:
        dev, udid = usb.select_device(devices)
    except usb.UsbError as e:
        fail("Could not select an iPhone over USB", str(e))
        return False
    ios = usb.parse_version(dev.get("ProductVersion"))

    if ios[0] >= 16:
        try:
            enabled = usb.developer_mode_enabled(udid)
        except usb.UsbError as e:
            fail("Could not read the Developer Mode status", str(e))
            return False
        if not enabled:
            fail("Developer Mode is off", usb.MSG_DEV_MODE)
            return False
        ok("Developer Mode is on")
    elif ios[0]:
        ok(f"Developer Mode is not needed on iOS {ios[0]}")
    if (17, 0) <= ios < (17, 4):
        warn(f"iOS {ios[0]}.{ios[1]}: no admin-free tunnel on this version", usb.MSG_IOS_17_EARLY)

    try:
        bundle = usb.find_wda_bundle(udid)
    except usb.UsbError as e:
        fail("Could not list the apps on the iPhone", str(e))
        return False
    if not bundle:
        fail("WebDriverAgent is not installed on the iPhone", usb.MSG_NO_WDA)
        return False
    ok(f"WebDriverAgent is installed ({bundle})")
    note("The Developer Disk Image is mounted automatically by 'start' (needs Internet the first time).")
    return True


def _check_wda(url):
    status, ready = usb.probe_wda(url)
    if not status:
        note(f"WebDriverAgent is not answering at {url}", ADVICE_NOT_RUNNING)
        return False
    if not ready:
        warn(f"WebDriverAgent at {url} reports it is unavailable for control")
        return False
    ip = (status.get("ios") or {}).get("ip") if isinstance(status.get("ios"), dict) else None
    ok(f"WebDriverAgent answers at {url}" + (f" (iPhone Wi-Fi address {ip})" if ip else ""))
    return True


def cmd_doctor(args):
    say(f"{APP_NAME}: checking USB control")
    say()
    usb_ok = _check_usb()
    urls = [DEFAULT_URL]
    if args.ip:
        ip = args.ip if ":" in args.ip else f"{args.ip}:{usb.WDA_PORT}"
        urls.append(ip if ip.startswith("http://") else f"http://{ip}")
    wda_ok = [_check_wda(u) for u in urls]
    say()
    if usb_ok:
        say("Result: USB control is ready. Turn on control in the app, or run: " + f"{MODULE} start")
        return 0
    if any(wda_ok):
        say("Result: the USB setup has a problem (see [FAIL] above), but WebDriverAgent is reachable,")
        say("        so control works while it keeps running.")
    else:
        say("Result: fix the problem marked [FAIL] above, then run the doctor again.")
    return 1


# ---------------------------------------------------------------------- setup helpers
def cmd_get_wda(args):
    dest = Path(args.out).expanduser() if args.out else None
    try:
        path = usb.download_wda_ipa(dest, progress=lambda m: say(f"       {m}"))
    except usb.UsbError as e:
        fail("Could not build WebDriverAgentRunner.ipa", str(e))
        return 1
    except OSError as e:
        fail("Could not save WebDriverAgentRunner.ipa", f"{e.filename or ''} {describe_error(e)}")
        return 1
    ok(f"Validated IPA saved as {path}")
    say()
    say("Next steps:")
    say("  1. Install Sideloadly (https://sideloadly.io) on this PC.")
    say("  2. Connect the iPhone with a cable, open Sideloadly, drop the .ipa into it,")
    say("     enter your Apple ID and click Start. A free Apple ID works, but then the")
    say("     app has to be installed again every 7 days.")
    say("  3. On the iPhone: Settings > General > VPN & Device Management > your Apple ID > Trust.")
    say(f"  4. iOS 16 and newer: turn on Developer Mode ({MODULE} reveal-dev-mode).")
    say(f"  5. Check everything: {MODULE} doctor")
    return 0


def cmd_reveal_dev_mode(args):
    try:
        dev, udid = usb.select_device(usb.list_devices())
    except usb.UsbError as e:
        fail("Could not select an iPhone over USB", str(e))
        return 1
    ios = usb.parse_version(dev.get("ProductVersion"))
    ok(f"iPhone on USB: {_device_line(dev)}")
    if ios[0] and ios[0] < 16:
        ok(f"iOS {ios[0]} has no Developer Mode switch; nothing to do.")
        return 0
    try:
        if usb.developer_mode_enabled(udid):
            ok("Developer Mode is already on.")
            return 0
    except usb.UsbError:
        pass  # the reveal below reports the actual problem
    try:
        code, out, err = usb.pmd3("amfi", "reveal-developer-mode", "--udid", udid, timeout=60)
    except usb.UsbError as e:
        fail("Could not reveal the Developer Mode switch", str(e))
        return 1
    if code:
        fail("Could not reveal the Developer Mode switch", usb.failure_text(err or out, "pymobiledevice3 said"))
        return 1
    ok("The Developer Mode switch is now visible on the iPhone.")
    say()
    say("On the iPhone:")
    say("  1. Open Settings > Privacy & Security > Developer Mode (near the bottom).")
    say("  2. Turn it on and tap Restart.")
    say("  3. After the restart unlock the iPhone, tap Turn On and enter the passcode.")
    say(f"Then check again: {MODULE} doctor")
    return 0


# ---------------------------------------------------------------------- start
def cmd_start(args):
    events = queue.Queue()
    ctrl = Controller(on_state=lambda state, message: events.put((state, message)))
    if args.ip:
        ctrl.device_ip = args.ip
    say("Starting control. Press Ctrl+C to stop.")
    code = 0
    ctrl.start()
    try:
        while True:
            try:
                state, message = events.get(timeout=0.5)  # short timeout: stay responsive to Ctrl+C
            except queue.Empty:
                continue
            if state == STARTING:
                _block("[..]", message)
            elif state == READY:
                ok(message)
                wda = ctrl.wda
                if wda is not None:
                    say(f"       WebDriverAgent: {wda.base}")
                say("       Running. Press Ctrl+C to stop.")
            elif state == ERROR:
                fail("Control stopped", message)
                code = 1
                break
            elif state == OFF:
                break
    except KeyboardInterrupt:
        say()
        say("Stopping...")
    finally:
        ctrl.stop()
    if code == 0:
        ok("Stopped; the USB helper processes have exited.")
    return code


# ---------------------------------------------------------------------- debug commands
def _with_wda(args, action, done):
    try:
        with WDA(args.url) as wda:
            action(wda)
    except ValueError as e:
        fail(str(e))
        return 1
    except WDAError as e:
        fail(f"WebDriverAgent at {args.url}: {e}", ADVICE_NO_WDA_URL)
        return 1
    ok(done)
    return 0


def cmd_tap(args):
    return _with_wda(args, lambda w: w.tap(args.x, args.y), f"Tapped ({args.x:g}, {args.y:g})")


def cmd_home(args):
    return _with_wda(args, lambda w: w.home(), "Pressed Home")


def cmd_type(args):
    text = args.text.replace("\\n", "\n").replace("\\b", "\b")
    return _with_wda(args, lambda w: w.type_text(text), f"Typed {len(text)} character(s)")


# ---------------------------------------------------------------------- main
def build_parser():
    p = argparse.ArgumentParser(
        prog=MODULE,
        description="Set up and test control of the iPhone (mouse and keyboard) over USB.",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="show log messages")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    s = sub.add_parser("doctor", help="check everything USB control needs")
    s.add_argument("--ip", help="also check WebDriverAgent over Wi-Fi at this iPhone address (IP[:PORT])")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("get-wda", help="download WebDriverAgent and build an .ipa for Sideloadly")
    s.add_argument("--out", metavar="PATH", help="where to save the .ipa (default: ~/Downloads/WebDriverAgentRunner.ipa)")
    s.set_defaults(func=cmd_get_wda)

    s = sub.add_parser("reveal-dev-mode", help="show the Developer Mode switch in the iPhone's Settings")
    s.set_defaults(func=cmd_reveal_dev_mode)

    s = sub.add_parser("start", help="start WebDriverAgent over USB and keep it running until Ctrl+C")
    s.add_argument("--ip", help="iPhone Wi-Fi address: use WebDriverAgent there if it is already running")
    s.set_defaults(func=cmd_start)

    for name, func, helptext in (
        ("tap", cmd_tap, "tap at X Y (points) on a running WebDriverAgent"),
        ("home", cmd_home, "go to the Home Screen"),
        ("type", cmd_type, "type TEXT into the focused field (\\n = Return, \\b = Backspace)"),
    ):
        s = sub.add_parser(name, help=helptext)
        if name == "tap":
            s.add_argument("x", type=float, help="X in points")
            s.add_argument("y", type=float, help="Y in points")
        elif name == "type":
            s.add_argument("text", help="text to type")
        s.add_argument("--url", default=DEFAULT_URL, help=f"WebDriverAgent URL (default: {DEFAULT_URL})")
        s.set_defaults(func=func)
    return p


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:  # never crash on a device name the console cannot show
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.ERROR,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except KeyboardInterrupt:
        say()
        return 130


if __name__ == "__main__":
    sys.exit(main())
