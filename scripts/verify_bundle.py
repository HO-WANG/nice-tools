"""배포 파일 하나만 복사해 실행하고 실제 화면·업로드 요청을 검사한다."""

import argparse
import os
import queue
import re
import shutil
import signal
import struct
import subprocess
import tempfile
import threading
import json
from pathlib import Path

from smoke import data_source_checks, main as smoke_main


def validate_windows_x64(path: Path):
    if path.name != "nice-tools.exe":
        raise ValueError("Distribution must be named nice-tools.exe")
    with path.open("rb") as file:
        header = file.read(64)
        if header[:2] != b"MZ" or len(header) < 64:
            raise ValueError("Distribution is not a Windows executable")
        file.seek(struct.unpack_from("<I", header, 60)[0])
        pe = file.read(6)
        if pe[:4] != b"PE\0\0" or pe[4:] != b"\x64\x86":
            raise ValueError("Distribution must be Windows x64")


def stop_process(process):
    if process.poll() is not None:
        return
    try:
        process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
        process.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=15)
        else:
            process.kill()
        process.wait(timeout=10)


def spawn_worker(executable, directory):
    guard = None
    process = subprocess.Popen(
        [str(executable), "--ase-worker"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=directory,
        creationflags=(subprocess.CREATE_NO_WINDOW | 0x00000004) if os.name == "nt" else 0,
        start_new_session=os.name != "nt",
    )
    try:
        if os.name == "nt":
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from nice_tools.ase_process import WindowsJob, resume_process
            guard = WindowsJob(process.pid)
            resume_process(process.pid)
        return process, guard
    except BaseException:
        if guard is not None:
            guard.close()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        for pipe in (process.stdin, process.stdout, process.stderr):
            pipe.close()
        raise


def worker_request(executable, payload, directory, expect_error=False):
    process, guard = spawn_worker(executable, directory)
    try:
        stdout, stderr = process.communicate(json.dumps(payload).encode(), timeout=60)
        returncode = process.returncode
    finally:
        if guard is not None:
            guard.close()
        elif process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
        for pipe in (process.stdin, process.stdout, process.stderr):
            pipe.close()
    password = payload["connection"]["password"].encode()
    assert password not in stdout and password not in stderr, "Worker emitted private credentials"
    events = [json.loads(line) for line in stdout.splitlines()]
    if expect_error:
        assert returncode != 0 and any(event["event"] == "error" for event in events), "Expected a failed transfer"
        return events
    assert returncode == 0, "Frozen ASE worker failed"
    results = [event["result"] for event in events if event["event"] == "result"]
    assert len(results) == 1, "Frozen worker did not return one result"
    return results[0]


def odbc_worker_checks(executable, directory, password, require_odbc, invalid_dsn):
    """Load real frozen pyodbc; never simulate catalog success without ASE."""
    connection = {"host": "fixture.internal", "port": 5000, "user": "smoke", "password": password}
    status = worker_request(executable, {"operation": "status", "connection": connection}, directory)
    data_source_checks(status["odbc"])
    if require_odbc:
        assert status["odbc"]["module_available"] is True, "Frozen worker could not import native pyodbc"
        assert status["odbc"]["version"] == "5.3.0", "Frozen worker did not load pinned native pyodbc"
    events = worker_request(executable, {"operation": "databases", "connection": connection}, directory, expect_error=True)
    assert any("ODBC" in event.get("detail", "") for event in events), "Worker did not report invalid ODBC driver"
    dsn_connection = {**connection, "dsn": invalid_dsn}
    dsn_events = worker_request(executable, {"operation": "databases", "connection": dsn_connection}, directory, expect_error=True)
    assert any("DSN" in event.get("detail", "") for event in dsn_events), "Frozen worker lost the selected DSN"
    target = Path(directory) / "한글 preserved.bcp"
    target.write_bytes(b"existing destination\n")
    stage = Path(directory) / ".nice-tools-smoke-out.bcp"
    stage.touch()
    obj = {"database": "fixture_db", "owner": "dbo", "owner_id": 7, "id": 42, "name": "orders", "kind": "U"}
    columns = [{"position": 1, "name": "id", "type_id": 56, "type_name": "int", "length": 4,
                "precision": 0, "scale": 0, "nullable": False, "identity": False}]
    transfer_events = worker_request(executable, {"operation": "transfer", "connection": dsn_connection, "object": obj,
                   "path": str(target), "stage_path": str(stage), "defer_publish": True,
                   "columns": columns, "options": {"direction": "out", "encoding": "utf-8"},
                   "out_target_fingerprint": {"exists": True, "size": target.stat().st_size,
                                              "mtime_ns": target.stat().st_mtime_ns}}, directory, expect_error=True)
    if status.get("bcp_available") is True:
        assert any("DSN" in event.get("detail", "") for event in transfer_events), "Fresh transfer schema check lost the selected DSN"
    assert target.read_bytes() == b"existing destination\n", "ODBC schema failure replaced an existing export"
    assert not stage.exists(), "ODBC schema failure retained reserved staging"
    target.unlink()
    target = "Frozen Windows" if require_odbc else "Development"
    print(f"{target} ODBC worker smoke: real native extension/driver/DSN discovery, sanitized invalid-DSN errors and export preservation passed; no live ASE transfer tested.")


def verify(path: Path, allow_non_windows: bool = False, sap_test_clients: Path | None = None):
    if not allow_non_windows:
        validate_windows_x64(path)
    with tempfile.TemporaryDirectory(prefix="nice-tools 실행 검사 ") as directory:
        executable = Path(directory) / path.name
        shutil.copy2(path, executable)
        assert len(list(Path(directory).iterdir())) == 1
        process = subprocess.Popen(
            [str(executable), "--no-browser"], cwd=directory,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        output = queue.Queue()

        def read_output():
            for line in process.stdout:
                output.put(line)
            output.put(None)

        threading.Thread(target=read_output, daemon=True).start()
        lines = []
        try:
            while True:
                line = output.get(timeout=60)
                if line is None:
                    raise RuntimeError("Executable exited before readiness: " + "".join(lines))
                lines.append(line)
                match = re.search(r"http://127\.0\.0\.1:\d+", line)
                if match:
                    smoke_args = ["--url", match.group(), "--large-files", "--config-folder", directory]
                    if not allow_non_windows:
                        smoke_args += ["--require-odbc"]
                    if sap_test_clients is not None:
                        smoke_args += ["--sap-test-clients", str(sap_test_clients), "--executable", str(executable)]
                    smoke_main(smoke_args)
                    break
            if process.poll() is not None:
                raise RuntimeError("Executable exited unexpectedly")
        finally:
            stop_process(process)
    print("Single-file bundle: isolated execution, bundled assets and real comparison passed.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("--allow-non-windows", action="store_true", help="Development check only; never a Windows release validation")
    parser.add_argument("--sap-test-clients", type=Path, help="External developer-only compiled console fixture folder")
    args = parser.parse_args()
    verify(args.executable.resolve(), args.allow_non_windows,
           args.sap_test_clients.resolve(strict=True) if args.sap_test_clients else None)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}".replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print("::error::Single EXE verification failed: " + detail, flush=True)
        raise
