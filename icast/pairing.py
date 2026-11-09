"""AirPlay legacy pairing (pair-setup / pair-verify) as used for screen mirroring."""
import hashlib
import logging

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

log = logging.getLogger(__name__)

_RAW = serialization.Encoding.Raw
_RAWPUB = serialization.PublicFormat.Raw


def _pub_bytes(pub) -> bytes:
    return pub.public_bytes(_RAW, _RAWPUB)


class ServerIdentity:
    """Long-lived Ed25519 key of the receiver (advertised as "pk")."""

    def __init__(self, seed_hex: str):
        self.private = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed_hex))
        self.public = _pub_bytes(self.private.public_key())


class PairingSession:
    """Per-connection state of the pair-verify handshake."""

    def __init__(self, identity: ServerIdentity):
        self.identity = identity
        self.ecdh_secret = None
        self.verified = False
        self._our_ecdh_pub = None
        self._their_ecdh = None
        self._their_ed = None
        self._key = self._iv = None

    def pair_setup(self, data: bytes) -> bytes:
        if len(data) != 32:
            raise ValueError("invalid pair-setup data")
        return self.identity.public

    def pair_verify(self, data: bytes):
        """Returns response body (bytes) or b"" for the 2nd step; raises on error."""
        if len(data) < 4:
            raise ValueError("invalid pair-verify data")
        if data[0] == 1:
            if len(data) != 4 + 32 + 32:
                raise ValueError("invalid pair-verify step 1")
            self._their_ecdh = data[4:36]
            self._their_ed = data[36:68]
            ours = X25519PrivateKey.generate()
            self._our_ecdh_pub = _pub_bytes(ours.public_key())
            self.ecdh_secret = ours.exchange(X25519PublicKey.from_public_bytes(self._their_ecdh))
            self._key = hashlib.sha512(b"Pair-Verify-AES-Key" + self.ecdh_secret).digest()[:16]
            self._iv = hashlib.sha512(b"Pair-Verify-AES-IV" + self.ecdh_secret).digest()[:16]
            signature = self.identity.private.sign(self._our_ecdh_pub + self._their_ecdh)
            enc = Cipher(algorithms.AES(self._key), modes.CTR(self._iv)).encryptor()
            return self._our_ecdh_pub + enc.update(signature)
        if data[0] == 0:
            if len(data) != 4 + 64 or self._key is None:
                raise ValueError("invalid pair-verify step 2")
            ctr = Cipher(algorithms.AES(self._key), modes.CTR(self._iv)).decryptor()
            ctr.update(bytes(64))  # keystream already used for our own signature
            signature = ctr.update(data[4:68])
            try:
                Ed25519PublicKey.from_public_bytes(self._their_ed).verify(
                    signature, self._their_ecdh + self._our_ecdh_pub
                )
                self.verified = True
            except InvalidSignature:
                log.warning("pair-verify: client signature did not verify")
                raise
            return b""
        raise ValueError(f"unknown pair-verify flag {data[0]}")
