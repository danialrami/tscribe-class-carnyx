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

Connection resilience: the transcript write-back fires at the *end* of a job
that may have started hours earlier, so the cached Drive transport's socket is
often dead by then (idle reset on the tunnel, googleapis closing a stale
keep-alive, or a token-refresh boundary). Every mutating call therefore runs
through `_execute_resilient`, which on a transport-level failure (BrokenPipe /
connection reset / transient 5xx) drops the cached service, backs off, rebuilds
a fresh authorized connection, and retries. Write-back uploads are also
resumable so a dropped chunk resumes instead of restarting from zero.
"""

from __future__ import annotations

import glob
import http.client
import os
import random
import socket
import ssl
import threading
import time
from pathlib import Path
from typing import Callable, Optional

SCOPES = ["https://www.googleapis.com/auth/drive"]

_service = None
_service_lock = threading.RLock()

# Connection-level failures that mean "the socket/transport died" rather than
# "the request was bad". These are exactly what a long-idle cached connection
# raises when the write-back finally fires hours after the download opened it.
_RETRYABLE_CONN = (
    BrokenPipeError,
    ConnectionError,  # incl. ConnectionResetError / ConnectionAbortedError
    TimeoutError,
    socket.timeout,
    socket.error,  # OSError alias — broad, but transport death is exactly this
    ssl.SSLError,
    http.client.RemoteDisconnected,
    http.client.BadStatusLine,
    http.client.IncompleteRead,
)

# Transient HTTP statuses worth retrying (server/back-end, not a client error).
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Tunables (env-overridable so ops can adjust without a code change).
_RETRY_ATTEMPTS = int(os.environ.get("CARNYX_DRIVE_RETRY_ATTEMPTS", "4"))
_RETRY_BASE_DELAY_S = float(os.environ.get("CARNYX_DRIVE_RETRY_BASE_DELAY_S", "1.0"))


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
    with _service_lock:
        if _service is None:
            from google.oauth2 import service_account  # lazy
            from googleapiclient.discovery import build  # lazy

            creds = service_account.Credentials.from_service_account_file(
                _credentials_path(), scopes=SCOPES
            )
            _service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return _service


def reset_service() -> None:
    """Drop the cached service so the next call rebuilds a fresh authorized
    connection (new socket + refreshed token). Called between retries when the
    cached transport has gone stale across a long-running job."""
    global _service
    with _service_lock:
        _service = None


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, _RETRYABLE_CONN):
        return True
    # googleapiclient.errors.HttpError — retry only on transient server statuses.
    status = getattr(getattr(exc, "resp", None), "status", None)
    try:
        return int(status) in _RETRYABLE_STATUS
    except (TypeError, ValueError):
        return False


def _execute_resilient(
    make_request: Callable[[], object],
    *,
    attempts: int = _RETRY_ATTEMPTS,
    base_delay: float = _RETRY_BASE_DELAY_S,
):
    """Run a Drive request with reset-and-retry on transport death.

    `make_request` must build the request *fresh on each call* so that, after a
    reset, the retry binds to a freshly-rebuilt service. On a retryable
    connection/transient error we drop the cached service, back off, and rebuild
    — which is what actually clears a half-open socket left by a multi-hour job.
    Non-retryable errors (bad request, 404, auth) propagate immediately.
    """
    last: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            return make_request().execute()
        except Exception as e:  # noqa: BLE001 - re-raised below if not retryable
            if not _is_retryable(e) or attempt == attempts:
                raise
            last = e
            reset_service()  # force a fresh connection + token on rebuild
            time.sleep(base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5))
    # Defensive: the loop either returns or raises; never falls through.
    raise last  # type: ignore[misc]


def get_metadata(file_id: str) -> dict:
    return _execute_resilient(
        lambda: get_service()
        .files()
        .get(fileId=file_id, fields="id,name,mimeType,size,parents", supportsAllDrives=True)
    )


def download_file(file_id: str, dest: str | Path, max_bytes: Optional[int] = None) -> Path:
    """Stream a file's bytes to `dest` via the Drive API (works for any size,
    no public link / interstitial needed). Runs at job start on a fresh
    connection; a transient blip restarts the whole download from zero."""
    from googleapiclient.http import MediaIoBaseDownload  # lazy

    dest = Path(dest)

    def _attempt() -> Path:
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

    last: Optional[BaseException] = None
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            return _attempt()
        except ValueError:
            raise  # size guard — not a transport failure
        except Exception as e:  # noqa: BLE001
            if not _is_retryable(e) or attempt == _RETRY_ATTEMPTS:
                raise
            last = e
            reset_service()
            time.sleep(_RETRY_BASE_DELAY_S * (2 ** (attempt - 1)) + random.uniform(0, 0.5))
    raise last  # type: ignore[misc]


def upload_text(name: str, text: str, parent_id: str, mime: str = "text/markdown") -> dict:
    """Write a text deliverable into `parent_id`. Resumable + reset-and-retry so
    a stale connection at the end of a long job doesn't lose the write-back."""
    from googleapiclient.http import MediaInMemoryUpload  # lazy

    data = text.encode("utf-8")
    body = {"name": name, "parents": [parent_id]}

    def _req():
        # Build media + request fresh each attempt so a retry binds to a rebuilt
        # service and starts a clean resumable session.
        media = MediaInMemoryUpload(data, mimetype=mime, resumable=True)
        return (
            get_service()
            .files()
            .create(
                body=body,
                media_body=media,
                fields="id,name,parents,webViewLink",
                supportsAllDrives=True,
            )
        )

    return _execute_resilient(_req)


def create_folder(name: str, parent_id: str) -> dict:
    body = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    return _execute_resilient(
        lambda: get_service()
        .files()
        .create(body=body, fields="id,name,parents", supportsAllDrives=True)
    )


def list_folder(parent_id: str, page_size: int = 100) -> list[dict]:
    q = f"'{parent_id}' in parents and trashed = false"
    out: list[dict] = []
    token = None
    while True:
        resp = _execute_resilient(
            lambda token=token: get_service()
            .files()
            .list(
                q=q,
                fields="nextPageToken, files(id,name,mimeType,size)",
                pageSize=page_size,
                pageToken=token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
        )
        out.extend(resp.get("files", []))
        token = resp.get("nextPageToken")
        if not token:
            return out


def move_file(file_id: str, new_parent_id: str) -> dict:
    """Reparent a file (add new parent, remove old). Non-destructive: the file
    is moved, never copied or deleted."""
    meta = _execute_resilient(
        lambda: get_service()
        .files()
        .get(fileId=file_id, fields="parents", supportsAllDrives=True)
    )
    prev_parents = ",".join(meta.get("parents", []))
    return _execute_resilient(
        lambda: get_service()
        .files()
        .update(
            fileId=file_id,
            addParents=new_parent_id,
            removeParents=prev_parents,
            fields="id,parents",
            supportsAllDrives=True,
        )
    )


def trash_file(file_id: str) -> dict:
    """Recoverable removal. We never hard-delete originals."""
    return _execute_resilient(
        lambda: get_service()
        .files()
        .update(fileId=file_id, body={"trashed": True}, supportsAllDrives=True)
    )
