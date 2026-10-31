"""Bounded, decodable FLV bootstrap for late-joining local media consumers."""
from collections import deque

from .media_protocol import MediaError

CHUNK_SIZE = 32768
MAX_CACHE_BYTES = 4 * 1024 * 1024
MAX_CACHE_CHUNKS = 128


def chunks(packet):
    """Keep subscriber queues bounded in bytes as well as item count."""
    for offset in range(0, len(packet), CHUNK_SIZE):
        yield packet[offset:offset + CHUNK_SIZE]


class FLVBootstrap:
    """Retain codec headers and only the latest complete keyframe group.

    Arbitrarily replaying the tail of a container can omit codec headers or
    a keyframe, and can flood a new HomeKit viewer with old video. Parse tags
    across pipe boundaries so every new reader receives a decodable beginning.
    The caller serializes feed() and bootstrap() with its session lock.
    """

    def __init__(self):
        self.pending = bytearray()
        self.header = None
        self.prefix = {}
        self.ring = deque()
        self.ring_bytes = 0
        self.ring_chunks = 0

    def _clear_ring(self):
        self.ring.clear()
        self.ring_bytes = self.ring_chunks = 0

    def _remember(self, tag):
        kind, payload = tag[0] & 31, tag[11:-4]
        config = None
        if kind == 18:
            config = "metadata"
        elif kind == 8 and len(payload) >= 2 and payload[0] >> 4 == 10 and payload[1] == 0:
            config = "audio"
        elif kind == 9 and len(payload) >= 2 and payload[0] & 15 == 7 and payload[1] == 0:
            config = "video"
        if config:
            if len(tag) > 65536:
                raise MediaError("Local camera codec header exceeds the limit")
            if config in self.prefix and self.prefix[config] != tag:
                self._clear_ring()
            self.prefix[config] = tag
            return
        keyframe = (kind == 9 and len(payload) >= 2
                    and payload[0] >> 4 == 1 and payload[0] & 15 == 7 and payload[1] == 1)
        if keyframe:
            self._clear_ring()
        if keyframe or self.ring:
            self.ring.append(tag)
            self.ring_bytes += len(tag)
            self.ring_chunks += (len(tag) + CHUNK_SIZE - 1) // CHUNK_SIZE
            if self.ring_bytes > MAX_CACHE_BYTES or self.ring_chunks > MAX_CACHE_CHUNKS:
                # A partial GOP cannot initialize a decoder. Wait for the next
                # keyframe rather than dropping its beginning from the cache.
                self._clear_ring()

    def feed(self, data):
        """Yield complete header/tags, and update the bounded bootstrap cache."""
        self.pending.extend(data)
        if self.header is None:
            if len(self.pending) < 9:
                return
            if self.pending[:4] != b"FLV\x01":
                raise MediaError("Invalid local camera container header")
            size = int.from_bytes(self.pending[5:9], "big")
            if not 9 <= size <= 4096:
                raise MediaError("Invalid local camera container header size")
            if len(self.pending) < size + 4:
                return
            if self.pending[size:size + 4] != b"\0\0\0\0":
                raise MediaError("Invalid local camera initial tag size")
            self.header = bytes(self.pending[:size + 4])
            del self.pending[:size + 4]
            yield self.header
        while len(self.pending) >= 11:
            size = int.from_bytes(self.pending[1:4], "big") + 15
            if size > 4_000_000:
                raise MediaError("Local camera frame exceeds the limit")
            if len(self.pending) < size:
                return
            tag = bytes(self.pending[:size])
            del self.pending[:size]
            if int.from_bytes(tag[-4:], "big") != size - 4:
                raise MediaError("Invalid local camera tag size")
            self._remember(tag)
            yield tag

    def bootstrap(self):
        """Yield headers and the newest GOP as bounded HTTP write chunks."""
        if self.header is not None:
            yield self.header
        for tag in (*self.prefix.values(), *self.ring):
            yield from chunks(tag)
