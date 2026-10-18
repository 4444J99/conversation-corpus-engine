from __future__ import annotations

import json
import mimetypes
import sqlite3
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .archive_ingest import ArchivePaths
from .archive_kb import open_kb, search, search_titles

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
MAX_LIMIT = 50
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
}
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
}


def _csv(values: list[str]) -> list[str]:
    return [item for value in values for item in value.split(",") if item]


class ArchiveServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], paths: ArchivePaths) -> None:
        if address[0] not in LOOPBACK_HOSTS:
            raise ValueError(
                f"The archive server only binds loopback addresses, not {address[0]!r}"
            )
        self.site_root = paths.site.resolve()
        self.paths = paths
        super().__init__(address, ArchiveHandler)

    def allowed_hosts(self) -> set[str]:
        port = self.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}


class ArchiveHandler(BaseHTTPRequestHandler):
    server: ArchiveServer
    server_version = "cce-archive"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "") in self.server.allowed_hosts()

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._json(HTTPStatus.FORBIDDEN, {"error": "host not allowed"})
            return
        parts = urlsplit(self.path)
        if parts.path.startswith("/api/"):
            self._api(parts.path, parse_qs(parts.query))
        else:
            self._static(parts.path)

    do_HEAD = do_GET  # noqa: N815

    def _method_not_allowed(self) -> None:
        self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "read-only server"})

    do_POST = do_PUT = do_DELETE = do_PATCH = _method_not_allowed  # noqa: N815

    def _static(self, raw_path: str) -> None:
        relative = unquote(raw_path).lstrip("/") or "index.html"
        target = (self.server.site_root / relative).resolve()
        try:
            target.relative_to(self.server.site_root)
        except ValueError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        kind = CONTENT_TYPES.get(target.suffix) or mimetypes.guess_type(target.name)[0]
        self._send(HTTPStatus.OK, target.read_bytes(), kind or "application/octet-stream")

    def _api(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/ping":
            self._json(HTTPStatus.OK, {"ok": True})
            return
        if path not in {"/api/search", "/api/titles"}:
            self._json(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})
            return
        text = (query.get("q") or [""])[0]
        try:
            limit = max(1, min(MAX_LIMIT, int((query.get("limit") or ["20"])[0])))
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "limit must be an integer"})
            return
        try:
            connection = open_kb(self.server.paths)
        except FileNotFoundError:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "knowledge base not built"})
            return
        try:
            if path == "/api/titles":
                results = search_titles(connection, text, limit=limit)
            else:
                results = search(
                    connection,
                    text,
                    providers=_csv(query.get("provider", [])) or None,
                    kinds=_csv(query.get("kind", [])) or None,
                    limit=limit,
                )
        except sqlite3.Error as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": f"query rejected: {exc}"})
            return
        finally:
            connection.close()
        self._json(HTTPStatus.OK, {"query": text, "results": results})


def start_in_thread(paths: ArchivePaths, port: int = 0) -> tuple[ArchiveServer, threading.Thread]:
    server = ArchiveServer(("127.0.0.1", port), paths)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def serve(paths: ArchivePaths, *, host: str = "127.0.0.1", port: int = 8765) -> None:
    if not (paths.site / "index.html").is_file():
        raise FileNotFoundError(f"No site at {paths.site}; run `cce archive site` first")
    server = ArchiveServer((host, port), paths)
    print(
        f"cce archive: http://127.0.0.1:{server.server_address[1]}/  (Ctrl-C to stop)", flush=True
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
