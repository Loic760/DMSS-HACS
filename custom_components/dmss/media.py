"""On-demand, shared cloud camera transport for HA and HomeKit.

The loopback URL contains only a random capability, never a DMSS credential.
An HTTP GET starts one cloud connection per camera. Video is copied unchanged;
only received audio is converted to AAC for Home Assistant's stream component.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import queue
import re
import secrets
import struct
import subprocess
import threading
from urllib.parse import urlsplit

from .client import decrypt_play_info
from .cloud_media import request_media
from .errors import is_auth_error
from .media_buffer import CHUNK_SIZE, FLVBootstrap, chunks
from .media_protocol import DHAVFrames, HTTPMediaFeed, MediaError, rtp_payload

_END = None


def normalize_dhav_date(frame, date):
    """Keep native millisecond ticks continuous across wall-clock seconds.

    FFmpeg's DHAV demuxer resets each track's PTS to whole seconds whenever
    the packed date changes. A constant session date avoids those PTS jumps.
    This edits only container metadata, never the encoded audio/video payload.
    """
    header = bytearray(frame[:24])
    struct.pack_into("<I", header, 16, date)
    header[23] = sum(header[:23]) & 255
    return bytes(header) + frame[24:]


@dataclass(repr=False)
class _Credentials:
    url: str = field(repr=False)
    username: str = field(repr=False)
    password: str = field(repr=False)
    tls: bool


class _Session:
    def __init__(self, manager, key):
        self.manager, self.key = manager, key
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.done = threading.Event()
        self.ready = threading.Event()
        self.clients = set()
        self.bootstrap = FLVBootstrap()
        self.idle_timer = None
        self._idle_generation = 0
        self.thread = None
        self.feed = None
        self.process = None
        self.future = None
        self.error = None
        self.video_frames = 0
        self.audio_frames = 0
        self.reconnects = 0
        self._reauth_requested = False

    def attach(self):
        subscriber = queue.Queue(maxsize=256)
        with self.lock:
            if self.done.is_set() or self.stop_event.is_set():
                raise MediaError("Camera session has ended")
            if self.idle_timer:
                self.idle_timer.cancel()
                self.idle_timer = None
            self._idle_generation += 1
            self.clients.add(subscriber)
            for chunk in self.bootstrap.bootstrap():
                subscriber.put_nowait(chunk)
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, name="dmss-camera", daemon=True)
                self.thread.start()
        return subscriber

    def detach(self, subscriber):
        with self.lock:
            self.clients.discard(subscriber)
            if not self.clients and not self.stop_event.is_set() and self.idle_timer is None:
                self._idle_generation += 1
                self.idle_timer = threading.Timer(
                    self.manager.idle_timeout, self._stop_if_idle, (self._idle_generation,))
                self.idle_timer.daemon = True
                self.idle_timer.start()

    def _stop_if_idle(self, generation):
        """A cancelled timer already awaiting the lock must not stop a reopen."""
        with self.lock:
            if self.clients or generation != self._idle_generation:
                return
            self.idle_timer = None
            self.stop()

    @staticmethod
    def _end(subscriber):
        try:
            subscriber.put_nowait(_END)
        except queue.Full:
            try:
                subscriber.get_nowait()
            except queue.Empty:
                pass
            subscriber.put_nowait(_END)

    def publish(self, chunk):
        if not chunk:
            return
        slow = set()
        with self.lock:
            for packet in self.bootstrap.feed(chunk):
                for block in chunks(packet):
                    for subscriber in tuple(self.clients):
                        try:
                            subscriber.put_nowait(block)
                        except queue.Full:
                            self._end(subscriber)
                            self.clients.discard(subscriber)
                            slow.add(subscriber)
        for subscriber in slow:
            self.detach(subscriber)

    def _pump(self):
        assemblers = {}
        session_date = None
        # Only advertised receive video/audio types; the stream-assist and
        # control tracks must never reach FFmpeg's DHAV demuxer.
        allowed = self._receive_types()
        remaining_attempts = 2
        try:
            while not self.stop_event.is_set():
                try:
                    channel, packet = self.feed.packet()
                    if channel == 255:
                        continue
                    if len(packet) >= 12 and packet[0] >> 6 == 2:
                        payload_type = packet[1] & 127
                        if payload_type not in allowed:
                            continue
                        data = rtp_payload(packet, payload_type)
                        track = payload_type
                    else:
                        data, track = packet, ("private", channel)
                    parser = assemblers.setdefault(track, DHAVFrames())
                    frames = parser.feed(data)
                except (MediaError, OSError):
                    recovered, remaining_attempts = self._recover_feed(remaining_attempts)
                    if not recovered:
                        break
                    # Partial packets belong to the old connection. Preserve
                    # only the session date and the running FFmpeg consumer.
                    assemblers = {}
                    allowed = self._receive_types()
                    continue
                for frame in frames:
                    if session_date is None:
                        session_date = struct.unpack_from("<I", frame, 16)[0]
                    frame = normalize_dhav_date(frame, session_date)
                    if frame[4] == 0xf0:
                        self.audio_frames += 1
                    elif frame[4] in (0xfc, 0xfd):
                        self.video_frames += 1
                    self.process.stdin.write(frame)
                    self.process.stdin.flush()
                    # A healthy media frame permits recovery from a later,
                    # independent expiry. Bad/empty reconnects share one budget.
                    remaining_attempts = 2
        except Exception:
            if not self.stop_event.is_set():
                self.error = "Camera media connection ended"
        finally:
            try:
                self.process.stdin.close()
            except (OSError, ValueError):
                pass

    def _receive_types(self):
        return {int(pt) for pt in re.findall(
            rb"a=rtpmap:(\d+) (?:H264|H265|HEVC|L16|PCMA|PCMU|AAC|MPEG4-GENERIC)/",
            self.feed.sdp,
        )}

    def _open_feed(self):
        """Reload independently renewable credentials before each connection."""
        if self.stop_event.is_set():
            return False
        self.future = asyncio.run_coroutine_threadsafe(
            self.manager.coordinator.async_call(self.manager._acquire, *self.key),
            self.manager.hass.loop,
        )
        credentials = self.future.result(timeout=30)
        if self.stop_event.is_set():
            return False
        feed = HTTPMediaFeed(credentials.url, credentials.username, credentials.password,
                             timeout=10, tls=credentials.tls)
        # Publish before connecting so stop() can close a pending handshake.
        self.feed = feed
        try:
            feed.connect()
            feed.sock = feed.sock.sock  # Aggregate deadline bounds startup only.
            feed.sock.settimeout(10)
            if self.stop_event.is_set():
                feed.close()
                return False
            return True
        except Exception:
            feed.close()
            raise

    def _request_reauth(self):
        self.error = "DMSS account needs authentication"
        if not self._reauth_requested:
            self._reauth_requested = True
            self.manager.hass.loop.call_soon_threadsafe(
                self.manager.coordinator.entry.async_start_reauth, self.manager.hass)

    def _recover_feed(self, remaining):
        """Keep the same local consumer through a bounded receive reconnect."""
        if self.feed:
            self.feed.close()
        while remaining > 0:
            delay = .5 if remaining == 2 else 1.0
            remaining -= 1
            if self.stop_event.wait(delay):
                return False, remaining
            try:
                if not self._open_feed():
                    return False, remaining
                self.reconnects += 1
                self.error = None
                return True, remaining
            except Exception as error:
                if self.stop_event.is_set():
                    return False, remaining
                if is_auth_error(error):
                    self._request_reauth()
                    return False, remaining
        if not self.stop_event.is_set():
            self.error = "Camera media connection could not be recovered"
        return False, remaining

    def _snapshots(self, fd):
        pending = bytearray()
        try:
            with os.fdopen(fd, "rb", buffering=0) as images:
                while not self.stop_event.is_set():
                    chunk = images.read(65536)
                    if not chunk:
                        return
                    pending.extend(chunk)
                    if len(pending) > 4_000_000:
                        return
                    while True:
                        begin = pending.find(b"\xff\xd8")
                        end = pending.find(b"\xff\xd9", max(0, begin + 2)) if begin >= 0 else -1
                        if end < 0:
                            break
                        image = bytes(pending[begin:end + 2])
                        del pending[:end + 2]
                        self.manager._set_snapshot(self.key, image)
        except (OSError, ValueError):
            return

    def _discard_errors(self):
        try:
            while self.process.stderr.read1(4096):
                pass
        except (OSError, ValueError):
            pass

    def _run(self):
        jpeg_read = jpeg_write = None
        threads = []
        try:
            if not self._open_feed():
                return
            jpeg_read, jpeg_write = os.pipe()
            command = [
                self.manager.binary, "-hide_banner", "-loglevel", "error",
                "-probesize", "1000000", "-analyzeduration", "1000000",
                "-f", "dhav", "-i", "pipe:0",
                "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "96k", "-ar", "16000", "-ac", "1",
                # FLV announces H264/AAC codec configuration immediately;
                # HomeKit's unmodified FFmpeg otherwise probes TS for ~5s.
                "-flvflags", "no_duration_filesize", "-flush_packets", "1",
                "-f", "flv", "pipe:1",
                "-map", "0:v:0", "-an", "-vf", "fps=1/2", "-threads", "1",
                "-c:v", "mjpeg", "-q:v", "1", "-f", "image2pipe", f"pipe:{jpeg_write}",
            ]
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, pass_fds=(jpeg_write,))
            os.close(jpeg_write)
            jpeg_write = None
            snapshot_thread = threading.Thread(target=self._snapshots, args=(jpeg_read,), daemon=True)
            jpeg_read = None  # The snapshot thread owns it now.
            threads = [snapshot_thread,
                       threading.Thread(target=self._discard_errors, daemon=True),
                       threading.Thread(target=self._pump, daemon=True)]
            for thread in threads:
                thread.start()
            self.ready.set()
            self.manager._set_error(self.key, None)
            while not self.stop_event.is_set():
                chunk = self.process.stdout.read1(CHUNK_SIZE)
                if not chunk:
                    break
                self.publish(chunk)
            if not self.stop_event.is_set() and not self.error:
                self.error = "Camera media conversion ended"
        except Exception as error:
            if not self.stop_event.is_set():
                self.error = "DMSS account needs authentication" if is_auth_error(error) else "Unable to start camera media"
                if is_auth_error(error):
                    self._request_reauth()
        finally:
            self.ready.set()
            self.stop()
            for fd in (jpeg_read, jpeg_write):
                if fd is not None:
                    os.close(fd)
            for thread in threads:
                if thread is not threading.current_thread():
                    thread.join(timeout=2)
            self._close_process_pipes()
            with self.lock:
                for subscriber in self.clients:
                    self._end(subscriber)
                self.clients.clear()
            self.manager._set_error(self.key, self.error)
            self.done.set()

    def _close_process_pipes(self):
        if self.process is None:
            return
        for pipe in (self.process.stdout, self.process.stderr):
            try:
                pipe.close()
            except (OSError, ValueError):
                pass

    def stop(self):
        with self.lock:
            self.stop_event.set()
            if self.idle_timer:
                self.idle_timer.cancel()
                self.idle_timer = None
            if self.future and not self.future.done():
                self.future.cancel()
            if self.feed:
                self.feed.close()
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=1)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _route(self):
        host = self.headers.get("Host", "")
        if host != f"127.0.0.1:{self.server.server_port}":
            return None
        return self.server.manager._routes.get(urlsplit(self.path).path)

    def do_HEAD(self):
        if self._route() is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "video/x-flv")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self):
        key = self._route()
        if key is None:
            self.send_error(404)
            return
        session = subscriber = None
        try:
            session, subscriber = self.server.manager._subscribe(key)
            if not session.ready.wait(timeout=30) or session.error:
                self.send_error(503, "Camera media unavailable")
                return
            self.send_response(200)
            self.send_header("Content-Type", "video/x-flv")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            while not self.server.manager.closed:
                try:
                    chunk = subscriber.get(timeout=1)
                except queue.Empty:
                    if session.done.is_set():
                        break
                    continue
                if chunk is _END:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, ValueError, MediaError):
            pass
        finally:
            if session is not None and subscriber is not None:
                session.detach(subscriber)
            self.close_connection = True


class DMSSMediaManager:
    """Lazy local endpoint, one upstream per door, shared by all its viewers."""

    def __init__(self, hass, coordinator, *, idle_timeout=1.0):
        self.hass, self.coordinator = hass, coordinator
        self.idle_timeout = idle_timeout
        self.closed = False
        self.lock = threading.RLock()
        self._server = None
        self._server_thread = None
        self._routes = {}
        self._urls = {}
        self._sessions = {}
        self._snapshots = {}
        self._errors = {}
        self.binary = None
        self._capability = secrets.token_urlsafe(32)

    def _acquire(self, serial, channel):
        client = self.coordinator.client
        lease = request_media(client, serial, channel, stream=0, protocol="http",
                              route="legacy", encrypt_mode=0)
        device = client.select_device(serial)
        info = decrypt_play_info(device["playInfo"], serial)
        secure = bool(lease.tls_url)
        return _Credentials(lease.tls_url if secure else lease.url,
                            info["devLoginName"], info["devLoginPassword"], secure)

    def _ensure_server(self):
        with self.lock:
            if self.closed:
                raise MediaError("Camera manager is closed")
            if self._server is None:
                self._server = _Server(("127.0.0.1", 0), _Handler)
                self._server.manager = self
                self._server_thread = threading.Thread(
                    target=self._server.serve_forever, kwargs={"poll_interval": .2}, daemon=True)
                self._server_thread.start()

    async def async_stream_source(self, serial, channel):
        if channel not in (1, 2):
            raise MediaError("Unknown entrance camera")
        if self.binary is None:
            from homeassistant.components.ffmpeg import get_ffmpeg_manager
            self.binary = get_ffmpeg_manager(self.hass).binary
        await self.hass.async_add_executor_job(self._ensure_server)
        key = (serial, channel)
        with self.lock:
            if key not in self._urls:
                path = f"/{self._capability}/{secrets.token_urlsafe(12)}.flv"
                self._routes[path] = key
                self._urls[key] = f"http://127.0.0.1:{self._server.server_port}{path}"
            return self._urls[key]

    async def async_camera_image(self, serial, channel):
        with self.lock:
            return self._snapshots.get((serial, channel))

    def last_error(self, serial, channel):
        with self.lock:
            return self._errors.get((serial, channel))

    def _set_snapshot(self, key, image):
        with self.lock:
            self._snapshots[key] = image

    def _set_error(self, key, error):
        with self.lock:
            self._errors[key] = error

    def _subscribe(self, key):
        with self.lock:
            if self.closed:
                raise MediaError("Camera manager is closed")
            session = self._sessions.get(key)
            if session is None or session.stop_event.is_set() or session.done.is_set():
                session = self._sessions[key] = _Session(self, key)
            subscriber = session.attach()
            return session, subscriber

    def _close(self):
        with self.lock:
            self.closed = True
            sessions = list(self._sessions.values())
            server, thread = self._server, self._server_thread
        for session in sessions:
            session.stop()
        if server:
            server.shutdown()
            server.server_close()
        if thread:
            thread.join(timeout=2)
        for session in sessions:
            if session.thread and session.thread is not threading.current_thread():
                session.thread.join(timeout=5)

    async def async_close(self):
        await self.hass.async_add_executor_job(self._close)
