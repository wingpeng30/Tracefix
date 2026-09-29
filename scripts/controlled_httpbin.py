"""Explicit local httpbin startup/reuse; never stops or replaces an existing listener."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import ProxyHandler, build_opener
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--identity-file", type=Path, required=True)
    parser.add_argument("--reuse", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    opener = build_opener(ProxyHandler({}))
    if args.reuse:
        identity = json.loads(args.identity_file.read_text(encoding="utf-8"))
        if identity["port"] != args.port:
            raise RuntimeError("requested port differs from saved identity")
        with opener.open(identity["identity_url"], timeout=5) as response:
            if json.load(response) != identity:
                raise RuntimeError("running service identity differs from saved startup")
        with opener.open(identity["health_url"], timeout=5) as response:
            if json.load(response).get("url") != identity["health_url"]:
                raise RuntimeError("service health mismatch")
        print(json.dumps({"status": "verified_reuse", "identity": identity}))
        return
    if args.identity_file.exists():
        raise RuntimeError("identity file already exists; use --reuse or a new file")
    if importlib.metadata.version("httpbin") != "0.10.2":
        raise RuntimeError("controlled service requires httpbin==0.10.2")
    from httpbin import app
    from werkzeug.serving import make_server

    token = uuid4().hex
    base = f"http://127.0.0.1:{args.port}"
    identity = {
        "schema_version": 1,
        "pid": os.getpid(),
        "python": str(Path(sys.executable).resolve()),
        "python_version": platform.python_version(),
        "started_at": datetime.now(timezone.utc).isoformat(),  # noqa: UP017 (Python 3.9 service)
        "startup_nonce": token,
        "port": args.port,
        "identity_url": f"{base}/__tracefix_identity/{token}",
        "health_url": f"{base}/get",
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "dependency_versions": {
            d.metadata["Name"]: d.version for d in importlib.metadata.distributions()
        },
    }
    app.add_url_rule(f"/__tracefix_identity/{token}", "tracefix_identity", lambda: identity)
    # Bind before publishing identity. An occupied port fails without touching its owner.
    server = make_server("127.0.0.1", args.port, app, threaded=True)
    try:
        args.identity_file.parent.mkdir(parents=True, exist_ok=True)
        with args.identity_file.open("x", encoding="utf-8") as stream:
            json.dump(identity, stream, indent=2)
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
