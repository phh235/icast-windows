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
                self.data.update(json.loads(path.read_text(encoding="utf-8")))
            except Exception as e:  # corrupt file: keep defaults, rewrite
                log.warning("%s unreadable (%s), using defaults", path.name, e)
        changed = False
        if not self.data.get("name"):
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

    @property
    def mac_bytes(self) -> bytes:
        return bytes.fromhex(self.data["mac"])

    @property
    def device_id(self) -> str:  # aa:bb:cc:dd:ee:ff
        return ":".join(f"{b:02x}" for b in self.mac_bytes)

    @property
    def raop_prefix(self) -> str:  # AABBCCDDEEFF
        return self.mac_bytes.hex().upper()
