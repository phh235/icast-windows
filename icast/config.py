"""Persistent settings and identity (name, MAC, Ed25519 key) of the receiver."""
import json
import logging
import os
import secrets
import socket
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

APP_NAME = "iCast"
APP_DESCRIPTION = "Mirror and control your iPhone on Windows."
APP_ICON = Path(__file__).with_name("icon.ico")
WINDOWS_APP_ID = "Phh235.iCast"
APP_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_NAME
SETTINGS_FILE = APP_DIR / "settings.json"
LOCAL_SETTINGS_FILE = APP_DIR / "settings-local.json"
LOG_FILE = APP_DIR / "icast.log"

DEFAULTS = {
    # name shown on the iPhone in Control Center -> Screen Mirroring
    "name": "",
    # resolution requested from the iPhone; the height controls the stream size
    "width": 1920,
    "height": 1080,
    "fps": 60,
    "audio": True,
    "always_on_top": True,
    # window geometry (logical pixels); 0 = automatic
    "x": None,
    "y": None,
    "phone_height": 0,
    # identity, generated on first start
    "mac": "",
    "ed25519_seed": "",
}

# Leave headroom for arithmetic on window coordinates before passing them to Qt.
_INT_LIMIT = 1 << 30
_INT_MINIMUMS = {"width": 1, "height": 1, "fps": 1, "phone_height": 0,
                 "x": -_INT_LIMIT, "y": -_INT_LIMIT}


def _valid_hex(value, size):
    if not isinstance(value, str) or len(value) != size * 2:
        return False
    try:
        return len(bytes.fromhex(value)) == size
    except ValueError:
        return False


class Settings:
    def __init__(self, path: Path = SETTINGS_FILE, default_name: str = ""):
        self.path = path
        self.data = dict(DEFAULTS, name=default_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(saved, dict):
                    raise ValueError("expected a JSON object")
                self.data.update(saved)
            except (OSError, ValueError, RecursionError) as e:
                log.warning("%s unreadable (%s), using defaults", path.name, e)
        changed = self._normalize_values()
        name = self.data["name"]
        if not isinstance(name, str) or not name.strip():
            self.data["name"] = socket.gethostname() or "Windows PC"
            changed = True
        if not _valid_hex(self.data.get("mac"), 6):
            mac = bytearray(secrets.token_bytes(6))
            mac[0] = (mac[0] | 0x02) & 0xFE  # locally administered, unicast
            self.data["mac"] = mac.hex()
            changed = True
        if not _valid_hex(self.data.get("ed25519_seed"), 32):
            self.data["ed25519_seed"] = secrets.token_hex(32)
            changed = True
        if changed:
            self.save()

    def _normalize_values(self):
        changed = False
        for key, minimum in _INT_MINIMUMS.items():
            value = self.data[key]
            try:
                number = int(value) if not isinstance(value, bool) else None
            except (TypeError, ValueError, OverflowError):
                number = None
            if number is None or not minimum <= number < _INT_LIMIT:
                number = DEFAULTS[key]
            if type(value) is not type(number) or value != number:
                self.data[key] = number
                changed = True
        for key in ("audio", "always_on_top"):
            if not isinstance(self.data[key], bool):
                self.data[key] = DEFAULTS[key]
                changed = True
        return changed

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v

    def get(self, k, default=None):
        return self.data.get(k, default)

    def save(self):
        try:
            with tempfile.TemporaryDirectory(dir=self.path.parent) as staging:
                tmp = Path(staging) / self.path.name
                with tmp.open("w", encoding="utf-8") as file:
                    json.dump(self.data, file, indent=2, ensure_ascii=False)
                os.replace(tmp, self.path)
        except Exception as e:
            log.warning("could not save settings: %s", e)
            return False
        return True

    @property
    def mac_bytes(self) -> bytes:
        return bytes.fromhex(self.data["mac"])

    @property
    def device_id(self) -> str:  # aa:bb:cc:dd:ee:ff
        return ":".join(f"{b:02x}" for b in self.mac_bytes)

    @property
    def raop_prefix(self) -> str:  # AABBCCDDEEFF
        return self.mac_bytes.hex().upper()
