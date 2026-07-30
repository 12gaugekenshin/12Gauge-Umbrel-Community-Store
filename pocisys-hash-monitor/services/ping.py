from __future__ import annotations

import socket
import time


def tcp_ping(host: str, ports=(80, 443, 4028), timeout=1.5):
    """Cross-platform reachability check using miner API ports."""
    for port in ports:
        started = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True, round((time.perf_counter() - started) * 1000, 1), port
        except OSError:
            continue
    return False, None, None
