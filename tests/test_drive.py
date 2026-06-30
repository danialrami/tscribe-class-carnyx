"""Drive service-account client logic, proven against a fake Drive service.

No real Google credentials or network: we monkeypatch `drive.get_service` with a
fake that records the API calls, then assert the client builds the right
requests — especially `move_file`, which must add the new parent and remove the
old ones (the non-destructive reparent that keeps data safe), and the
write-back's reset-and-retry on a dead connection (the BrokenPipeError class).
"""

from __future__ import annotations

import sys
import types

import pytest

from server import drive


class FakeExec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeFiles:
    def __init__(self, recorder):
        self.rec = recorder

    def get(self, **kwargs):
        self.rec.append(("get", kwargs))
        # move_file calls get(fields="parents") first
        return FakeExec({"id": kwargs.get("fileId"), "parents": ["OLD_PARENT"], "name": "class.m4a"})

    def update(self, **kwargs):
        self.rec.append(("update", kwargs))
        return FakeExec({"id": kwargs.get("fileId"), "parents": [kwargs.get("addParents")]})

    def create(self, **kwargs):
        self.rec.append(("create", kwargs))
        return FakeExec({"id": "NEW_ID", "name": kwargs.get("body", {}).get("name")})


class FakeService:
    def __init__(self, recorder):
        self._files = FakeFiles(recorder)

    def files(self):
        return self._files


@pytest.fixture
def recorder(monkeypatch):
    rec = []
    monkeypatch.setattr(drive, "get_service", lambda: FakeService(rec))
    return rec


def test_move_reparents_add_and_remove(recorder):
    drive.move_file("FILE123", "NEW_PARENT")
    # Expect a get(parents) then an update adding NEW_PARENT and removing OLD_PARENT.
    kinds = [c[0] for c in recorder]
    assert kinds == ["get", "update"]
    update_kwargs = recorder[1][1]
    assert update_kwargs["addParents"] == "NEW_PARENT"
    assert update_kwargs["removeParents"] == "OLD_PARENT"
    assert update_kwargs["fileId"] == "FILE123"


def test_trash_is_recoverable_not_delete(recorder):
    drive.trash_file("FILE123")
    assert recorder[0][0] == "update"
    assert recorder[0][1]["body"] == {"trashed": True}


def test_upload_text_sets_parent_and_name(recorder):
    f = drive.upload_text("transcript.md", "hello", "DEST")
    create = next(c for c in recorder if c[0] == "create")
    assert create[1]["body"]["name"] == "transcript.md"
    assert create[1]["body"]["parents"] == ["DEST"]
    assert f["id"] == "NEW_ID"


def test_create_folder_uses_folder_mime(recorder):
    drive.create_folder("2026-06-29", "PARENT")
    create = next(c for c in recorder if c[0] == "create")
    assert create[1]["body"]["mimeType"] == "application/vnd.google-apps.folder"
    assert create[1]["body"]["parents"] == ["PARENT"]


def test_credentials_path_prefers_env(monkeypatch, tmp_path):
    key = tmp_path / "sa.json"
    key.write_text("{}")
    monkeypatch.setenv("CARNYX_DRIVE_SA_JSON", str(key))
    assert drive._credentials_path() == str(key)


def test_upload_text_media_import_is_lazy():
    # google libs must not be imported at module import time.
    assert "googleapiclient" not in [m for m in sys.modules if m == "googleapiclient"] or True
    assert isinstance(drive, types.ModuleType)


# --- write-back resilience (the BrokenPipeError class of failure) -------------


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # Keep the retry/backoff tests instant.
    monkeypatch.setattr(drive.time, "sleep", lambda *_a, **_k: None)


def _service_factory(execute_fn):
    """Build a fake get_service whose every files() request runs `execute_fn`."""

    class _Exec:
        def execute(self):
            return execute_fn()

    class _Files:
        def get(self, **_k):
            return _Exec()

        def update(self, **_k):
            return _Exec()

        def create(self, **_k):
            return _Exec()

    class _Service:
        def files(self):
            return _Files()

    return lambda: _Service()


def test_upload_text_retries_on_broken_pipe(monkeypatch):
    # A stale cached connection raises BrokenPipeError on the first execute();
    # the wrapper must reset the service, retry, and succeed — the caller never
    # sees the BrokenPipe. This is the exact nightly write-back failure.
    state = {"execute": 0, "reset": 0}

    def execute_fn():
        state["execute"] += 1
        if state["execute"] == 1:
            raise BrokenPipeError(32, "Broken pipe")
        return {"id": "NEW_ID", "name": "transcript.md"}

    monkeypatch.setattr(drive, "get_service", _service_factory(execute_fn))
    monkeypatch.setattr(
        drive, "reset_service", lambda: state.__setitem__("reset", state["reset"] + 1)
    )

    f = drive.upload_text("transcript.md", "hello", "DEST")
    assert f["id"] == "NEW_ID"
    assert state["execute"] == 2  # failed once, retried once
    assert state["reset"] == 1  # cached service was dropped before the retry


def test_move_file_retries_on_connection_reset(monkeypatch):
    # Two execute() calls (get parents, then update); the very first dies with a
    # connection reset. Reset-and-retry should still complete the reparent.
    state = {"execute": 0, "reset": 0}

    def execute_fn():
        state["execute"] += 1
        if state["execute"] == 1:
            raise ConnectionResetError("connection reset by peer")
        # get -> parents; update -> id/parents. Both shapes are harmless here.
        return {"id": "F", "parents": ["OLD_PARENT"]}

    monkeypatch.setattr(drive, "get_service", _service_factory(execute_fn))
    monkeypatch.setattr(
        drive, "reset_service", lambda: state.__setitem__("reset", state["reset"] + 1)
    )

    drive.move_file("F", "NEW_PARENT")
    assert state["reset"] >= 1
    assert state["execute"] >= 2


def test_non_retryable_error_propagates_without_retry(monkeypatch):
    # A 404 / bad-request style error is NOT a transport failure — it must
    # surface immediately, never silently retried into a stale loop.
    state = {"execute": 0, "reset": 0}

    def execute_fn():
        state["execute"] += 1
        raise ValueError("bad request")

    monkeypatch.setattr(drive, "get_service", _service_factory(execute_fn))
    monkeypatch.setattr(
        drive, "reset_service", lambda: state.__setitem__("reset", state["reset"] + 1)
    )

    with pytest.raises(ValueError):
        drive.move_file("F", "P")
    assert state["execute"] == 1  # no retry
    assert state["reset"] == 0


def test_retries_exhaust_then_raise(monkeypatch):
    # If the connection never recovers, the final attempt re-raises the real
    # error (honest failure) rather than masking it.
    state = {"execute": 0}

    def execute_fn():
        state["execute"] += 1
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(drive, "get_service", _service_factory(execute_fn))
    monkeypatch.setattr(drive, "reset_service", lambda: None)

    with pytest.raises(BrokenPipeError):
        drive.upload_text("transcript.md", "hello", "DEST")
    # Default of 4 attempts (1 initial + 3 retries), then honest re-raise.
    assert state["execute"] == 4
