from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path


RULES = [
    (re.compile(r"\b(candidate|submitblock|block found)\b", re.I), "block", "🎯 Possible Block Event", "critical", True),
    (re.compile(r"\b(rpc.*(?:error|failed|timeout)|node.*error)\b", re.I), "rpc", "Pool RPC / Node Error", "critical", True),
    (re.compile(r"\b(unauthori[sz]ed|forbidden|auth(?:entication)?\s+(?:failed|failure|error)|invalid\s+(?:user|password|login)|banned|blacklist|malformed|flood|ddos|attack|suspicious)\b", re.I), "security", "Local Pool Security Warning", "critical", True),
    (re.compile(r"\b(disconnected|reconnect|timeout|failed)\b", re.I), "connection", "Pool Connection Event", "warning", True),
    (re.compile(r"\b(rejected|stale|duplicate)\b", re.I), "bad-share", "Rejected / Stale Share", "warning", True),
    (re.compile(r"\b(error|warning)\b", re.I), "warning", "Pool Warning", "warning", True),
    (re.compile(r"\b(accepted|subscribed|authorized|stratum|client|worker)\b", re.I), "activity", "Pool Activity", "info", False),
]

MAX_POOL_EVENTS = 50
MAX_LOG_LINE_BYTES = 64 * 1024
MAX_LINES_PER_SCAN = 5000
MAX_PUBLIC_POOL_RESPONSE_BYTES = 1024 * 1024
MAX_PUBLIC_POOL_WORKERS = 100


def _api_json(url: str, timeout: float = 4.0):
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "PoCiSys-Hash-Monitor/1.3"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(MAX_PUBLIC_POOL_RESPONSE_BYTES + 1)
    if len(body) > MAX_PUBLIC_POOL_RESPONSE_BYTES:
        raise ValueError("Public Pool response exceeded the 1 MB safety limit")
    return json.loads(body.decode("utf-8", errors="replace"))


def probe_public_pool(api_url: str, timeout: float = 4.0):
    base = api_url.strip().rstrip("/")
    payload = _api_json(f"{base}/api/pool", timeout=timeout)
    required = {"totalHashRate", "blockHeight", "totalMiners"}
    if not isinstance(payload, dict) or not required.intersection(payload):
        raise ValueError("This does not look like a Public Pool API")
    return base, payload


class PoolLogService:
    def __init__(self, pools: list[dict], alert_engine, status_provider=None):
        self.pools = pools
        self.alert_engine = alert_engine
        self.status_provider = status_provider or (lambda: [])
        self.events = deque(maxlen=MAX_POOL_EVENTS)
        self.positions = {}
        self.latest = {}
        self.seen_blocks = {}
        self.running = False
        self.task = None

    def reconfigure(self, pools: list[dict]):
        active_paths = {
            str(Path(pool.get("log_path", "")).resolve())
            for pool in pools
            if pool.get("log_path")
        }
        self.positions = {path: offset for path, offset in self.positions.items() if path in active_paths}
        active_ids = {pool.get("id", pool.get("name")) for pool in pools}
        self.latest = {key: value for key, value in self.latest.items() if key in active_ids}
        self.seen_blocks = {key: value for key, value in self.seen_blocks.items() if key in active_ids}
        self.pools = pools

    def _pool_status(self, pool):
        key = pool.get("id", pool.get("name"))
        if pool.get("mode") == "public_pool_api":
            return {
                "name": pool.get("name", "Public Pool"),
                "mode": "public_pool_api",
                "enabled": pool.get("enabled", False),
                "api_url": pool.get("api_url"),
                **self.latest.get(key, {"available": False, "message": "Waiting for Public Pool"}),
            }
        path = Path(pool.get("log_path", ""))
        return {
            "name": pool.get("name", "Unnamed pool"),
            "mode": pool.get("mode", "local_log"),
            "enabled": pool.get("enabled", False),
            "log_path": str(path),
            "available": path.is_file(),
            "size_bytes": path.stat().st_size if path.is_file() else None,
        }

    def status(self):
        return [self._pool_status(pool) for pool in self.pools]

    def _miner_address(self, pool):
        configured = str(pool.get("bitcoin_address") or "").strip()
        if configured:
            return configured.split(".", 1)[0]
        for miner in self.status_provider():
            user = str(miner.get("pool", {}).get("user") or "").strip()
            if user:
                return user.split(".", 1)[0]
        return ""

    async def _poll_public_pool(self, pool):
        key = pool.get("id", pool.get("name"))
        base = str(pool.get("api_url") or "").rstrip("/")
        if not base:
            self.latest[key] = {"available": False, "message": "API URL is missing"}
            return
        try:
            pool_data = await asyncio.to_thread(_api_json, f"{base}/api/pool")
            if not isinstance(pool_data, dict):
                raise ValueError("Pool API returned an invalid response")
            raw_blocks = pool_data.get("blocksFound", [])
            blocks = raw_blocks if isinstance(raw_blocks, list) else []
            unique_blocks = []
            unique_ids = set()
            for block in reversed(blocks):
                if not isinstance(block, dict):
                    continue
                identifier = f"{block.get('height')}:{block.get('sessionId')}:{block.get('worker')}"
                if identifier in unique_ids:
                    continue
                unique_ids.add(identifier)
                unique_blocks.append({
                    "height": block.get("height"),
                    "worker": block.get("worker"),
                    "session_id": block.get("sessionId"),
                })
                if len(unique_blocks) >= 10:
                    break

            previous = self.seen_blocks.get(key)
            current_real = {
                f"{item.get('height')}:{item.get('session_id')}:{item.get('worker')}"
                for item in unique_blocks
                if int(item.get("height") or 0) > 1
            }
            if previous is not None:
                for identifier in current_real - set(previous):
                    block = next(
                        item for item in unique_blocks
                        if f"{item.get('height')}:{item.get('session_id')}:{item.get('worker')}" == identifier
                    )
                    event = {
                        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "pool": pool.get("name", "Public Pool"),
                        "category": "block",
                        "title": "🎯 Public Pool Block Event",
                        "severity": "critical",
                        "important": True,
                        "line": f"Block height {block.get('height')} reported by {block.get('worker') or 'a worker'}",
                    }
                    self.events.appendleft(event)
                    await self.alert_engine.pool_event(event)
            self.seen_blocks[key] = deque(current_real, maxlen=25)

            address = self._miner_address(pool)
            client = {}
            if address:
                encoded = urllib.parse.quote(address, safe="")
                try:
                    client = await asyncio.to_thread(_api_json, f"{base}/api/client/{encoded}")
                except (OSError, ValueError, json.JSONDecodeError):
                    client = {}
            workers = client.get("workers", []) if isinstance(client, dict) else []
            workers = workers if isinstance(workers, list) else []
            self.latest[key] = {
                "available": True,
                "message": "Public Pool API connected",
                "total_hashrate_ths": float(pool_data.get("totalHashRate") or 0) / 1e12,
                "block_height": pool_data.get("blockHeight"),
                "total_miners": pool_data.get("totalMiners"),
                "blocks_found": len(blocks),
                "best_difficulty": client.get("bestDifficulty") if isinstance(client, dict) else None,
                "workers_count": client.get("workersCount") if isinstance(client, dict) else None,
                "workers": [
                    {
                        "name": item.get("name") or "worker",
                        "hashrate_ths": float(item.get("hashRate") or 0) / 1e12,
                        "best_difficulty": item.get("bestDifficulty"),
                        "last_seen": item.get("lastSeen"),
                    }
                    for item in workers[:MAX_PUBLIC_POOL_WORKERS]
                    if isinstance(item, dict)
                ],
                "address_detected": bool(address),
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self.latest[key] = {
                "available": False,
                "message": str(exc)[:240],
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }

    async def _scan(self, pool):
        path = Path(pool.get("log_path", ""))
        key = str(path.resolve()) if path else ""
        if not path.is_file():
            return
        size = path.stat().st_size
        if key not in self.positions:
            self.positions[key] = size
            return
        if size < self.positions[key]:
            self.positions[key] = 0
        if size == self.positions[key]:
            return

        processed = 0
        with path.open("rb") as handle:
            handle.seek(self.positions[key])
            while handle.tell() < size and processed < MAX_LINES_PER_SCAN:
                line_bytes = handle.readline(MAX_LOG_LINE_BYTES)
                if not line_bytes:
                    break
                processed += 1
                clean = line_bytes.decode("utf-8", errors="replace").strip()
                if not clean:
                    continue
                for pattern, category, title, severity, important in RULES:
                    if pattern.search(clean):
                        event = {
                            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "pool": pool.get("name", "Pool"),
                            "category": category,
                            "title": title,
                            "severity": severity,
                            "important": important,
                            "line": clean[:1000],
                        }
                        self.events.appendleft(event)
                        await self.alert_engine.pool_event(event)
                        break
            self.positions[key] = handle.tell()

    async def run(self):
        self.running = True
        while self.running:
            for pool in self.pools:
                if not pool.get("enabled"):
                    continue
                mode = pool.get("mode", "local_log")
                if mode == "local_log":
                    try:
                        await self._scan(pool)
                    except (OSError, ValueError):
                        pass
                elif mode == "public_pool_api":
                    await self._poll_public_pool(pool)
            await asyncio.sleep(5)

    def start(self):
        if not self.task:
            self.task = asyncio.create_task(self.run())

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
