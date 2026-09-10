"""Server configuration and the transcriber factory.

The heavy transcriber import (faster-whisper, via tscribe's Transcriber) is done
*lazily* inside the factory so the app and tests can import without pulling the
whole ML stack. Tests override `transcriber_factory` with a mock.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import requests


@dataclass
class Settings:
    api_key: str = field(default_factory=lambda: os.environ.get("TSCRIBE_API_KEY", ""))
    # Chunk length in minutes. Kept at 12 (not 15) so a 16k/16-bit mono WAV chunk
    # stays under LiteLLM's non-configurable 25 MB multipart upload limit: a
    # 12-min chunk is ~22 MB worst case with the 10s snap window (a 15-min chunk
    # is ~28 MB and gets rejected, silently falling back to whisper-small).
    chunk_minutes: float = float(os.environ.get("TSCRIBE_CHUNK_MINUTES", "12"))
    workers: int = int(os.environ.get("TSCRIBE_WORKERS", "2"))
    snap_window_s: float = float(os.environ.get("TSCRIBE_SNAP_WINDOW_S", "10"))
    # Max bytes the server will accept for a direct upload or pull (sanity guard).
    max_audio_bytes: int = int(os.environ.get("TSCRIBE_MAX_AUDIO_BYTES", str(2 * 1024 ** 3)))
    download_timeout_s: int = int(os.environ.get("TSCRIBE_DOWNLOAD_TIMEOUT_S", "600"))
    # Per-chunk remote transcription read timeout. transcription_tool's
    # Transcriber hardcodes timeout=300, which the full (non-distil) whisper
    # model routinely exceeds on a 15-min chunk, causing a silent fallback to
    # local whisper-small (which loops catastrophically). Default 2100s = 35 min.
    transcription_timeout_s: int = int(os.environ.get("TSCRIBE_TRANSCRIPTION_TIMEOUT_S", "2100"))
    # Optional explicit path to the Drive service-account JSON. If empty,
    # server.drive falls back to $GOOGLE_APPLICATION_CREDENTIALS or ./credentials/*.json.
    drive_sa_json: str = field(default_factory=lambda: os.environ.get("CARNYX_DRIVE_SA_JSON", ""))


SETTINGS = Settings()


class TimedRemoteTranscriber:
    """Transcriber wrapper whose remote read timeout is configurable.

    transcription_tool.Transcriber hardcodes `requests.post(..., timeout=300)`
    with no env hook. Rather than patch the pinned dependency, mirror its
    `_transcribe_remote` here so the timeout can be tuned (default 35 min) to
    keep the full whisper model from tripping the read timeout and silently
    falling back to local whisper-small.
    """

    def __init__(self, timeout_s: int, **kwargs):
        self._timeout_s = timeout_s
        from transcription_tool.transcriber import Transcriber  # heavy, deferred

        self._inner = Transcriber(use_remote=True, **kwargs)

    def transcribe(self, audio_path):
        # Prefer this wrapper's remote path (configurable timeout). On failure,
        # fall back to local whisper exactly as the original Transcriber does,
        # without re-attempting remote.
        if (
            self._inner.use_remote
            and self._inner.transcription_api_base
            and self._inner.transcription_api_key
        ):
            try:
                return self._transcribe_remote(audio_path)
            except Exception as e:
                print(f"Remote transcription failed: {e}", file=sys.stderr)
                print("Falling back to local Whisper...", file=sys.stderr)
        model = self._inner._load_model()
        segments, info = model.transcribe(str(audio_path), language=self._inner.language)
        text = " ".join(seg.text for seg in segments)
        return text, info

    def _transcribe_remote(self, audio_path):
        url = f"{self._inner.transcription_api_base}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {self._inner.transcription_api_key}"}

        with open(audio_path, "rb") as f:
            files = {"file": (Path(audio_path).name, f, "audio/wav")}
            data = {
                "model": self._inner.transcription_model,
                "language": self._inner.language,
                "vad_filter": "true",
            }

            response = requests.post(
                url, headers=headers, files=files, data=data, timeout=self._timeout_s
            )
            response.raise_for_status()

            result = response.json()
            text = result.get("text", "")

        class MockInfo:
            language = self._inner.language
            language_probability = 1.0

        return text, MockInfo()


def default_transcriber_factory():
    """Build tscribe's real transcriber (carnyx-first via LiteLLM), wrapped so the
    per-chunk read timeout is configurable. Imported lazily so the FastAPI
    process needn't load faster-whisper unless it actually runs."""
    return TimedRemoteTranscriber(
        timeout_s=SETTINGS.transcription_timeout_s,
    )


# Overridable hook (tests swap in a mock). Must return an object with
# transcribe(path) -> (text, info).
transcriber_factory: Callable[[], object] = default_transcriber_factory
