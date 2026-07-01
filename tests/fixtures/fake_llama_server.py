"""
Minimal stand-in for llama-server's CLI surface and /health endpoint, used only in
tests. Lets us exercise server_manager.start_server against a REAL subprocess and
REAL socket, rather than mocking subprocess.Popen — closing the gap between "the
wiring is correct" and "a real process behaves as expected" as far as is possible
without the actual binary or a GPU.

Also stubs an OpenAI-compatible /v1/chat/completions endpoint, since both
supported backends (llama-server, vLLM) expose that surface and llm/client.py
is tested against it the same real-socket way.
"""

from __future__ import annotations

import argparse
import http.server
import json
import socketserver


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (stdlib method name)
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 (stdlib method name)
        if self.path == "/v1/chat/completions":
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)  # request body content is irrelevant to this stub
            body = json.dumps(
                {"choices": [{"message": {"role": "assistant", "content": "[]"}}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - silence logging
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args, _unknown = parser.parse_known_args()

    with socketserver.TCPServer((args.host, args.port), _Handler) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    main()
