from __future__ import annotations

import json
import re
import socket
import time

from .base import MAX_API_RESPONSE_BYTES, MinerDriver, first, integer, normalize_hashrate_ths, number, truthy


def _first_row(payload: dict, section: str):
    rows = payload.get(section, []) if isinstance(payload, dict) else []
    return rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}


def _positive_values(row: dict, pattern: str):
    matcher = re.compile(pattern, re.IGNORECASE)
    values = []
    for key, value in row.items():
        parsed = number(value)
        if matcher.fullmatch(str(key)) and parsed is not None and parsed > 0:
            values.append(parsed)
    return values


class LuxOSDriver(MinerDriver):
    api_paths = (
        "/api/miner/status",
        "/api/status",
        "/api/v1/status",
        "/api/system/info",
    )

    def _cgminer(self):
        merged = {}
        started = time.perf_counter()
        deadline = time.monotonic() + self.timeout
        for command in ("summary", "pools", "devs", "stats"):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("LuxOS API poll exceeded its time limit")
            request = json.dumps({"command": command}).encode()
            with socket.create_connection((self.ip, 4028), timeout=remaining) as sock:
                sock.settimeout(remaining)
                sock.sendall(request)
                body = bytearray()
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > MAX_API_RESPONSE_BYTES:
                        raise ValueError("LuxOS API response exceeded the 2 MB safety limit")
            text = bytes(body).rstrip(b"\x00").decode("utf-8", errors="replace")
            if text:
                merged[command] = json.loads(text)
        return merged, round((time.perf_counter() - started) * 1000, 1)

    def _normalized_cgminer(self, data: dict, latency: float):
        result = self.empty_status()
        summary_payload = data.get("summary", {})
        pools_payload = data.get("pools", {})
        stats_payload = data.get("stats", {})
        summary = _first_row(summary_payload, "SUMMARY")
        pools = pools_payload.get("POOLS", []) if isinstance(pools_payload, dict) else []
        pools = [pool for pool in pools if isinstance(pool, dict)]
        active_pool = next(
            (pool for pool in pools if truthy(pool.get("Stratum Active")) or str(pool.get("Status", "")).lower() == "alive"),
            pools[0] if pools else {},
        )
        stats_rows = stats_payload.get("STATS", []) if isinstance(stats_payload, dict) else []
        stats_rows = [row for row in stats_rows if isinstance(row, dict)]
        stats = next(
            (row for row in stats_rows if any(key in row for key in ("GHS 5s", "GHS av", "temp_max", "fan_num"))),
            stats_rows[-1] if stats_rows else {},
        )
        devs_payload = data.get("devs", {})
        devs = devs_payload.get("DEVS", []) if isinstance(devs_payload, dict) else []
        devs = [row for row in devs if isinstance(row, dict)]

        hash_value = summary.get("GHS 5s")
        hash_key = "GHS 5s"
        if hash_value in (None, ""):
            hash_value = summary.get("GHS av")
            hash_key = "GHS av"
        if hash_value in (None, ""):
            hash_value = stats.get("GHS 5s", stats.get("GHS av"))
        hashrate = normalize_hashrate_ths(hash_value, hash_key)

        local_temps = _positive_values(stats, r"temp\d+")
        remote_temps = _positive_values(stats, r"temp2(?:_\d+)?")
        board_temp = max(local_temps, default=number(stats.get("temp_max")))
        chip_temp = max(remote_temps, default=None)

        fan_values = _positive_values(stats, r"fan\d+")
        fans = [{"name": f"fan{index + 1}", "rpm": rpm} for index, rpm in enumerate(fan_values)]
        pool_status = str(active_pool.get("Status", "unknown"))
        pool_connected = (
            truthy(active_pool.get("Stratum Active"))
            if active_pool
            else None
        )
        if active_pool and active_pool.get("Stratum Active") is None:
            pool_connected = pool_status.lower() == "alive"

        status_metadata = _first_row(summary_payload, "STATUS")
        firmware = status_metadata.get("Description") or (
            first(stats_rows[0], "Description", "Miner", default="LuxOS") if stats_rows else "LuxOS"
        )
        hardware_errors = sum(_positive_values(stats, r"chain_hw\d+"))
        chip_items = []
        for index, device in enumerate(devs):
            chain = index + 1
            chip_map = str(stats.get(f"chain_acs{chain}") or "")
            symbols = [symbol.lower() for symbol in chip_map if not symbol.isspace()]
            good_chips = symbols.count("o") if symbols else integer(stats.get(f"chain_acn{chain}"), None)
            total_chips = len(symbols) if symbols else integer(stats.get(f"chain_acn{chain}"), None)
            device_status = str(device.get("Status") or "unknown")
            chain_faults = _positive_values(stats, rf"chain_hw{chain}")
            missing_chips = good_chips is not None and total_chips is not None and good_chips < total_chips
            healthy = device_status.lower() == "alive" and not chain_faults and not missing_chips
            chip_items.append({
                "name": f"Hashboard {chain}",
                "status": "healthy" if healthy else "warning",
                "chips_healthy": good_chips,
                "chips_total": total_chips,
                "temperature_c": number(device.get("Temperature")),
                "hashrate_ths": normalize_hashrate_ths(
                    device.get("MHS 5s", stats.get(f"chain_rate{chain}")),
                    "mhs" if device.get("MHS 5s") is not None else "ghs",
                ),
                "hardware_errors": integer(device.get("Hardware Errors"), 0),
                "board": device.get("Board"),
            })
        healthy_boards = sum(1 for item in chip_items if item["status"] == "healthy")

        result.update(
            online=True,
            api_ok=True,
            ping_ms=latency,
            hashrate_ths=hashrate,
            expected_hashrate_ths=normalize_hashrate_ths(stats.get("total_rateideal"), "ghs"),
            temps={
                "asic_c": None,
                "vrm_c": None,
                "board_c": board_temp,
                "chip_c": chip_temp,
            },
            chip_health={
                "reported": bool(chip_items),
                "healthy": healthy_boards if chip_items else None,
                "total": len(chip_items) if chip_items else None,
                "items": chip_items,
            },
            fans=fans,
            pool={
                "url": active_pool.get("URL") or active_pool.get("Stratum URL"),
                "user": active_pool.get("User"),
                "connected": pool_connected,
                "status": pool_status,
                "source": f"pool {active_pool.get('POOL')}" if active_pool.get("POOL") is not None else "active",
            },
            shares={
                "valid": integer(summary.get("Accepted")),
                "invalid": integer(summary.get("Discarded")),
                "stale": integer(summary.get("Stale")),
                "rejected": integer(summary.get("Rejected")),
            },
            difficulty={
                "best_session": summary.get("Best Session Share"),
                "best_all_time": summary.get("Best Share"),
            },
            uptime_seconds=integer(summary.get("Elapsed"), None),
            firmware=firmware,
            frequency_mhz=number(stats.get("frequency", stats.get("total_freqavg"))),
            hardware_errors=int(hardware_errors) if hardware_errors else 0,
            blocks_found=integer(summary.get("Found Blocks"), 0),
            status="Healthy",
            raw=data,
        )
        return result

    def _normalized_rest(self, data: dict, latency: float):
        """Fallback for LuxOS variants exposing an HTTP status object."""
        result = self.empty_status()
        hashrate_keys = ("GHS 5s", "GHS av", "THS 5s", "hashrate", "hashRate")
        hash_value = first(data, *hashrate_keys)
        hash_key = next((key for key in hashrate_keys if first(data, key) is not None), "")
        hashrate = normalize_hashrate_ths(hash_value, hash_key)
        temp = number(first(data, "Temperature", "Temp", "temp", "boardTemp", "chipTemp"))
        fan_values = []
        for key in ("Fan Speed In", "Fan Speed Out", "fan1", "fan2", "fanRPM"):
            rpm = number(first(data, key))
            if rpm is not None and rpm not in fan_values:
                fan_values.append(rpm)
        pool_status_value = first(data, "poolStatus", "stratumStatus", default="unknown")
        pool_status = pool_status_value if isinstance(pool_status_value, (str, int, float, bool)) else "unknown"
        pool_connected = truthy(first(data, "Stratum Active", "poolConnected"), None)
        if pool_connected is None and str(pool_status).lower() != "unknown":
            pool_connected = str(pool_status).lower() in {"alive", "connected", "active"}

        result.update(
            online=True,
            api_ok=True,
            ping_ms=latency,
            hashrate_ths=hashrate,
            expected_hashrate_ths=normalize_hashrate_ths(
                first(data, "expectedHashrate", "total_rateideal"),
                "ghs",
            ),
            temps={
                "asic_c": number(first(data, "asicTemp")),
                "vrm_c": number(first(data, "vrmTemp")),
                "board_c": number(first(data, "boardTemp", "Temperature")) or temp,
                "chip_c": number(first(data, "chipTemp", "Temp")),
            },
            chip_health={"reported": False, "healthy": None, "total": None, "items": []},
            fans=[{"name": f"fan{index + 1}", "rpm": rpm} for index, rpm in enumerate(fan_values)],
            pool={
                "url": first(data, "URL", "poolUrl", "pool", "stratumURL"),
                "user": first(data, "User", "stratumUser"),
                "connected": pool_connected,
                "status": str(pool_status),
                "source": str(first(data, "poolSource", default="active")),
            },
            shares={
                "valid": integer(first(data, "Accepted", "sharesAccepted", "validShares")),
                "invalid": integer(first(data, "Discarded", "invalidShares")),
                "stale": integer(first(data, "Stale", "staleShares")),
                "rejected": integer(first(data, "Rejected", "rejectedShares")),
            },
            difficulty={
                "best_session": first(data, "Best Session Share", "bestSessionDiff"),
                "best_all_time": first(data, "Best Share", "bestDiff", "bestDifficulty"),
            },
            uptime_seconds=integer(first(data, "Elapsed", "uptime", "uptimeSeconds"), None),
            firmware=first(data, "Description", "firmware", "version", default="LuxOS"),
            hardware_errors=integer(first(data, "Hardware Errors", "Hardware Errors MHS", "hwErrors"), None),
            blocks_found=integer(first(data, "Found Blocks", "blockFound"), 0),
            status=str(first(data, "state", "minerStatus", default="Healthy")),
            raw=data,
        )
        return result

    def poll(self):
        try:
            data, latency = self.fetch_first_json()
            return self._normalized_rest(data, latency)
        except Exception as rest_error:
            try:
                data, latency = self._cgminer()
                return self._normalized_cgminer(data, latency)
            except Exception as cg_error:
                result = self.empty_status()
                result["warnings"].append(f"API unreachable: REST: {rest_error}; port 4028: {cg_error}")
                result["status"] = "API unreachable"
                return result
