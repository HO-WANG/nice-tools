import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def worker():
    try:
        from nice_tools import ase_worker
    except ImportError:
        pytest.fail("ASE worker is not implemented")
    return ase_worker


def client(monkeypatch, operation=None, status=None):
    module = SimpleNamespace(
        client_status=status or (lambda: {"available": False, "detail": "Unavailable"}),
        run_operation=(lambda payload, emit=None, register=None: operation(payload, emit)) if operation else (lambda payload, emit=None, register=None: {}),
    )
    monkeypatch.setitem(sys.modules, "nice_tools.ase_cli", module)
    return module


def fingerprint(path):
    stat = path.stat()
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": digest}


def target_fingerprint(path):
    if not path.exists():
        return {"exists": False}
    stat = path.stat()
    return {"exists": True, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def transfer_payload(path, direction):
    return {
        "operation": "transfer", "path": str(path),
        "connection": {"password": "private-password"},
        "object": {"database": "data", "owner": "dbo", "id": 7, "name": "items", "kind": "table"},
        "columns": [{"position": 1, "name": "item"}],
        "options": {"direction": direction},
    }


def test_status_uses_native_status_without_requiring_a_connection(worker, monkeypatch):
    client(monkeypatch, status=lambda: {"available": False, "version": None, "detail": "Windows required"})
    assert worker.execute({"operation": "status"}) == {"available": False, "version": None, "detail": "Windows required"}


def test_picker_does_not_load_native_library(worker, monkeypatch):
    monkeypatch.setitem(sys.modules, "nice_tools.ase_cli", None)
    monkeypatch.setitem(sys.modules, "nice_tools.ase_picker", SimpleNamespace(
        pick_file=lambda direction, suggested_name: {"cancelled": False, "path": "C:\\资料\\导出.bcp"},
    ))
    assert worker.execute({"operation": "pick", "direction": "out", "suggested_name": "导出.bcp"})["path"] == "C:\\资料\\导出.bcp"


def test_input_preview_uses_fresh_object_metadata(worker, monkeypatch, tmp_path):
    path = tmp_path / "资料.bcp"
    path.write_text("1\n", encoding="utf-8")
    columns = [{"position": 1, "name": "item", "type_name": "int"}]
    client(monkeypatch, operation=lambda payload, emit: {"object": {"id": 7, "name": "items"}, "columns": columns})
    def inspect(actual_path, actual_columns, options, emit):
        assert actual_path == path
        assert actual_columns == columns
        return {"rows": 1, "bytes": 2, "sample": [["1"]], "warnings": [], "fingerprint": fingerprint(path)}
    monkeypatch.setitem(sys.modules, "nice_tools.ase_files", SimpleNamespace(inspect_file=inspect, parse_options=lambda options: options))
    payload = transfer_payload(path, "in")
    payload["operation"] = "preview"
    result = worker.execute(payload)
    assert result["object"] == {"id": 7, "name": "items"}
    assert result["columns"] == columns
    assert result["rows"] == 1
    assert result["path"] == str(path)
    assert result["fingerprint"] == fingerprint(path)


def test_output_preview_reports_existing_target_fingerprint(worker, monkeypatch, tmp_path):
    path = tmp_path / "导出.bcp"
    path.write_bytes(b"existing")
    client(monkeypatch, operation=lambda payload, emit: {"object": {"id": 7}, "columns": []})
    monkeypatch.setitem(sys.modules, "nice_tools.ase_files", SimpleNamespace(parse_options=lambda options: options))
    payload = transfer_payload(path, "out")
    payload["operation"] = "preview"
    result = worker.execute(payload)
    assert result["out_target_fingerprint"] == target_fingerprint(path)
    assert result["warnings"] == []
    assert path.read_bytes() == b"existing"


def test_input_transfer_checks_reviewed_hash_streaming(worker, monkeypatch, tmp_path):
    path = tmp_path / "资料.bcp"
    path.write_bytes(b"1\n")
    payload = transfer_payload(path, "in")
    payload["fingerprint"] = fingerprint(path)
    client(monkeypatch, operation=lambda request, emit: {"rows": 1, "direction": "in"})
    monkeypatch.setattr(Path, "read_bytes", lambda path: pytest.fail("Input verification must stream the file"))
    assert worker.execute(payload)["rows"] == 1


def test_input_transfer_rejects_changed_file_before_native_work(worker, monkeypatch, tmp_path):
    path = tmp_path / "input.bcp"
    path.write_bytes(b"1\n")
    payload = transfer_payload(path, "in")
    payload["fingerprint"] = fingerprint(path)
    path.write_bytes(b"2\n")
    client(monkeypatch, operation=lambda request, emit: pytest.fail("Changed input must not reach native transfer"))
    with pytest.raises(ValueError, match="changed"):
        worker.execute(payload)


def test_input_verification_rejects_replaced_file_even_when_stat_and_hash_match(worker, monkeypatch, tmp_path):
    path = tmp_path / "input.bcp"
    path.write_bytes(b"1\n")
    payload = transfer_payload(path, "in")
    payload["fingerprint"] = fingerprint(path)
    replacement = tmp_path / "replacement.bcp"
    replacement.write_bytes(b"1\n")
    stat = path.stat()
    os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    original_stat = Path.stat
    replaced = False
    def replace_before_current_stat(actual_path, *args, **kwargs):
        nonlocal replaced
        if actual_path == path and not replaced:
            # _check_input closes its read handle before this path stat. Windows
            # forbids replacing an ordinary Python file while it remains open.
            os.replace(replacement, path)
            replaced = True
        return original_stat(actual_path, *args, **kwargs)
    monkeypatch.setattr(Path, "stat", replace_before_current_stat)
    with pytest.raises(ValueError, match="changed"):
        worker._check_input(path, payload["fingerprint"])
    assert replaced


@pytest.mark.parametrize("native_error", [False, True])
def test_windows_input_guard_denies_writes_and_delete_until_native_finishes(worker, monkeypatch, tmp_path, native_error):
    path = tmp_path / "资料.bcp"
    path.write_bytes(b"1\n")
    payload = transfer_payload(path, "in")
    payload["fingerprint"] = fingerprint(path)
    events = []
    def create(path_value, access, sharing, security, creation, flags, template):
        assert path_value == str(path)
        assert access == 0x80000000
        assert sharing == 0x1
        assert creation == 3
        assert flags == 0x80
        events.append("open")
        return 123
    def close(handle):
        assert handle == 123
        events.append("close")
        return 1
    monkeypatch.setattr(worker.sys, "platform", "win32")
    monkeypatch.setattr(worker, "_load_windows_file_api", lambda: SimpleNamespace(CreateFileW=create, CloseHandle=close), raising=False)
    def operation(request, emit):
        assert events == ["open"]
        events.append("native")
        if native_error:
            raise ValueError("Import failed")
        return {"rows": 1}
    client(monkeypatch, operation=operation)
    if native_error:
        with pytest.raises(ValueError, match="Import failed"):
            worker.execute(payload)
    else:
        assert worker.execute(payload)["rows"] == 1
    assert events == ["open", "native", "close"]


def test_windows_input_guard_reports_open_editor_sharing_error(worker, monkeypatch, tmp_path):
    import ctypes
    path = tmp_path / "input.bcp"
    path.write_bytes(b"1\n")
    payload = transfer_payload(path, "in")
    payload["fingerprint"] = fingerprint(path)
    create = lambda *args: ctypes.c_void_p(-1).value
    close = lambda handle: pytest.fail("Invalid handles must not be closed")
    monkeypatch.setattr(worker.sys, "platform", "win32")
    monkeypatch.setattr(worker, "_load_windows_file_api", lambda: SimpleNamespace(CreateFileW=create, CloseHandle=close), raising=False)
    monkeypatch.setattr(worker, "_last_windows_error", lambda: 32, raising=False)
    client(monkeypatch, operation=lambda request, emit: pytest.fail("Locked input must not reach ASE"))
    with pytest.raises(ValueError, match="Close.*editing"):
        worker.execute(payload)


@pytest.mark.skipif(sys.platform != "win32", reason="Actual Windows file sharing requires Windows")
def test_windows_input_guard_blocks_real_writes_delete_and_replace_but_allows_read(worker, tmp_path):
    path = tmp_path / "자료 😀.bcp"
    path.write_bytes(b"reviewed data")
    replacement = tmp_path / "replacement.bcp"
    replacement.write_bytes(b"different data")
    with worker._input_guard(path):
        assert path.read_bytes() == b"reviewed data"
        with pytest.raises(PermissionError):
            path.write_bytes(b"external edit")
        with pytest.raises(PermissionError):
            path.unlink()
        with pytest.raises(PermissionError):
            os.replace(replacement, path)
    path.write_bytes(b"editable again")
    assert path.read_bytes() == b"editable again"


def test_output_transfer_atomically_replaces_unicode_path(worker, monkeypatch, tmp_path):
    path = tmp_path / "资料 导出.bcp"
    path.write_bytes(b"original")
    stage = tmp_path / ".nice-tools-reserved.bcp"
    stage.touch()
    payload = transfer_payload(path, "out")
    payload.update(stage_path=str(stage), out_target_fingerprint=target_fingerprint(path))
    def transfer(request, emit):
        assert request["path"] == str(stage)
        Path(request["path"]).write_bytes(b"complete export")
        assert path.read_bytes() == b"original"
        return {"rows": 3, "direction": "out"}
    client(monkeypatch, operation=transfer)
    assert worker.execute(payload) == {"rows": 3, "direction": "out", "path": str(path)}
    assert path.read_bytes() == b"complete export"
    assert not stage.exists()


def test_deferred_output_preserves_target_until_manager_publishes(worker, monkeypatch, tmp_path):
    path = tmp_path / "output.bcp"
    path.write_bytes(b"original")
    stage = tmp_path / ".nice-tools-reserved.bcp"
    stage.touch()
    payload = transfer_payload(path, "out")
    payload.update(stage_path=str(stage), out_target_fingerprint=target_fingerprint(path), defer_publish=True)
    def transfer(request, emit):
        Path(request["path"]).write_bytes(b"complete export")
        return {"rows": 3, "direction": "out"}
    client(monkeypatch, operation=transfer)
    assert worker.execute(payload) == {"rows": 3, "direction": "out", "path": str(path), "stage_path": str(stage)}
    assert path.read_bytes() == b"original"
    assert stage.read_bytes() == b"complete export"


def test_failed_output_transfer_preserves_original_and_cleans_stage(worker, monkeypatch, tmp_path):
    path = tmp_path / "output.bcp"
    path.write_bytes(b"original")
    stage = tmp_path / ".nice-tools-reserved.bcp"
    stage.touch()
    payload = transfer_payload(path, "out")
    payload.update(stage_path=str(stage), out_target_fingerprint=target_fingerprint(path))
    def transfer(request, emit):
        Path(request["path"]).write_bytes(b"partial")
        raise ValueError("Export failed")
    client(monkeypatch, operation=transfer)
    with pytest.raises(ValueError, match="Export failed"):
        worker.execute(payload)
    assert path.read_bytes() == b"original"
    assert not stage.exists()


@pytest.mark.parametrize("invalid_stage", ["target", "other_directory", "nonempty"])
def test_rejected_stage_never_removes_existing_user_files(worker, monkeypatch, tmp_path, invalid_stage):
    path = tmp_path / "output.bcp"
    path.write_bytes(b"original")
    if invalid_stage == "target":
        stage = path
    elif invalid_stage == "other_directory":
        directory = tmp_path / "other"
        directory.mkdir()
        stage = directory / ".nice-tools-reserved.bcp"
        stage.write_bytes(b"user data")
    else:
        stage = tmp_path / ".nice-tools-reserved.bcp"
        stage.write_bytes(b"user data")
    payload = transfer_payload(path, "out")
    payload.update(stage_path=str(stage), out_target_fingerprint=target_fingerprint(path))
    client(monkeypatch, operation=lambda request, emit: pytest.fail("Invalid stage must never reach ASE"))
    with pytest.raises(ValueError, match="staging"):
        worker.execute(payload)
    assert path.read_bytes() == b"original"
    assert stage.read_bytes() == (b"original" if invalid_stage == "target" else b"user data")


def test_hardlinked_output_stage_is_rejected_without_touching_destination(worker, monkeypatch, tmp_path):
    path = tmp_path / "output.bcp"
    path.touch()
    stage = tmp_path / ".nice-tools-reserved.bcp"
    os.link(path, stage)
    payload = transfer_payload(path, "out")
    payload.update(stage_path=str(stage), out_target_fingerprint=target_fingerprint(path))
    def operation(request, emit):
        Path(request["path"]).write_bytes(b"partial export")
        raise ValueError("Export failed")
    client(monkeypatch, operation=operation)
    with pytest.raises(ValueError, match="staging"):
        worker.execute(payload)
    assert path.read_bytes() == b""
    assert stage.read_bytes() == b""


@pytest.mark.parametrize("during_transfer", [False, True])
def test_output_transfer_rejects_target_changed_since_preview(worker, monkeypatch, tmp_path, during_transfer):
    path = tmp_path / "output.bcp"
    path.write_bytes(b"original")
    payload = transfer_payload(path, "out")
    payload["out_target_fingerprint"] = target_fingerprint(path)
    if not during_transfer:
        path.write_bytes(b"externally changed")
    def transfer(request, emit):
        assert during_transfer, "Stale destination must be rejected before native transfer"
        Path(request["path"]).write_bytes(b"export")
        path.write_bytes(b"externally changed")
        return {"rows": 1}
    client(monkeypatch, operation=transfer)
    with pytest.raises(ValueError, match="changed"):
        worker.execute(payload)
    assert path.read_bytes() == b"externally changed"
    assert list(tmp_path.glob(".nice-tools-*.bcp")) == []


def test_worker_protocol_emits_progress_and_result_with_secret_redaction(worker, monkeypatch):
    def operation(payload, emit):
        print("private-password native accidental print")
        emit({"rows": 2, "detail": "private-password"})
        return {"rows": 2}
    client(monkeypatch, operation=operation)
    stream = io.StringIO()
    code = worker.main(io.StringIO(json.dumps({"operation": "databases", "connection": {"password": "private-password"}})), stream)
    assert code == 0
    assert "private-password" not in stream.getvalue()
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [record["event"] for record in records] == ["progress", "result"]
    assert records[-1]["result"]["rows"] == 2


@pytest.mark.parametrize("password, result", [
    ("id", {"databases": [{"id": 7, "name": "paid", "identity": True}]}),
    ("a", {"path": "C:\\data\\catalog.bcp", "fingerprint": {"sha256": "abcd1234"}, "sample": [["a"]]}),
])
def test_success_protocol_preserves_catalog_paths_hashes_and_samples(worker, monkeypatch, password, result):
    client(monkeypatch, operation=lambda payload, emit: result)
    stream = io.StringIO()
    payload = {"operation": "databases", "connection": {"password": password}}
    assert worker.main(io.StringIO(json.dumps(payload)), stream) == 0
    assert json.loads(stream.getvalue()) == {"event": "result", "result": result}


def test_short_password_redaction_preserves_protocol_and_progress_keys(worker, monkeypatch):
    def operation(payload, emit):
        emit({"message": "password a", "phase": "metadata", "rows": 2})
        return {"rows": 2}
    client(monkeypatch, operation=operation)
    stream = io.StringIO()
    payload = {"operation": "databases", "connection": {"password": "a"}}
    assert worker.main(io.StringIO(json.dumps(payload)), stream) == 0
    progress, result = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert progress["event"] == "progress"
    assert progress["rows"] == 2
    assert progress["phase"] == "metadata"
    assert "[redacted]" in progress["message"]
    assert result == {"event": "result", "result": {"rows": 2}}


@pytest.mark.parametrize("exception, expected", [(ValueError("private-password could not connect"), "could not connect"), (RuntimeError("private-password internals"), "ASE operation failed")])
def test_worker_protocol_returns_sanitized_errors(worker, monkeypatch, exception, expected):
    def operation(payload, emit):
        raise exception
    client(monkeypatch, operation=operation)
    stream = io.StringIO()
    assert worker.main(io.StringIO(json.dumps({"operation": "databases", "connection": {"password": "private-password"}})), stream) == 1
    assert "private-password" not in stream.getvalue()
    assert expected in json.loads(stream.getvalue())["detail"]
    assert "Traceback" not in stream.getvalue()


@pytest.mark.parametrize("input_text", ["{}{}", "[]", '{"operation":"unknown"}'])
def test_worker_rejects_invalid_single_payload(worker, input_text):
    stream = io.StringIO()
    assert worker.main(io.StringIO(input_text), stream) == 1
    assert json.loads(stream.getvalue())["event"] == "error"


def test_worker_metadata_limit_does_not_limit_transfer_file_size(worker, monkeypatch, tmp_path):
    monkeypatch.setattr(worker, "MAX_PAYLOAD_BYTES", 100)
    stream = io.StringIO()
    assert worker.main(io.BytesIO(b" " * 101), stream) == 1
    assert "metadata limit" in json.loads(stream.getvalue())["detail"]
    path = tmp_path / "larger-than-request-limit.bcp"
    path.write_bytes(b"1\n" * 1000)
    payload = transfer_payload(path, "in")
    payload["fingerprint"] = fingerprint(path)
    client(monkeypatch, operation=lambda request, emit: {"rows": 1000})
    assert worker.execute(payload)["rows"] == 1000


def test_worker_real_module_uses_stdin_json_for_status():
    completed = subprocess.run(
        [sys.executable, "-m", "nice_tools.ase_worker"], input=b'{"operation":"status"}',
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        cwd=Path(__file__).resolve().parents[1], timeout=10,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    assert completed.stderr == b""
    records = [json.loads(line) for line in completed.stdout.splitlines()]
    assert len(records) == 1
    assert records[0]["event"] == "result"
    assert isinstance(records[0]["result"]["available"], bool)


def test_catalog_cli_receives_parent_process_registration(worker, monkeypatch):
    handles = []
    registration = lambda handle: handles.append(handle)
    client_handle = object()
    def run(payload, emit=None, register=None):
        assert payload["operation"] == "databases"
        register(client_handle)
        return {"databases": []}
    monkeypatch.setitem(sys.modules, "nice_tools.ase_cli", SimpleNamespace(run_operation=run))
    assert worker.execute({"operation": "databases"}, register=registration) == {"databases": []}
    assert handles == [client_handle]


def test_preview_cli_receives_parent_process_registration(worker, monkeypatch, tmp_path):
    source = tmp_path / "data.txt"
    source.write_bytes(b"1\n")
    registration = lambda handle: None
    def run(payload, emit=None, register=None):
        assert register is registration
        assert payload["operation"] == "columns"
        return {"columns": []}
    monkeypatch.setitem(sys.modules, "nice_tools.ase_cli", SimpleNamespace(run_operation=run))
    monkeypatch.setitem(sys.modules, "nice_tools.ase_files", SimpleNamespace(parse_options=lambda options: options))
    result = worker.execute({"operation": "preview", "options": {"direction": "out"}, "path": str(source)}, register=registration)
    assert result["columns"] == []
