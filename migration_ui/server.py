"""Run: python -m migration_ui.server (local-only; no production deployment)."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import threading

from .core import MAX_SOURCE, MigrationError, canonical, strict_json
from .engine import MigrationEngine
from .storage import PrivateLedger

STATIC = Path(__file__).parent / "static"
MAX_POST = MAX_SOURCE * 4 // 3 + 65536


def make_server(engine, port=0):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *_):
            pass  # URLs, config, source content, and keys never go to logs.

        def respond(self, status, body, content_type="application/json; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(body)

        def failure(self, code, status=400):
            self.respond(status, canonical({"error": code}).encode())

        def gate(self, *, api=False):
            authority = f"127.0.0.1:{self.server.server_address[1]}"
            origin = "http://" + authority
            if self.headers.get_all("Host", []) != [authority]:
                return False
            if self.headers.get("Sec-Fetch-Site") not in {None, "none", "same-origin"}:
                return False
            if self.headers.get("Origin") not in {None, origin}:
                return False
            if api and self.headers.get("X-Migration-CSRF") != token:
                return False
            return True

        def do_GET(self):
            if not self.gate(api=self.path.startswith("/api/")):
                return self.failure("local_request_rejected", 403)
            if self.path == "/api/status":
                return self.respond(200, canonical(engine.snapshot()).encode("utf-8"))
            paths = {"/": ("index.html", "text/html; charset=utf-8"),
                     "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                     "/style.css": ("style.css", "text/css; charset=utf-8")}
            if self.path not in paths:
                return self.failure("not_found", 404)
            name, kind = paths[self.path]
            data = (STATIC / name).read_bytes()
            if name == "index.html":
                data = data.replace(b"__CSRF_TOKEN__", token.encode("ascii"))
            self.respond(200, data, kind)

        def do_POST(self):
            if not self.gate(api=True):
                return self.failure("local_request_rejected", 403)
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json" or self.headers.get("Transfer-Encoding"):
                return self.failure("json_body_required")
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not lengths[0].isdigit() or not 0 < int(lengths[0]) <= MAX_POST:
                return self.failure("request_size_limit")
            try:
                raw = self.rfile.read(int(lengths[0]))
                if len(raw) != int(lengths[0]):
                    raise MigrationError("incomplete_request")
                data = strict_json(raw.decode("utf-8"))
                if not isinstance(data, dict):
                    raise MigrationError("object_required")
                if self.path == "/api/source":
                    value = engine.prepare(data)
                elif self.path == "/api/start":
                    value = engine.start(data)
                elif self.path == "/api/cancel":
                    engine.cancel()
                    value = {"cancel_requested": True}
                else:
                    return self.failure("not_found", 404)
                self.respond(200, canonical(value).encode("utf-8"))
            except MigrationError as exc:
                self.failure(str(exc))
            except (ValueError, UnicodeError, TypeError):
                self.failure("invalid_request")
            except Exception:
                self.failure("local_processing_error", 500)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description="ST ordinary-memory migration: local-only, explicit consent, no self writes")
    default = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share"))) / "StillerBrain" / "migration-ui"
    parser.add_argument("--data-dir", type=Path, default=default, help="Dedicated private migration-ui directory (never a workspace or existing data root)")
    parser.add_argument("--port", type=int, default=0, help="Loopback port; 0 chooses a free port")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("invalid port")
    ledger = PrivateLedger(args.data_dir)
    engine = MigrationEngine(ledger)
    server = make_server(engine, args.port)
    print(f"ST local migration: http://127.0.0.1:{server.server_address[1]}/")
    print("Content stays local until consent. Ctrl+C stops between items; in-flight writes are not replayed.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        engine.cancel()
    finally:
        server.server_close()
        if engine.worker:
            engine.worker.join()
        ledger.close()


if __name__ == "__main__":
    main()
