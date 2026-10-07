"""Voice-activity detection (AgentOS V2 Phase 5.1): turns a stream of telephone PCM into bounded utterances.

``UtteranceDetector.feed`` takes Exotel media chunks of any size (linear16 mono 16 kHz), splits them into exact 20 ms
frames (640 bytes; a partial frame waits for the next chunk), classifies each with WebRTC VAD and returns the
utterances that completed:

* an utterance starts after ``start_frames`` consecutive voiced frames (those frames are kept: nothing is clipped);
* it ends after ``end_silence_frames`` consecutive unvoiced frames (default 600 ms), or is cut at
  ``max_utterance_frames`` (default 11 s);
* one shorter than ``min_speech_frames`` of voiced audio is discarded (a cough, a click);
* silence outside an utterance is never kept beyond the short start window, so prolonged silence costs nothing.

Memory is bounded by construction (``max_buffer_bytes``). While the assistant is speaking the detector is paused
(``listening = False``): incoming audio -- including the echo of our own playback -- is dropped, never transcribed.
``reset`` clears everything between turns. Nothing here stores or logs audio.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional, Protocol

from app.communication.voice.audio import FRAME_BYTES, SAMPLE_RATE


class VoiceActivity(Protocol):
    def is_speech(self, frame: bytes, sample_rate: int) -> bool: ...


@dataclass(frozen=True)
class VadConfig:
    aggressiveness: int = 2  # WebRTC VAD mode 0-3 (3 = most aggressive at filtering non-speech)
    start_frames: int = 3  # 60 ms of speech opens an utterance
    end_silence_frames: int = 30  # 600 ms of silence closes it
    min_speech_frames: int = 10  # 200 ms of voiced audio, else it is discarded
    max_utterance_frames: int = 550  # 11 s hard cut

    def __post_init__(self) -> None:
        if not 0 <= self.aggressiveness <= 3:
            raise ValueError("aggressiveness must be 0-3")
        if not 1 <= self.start_frames <= 25:
            raise ValueError("start_frames must be 1-25")
        if not 25 <= self.end_silence_frames <= 40:  # 500-800 ms
            raise ValueError("end_silence_frames must be 25-40 (500-800 ms)")
        if not 1 <= self.min_speech_frames <= 100:
            raise ValueError("min_speech_frames must be 1-100")
        if not 100 <= self.max_utterance_frames <= 600:  # 2-12 s
            raise ValueError("max_utterance_frames must be 100-600")

    @property
    def max_buffer_bytes(self) -> int:
        return (self.max_utterance_frames + self.start_frames) * FRAME_BYTES


def webrtc_vad(aggressiveness: int) -> VoiceActivity:
    import webrtcvad  # webrtcvad-wheels

    return webrtcvad.Vad(aggressiveness)


class UtteranceDetector:
    def __init__(self, config: Optional[VadConfig] = None, vad: Optional[VoiceActivity] = None) -> None:
        self.config = config or VadConfig()
        self.vad = vad or webrtc_vad(self.config.aggressiveness)
        self.listening = True
        self.reset()

    def reset(self) -> None:
        self._partial = b""
        self._window: Deque[bytes] = deque(maxlen=self.config.start_frames)  # candidate start (voiced frames)
        self._utterance: List[bytes] = []
        self._speaking = False
        self._voiced = 0
        self._silence = 0

    @property
    def buffered_bytes(self) -> int:
        return len(self._partial) + sum(map(len, self._window)) + sum(map(len, self._utterance))

    def pause(self) -> None:
        """Assistant playback started: drop input until ``resume`` (no echo is ever treated as speech)."""
        self.listening = False
        self.reset()

    def resume(self) -> None:
        self.listening = True
        self.reset()

    def feed(self, chunk: bytes) -> List[bytes]:
        if not self.listening or not chunk:
            return []
        data = self._partial + chunk
        whole = len(data) - len(data) % FRAME_BYTES
        self._partial = data[whole:]
        done: List[bytes] = []
        for start in range(0, whole, FRAME_BYTES):
            utterance = self._frame(data[start:start + FRAME_BYTES])
            if utterance is not None:
                done.append(utterance)
        return done

    def _frame(self, frame: bytes) -> Optional[bytes]:
        voiced = bool(self.vad.is_speech(frame, SAMPLE_RATE))
        if not self._speaking:
            if not voiced:
                self._window.clear()  # silence is discarded: only an unbroken voiced run can open an utterance
                return None
            self._window.append(frame)
            if len(self._window) < self.config.start_frames:
                return None
            self._speaking, self._utterance = True, list(self._window)
            self._voiced, self._silence = len(self._window), 0
            self._window.clear()
            return None
        self._utterance.append(frame)
        if voiced:
            self._voiced, self._silence = self._voiced + 1, 0
        else:
            self._silence += 1
        if self._silence >= self.config.end_silence_frames or len(self._utterance) >= self.config.max_utterance_frames:
            return self._close()
        return None

    def _close(self) -> Optional[bytes]:
        frames = self._utterance[:len(self._utterance) - self._silence] if self._silence else self._utterance
        voiced = self._voiced
        self._utterance, self._speaking, self._voiced, self._silence = [], False, 0, 0
        if voiced < self.config.min_speech_frames:
            return None
        return b"".join(frames)
