"""In-memory async job store + the background runner.

A job obtains an audio file (Drive download via service account, public-URL
pull, or direct upload), runs the *verified* class pipeline from the tscribe
package, stores the proven transcript + report, and — when given a Drive
destination — writes the transcript back and (optionally) groups the source
assets into the dated folder by **moving** them.

Single source of truth: the algorithm is `transcription_tool.class_pipeline`.
Data-safety: the transcript text is always kept in the job result even if the
Drive write-back fails; asset grouping uses non-destructive moves and only runs
after a successful, verified transcript. Nothing here deletes an original.
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
    transcript_file_id: Optional[str] = None  # Drive id of the written-back transcript
    upload_error: Optional[str] = None        # non-fatal: transcript still in `transcript`
    moved: list = field(default_factory=list)  # file ids successfully grouped
    move_errors: list = field(default_factory=list)

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
            "transcript_file_id": self.transcript_file_id,
            "upload_error": self.upload_error,
            "moved": self.moved,
            "move_errors": self.move_errors,
        }


_JOBS: dict[str, Job] = {}
_LOCK = threading.Lock()


def get_job(job_id: str) -> Optional[Job]:
    with _LOCK:
        return _JOBS.get(job_id)


def _download_url(url: str, dest: Path) -> None:
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
        "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in report.checks],
        "failures": [c.name for c in report.failures],
    }


def _transcript_name(audio_path: Path) -> str:
    return f"{audio_path.stem}_transcript.md"


def _run(
    job: Job,
    local_audio: Path,
    cleanup_audio: bool,
    dest_folder_id: Optional[str],
    move_file_ids: Optional[list],
) -> None:
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

        # Write-back + grouping happen only after a verified transcript. Failures
        # here are non-fatal: the transcript is already safe in job.transcript.
        if dest_folder_id:
            from . import drive  # lazy: only needs google libs when used
            try:
                f = drive.upload_text(_transcript_name(local_audio), result.transcript, dest_folder_id)
                job.transcript_file_id = f.get("id")
            except Exception as e:  # noqa: BLE001
                job.upload_error = f"{type(e).__name__}: {e}"

            # Group source assets into the dated folder (non-destructive move),
            # only once the deliverable exists.
            if move_file_ids and job.transcript_file_id:
                for fid in move_file_ids:
                    try:
                        drive.move_file(fid, dest_folder_id)
                        job.moved.append(fid)
                    except Exception as e:  # noqa: BLE001
                        job.move_errors.append(f"{fid}: {type(e).__name__}: {e}")

        job.touch("done")
    except ContractViolation as e:
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


def submit(
    *,
    audio_url: Optional[str] = None,
    local_path: Optional[str] = None,
    drive_file_id: Optional[str] = None,
    dest_folder_id: Optional[str] = None,
    move_file_ids: Optional[list] = None,
    source_name: Optional[str] = None,
) -> Job:
    """Create a job. Audio source is one of: `drive_file_id` (service-account
    download — preferred for big files), `audio_url` (public pull), or
    `local_path` (direct upload). If `dest_folder_id` is given, the verified
    transcript is written back there; `move_file_ids` are then grouped into it."""
    src = drive_file_id or audio_url or (local_path or "upload")
    job = Job(id=uuid.uuid4().hex, source=src)
    with _LOCK:
        _JOBS[job.id] = job

    def worker():
        cleanup = False
        try:
            if drive_file_id:
                from . import drive  # lazy
                name = source_name
                if not name:
                    try:
                        name = drive.get_metadata(drive_file_id).get("name")
                    except Exception:  # noqa: BLE001
                        name = None
                dl_dir = Path(tempfile.mkdtemp(prefix="tscribe_dl_"))
                audio = dl_dir / (name or "audio.bin")
                drive.download_file(drive_file_id, audio, max_bytes=SETTINGS.max_audio_bytes)
                cleanup = True
            elif audio_url:
                audio = Path(tempfile.mkdtemp(prefix="tscribe_dl_")) / (source_name or "audio.bin")
                _download_url(audio_url, audio)
                cleanup = True
            elif local_path:
                audio = Path(local_path)
                cleanup = True
            else:
                raise ValueError("no audio source provided")
            _run(job, audio, cleanup, dest_folder_id, move_file_ids)
        except Exception as e:  # noqa: BLE001
            job.error = f"{type(e).__name__}: {e}"
            job.touch("failed")

    threading.Thread(target=worker, daemon=True).start()
    return job
