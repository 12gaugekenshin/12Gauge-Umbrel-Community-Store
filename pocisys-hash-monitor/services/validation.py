from __future__ import annotations

import re
import urllib.parse


HOST_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")
MINER_TYPES = {"axeos", "bitaxe", "nerdaxe", "nerdqaxe", "luxos", "canaan_avalon", "avalon", "cgminer"}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def as_float(value, default=None):
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ApiError(400, "Enter a valid number")


def as_int(value, default=0):
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ApiError(400, "Enter a valid whole number")


def clean_host(value):
    host = str(value or "").strip()
    for prefix in ("http://", "https://"):
        if host.lower().startswith(prefix):
            host = host[len(prefix):]
    host = host.rstrip("/")
    if not host or "/" in host or not HOST_PATTERN.fullmatch(host):
        raise ApiError(400, "Use an IP address or hostname without a URL path")
    return host


def clean_time(value, default):
    text = str(value or default).strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
        raise ApiError(400, "Schedule times must use HH:MM")
    return text


def clean_profile(value, label):
    text = str(value or "").strip()
    if len(text) > 80 or "," in text or any(ord(char) < 32 for char in text):
        raise ApiError(400, f"Choose a valid {label} LuxOS profile")
    if text in {"0", "1", "2", "3"}:
        raise ApiError(400, f"LuxOS profile name '{text}' is ambiguous; choose a named profile")
    return text


def clean_miner(data):
    name = str(data.get("name") or "").strip()
    miner_type = str(data.get("type") or "").lower()
    if not name or len(name) > 80:
        raise ApiError(400, "Miner name is required")
    if miner_type not in MINER_TYPES:
        raise ApiError(400, "Unsupported miner API type")
    warning = as_float(data.get("temp_warning_c"), 70)
    critical = as_float(data.get("temp_critical_c"), 80)
    if warning is not None and not 0 <= warning <= 150:
        raise ApiError(400, "Temperature warning must be between 0 and 150")
    if critical is not None and not 0 <= critical <= 150:
        raise ApiError(400, "Critical temperature must be between 0 and 150")
    if warning is not None and critical is not None and critical < warning:
        raise ApiError(400, "Critical temperature must be at least the warning temperature")
    minimum = as_float(data.get("min_hashrate_ths"), None)
    if minimum is not None and minimum < 0:
        raise ApiError(400, "Minimum hashrate cannot be negative")
    mining_target = str(data.get("mining_target") or "btc").lower()
    if mining_target not in {"btc", "bch", "pool"}:
        raise ApiError(400, "Choose BTC Solo, BCH Solo, or Pool mining")
    cleaned = {
        "name": name,
        "ip": clean_host(data.get("ip")),
        "type": miner_type,
        "group": str(data.get("group") or "Ungrouped").strip()[:80] or "Ungrouped",
        "mining_target": mining_target,
        "enabled": bool(data.get("enabled", True)),
        "display_order": max(0, min(9999, as_int(data.get("display_order"), 0))),
        "min_hashrate_ths": minimum,
        "temp_warning_c": warning,
        "temp_critical_c": critical,
    }
    if miner_type == "luxos":
        low_mode = str(data.get("control_low_mode") or "profile")
        if low_mode not in {"profile", "boards_off"}:
            raise ApiError(400, "Choose low profile or Sleep (hashboards off) mode")
        low_time = clean_time(data.get("control_low_time"), "16:00")
        full_time = clean_time(data.get("control_full_time"), "21:00")
        if low_time == full_time and data.get("control_schedule_enabled"):
            raise ApiError(400, "Low-power and full-power schedule times must be different")
        low_profile = clean_profile(data.get("control_low_profile"), "low-power")
        full_profile = clean_profile(data.get("control_full_profile"), "normal")
        control_enabled = bool(data.get("control_enabled", False))
        schedule_enabled = bool(data.get("control_schedule_enabled", False))
        recovery_enabled = bool(data.get("auto_recover_hashboards", False))
        if (control_enabled or schedule_enabled) and not full_profile:
            raise ApiError(400, "Select the normal LuxOS profile before enabling miner control")
        if schedule_enabled and low_mode == "profile" and not low_profile:
            raise ApiError(400, "Select the low-power LuxOS profile for this schedule")
        cleaned.update(
            control_enabled=control_enabled,
            control_schedule_enabled=schedule_enabled,
            control_low_mode=low_mode,
            control_low_time=low_time,
            control_full_time=full_time,
            control_low_profile=low_profile,
            control_full_profile=full_profile,
            auto_recover_hashboards=recovery_enabled,
            chip_health_score_threshold=max(0, min(100, as_float(data.get("chip_health_score_threshold"), 0))),
        )
    return cleaned


def clean_pool(data):
    name = str(data.get("name") or "").strip()
    mode = str(data.get("mode") or "public_pool_api")
    if not name or len(name) > 80:
        raise ApiError(400, "Local pool monitor name is required")
    if mode not in {"public_pool_api", "local_log"}:
        raise ApiError(400, "Unsupported local pool monitor type")
    api_url = str(data.get("api_url") or "").strip().rstrip("/")
    log_path = str(data.get("log_path") or "").strip()
    if mode == "public_pool_api":
        parsed = urllib.parse.urlparse(api_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ApiError(400, "Enter a valid self-hosted Public Pool API URL")
    elif not log_path:
        raise ApiError(400, "Add a local pool log path")
    return {
        "name": name,
        "type": str(data.get("type") or ("public_pool" if mode == "public_pool_api" else "ckpool"))[:40],
        "mode": mode,
        "enabled": bool(data.get("enabled", True)),
        "log_path": log_path[:1024],
        "api_url": api_url[:1024],
        "bitcoin_address": str(data.get("bitcoin_address") or "").strip()[:160],
    }
