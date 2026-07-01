"""
Verifies the data-egress guarantee in docs/architecture.md by inspecting actual
sockets, rather than trusting configuration alone.

Usage:
    python scripts/network_audit.py --pid <root_pid> [--duration 30]

Walks the process tree rooted at the given PID (the meeting-agent CLI process and
any children it spawns — llama-server/vLLM, the MCP server) and repeatedly samples
each process' open connections via psutil. Fails (non-zero exit, printed offenders)
if any connection is observed:
    - listening on a non-loopback address, or
    - established to a remote address outside 127.0.0.1/::1.

This is intended to be run wrapped around a full record -> process -> review -> apply
cycle during development and CI, and is the artefact referenced by the setup guide's
"verify with airplane mode, then verify with this script" recommendation.
"""

from __future__ import annotations

import argparse
import ipaddress
import sys
import time

import psutil


def _is_loopback(address: str) -> bool:
    """True if `address` is a loopback address under any representation.

    Originally a string-prefix check against ("127.", "::1", "localhost"),
    which missed the IPv4-mapped IPv6 forms a dual-stack socket can report --
    e.g. "::ffff:127.0.0.1" or its compressed/expanded variants. psutil/the
    OS can hand back either family depending on platform and socket options,
    so this parses the address properly via `ipaddress` (which already knows
    about IPv4-mapped IPv6) instead of re-deriving the prefix list by hand.
    "localhost" is kept as an explicit string fallback since it is a hostname,
    not an IP literal, and would otherwise fail `ip_address()` parsing.
    """
    if address == "localhost":
        return True
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def _process_tree(root_pid: int) -> list[psutil.Process]:
    try:
        root = psutil.Process(root_pid)
    except psutil.NoSuchProcess:
        return []
    return [root, *root.children(recursive=True)]


def audit_once(root_pid: int) -> list[str]:
    offenders: list[str] = []
    for proc in _process_tree(root_pid):
        try:
            connections = proc.net_connections(kind="inet")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        for conn in connections:
            laddr_ip = conn.laddr.ip if conn.laddr else ""
            raddr_ip = conn.raddr.ip if conn.raddr else ""
            if laddr_ip and not _is_loopback(laddr_ip):
                offenders.append(
                    f"pid={proc.pid} ({proc.name()}) listening on non-loopback "
                    f"{laddr_ip}:{conn.laddr.port}"
                )
            if raddr_ip and not _is_loopback(raddr_ip):
                offenders.append(
                    f"pid={proc.pid} ({proc.name()}) connected to remote "
                    f"{raddr_ip}:{conn.raddr.port}"
                )
    return offenders


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True, help="Root PID to monitor")
    parser.add_argument(
        "--duration", type=float, default=30.0, help="Seconds to sample for"
    )
    parser.add_argument(
        "--interval", type=float, default=1.0, help="Sampling interval in seconds"
    )
    args = parser.parse_args()

    all_offenders: set[str] = set()
    deadline = time.monotonic() + args.duration
    while time.monotonic() < deadline:
        all_offenders.update(audit_once(args.pid))
        time.sleep(args.interval)

    if all_offenders:
        print("NETWORK AUDIT FAILED — non-loopback activity detected:", file=sys.stderr)
        for offender in sorted(all_offenders):
            print(f"  - {offender}", file=sys.stderr)
        return 1

    print("Network audit passed: no non-loopback sockets observed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
