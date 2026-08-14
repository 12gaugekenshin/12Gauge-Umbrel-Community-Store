from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path


RULES = [
    (re.compile(r"\b(candidate|submitblock|block found)\b", re.I), "block", "Possible Block Event", "critical", True),
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
MAX_ACCEPTED_SHARES_PER_POOL = 10
MAX_SHARE_HISTORY_FILE_BYTES = 256 * 1024


def _api_json(url: str, timeout: float = 4.0):
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "PoCiSys-Hash-Monitor/1.8"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(MAX_PUBLIC_POOL_RESPONSE_BYTES + 1)
    if len(body) > MAX_PUBLIC_POOL_RESPONSE_BYTES:
        raise ValueError("Public Pool response exceeded the 1 MB safety limit")
    return json.loads(body.decode("utf-8", errors="replace"))


def _pool_snapshot(base: str, timeout: float = 4.0):
    errors = []
    try:
        payload = _api_json(f"{base}/api/pool", timeout=timeout)
        required = {"totalHashRate", "blockHeight", "totalMiners"}
        if isinstance(payload, dict) and required.intersection(payload):
            return "public_pool", payload, None
        errors.append("/api/pool returned an unrecognized object")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        errors.append(str(exc))

    try:
        status = _api_json(f"{base}/api/status", timeout=timeout)
        if isinstance(status, dict) and isinstance(status.get("connection"), dict) and "workers" in status:
            candidates = status.get("candidates") if isinstance(status.get("candidates"), list) else []
            normalized = {
                "totalHashRate": status.get("totalHashRate"),
                "blockHeight": status.get("blockHeight"),
                "totalMiners": status.get("totalMiners"),
                "blocksFound": candidates,
                "acceptedShares": status.get("acceptedShares") if isinstance(status.get("acceptedShares"), list) else [],
            }
            return "pocisys_pool_port", normalized, status
        errors.append("/api/status returned an unrecognized object")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        errors.append(str(exc))
    raise ValueError("This does not look like Public Pool or PoCiSys Public Pool Port: " + "; ".join(errors[-2:]))


def probe_public_pool(api_url: str, timeout: float = 4.0):
    base = api_url.strip().rstrip("/")
    adapter, payload, _ = _pool_snapshot(base, timeout)
    payload = dict(payload)
    payload["_adapter"] = adapter
    return base, payload


def _difficulty(value):
    try:
        parsed = float(str(value).replace(",", ""))
        return parsed if parsed >= 0 else None
    except (TypeError, ValueError):
        return None


def _share_time(value):
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp, timezone.utc)
    text = str(value or "").strip()
    if text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _normalize_share(item, pool_key):
    if not isinstance(item, dict):
        return None
    difficulty = _difficulty(item.get("difficulty") or item.get("shareDifficulty") or item.get("diff"))
    if difficulty is None:
        return None
    when = _share_time(item.get("received_at") or item.get("receivedAt") or item.get("time") or item.get("timestamp"))
    worker = str(item.get("worker") or item.get("clientName") or item.get("miner") or "worker")[:128]
    fingerprint = str(item.get("header_hash") or item.get("headerHash") or item.get("id") or "")[:128]
    if not fingerprint:
        fingerprint = f"{when.isoformat(timespec='milliseconds')}:{worker}:{difficulty:.12g}"
    return {
        "id": fingerprint if fingerprint.startswith(f"{pool_key}:") else f"{pool_key}:{fingerprint}",
        "time": when.astimezone(timezone.utc).isoformat(timespec="milliseconds"),
        "timestamp_ms": int(when.timestamp() * 1000),
        "worker": worker,
        "difficulty": difficulty,
    }


class PoolLogService:
    def __init__(self, pools: list[dict], alert_engine, status_provider=None, history_path=None, network_provider=None):
        self.pools = pools
        self.alert_engine = alert_engine
        self.status_provider = status_provider or (lambda: [])
        self.network_provider = network_provider or (lambda: {})
        self.history_path = Path(history_path).resolve() if history_path else None
        self.events = deque(maxlen=MAX_POOL_EVENTS)
        self.positions = {}
        self.latest = {}
        self.seen_blocks = {}
        self.share_history = {}
        self.all_time_best = {}
        self.session_best = {}
        self.running = False
        self.task = None
        self._load_share_history()

    @staticmethod
    def _key(pool):
        return str(pool.get("id") or pool.get("name") or "pool")

    def _load_share_history(self):
        if not self.history_path or not self.history_path.is_file():
            return
        try:
            if self.history_path.stat().st_size > MAX_SHARE_HISTORY_FILE_BYTES:
                return
            payload = json.loads(self.history_path.read_text(encoding="utf-8"))
            pools = payload.get("pools", {}) if isinstance(payload, dict) else {}
            for key, saved in list(pools.items())[:100]:
                if not isinstance(saved, dict):
                    continue
                saved_shares = saved.get("shares", []) if isinstance(saved.get("shares"), list) else []
                normalized = [
                    item for item in (_normalize_share(value, key) for value in saved_shares[:MAX_ACCEPTED_SHARES_PER_POOL])
                    if item
                ]
                self.share_history[key] = deque(normalized, maxlen=MAX_ACCEPTED_SHARES_PER_POOL)
                best = _difficulty(saved.get("all_time_best"))
                if best is not None:
                    self.all_time_best[key] = best
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return

    def _save_share_history(self):
        if not self.history_path:
            return
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "pools": {
                key: {
                    "shares": list(shares)[:MAX_ACCEPTED_SHARES_PER_POOL],
                    "all_time_best": self.all_time_best.get(key),
                }
                for key, shares in self.share_history.items()
            },
        }
        temporary = self.history_path.with_suffix(self.history_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, self.history_path)

    def reconfigure(self, pools: list[dict]):
        active_paths = {
            str(Path(pool.get("log_path", "")).resolve())
            for pool in pools
            if pool.get("log_path")
        }
        self.positions = {path: offset for path, offset in self.positions.items() if path in active_paths}
        active_ids = {self._key(pool) for pool in pools}
        self.latest = {key: value for key, value in self.latest.items() if key in active_ids}
        self.seen_blocks = {key: value for key, value in self.seen_blocks.items() if key in active_ids}
        self.share_history = {key: value for key, value in self.share_history.items() if key in active_ids}
        self.all_time_best = {key: value for key, value in self.all_time_best.items() if key in active_ids}
        self.session_best = {key: value for key, value in self.session_best.items() if key in active_ids}
        self.pools = pools
        self._save_share_history()

    def _network_difficulty(self):
        try:
            snapshot = self.network_provider() or {}
            return _difficulty((snapshot.get("btc") or {}).get("difficulty"))
        except Exception:
            return None

    def _share_summary(self, pool):
        key = self._key(pool)
        shares = sorted(list(self.share_history.get(key, ())), key=lambda item: (item["timestamp_ms"], item["id"]), reverse=True)
        network_difficulty = self._network_difficulty()
        for index, share in enumerate(shares):
            older = shares[index + 1] if index + 1 < len(shares) else None
            share["elapsed_seconds"] = max(0, (share["timestamp_ms"] - older["timestamp_ms"]) / 1000) if older else None
            share["network_percent"] = (
                share["difficulty"] / network_difficulty * 100
                if network_difficulty and network_difficulty > 0 else None
            )
        difficulties = [item["difficulty"] for item in shares]
        highest_id = max(shares, key=lambda item: item["difficulty"])["id"] if shares else None
        return {
            "accepted_shares": shares,
            "accepted_share_count": len(shares),
            "highest_recent_share_id": highest_id,
            "average_share_difficulty": sum(difficulties) / len(difficulties) if difficulties else None,
            "session_best_difficulty": self.session_best.get(key),
            "all_time_best_difficulty": self.all_time_best.get(key),
            "network_difficulty": network_difficulty,
        }

    def _pool_status(self, pool):
        key = self._key(pool)
        if pool.get("mode") == "public_pool_api":
            return {
                "id": key,
                "name": pool.get("name", "Public Pool"),
                "mode": "public_pool_api",
                "enabled": pool.get("enabled", False),
                "api_url": pool.get("api_url"),
                **self.latest.get(key, {"available": False, "message": "Waiting for Public Pool"}),
                **self._share_summary(pool),
            }
        path = Path(pool.get("log_path", ""))
        return {
            "id": key,
            "name": pool.get("name", "Unnamed pool"),
            "mode": pool.get("mode", "local_log"),
            "enabled": pool.get("enabled", False),
            "log_path": str(path),
            "available": path.is_file(),
            "size_bytes": path.stat().st_size if path.is_file() else None,
            **self._share_summary(pool),
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

    def _merge_shares(self, pool, raw_shares):
        key = self._key(pool)
        current = list(self.share_history.get(key, ()))
        by_id = {item["id"]: item for item in current}
        changed = False
        for raw in raw_shares[:100] if isinstance(raw_shares, list) else []:
            share = _normalize_share(raw, key)
            if not share:
                continue
            if share["id"] not in by_id:
                changed = True
            by_id[share["id"]] = share
            best = share["difficulty"]
            self.session_best[key] = max(best, self.session_best.get(key, 0))
            self.all_time_best[key] = max(best, self.all_time_best.get(key, 0))
        merged = sorted(by_id.values(), key=lambda item: (item["timestamp_ms"], item["id"]), reverse=True)
        merged = merged[:MAX_ACCEPTED_SHARES_PER_POOL]
        if [item["id"] for item in merged] != [item["id"] for item in current]:
            changed = True
        self.share_history[key] = deque(merged, maxlen=MAX_ACCEPTED_SHARES_PER_POOL)
        if changed:
            self._save_share_history()

    async def _poll_public_pool(self, pool):
        key = self._key(pool)
        base = str(pool.get("api_url") or "").rstrip("/")
        if not base:
            self.latest[key] = {"available": False, "message": "API URL is missing"}
            return
        try:
            adapter, pool_data, port_status = await asyncio.to_thread(_pool_snapshot, base)
            raw_blocks = pool_data.get("blocksFound", [])
            blocks = raw_blocks if isinstance(raw_blocks, list) else []
            unique_blocks = []
            unique_ids = set()
            for block in reversed(blocks):
                if not isinstance(block, dict):
                    continue
                identifier = f"{block.get('height')}:{block.get('sessionId') or block.get('session_id')}:{block.get('worker')}"
                if identifier in unique_ids:
                    continue
                unique_ids.add(identifier)
                unique_blocks.append({
                    "height": block.get("height"),
                    "worker": block.get("worker"),
                    "session_id": block.get("sessionId") or block.get("session_id"),
                })
                if len(unique_blocks) >= 10:
                    break

            previous = self.seen_blocks.get(key)
            current_real = {
                f"{item.get('height')}:{item.get('session_id')}:{item.get('worker')}"
                for item in unique_blocks if int(item.get("height") or 0) > 1
            }
            if previous is not None:
                for identifier in current_real - set(previous):
                    block = next(item for item in unique_blocks if f"{item.get('height')}:{item.get('session_id')}:{item.get('worker')}" == identifier)
                    event = {
                        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "pool": pool.get("name", "Public Pool"),
                        "category": "block",
                        "title": "Public Pool Block Event",
                        "severity": "critical",
                        "important": True,
                        "line": f"Block height {block.get('height')} reported by {block.get('worker') or 'a worker'}",
                    }
                    self.events.appendleft(event)
                    await self.alert_engine.pool_event(event)
            self.seen_blocks[key] = deque(current_real, maxlen=25)

            address = self._miner_address(pool)
            client = {}
            workers = []
            raw_shares = pool_data.get("acceptedShares") if isinstance(pool_data.get("acceptedShares"), list) else []
            share_feed_available = bool(raw_shares) or adapter == "pocisys_pool_port"
            if adapter == "pocisys_pool_port" and isinstance(port_status, dict):
                workers = port_status.get("workers", []) if isinstance(port_status.get("workers"), list) else []
                if not raw_shares:
                    try:
                        share_payload = await asyncio.to_thread(_api_json, f"{base}/api/shares")
                        raw_shares = share_payload if isinstance(share_payload, list) else []
                    except (OSError, ValueError, TypeError, json.JSONDecodeError):
                        raw_shares = []
                share_feed_available = bool((port_status.get("shareFeed") or {}).get("available", True))
            elif address:
                encoded = urllib.parse.quote(address, safe="")
                try:
                    client = await asyncio.to_thread(_api_json, f"{base}/api/client/{encoded}")
                except (OSError, ValueError, json.JSONDecodeError):
                    client = {}
                workers = client.get("workers", []) if isinstance(client, dict) else []
                workers = workers if isinstance(workers, list) else []
                client_shares = client.get("acceptedShares") if isinstance(client, dict) else None
                if isinstance(client_shares, list):
                    raw_shares = client_shares
                    share_feed_available = True
            self._merge_shares(pool, raw_shares)

            normalized_workers = []
            for item in workers[:MAX_PUBLIC_POOL_WORKERS]:
                if not isinstance(item, dict):
                    continue
                normalized_workers.append({
                    "name": item.get("name") or item.get("clientName") or "worker",
                    "hashrate_ths": float(item.get("hashRate") or 0) / 1e12,
                    "best_difficulty": item.get("bestDifficulty"),
                    "last_seen": item.get("lastSeen") or item.get("updatedAt"),
                })
            worker_best = max((_difficulty(item.get("best_difficulty")) or 0 for item in normalized_workers), default=0)
            client_best = _difficulty(client.get("bestDifficulty")) if isinstance(client, dict) else None
            prior_all_time = self.all_time_best.get(key, 0)
            if worker_best or client_best:
                self.session_best[key] = max(worker_best, client_best or 0, self.session_best.get(key, 0))
                self.all_time_best[key] = max(self.session_best[key], self.all_time_best.get(key, 0))
                if self.all_time_best[key] != prior_all_time:
                    self._save_share_history()

            self.latest[key] = {
                "available": True,
                "message": "PoCiSys Public Pool Port connected" if adapter == "pocisys_pool_port" else "Public Pool API connected",
                "adapter": adapter,
                "share_feed_available": share_feed_available,
                "share_feed_message": "Actual accepted submissions" if share_feed_available else "This pool version does not expose accepted-share submissions",
                "total_hashrate_ths": float(pool_data.get("totalHashRate") or 0) / 1e12,
                "block_height": pool_data.get("blockHeight"),
                "total_miners": pool_data.get("totalMiners"),
                "blocks_found": len(blocks),
                "best_difficulty": client.get("bestDifficulty") if isinstance(client, dict) else worker_best or None,
                "workers_count": len(normalized_workers) if adapter == "pocisys_pool_port" else client.get("workersCount") if isinstance(client, dict) else None,
                "workers": normalized_workers,
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
