"""Server configuration and the transcriber factory.

The heavy transcriber import (faster-whisper, via tscribe's Transcriber) is done
*lazily* inside the factory so the app and tests can import without pulling the
whole ML stack. Tests override `transcriber_factory` with a mock.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Settings:
    api_key: str = field(default_factory=lambda: os.environ.get("TSCRIBE_API_KEY", ""))
    chunk_minutes: float = float(os.environ.get("TSCRIBE_CHUNK_MINUTES", "15"))
    workers: int = int(os.environ.get("TSCRIBE_WORKERS", "2"))
    snap_window_s: float = float(os.environ.get("TSCRIBE_SNAP_WINDOW_S", "10"))
    # Max bytes the server will accept for a direct upload or pull (sanity guard).
    max_audio_bytes: int = int(os.environ.get("TSCRIBE_MAX_AUDIO_BYTES", str(2 * 1024 ** 3)))
    download_timeout_s: int = int(os.environ.get("TSCRIBE_DOWNLOAD_TIMEOUT_S", "600"))


SETTINGS = Settings()


def default_transcriber_factory():
    """Build tscribe's real Transcriber (carnyx-first via LiteLLM). Imported lazily
    so the FastAPI process needn't load faster-whisper unless it actually runs."""
    from transcription_tool.transcriber import Transcriber  # heavy import, deferred

    return Transcriber(use_remote=True)


# Overridable hook (tests swap in a mock). Must return an object with
# transcribe(path) -> (text, info).
transcriber_factory: Callable[[], object] = default_transcriber_factory
