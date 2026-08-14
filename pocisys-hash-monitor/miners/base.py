from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

MAX_API_RESPONSE_BYTES = 2 * 1024 * 1024


def first(data: Any, *keys: str, default=None):
    """Find the first matching key, including nested API objects."""
    if isinstance(data, list):
        for item in data:
            found = first(item, *keys, default=None)
            if found not in (None, ""):
                return found
        return default
    if not isinstance(data, dict):
        return default
    lowered = {str(k).lower(): v for k, v in data.items()}
    for key in keys:
        if key.lower() in lowered and lowered[key.lower()] not in (None, ""):
            return lowered[key.lower()]
    for value in data.values():
        if isinstance(value, (dict, list)):
            found = first(value, *keys, default=None)
            if found not in (None, ""):
                return found
    return default


def number(value: Any, default=None):
    try:
        if value is None or value == "":
            return default
        return float(str(value).replace(",", "").split()[0])
    except (ValueError, TypeError):
        return default


def integer(value: Any, default=0):
    parsed = number(value)
    return int(parsed) if parsed is not None else default


def truthy(value: Any, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "connected", "alive"}


def normalize_hashrate_ths(value: Any, key_hint: str = ""):
    parsed = number(value)
    if parsed is None:
        return None
    hint = key_hint.lower()
    if "ths" in hint or "th/s" in hint:
        return parsed
    if "gh" in hint:
        return parsed / 1000
    if "mh" in hint:
        return parsed / 1_000_000
    if "kh" in hint:
        return parsed / 1_000_000_000
    if hint.endswith("hs") or "hashrate" in hint and parsed > 1_000_000:
        return parsed / 1_000_000_000_000
    return parsed


class MinerDriver:
    api_paths: tuple[str, ...] = ()

    def __init__(self, miner: dict, timeout: float = 4.0):
        self.miner = miner
        self.ip = miner["ip"]
        self.timeout = timeout

    def empty_status(self):
        return {
            "name": self.miner.get("name", self.ip),
            "ip": self.ip,
            "type": self.miner.get("type", "unknown"),
            "group": self.miner.get("group", "Ungrouped"),
            "mining_target": self.miner.get("mining_target", "btc"),
            "online": False,
            "api_ok": False,
            "ping_ms": None,
            "hashrate_ths": None,
            "expected_hashrate_ths": None,
            "temps": {"asic_c": None, "vrm_c": None, "board_c": None, "chip_c": None},
            "chip_health": {"reported": False, "healthy": None, "total": None, "items": []},
            "fans": [],
            "pool": {"url": None, "user": None, "connected": None, "status": "unknown", "source": None},
            "shares": {"valid": 0, "invalid": 0, "stale": 0, "rejected": 0},
            "difficulty": {"best_session": None, "best_all_time": None},
            "uptime_seconds": None,
            "firmware": None,
            "frequency_mhz": None,
            "voltage_mv": None,
            "wifi_rssi": None,
            "hardware_errors": None,
            "hardware_error_percent": None,
            "blocks_found": 0,
            "status": "Offline",
            "health": {"state": "Offline", "reasons": [], "diagnostics": [], "transition": None},
            "warnings": [],
            "raw": {},
        }

    def get_json(self, path: str, timeout: float | None = None):
        url = path if path.startswith("http") else f"http://{self.ip}{path}"
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "PoCiSys-Hash-Monitor/1.0"},
        )
        started = time.perf_counter()
        with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
            body = response.read(MAX_API_RESPONSE_BYTES + 1)
            if len(body) > MAX_API_RESPONSE_BYTES:
                raise ValueError("Miner API response exceeded the 2 MB safety limit")
            payload = json.loads(body.decode("utf-8", errors="replace"))
        return payload, round((time.perf_counter() - started) * 1000, 1)

    def fetch_first_json(self):
        errors = []
        deadline = time.monotonic() + self.timeout
        for path in self.api_paths:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                data, latency = self.get_json(path, remaining)
                if isinstance(data, dict):
                    return data, latency
                errors.append(f"{path}: response was not an object")
            except (OSError, ValueError, urllib.error.URLError) as exc:
                errors.append(f"{path}: {exc}")
        raise ConnectionError("; ".join(errors))

    def poll(self):
        raise NotImplementedError
