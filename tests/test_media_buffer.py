"""Decoder-based late-join regression tests; all video is generated offline."""
import json
import shutil
import subprocess
import unittest

from custom_components.dmss.media_buffer import FLVBootstrap, CHUNK_SIZE
from custom_components.dmss.media_protocol import MediaError


class BufferTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools unavailable")
    def test_fragmented_input_late_join_decodes_latest_gop_with_audio(self):
        original = subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=20",
            "-f", "lavfi", "-i", "sine=frequency=800:sample_rate=16000",
            "-t", "4", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-g", "20", "-sc_threshold", "0", "-c:a", "aac", "-ac", "1",
            "-flvflags", "no_duration_filesize", "-f", "flv", "pipe:1"],
            capture_output=True, check=True, timeout=8).stdout
        cache = FLVBootstrap()
        emitted = []
        for offset in range(0, len(original), 7):
            emitted.extend(cache.feed(original[offset:offset + 7]))
        self.assertEqual(b"".join(emitted), original)
        bootstrap = b"".join(cache.bootstrap())
        self.assertLess(len(bootstrap), len(original))
        probe = subprocess.run([
            "ffprobe", "-v", "error", "-show_streams", "-show_frames", "-of", "json", "pipe:0"],
            input=bootstrap, capture_output=True, check=True, timeout=5)
        decoded = json.loads(probe.stdout)
        streams = {stream["codec_type"]: stream for stream in decoded["streams"]}
        self.assertEqual(streams["video"]["codec_name"], "h264")
        self.assertEqual((streams["video"]["width"], streams["video"]["height"]), (64, 48))
        self.assertEqual(streams["audio"]["codec_name"], "aac")
        frames = [frame for frame in decoded["frames"] if frame["media_type"] == "video"]
        self.assertEqual(len(frames), 20)
        self.assertEqual(frames[0]["key_frame"], 1)
        self.assertGreaterEqual(float(frames[0]["pts_time"]), 3)
        # Verify actual pictures, not just parsable headers, and no stale GOP.
        def hashes(data):
            result = subprocess.run([
                "ffmpeg", "-v", "error", "-i", "pipe:0", "-map", "0:v:0",
                "-f", "framemd5", "pipe:1"], input=data, capture_output=True, check=True, timeout=5)
            return [line.rsplit(b",", 1)[-1] for line in result.stdout.splitlines()
                    if line and not line.startswith(b"#")]
        self.assertEqual(hashes(bootstrap), hashes(original)[-20:])

    def test_oversized_gop_is_not_replayed_as_a_partial_gop(self):
        def tag(payload):
            header = b"\x09" + len(payload).to_bytes(3, "big") + b"\0" * 7
            return header + payload + (len(payload) + 11).to_bytes(4, "big")
        header = b"FLV\x01\x01\0\0\0\x09\0\0\0\0"
        cache = FLVBootstrap()
        list(cache.feed(header + tag(b"\x17\x01first-keyframe")))
        for _ in range(140):
            list(cache.feed(tag(b"\x27\x01" + b"x" * CHUNK_SIZE)))
        self.assertEqual(b"".join(cache.bootstrap()), header)
        fresh = tag(b"\x17\x01fresh-keyframe")
        list(cache.feed(fresh))
        self.assertEqual(b"".join(cache.bootstrap()), header + fresh)

    def test_invalid_header_and_oversized_frame_fail_without_buffer_growth(self):
        with self.assertRaises(MediaError):
            list(FLVBootstrap().feed(b"invalid-header"))
        cache = FLVBootstrap()
        list(cache.feed(b"FLV\x01\x01\0\0\0\x09\0\0\0\0"))
        with self.assertRaises(MediaError):
            list(cache.feed(b"\x09\xff\xff\xff" + b"\0" * 7))
        self.assertLessEqual(len(cache.pending), 11)


if __name__ == "__main__":
    unittest.main()
