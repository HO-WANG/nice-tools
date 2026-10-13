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


def verify(path: Path, allow_non_windows: bool = False):
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
                    smoke_main(["--url", match.group(), "--large-files"])
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
    args = parser.parse_args()
    verify(args.executable.resolve(), args.allow_non_windows)


if __name__ == "__main__":
    main()
