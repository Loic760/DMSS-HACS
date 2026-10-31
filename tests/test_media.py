"""Offline loopback/FFmpeg tests. No DMSS account or camera is contacted."""
import asyncio
import io
import json
import queue
import re
import select
import shutil
import socket
import struct
import subprocess
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests
import av

from custom_components.dmss.media import (
    DMSSMediaManager, _Credentials, _Session, normalize_dhav_date,
)
from custom_components.dmss.media_protocol import MediaError
from custom_components.dmss.media_protocol import rtp_payload
from custom_components.dmss.client import APIError


def dhav(payload, kind, sequence, millis, extra=b"", date=0x6aa80000):
    size = 32 + len(extra) + len(payload)
    header = bytearray(struct.pack("<4sB3xIIIHBB", b"DHAV", kind, sequence, size,
                                   date, millis & 65535, len(extra), 0))
    header[23] = sum(header[:23]) & 255
    return bytes(header) + extra + payload + b"dhav" + struct.pack("<I", size)


FLV_HEADER = b"FLV\x01\x05\x00\x00\x00\x09\x00\x00\x00\x00"


def flv_tag(payload, kind=9):
    header = bytes([kind]) + len(payload).to_bytes(3, "big") + b"\0" * 7
    return header + payload + (len(payload) + 11).to_bytes(4, "big")


class _RTPViewer:
    """Real FFmpeg copy-video/Opus consumer, with local RTP rather than SRTP.

    This exercises HomeKit's default input probing without changing its input
    flags. RTP payload decoding also establishes that actual video is produced.
    """

    def __init__(self, url):
        self.video = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for sock in (self.video, self.audio):
            sock.bind(("127.0.0.1", 0))
        self.started = time.monotonic()
        self.process = subprocess.Popen([
            "ffmpeg", "-v", "error", "-i", url,
            "-map", "0:v:0", "-an", "-c:v", "copy", "-payload_type", "99",
            "-f", "rtp", f"rtp://127.0.0.1:{self.video.getsockname()[1]}?pkt_size=1316",
            "-map", "0:a:0", "-vn", "-c:a", "libopus", "-application", "lowdelay",
            "-ac", "1", "-ar", "16000", "-b:a", "24k", "-frame_duration", "20",
            "-payload_type", "110", "-f", "rtp",
            f"rtp://127.0.0.1:{self.audio.getsockname()[1]}?pkt_size=188"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def receive(self):
        deadline = time.monotonic() + 6
        timestamps = set()
        bitstream = bytearray()
        audio_packets = 0
        first_video = None
        while time.monotonic() < deadline:
            for sock in select.select([self.video, self.audio], [], [], .1)[0]:
                packet = sock.recv(65536)
                if sock is self.audio:
                    rtp_payload(packet, 110)
                    audio_packets += 1
                    continue
                payload = rtp_payload(packet, 99)
                if first_video is None:
                    first_video = time.monotonic() - self.started
                nal = payload[0] & 31
                if nal == 24:  # STAP-A: SPS/PPS and potentially other NALs.
                    offset = 1
                    while offset + 2 <= len(payload):
                        size = int.from_bytes(payload[offset:offset + 2], "big")
                        offset += 2
                        bitstream.extend(b"\0\0\0\1" + payload[offset:offset + size])
                        offset += size
                elif nal == 28:  # FU-A: fragmented H264 NAL.
                    if payload[1] & 128:
                        bitstream.extend(b"\0\0\0\1" + bytes([(payload[0] & 224) | (payload[1] & 31)]))
                    bitstream.extend(payload[2:])
                else:
                    bitstream.extend(b"\0\0\0\1" + payload)
                if packet[1] & 128:
                    timestamps.add(struct.unpack_from("!I", packet, 4)[0])
            if len(timestamps) >= 12 and audio_packets:
                return first_video, bytes(bitstream), len(timestamps), audio_packets
        raise AssertionError("Local HomeKit-style consumer did not receive video and audio")

    def close(self):
        self.process.terminate()
        try:
            self.process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate()
        self.video.close()
        self.audio.close()


def camera_packets(seconds=4):
    """Generate the real device's H264/PCM framing without accessing a device."""
    encoded = subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=20",
        "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-x264-params", "aud=1:keyint=20", "-f", "h264", "pipe:1"],
        capture_output=True, check=True, timeout=8)
    units = [b"\0\0\0\1\x09" + unit for unit in
             re.split(b"\x00\x00\x00\x01\x09", encoded.stdout)[1:]]
    packets, items = [], []
    # Vary packed seconds like real frames; normalization must preserve ticks.
    base_date = (26 << 26) | (10 << 22) | (31 << 17) | (12 << 12)
    for number, unit in enumerate(units):
        ms = number * 50
        frame = dhav(unit, 0xfd if number % 20 == 0 else 0xfc, number, ms,
                     b"\x80\0\x08\x06\x81\0\x02\x14", base_date + ms // 1000)
        items.append((ms, 96, frame))
    for number in range(seconds * 25):
        ms = number * 40
        payload = struct.pack("<640h", *[1000 if (n + number * 640) % 32 < 16 else -1000
                                         for n in range(640)])
        items.append((ms, 97, dhav(payload, 0xf0, number, ms,
                                 b"\x83\x01\x10\x04", base_date + ms // 1000)))
    for number, (_ms, pt, frame) in enumerate(sorted(items, key=lambda item: item[0])):
        split = max(1, len(frame) // 2)
        for body in (frame[:split], frame[split:]):
            packets.append((0 if pt == 96 else 2,
                            struct.pack("!BBHII", 128, pt, number, number * 1000, 1) + body))
    return packets


def synthetic_feed(packets, feeds, delay=.002):
    class Feed:
        def __init__(self, *_args, **_kwargs):
            self.sdp = b"v=0\r\na=rtpmap:96 H264/90000\r\na=rtpmap:97 L16/16000\r\n"
            self.closed = threading.Event()
            self.sock = SimpleNamespace(sock=Mock())
            self.index = 0
            feeds.append(self)

        def connect(self):
            return self

        def packet(self):
            if self.index >= len(packets):
                self.closed.wait(8)
                raise MediaError("Synthetic feed closed")
            packet = packets[self.index]
            self.index += 1
            self.closed.wait(delay)
            return packet

        def close(self):
            self.closed.set()
    return Feed


class _Hass:
    def __init__(self):
        self.loop = asyncio.get_running_loop()

    async def async_add_executor_job(self, function, *args):
        return await asyncio.to_thread(function, *args)


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hass = _Hass()
        self.coordinator = Mock()
        async def call(function, *args):
            return await asyncio.to_thread(function, *args)
        self.coordinator.async_call = call
        self.manager = DMSSMediaManager(self.hass, self.coordinator, idle_timeout=.05)
        self.manager.binary = shutil.which("ffmpeg") or "ffmpeg"

    async def asyncTearDown(self):
        await self.manager.async_close()

    async def test_urls_thumbnails_and_head_are_lazy_and_private(self):
        self.manager._acquire = Mock(side_effect=AssertionError("Must not open cloud"))
        url = await self.manager.async_stream_source("private-device-serial", 1)
        self.assertEqual(url, await self.manager.async_stream_source("private-device-serial", 1))
        self.assertNotIn("private-device-serial", url)
        self.assertIsNone(await self.manager.async_camera_image("private-device-serial", 1))
        response = await asyncio.to_thread(requests.head, url, timeout=2)
        self.assertEqual(response.status_code, 200)
        response = await asyncio.to_thread(requests.get, url + "/invalid", timeout=2)
        self.assertEqual(response.status_code, 404)
        response = await asyncio.to_thread(requests.get, url,
                                           headers={"Host": "foreign.invalid"}, timeout=2)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.manager._sessions, {})
        self.manager._acquire.assert_not_called()
        self.manager._set_snapshot(("private-device-serial", 1), b"full-resolution-jpeg")
        self.assertEqual(await self.manager.async_camera_image("private-device-serial", 1),
                         b"full-resolution-jpeg")

    async def test_two_viewers_share_upstream_and_last_detach_stops_it(self):
        starts = []
        def run(session):
            starts.append(session)
            session.ready.set()
            session.stop_event.wait(3)
            session.done.set()
        with patch.object(_Session, "_run", run):
            session, first = self.manager._subscribe(("test-only", 1))
            same, second = self.manager._subscribe(("test-only", 1))
            self.assertIs(session, same)
            self.assertTrue(await asyncio.to_thread(session.ready.wait, 1))
            self.assertEqual(len(starts), 1)
            session.publish(FLV_HEADER)
            self.assertEqual(first.get_nowait(), second.get_nowait())
            session.detach(first)
            await asyncio.sleep(.1)
            self.assertFalse(session.stop_event.is_set())
            session.detach(second)
            self.assertTrue(await asyncio.to_thread(session.done.wait, 1))
            again, subscriber = self.manager._subscribe(("test-only", 1))
            self.assertIsNot(again, session)
            again.detach(subscriber)

    async def test_slow_reader_and_ring_are_bounded(self):
        session = _Session(self.manager, ("test-only", 1))
        subscriber = queue.Queue(maxsize=1)
        session.clients.add(subscriber)
        session.publish(FLV_HEADER)
        session.publish(flv_tag(b"\x17\x01keyframe"))
        self.assertNotIn(subscriber, session.clients)
        self.assertIsNone(subscriber.get_nowait())
        for _ in range(1000):
            session.publish(flv_tag(b"\x27\x01" + b"x" * 32768))
        self.assertLessEqual(session.bootstrap.ring_bytes, 4 * 1024 * 1024)
        self.assertLessEqual(session.bootstrap.ring_chunks, 128)
        session.stop()

    async def test_cancelled_idle_timer_cannot_stop_reopened_or_newer_session(self):
        session = _Session(self.manager, ("test-only", 1))
        session.thread = Mock()  # Exercise attachment without opening any feed.
        with patch("custom_components.dmss.media.threading.Timer") as timer:
            first = session.attach()
            session.detach(first)
            _delay, old_callback, old_args = timer.call_args.args
            second = session.attach()
            # A Timer.cancel() cannot stop a callback which already started
            # and was blocked waiting for the session lock.
            old_callback(*old_args)
            self.assertFalse(session.stop_event.is_set())
            session.detach(second)
            _delay, current_callback, current_args = timer.call_args.args
            old_callback(*old_args)
            self.assertFalse(session.stop_event.is_set())
            current_callback(*current_args)
            self.assertTrue(session.stop_event.is_set())

    async def test_unload_closes_listener_and_session(self):
        url = await self.manager.async_stream_source("test-only", 2)
        server_thread = self.manager._server_thread
        await self.manager.async_close()
        self.assertFalse(server_thread.is_alive())
        with self.assertRaises(requests.ConnectionError):
            await asyncio.to_thread(requests.head, url, timeout=.2)
        with self.assertRaises(MediaError):
            await self.manager.async_stream_source("test-only", 2)

    async def test_main_stream_credentials_are_refreshed_without_secret_repr(self):
        client = self.coordinator.client
        client.select_device.return_value = {"playInfo": "encrypted-only"}
        lease = SimpleNamespace(url="http://example.invalid/signed-secret",
                                tls_url="https://example.invalid/signed-secret")
        with patch("custom_components.dmss.media.request_media", return_value=lease) as acquire, \
                patch("custom_components.dmss.media.decrypt_play_info", return_value={
                    "devLoginName": "private-user", "devLoginPassword": "private-password"}):
            result = self.manager._acquire("test-only", 2)
        acquire.assert_called_once_with(client, "test-only", 2, stream=0, protocol="http",
                                        route="legacy", encrypt_mode=0)
        self.assertTrue(result.tls)
        self.assertNotIn("private-password", repr(result))
        self.assertNotIn("signed-secret", repr(result))

    async def test_dhav_date_normalization_keeps_payload_and_ticks(self):
        frame = dhav(b"unaltered-encoded-payload", 0xf0, 4, 1240, b"\x83\x01\x10\x04", date=123)
        result = normalize_dhav_date(frame, 456)
        self.assertEqual(struct.unpack_from("<I", result, 16)[0], 456)
        self.assertEqual(result[20:23], frame[20:23])
        self.assertEqual(result[24:], frame[24:])
        self.assertEqual(result[23], sum(result[:23]) & 255)

    async def test_receive_eof_refreshes_credentials_without_restarting_consumer(self):
        old_frame = dhav(b"old-video-payload", 0xfd, 1, 1000, date=123)
        new_frame = dhav(b"new-video-payload", 0xfd, 2, 1040, date=124)
        def packet(body, pt):
            return 0, struct.pack("!BBHII", 128, pt, 1, 1, 1) + body
        old_feed = Mock(sdp=b"a=rtpmap:96 H264/90000\r\n")
        old_feed.packet.side_effect = [packet(old_frame, 96), packet(new_frame[:15], 96),
                                       MediaError("Connection ended with a private URL")]
        new_feed = Mock(sdp=b"a=rtpmap:98 H264/90000\r\n",
                        sock=SimpleNamespace(sock=Mock()))
        new_feed.packet.return_value = packet(new_frame, 98)
        session = _Session(self.manager, ("test-only", 2))
        session.feed = old_feed
        written = []
        def write(frame):
            written.append(frame)
            if len(written) == 2:
                session.stop_event.set()
        sink = Mock(write=Mock(side_effect=write))
        process = session.process = SimpleNamespace(stdin=sink, poll=lambda: 0)
        self.manager._acquire = Mock(return_value=_Credentials(
            "https://example.invalid/fresh-private-lease", "dummy", "dummy", True))
        with patch("custom_components.dmss.media.HTTPMediaFeed", return_value=new_feed) as factory, \
                patch("custom_components.dmss.media.subprocess.Popen") as spawn:
            await asyncio.wait_for(asyncio.to_thread(session._pump), 3)
        self.assertIs(session.process, process)
        spawn.assert_not_called()
        self.manager._acquire.assert_called_once_with("test-only", 2)
        factory.assert_called_once_with("https://example.invalid/fresh-private-lease",
                                       "dummy", "dummy", timeout=10, tls=True)
        new_feed.connect.assert_called_once()
        old_feed.close.assert_called_once()
        self.assertEqual(session.reconnects, 1)
        self.assertEqual(session.video_frames, 2)
        self.assertEqual(len(written), 2)
        self.assertEqual(written[0], old_frame)
        self.assertEqual(written[1][24:], new_frame[24:])
        self.assertEqual(struct.unpack_from("<I", written[1], 16)[0], 123)
        self.assertIsNone(session.error)
        sink.close.assert_called_once()
        session.stop()

    async def test_stop_during_reconnect_backoff_opens_no_new_connection(self):
        failed = threading.Event()
        old_feed = Mock(sdp=b"a=rtpmap:96 H264/90000\r\n")
        old_feed.packet.side_effect = MediaError("Synthetic EOF")
        old_feed.close.side_effect = failed.set
        session = _Session(self.manager, ("test-only", 1))
        session.feed = old_feed
        session.process = SimpleNamespace(stdin=Mock(), poll=lambda: 0)
        self.manager._acquire = Mock(side_effect=AssertionError("No reconnect after stop"))
        pump = asyncio.create_task(asyncio.to_thread(session._pump))
        self.assertTrue(await asyncio.to_thread(failed.wait, 1))
        session.stop_event.set()
        await asyncio.wait_for(pump, 1)
        self.manager._acquire.assert_not_called()
        self.assertEqual(session.reconnects, 0)
        self.assertIsNone(session.error)
        session.stop()

    async def test_failed_recovery_has_two_attempts_and_fixed_error(self):
        session = _Session(self.manager, ("test-only", 1))
        session.feed = Mock(sdp=b"a=rtpmap:96 H264/90000\r\n")
        session.feed.packet.side_effect = MediaError("Synthetic EOF")
        session.process = SimpleNamespace(stdin=Mock(), poll=lambda: 0)
        self.manager._acquire = Mock(side_effect=MediaError("private signed URL or token"))
        await asyncio.wait_for(asyncio.to_thread(session._pump), 4)
        self.assertEqual(self.manager._acquire.call_count, 2)
        self.assertEqual(session.error, "Camera media connection could not be recovered")
        self.assertNotIn("private", session.error)
        session.stop()

    async def test_account_verification_stops_recovery_and_requests_reauth_once(self):
        session = _Session(self.manager, ("test-only", 1))
        session.feed = Mock(sdp=b"a=rtpmap:96 H264/90000\r\n")
        session.feed.packet.side_effect = MediaError("Synthetic EOF")
        session.process = SimpleNamespace(stdin=Mock(), poll=lambda: 0)
        self.manager._acquire = Mock(side_effect=APIError("DC00050531", 200, "/login"))
        await asyncio.wait_for(asyncio.to_thread(session._pump), 3)
        await asyncio.sleep(0)
        self.assertEqual(self.manager._acquire.call_count, 1)
        self.coordinator.entry.async_start_reauth.assert_called_once_with(self.hass)
        self.assertEqual(session.error, "DMSS account needs authentication")
        session.stop()

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools unavailable")
    async def test_loopback_outputs_original_h264_aac_and_cached_snapshot(self):
        packets = await asyncio.to_thread(camera_packets)
        feeds = []
        Feed = synthetic_feed(packets, feeds)
        self.manager._acquire = Mock(return_value=_Credentials("http://example.invalid/test", "dummy", "dummy", False))
        url = await self.manager.async_stream_source("test-only", 1)
        captured = bytearray()
        def read():
            with requests.get(url, stream=True, timeout=(3, 5)) as response:
                self.assertEqual(response.status_code, 200)
                for block in response.iter_content(4096):
                    captured.extend(block)
                    if len(captured) >= 16000:
                        break
        with patch("custom_components.dmss.media.HTTPMediaFeed", Feed):
            await asyncio.wait_for(asyncio.to_thread(read), 8)
            snapshot = None
            for _ in range(30):
                snapshot = await self.manager.async_camera_image("test-only", 1)
                if snapshot:
                    break
                await asyncio.sleep(.05)
            self.assertIsNotNone(snapshot)
            self.assertTrue(snapshot.startswith(b"\xff\xd8"))
            await self.manager.async_close()
        self.assertEqual(len(feeds), 1)
        self.assertTrue(feeds[0].closed.is_set())
        session = self.manager._sessions[("test-only", 1)]
        self.assertGreater(session.video_frames, 0)
        self.assertGreater(session.audio_frames, 0)
        self.assertIsNotNone(session.process.poll())
        result = await asyncio.to_thread(subprocess.run, [
            "ffprobe", "-v", "error", "-show_streams", "-show_packets", "-of", "json", "pipe:0"],
            input=bytes(captured), capture_output=True, timeout=5, check=True)
        data = json.loads(result.stdout)
        streams = {stream["codec_type"]: stream for stream in data["streams"]}
        self.assertEqual(streams["video"]["codec_name"], "h264")
        self.assertEqual((streams["video"]["width"], streams["video"]["height"]), (64, 48))
        self.assertEqual(streams["audio"]["codec_name"], "aac")
        # HA uses PyAV rather than the HomeKit FFmpeg process. The same local
        # container must remain decodable by HA's installed libav version.
        with av.open(io.BytesIO(captured)) as container:
            frames = list(container.decode(video=0))
            self.assertGreater(len(frames), 5)
            self.assertEqual((frames[0].width, frames[0].height), (64, 48))
        audio_pts = [float(packet["pts_time"]) for packet in data["packets"]
                     if packet["codec_type"] == "audio" and "pts_time" in packet]
        self.assertGreater(len(audio_pts), 5)
        self.assertTrue(all(.063 <= second - first <= .065 for first, second in zip(audio_pts, audio_pts[1:])))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools unavailable")
    async def test_homekit_style_cold_join_and_reopen_deliver_decodable_video_and_opus(self):
        packets = await asyncio.to_thread(camera_packets, 12)
        feeds, viewers = [], []
        Feed = synthetic_feed(packets, feeds, delay=.005)
        self.manager._acquire = Mock(return_value=_Credentials("http://example.invalid/test", "dummy", "dummy", False))
        key = ("test-only", 1)
        url = await self.manager.async_stream_source(*key)
        async def view():
            viewer = await asyncio.to_thread(_RTPViewer, url)
            viewers.append(viewer)
            first_video, bitstream, frames, audio_packets = await asyncio.to_thread(viewer.receive)
            self.assertLess(first_video, 5)
            self.assertGreaterEqual(frames, 12)
            self.assertGreater(audio_packets, 0)
            result = await asyncio.to_thread(subprocess.run, [
                "ffprobe", "-v", "error", "-f", "h264", "-count_frames",
                "-show_streams", "-of", "json", "pipe:0"], input=bitstream,
                capture_output=True, check=True, timeout=5)
            stream = json.loads(result.stdout)["streams"][0]
            self.assertEqual((stream["width"], stream["height"]), (64, 48))
            self.assertGreaterEqual(int(stream["nb_read_frames"]), 10)
            return viewer
        try:
            with patch("custom_components.dmss.media.HTTPMediaFeed", Feed):
                first = await view()
                second = await view()
                self.assertEqual(len(feeds), 1)
                old_session = self.manager._sessions[key]
                await asyncio.to_thread(first.close)
                viewers.remove(first)
                await asyncio.to_thread(second.close)
                viewers.remove(second)
                self.assertTrue(await asyncio.to_thread(old_session.done.wait, 2))
                await view()
                self.assertEqual(len(feeds), 2)
                self.assertIsNot(self.manager._sessions[key], old_session)
                self.assertTrue(feeds[0].closed.is_set())
        finally:
            for viewer in viewers:
                await asyncio.to_thread(viewer.close)
            await self.manager.async_close()


if __name__ == "__main__":
    unittest.main()
