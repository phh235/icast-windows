"""Screen-mirroring video: TCP stream parsing, AES-CTR decryption, H.264 decoding."""
import asyncio
import hashlib
import logging
import queue
import struct
import threading
import time

import av
from av.video.reformatter import VideoReformatter
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import net

log = logging.getLogger(__name__)

START = b"\x00\x00\x00\x01"


def mirror_cipher(aeskey: bytes, stream_connection_id: int):
    """AES-128-CTR decryptor for the mirror stream (key/iv derived like the receiver)."""
    skey = f"AirPlayStreamKey{stream_connection_id}".encode()
    siv = f"AirPlayStreamIV{stream_connection_id}".encode()
    key = hashlib.sha512(skey + aeskey).digest()[:16]
    iv = hashlib.sha512(siv + aeskey).digest()[:16]
    return Cipher(algorithms.AES(key), modes.CTR(iv))


def avcc_to_annexb(data: bytes):
    """4-byte big-endian length prefixed NALs -> start-code NALs. None if malformed."""
    out = bytearray()
    pos, n = 0, len(data)
    while pos < n:
        if pos + 4 > n:
            return None
        size = int.from_bytes(data[pos:pos + 4], "big")
        if size <= 0 or pos + 4 + size > n or data[pos + 4] & 0x80:
            return None
        out += START
        out += data[pos + 4:pos + 4 + size]
        pos += 4 + size
    return bytes(out)


class VideoDecoder(threading.Thread):
    """Decodes Annex-B access units; hands the newest BGRA VideoFrame to on_frame."""

    def __init__(self, on_frame):
        super().__init__(name="video-decoder", daemon=True)
        self.on_frame = on_frame
        self.q = queue.Queue(maxsize=240)
        self.target = None  # (w, h) in device pixels, set by the UI
        self.last_frame = None  # full-resolution av.VideoFrame (for screenshots)
        self._codec = None
        self._reformatter = VideoReformatter()
        self._stop = False

    def submit(self, data: bytes):
        try:
            self.q.put_nowait(data)
        except queue.Full:
            log.warning("decoder queue full, dropping frame")

    def reset(self):
        self.submit(None)

    def stop(self):
        self._stop = True
        self.submit(b"")

    def _new_codec(self):
        c = av.CodecContext.create("h264", "r")
        try:
            c.thread_type = "SLICE"  # no frame-threading delay
        except Exception:
            pass
        c.options = {"flags": "low_delay"}
        return c

    def run(self):
        while not self._stop:
            data = self.q.get()
            if self._stop:
                break
            if data is None:
                self._codec = None
                continue
            if self._codec is None:
                self._codec = self._new_codec()
            try:
                frames = self._codec.decode(av.Packet(data))
            except av.error.FFmpegError as e:
                log.debug("decode error: %s", e)
                continue
            if not frames or not self.q.empty():
                continue  # behind: skip conversion, keep decoding references
            frame = frames[-1]
            try:
                self._deliver(frame)
            except Exception:
                log.exception("frame conversion failed")

    def _deliver(self, frame):
        self.last_frame = frame
        tw, th = self.target or (frame.width, frame.height)
        tw, th = max(16, int(tw)), max(16, int(th))
        # keep the source aspect if the UI has not adapted yet
        if abs(tw / th - frame.width / frame.height) > 0.02:
            th = max(16, round(tw * frame.height / frame.width))
        img = self._reformatter.reformat(frame, width=tw, height=th, format="bgra", interpolation="AREA")
        self.on_frame(img, frame.width, frame.height)


class MirrorStream:
    """Accepts the iPhone's mirror TCP connection and feeds the decoder."""

    def __init__(self, aeskey: bytes, stream_connection_id: int, decoder: VideoDecoder, events):
        self.decryptor = mirror_cipher(aeskey, stream_connection_id).decryptor()
        self.decoder = decoder
        self.events = events  # callable(event: str, **info)
        self.server = None
        self.port = 0
        self._sps_pps = None
        self._checked = False
        self._bad = 0
        self._task = None
        self.frames = 0

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, host=net.BIND_HOST, port=0)
        self.port = self.server.sockets[0].getsockname()[1]
        log.info("mirror data port %d", self.port)
        return self.port

    async def stop(self):
        if self.server:
            self.server.close()
            self.server = None
        if self._task:
            self._task.cancel()
            self._task = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._task = asyncio.current_task()
        peer = writer.get_extra_info("peername")
        log.info("mirror stream connected from %s", peer)
        sock = writer.get_extra_info("socket")
        try:
            import socket as _s
            sock.setsockopt(_s.SOL_SOCKET, _s.SO_RCVBUF, 4 * 1024 * 1024)
        except Exception:
            pass
        self.decoder.reset()
        try:
            while True:
                hdr = await reader.readexactly(128)
                size = struct.unpack_from("<I", hdr, 0)[0]
                payload = await reader.readexactly(size) if size else b""
                self._packet(hdr, payload)
        except (asyncio.IncompleteReadError, ConnectionError):
            log.info("mirror stream closed")
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("mirror stream error")
        finally:
            writer.close()
            self.events("mirror_stopped")

    def _packet(self, hdr: bytes, payload: bytes):
        ptype = hdr[4]
        if ptype == 0:  # encrypted video access unit
            dec = self.decryptor.update(payload)
            nal = avcc_to_annexb(dec)
            if nal is None:
                self._bad += 1
                if not self._checked:
                    self._checked = True
                    log.error("first video packet failed to decrypt into valid H.264 (key mismatch?)")
                    self.events("decrypt_error")
                return
            if not self._checked:
                self._checked = True
                log.info("video decryption OK")
            if self._sps_pps:
                nal = self._sps_pps + nal
                self._sps_pps = None
            self.frames += 1
            self.decoder.submit(nal)
        elif ptype == 1:  # unencrypted codec config (SPS + PPS)
            if payload[4:8] == b"hvc1":
                log.error("client sent H.265, which is not supported")
                return
            sps_size = struct.unpack_from(">H", payload, 6)[0]
            sps = payload[8:8 + sps_size]
            pps_size = struct.unpack_from(">H", payload, sps_size + 9)[0]
            pps = payload[sps_size + 11:sps_size + 11 + pps_size]
            self._sps_pps = START + sps + START + pps
            w, h = struct.unpack_from("<ff", hdr, 56)
            sw, sh = struct.unpack_from("<ff", hdr, 40)
            log.info("codec config: video %dx%d (source %dx%d) option %02x", w, h, sw, sh, hdr[6])
            if hdr[6] in (0x56, 0x5E):
                self.events("paused")
            else:
                self.events("format", width=int(w), height=int(h))
        # 2 = heartbeat (old protocol), 5 = streaming report: nothing to do
