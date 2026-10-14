"""Bound CI tests and publish a useful diagnostic when protected logs hang.

The development test process owns its descendants. This script is never part of
the end-user executable and changes no application or file-transfer timeouts.
"""

import argparse
from collections import deque
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading


def annotation(message):
    # GitHub truncates a single public annotation around 4 KiB. Keep each part
    # below that limit so a traceback cannot hide the final failed-test summary.
    parts = []
    current = []
    size = 0
    for character in message:
        escaped = {"%": "%25", "\r": "%0D", "\n": "%0A"}.get(character, character)
        width = len(escaped.encode("utf-8"))
        if size + width > 2800:
            parts.append("".join(current))
            current = []
            size = 0
        current.append(escaped)
        size += width
    if current:
        parts.append("".join(current))
    for index, part in enumerate(parts, start=1):
        prefix = f"Diagnostic {index}/{len(parts)}: " if len(parts) > 1 else ""
        print("::error::" + prefix + part, flush=True)


def terminate(process, guard):
    if guard is not None:
        guard.terminate()
    elif os.name == "nt":
        # Job creation may fail before the suspended child has an owner. Kill
        # that exact process and preserve the original setup failure.
        process.kill()
    else:
        os.killpg(process.pid, signal.SIGKILL)


def run(timeout, pytest_args):
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("The test timeout must be positive and finite.")
    root = Path(__file__).resolve().parents[1]
    command = [sys.executable, "-m", "pytest", "-q", "-o", "faulthandler_timeout=90", *pytest_args]
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "utf-8"
    process = subprocess.Popen(
        command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        env=environment,
        creationflags=(subprocess.CREATE_NO_WINDOW | 0x00000004) if os.name == "nt" else 0,
        start_new_session=os.name != "nt",
    )
    guard = None
    tail = deque(maxlen=100)
    reader = None
    try:
        if os.name == "nt":
            sys.path.insert(0, str(root))
            from nice_tools.ase_process import WindowsJob, resume_process
            guard = WindowsJob(process.pid)
            resume_process(process.pid)

        def capture():
            with (root / "source-tests.log").open("w", encoding="utf-8") as log:
                for line in process.stdout:
                    tail.append(line)
                    log.write(line)
                    log.flush()
                    sys.stdout.write(line)
                    sys.stdout.flush()

        reader = threading.Thread(target=capture, daemon=True, name="source-test-log")
        reader.start()
        timed_out = False
        try:
            exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate(process, guard)
            process.wait(timeout=10)
            exit_code = 1
        reader.join(timeout=5)
        if exit_code:
            summary = [line for line in list(tail) if line.startswith(("FAILED ", "ERROR "))
                       or re.search(r"\b\d+ (?:failed|passed|skipped)\b.*\bin [0-9.]+s\b", line)]
            if summary:
                annotation("Source test summary:\n" + "".join(summary))
        if timed_out:
            annotation(f"Source tests exceeded {timeout:g} seconds. Last captured output:\n" + "".join(tail))
        elif exit_code:
            annotation("Source tests failed. Last captured output:\n" + "".join(tail))
        return exit_code
    finally:
        if guard is not None:
            guard.close()
        elif process.poll() is None:
            terminate(process, guard)
        process.wait(timeout=10)
        if reader is not None:
            reader.join(timeout=5)
        process.stdout.close()


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    pytest_args = args.pytest_args[1:] if args.pytest_args[:1] == ["--"] else args.pytest_args
    return run(args.timeout, pytest_args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        annotation(f"Source test runner failed: {type(error).__name__}: {error}")
        raise
