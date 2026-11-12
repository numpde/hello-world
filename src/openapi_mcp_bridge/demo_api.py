"""Tiny independent HTTP API for the converter's real transport tests."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class DocumentAPI(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int]):
        self.documents = {"doc-1": {"id": "doc-1", "title": "Read me"}}
        self.seen_requests: list[tuple[str, str]] = []
        super().__init__(address, DocumentHandler)


class DocumentHandler(BaseHTTPRequestHandler):
    server: DocumentAPI

    def respond(self, status: int, payload: object) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("ETag", '"demo-1"')
        self.send_header("X-Request-ID", "demo-request")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        self.server.seen_requests.append(("GET", path))
        if path == "/documents":
            self.respond(200, {"documents": list(self.server.documents.values())})
        elif path.startswith("/documents/"):
            document = self.server.documents.get(path.removeprefix("/documents/"))
            if document is None:
                self.respond(404, {"error": "not found"})
            else:
                self.respond(200, document)
        elif path == "/admin/keys":
            self.respond(200, {"private": "never expose as an MCP tool"})
        else:
            self.respond(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        self.server.seen_requests.append(("POST", path))
        if path != "/documents":
            self.respond(404, {"error": "not found"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096:
                raise ValueError("invalid body size")
            body = json.loads(self.rfile.read(size))
            title = body["title"]
            if not isinstance(title, str) or not title.strip():
                raise ValueError("invalid title")
        except (ValueError, KeyError, TypeError):
            self.respond(400, {"error": "invalid document"})
            return

        document_id = f"doc-{len(self.server.documents) + 1}"
        document = {"id": document_id, "title": title}
        self.server.documents[document_id] = document
        self.respond(201, document)

    def log_message(self, _format: str, *args: object) -> None:
        # MCP stdio cleanliness is tested on the server, not the demo API.
        pass


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    with DocumentAPI(("127.0.0.1", args.port)) as api:
        print(f"Local API on http://127.0.0.1:{api.server_port}", flush=True)
        try:
            api.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
