"""FastAPI surface for the carnyx class-transcription server.

Endpoints:
  GET  /healthz            — liveness
  POST /jobs               — start a job (JSON: drive_file_id OR audio_url, with
                             optional dest_folder_id / move_file_ids for write-back)
  POST /jobs/upload        — start a job from a direct multipart upload
  GET  /jobs/{id}          — poll status / fetch proven transcript + report

Auth: every non-health route requires the `X-API-Key` header to match
TSCRIBE_API_KEY. Preferred input is `drive_file_id` — carnyx downloads it via
the service account (any size, no public link, no tunnel body limit) and can
write the transcript back to `dest_folder_id`.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from pydantic import BaseModel

from .config import SETTINGS
from . import jobs

app = FastAPI(title="tscribe-class-carnyx", version="0.2.0")


def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    # If no key is configured, refuse rather than run open (fail closed).
    if not SETTINGS.api_key:
        raise HTTPException(status_code=503, detail="server API key not configured")
    if x_api_key != SETTINGS.api_key:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


class JobRequest(BaseModel):
    # Audio source — provide exactly one of these two:
    drive_file_id: Optional[str] = None   # preferred: carnyx downloads via service account
    audio_url: Optional[str] = None       # public-link pull (sub-100 MB)
    # Optional Drive write-back:
    dest_folder_id: Optional[str] = None  # where to write the verified transcript
    move_file_ids: Optional[List[str]] = None  # source assets to group into dest_folder_id
    source_name: Optional[str] = None     # filename to use for the audio / transcript stem


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "service": "tscribe-class-carnyx"}


@app.post("/jobs", dependencies=[Depends(require_api_key)])
def create_job(body: JobRequest) -> dict:
    """Start a transcription job. `drive_file_id` (service-account download) is
    preferred — it handles 4-hour files and needs no public link. `audio_url` is
    the public-pull fallback. With `dest_folder_id`, the verified transcript is
    written back to Drive and `move_file_ids` are grouped into that folder."""
    if not body.drive_file_id and not body.audio_url:
        raise HTTPException(status_code=400, detail="provide drive_file_id or audio_url")
    job = jobs.submit(
        drive_file_id=body.drive_file_id,
        audio_url=body.audio_url,
        dest_folder_id=body.dest_folder_id,
        move_file_ids=body.move_file_ids,
        source_name=body.source_name,
    )
    return {"id": job.id, "status": job.status}


@app.post("/jobs/upload", dependencies=[Depends(require_api_key)])
async def create_job_upload(file: UploadFile = File(...)) -> dict:
    """Direct multipart upload. Only usable for files under the tunnel's
    proxied-body limit (100 MB Free/Pro, 200 MB Business)."""
    dest = Path(tempfile.mkdtemp(prefix="tscribe_up_")) / (file.filename or "audio.bin")
    size = 0
    with open(dest, "wb") as f:
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > SETTINGS.max_audio_bytes:
                raise HTTPException(status_code=413, detail="audio exceeds max_audio_bytes")
            f.write(chunk)
    job = jobs.submit(local_path=str(dest))
    return {"id": job.id, "status": job.status}


@app.get("/jobs/{job_id}", dependencies=[Depends(require_api_key)])
def job_status(job_id: str) -> dict:
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job.public()
