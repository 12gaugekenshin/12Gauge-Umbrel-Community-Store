from __future__ import annotations

import asyncio
import ipaddress
import math
import socket
import time
import urllib.parse
from datetime import datetime, timezone


ATTEMPTS = 3
COOLDOWN_SECONDS = 30
CONNECT_TIMEOUT_SECONDS = 3


class PoolProbeCooldown(Exception):
    def __init__(self, remaining_seconds: int):
        super().__init__(f"Wait {remaining_seconds} seconds before testing another pool")
        self.remaining_seconds = remaining_seconds


class PoolConnectionProbe:
    """Keeps only the latest external pool connectivity result in memory."""

    def __init__(self):
        self.latest = None
        self.last_started = 0.0
        self.lock = asyncio.Lock()

    def cooldown_remaining(self):
        remaining = COOLDOWN_SECONDS - (time.monotonic() - self.last_started)
        return max(0, math.ceil(remaining)) if self.last_started else 0

    def snapshot(self):
        return {
            "result": dict(self.latest) if self.latest else None,
            "cooldown_remaining": self.cooldown_remaining(),
            "cooldown_seconds": COOLDOWN_SECONDS,
        }

    @staticmethod
    def _parse_target(value):
        raw = str(value or "").strip()
        if not raw:
            raise ValueError("Enter a pool endpoint such as stratum+tcp://pool.example:3333")
        if "://" not in raw:
            raw = f"stratum+tcp://{raw}"
        parsed = urllib.parse.urlparse(raw)
        if parsed.scheme.lower() not in {"stratum+tcp", "tcp"}:
            raise ValueError("Use a stratum+tcp:// or tcp:// pool endpoint")
        if not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Enter a pool hostname and port without credentials")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("Pool endpoints cannot include paths, queries, or fragments")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Enter a valid pool port") from exc
        if not port or not 1 <= port <= 65535:
            raise ValueError("Enter a pool port between 1 and 65535")
        host = parsed.hostname.strip().lower()
        if len(host) > 253:
            raise ValueError("Pool hostname is too long")
        display_host = f"[{host}]" if ":" in host else host
        return host, port, f"stratum+tcp://{display_host}:{port}"

    @staticmethod
    def _public_addresses(records):
        addresses = []
        for family, _, _, _, sockaddr in records:
            if family not in {socket.AF_INET, socket.AF_INET6}:
                continue
            address = str(sockaddr[0])
            ip = ipaddress.ip_address(address)
            if any((ip.is_private, ip.is_loopback, ip.is_link_local, ip.is_multicast, ip.is_reserved, ip.is_unspecified)):
                continue
            item = (family, address)
            if item not in addresses:
                addresses.append(item)
        # Most mining appliances use IPv4; keep IPv6 as a fallback.
        return sorted(addresses, key=lambda item: 0 if item[0] == socket.AF_INET else 1)

    async def test(self, value):
        async with self.lock:
            remaining = self.cooldown_remaining()
            if remaining:
                raise PoolProbeCooldown(remaining)
            host, port, target = self._parse_target(value)
            self.last_started = time.monotonic()

            dns_started = time.perf_counter()
            try:
                records = await asyncio.wait_for(
                    asyncio.to_thread(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM),
                    timeout=CONNECT_TIMEOUT_SECONDS,
                )
            except (asyncio.TimeoutError, OSError, socket.gaierror) as exc:
                result = {
                    "target": target,
                    "host": host,
                    "port": port,
                    "tested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "reachable": False,
                    "dns_ms": round((time.perf_counter() - dns_started) * 1000, 1),
                    "attempts": ATTEMPTS,
                    "successes": 0,
                    "failures": ATTEMPTS,
                    "error": f"DNS lookup failed: {exc}"[:240],
                }
                self.latest = result
                return self.snapshot()

            dns_ms = round((time.perf_counter() - dns_started) * 1000, 1)
            addresses = self._public_addresses(records)
            if not addresses:
                raise ValueError("External pool tests require a publicly routed hostname or IP address")

            family, address = addresses[0]
            timings = []
            errors = []
            for attempt in range(ATTEMPTS):
                started = time.perf_counter()
                writer = None
                try:
                    _, writer = await asyncio.wait_for(
                        asyncio.open_connection(address, port, family=family),
                        timeout=CONNECT_TIMEOUT_SECONDS,
                    )
                    timings.append(round((time.perf_counter() - started) * 1000, 1))
                except (asyncio.TimeoutError, OSError) as exc:
                    errors.append(str(exc)[:120] or exc.__class__.__name__)
                finally:
                    if writer:
                        writer.close()
                        try:
                            await writer.wait_closed()
                        except OSError:
                            pass
                if attempt < ATTEMPTS - 1:
                    await asyncio.sleep(0.15)

            successes = len(timings)
            result = {
                "target": target,
                "host": host,
                "port": port,
                "resolved_ip": address,
                "tested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "reachable": successes > 0,
                "dns_ms": dns_ms,
                "attempts": ATTEMPTS,
                "successes": successes,
                "failures": ATTEMPTS - successes,
                "min_ms": min(timings) if timings else None,
                "average_ms": round(sum(timings) / successes, 1) if timings else None,
                "max_ms": max(timings) if timings else None,
                "error": errors[-1] if errors and not timings else None,
            }
            self.latest = result
            return self.snapshot()
