"""사용자 PC에서 실행하는 단일 파일 앱의 진입점."""

import argparse
import signal
import socket
import threading
import time
import webbrowser

import uvicorn


def open_when_ready(server, url: str, opener=webbrowser.open):
    deadline = time.monotonic() + 30
    while not server.started and not server.should_exit and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started or server.should_exit:
        return
    print(f"nice-tools ready: {url}", flush=True)
    print("Keep this window open. Press Ctrl+C to quit.", flush=True)
    if opener is not None:
        try:
            if opener(url) is False:
                print("Open the address above in Chrome or Edge.", flush=True)
        except Exception:
            print("Could not open the browser. Open the address above manually.", flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="nice-tools local desktop launcher")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser (for automated verification)")
    parser.add_argument("--port", type=int, default=0, help="Local port; default automatically selects an available port")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", args.port))
            listener.listen(128)
        except OSError as exc:
            print(f"Could not start nice-tools: {exc}", flush=True)
            return 1
        from nice_tools.main import app

        port = listener.getsockname()[1]
        config = uvicorn.Config(
            app, host="127.0.0.1", port=port, loop="asyncio", http="h11",
            ws="none", lifespan="on", log_level="warning", access_log=False,
            proxy_headers=False, timeout_graceful_shutdown=3,
        )
        server = uvicorn.Server(config)
        browser = threading.Thread(
            target=open_when_ready,
            args=(server, f"http://127.0.0.1:{port}", None if args.no_browser else webbrowser.open),
            daemon=True,
        )
        # Windows CI sends CTRL_BREAK to the process group. Normal users use Ctrl+C.
        previous_break = None
        if hasattr(signal, "SIGBREAK"):
            previous_break = signal.getsignal(signal.SIGBREAK)
            signal.signal(signal.SIGBREAK, lambda _signal, _frame: setattr(server, "should_exit", True))
        browser.start()
        try:
            server.run(sockets=[listener])
        except KeyboardInterrupt:
            pass
        finally:
            server.should_exit = True
            if previous_break is not None:
                signal.signal(signal.SIGBREAK, previous_break)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
