"""Phone audio on the PC speakers.

The phone streams its system audio as RTP: one raw AAC-ELD frame (480 samples, 10 ms, 48 kHz
stereo) per packet. Two details make FFmpeg decode it:
  * the phone's AudioSpecificConfig (F8 E6 40 00) says 512-sample frames, but the frames hold
    480; flipping frameLengthFlag gives F8 E6 50 00, which FFmpeg's AAC-ELD decoder accepts;
  * the decoder needs the output sample rate / layout set explicitly.
Decoded audio goes through a small jitter buffer to the default output device (sounddevice).
"""

from __future__ import annotations

import collections
import logging
import threading
from typing import Any, Optional

logger = logging.getLogger("phonyscreen.audio")

ASC_ELD_48K_STEREO_480 = bytes([0xF8, 0xE6, 0x50, 0x00])
RATE = 48000
FRAME_SAMPLES = 480
BYTES_PER_SAMPLE = 4  # s16 stereo
START_BUFFER_MS = 40  # wait for this much audio before starting (absorbs network jitter)
MAX_BUFFER_MS = 200  # drop the oldest audio beyond this, so sound never lags the picture


class AacEldDecoder:
    """One raw AAC-ELD frame in, interleaved s16le stereo PCM out."""

    def __init__(self) -> None:
        import av

        self._av = av
        self._ctx = av.codec.CodecContext.create("aac", "r")
        self._ctx.extradata = ASC_ELD_48K_STEREO_480
        self._ctx.sample_rate = RATE
        self._ctx.layout = "stereo"
        self._resampler = av.AudioResampler(format="s16", layout="stereo", rate=RATE)

    def decode(self, frame: bytes) -> bytes:
        out = bytearray()
        for f in self._ctx.decode(self._av.Packet(frame)):
            f.pts = None  # resample by sample count, not timestamps (keeps the length exact)
            for r in self._resampler.resample(f):
                out += bytes(r.planes[0])[: r.samples * BYTES_PER_SAMPLE]
        return bytes(out)


class JitterBuffer:
    """Thread-safe PCM FIFO between the network side and the audio callback."""

    def __init__(self) -> None:
        self._chunks: collections.deque[bytes] = collections.deque()
        self._size = 0
        self._lock = threading.Lock()
        self._started = False
        self.underruns = 0
        self.dropped_ms = 0

    @staticmethod
    def _ms(nbytes: int) -> float:
        return nbytes / BYTES_PER_SAMPLE / RATE * 1000

    def push(self, pcm: bytes) -> None:
        with self._lock:
            self._chunks.append(pcm)
            self._size += len(pcm)
            while self._chunks and self._ms(self._size) > MAX_BUFFER_MS:
                old = self._chunks.popleft()
                self._size -= len(old)
                self.dropped_ms += round(self._ms(len(old)))

    def pull(self, nbytes: int) -> bytes:
        """Exactly ``nbytes`` of audio (silence where none is available)."""
        with self._lock:
            if not self._started:
                if self._ms(self._size) < START_BUFFER_MS:
                    return bytes(nbytes)
                self._started = True
            out = bytearray()
            while len(out) < nbytes and self._chunks:
                chunk = self._chunks[0]
                take = min(len(chunk), nbytes - len(out))
                out += chunk[:take]
                if take == len(chunk):
                    self._chunks.popleft()
                else:
                    self._chunks[0] = chunk[take:]
                self._size -= take
            if len(out) < nbytes:
                self.underruns += 1
                self._started = False  # refill before resuming, instead of crackling
                out += bytes(nbytes - len(out))
            return bytes(out)

    def clear(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._size = 0
            self._started = False


class AudioPlayer:
    """Plays pushed PCM on the default output device."""

    def __init__(self) -> None:
        self.buffer = JitterBuffer()
        self.enabled = True
        self._stream: Optional[Any] = None

    def start(self) -> bool:
        try:
            import sounddevice as sd

            def callback(outdata, frames, _time, _status) -> None:
                outdata[:] = self.buffer.pull(frames * BYTES_PER_SAMPLE)

            self._stream = sd.RawOutputStream(
                samplerate=RATE, channels=2, dtype="int16", blocksize=FRAME_SAMPLES, latency="low", callback=callback
            )
            self._stream.start()
            return True
        except Exception as e:
            logger.warning("no audio output: %s", e)
            self._stream = None
            return False

    @property
    def ok(self) -> bool:
        """Playing to a real output device."""
        return self._stream is not None

    def push(self, pcm: bytes) -> None:
        if self.enabled and self._stream is not None:
            self.buffer.push(pcm)

    def set_enabled(self, on: bool) -> None:
        self.enabled = on
        if not on:
            self.buffer.clear()

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
