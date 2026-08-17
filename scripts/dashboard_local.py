#!/usr/bin/env python3
"""Serve the dashboard Lambda handler over plain HTTP, for `npm run dev`.

Same module, same queries, same responses as the deployed Function URL — the
only difference is the transport. That is deliberate: developing the frontend
against a different implementation than the one it ships against is how contract
drift starts.

    make dashboard            # http://127.0.0.1:8787
    VITE_API_BASE_URL=http://127.0.0.1:8787 npm run dev
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from _env import bootstrap

bootstrap()

import sys  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents" / "dashboard"))

import app as dashboard  # noqa: E402

PORT = int(os.environ.get("DASHBOARD_PORT", "8787"))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        event = {
            "requestContext": {"http": {"method": method, "path": parsed.path}},
            "rawPath": parsed.path,
            "queryStringParameters": {
                k: ",".join(v) for k, v in parse_qs(parsed.query).items()
            },
            "body": body,
            "isBase64Encoded": False,
        }
        result = dashboard.handler(event)
        payload = (result.get("body") or "").encode()
        self.send_response(result["statusCode"])
        for key, value in (result.get("headers") or {}).items():
            self.send_header(key, value)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch("OPTIONS")

    def log_message(self, fmt: str, *args) -> None:
        print(f"  {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}", flush=True)


def main() -> int:
    print(json.dumps({"listening": f"http://127.0.0.1:{PORT}",
                      "generator_url": dashboard.GENERATOR_URL or None}), flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
