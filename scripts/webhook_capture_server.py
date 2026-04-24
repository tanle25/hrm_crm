from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


OUTPUT_PATH = Path("tmp/webhook_events.ndjson")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with OUTPUT_PATH.open("a", encoding="utf-8") as fh:
            fh.write(raw.decode("utf-8"))
            fh.write("\n")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return None


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print(json.dumps({"listening": "http://127.0.0.1:8765", "output": str(OUTPUT_PATH)}, ensure_ascii=False))
    server.serve_forever()


if __name__ == "__main__":
    main()
