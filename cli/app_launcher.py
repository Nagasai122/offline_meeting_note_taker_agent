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

import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

DASHBOARD_URL = "http://127.0.0.1:8000"
_STARTUP_TIMEOUT_S = 30.0


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _start_server(repo_root: Path) -> subprocess.Popen:
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW  # shortcut runs via pythonw
    return subprocess.Popen(
        [sys.executable, "-m", "cli.main", "web"],
        cwd=str(repo_root),
        creationflags=creationflags,
    )


def run_app(repo_root: Path | str | None = None, open_browser: bool = True) -> int:
    repo_root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[1]

    reused_existing = _port_open("127.0.0.1", 8000)
    server = None
    if reused_existing:
        print("Dashboard already running on :8000 — reusing it.")
    else:
        server = _start_server(repo_root)
        deadline = time.monotonic() + _STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            if server.poll() is not None:
                print("The dashboard process exited during startup; run "
                      "`meeting-agent web` in a terminal to see why.", file=sys.stderr)
                return 1
            if _port_open("127.0.0.1", 8000):
                break
            time.sleep(0.3)
        else:
            print("Dashboard did not come up within 30s.", file=sys.stderr)
            server.terminate()
            return 1

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
