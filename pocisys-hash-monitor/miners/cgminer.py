from __future__ import annotations

import socket
import time
from typing import Any

from .base import MAX_API_RESPONSE_BYTES, MinerDriver, integer, normalize_hashrate_ths, number


CGMINER_PORT = 4028


def _field(record: dict[str, Any] | None, *keys: str, default=None):
    if not record:
        return default
    lowered = {str(key).lower(): value for key, value in record.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return default


def _section(records: list[dict[str, Any]], name: str):
    wanted = name.upper()
    return [record for record in records if str(record.get("_section", "")).upper() == wanted]


def _first_section(records: list[dict[str, Any]], *names: str):
    for name in names:
        matches = _section(records, name)
        if matches:
            return matches[0]
    return None


def _parse_cgminer_response(text: str):
    records: list[dict[str, Any]] = []
    for section in text.replace("\x00", "").strip().split("|"):
        section = section.strip()
        if not section:
            continue
        record: dict[str, Any] = {}
        for index, part in enumerate(section.split(",")):
            part = part.strip()
            if not part:
                continue
            if "=" in part:
                key, value = part.split("=", 1)
                clean_key = key.strip()
                if index == 0 and clean_key.upper() == clean_key and " " not in clean_key:
                    record["_section"] = clean_key
                record[clean_key] = value.strip()
            elif index == 0:
                record["_section"] = part.strip()
        if record:
            records.append(record)
    return records


def _compact_firmware(version_record: dict[str, Any] | None, fallback: str):
    bits = [
        _field(version_record, "PROD", "Product", "Manufacturer"),
        _field(version_record, "MODEL", "Model"),
        _field(version_record, "CGMiner", "BMMiner"),
        _field(version_record, "API"),
    ]
    bits = [str(bit) for bit in bits if bit not in (None, "")]
    return " · ".join(bits) if bits else fallback


def _hashrate_from(record: dict[str, Any] | None):
    if not record:
        return None
    candidates = [
        ("GHS 5s", "ghs"),
        ("GHS av", "ghs"),
        ("GHS avg", "ghs"),
        ("GHSmm", "ghs"),
        ("MHS 5s", "mhs"),
        ("MHS av", "mhs"),
        ("MHS avg", "mhs"),
        ("KHS 5s", "khs"),
        ("KHS av", "khs"),
        ("KHS avg", "khs"),
        ("HS 5s", "hs"),
        ("HS av", "hs"),
        ("HS avg", "hs"),
        ("hashrate", "hashrate"),
    ]
    for key, hint in candidates:
        value = _field(record, key)
        parsed = normalize_hashrate_ths(value, hint)
        if parsed is not None:
            return parsed
    return None


def _all_numbers_with_name(records: list[dict[str, Any]], word: str):
    found = []
    word = word.lower()
    for record in records:
        for key, value in record.items():
            if key.startswith("_"):
                continue
            if word in str(key).lower():
                parsed = number(value)
                if parsed is not None:
                    found.append((key, parsed))
    return found


class CgminerDriver(MinerDriver):
    """Read-only cgminer-compatible TCP API driver.

    This intentionally sends only informational commands. It never sends pool,
    reboot, privileged, or configuration commands.
    """

    product_name = "cgminer"

    def host_port(self):
        host = str(self.ip).strip()
        if host.count(":") == 1:
            possible_host, possible_port = host.rsplit(":", 1)
            if possible_port.isdigit():
                return possible_host, int(possible_port)
        return host, CGMINER_PORT

    def command(self, name: str, deadline: float):
        host, port = self.host_port()
        remaining = max(0.1, deadline - time.monotonic())
        started = time.perf_counter()
        chunks: list[bytes] = []
        with socket.create_connection((host, port), timeout=min(self.timeout, remaining)) as sock:
            sock.settimeout(min(self.timeout, remaining))
            sock.sendall(name.encode("ascii"))
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            total = 0
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_API_RESPONSE_BYTES:
                    raise ValueError("Miner API response exceeded the 2 MB safety limit")
        text = b"".join(chunks).decode("utf-8", errors="replace")
        return _parse_cgminer_response(text), round((time.perf_counter() - started) * 1000, 1)

    def safe_command(self, name: str, deadline: float):
        try:
            return self.command(name, deadline)
        except Exception:
            return [], None

    def poll(self):
        result = self.empty_status()
        deadline = time.monotonic() + self.timeout
        errors = []
        commands: dict[str, list[dict[str, Any]]] = {}
        latencies = []

        for command_name in ("version", "summary", "devs", "pools", "stats"):
            try:
                records, latency = self.command(command_name, deadline)
                commands[command_name] = records
                latencies.append(latency)
            except Exception as exc:
                commands[command_name] = []
                errors.append(f"{command_name}: {exc}")
            if deadline - time.monotonic() <= 0:
                break

        version = _first_section(commands.get("version", []), "VERSION")
        summary = _first_section(commands.get("summary", []), "SUMMARY")
        devices = _section(commands.get("devs", []), "ASC") + _section(commands.get("devs", []), "PGA")
        pools = _section(commands.get("pools", []), "POOL")
        stats = commands.get("stats", [])

        if not any((version, summary, devices, pools, stats)):
            result["warnings"].append("cgminer API unreachable: " + "; ".join(errors[:3]))
            result["status"] = "cgminer API unreachable"
            return result

        device_hashrates = [_hashrate_from(device) for device in devices]
        device_hashrates = [value for value in device_hashrates if value is not None]
        hashrate = _hashrate_from(summary)
        if hashrate is None and device_hashrates:
            hashrate = sum(device_hashrates)

        pool = pools[0] if pools else None
        pool_status = str(_field(pool, "Status", "Stratum Active", default="unknown"))
        pool_connected = pool_status.lower() in {"alive", "active", "enabled", "true", "1", "yes"}
        if pool_status.lower() == "unknown":
            pool_connected = None

        temp_values = _all_numbers_with_name(devices + stats, "temp")
        highest_temp = max([value for _, value in temp_values if 0 < value < 150], default=None)
        fan_values = []
        for key, value in _all_numbers_with_name(devices + stats, "fan"):
            if value and value > 0 and "percent" not in key.lower():
                fan_values.append(value)
        seen_fans = []
        for rpm in fan_values:
            if rpm not in seen_fans:
                seen_fans.append(rpm)

        hardware_errors = (
            integer(_field(summary, "Hardware Errors", "Device Hardware%"), None)
            or sum(integer(_field(device, "Hardware Errors"), 0) for device in devices)
            or None
        )
        accepted = integer(_field(summary, "Accepted"), None)
        rejected = integer(_field(summary, "Rejected"), None)
        stale = integer(_field(summary, "Stale"), None)
        if pools:
            accepted = accepted if accepted is not None else sum(integer(_field(item, "Accepted"), 0) for item in pools)
            rejected = rejected if rejected is not None else sum(integer(_field(item, "Rejected"), 0) for item in pools)
            stale = stale if stale is not None else sum(integer(_field(item, "Stale"), 0) for item in pools)

        chip_items = []
        for index, device in enumerate(devices):
            status = str(_field(device, "Status", default="unknown"))
            temp = number(_field(device, "Temperature", "Temp", "Chip Temp"))
            normalized_status = status.lower()
            if normalized_status in {"alive", "enabled", "active", "running", "true", "1"}:
                health_state = "healthy"
            elif normalized_status in {"dead", "failed", "missing", "disabled", "false", "0"}:
                health_state = "unhealthy"
            else:
                health_state = "unknown"
            chip_items.append({
                "name": _field(device, "Name", "ID", default=f"Chain {index + 1}"),
                "status": health_state,
                "chips_healthy": 1 if health_state == "healthy" else 0 if health_state == "unhealthy" else None,
                "chips_total": 1,
                "temperature_c": temp if temp and 0 < temp < 150 else None,
                "hashrate_ths": _hashrate_from(device),
                "hardware_errors": integer(_field(device, "Hardware Errors"), None),
            })

        healthy_count = sum(1 for item in chip_items if item["status"] == "healthy")
        known_count = sum(1 for item in chip_items if item["status"] != "unknown")
        result.update(
            online=True,
            api_ok=True,
            ping_ms=min(latencies) if latencies else None,
            hashrate_ths=hashrate,
            temps={"asic_c": highest_temp, "vrm_c": None, "board_c": None, "chip_c": None},
            chip_health={
                "reported": bool(chip_items),
                "healthy": healthy_count if known_count else None,
                "total": len(chip_items) if chip_items else None,
                "items": chip_items,
            },
            fans=[{"name": f"fan{index + 1}", "rpm": rpm} for index, rpm in enumerate(seen_fans[:8])],
            pool={
                "url": _field(pool, "URL", "Pool", default=None),
                "user": _field(pool, "User", "Username", default=None),
                "connected": pool_connected,
                "status": pool_status,
                "source": "cgminer",
            },
            shares={
                "valid": accepted or 0,
                "invalid": 0,
                "stale": stale or 0,
                "rejected": rejected or 0,
            },
            difficulty={
                "best_session": _field(summary, "Best Share", "Best Share Difficulty", "Best Diff"),
                "best_all_time": _field(summary, "Best Share", "Best Share Difficulty", "Best Diff"),
            },
            uptime_seconds=integer(_field(summary, "Elapsed", "Uptime"), None),
            firmware=_compact_firmware(version, self.product_name),
            hardware_errors=hardware_errors,
            status=str(_field(summary, "Status", "Msg", default="Healthy")),
            raw=commands,
        )
        if errors:
            result["warnings"].append("Some cgminer beta fields were unavailable")
        return result


class AvalonDriver(CgminerDriver):
    product_name = "Avalon / Canaan"
