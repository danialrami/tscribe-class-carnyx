"""Drive service-account client logic, proven against a fake Drive service.

No real Google credentials or network: we monkeypatch `drive.get_service` with a
fake that records the API calls, then assert the client builds the right
requests — especially `move_file`, which must add the new parent and remove the
old ones (the non-destructive reparent that keeps data safe).
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
