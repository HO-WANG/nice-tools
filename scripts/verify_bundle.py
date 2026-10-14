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
import time
from pathlib import Path

from smoke import main as smoke_main


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
    assert returncode == 0, "Frozen SAP worker failed with fixture clients"
    results = [event["result"] for event in events if event["event"] == "result"]
    assert len(results) == 1, "Frozen worker did not return one result"
    return results[0]


def sap_worker_checks(executable, directory, clients):
    """Actual frozen workers, external console PE clients, fixture data only."""
    directory = Path(directory)
    connection = {"host": "fixture.internal", "port": 5000, "user": "smoke",
                  "password": "fixture-db-password-never-log!42"}
    obj = {"database": "fixture_db", "owner": "dbo", "owner_id": 7,
           "id": 42, "name": "orders", "kind": "U"}
    base = {"connection": connection, "object": obj}
    target = directory / "한글 data.bcp"
    target.write_bytes(b"existing destination\n")
    out = {"direction": "out", "encoding": "utf-8", "field_terminator": r"\t", "row_terminator": r"\n"}
    preview = worker_request(executable, {**base, "operation": "preview", "path": str(target), "options": out}, directory)
    assert preview["out_target_fingerprint"]["exists"]
    assert preview["columns"][0]["name"] == "id"
    stage = directory / ".nice-tools-smoke-out.bcp"
    stage.touch()
    transfer = {**base, "operation": "transfer", "path": str(target), "options": out,
                "columns": preview["columns"], "out_target_fingerprint": preview["out_target_fingerprint"],
                "stage_path": str(stage), "defer_publish": True}
    result = worker_request(executable, transfer, directory)
    assert result["rows"] == 3 and result["direction"] == "out"
    assert result["bcp_seconds"] > 0 and result["bcp_rows_per_second"] > 0
    assert target.read_bytes() == b"existing destination\n", "Deferred worker bypassed parent publication"
    assert stage.read_bytes() == b"1\n2\n3\n"
    os.replace(stage, target)
    incoming = {**out, "direction": "in", "batch_size": 2}
    preview_in = worker_request(executable, {**base, "operation": "preview", "path": str(target), "options": incoming}, directory)
    assert preview_in["rows"] == 3 and preview_in["fingerprint"]["size"] == 6
    result_in = worker_request(executable, {**base, "operation": "transfer", "path": str(target), "options": incoming,
                              "columns": preview_in["columns"], "fingerprint": preview_in["fingerprint"]}, directory)
    assert result_in["rows"] == 3 and result_in["direction"] == "in"

    failed = {**obj, "id": 44, "name": "fail_me"}
    stage.touch()
    transfer.update(object=failed, out_target_fingerprint={"exists": True, "size": target.stat().st_size,
                                                         "mtime_ns": target.stat().st_mtime_ns})
    worker_request(executable, transfer, directory, expect_error=True)
    assert target.read_bytes() == b"1\n2\n3\n" and not stage.exists(), "Failed export changed destination or retained staging"

    # The real parent owns its worker tree with this same Windows Job mechanism.
    # Reserved staging makes cleanup deterministic even if the worker is killed.
    if os.name == "nt":
        stage.touch()
        transfer.update(object={**obj, "id": 43, "name": "cancel_me"})
        process, guard = spawn_worker(executable, directory)
        try:
            process.stdin.write(json.dumps(transfer).encode())
            process.stdin.close()
            deadline = time.monotonic() + 30
            while stage.stat().st_size == 0 and time.monotonic() < deadline:
                time.sleep(.05)
            assert stage.stat().st_size == 6, "Cancellation fixture did not start the external BCP process"
            commands = (Path(clients) / "fixture-commands.log").read_text(encoding="utf-8")
            child_pid = int(re.findall(r"bcp\.exe\tPID=([0-9]+)", commands)[-1])
            guard.terminate()
            process.wait(timeout=10)
            assert target.read_bytes() == b"1\n2\n3\n", "Cancelled worker changed the existing destination"
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            child = kernel.OpenProcess(0x1000, False, child_pid)
            if child:
                try:
                    status = wintypes.DWORD()
                    child_deadline = time.monotonic() + 5
                    while kernel.GetExitCodeProcess(child, ctypes.byref(status)) and status.value == 259 and time.monotonic() < child_deadline:
                        time.sleep(.05)
                    assert kernel.GetExitCodeProcess(child, ctypes.byref(status)) and status.value != 259, "Cancelled worker left BCP running"
                finally:
                    kernel.CloseHandle(child)
        finally:
            guard.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            for pipe in (process.stdout, process.stderr):
                pipe.close()
            stage.unlink(missing_ok=True)

    commands = (Path(clients) / "fixture-commands.log").read_text(encoding="utf-8")
    assert connection["password"] not in commands and "fixture-master" not in commands
    assert "\t-P" not in commands, "Password was passed as a command argument"
    assert "isql.exe\t" in commands and "bcp.exe\t" in commands
    print("SAP frozen worker smoke: console authentication, schema/file preview, deferred OUT, append IN, failure/cancel preservation passed; fixture clients only.")


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
