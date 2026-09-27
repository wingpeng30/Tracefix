"""Serve the local Requests test API on HTTP/80 and verified HTTPS/443."""

from __future__ import annotations

import argparse
import signal
import threading
import time
from pathlib import Path

from httpbin import app
from werkzeug.serving import make_server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--certificate", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    args = parser.parse_args()

    servers = (
        make_server("127.0.0.1", 80, app, threaded=True),
        make_server(
            "127.0.0.1",
            443,
            app,
            threaded=True,
            ssl_context=(str(args.certificate), str(args.private_key)),
        ),
    )
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def stop(*_args: object) -> None:
        for server in servers:
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while any(thread.is_alive() for thread in threading.enumerate() if thread is not threading.main_thread()):
        time.sleep(0.1)


if __name__ == "__main__":
    main()
