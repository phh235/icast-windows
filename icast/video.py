"""Screen-mirroring video: TCP stream parsing, AES-CTR decryption, H.264 decoding."""
import asyncio
import hashlib
import logging
import struct
import threading
import time
from collections import deque
from contextlib import suppress

import av
from av.video.reformatter import VideoReformatter
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import net

log = logging.getLogger(__name__)

START = b"\x00\x00\x00\x01"
MAX_PACKET_BYTES = 4 * 1024 * 1024
MAX_CONFIG_BYTES = 64 * 1024
MAX_FRAME_PIXELS = 4096 * 2304
MIRROR_CONNECT_TIMEOUT = 10
MIRROR_IDLE_TIMEOUT = 120


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

    MAX_PENDING = 240
    MAX_PENDING_BYTES = 12 * 1024 * 1024

    def __init__(self, on_frame):
        super().__init__(name="video-decoder", daemon=True)
        self.on_frame = on_frame
        self._pending = deque()
        self._pending_bytes = 0
        self.target = None  # (w, h) in device pixels, set by the UI
        self.last_frame = None  # full-resolution av.VideoFrame (for screenshots)
        self._reformatter = VideoReformatter()
        self._condition = threading.Condition()
        self._session = None
        self._last_activity = None
        self._stopping = False

    def submit(self, session, data: bytes):
        with self._condition:
            if session is None or session is not self._session:
                return
            if len(self._pending) >= self.MAX_PENDING or self._pending_bytes + len(data) > self.MAX_PENDING_BYTES:
                return
            self._pending.append(data)
            self._pending_bytes += len(data)
            self._condition.notify()

    def _set_session(self, session):
        self._session = session
        self._pending.clear()
        self._pending_bytes = 0
        self.last_frame = None
        self._last_activity = None

    def last_activity(self, session):
        with self._condition:
            return self._last_activity if session is self._session else None

    def reset(self):
        with self._condition:
            if not self._stopping:
                self._set_session(object())
                return self._session

    def end(self, session):
        with self._condition:
            if session is None or session is not self._session:
                return False
            self._set_session(None)
            return True

    def stop(self):
        with self._condition:
            self._stopping = True
            self._set_session(None)
            self._condition.notify()

    def _new_codec(self):
        c = av.CodecContext.create("h264", "r")
        try:
            c.thread_type = "SLICE"  # no frame-threading delay
        except Exception:
            pass
        c.options = {"flags": "low_delay", "max_pixels": str(MAX_FRAME_PIXELS)}
        return c

    def run(self):
        codec_session = None
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending or self._stopping)
                if self._stopping:
                    return
                session, data = self._session, self._pending.popleft()
                self._pending_bytes -= len(data)
            if session is not codec_session:
                codec = self._new_codec()
                codec_session = session
            try:
                frames = codec.decode(av.Packet(data))
            except av.error.FFmpegError as e:
                log.debug("decode error: %s", e)
                continue
            if not frames:
                continue
            frame = frames[-1]
            if frame.is_corrupt or not 0 < frame.width * frame.height <= MAX_FRAME_PIXELS:
                continue
            with self._condition:
                if session is not self._session:
                    continue
                self._last_activity = time.monotonic()
                if self._pending:
                    continue  # behind: skip conversion, keep decoding references
            try:
                self._deliver(session, frame)
            except Exception:
                log.exception("frame conversion failed")

    def _deliver(self, session, frame):
        if frame.width * frame.height > MAX_FRAME_PIXELS:
            return
        tw, th = self.target or (frame.width, frame.height)
        tw, th = max(16, int(tw)), max(16, int(th))
        # keep the source aspect if the UI has not adapted yet
        if abs(tw / th - frame.width / frame.height) > 0.02:
            th = max(16, round(tw * frame.height / frame.width))
        if tw > frame.width or th > frame.height:
            tw, th = frame.width, frame.height
        if tw * th > MAX_FRAME_PIXELS:
            return
        img = self._reformatter.reformat(frame, width=tw, height=th, format="bgra", interpolation="AREA")
        with self._condition:
            if session is self._session:
                self.last_frame = frame
                self.on_frame(img, frame.width, frame.height)


class MirrorStream:
    """Accepts the iPhone's mirror TCP connection and feeds the decoder."""

    def __init__(self, aeskey: bytes, stream_connection_id: int, decoder: VideoDecoder, events, *,
                 peer_ip: str, client_name=None, on_end=None):
        self.decryptor = mirror_cipher(aeskey, stream_connection_id).decryptor()
        self.decoder = decoder
        self.events = events  # callable(event: str, **info)
        self.peer_ip, self.client_name = peer_ip, client_name
        self.on_end = on_end
        self.server = None
        self._offer_timer = None
        self._offer_deadline = None
        self.revoked = threading.Event()
        self._sps_pps = None
        self._checked = False
        self._pause_activity = None
        self._task = None
        self._session = decoder.reset()
        self._closed = False

    async def start(self, offer_deadline=None) -> int:
        loop = asyncio.get_running_loop()
        if offer_deadline is not None and offer_deadline <= loop.time():
            raise ValueError("mirror offer expired; reconnect to start mirroring")
        self.server = await asyncio.start_server(self._handle, host=net.BIND_HOST, port=0)
        self._offer_deadline = offer_deadline if offer_deadline is not None else loop.time() + MIRROR_CONNECT_TIMEOUT
        self._offer_timer = loop.call_at(self._offer_deadline, self._expire_offer)
        port = self.server.sockets[0].getsockname()[1]
        log.info("mirror data port %d", port)
        return port

    async def stop(self):
        self._close_listener()
        self._end()
        if self._task:
            task = self._task
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _close_listener(self):
        self._closed = True
        if self._offer_timer:
            self._offer_timer.cancel()
            self._offer_timer = None
        if self.server:
            self.server.close()
            self.server = None

    def _expire_offer(self):
        last = self.last_activity
        if last is not None:
            remaining = MIRROR_IDLE_TIMEOUT - (time.monotonic() - last)
            if remaining > 0:
                self._offer_timer = asyncio.get_running_loop().call_later(remaining, self._expire_offer)
                return
        self._close_listener()
        self._end()
        if self._task:
            self._task.cancel()

    @property
    def last_activity(self):
        last_frame = self.decoder.last_activity(self._session)
        if last_frame is not None and self._pause_activity is not None:
            return max(last_frame, self._pause_activity)
        return last_frame

    @property
    def pending_deadline(self):
        return self._offer_deadline if self.last_activity is None else None

    def _end(self):
        self.revoked.set()
        self._offer_deadline = self.pending_deadline
        session, self._session = self._session, None
        if session is not None:
            if self.decoder.end(session):
                self.events("mirror_stopped")
            if self.on_end:
                self.on_end(self)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        if self._closed or self._task is not None or not peer or peer[0] != self.peer_ip:
            writer.close()
            return
        self._task = asyncio.current_task()
        if self._session is None:
            self._session = self.decoder.reset()
        self.events("connecting", name=self.client_name or "iPhone", ip=self.peer_ip, revoked=self.revoked)
        log.info("mirror stream connected from %s", peer)
        sock = writer.get_extra_info("socket")
        try:
            import socket as _s
            sock.setsockopt(_s.SOL_SOCKET, _s.SO_RCVBUF, 4 * 1024 * 1024)
        except Exception:
            pass
        try:
            header_timeout = MIRROR_CONNECT_TIMEOUT
            while True:
                async with asyncio.timeout(header_timeout):
                    hdr = await reader.readexactly(128)
                header_timeout = MIRROR_IDLE_TIMEOUT
                size = struct.unpack_from("<I", hdr, 0)[0]
                if size > (MAX_PACKET_BYTES if hdr[4] == 0 else MAX_CONFIG_BYTES):
                    log.warning("oversized mirror packet")
                    return
                async with asyncio.timeout(10):
                    payload = await reader.readexactly(size) if size else b""
                self._packet(hdr, payload)
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError):
            log.info("mirror stream closed")
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("mirror stream error")
        finally:
            writer.close()
            try:
                self._close_listener()
                self._end()
                with suppress(OSError):
                    await writer.wait_closed()
            finally:
                self._task = None

    def _packet(self, hdr: bytes, payload: bytes):
        ptype = hdr[4]
        if ptype == 0:  # encrypted video access unit
            dec = self.decryptor.update(payload)
            nal = avcc_to_annexb(dec)
            if nal is None:
                if not self._checked:
                    self._checked = True
                    log.error("first video packet failed to decrypt into valid H.264 (key mismatch?)")
                    self.events("decrypt_error")
                return
            if not self._checked:
                self._checked = True
                log.info("video decryption OK")
            if nal:
                self._pause_activity = None
            if self._sps_pps:
                nal = self._sps_pps + nal
                self._sps_pps = None
            self.decoder.submit(self._session, nal)
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
                self._pause_activity = time.monotonic()
                self.events("paused")
            else:
                self._pause_activity = None
                self.events("format", width=int(w), height=int(h))
        elif hdr[4] in (2, 5) and self._pause_activity is not None:
            # A locked phone can send heartbeats instead of frames until it is unlocked.
            self._pause_activity = time.monotonic()
