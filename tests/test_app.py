"""Proof the carnyx server drives the verified pipeline and enforces auth.

Uses FastAPI's TestClient with a mock transcriber (no faster-whisper, no carnyx).
Generates a tiny tone WAV as the audio, runs a real job through the real
`class_pipeline`, and asserts: auth is enforced, the job reaches `done`, the
verification report is surfaced and `ok`, and a transcript comes back.

Run: TSCRIBE_API_KEY=test PYTHONPATH=.:../tscribe/src pytest -q
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

os.environ.setdefault("TSCRIBE_API_KEY", "test-key")

from fastapi.testclient import TestClient  # noqa: E402

from server import app as app_module  # noqa: E402
from server import config, jobs  # noqa: E402

client = TestClient(app_module.app)


# Deterministic non-repetitive filler. " ".join(["lorem"] * n) is 100%
# back-to-back repetition, which the pipeline's degeneracy check correctly reads
# as a transcriber loop. Synthetic text standing in for speech has to look like
# speech in the ways the contract measures.
_VOCAB = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo "
          "lima mike november oscar papa quebec romeo sierra tango uniform "
          "victor whiskey xray yankee zulu").split()


def _words(n: int, seed: int = 1234) -> str:
    import random
    rng = random.Random(seed)
    return " ".join(rng.choice(_VOCAB) for _ in range(n))




class MockTranscriber:
    def transcribe(self, audio_path: str):
        from transcription_tool.class_pipeline.verify import _mean_volume_db
        from transcription_tool.class_pipeline.splitter import probe_duration
        if (_mean_volume_db(audio_path) or -99) <= -45.0:
            return "", None
        dur = probe_duration(audio_path)
        return _words(max(1, int(120 * dur / 60.0))), None


@pytest.fixture(autouse=True)
def mock_transcriber():
    prev = config.transcriber_factory
    config.transcriber_factory = lambda: MockTranscriber()
    yield
    config.transcriber_factory = prev


@pytest.fixture
def tone_wav(tmp_path):
    p = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
         "sine=frequency=220:duration=6", "-ac", "1", "-ar", "16000",
         "-sample_fmt", "s16", str(p)],
        check=True,
    )
    return p


def _wait(job_id, timeout=30):
    for _ in range(timeout * 5):
        r = client.get(f"/jobs/{job_id}", headers={"X-API-Key": "test-key"})
        if r.json()["status"] in ("done", "failed"):
            return r.json()
        time.sleep(0.2)
    raise TimeoutError("job did not finish")


def test_healthz_open():
    assert client.get("/healthz").json()["ok"] is True


def test_auth_required(tone_wav):
    with open(tone_wav, "rb") as f:
        r = client.post("/jobs/upload", files={"file": ("tone.wav", f, "audio/wav")})
    assert r.status_code == 401


def test_upload_job_is_verified(tone_wav):
    with open(tone_wav, "rb") as f:
        r = client.post("/jobs/upload", headers={"X-API-Key": "test-key"},
                        files={"file": ("tone.wav", f, "audio/wav")})
    assert r.status_code == 200
    result = _wait(r.json()["id"])
    assert result["status"] == "done", result.get("error")
    assert result["report"]["ok"] is True
    assert "## [00:00:00]" in result["transcript"]


def test_audio_url_pull_path(tone_wav, monkeypatch):
    # carnyx-pulls-from-URL branch: stub the download to copy the local fixture.
    import shutil
    monkeypatch.setattr(jobs, "_download_url", lambda url, dest: shutil.copy(tone_wav, dest))
    r = client.post("/jobs", headers={"X-API-Key": "test-key"},
                    json={"audio_url": "https://example.com/tone.wav"})
    assert r.status_code == 200
    result = _wait(r.json()["id"])
    assert result["status"] == "done", result.get("error")
    assert result["report"]["ok"] is True


def test_grouping_skip_logged_when_writeback_fails(tone_wav, monkeypatch):
    """A failed transcript write-back must not silently drop the grouping move.

    The move is deliberately gated on a successful write-back, so when the
    upload dies (e.g. a stale-connection BrokenPipeError), the sources are NOT
    grouped — and that skip is logged per source file rather than surfacing as a
    silent empty `moved: []`. The verified transcript is still kept, and the
    move is never even attempted.
    """
    import shutil
    from server import drive as drive_mod

    monkeypatch.setattr(jobs, "_download_url", lambda url, dest: shutil.copy(tone_wav, dest))

    def _broken_pipe(*a, **k):
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(drive_mod, "upload_text", _broken_pipe)

    move_calls = []
    monkeypatch.setattr(drive_mod, "move_file", lambda fid, dest: move_calls.append(fid))

    job = jobs.submit(
        audio_url="https://example.com/tone.wav",
        dest_folder_id="DEST",
        move_file_ids=["AUDIO_ID", "NOTES_ID"],
        source_name="tone.wav",
    )

    deadline = time.time() + 30
    while time.time() < deadline:
        j = jobs.get_job(job.id)
        if j.status in ("done", "failed"):
            break
        time.sleep(0.2)
    j = jobs.get_job(job.id)

    assert j.status == "done", j.error
    assert j.transcript  # transcript still safe despite the failed upload
    assert j.transcript_file_id is None
    assert "BrokenPipeError" in (j.upload_error or "")
    assert j.moved == []
    assert move_calls == []  # move never attempted after a failed write-back
    assert len(j.move_errors) == 2  # one honest skip entry per requested move
    assert all("skipped grouping" in e for e in j.move_errors)
    assert all("BrokenPipeError" in e for e in j.move_errors)
