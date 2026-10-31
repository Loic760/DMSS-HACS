"""Actual HA HomeKit FFmpeg/SRTP startup; synthetic media, no real devices."""
import asyncio
import base64
import os
import shutil
import socket
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import HomeAssistant
from homeassistant.components.homekit.type_cameras import Camera, CONFIG_DEFAULTS

from custom_components.dmss.media import DMSSMediaManager, _Credentials
from test_media import camera_packets, synthetic_feed


def local_ports():
    """Reserve a free RTP/RTCP pair plus an audio port for this local test."""
    for _ in range(20):
        sockets = []
        try:
            first = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sockets.append(first)
            first.bind(("127.0.0.1", 0))
            port = first.getsockname()[1]
            if port > 65532:
                continue
            for next_port in (port + 1, port + 2):
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sockets.append(sock)
                sock.bind(("127.0.0.1", next_port))
            return port, port + 2
        except OSError:
            pass
        finally:
            for sock in sockets:
                sock.close()
    raise AssertionError("Unable to reserve local test ports")


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg unavailable")
class HomeKitMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_homekit_start_stop_and_same_port_reopen_with_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            coordinator = Mock()
            async def call(function, *args):
                return await hass.async_add_executor_job(function, *args)
            coordinator.async_call = call
            manager = DMSSMediaManager(hass, coordinator, idle_timeout=.05)
            manager.binary = shutil.which("ffmpeg")
            manager._acquire = Mock(return_value=_Credentials("http://example.invalid/test", "dummy", "dummy", False))
            packets = await asyncio.to_thread(camera_packets, 12, True)
            feeds = []
            url = await manager.async_stream_source("test-only", 1)
            # Use the real HomeKit implementation without starting/pairing a
            # HAP accessory. Its complete SRTP/audio-proxy path is exercised.
            camera = object.__new__(Camera)
            camera.hass = hass
            camera.display_name = "Offline HomeKit test"
            camera.config = {**CONFIG_DEFAULTS, "video_codec":"copy", "support_audio":True}
            camera._ffmpeg = SimpleNamespace(binary=manager.binary)
            camera._async_get_stream_source = AsyncMock(return_value=url)
            camera.set_streaming_available = Mock()
            camera.sessions = {}
            errors = []
            async def stderr(reader):
                while line := await reader.readline():
                    errors.append(line.decode(errors="replace"))
            camera._async_log_stderr_stream = stderr
            video_port, audio_port = local_ports()
            active = None
            try:
                with patch("custom_components.dmss.media.HTTPMediaFeed", synthetic_feed(packets, feeds, .005)):
                    for number in range(2):
                        errors.clear()
                        info = {"id":f"offline-{number}", "stream_idx":0}
                        camera.sessions[info["id"]] = info
                        config = {
                            "address":"127.0.0.1", "v_port":video_port, "a_port":audio_port,
                            "v_srtp_key":base64.b64encode(os.urandom(30)).decode(),
                            "a_srtp_key":base64.b64encode(os.urandom(30)).decode(),
                            "v_ssrc":1234, "a_ssrc":5678, "fps":30,
                            "v_max_bitrate":299, "a_max_bitrate":24,
                            "a_sample_rate":16, "a_packet_time":20,
                        }
                        self.assertTrue(await camera.start_stream(info, config))
                        active = info
                        process = info["stream"].process
                        deadline = time.monotonic() + 5
                        while not any("Stream #1:0: Audio: opus" in line for line in errors):
                            self.assertIsNone(process.returncode, "HomeKit FFmpeg exited during startup")
                            self.assertLess(time.monotonic(), deadline, "HomeKit media startup timed out")
                            await asyncio.sleep(.05)
                        await asyncio.sleep(.3)
                        self.assertIsNone(process.returncode)
                        self.assertFalse(any("backward in time" in line or "Non-monotonic" in line for line in errors))
                        session = manager._sessions[("test-only", 1)]
                        await camera.stop_stream(info)
                        active = None
                        await asyncio.wait_for(process.wait(), 2)
                        self.assertTrue(await asyncio.to_thread(session.done.wait, 2))
                    self.assertEqual(len(feeds), 2)
                    self.assertTrue(all(feed.closed.is_set() for feed in feeds))
            finally:
                if active is not None:
                    await camera.stop_stream(active)
                await manager.async_close()
                await hass.async_stop()
