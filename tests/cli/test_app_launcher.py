from __future__ import annotations

import http.server
import socket
import threading

import pytest

from cli.app_launcher import _configure_file_logging, _serves_dashboard


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _FixedStatusHandler(http.server.BaseHTTPRequestHandler):
    """Minimal HTTP handler that always answers with a fixed status code,
    regardless of path -- enough to distinguish "this app's dashboard" (200)
    from "some other unrelated service" (anything else) without needing a
    real FastAPI app running in the test."""

    status_code = 200

    def do_GET(self):  # noqa: N802 - stdlib handler method name
        self.send_response(self.status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):  # silence per-request stderr logging
        pass


def _run_server_in_background(port: int, status_code: int) -> http.server.HTTPServer:
    handler = type("Handler", (_FixedStatusHandler,), {"status_code": status_code})
    server = http.server.HTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_serves_dashboard_true_when_endpoint_responds_200():
    port = _free_port()
    server = _run_server_in_background(port, 200)
    try:
        assert _serves_dashboard("127.0.0.1", port, timeout=2.0) is True
    finally:
        server.shutdown()


def test_serves_dashboard_false_when_nothing_listening():
    port = _free_port()  # deliberately never bound
    assert _serves_dashboard("127.0.0.1", port, timeout=0.5) is False


def test_serves_dashboard_false_when_response_is_not_200():
    """A port occupied by some other, unrelated local service (e.g. another
    dev server someone left running) must not be mistaken for this app's
    dashboard -- this is exactly the gap the app-icon bugfix closes: previously
    `_port_open` alone decided "already running" and silently reused whatever
    was on the port."""
    port = _free_port()
    server = _run_server_in_background(port, 404)
    try:
        assert _serves_dashboard("127.0.0.1", port, timeout=2.0) is False
    finally:
        server.shutdown()


def test_configure_file_logging_creates_log_file_and_directory(tmp_path):
    log_path = _configure_file_logging(tmp_path)
    assert log_path == tmp_path / "data" / "logs" / "app_launcher.log"
    assert log_path.parent.is_dir()

    import logging
    logger = logging.getLogger("meeting_agent.app_launcher")
    logger.info("test message written during test_configure_file_logging")
    for handler in logger.handlers:
        handler.flush()

    assert log_path.exists()
    assert "test message written during test_configure_file_logging" in log_path.read_text(encoding="utf-8")

    # Cleanup: this test attaches a real FileHandler to a module-level logger;
    # remove it so later tests in the same process don't keep writing to a
    # tmp_path-scoped file that no longer matters (and so the handle is closed
    # before tmp_path is torn down on Windows).
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
