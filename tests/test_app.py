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


class MockTranscriber:
    def transcribe(self, audio_path: str):
        from transcription_tool.class_pipeline.verify import _mean_volume_db
        from transcription_tool.class_pipeline.splitter import probe_duration
        if (_mean_volume_db(audio_path) or -99) <= -45.0:
            return "", None
        dur = probe_duration(audio_path)
        return " ".join(["lorem"] * max(1, int(120 * dur / 60.0))), None


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
    monkeypatch.setattr(jobs, "_download", lambda url, dest: shutil.copy(tone_wav, dest))
    r = client.post("/jobs", headers={"X-API-Key": "test-key"},
                    json={"audio_url": "https://example.com/tone.wav"})
    assert r.status_code == 200
    result = _wait(r.json()["id"])
    assert result["status"] == "done", result.get("error")
    assert result["report"]["ok"] is True
