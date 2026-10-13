import os
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def start_desktop(tmp_path, *arguments, reader_delay=0):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    process = subprocess.Popen(
        [sys.executable, "-m", "nice_tools.desktop", "--no-browser", *arguments],
        cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    output = queue.Queue()

    def read_output():
        time.sleep(reader_delay)
        for line in process.stdout:
            output.put(line)
        output.put(None)

    threading.Thread(target=read_output, daemon=True).start()
    return process, output


def stop_desktop(process):
    if process.poll() is None:
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
            process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            # A noninteractive Windows worker may have no console for CTRL_BREAK.
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            return False
    return True


def wait_for_url(process, output):
    lines = []
    while True:
        try:
            line = output.get(timeout=20)
        except queue.Empty:
            pytest.fail("Desktop app did not become ready: " + "".join(lines))
        if line is None:
            pytest.fail("Desktop app exited before readiness: " + "".join(lines))
        lines.append(line)
        match = re.search(r"http://127\.0\.0\.1:\d+", line)
        if match:
            return match.group()


def test_desktop_serves_comparison_from_another_directory(tmp_path):
    process, output = start_desktop(tmp_path)
    try:
        url = wait_for_url(process, output)
        check = subprocess.run([sys.executable, str(ROOT / "scripts/smoke.py"), "--url", url], capture_output=True, text=True, timeout=30)
        assert check.returncode == 0, check.stdout + check.stderr
        assert process.poll() is None
    finally:
        graceful = stop_desktop(process)
    if graceful:
        assert process.returncode == 0
    else:
        assert os.name == "nt", "Interactive shutdown should work on the Linux test host"


def test_automatic_port_does_not_depend_on_port_8000(tmp_path):
    first, first_output = start_desktop(tmp_path)
    second, second_output = start_desktop(tmp_path)
    try:
        first_url = wait_for_url(first, first_output)
        second_url = wait_for_url(second, second_output)
        assert first_url != second_url
    finally:
        stop_desktop(first)
        stop_desktop(second)


@pytest.mark.parametrize("reader_delay", [0, 0.5])
def test_occupied_explicit_port_fails_cleanly(tmp_path, reader_delay):
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        process, output = start_desktop(tmp_path, "--port", str(occupied.getsockname()[1]), reader_delay=reader_delay)
        try:
            process.wait(timeout=10)
            assert process.returncode == 1
            text = ""
            while True:
                line = output.get(timeout=5)
                if line is None:
                    break
                text += line
            assert "Could not start nice-tools" in text
        finally:
            stop_desktop(process)


def test_browser_opens_only_after_server_start(monkeypatch):
    try:
        from nice_tools.desktop import open_when_ready
    except ImportError:
        pytest.fail("Desktop browser launcher is not implemented")
    from types import SimpleNamespace
    server = SimpleNamespace(started=False, should_exit=False)
    urls = []
    task = threading.Thread(target=open_when_ready, args=(server, "http://127.0.0.1:12345", urls.append))
    task.start()
    assert urls == []
    server.started = True
    task.join(timeout=3)
    assert not task.is_alive()
    assert urls == ["http://127.0.0.1:12345"]


def test_startup_failure_does_not_open_browser():
    try:
        from nice_tools.desktop import open_when_ready
    except ImportError:
        pytest.fail("Desktop browser launcher is not implemented")
    from types import SimpleNamespace
    urls = []
    open_when_ready(SimpleNamespace(started=False, should_exit=True), "http://127.0.0.1:12345", urls.append)
    assert urls == []


def test_cleanup_handles_unavailable_console_signal(tmp_path, monkeypatch):
    process, output = start_desktop(tmp_path)
    original = process.send_signal

    def unavailable_interrupt(value):
        if value == (signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT):
            raise OSError("No attached console")
        return original(value)

    try:
        wait_for_url(process, output)
        monkeypatch.setattr(process, "send_signal", unavailable_interrupt)
        assert stop_desktop(process) is False
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
