#!/usr/bin/env python3
"""
local_submit_dump.py — BREAK-GLASS local runner for tscribe Run A (SUBMIT).

Use ONLY when the cloud agent (Ciani) reports it cannot reach
tscribe-api.lufshq.com from its sandbox. Run this ON carnyx, from the
tscribe-class-carnyx repo checkout, so it can:
  - hit the API over loopback (127.0.0.1:6390) — no tunnel, no DNS, no
    cloud-sandbox egress dependency at all
  - reuse server/drive.py's Drive calls (same service account, same
    resilient retry logic already proven in production)
  - read TSCRIBE_API_KEY straight out of /etc/tscribe-class.env — never
    pasted into a script, a chat, or a repo

Reproduces exactly what cloud Run A does, and posts a byte-identical
POST /jobs body (see JobRequest in server/app.py) to what
tscribe-carnyx-runner/carnyx_client.py sends from the cloud side. Run B
(cloud, 3am) does not change: it reads job.json from the dated folder
exactly as before.

Usage (on carnyx):
    cd ~/repos/tscribe-class-carnyx
    set -a; source /etc/tscribe-class.env; set +a
    uv run python local_submit_dump.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict

import requests

from server import drive  # same service account, same resilient Drive calls

# Fixed Drive layout — identical to the cloud Run A contract.
DUMP_FOLDER_ID = "1cRa_PWrW-cTdXVf5nFBdA8bcG9BDVogV"
PARENT_FOLDER_ID = "1Z_Z4HAYxS1SLIWdzTYW4Qbsob7679uPQ"

# carnyx over loopback — no tunnel, no public DNS, no cloud sandbox involved.
CARNYX_BASE_URL = os.environ.get("CARNYX_LOCAL_URL", "http://127.0.0.1:6390")
API_KEY = os.environ["TSCRIBE_API_KEY"]  # KeyError on purpose if unset — fail loud, never open

DATE_PREFIX = re.compile(r"^(\d{4})(\d{2})(\d{2})[_-]")   # 20260825_175851.m4a
DATE_ISO = re.compile(r"^(\d{4}-\d{2}-\d{2})")            # 2026-08-25.md


def date_for(filename: str) -> str | None:
    """Same date-derivation rule as cloud Run A: an 8-digit YYYYMMDD prefix
    (audio), or an already-ISO YYYY-MM-DD prefix (notes)."""
    m = DATE_PREFIX.match(filename)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = DATE_ISO.match(filename)
    return m.group(1) if m else None


def find_or_create_dated_folder(date_str: str) -> str:
    siblings = drive.list_folder(PARENT_FOLDER_ID)
    for f in siblings:
        if f["name"] == date_str and f["mimeType"] == "application/vnd.google-apps.folder":
            return f["id"]
    return drive.create_folder(date_str, PARENT_FOLDER_ID)["id"]


def submit_job(drive_file_id: str, dest_folder_id: str, move_file_ids: list[str], source_name: str) -> str:
    """Exactly the JobRequest shape server/app.py expects."""
    payload = {
        "drive_file_id": drive_file_id,
        "dest_folder_id": dest_folder_id,
        "move_file_ids": move_file_ids,
        "source_name": source_name,
    }
    r = requests.post(f"{CARNYX_BASE_URL}/jobs", headers={"X-API-Key": API_KEY}, json=payload, timeout=60)
    r.raise_for_status()
    return r.json()["id"]


def write_job_manifest(dest_folder_id: str, manifest: dict) -> None:
    """Replace, don't duplicate — matches cloud Run A's job.json rule."""
    for f in drive.list_folder(dest_folder_id):
        if f["name"] == "job.json":
            drive.trash_file(f["id"])  # recoverable, non-destructive
    drive.upload_text("job.json", json.dumps(manifest, indent=2), dest_folder_id, mime="application/json")


def main() -> int:
    files = drive.list_folder(DUMP_FOLDER_ID)
    if not files:
        print("dump folder empty — nothing to process")
        return 0

    groups: dict[str, list[dict]] = defaultdict(list)
    unmatched = []
    for f in files:
        d = date_for(f["name"])
        if d:
            groups[d].append(f)
        else:
            unmatched.append(f["name"])
    if unmatched:
        print(f"WARNING: could not derive a date for: {unmatched} — left in place, not submitted", file=sys.stderr)

    results = []
    for date_str, group_files in sorted(groups.items()):
        try:
            audio = next((f for f in group_files if f["name"].lower().endswith((".m4a", ".wav"))), None)
            if audio is None:
                print(f"{date_str}: no audio file in group ({[f['name'] for f in group_files]}) — skipping", file=sys.stderr)
                continue
            dest_id = find_or_create_dated_folder(date_str)
            move_ids = [f["id"] for f in group_files]
            job_id = submit_job(audio["id"], dest_id, move_ids, audio["name"])
            write_job_manifest(dest_id, {"job_id": job_id, "dest_folder_id": dest_id, "audio_name": audio["name"], "date": date_str})
            print(f"{date_str}: submitted job={job_id} dest={dest_id}")
            results.append({"date": date_str, "job_id": job_id, "dest_folder_id": dest_id, "error": None})
        except Exception as e:  # noqa: BLE001 — one day's failure must not stop the others
            print(f"{date_str}: FAILED — {type(e).__name__}: {e}", file=sys.stderr)
            results.append({"date": date_str, "job_id": None, "dest_folder_id": None, "error": str(e)})

    print(json.dumps(results, indent=2))
    return 0 if all(r["job_id"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
