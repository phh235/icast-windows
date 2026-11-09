"""Regression tests for ending a mirror session and preserving settings."""
import asyncio
import json
import plistlib
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import av

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from icast.airplay import Connection, Receiver, Response  # noqa: E402
from icast.config import Settings  # noqa: E402
from icast.video import MirrorStream, VideoDecoder  # noqa: E402


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
