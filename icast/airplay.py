"""AirPlay screen-mirroring receiver: RTSP control connection and session setup."""
import asyncio
import hashlib
import logging
import math
import plistlib
import struct
import threading
import time
from collections import Counter
from concurrent.futures import Future, InvalidStateError
from contextlib import suppress

from . import discovery, net
from .audio import AudioStream, Player
from .fairplay import FairPlay
from .pairing import PairingSession, ServerIdentity
from .video import MirrorStream, VideoDecoder

log = logging.getLogger(__name__)

SERVER = f"AirTunes/{discovery.SRCVERS}"
NTP_EPOCH = 2208988800
RTSP_CLOSE_TIMEOUT = 0.5
RTSP_HEADER_TIMEOUT = 15
RTSP_MEDIA_IDLE_TIMEOUT = 120
RTSP_BODY_LIMIT = 64 * 1024
RTSP_CONNECTION_LIMIT = 8
PLIST_PARSE_BUDGET = 4 * 1024 * 1024


def _read_plist(data):
    if data.startswith(b"bplist00"):
        if len(data) < 40:
            raise ValueError("truncated binary plist")
        offset_size, ref_size, count, root, table = struct.unpack(">6xBBQQQ", data[-32:])
        # Bound work even if distinct offsets describe overlapping encoded objects.
        if (not 1 <= offset_size <= 8 or not 1 <= ref_size <= 8 or not 0 <= root < count
                or not 8 <= table <= len(data) - 32 or table + count * offset_size > len(data) - 32
                or count > 512 or count * len(data) > PLIST_PARSE_BUDGET):
            raise ValueError("binary plist exceeds the parsing budget")
        offsets = {int.from_bytes(data[i:i + offset_size], "big")
                   for i in range(table, table + count * offset_size, offset_size)}
        if len(offsets) != count or any(not 8 <= offset < table for offset in offsets):
            raise ValueError("invalid binary plist offsets")
    return plistlib.loads(data)


def _bplist(obj) -> bytes:
    return plistlib.dumps(obj, fmt=plistlib.FMT_BINARY, sort_keys=False)


class Request:
    def __init__(self, method, url, protocol, headers, body):
        self.method, self.url, self.protocol = method, url, protocol
        self.headers = headers  # lower-case keys
        self.body = body

    def header(self, name, default=None):
        return self.headers.get(name.lower(), default)


class Response:
    def __init__(self, status=200, reason="OK"):
        self.status, self.reason = status, reason
        self.headers = []
        self.body = b""
        self.close = False

    def set(self, body: bytes, content_type: str):
        self.body = body
        self.headers.append(("Content-Type", content_type))


class _TimingProto(asyncio.DatagramProtocol):
    def datagram_received(self, data, addr):
        pass  # replies are not needed: we render as soon as frames arrive


class TimingSender:
    """Sends NTP timing requests to the client like an Apple TV does."""

    def __init__(self):
        self.transport = None
        self.task = None
        self.port = 0

    async def start(self, remote_ip, remote_port):
        loop = asyncio.get_running_loop()
        self.transport, _ = await loop.create_datagram_endpoint(_TimingProto, local_addr=(net.BIND_HOST, 0))
        self.port = self.transport.get_extra_info("sockname")[1]
        if remote_port:
            self.task = asyncio.create_task(self._loop((remote_ip, remote_port)))
        return self.port

    async def _loop(self, addr):
        try:
            while True:
                now = time.time() + NTP_EPOCH
                sec = int(now)
                frac = int((now - sec) * (1 << 32)) & 0xFFFFFFFF
                pkt = bytes([0x80, 0xD2, 0x00, 0x07]) + bytes(20) + struct.pack(">II", sec, frac)
                self.transport.sendto(pkt, addr)
                await asyncio.sleep(3)
        except asyncio.CancelledError:
            pass

    def stop(self):
        if self.task:
            self.task.cancel()
        if self.transport:
            self.transport.close()


class Connection:
    def __init__(self, receiver, reader, writer):
        self.rx = receiver
        self.reader, self.writer = reader, writer
        peer = writer.get_extra_info("peername") or ("?", 0)
        self.remote_ip = peer[0]
        self.pairing = PairingSession(receiver.identity)
        self.fairplay = FairPlay()
        self.aeskey = None
        self.aesiv = None
        self.timing = None
        self.mirror = None
        self.audio = None
        # Stream-only TEARDOWN and SETUP retries retain the original deadline until usable media arrives.
        self._mirror_offer_deadline = None
        self._audio_offer_deadline = None
        self.client_name = None

    # ---------------------------------------------------------------- I/O
    async def serve(self):
        log.info("connection from %s", self.remote_ip)
        try:
            while True:
                req = await self._read_request()
                if req is None:
                    break
                resp = await self._dispatch(req)
                await self._write(req, resp)
                if resp.close:
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except (ValueError, TimeoutError, asyncio.LimitOverrunError):
            log.warning("invalid or incomplete RTSP request from %s", self.remote_ip)
        except Exception:
            log.exception("connection error")
        finally:
            try:
                await self.cleanup()
            finally:
                self.writer.close()
                try:
                    # A peer that has stopped reading can hold buffered replies open.
                    async with asyncio.timeout(RTSP_CLOSE_TIMEOUT):
                        await self.writer.wait_closed()
                except OSError:
                    self.writer.transport.abort()
                except asyncio.CancelledError:
                    self.writer.transport.abort()
                    raise
            log.info("connection from %s closed", self.remote_ip)

    async def _read_request(self):
        # Quiet control sockets are normal during playback. A partial request still has its own deadline.
        timeout = RTSP_MEDIA_IDLE_TIMEOUT if self.mirror or self.audio else RTSP_HEADER_TIMEOUT
        try:
            while True:
                try:
                    async with asyncio.timeout(timeout):
                        first = await self.reader.readexactly(1)
                    break
                except TimeoutError:
                    last = max(
                        (self.mirror.last_activity or 0) if self.mirror else 0,
                        (self.audio.last_activity or 0) if self.audio else 0,
                    )
                    timeout = last + RTSP_MEDIA_IDLE_TIMEOUT - time.monotonic()
                    if not last or timeout <= 0:
                        raise
            async with asyncio.timeout(RTSP_HEADER_TIMEOUT):
                head = first + await self.reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError:
            return None
        lines = head.decode("utf-8", "replace").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3 or not lines[0].isprintable():
            return None
        method, url, protocol = parts[0], parts[1], parts[2]
        if protocol not in ("RTSP/1.0", "HTTP/1.1", "HTTP/1.0"):
            return None
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                key = k.strip().lower()
                if key == "content-length" and key in headers:
                    raise ValueError("duplicate RTSP Content-Length")
                headers[key] = v.strip()
        length = headers.get("content-length", "0")
        if len(length) > 10 or not length.isascii() or not length.isdecimal() or int(length) > RTSP_BODY_LIMIT:
            raise ValueError("invalid or oversized RTSP Content-Length")
        n = int(length)
        async with asyncio.timeout(10):
            body = await self.reader.readexactly(n) if n else b""
        return Request(method, url, protocol, headers, body)

    async def _write(self, req: Request, resp: Response):
        out = [f"{req.protocol} {resp.status} {resp.reason}"]
        cseq = req.header("CSeq")
        if cseq is not None and len(cseq) <= 12 and cseq.isascii() and cseq.isdecimal():
            out.append(f"CSeq: {cseq}")
        out.append(f"Server: {SERVER}")
        for k, v in resp.headers:
            out.append(f"{k}: {v}")
        out.append(f"Content-Length: {len(resp.body)}")
        self.writer.write(("\r\n".join(out) + "\r\n\r\n").encode() + resp.body)
        async with asyncio.timeout(10):
            await self.writer.drain()

    async def _dispatch(self, req: Request) -> Response:
        path = req.url.split("?")[0]
        if req.method == "POST" and path == "/feedback":
            log.debug("%r %r", req.method, req.url)
        else:
            log.info("%r %r (%d bytes)", req.method, req.url, len(req.body))
        resp = Response()
        try:
            if req.method == "GET" and path == "/info":
                self.on_info(req, resp)
            elif req.method == "POST" and path == "/pair-setup":
                resp.set(self.pairing.pair_setup(req.body), "application/octet-stream")
            elif req.method == "POST" and path == "/pair-verify":
                resp.set(self.pairing.pair_verify(req.body), "application/octet-stream")
            elif req.method == "POST" and path == "/fp-setup":
                self.on_fp_setup(req, resp)
            elif req.method == "OPTIONS":
                resp.headers.append(("Public", "SETUP, RECORD, FLUSH, TEARDOWN, OPTIONS, GET_PARAMETER, SET_PARAMETER"))
            elif req.method == "SETUP":
                await self.on_setup(req, resp)
            elif req.method == "GET_PARAMETER":
                self.on_get_parameter(req, resp)
            elif req.method == "SET_PARAMETER":
                self.on_set_parameter(req)
            elif req.method == "RECORD":
                resp.headers.append(("Audio-Latency", "11025"))
                resp.headers.append(("Audio-Jack-Status", "connected; type=analog"))
            elif req.method == "FLUSH":
                if self.audio:
                    self.audio.flush()
            elif req.method == "TEARDOWN":
                await self.on_teardown(req, resp)
            elif req.method == "POST" and path in ("/feedback", "/audioMode"):
                pass
            else:
                log.warning("unhandled request %s %s", req.method, req.url)
        except Exception as e:
            log.exception("error handling %r %r", req.method, req.url)
            resp = Response(500, "Internal Server Error")
            if isinstance(e, ValueError):
                resp = Response(400, "Bad Request")
        return resp

    # ---------------------------------------------------------------- handlers
    def on_info(self, req, resp):
        rx = self.rx
        ct = req.header("Content-Type") or ""
        res = {}
        if "application/x-apple-binary-plist" in ct and req.body:
            try:
                q = _read_plist(req.body).get("qualifier") or []
            except Exception:
                q = []
            if "txtAirPlay" in q:
                res["txtAirPlay"] = discovery.txt_bytes(discovery.airplay_txt(rx.settings, rx.pk_hex))
            if "txtRAOP" in q:
                res["txtRAOP"] = discovery.txt_bytes(discovery.raop_txt(rx.pk_hex))
        if not ct:
            s = rx.settings
            res.update(
                {
                    "deviceID": s.device_id,
                    "macAddress": s.device_id,
                    "pk": rx.identity.public,
                    "features": discovery.FEATURES,
                    "name": s["name"],
                    "pi": discovery.PI,
                    "vv": 2,
                    "statusFlags": 68,
                    "keepAliveLowPower": True,
                    "sourceVersion": discovery.SRCVERS,
                    "keepAliveSendStatsAsBody": True,
                    "model": discovery.MODEL,
                }
            )
            if req.header("CSeq") is not None:
                res.update(
                    {
                        "initialVolume": 0.0,
                        "audioLatencies": [
                            {"type": 100, "inputLatencyMicros": 0, "audioType": "default", "outputLatencyMicros": False},
                            {"type": 101, "audioType": "default", "inputLatencyMicros": 0, "outputLatencyMicros": False},
                        ],
                        "audioFormats": [
                            {"audioOutputFormats": 0x3FFFFFC, "type": 100, "audioInputFormats": 0x3FFFFFC},
                            {"audioOutputFormats": 0x3FFFFFC, "type": 101, "audioInputFormats": 0x3FFFFFC},
                        ],
                        "displays": [
                            {
                                "uuid": "e0ff8a27-6738-3d56-8a16-cc53aacee925",
                                "widthPhysical": 0,
                                "heightPhysical": 0,
                                "width": int(s["width"]),
                                "height": int(s["height"]),
                                "widthPixels": int(s["width"]),
                                "heightPixels": int(s["height"]),
                                "rotation": False,
                                "refreshRate": 1.0 / int(s["fps"]),
                                "maxFPS": int(s["fps"]),
                                "overscanned": False,
                                "features": 14,
                            }
                        ],
                    }
                )
        resp.set(_bplist(res), "application/x-apple-binary-plist")

    def on_fp_setup(self, req, resp):
        if len(req.body) == 16:
            if req.body[4] != 0x03:
                log.error("unsupported FairPlay version %d", req.body[4])
                resp.status, resp.reason = 501, "Not Implemented"
                return
            resp.set(self.fairplay.setup(req.body), "application/octet-stream")
        elif len(req.body) == 164:
            resp.set(self.fairplay.handshake(req.body), "application/octet-stream")
        else:
            raise ValueError("invalid fp-setup length")

    async def on_setup(self, req, resp):
        rx = self.rx
        plist = _read_plist(req.body) if req.body else {}
        if not isinstance(plist, dict):
            raise ValueError("invalid AirPlay setup")
        streams = plist.get("streams", [])
        if not isinstance(streams, list) or len(streams) > 4 or any(
            not isinstance(s, dict) or type(s.get("type")) is not int or not 0 <= s["type"] <= 255
            for s in streams
        ):
            raise ValueError("invalid AirPlay stream list")
        kinds = {s["type"] for s in streams}
        if len(kinds) != len(streams):
            raise ValueError("duplicate AirPlay stream type")
        for stream in streams:
            kind = stream["type"]
            if kind == 110:
                sid = stream.get("streamConnectionID", 0)
                if type(sid) is not int or not -(1 << 63) <= sid < (1 << 64):
                    raise ValueError("invalid mirror stream ID")
            elif kind == 96:
                ct = stream.get("ct", 8)
                if type(ct) is not int or not 0 <= ct <= 255:
                    raise ValueError("invalid AirPlay audio codec")
        out = {}
        if "ekey" in plist and "eiv" in plist:
            for field in ("name", "model", "osVersion"):
                value = plist.get(field, "")
                if not isinstance(value, str) or len(value) > 128 or any(c in value for c in "\r\n\0"):
                    raise ValueError("invalid AirPlay device metadata")
            ekey, eiv = plist["ekey"], plist["eiv"]
            if not isinstance(ekey, bytes) or len(ekey) != 72 or not isinstance(eiv, bytes) or len(eiv) != 16:
                raise ValueError("invalid AirPlay key or IV")
            timing_port = plist.get("timingPort", 0)
            if type(timing_port) is not int or not 0 <= timing_port <= 65535:
                raise ValueError("invalid AirPlay timing port")
            self.client_name = plist.get("name") or "iPhone"
            log.info("SETUP from %r (%r, %r)", self.client_name, plist.get("model", ""), plist.get("osVersion", ""))
            loop = asyncio.get_running_loop()
            aeskey = await loop.run_in_executor(None, self.fairplay.decrypt, ekey)
            if self.pairing.ecdh_secret is not None:
                aeskey = hashlib.sha512(aeskey + self.pairing.ecdh_secret).digest()[:16]
            self.aeskey, self.aesiv = aeskey, eiv
            if self.timing:
                self.timing.stop()
            self.timing = TimingSender()
            tport = await self.timing.start(self.remote_ip, timing_port)
            out["timingPort"] = tport
            out["eventPort"] = 0
        if streams:
            if self.aeskey is None:
                raise ValueError("stream SETUP before key exchange")
            async with rx._stream_lock:
                if (110 in kinds and rx._mirror_owner not in (None, self)) or (
                    96 in kinds and rx._audio_owner not in (None, self)
                ) or any(owner and owner.remote_ip != self.remote_ip
                         for owner in (rx._mirror_owner, rx._audio_owner)):
                    resp.status, resp.reason = 453, "Receiver Busy"
                    return
                try:
                    out["streams"] = await self._setup_streams(streams)
                except BaseException:
                    if 96 in kinds:
                        self._stop_audio()
                    if 110 in kinds:
                        await self._stop_mirror()
                    raise
        resp.set(_bplist(out), "application/x-apple-binary-plist")

    async def _setup_streams(self, streams):
        rx = self.rx
        results = []
        for stream in streams:
            kind = stream["type"]
            if kind == 110:
                sid = stream.get("streamConnectionID", 0)
                await self._stop_mirror()
                mirror = MirrorStream(self.aeskey, sid & 0xFFFFFFFFFFFFFFFF, rx.decoder, rx.emit,
                                      peer_ip=self.remote_ip, client_name=self.client_name,
                                      on_end=self._mirror_finished)
                try:
                    port = await mirror.start(self._mirror_offer_deadline)
                except BaseException:
                    await mirror.stop()
                    raise
                self.mirror, rx._mirror_owner = mirror, self
                results.append({"dataPort": port, "type": 110})
            elif kind == 96:
                ct = stream.get("ct", 8)
                # Replacing a stream from the same connection retains its player and volume.
                if self.audio:
                    self._audio_offer_deadline = self.audio.pending_deadline
                    self.audio.stop()
                    self.audio = None
                player = rx.get_player()
                if player is None:
                    self._stop_audio()
                    results.append({"dataPort": 0, "controlPort": 0, "type": 96})
                    continue
                audio = AudioStream(self.aeskey, self.aesiv, ct, player, peer_ip=self.remote_ip,
                                    on_end=self._audio_finished)
                try:
                    dport, cport = await audio.start(self._audio_offer_deadline)
                    if rx.player is not player:
                        raise ConnectionError("audio owner ended during stream setup")
                except BaseException:
                    audio.stop()
                    if rx._audio_owner is self:
                        self._stop_audio()
                    else:
                        player.close()
                        rx.player = None
                    raise
                self.audio, rx._audio_owner = audio, self
                results.append({"dataPort": dport, "controlPort": cport, "type": 96})
            else:
                log.warning("unknown stream type %d", kind)
        return results

    def on_get_parameter(self, req, resp):
        if req.body.startswith(b"volume"):
            resp.set(f"volume: {self.rx.volume_db:9.6f}\r\n".encode(), "text/parameters")

    def on_set_parameter(self, req):
        ct = req.header("Content-Type") or ""
        if self.rx._audio_owner is self and ct == "text/parameters" and req.body.startswith(b"volume:"):
            db = float(req.body.split(b":", 1)[1].strip())
            if not math.isfinite(db) or not -144 <= db <= 0:
                raise ValueError("invalid AirPlay volume")
            self.rx.set_volume(db)

    def _stop_audio(self):
        if self.audio:
            self.audio.stop()
        self._audio_finished(self.audio)

    def _audio_finished(self, audio):
        if self.audio is audio:
            if audio:
                self._audio_offer_deadline = audio.pending_deadline
            self.audio = None
            if self.rx._audio_owner is self:
                self.rx._audio_owner = None
                self.rx.set_volume(0)
                if self.rx.player:
                    self.rx.player.close()
                    self.rx.player = None

    async def _stop_mirror(self):
        mirror, self.mirror = self.mirror, None
        if mirror:
            await mirror.stop()
            self._mirror_offer_deadline = mirror.pending_deadline
        if self.rx._mirror_owner is self:
            self.rx._mirror_owner = None

    def _mirror_finished(self, mirror):
        if self.mirror is mirror:
            self._mirror_offer_deadline = mirror.pending_deadline
            self.mirror = None
            if self.rx._mirror_owner is self:
                self.rx._mirror_owner = None
            audio_owner = self.rx._audio_owner
            if audio_owner and audio_owner.remote_ip == self.remote_ip:
                audio_owner._stop_audio()

    async def on_teardown(self, req, resp):
        types = set()
        if req.body:
            try:
                types = {s.get("type") for s in _read_plist(req.body).get("streams", [])}
            except Exception:
                pass
        resp.headers.append(("Connection", "close"))
        if 96 in types:
            self._stop_audio()
        if 110 in types:
            await self._stop_mirror()
        if not types:
            await self.cleanup()
            resp.close = True

    async def cleanup(self):
        self._stop_audio()
        await self._stop_mirror()
        if self.timing:
            self.timing.stop()
            self.timing = None


class Receiver:
    """Runs the AirPlay receiver on its own asyncio thread."""

    def __init__(self, settings, on_frame, on_event):
        self.settings = settings
        self.identity = ServerIdentity(settings["ed25519_seed"])
        self.pk_hex = self.identity.public.hex()
        self.on_event = on_event
        self.decoder = VideoDecoder(on_frame)
        self.player = None
        self.volume_db = 0.0
        self.muted = False
        self.loop = None
        self.port = 0
        self.advertiser = None
        self._mirror_owner = None
        self._audio_owner = None
        self._stream_lock = asyncio.Lock()
        self._connections = Counter()
        self._thread = None
        self._server = None
        self._shutdown = Future()  # signaled by the GUI thread, awaited on the receiver loop
        self._advertiser_lock = threading.Lock()

    def emit(self, event, **info):
        try:
            self.on_event(event, info)
        except Exception:
            log.exception("event handler failed")

    # ---------------------------------------------------------------- audio
    def get_player(self):
        if not self.settings.get("audio", True):
            return None
        if self.player is None:
            self.player = Player()
            self._apply_volume()
        return self.player

    def set_volume(self, db: float):
        self.volume_db = db
        self._apply_volume()

    def set_muted(self, muted: bool):
        self.muted = muted
        self._apply_volume()

    def _apply_volume(self):
        if self.player is None:
            return
        db = self.volume_db
        self.player.muted = self.muted or db <= -144
        self.player.gain = 0.0 if db <= -144 else max(0.0, min(1.0, 10 ** (db / 20.0)))

    # ---------------------------------------------------------------- lifecycle
    def start(self):
        self.decoder.start()
        self._thread = threading.Thread(target=self._run, name="airplay", daemon=True)
        self._thread.start()

    def _run(self):
        try:
            asyncio.run(self._serve())
        except Exception:
            log.exception("receiver crashed")
            self.emit("error", message="The AirPlay server crashed. See the log for details.")

    async def _serve(self):
        self.loop = asyncio.get_running_loop()
        try:
            if not self._shutdown.done():
                await self._main()
                await asyncio.wrap_future(self._shutdown)
        finally:
            self._shutdown.cancel()
            if self._server:
                self._server.close()
            # RTSP, mirror and timing tasks all belong to this receiver's loop.
            tasks = asyncio.all_tasks() - {asyncio.current_task()}
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._server:
                await self._server.wait_closed()
            if self.player:
                self.player.close()
            if self.advertiser:
                try:
                    await asyncio.to_thread(self._advertise, restart=False)
                except Exception:
                    log.exception("could not stop mDNS advertisement")

    async def _main(self):
        async def handler(reader, writer):
            peer = writer.get_extra_info("peername")
            if (self._shutdown.done() or not peer or self._connections[peer[0]] >= 3
                    or sum(self._connections.values()) >= RTSP_CONNECTION_LIMIT):
                writer.transport.abort()
                return
            self._connections[peer[0]] += 1
            try:
                await Connection(self, reader, writer).serve()
            finally:
                self._connections[peer[0]] -= 1
                if not self._connections[peer[0]]:
                    del self._connections[peer[0]]

        try:
            self._server = await asyncio.start_server(handler, host=net.BIND_HOST, port=net.PORT, limit=16384)
        except OSError:
            self._server = await asyncio.start_server(handler, host=net.BIND_HOST, port=0, limit=16384)
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("RTSP server listening on port %d", self.port)
        if not net.ADVERTISE:
            self.emit("listening", name=self.settings["name"], addresses=[net.BIND_HOST], port=self.port)
            return
        self.advertiser = discovery.Advertiser(self.settings, self.pk_hex, self.port)
        try:
            await asyncio.to_thread(self._advertise)
        except Exception:
            log.exception("mDNS advertisement failed")
            self.emit("error", message="Could not announce this PC on the network (mDNS).")
            return
        self.emit("listening", name=self.settings["name"], addresses=self.advertiser.addresses, port=self.port)

    def _advertise(self, restart=True):
        with self._advertiser_lock:
            self.advertiser.stop()
            if restart and not self._shutdown.done():
                self.advertiser.start()
                return True
        return False

    def rename(self, name: str):
        """Change the name shown on the iPhone and re-announce the receiver."""
        self.settings["name"] = name
        self.settings.save()
        if not self.loop or not self.advertiser or self._shutdown.done():
            return

        def reannounce():
            try:
                if self._advertise():
                    self.emit("listening", name=name, addresses=self.advertiser.addresses, port=self.port)
            except Exception:
                log.exception("re-announce failed")

        with suppress(RuntimeError):  # the loop may close before this callback is queued
            self.loop.call_soon_threadsafe(lambda: self.loop.run_in_executor(None, reannounce))

    def stop(self):
        with suppress(InvalidStateError):
            self._shutdown.set_result(None)
        self.decoder.stop()
        if self._thread:
            self._thread.join(timeout=2)
        if self.decoder.ident is not None:
            self.decoder.join(timeout=2)
