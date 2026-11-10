"""Regression tests for ending a mirror session and preserving settings."""
import asyncio
import json
import plistlib
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import av

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from icast.airplay import Connection, Receiver, Request, Response, RTSP_BODY_LIMIT, _read_plist  # noqa: E402
from icast.config import Settings  # noqa: E402
from icast.video import MirrorStream, VideoDecoder  # noqa: E402
from tools.fake_iphone import Rtsp  # noqa: E402
from tools.gen_fairplay import gen_garble  # noqa: E402


class VideoLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_teardown_emits_stop_once_after_stream_eof(self):
        events = []
        decoder = VideoDecoder(lambda *args: None)
        mirror = MirrorStream(b"\0" * 16, 1, decoder, events.append)
        reader = asyncio.StreamReader()
        reader.feed_eof()
        writer = mock.Mock(wait_closed=mock.AsyncMock())
        await mirror._handle(reader, writer)
        writer.close.assert_called_once()
        writer.wait_closed.assert_awaited_once()

        connection = object.__new__(Connection)
        connection.mirror = mirror
        request = SimpleNamespace(body=plistlib.dumps({"streams": [{"type": 110}]}))
        await connection.on_teardown(request, Response())
        self.assertEqual(events, ["mirror_stopped"])

    async def test_teardown_before_video_connect_also_notifies(self):
        events = []
        mirror = MirrorStream(b"\0" * 16, 1, VideoDecoder(lambda *args: None), events.append)
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
