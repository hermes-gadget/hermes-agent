"""Playback priming, bounded buffering and local interruption."""
import threading
import time

class LivePlaybackBuffer:
    """Bounded, thread-safe PCM buffer for paced 20 ms playback."""

    def __init__(self, *, prime_ms: int = 120, mute_ms: int = 250, frame_bytes: int = 960, sample_alignment: int = 2):
        self._frame_bytes = frame_bytes
        self._alignment = sample_alignment
        self._lock = threading.Lock()
        self._pcm = bytearray()
        self._prime_bytes = max(self._frame_bytes, (prime_ms + 19) // 20 * self._frame_bytes)
        self._limit = max(self._prime_bytes, 100 * self._frame_bytes)
        self._mute_ms = mute_ms
        self._muted_until = 0.0
        self._primed = False
        self.generation = 0
        self._first_feed_at = None

    def feed(self, pcm: bytes) -> None:
        with self._lock:
            if time.monotonic() < self._muted_until:
                return
            if pcm and not self._pcm:
                self._first_feed_at = time.monotonic()
            self._pcm.extend(pcm)
            overflow = len(self._pcm) - self._limit
            if overflow > 0:
                del self._pcm[:(overflow + self._alignment - 1) // self._alignment * self._alignment]

    def read_frame(self) -> bytes | None:
        with self._lock:
            if not self._primed:
                self._primed = (len(self._pcm) >= self._prime_bytes or bool(
                    self._pcm and self._first_feed_at is not None
                    and time.monotonic() - self._first_feed_at >= self._prime_bytes / self._frame_bytes * 0.02
                ))
            if not self._primed:
                return None
            if len(self._pcm) < self._frame_bytes:
                frame = bytes(self._pcm) if self._pcm else None
                self._pcm.clear()
                self._primed = False
                return frame.ljust(self._frame_bytes, b"\x00") if frame else None
            frame = bytes(self._pcm[:self._frame_bytes])
            del self._pcm[:self._frame_bytes]
            return frame

    def flush(self) -> None:
        with self._lock:
            self._pcm.clear()
            self._primed = False
            self.generation += 1
            self._first_feed_at = None
            self._muted_until = time.monotonic() + self._mute_ms / 1000

    @property
    def active(self) -> bool:
        with self._lock:
            return bool(self._pcm)
