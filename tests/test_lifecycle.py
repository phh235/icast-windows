"""Regression tests for ending a mirror session and preserving settings."""
import asyncio
import json
import plistlib
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import av

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from icast.airplay import Connection, Receiver, Request, Response, RTSP_BODY_LIMIT, _read_plist  # noqa: E402
from icast.audio import _Proto  # noqa: E402
from icast.config import Settings  # noqa: E402
from icast.video import MAX_PACKET_BYTES, MirrorStream, VideoDecoder  # noqa: E402
from tools.fake_iphone import Rtsp  # noqa: E402
from tools.gen_fairplay import gen_garble  # noqa: E402


class VideoLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_teardown_emits_stop_once_after_stream_eof(self):
        events = []
        decoder = VideoDecoder(lambda *args: None)
        mirror = MirrorStream(b"\0" * 16, 1, decoder, lambda event, **info: events.append(event),
                              peer_ip="127.0.0.1")
        reader = asyncio.StreamReader()
        reader.feed_eof()
        writer = mock.Mock(wait_closed=mock.AsyncMock())
        writer.get_extra_info.return_value = ("127.0.0.1", 0)
        await mirror._handle(reader, writer)
        writer.close.assert_called_once()
        writer.wait_closed.assert_awaited_once()

        connection = object.__new__(Connection)
        connection.rx = SimpleNamespace(_mirror_owner=connection)
        connection.mirror = mirror
        request = SimpleNamespace(body=plistlib.dumps({"streams": [{"type": 110}]}))
        await connection.on_teardown(request, Response())
        self.assertEqual(events, ["connecting", "mirror_stopped"])
        self.assertIsNone(connection.rx._mirror_owner)

    async def test_teardown_before_video_connect_also_notifies(self):
        events = []
        mirror = MirrorStream(b"\0" * 16, 1, VideoDecoder(lambda *args: None), events.append,
                              peer_ip="127.0.0.1")
        await mirror.stop()
        await mirror.stop()
        self.assertEqual(events, ["mirror_stopped"])

    def test_new_session_clears_full_queue_and_stale_screenshot(self):
        deliveries = []
        decoder = VideoDecoder(lambda frame, w, h: deliveries.append(frame))
        old = decoder.reset()
        frame = av.VideoFrame(16, 16, "bgra")
        decoder._deliver(old, frame)
        self.assertIs(decoder.last_frame, frame)
        for _ in range(decoder.MAX_PENDING):
            decoder.submit(old, b"queued frame")
        self.assertEqual(len(decoder._pending), decoder.MAX_PENDING)

        current = decoder.reset()
        self.assertFalse(decoder._pending)
        self.assertIsNone(decoder.last_frame)
        self.assertFalse(decoder.end(old))
        decoder.submit(old, b"late packet")
        self.assertFalse(decoder._pending)
        decoder._deliver(old, frame)
        self.assertEqual(len(deliveries), 1)
        decoder._deliver(current, frame)
        self.assertEqual(len(deliveries), 2)
        self.assertTrue(decoder.end(current))
        self.assertIsNone(decoder.last_frame)

    def test_frame_conversion_finishing_after_teardown_is_discarded(self):
        decoder = VideoDecoder(mock.Mock())
        generation = decoder.reset()
        started = threading.Event()
        finish = threading.Event()
        frame = av.VideoFrame(16, 16, "bgra")

        def reformat(*args, **kwargs):
            started.set()
            finish.wait(3)
            return frame

        decoder._reformatter = mock.Mock(reformat=reformat)
        worker = threading.Thread(target=decoder._deliver, args=(generation, frame))
        worker.start()
        try:
            self.assertTrue(started.wait(3))
            self.assertTrue(decoder.end(generation))
        finally:
            finish.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertIsNone(decoder.last_frame)
        decoder.on_frame.assert_not_called()

    def test_decoder_can_be_joined_after_stop(self):
        decoder = VideoDecoder(lambda *args: None)
        decoder.start()
        decoder.stop()
        decoder.join(3)
        self.assertFalse(decoder.is_alive())


class SettingsTests(unittest.TestCase):
    def test_failed_write_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            settings = Settings(path)
            original = path.read_bytes()
            settings["name"] = "Pending name"

            def partial_dump(data, file, **kwargs):
                file.write('{"name":')
                raise OSError("simulated interrupted write")

            with mock.patch("icast.config.json.dump", side_effect=partial_dump):
                with self.assertLogs("icast.config", level="WARNING"):
                    settings.save()
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.iterdir()), [path])
            settings.save()
            self.assertEqual(Settings(path)["name"], "Pending name")

    def test_replaces_invalid_identity_but_retains_other_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"name": "My receiver", "mac": "bad", "ed25519_seed": 1}), encoding="utf-8")
            settings = Settings(path)
            self.assertEqual(settings["name"], "My receiver")
            self.assertEqual(len(settings.mac_bytes), 6)
            self.assertEqual(len(bytes.fromhex(settings["ed25519_seed"])), 32)
            self.assertEqual(Settings(path).data, settings.data)


class NetworkBoundaryTests(unittest.IsolatedAsyncioTestCase):
    HEARTBEAT = struct.pack("<IB", 0, 2) + bytes(123)
    AVC_CONFIG = bytes.fromhex("0164001fffe100016701000168")

    def setUp(self):
        self.receiver = Receiver({"ed25519_seed": "ab" * 32}, mock.Mock(), mock.Mock())

    def connection(self, ip="127.0.0.1"):
        writer = mock.Mock(wait_closed=mock.AsyncMock())
        writer.get_extra_info.return_value = (ip, 1)
        return Connection(self.receiver, asyncio.StreamReader(), writer)

    async def test_rtsp_rejects_lengths_before_waiting_for_body(self):
        for length in (b"-1", str(RTSP_BODY_LIMIT + 1).encode(), b"1\r\nContent-Length: 2"):
            with self.subTest(length=length):
                conn = self.connection()
                conn.reader.feed_data(b"SETUP * RTSP/1.0\r\nContent-Length: " + length + b"\r\n\r\n")
                with self.assertRaises(ValueError):
                    await asyncio.wait_for(conn._read_request(), 0.2)
        conn = self.connection()
        conn.reader.feed_data(b"SETUP * RTSP/1.0\r\nContent-Length: 2\r\n\r\nxx")
        self.assertEqual((await conn._read_request()).body, b"xx")

    async def test_quiet_rtsp_survives_active_media_but_partial_header_times_out(self):
        for stream in ("mirror", "audio"):
            with self.subTest(stream=stream):
                conn = self.connection()
                checked = asyncio.Event()

                def recent_frame():
                    checked.set()
                    return time.monotonic()

                media = mock.Mock()
                setattr(conn, stream, media)
                with mock.patch.object(type(media), "last_activity", create=True,
                                       new_callable=mock.PropertyMock, side_effect=recent_frame), \
                        mock.patch("icast.airplay.RTSP_MEDIA_IDLE_TIMEOUT", 0.05), \
                        mock.patch("icast.airplay.RTSP_HEADER_TIMEOUT", 0.05):
                    task = asyncio.create_task(conn._read_request())
                    try:
                        await asyncio.wait_for(checked.wait(), 1)
                        conn.reader.feed_data(b"OPTIONS * RTSP/1.0\r\n\r\n")
                        self.assertEqual((await asyncio.wait_for(task, 1)).method, "OPTIONS")
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    conn.reader.feed_data(b"O")
                    with self.assertRaises(TimeoutError):
                        await asyncio.wait_for(conn._read_request(), 1)

    async def test_rtsp_times_out_with_stale_media(self):
        conn = self.connection()
        conn.mirror = SimpleNamespace(last_activity=time.monotonic() - 60)
        with mock.patch("icast.airplay.RTSP_MEDIA_IDLE_TIMEOUT", 0.01), self.assertRaises(TimeoutError):
            await conn._read_request()

    async def test_setup_rejects_container_and_multiline_metadata(self):
        conn = self.connection()
        conn.fairplay.decrypt = mock.Mock()
        for name in ([["nested"]], "forged\nline"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "metadata"):
                body = plistlib.dumps({"name": name, "ekey": bytes(72), "eiv": bytes(16)})
                await conn.on_setup(Request("SETUP", "*", "RTSP/1.0", {}, body), Response())
        conn.fairplay.decrypt.assert_not_called()
        self.receiver.on_event.assert_not_called()

    def test_binary_plist_budget_applies_before_parse(self):
        alias = b"bplist00" + b"\x09\x08\x08" + struct.pack(">6xBBQQQ", 1, 1, 2, 0, 9)
        costly = b"bplist00" + bytes(60000) + struct.pack(">6xBBQQQ", 1, 1, 100, 0, 59000)
        with mock.patch("icast.airplay.plistlib.loads") as parse:
            for payload in (alias, costly):
                with self.subTest(length=len(payload)), self.assertRaises(ValueError):
                    _read_plist(payload)
            parse.assert_not_called()
        self.assertEqual(_read_plist(plistlib.dumps({"name": "iPhone"}, fmt=plistlib.FMT_BINARY)), {"name": "iPhone"})

    async def test_other_connection_cannot_take_over_mirror_or_volume(self):
        owner, peer = self.connection(), self.connection("127.0.0.2")
        self.receiver._mirror_owner = self.receiver._audio_owner = owner
        owner.mirror = mock.Mock()
        peer.aeskey = bytes(16)
        body = plistlib.dumps({"streams": [{"type": 110}]})
        resp = Response()
        await peer.on_setup(Request("SETUP", "*", "RTSP/1.0", {}, body), resp)
        self.assertEqual(resp.status, 453)
        owner.mirror.stop.assert_not_called()
        req = Request("SET_PARAMETER", "*", "RTSP/1.0", {"content-type": "text/parameters"}, b"volume: -144")
        peer.on_set_parameter(req)
        self.assertEqual(self.receiver.volume_db, 0)
        owner.on_set_parameter(req)
        self.assertEqual(self.receiver.volume_db, -144)

    async def test_unattached_offer_expires_and_releases_ownership(self):
        conn = self.connection()
        mirror = MirrorStream(bytes(16), 1, self.receiver.decoder, mock.Mock(), peer_ip=conn.remote_ip,
                              on_end=conn._mirror_finished)
        conn.mirror, self.receiver._mirror_owner = mirror, conn
        mirror.server = mock.Mock()
        listener = mirror.server
        mirror._expire_offer()
        listener.close.assert_called_once()
        self.assertTrue(mirror.revoked.is_set())
        self.assertIsNone(self.receiver._mirror_owner)

    async def test_heartbeats_and_stream_retries_cannot_extend_pending_reservations(self):
        conn, peer = self.connection(), self.connection("127.0.0.2")
        conn.aeskey = conn.aesiv = peer.aeskey = bytes(16)
        streams = [{"type": 110}, {"type": 96, "ct": 4}]
        setup = Request("SETUP", "*", "RTSP/1.0", {}, plistlib.dumps({"streams": streams}))
        mirror_only = plistlib.dumps({"streams": streams[:1]})
        teardown = Request("TEARDOWN", "*", "RTSP/1.0", {}, mirror_only)
        listener = mock.Mock(sockets=[mock.Mock()])
        listener.sockets[0].getsockname.return_value = ("127.0.0.1", 12345)
        transport = mock.Mock()
        transport.get_extra_info.return_value = ("127.0.0.1", 12346)
        with mock.patch("icast.video.asyncio.start_server", return_value=listener), \
                mock.patch.object(asyncio.get_running_loop(), "create_datagram_endpoint",
                                  return_value=(transport, None)), \
                mock.patch("icast.airplay.Player"), mock.patch("icast.video.MIRROR_CONNECT_TIMEOUT", 0.2):
            await conn.on_setup(setup, Response())
            self.addAsyncCleanup(conn.cleanup)
            video_deadline, audio_deadline = conn.mirror.pending_deadline, conn.audio.pending_deadline
            await conn.on_teardown(teardown, Response())
            await conn.on_setup(setup, Response())
            mirror = conn.mirror
            self.assertEqual(mirror.pending_deadline, video_deadline)
            self.assertEqual(conn.audio.pending_deadline, audio_deadline)

            peer_setup = Request("SETUP", "*", "RTSP/1.0", {}, mirror_only)
            response = Response()
            await peer.on_setup(peer_setup, response)
            self.assertEqual(response.status, 453)

            reader = asyncio.StreamReader()
            pause = struct.pack("<IBBB", len(self.AVC_CONFIG), 1, 0, 0x56) + bytes(121)
            reader.feed_data(pause + self.AVC_CONFIG + self.HEARTBEAT * 3)
            await asyncio.wait_for(mirror._handle(reader, conn.writer), 1)
            self.assertTrue(mirror.revoked.is_set())
            self.assertIsNone(self.receiver._mirror_owner)
            self.assertIsNone(self.receiver._audio_owner)
            conn.writer.close.assert_called_once()
            with self.assertRaisesRegex(ValueError, "mirror offer expired"):
                await conn.on_setup(setup, Response())

            response = Response()
            await peer.on_setup(peer_setup, response)
            self.addAsyncCleanup(peer.cleanup)
            self.assertEqual(response.status, 200)
            self.assertIs(self.receiver._mirror_owner, peer)

    async def test_paused_mirror_with_decoded_video_stays_alive_on_heartbeats(self):
        conn = self.connection()
        mirror = MirrorStream(bytes(16), 1, self.receiver.decoder, mock.Mock(), peer_ip=conn.remote_ip)
        conn.mirror = mirror
        self.addAsyncCleanup(mirror.stop)
        pause = bytearray(128)
        pause[4], pause[6] = 1, 0x56
        old_frame = time.monotonic() - 180
        with mock.patch.object(mirror.decoder, "last_activity", return_value=old_frame), \
                mock.patch("icast.airplay.RTSP_MEDIA_IDLE_TIMEOUT", 0.1), \
                mock.patch("icast.video.MIRROR_IDLE_TIMEOUT", 0.1):
            mirror._packet(pause, self.AVC_CONFIG)
            self.assertGreater(mirror.last_activity, old_frame)

            task = asyncio.create_task(conn._read_request())
            try:
                for _ in range(3):
                    await asyncio.sleep(0.05)
                    mirror._packet(self.HEARTBEAT, b"")
                mirror._expire_offer()
                self.assertFalse(mirror.revoked.is_set())
                conn.reader.feed_data(b"OPTIONS * RTSP/1.0\r\n\r\n")
                self.assertEqual((await asyncio.wait_for(task, 1)).method, "OPTIONS")
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_audio_setup_replacement_preserves_negotiated_volume(self):
        conn = self.connection()
        conn.aeskey = conn.aesiv = bytes(16)
        self.receiver.player = player = mock.Mock()
        self.receiver._audio_owner = conn
        conn.audio = previous = mock.Mock(pending_deadline=None)
        self.receiver.set_volume(-144)
        replacement = mock.Mock(start=mock.AsyncMock(return_value=(1234, 1235)))
        with mock.patch("icast.airplay.AudioStream", return_value=replacement):
            await conn._setup_streams([{"type": 96}])
        previous.stop.assert_called_once()
        self.assertIs(conn.audio, replacement)
        self.assertIs(self.receiver.player, player)
        self.assertEqual(self.receiver.volume_db, -144)
        self.assertTrue(player.muted)
        self.assertEqual(player.gain, 0)
        player.close.assert_not_called()

        conn._stop_audio()
        self.assertIsNone(self.receiver._audio_owner)
        self.assertEqual(self.receiver.volume_db, 0)
        player.close.assert_called_once()

    async def test_video_checks_peer_and_size_before_buffering(self):
        decoder = self.receiver.decoder
        mirror = MirrorStream(bytes(16), 1, decoder, mock.Mock(), peer_ip="127.0.0.1")
        peer = self.connection("127.0.0.2").writer
        reader = mock.Mock(readexactly=mock.AsyncMock())
        await mirror._handle(reader, peer)
        reader.readexactly.assert_not_awaited()
        peer.close.assert_called_once()
        owner = self.connection().writer
        reader.readexactly.return_value = struct.pack("<I", MAX_PACKET_BYTES + 1) + bytes(124)
        await mirror._handle(reader, owner)
        reader.readexactly.assert_awaited_once_with(128)

    def test_udp_checks_sender_and_decoder_caps_bytes(self):
        handler = mock.Mock()
        proto = _Proto(handler, "127.0.0.1")
        proto.datagram_received(b"peer", ("127.0.0.2", 1))
        proto.datagram_received(b"owner", ("127.0.0.1", 2))
        handler.assert_called_once_with(b"owner")
        decoder = self.receiver.decoder
        session = decoder.reset()
        with mock.patch.object(decoder, "MAX_PENDING_BYTES", 4):
            decoder.submit(session, b"aaaa")
            decoder.submit(session, b"b")
        self.assertEqual(list(decoder._pending), [b"aaaa"])


class ToolAndAudioTests(unittest.TestCase):
    def test_generator_accepts_arithmetic_and_rejects_python_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp)
            src = lib / "playfair" / "hand_garble.c"
            src.parent.mkdir()
            src.write_text("void garble() { unsigned int tmp; tmp=1; buffer0[0]=rol8(tmp, 2); }")
            compile(gen_garble(lib), "<generated>", "exec")
            src.write_text('void garble() { buffer0[0]=__import__("os").system("example"); }')
            with self.assertRaisesRegex(SystemExit, "unsupported FairPlay"):
                gen_garble(lib)

    def test_simulator_treats_early_eof_as_an_error(self):
        client = object.__new__(Rtsp)
        client.cseq = 0
        header = b"RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Length: 2\r\n\r\n"
        client.s = mock.Mock()
        client.s.recv.side_effect = [bytes([b]) for b in header] + [b"x", b""]
        with self.assertRaisesRegex(ConnectionError, "truncated"):
            client.request("GET", "/info")


class ReceiverShutdownTests(unittest.TestCase):
    def test_stop_aborts_stalled_rtsp_writer_and_closes_loop(self):
        connected = threading.Event()
        receiver = Receiver({"ed25519_seed": "ab" * 32}, lambda *args: None, lambda *args: None)
        tasks = []

        async def read_request():
            connected.set()
            await asyncio.Event().wait()

        async def wait_closed():
            await asyncio.Event().wait()

        writer = mock.Mock(wait_closed=mock.AsyncMock(side_effect=wait_closed))
        writer.get_extra_info.return_value = ("127.0.0.1", 0)

        async def start():
            connection = Connection(receiver, None, writer)
            connection._read_request = read_request
            tasks.append(asyncio.create_task(connection.serve()))

        with mock.patch.object(receiver, "_main", side_effect=start), mock.patch("icast.airplay.RTSP_CLOSE_TIMEOUT", 0.01):
            receiver.start()
            try:
                self.assertTrue(connected.wait(3))
            finally:
                receiver.stop()
        writer.wait_closed.assert_awaited_once()
        writer.transport.abort.assert_called_once()
        self.assertTrue(tasks[0].cancelled())
        self.assertTrue(receiver.loop.is_closed())
        self.assertFalse(receiver.decoder.is_alive())


if __name__ == "__main__":
    unittest.main()
