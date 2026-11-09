"""FairPlay (fp-setup) handling for the AirPlay receiver."""
from . import _tables as T
from .playfair import playfair_decrypt


class FairPlay:
    def __init__(self):
        self.keymsg = None

    def setup(self, req: bytes) -> bytes:
        """First /fp-setup (16 bytes) -> 142-byte canned reply for the requested mode."""
        if len(req) != 16 or req[4] != 0x03:
            raise ValueError("unsupported FairPlay version")
        mode = req[14]
        if mode > 3:
            raise ValueError(f"unsupported FairPlay mode {mode}")
        return T.REPLY_MESSAGE[mode * 142:(mode + 1) * 142]

    def handshake(self, req: bytes) -> bytes:
        """Second /fp-setup (164 bytes) -> 32-byte reply; remembers the key message."""
        if len(req) != 164 or req[4] != 0x03:
            raise ValueError("bad FairPlay handshake")
        self.keymsg = bytes(req)
        return T.FP_HEADER + req[144:164]

    def decrypt(self, ekey: bytes) -> bytes:
        if self.keymsg is None:
            raise RuntimeError("FairPlay handshake not done")
        return playfair_decrypt(self.keymsg, ekey)
