"""In-memory async job store + the background runner.

A job downloads (or accepts) an audio file, runs the *verified* class pipeline
from the tscribe package, and stores the proven transcript plus its verification
report. The contract runs here, on carnyx, where the GPU and full install live.

Single source of truth: the algorithm is `transcription_tool.class_pipeline` —
this module only wraps it in a job lifecycle. No transcription logic is
reimplemented here.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

from transcription_tool.class_pipeline import ContractViolation, transcribe_class

from . import config
from .config import SETTINGS


@dataclass
class Job:
    id: str
    status: str = "queued"  # queued -> running -> done | failed
    source: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    transcript: Optional[str] = None
    report: Optional[dict] = None
    error: Optional[str] = None

    def touch(self, status: str) -> None:
        self.status = status
        self.updated_at = time.time()

    def public(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "source": self.source,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "transcript": self.transcript,
            "report": self.report,
            "error": self.error,
        }


_JOBS: dict[str, Job] = {}
_LOCK = threading.Lock()


def get_job(job_id: str) -> Optional[Job]:
    with _LOCK:
        return _JOBS.get(job_id)


def _download(url: str, dest: Path) -> None:
    with requests.get(url, stream=True, timeout=SETTINGS.download_timeout_s) as r:
        r.raise_for_status()
        total = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                total += len(chunk)
                if total > SETTINGS.max_audio_bytes:
                    raise ValueError("audio exceeds max_audio_bytes")
                f.write(chunk)


def _report_to_dict(report) -> dict:
    return {
        "ok": report.ok,
        "checks": [
            {"name": c.name, "ok": c.ok, "detail": c.detail} for c in report.checks
        ],
        "failures": [c.name for c in report.failures],
    }


def _run(job: Job, local_audio: Path, cleanup_audio: bool) -> None:
    work = Path(tempfile.mkdtemp(prefix="tscribe_job_"))
    out = work / "transcript.md"
    try:
        job.touch("running")
        result = transcribe_class(
            input_path=str(local_audio),
            output_path=str(out),
            transcriber=config.transcriber_factory(),
            chunk_minutes=SETTINGS.chunk_minutes,
            workers=SETTINGS.workers,
            snap_window_s=SETTINGS.snap_window_s,
        )
        job.transcript = result.transcript
        job.report = _report_to_dict(result.report)
        job.touch("done")
    except ContractViolation as e:
        # The pipeline proved the transcript wrong: fail loudly, surface why.
        job.error = f"ContractViolation: {e}"
        job.touch("failed")
    except Exception as e:  # noqa: BLE001 - report any failure honestly
        job.error = f"{type(e).__name__}: {e}"
        job.touch("failed")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if cleanup_audio:
            try:
                local_audio.unlink(missing_ok=True)
            except OSError:
                pass


def submit(*, audio_url: Optional[str] = None, local_path: Optional[str] = None) -> Job:
    """Create a job. Either fetch `audio_url` (carnyx pulls — no tunnel body
    limit) or use an already-saved `local_path` (direct upload)."""
    job = Job(id=uuid.uuid4().hex, source=audio_url or (local_path or "upload"))
    with _LOCK:
        _JOBS[job.id] = job

    def worker():
        cleanup = False
        try:
            if audio_url:
                tmp = Path(tempfile.mkdtemp(prefix="tscribe_dl_")) / "audio.bin"
                _download(audio_url, tmp)
                audio = tmp
                cleanup = True
            else:
                audio = Path(local_path)  # type: ignore[arg-type]
                cleanup = True
            _run(job, audio, cleanup)
        except Exception as e:  # noqa: BLE001
            job.error = f"{type(e).__name__}: {e}"
            job.touch("failed")

    threading.Thread(target=worker, daemon=True).start()
    return job
