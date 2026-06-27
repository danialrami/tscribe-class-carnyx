"""Google Drive access for carnyx via a service account.

carnyx uses a service-account JSON (shared as Editor on the class-recordings
folder) to do the heavy Drive I/O the cloud agent can't: download big audio
files (no 100 MB tunnel limit, no public-link virus-scan interstitial), write
the verified transcript back, and group/move files within the shared tree.

Credential resolution order:
  1. $CARNYX_DRIVE_SA_JSON  (explicit path)
  2. $GOOGLE_APPLICATION_CREDENTIALS
  3. the first *.json in ./credentials/  (matches the repo layout)

google-api-python-client / google-auth are imported lazily so the rest of the
server (and its tests) don't require them unless Drive is actually used.

Data-safety stance: grouping uses **move** (reparent — the file is never copied
or deleted, just moved), and we never hard-delete. `trash_file` is the only
removal and it is recoverable. Nothing here destroys an original.
"""

from __future__ import annotations

import glob
import os
from pathlib import Path
from typing import Optional

SCOPES = ["https://www.googleapis.com/auth/drive"]

_service = None


def _credentials_path() -> str:
    for env in ("CARNYX_DRIVE_SA_JSON", "GOOGLE_APPLICATION_CREDENTIALS"):
        p = os.environ.get(env)
        if p and Path(p).expanduser().exists():
            return str(Path(p).expanduser())
    creds_dir = Path(__file__).resolve().parent.parent / "credentials"
    matches = sorted(glob.glob(str(creds_dir / "*.json")))
    if matches:
        return matches[0]
    raise RuntimeError(
        "No service-account JSON found. Set CARNYX_DRIVE_SA_JSON or drop the key "
        "in ./credentials/."
    )


def get_service():
    """Build (and cache) the Drive v3 service from the service-account key."""
    global _service
    if _service is None:
        from google.oauth2 import service_account  # lazy
        from googleapiclient.discovery import build  # lazy

        creds = service_account.Credentials.from_service_account_file(
            _credentials_path(), scopes=SCOPES
        )
        _service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _service


def get_metadata(file_id: str) -> dict:
    return (
        get_service()
        .files()
        .get(fileId=file_id, fields="id,name,mimeType,size,parents", supportsAllDrives=True)
        .execute()
    )


def download_file(file_id: str, dest: str | Path, max_bytes: Optional[int] = None) -> Path:
    """Stream a file's bytes to `dest` via the Drive API (works for any size,
    no public link / interstitial needed)."""
    from googleapiclient.http import MediaIoBaseDownload  # lazy

    dest = Path(dest)
    svc = get_service()
    req = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
    with open(dest, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, req, chunksize=8 * 1024 * 1024)
        done = False
        while not done:
            _status, done = downloader.next_chunk()
            if max_bytes is not None and fh.tell() > max_bytes:
                raise ValueError("audio exceeds max_audio_bytes")
    return dest


def upload_text(name: str, text: str, parent_id: str, mime: str = "text/markdown") -> dict:
    from googleapiclient.http import MediaInMemoryUpload  # lazy

    media = MediaInMemoryUpload(text.encode("utf-8"), mimetype=mime, resumable=False)
    body = {"name": name, "parents": [parent_id]}
    return (
        get_service()
        .files()
        .create(body=body, media_body=media,
                fields="id,name,parents,webViewLink", supportsAllDrives=True)
        .execute()
    )


def create_folder(name: str, parent_id: str) -> dict:
    body = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    return (
        get_service()
        .files()
        .create(body=body, fields="id,name,parents", supportsAllDrives=True)
        .execute()
    )


def list_folder(parent_id: str, page_size: int = 100) -> list[dict]:
    q = f"'{parent_id}' in parents and trashed = false"
    out: list[dict] = []
    token = None
    svc = get_service()
    while True:
        resp = (
            svc.files()
            .list(q=q, fields="nextPageToken, files(id,name,mimeType,size)",
                  pageSize=page_size, pageToken=token,
                  supportsAllDrives=True, includeItemsFromAllDrives=True)
            .execute()
        )
        out.extend(resp.get("files", []))
        token = resp.get("nextPageToken")
        if not token:
            return out


def move_file(file_id: str, new_parent_id: str) -> dict:
    """Reparent a file (add new parent, remove old). Non-destructive: the file
    is moved, never copied or deleted."""
    svc = get_service()
    meta = svc.files().get(fileId=file_id, fields="parents", supportsAllDrives=True).execute()
    prev_parents = ",".join(meta.get("parents", []))
    return (
        svc.files()
        .update(fileId=file_id, addParents=new_parent_id, removeParents=prev_parents,
                fields="id,parents", supportsAllDrives=True)
        .execute()
    )


def trash_file(file_id: str) -> dict:
    """Recoverable removal. We never hard-delete originals."""
    return (
        get_service()
        .files()
        .update(fileId=file_id, body={"trashed": True}, supportsAllDrives=True)
        .execute()
    )
