"""Bound CI tests and publish a useful diagnostic when protected logs hang.

The development test process owns its descendants. This script is never part of
the end-user executable and changes no application or file-transfer timeouts.
"""

import argparse
from collections import deque
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading


def annotation(message):
    encoded = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print("::error::" + encoded, flush=True)


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
            if guard is not None:
                guard.terminate()
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
            exit_code = 1
        reader.join(timeout=5)
        if timed_out:
            annotation(f"Source tests exceeded {timeout:g} seconds. Last captured output:\n" + "".join(tail))
        elif exit_code:
            annotation("Source tests failed. Last captured output:\n" + "".join(tail))
        return exit_code
    finally:
        if guard is not None:
            guard.close()
        elif process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
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
