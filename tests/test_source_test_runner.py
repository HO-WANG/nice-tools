"""The CI watchdog must clean up a failed Windows ownership setup."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_failed_windows_job_setup_kills_suspended_child_and_preserves_error(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts" / "run_source_tests.py"
    spec = importlib.util.spec_from_file_location("source_test_watchdog", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    process = SimpleNamespace(
        pid=123, returncode=None,
        stdout=SimpleNamespace(close=lambda: calls.append("close-output")),
        poll=lambda: process.returncode,
        wait=lambda **kwargs: calls.append("wait"),
    )

    def kill():
        calls.append("kill-exact-process")
        process.returncode = -1

    process.kill = kill
    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt", environ={}))
    monkeypatch.setattr(module, "subprocess", SimpleNamespace(
        Popen=lambda *args, **kwargs: process, PIPE=-1, STDOUT=-2, CREATE_NO_WINDOW=0x08000000,
    ))
    from nice_tools import ase_process

    def failed_job(pid):
        raise OSError("original Windows Job setup failure")

    monkeypatch.setattr(ase_process, "WindowsJob", failed_job)
    with pytest.raises(OSError, match="original Windows Job setup failure"):
        module.run(300, [])
    assert calls == ["kill-exact-process", "wait", "close-output"]
