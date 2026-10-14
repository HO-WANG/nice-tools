import json
import sys
import time
import threading
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def sap_client(monkeypatch):
    from nice_tools import ase_cli
    monkeypatch.setattr(ase_cli, "client_status", lambda: {"available": True})
    monkeypatch.setattr(ase_cli, "run_operation", None)
    return ase_cli


def manager_for(tmp_path, transfer="success", rows=1, contents=b"new\n", catalog_timings=None, transfer_metrics=None):
    from nice_tools.ase_jobs import AseManager

    script = tmp_path / "worker.py"
    script.write_text('''import sys,json,time
p=json.load(sys.stdin.buffer)
op=p['operation']
catalog_timings=CATALOG_TIMINGS
if op=='databases': r={'databases':[{'id':1,'name':'db'}]}
elif op in ('owners','objects','columns'): r={op:[]}
elif op=='pick': r={'cancelled':False,'path':p['suggested_name']}
elif op=='preview':
 if p['options']['batch_size']==1111:
  from pathlib import Path
  Path(__file__).with_name('preview-started').touch()
  deadline=time.monotonic()+5
  while not Path(__file__).with_name('preview-release').exists() and time.monotonic()<deadline: time.sleep(.01)
 r={'object':p['object'],'columns':[{'position':1,'name':'x','type_id':56,'type_name':'int','length':4,'precision':10,'scale':0,'nullable':False,'identity':False}],'path':p['path'],'warnings':[]}
elif op=='transfer':
 if 'stage_path' in p:
  open(p['stage_path'],'wb').write(OUTPUT_BYTES)
 MODE
 r={'rows':ROW_COUNT, **TRANSFER_METRICS}
else: r={}
if op in ('databases','owners','objects','columns') and catalog_timings is not None:
 r['timings']=catalog_timings
print(json.dumps({'event':'result','result':r}),flush=True)
'''.replace("OUTPUT_BYTES", repr(contents)).replace("ROW_COUNT", repr(rows)).replace("TRANSFER_METRICS", repr(transfer_metrics or {})).replace("CATALOG_TIMINGS", repr(catalog_timings)).replace(" MODE", {
        "success": " pass",
        "fail": " print(json.dumps({'event':'error','detail':p['connection']['password']+' failed'}),flush=True); sys.exit(1)",
        "wait": " print(json.dumps({'event':'progress','phase':'transfer','message':'running'}),flush=True); time.sleep(60)",
    }[transfer]), encoding="utf-8")
    def run_operation(payload, emit=None, register=None):
        operation = payload["operation"]
        if operation == "databases":
            result = {"databases": [{"id": 1, "name": "db"}]}
        elif operation == "columns":
            if payload.get("options", {}).get("batch_size") == 1111:
                (tmp_path / "preview-started").touch()
                deadline = time.monotonic() + 5
                while not (tmp_path / "preview-release").exists() and time.monotonic() < deadline:
                    time.sleep(.01)
            result = {"object": payload.get("object"), "columns": [{"position": 1, "name": "x", "type_id": 56, "type_name": "int", "length": 4, "precision": 10, "scale": 0, "nullable": False, "identity": False}]}
        else:
            result = {operation: []}
        if catalog_timings is not None:
            result["timings"] = dict(catalog_timings)
        return result
    sys.modules["nice_tools.ase_cli"].run_operation = run_operation
    return AseManager(worker_command=[sys.executable, str(script)])


def selection(manager, path):
    session = manager.connect({"host":"localhost","port":5000,"user":"u","password":"secret-pass"})["session"]
    selected = manager.pick_file(session, "out", str(path))
    review = manager.preview(session, {"database":"db","owner":"dbo","owner_id":1,"id":10,"name":"t","kind":"U"}, selected["file"], {"direction":"out"})["review"]
    return session, review


def finished(manager, session, job):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = manager.job(session, job)
        if result["state"] != "running":
            return result
        time.sleep(0.02)
    pytest.fail("job did not finish")


def test_review_cannot_be_replayed(tmp_path):
    manager = manager_for(tmp_path)
    try:
        session, review = selection(manager, tmp_path / "out.txt")
        job = manager.start(session, review)["job"]
        assert finished(manager, session, job)["state"] == "succeeded"
        with pytest.raises(ValueError):
            manager.start(session, review)
    finally:
        manager.close()


def test_failed_out_removes_stage_and_preserves_destination(tmp_path):
    manager = manager_for(tmp_path, "fail")
    destination = tmp_path / "결과.txt"
    destination.write_bytes(b"original\n")
    try:
        session, review = selection(manager, destination)
        job = manager.start(session, review)["job"]
        result = finished(manager, session, job)
        assert result["state"] == "failed"
        assert "secret-pass" not in json.dumps(result)
        assert destination.read_bytes() == b"original\n"
        assert not list(tmp_path.glob(".nice-tools-*.bcp"))
    finally:
        manager.close()


def test_cancel_terminates_worker_and_removes_stage(tmp_path):
    manager = manager_for(tmp_path, "wait")
    try:
        session, review = selection(manager, tmp_path / "out.txt")
        job = manager.start(session, review)["job"]
        deadline = time.monotonic() + 5
        while manager.job(session, job)["phase"] != "transfer" and time.monotonic() < deadline:
            time.sleep(0.02)
        manager.cancel(session, job)
        result = finished(manager, session, job)
        assert result["state"] == "cancelled"
        assert not list(tmp_path.glob(".nice-tools-*.bcp"))
        assert not (tmp_path / "out.txt").exists()
    finally:
        manager.close()


def test_empty_ase_password_is_supported(tmp_path):
    manager = manager_for(tmp_path)
    try:
        connected = manager.connect({"host": "localhost", "user": "u", "password": ""})
        assert connected["databases"] == [{"id": 1, "name": "db"}]
    finally:
        manager.close()


def test_shutdown_finishes_transfer_cleanup(tmp_path):
    manager = manager_for(tmp_path, "wait")
    session, review = selection(manager, tmp_path / "out.txt")
    job = manager.start(session, review)["job"]
    deadline = time.monotonic() + 5
    while manager.job(session, job)["phase"] != "transfer" and time.monotonic() < deadline:
        time.sleep(0.02)
    manager.close()
    assert not list(tmp_path.glob(".nice-tools-*.bcp"))


def test_late_preview_cannot_invalidate_newer_review(tmp_path):
    manager = manager_for(tmp_path)
    try:
        session = manager.connect({"host":"localhost","user":"u","password":"pw"})["session"]
        selected = manager.pick_file(session, "out", str(tmp_path / "out.txt"))
        obj = {"database":"db","owner":"dbo","owner_id":1,"id":10,"name":"t","kind":"U"}
        old_result = []

        def old_preview():
            try:
                old_result.append(manager.preview(session, obj, selected["file"], {"direction":"out","batch_size":1111}))
            except ValueError as exc:
                old_result.append(exc)

        thread = threading.Thread(target=old_preview)
        thread.start()
        deadline = time.monotonic() + 5
        while not (tmp_path / "preview-started").exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert (tmp_path / "preview-started").exists()
        newer = manager.preview(session, obj, selected["file"], {"direction":"out","batch_size":2222})
        (tmp_path / "preview-release").touch()
        thread.join(timeout=5)
        assert len(old_result) == 1
        assert isinstance(old_result[0], ValueError)
        job = manager.start(session, newer["review"])["job"]
        assert finished(manager, session, job)["state"] == "succeeded"
    finally:
        (tmp_path / "preview-release").touch()
        manager.close()


class TransferClock:
    def __init__(self):
        self.value = 100.0

    def monotonic(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def transfer_clock(monkeypatch, manager, spawn_seconds=0.0, publish_seconds=0.0):
    """Advance a deterministic clock at external worker and filesystem boundaries."""
    from nice_tools import ase_jobs

    clock = TransferClock()
    spawn = manager._spawn
    replace = ase_jobs.os.replace

    def timed_spawn(payload, session=None):
        if payload["operation"] == "transfer":
            clock.advance(spawn_seconds)
        return spawn(payload, session)

    def timed_replace(source, destination):
        replace(source, destination)
        clock.advance(publish_seconds)

    monkeypatch.setattr(ase_jobs, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(manager, "_spawn", timed_spawn)
    monkeypatch.setattr(ase_jobs.os, "replace", timed_replace)
    return clock


def test_out_statistics_include_startup_and_atomic_publication(tmp_path, monkeypatch):
    contents = "첫째\n둘째\n".encode("utf-8") * 6
    manager = manager_for(tmp_path, rows=12, contents=contents)
    destination = tmp_path / "결과.txt"
    try:
        session, review = selection(manager, destination)
        transfer_clock(monkeypatch, manager, spawn_seconds=2.0, publish_seconds=3.0)
        job = manager.start(session, review)["job"]
        result = finished(manager, session, job)
        assert result["state"] == "succeeded"
        assert result["result"]["rows"] == 12
        assert result["result"]["statistics"] == {
            "elapsed_seconds": 5.0,
            "rows_per_second": 2.4,
            "bytes_written": len(contents),
        }
        assert destination.read_bytes() == contents
        assert "started_at" not in result
        # Statistics describe the completed export, not a later edit of its file.
        destination.write_bytes(b"edited later")
        assert manager.job(session, job)["result"]["statistics"]["bytes_written"] == len(contents)
    finally:
        manager.close()


@pytest.mark.parametrize("seconds, rows, contents, expected_rate", [
    (0.0, 1, b"1\n", None),
    (0.0, 0, b"", None),
    (2.0, 0, b"", 0.0),
])
def test_out_statistics_handle_zero_duration_and_empty_output(tmp_path, monkeypatch, seconds, rows, contents, expected_rate):
    manager = manager_for(tmp_path, rows=rows, contents=contents)
    try:
        session, review = selection(manager, tmp_path / "out.txt")
        transfer_clock(monkeypatch, manager, spawn_seconds=seconds)
        job = manager.start(session, review)["job"]
        result = finished(manager, session, job)
        assert result["state"] == "succeeded"
        assert result["result"]["statistics"] == {
            "elapsed_seconds": seconds,
            "rows_per_second": expected_rate,
            "bytes_written": len(contents),
        }
    finally:
        manager.close()


def test_failed_out_reports_elapsed_without_completed_statistics(tmp_path, monkeypatch):
    manager = manager_for(tmp_path, "fail")
    destination = tmp_path / "out.txt"
    destination.write_bytes(b"original\n")
    try:
        session, review = selection(manager, destination)
        transfer_clock(monkeypatch, manager, spawn_seconds=1.5)
        job = manager.start(session, review)["job"]
        result = finished(manager, session, job)
        assert result["state"] == "failed"
        assert result["elapsed_seconds"] == 1.5
        assert "result" not in result
        assert "statistics" not in result
        assert "started_at" not in result
        assert destination.read_bytes() == b"original\n"
    finally:
        manager.close()


def test_cancelled_out_reports_elapsed_without_completed_statistics(tmp_path, monkeypatch):
    manager = manager_for(tmp_path, "wait")
    destination = tmp_path / "out.txt"
    destination.write_bytes(b"original\n")
    spawned = threading.Event()
    try:
        session, review = selection(manager, destination)
        clock = transfer_clock(monkeypatch, manager)
        spawn = manager._spawn

        def ready_spawn(payload, token=None):
            process = spawn(payload, token)
            if payload["operation"] == "transfer":
                spawned.set()
            return process

        monkeypatch.setattr(manager, "_spawn", ready_spawn)
        job = manager.start(session, review)["job"]
        assert spawned.wait(timeout=5)
        clock.advance(4.0)
        manager.cancel(session, job)
        manager.sessions[session]["jobs"][job]["thread"].join(timeout=5)
        result = manager.job(session, job)
        assert result["state"] == "cancelled"
        assert result["elapsed_seconds"] == 4.0
        assert "result" not in result
        assert "statistics" not in result
        assert "started_at" not in result
        assert destination.read_bytes() == b"original\n"
        assert not list(tmp_path.glob(".nice-tools-*.bcp"))
    finally:
        manager.close()


@pytest.mark.parametrize("rows", [None, True, -1, "1", 1.5, pytest.param(1 << 4096, id="oversized")])
def test_invalid_completed_row_count_cannot_publish_out(tmp_path, rows):
    manager = manager_for(tmp_path, rows=rows)
    destination = tmp_path / "out.txt"
    destination.write_bytes(b"original\n")
    try:
        session, review = selection(manager, destination)
        job = manager.start(session, review)["job"]
        result = finished(manager, session, job)
        assert result["state"] == "failed"
        assert "result" not in result
        assert destination.read_bytes() == b"original\n"
    finally:
        manager.close()


def test_output_publish_failure_has_no_completed_statistics(tmp_path, monkeypatch):
    from nice_tools import ase_jobs

    manager = manager_for(tmp_path)
    destination = tmp_path / "out.txt"
    destination.write_bytes(b"original\n")
    try:
        session, review = selection(manager, destination)
        clock = transfer_clock(monkeypatch, manager, spawn_seconds=2.0)

        def failed_replace(source, target):
            clock.advance(3.0)
            raise PermissionError("destination is open")

        monkeypatch.setattr(ase_jobs.os, "replace", failed_replace)
        job = manager.start(session, review)["job"]
        result = finished(manager, session, job)
        assert result["state"] == "failed"
        assert result["elapsed_seconds"] == 5.0
        assert "result" not in result
        assert destination.read_bytes() == b"original\n"
    finally:
        manager.close()


def test_in_does_not_report_out_statistics(tmp_path, monkeypatch):
    manager = manager_for(tmp_path)
    source = tmp_path / "in.txt"
    source.write_bytes(b"1\n")
    try:
        session = manager.connect({"host": "localhost", "user": "u", "password": "pw"})["session"]
        selected = manager.pick_file(session, "in", str(source))
        review = manager.preview(session, {"database": "db", "owner": "dbo", "owner_id": 1, "id": 10, "name": "t", "kind": "U"}, selected["file"], {"direction": "in"})["review"]
        transfer_clock(monkeypatch, manager, spawn_seconds=2.0)
        job = manager.start(session, review)["job"]
        result = finished(manager, session, job)
        assert result["state"] == "succeeded"
        assert result["result"] == {"rows": 1, "path": str(source.resolve()), "direction": "in"}
        assert "started_at" not in result
    finally:
        manager.close()


def catalog_clock(monkeypatch, manager):
    """Measure the direct SAP CLI boundary, with no application subprocess."""
    from nice_tools import ase_jobs
    clock = TransferClock()
    operation = sys.modules["nice_tools.ase_cli"].run_operation
    def timed_operation(*args, **kwargs):
        clock.advance(5.0)
        return operation(*args, **kwargs)
    monkeypatch.setattr(sys.modules["nice_tools.ase_cli"], "run_operation", timed_operation)
    monkeypatch.setattr(ase_jobs, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(manager, "_spawn", lambda *args, **kwargs: pytest.fail("Catalog queries must not start the application EXE"))
    return clock


def test_connect_preserves_client_timings_and_parent_total(tmp_path, monkeypatch):
    timings = {"client_seconds": 4.75}
    manager = manager_for(tmp_path, catalog_timings=timings)
    try:
        catalog_clock(monkeypatch, manager)
        result = manager.connect({"host": "localhost", "user": "u", "password": "secret-pass"})
        assert result["databases"] == [{"id": 1, "name": "db"}]
        assert result["session"] in manager.sessions
        assert result["timings"] == {**timings, "total_seconds": 5.0}
        assert "secret-pass" not in json.dumps(result)
    finally:
        manager.close()


@pytest.mark.parametrize("operation", ["owners", "objects", "columns"])
def test_catalog_calls_sap_cli_without_application_worker(tmp_path, monkeypatch, operation):
    timings = {"client_seconds": 4.75}
    manager = manager_for(tmp_path, catalog_timings=timings)
    try:
        session = manager.connect({"host": "localhost", "user": "u", "password": "pw"})["session"]
        catalog_clock(monkeypatch, manager)
        result = manager.catalog(session, operation, database="db")
        assert result["timings"] == {**timings, "total_seconds": 5.0}
        assert operation in result
    finally:
        manager.close()


def test_status_resolves_clients_without_application_worker(tmp_path, monkeypatch):
    manager = manager_for(tmp_path)
    monkeypatch.setattr(manager, "_spawn", lambda *args, **kwargs: pytest.fail("Status must not start the application EXE"))
    assert manager.status() == {"available": True, "platform": sys.platform}


def test_preview_queries_sap_cli_without_application_worker(tmp_path, monkeypatch):
    manager = manager_for(tmp_path)
    try:
        session = manager.connect({"host": "localhost", "user": "u", "password": "pw"})["session"]
        selected = manager.pick_file(session, "out", str(tmp_path / "out.txt"))
        monkeypatch.setattr(manager, "_spawn", lambda *args, **kwargs: pytest.fail("Preview must not start the application EXE"))
        result = manager.preview(session, {"database":"db", "owner":"dbo", "owner_id":1, "id":10, "name":"t", "kind":"U"}, selected["file"], {"direction":"out"})
        assert result["columns"][0]["name"] == "x"
        assert result["review"] in manager.sessions[session]["reviews"]
    finally:
        manager.close()


def test_direct_catalog_registers_process_for_disconnect(tmp_path, monkeypatch):
    manager = manager_for(tmp_path)
    session = manager.connect({"host": "localhost", "user": "u", "password": "pw"})["session"]
    entered = threading.Event()
    killed = threading.Event()
    errors = []
    class Handle:
        pid = 12345
        def poll(self):
            return None
        def kill(self):
            killed.set()
        def wait(self, timeout=None):
            return None
    handle = Handle()
    def operation(payload, emit=None, register=None):
        cleanup = register(handle)
        entered.set()
        try:
            assert killed.wait(timeout=5)
            return {"owners": []}
        finally:
            cleanup()
    monkeypatch.setattr(sys.modules["nice_tools.ase_cli"], "run_operation", operation)
    def query():
        try:
            manager.catalog(session, "owners", database="db")
        except ValueError as error:
            errors.append(error)
    thread = threading.Thread(target=query)
    thread.start()
    try:
        assert entered.wait(timeout=5)
        assert manager.processes[handle] == session
        manager.disconnect(session)
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert killed.is_set()
        assert handle not in manager.processes
        assert len(errors) == 1
    finally:
        killed.set()
        thread.join(timeout=5)
        manager.close()


def test_connect_cannot_create_session_after_manager_shutdown(tmp_path, monkeypatch):
    manager = manager_for(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    errors = []
    def operation(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return {"databases": []}
    monkeypatch.setattr(sys.modules["nice_tools.ase_cli"], "run_operation", operation)
    def connect():
        try:
            manager.connect({"host": "localhost", "user": "u", "password": "pw"})
        except ValueError as error:
            errors.append(error)
    thread = threading.Thread(target=connect)
    thread.start()
    try:
        assert entered.wait(timeout=5)
        manager.close()
        release.set()
        thread.join(timeout=5)
        assert not manager.sessions
        assert len(errors) == 1
    finally:
        release.set()
        thread.join(timeout=5)
        manager.close()


def _child_running(pid):
    if sys.platform == "win32":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel.GetExitCodeProcess.restype = ctypes.c_int32
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int32
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_uint32()
            assert kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        import os
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = __import__("pathlib").Path(f"/proc/{pid}/stat")
    if stat.exists() and stat.read_text().split()[2] == "Z":
        return False
    return True


@pytest.mark.parametrize("action", ["cancel", "disconnect", "close"])
def test_transfer_cancellation_terminates_real_client_child(tmp_path, action):
    from pathlib import Path
    from nice_tools.ase_jobs import AseManager
    manager = manager_for(tmp_path)
    child_code = "import time; from pathlib import Path; p=Path(" + repr(str(tmp_path / "child-ready")) + "); p.touch(); time.sleep(60)"
    worker = tmp_path / "client-worker.py"
    worker.write_text("\n".join([
        "import json, subprocess, sys, time", "from pathlib import Path", "payload=json.load(sys.stdin)",
        "child=subprocess.Popen([sys.executable, '-c', " + repr(child_code) + "])",
        "Path(" + repr(str(tmp_path / "child-pid")) + ").write_text(str(child.pid))",
        "Path(payload['stage_path']).write_bytes(b'partial export')",
        "print(json.dumps({'event':'progress','phase':'transfer','message':'running'}), flush=True)",
        "time.sleep(60)",
    ]), encoding="utf-8")
    destination = tmp_path / "out.txt"
    destination.write_bytes(b"original")
    child_pid = None
    try:
        session, review = selection(manager, destination)
        manager.worker_command = [sys.executable, str(worker)]
        job = manager.start(session, review)["job"]
        deadline = time.monotonic() + 5
        while not (tmp_path / "child-ready").exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert (tmp_path / "child-ready").exists()
        child_pid = int((tmp_path / "child-pid").read_text())
        assert _child_running(child_pid)
        thread = manager.sessions[session]["jobs"][job]["thread"]
        if action == "cancel":
            manager.cancel(session, job)
        elif action == "disconnect":
            manager.disconnect(session)
        else:
            manager.close()
        thread.join(timeout=5)
        assert not thread.is_alive()
        deadline = time.monotonic() + 5
        while _child_running(child_pid) and time.monotonic() < deadline:
            time.sleep(.01)
        assert not _child_running(child_pid), "A bcp/isql child survived cancellation"
        assert destination.read_bytes() == b"original"
        assert not list(tmp_path.glob(".nice-tools-*.bcp"))
        assert not manager.processes
        assert not manager.worker_jobs
    finally:
        manager.close()
        if child_pid is not None and _child_running(child_pid):
            import os
            import signal
            os.kill(child_pid, signal.SIGTERM)


def test_worker_timeout_terminates_real_client_child(tmp_path):
    from nice_tools.ase_jobs import AseManager
    import subprocess
    manager = manager_for(tmp_path)
    child_code = "import time; from pathlib import Path; Path(" + repr(str(tmp_path / "child-ready")) + ").touch(); time.sleep(60)"
    script = tmp_path / "timeout-worker.py"
    script.write_text("\n".join([
        "import json, subprocess, sys, time", "from pathlib import Path", "json.load(sys.stdin)",
        "child=subprocess.Popen([sys.executable, '-c', " + repr(child_code) + "])",
        "Path(" + repr(str(tmp_path / "child-pid")) + ").write_text(str(child.pid))", "time.sleep(60)",
    ]), encoding="utf-8")
    manager.worker_command = [sys.executable, str(script)]
    child_pid = None
    try:
        with pytest.raises(ValueError, match="초과"):
            manager._invoke({"operation": "pick"}, timeout=1.0)
        assert (tmp_path / "child-ready").exists()
        child_pid = int((tmp_path / "child-pid").read_text())
        deadline = time.monotonic() + 5
        while _child_running(child_pid) and time.monotonic() < deadline:
            time.sleep(.01)
        assert not _child_running(child_pid)
        assert not manager.processes
        assert not manager.worker_jobs
    finally:
        manager.close()
        if child_pid is not None and _child_running(child_pid):
            import os
            import signal
            os.kill(child_pid, signal.SIGTERM)


def test_worker_job_assignment_precedes_request_stdin(tmp_path, monkeypatch):
    from nice_tools import ase_jobs
    events = []
    class Guard:
        def assign(self, pid):
            events.append(("assign", pid))
        def terminate(self):
            events.append("terminate")
        def close(self):
            events.append("close")
    class Process:
        pid = 123
        returncode = 0
        stdin = stdout = stderr = None
        def communicate(self, payload, timeout):
            assert events == [("assign", 123), ("resume", 123)]
            events.append("stdin")
            return json.dumps({"event":"result", "result": {"cancelled": True}}), ""
        def poll(self):
            return 0
        def wait(self, timeout=None):
            return 0
    def spawn(*args, **kwargs):
        assert kwargs["start_new_session"] is False
        assert kwargs["creationflags"] == 0x8000004
        return Process()
    manager = manager_for(tmp_path)
    monkeypatch.setitem(sys.modules, "nice_tools.ase_process", SimpleNamespace(WindowsJob=Guard, resume_process=lambda pid: events.append(("resume", pid))))
    monkeypatch.setattr(ase_jobs, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(ase_jobs.subprocess, "CREATE_NO_WINDOW", 0x8000000, raising=False)
    monkeypatch.setattr(ase_jobs.subprocess, "Popen", spawn)
    assert manager._invoke({"operation":"pick"}) == {"cancelled": True}
    assert events == [("assign", 123), ("resume", 123), "stdin", "terminate", "close"]
    assert not manager.worker_jobs


def test_out_statistics_separate_bcp_runtime_from_entire_export(tmp_path, monkeypatch):
    manager = manager_for(tmp_path, rows=12, transfer_metrics={"bcp_seconds": 2.0, "bcp_rows_per_second": 6.0})
    try:
        session, review = selection(manager, tmp_path / "out.txt")
        transfer_clock(monkeypatch, manager, spawn_seconds=3.0, publish_seconds=5.0)
        job = manager.start(session, review)["job"]
        result = finished(manager, session, job)
        assert result["state"] == "succeeded"
        assert result["result"]["statistics"] == {
            "elapsed_seconds": 8.0, "rows_per_second": 1.5, "bytes_written": 4,
            "bcp_seconds": 2.0, "bcp_rows_per_second": 6.0,
        }
    finally:
        manager.close()


@pytest.mark.parametrize("failure", ["create", "assign", "resume"])
def test_worker_security_setup_failure_kills_suspended_process(tmp_path, monkeypatch, failure):
    from nice_tools import ase_jobs
    events = []
    class Guard:
        def __init__(self):
            if failure == "create":
                raise OSError("Job creation failed")
        def assign(self, pid):
            events.append("assign")
            if failure == "assign":
                raise OSError("Job assignment failed")
        def close(self):
            events.append("job-close")
    def resume(pid):
        events.append("resume")
        if failure == "resume":
            raise OSError("Resume failed")
    class Process:
        pid = 123
        stdin = stdout = stderr = None
        dead = False
        def poll(self):
            return -9 if self.dead else None
        def kill(self):
            self.dead = True
            events.append("process-kill")
        def wait(self):
            assert self.dead
            events.append("wait")
    process = Process()
    manager = manager_for(tmp_path)
    monkeypatch.setitem(sys.modules, "nice_tools.ase_process", SimpleNamespace(WindowsJob=Guard, resume_process=resume))
    monkeypatch.setattr(ase_jobs, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(ase_jobs.subprocess, "CREATE_NO_WINDOW", 0x8000000, raising=False)
    monkeypatch.setattr(ase_jobs.subprocess, "Popen", lambda *args, **kwargs: process)
    with pytest.raises(OSError):
        manager._spawn({"operation":"pick"})
    assert process.dead
    assert events[-2:] == ["process-kill", "wait"]
    assert not manager.processes
    assert not manager.worker_jobs
