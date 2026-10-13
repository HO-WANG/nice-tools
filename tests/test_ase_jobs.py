import json
import sys
import time

import pytest


def manager_for(tmp_path, transfer="success"):
    from nice_tools.ase_jobs import AseManager

    script = tmp_path / "worker.py"
    script.write_text('''import sys,json,time
p=json.load(sys.stdin.buffer)
op=p['operation']
if op=='databases': r={'databases':[{'id':1,'name':'db'}]}
elif op=='pick': r={'cancelled':False,'path':p['suggested_name']}
elif op=='preview':
 r={'object':p['object'],'columns':[{'position':1,'name':'x','type_id':56,'type_name':'int','length':4,'precision':10,'scale':0,'nullable':False,'identity':False}],'path':p['path'],'warnings':[]}
elif op=='transfer':
 if 'stage_path' in p:
  open(p['stage_path'],'wb').write(b'new\\n')
 MODE
 r={'rows':1}
else: r={}
print(json.dumps({'event':'result','result':r}),flush=True)
'''.replace(" MODE", {
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
