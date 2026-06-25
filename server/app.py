"""FastAPI surface for the carnyx class-transcription server.

Endpoints:
  GET  /healthz            — liveness
  POST /jobs               — start a job (JSON {audio_url} OR multipart file)
  GET  /jobs/{id}          — poll status / fetch proven transcript + report

Auth: every non-health route requires the `X-API-Key` header to match
TSCRIBE_API_KEY. The big audio file does NOT need to cross the tunnel — prefer
`audio_url` (carnyx pulls from Drive) to sidestep Cloudflare's ~100 MB proxied
body limit. Direct multipart upload is supported for files under that limit.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from pydantic import BaseModel

from .config import SETTINGS
from . import jobs

app = FastAPI(title="tscribe-class-carnyx", version="0.1.0")


def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    # If no key is configured, refuse rather than run open (fail closed).
    if not SETTINGS.api_key:
        raise HTTPException(status_code=503, detail="server API key not configured")
    if x_api_key != SETTINGS.api_key:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


class JobRequest(BaseModel):
    audio_url: Optional[str] = None


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "service": "tscribe-class-carnyx"}


@app.post("/jobs", dependencies=[Depends(require_api_key)])
def create_job(body: JobRequest) -> dict:
    """Primary path: carnyx pulls the audio from `audio_url` (e.g. a Google Drive
    download link). The big file never crosses the tunnel inbound, so Cloudflare's
    ~100 MB proxied-body limit never applies."""
    if not body.audio_url:
        raise HTTPException(status_code=400, detail="audio_url is required")
    job = jobs.submit(audio_url=body.audio_url)
    return {"id": job.id, "status": job.status}


@app.post("/jobs/upload", dependencies=[Depends(require_api_key)])
async def create_job_upload(file: UploadFile = File(...)) -> dict:
    """Secondary path: direct multipart upload. Only usable for files under the
    tunnel's proxied-body limit (100 MB Free/Pro, 200 MB Business)."""
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
