import json
import sys
import time
import threading
from types import SimpleNamespace

import pytest


def manager_for(tmp_path, transfer="success", rows=1, contents=b"new\n"):
    from nice_tools.ase_jobs import AseManager

    script = tmp_path / "worker.py"
    script.write_text('''import sys,json,time
p=json.load(sys.stdin.buffer)
op=p['operation']
if op=='databases': r={'databases':[{'id':1,'name':'db'}]}
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
 r={'rows':ROW_COUNT}
else: r={}
print(json.dumps({'event':'result','result':r}),flush=True)
'''.replace("OUTPUT_BYTES", repr(contents)).replace("ROW_COUNT", repr(rows)).replace(" MODE", {
        "success": " pass",
        "fail": " print(json.dumps({'event':'error','detail':p['connection']['password']+' failed'}),flush=True); sys.exit(1)",
        "wait": " print(json.dumps({'event':'progress','phase':'transfer','message':'running'}),flush=True); time.sleep(60)",
    }[transfer]), encoding="utf-8")
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
