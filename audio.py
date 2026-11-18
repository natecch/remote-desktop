"""Sound from this computer, for viewers who turn sound on.

Records what the speakers are playing (Windows: WASAPI loopback; Linux: the PulseAudio
or PipeWire monitor; macOS needs a loopback device such as BlackHole) with the
`soundcard` library, and hands out 20 ms chunks of 16-bit stereo PCM at 24 kHz
(about 770 kbit/s while something plays; silence is not sent).

One recording thread serves every listener; it runs only while someone listens.
"""

import threading
import warnings

import numpy as np

RATE, CHANNELS = 24000, 2      # what viewers receive
CAPTURE_RATE = 48000           # what is recorded (halved by averaging sample pairs)
BLOCK = CAPTURE_RATE // 50     # 20 ms
SILENCE = 1e-4                 # peak below this counts as silence

try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import soundcard
    IMPORT_ERROR = None
except Exception as exc:  # missing library, or no audio system
    soundcard = None
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


class Capture:
    def __init__(self):
        self.listeners = {}  # key -> callback(bytes)
        self.lock = threading.Lock()
        self.thread = None
        self.error = None

    def problem(self):
        """Why sound cannot be captured here, or None."""
        if soundcard is None:
            return f"Sound is not available on this computer ({IMPORT_ERROR})."
        try:
            speaker = soundcard.default_speaker()
            soundcard.get_microphone(id=str(speaker.name), include_loopback=True)
            return None
        except Exception as exc:
            return f"This computer's sound output cannot be recorded ({exc})."

    def add(self, key, callback):
        with self.lock:
            self.listeners[key] = callback
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, name="audio", daemon=True)
                self.thread.start()

    def remove(self, key):
        with self.lock:
            self.listeners.pop(key, None)

    def _run(self):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # soundcard warns about discontinuities; harmless
                speaker = soundcard.default_speaker()
                mic = soundcard.get_microphone(id=str(speaker.name), include_loopback=True)
                with mic.recorder(samplerate=CAPTURE_RATE, channels=CHANNELS, blocksize=BLOCK) as rec:
                    while True:
                        with self.lock:
                            targets = list(self.listeners.values())
                            if not targets:
                                self.thread = None
                                return
                        data = rec.record(numframes=BLOCK)
                        if not len(data) or float(np.abs(data).max()) < SILENCE:
                            continue
                        chunk = encode(data)
                        for callback in targets:
                            try:
                                callback(chunk)
                            except Exception:
                                pass
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            with self.lock:
                self.thread = None


def encode(data):
    """float32 frames at 48 kHz (n x channels) -> int16 interleaved bytes at 24 kHz."""
    frames = data[: len(data) // 2 * 2]
    if frames.ndim == 1:
        frames = np.repeat(frames[:, None], CHANNELS, axis=1)
    elif frames.shape[1] == 1:
        frames = np.repeat(frames, CHANNELS, axis=1)
    halved = frames.reshape(-1, 2, frames.shape[1]).mean(axis=1)[:, :CHANNELS]
    return (np.clip(halved, -1.0, 1.0) * 32767).astype("<i2").tobytes()


CAPTURE = Capture()
