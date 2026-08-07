"""FastAPI surface for the carnyx class-transcription server.

Endpoints:
  GET  /healthz            — liveness (public, deliberately uninformative)
  GET  /version            — which pipeline commit this process actually imported
  POST /jobs               — start a job (JSON: drive_file_id OR audio_url, with
                             optional dest_folder_id / move_file_ids for write-back)
  POST /jobs/upload        — start a job from a direct multipart upload
  GET  /jobs/{id}          — poll status / fetch proven transcript + report

Auth: every non-health route requires the `X-API-Key` header to match
TSCRIBE_API_KEY. Preferred input is `drive_file_id` — carnyx downloads it via
the service account (any size, no public link, no tunnel body limit) and can
write the transcript back to `dest_folder_id`.

Reading a `GET /jobs/{id}` response: `verified` is the only field that authorises
use of the transcript. A **failed** job may still carry a `transcript` and a
`report` so the failure can be audited — that transcript is evidence, not a
deliverable.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import List, Optional

from functools import lru_cache

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


@lru_cache(maxsize=1)
def pipeline_build() -> dict:
    """What `transcription_tool` code this process actually imported.

    Answering "which code is live?" used to require a shell on the box, and the
    obvious check there was wrong: `uv run python -c ...` interrogates the
    **venv**, not the running process. On 2026-08-07 that check printed the
    expected answers *before* the service had been restarted, so it would happily
    certify a stale server — the exact "it looks done" failure the deploy README
    warns about, reproduced by the tool meant to detect it.

    This asks the process itself. `direct_url.json` is written by the installer
    for a VCS dependency and carries the **commit id** that was built, so the
    answer is the sha, not a guess from feature-probing.

    Cached for the process lifetime deliberately: the value cannot change without
    a restart, and a restart is precisely what invalidates it.

    Fails soft — a liveness route must never 500 because provenance was
    unavailable. Missing keys mean "could not determine," never "wrong."
    """
    info: dict = {}
    try:
        from importlib.metadata import distribution

        dist = distribution("transcription-tool")
        info["version"] = dist.version
        raw = dist.read_text("direct_url.json")
        if raw:
            commit = json.loads(raw).get("vcs_info", {}).get("commit_id")
            if commit:
                info["commit"] = commit
    except Exception:  # noqa: BLE001 - provenance is best-effort
        pass
    try:
        from transcription_tool.class_pipeline import verify as v

        fields = sorted(v.Contract.__dataclass_fields__)
        checks = sorted(n for n in dir(v) if n.startswith("assert_"))
        # Changes when the contract's shape changes, and not when a docstring
        # does. Catches a hand-edit on the box that no commit sha would reveal.
        info["contract_fingerprint"] = hashlib.sha256(
            ("|".join(fields) + "||" + "|".join(checks)).encode()
        ).hexdigest()[:12]
        info["contract_fields"] = len(fields)
        info["contract_checks"] = len(checks)
    except Exception:  # noqa: BLE001
        pass
    return info


@app.get("/healthz")
def healthz() -> dict:
    """Liveness only, and deliberately public — so deliberately uninformative.

    Build provenance lives on the authenticated `/version`. This endpoint is
    reachable by anyone who can resolve the tunnel, and an anonymous caller has
    no business learning which commit is deployed."""
    return {"ok": True, "service": "tscribe-class-carnyx"}


@app.get("/version", dependencies=[Depends(require_api_key)])
def version() -> dict:
    """Which code is actually serving this request.

    The honest answer to a deploy check: read by the running process, from its
    own installed distribution. Compare `pipeline.commit` against the merge sha
    you expected."""
    return {
        "service": "tscribe-class-carnyx",
        "server_version": app.version,
        "pipeline": pipeline_build(),
    }


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
