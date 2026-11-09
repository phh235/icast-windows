"""Bonjour / mDNS advertisement of the receiver (_airplay._tcp and _raop._tcp)."""
import logging
import socket

from zeroconf import IPVersion, ServiceInfo, Zeroconf

log = logging.getLogger(__name__)

MODEL = "AppleTV3,2"
SRCVERS = "220.68"
FEATURES = 0x5A7FFEE6  # legacy pairing on, no HomeKit pairing, H.264 mirroring
FEATURES_STR = f"0x{FEATURES & 0xFFFFFFFF:X},0x{FEATURES >> 32:X}"
PI = "2e388006-13ba-4041-9a67-25dd4a43d536"


def airplay_txt(settings, pk_hex: str) -> dict:
    return {
        "deviceid": settings.device_id,
        "features": FEATURES_STR,
        "flags": "0x4",
        "model": MODEL,
        "pk": pk_hex,
        "pi": PI,
        "srcvers": SRCVERS,
        "vv": "2",
        "pw": "false",
    }


def raop_txt(pk_hex: str) -> dict:
    return {
        "ch": "2",
        "cn": "0,1,2,3",
        "da": "true",
        "et": "0,3,5",
        "vv": "2",
        "ft": FEATURES_STR,
        "am": MODEL,
        "md": "0,1,2",
        "rhd": "5.6.0.0",
        "pw": "false",
        "sf": "0x4",
        "sr": "44100",
        "ss": "16",
        "sv": "false",
        "tp": "UDP",
        "txtvers": "1",
        "vs": SRCVERS,
        "vn": "65537",
        "pk": pk_hex,
    }


def txt_bytes(txt: dict) -> bytes:
    """DNS TXT rdata encoding (used in GET /info "txtAirPlay")."""
    out = bytearray()
    for k, v in txt.items():
        item = f"{k}={v}".encode()
        out.append(len(item))
        out += item
    return bytes(out)


def local_ipv4_addresses():
    addrs = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except socket.gaierror:
        pass
    try:  # address of the default route
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            addrs.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(a for a in addrs if not a.startswith(("127.", "169.254.")))


class Advertiser:
    def __init__(self, settings, pk_hex: str, port: int):
        self.settings = settings
        self.pk_hex = pk_hex
        self.port = port
        self.zc = None
        self.infos = []
        self.addresses = []

    def start(self):
        self.addresses = local_ipv4_addresses()
        name = self.settings["name"]
        host = socket.gethostname().split(".")[0] + ".local."
        self.zc = Zeroconf(ip_version=IPVersion.V4Only)
        self.infos = [
            ServiceInfo(
                "_airplay._tcp.local.",
                f"{name}._airplay._tcp.local.",
                port=self.port,
                properties=airplay_txt(self.settings, self.pk_hex),
                server=host,
                parsed_addresses=self.addresses or None,
            ),
            ServiceInfo(
                "_raop._tcp.local.",
                f"{self.settings.raop_prefix}@{name}._raop._tcp.local.",
                port=self.port,
                properties=raop_txt(self.pk_hex),
                server=host,
                parsed_addresses=self.addresses or None,
            ),
        ]
        for info in self.infos:
            self.zc.register_service(info, allow_name_change=True)
        log.info("advertised '%s' on %s port %d", name, ", ".join(self.addresses) or "?", self.port)

    def stop(self):
        if self.zc:
            try:
                for info in self.infos:
                    self.zc.unregister_service(info)
            finally:
                self.zc.close()
                self.zc = None
