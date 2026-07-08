"""
`meeting-agent app` — run the whole thing like a desktop application.

What it does:
1. starts the web dashboard (the same `cli.main web` everything else uses)
   as a child process on 127.0.0.1:8000;
2. waits for it to come up, then opens the default browser;
3. if `pystray` + `Pillow` are available, parks a tray icon (the petrol
   record-ring from assets/) with Open Dashboard / Quit — closing the
   browser tab does NOT kill the agent, the tray does;
4. without pystray, stays in the foreground until Ctrl+C.

Zero-egress unchanged: everything here is localhost orchestration —
`webbrowser.open` navigates the user's own browser to 127.0.0.1.

`scripts/install_app.ps1` creates Start Menu/Desktop shortcuts that run
this via pythonw.exe (no console window).
"""

from __future__ import annotations

import logging
import socket
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

DASHBOARD_URL = "http://127.0.0.1:8000"
_STARTUP_TIMEOUT_S = 30.0

_logger = logging.getLogger("meeting_agent.app_launcher")


def _configure_file_logging(repo_root: Path) -> Path:
    """Point this launcher's logging at a file under data/logs/.

    Necessary because the Start Menu/Desktop shortcut runs this module via
    pythonw.exe (no console window) -- under pythonw, sys.stdout/sys.stderr
    are None, so every print()/StreamHandler(sys.stderr) call this file used
    to make (and cli.main's own logging.basicConfig(), which also defaults to
    stderr) silently vanished with no error, no dialog, nothing. That made a
    genuine startup failure indistinguishable from "double-clicking the icon
    did nothing" -- there was no file anywhere a user could point at when
    reporting the problem. This is a launcher-only fix: `cli.main web` run
    from an actual terminal is unaffected and keeps logging to that terminal.
    """
    log_dir = repo_root / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "app_launcher.log"
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _logger.setLevel(logging.INFO)
    _logger.addHandler(handler)
    return log_path


def _show_error_dialog(title: str, message: str) -> None:
    """User-visible failure surface for a pythonw-launched process, where
    print()-to-stderr goes nowhere (see _configure_file_logging's docstring
    for why). ctypes.windll.user32.MessageBoxW is stdlib on Windows -- no new
    dependency, and unlike print(), it doesn't need a console to be visible.
    Best-effort: a failure to show the dialog must never crash the launcher's
    own error-handling path over a second, unrelated error."""
    if sys.platform != "win32":
        print(f"{title}: {message}", file=sys.stderr)
        return
    try:
        import ctypes
        MB_ICONERROR = 0x10
        MB_SETFOREGROUND = 0x10000
        ctypes.windll.user32.MessageBoxW(None, message, title, MB_ICONERROR | MB_SETFOREGROUND)
    except Exception:
        pass


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _serves_dashboard(host: str, port: int, timeout: float = 2.0) -> bool:
    """Confirm the process listening on `port` is actually this app's
    dashboard, not some unrelated service that happens to occupy the same
    port -- _port_open alone can't tell the two apart, which previously meant
    a coincidentally-occupied port 8000 made run_app silently "reuse" the
    wrong service and open the browser to it with no explanation.

    A raw-socket HTTP GET (not an httpx/requests client) to keep this in the
    same stdlib-only, zero-egress-friendly, loopback-only style _port_open
    already used -- this never leaves 127.0.0.1 and adds no new dependency."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(
                f"GET /api/briefing HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode("ascii")
            )
            sock.settimeout(timeout)
            response = b""
            while len(response) < 4096:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
            status_line = response.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
            return " 200 " in status_line
    except OSError:
        return False


def _start_server(repo_root: Path, log_dir: Path) -> subprocess.Popen:
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW  # shortcut runs via pythonw
    # The child's stdout/stderr are captured to a file rather than left
    # inherited -- under pythonw the parent's own stdout/stderr are None, so
    # inherited streams would vanish exactly like the launcher's own print()
    # calls did (see _configure_file_logging's docstring). Left open for the
    # lifetime of the child; not wrapped in `with`, since closing it here
    # would close the fd the subprocess is still writing to.
    dashboard_log = open(log_dir / "dashboard.log", "a", encoding="utf-8")
    dashboard_log.write(f"\n--- launched {datetime.now().isoformat()} ---\n")
    dashboard_log.flush()
    return subprocess.Popen(
        [sys.executable, "-m", "cli.main", "web"],
        cwd=str(repo_root),
        creationflags=creationflags,
        stdout=dashboard_log,
        stderr=subprocess.STDOUT,
    )


def run_app(repo_root: Path | str | None = None, open_browser: bool = True) -> int:
    repo_root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[1]
    log_dir = repo_root / "data" / "logs"
    log_path = _configure_file_logging(repo_root)
    _logger.info("run_app starting (repo_root=%s)", repo_root)

    port_listening = _port_open("127.0.0.1", 8000)
    reused_existing = port_listening and _serves_dashboard("127.0.0.1", 8000)

    if port_listening and not reused_existing:
        # Something other than this app's dashboard already holds port 8000
        # (the previous behaviour here just assumed "already running" and
        # silently opened the browser to whatever that was). Fail loudly
        # instead of guessing.
        message = (
            "Port 8000 is already in use by another program, so Meeting "
            "Agent's dashboard could not start. Close whatever is using "
            "that port, then try again."
        )
        _logger.error(message)
        _show_error_dialog("Meeting Agent — could not start", message)
        return 1

    server = None
    if reused_existing:
        _logger.info("Dashboard already running on :8000 -- reusing it.")
        print("Dashboard already running on :8000 — reusing it.")
    else:
        server = _start_server(repo_root, log_dir)
        deadline = time.monotonic() + _STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            if server.poll() is not None:
                message = (
                    f"The dashboard process exited during startup "
                    f"(exit code {server.returncode}). See "
                    f"{log_dir / 'dashboard.log'} for details."
                )
                _logger.error(message)
                _show_error_dialog("Meeting Agent — could not start", message)
                return 1
            if _port_open("127.0.0.1", 8000):
                break
            time.sleep(0.3)
        else:
            message = (
                f"Dashboard did not come up within {int(_STARTUP_TIMEOUT_S)}s. "
                f"See {log_dir / 'dashboard.log'} for details."
            )
            _logger.error(message)
            _show_error_dialog("Meeting Agent — could not start", message)
            server.terminate()
            return 1

    _logger.info("Dashboard is up; log file at %s", log_path)

    if open_browser:
        webbrowser.open(DASHBOARD_URL)

    def _shutdown() -> None:
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()

    try:
        return _run_tray_or_wait(server, _shutdown)
    finally:
        _shutdown()


def _run_tray_or_wait(server: subprocess.Popen | None, shutdown) -> int:
    try:
        import pystray
        from PIL import Image
    except ImportError:
        print(f"Meeting Agent running at {DASHBOARD_URL} — Ctrl+C to quit "
              "(install the 'app' extra for a tray icon).")
        try:
            while server is None or server.poll() is None:
                time.sleep(1.0)
            return server.returncode or 0
        except KeyboardInterrupt:
            return 0

    icon_path = Path(__file__).resolve().parents[1] / "assets" / "meeting-agent.png"
    image = Image.open(icon_path) if icon_path.exists() else Image.new("RGB", (64, 64), (20, 101, 90))

    def _open(icon, item) -> None:  # noqa: ANN001 - pystray callback signature
        webbrowser.open(DASHBOARD_URL)

    def _quit(icon, item) -> None:  # noqa: ANN001
        icon.stop()

    tray = pystray.Icon(
        "meeting-agent",
        image,
        "Meeting Agent — local & offline",
        menu=pystray.Menu(
            pystray.MenuItem("Open Dashboard", _open, default=True),
            pystray.MenuItem("Quit Meeting Agent", _quit),
        ),
    )
    tray.run()  # blocks until Quit
    return 0
