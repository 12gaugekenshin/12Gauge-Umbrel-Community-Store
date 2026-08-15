from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime, timezone

from .config_store import make_id, normalize_config
from .validation import ApiError, clean_miner, clean_pool


BACKUP_SCHEMA_VERSION = 1
MAX_BACKUP_MINERS = 500
MAX_BACKUP_POOLS = 100

APP_BOOL_KEYS = {
    "difficulty_rain_enabled",
    "lan_access_enabled",
    "luxos_control_enabled",
    "luxos_control_acknowledged",
}
APP_INT_RANGES = {
    "poll_interval_seconds": (2, 3600, 10),
    "dashboard_port": (1024, 65535, 8765),
    "alert_cooldown_seconds": (0, 86400, 600),
    "offline_alert_grace_seconds": (0, 3600, 60),
    "pool_disconnect_grace_seconds": (0, 3600, 60),
    "control_utc_offset_minutes": (-840, 840, 0),
}
APP_FLOAT_RANGES = {"request_timeout_seconds": (0.5, 30.0, 4.0)}
DISCORD_BOOL_KEYS = {
    "enabled",
    "send_offline_alerts",
    "send_recovery_alerts",
    "send_hashrate_alerts",
    "send_temperature_alerts",
    "send_chip_health_alerts",
    "send_control_alerts",
    "send_best_diff_alerts",
    "send_block_found_alerts",
    "send_pool_alerts",
    "send_pool_switch_alerts",
    "send_share_alerts",
    "verbose_pool_events",
}
ODDS_BOOL_KEYS = {
    "btc_enabled",
    "bch_enabled",
    "bsv_enabled",
    "xec_enabled",
    "dgb_enabled",
    "chta_enabled",
    "auto_network_data",
}
ODDS_NUMBER_KEYS = {
    "manual_btc_network_hashrate_eh",
    "manual_bch_network_hashrate_eh",
    "manual_bsv_network_hashrate_eh",
    "manual_xec_network_hashrate_eh",
    "manual_dgb_network_hashrate_eh",
    "manual_chta_network_hashrate_eh",
}


def make_safe_backup(config: dict, app_version: str):
    safe = deepcopy(config)
    safe.setdefault("discord", {}).pop("webhook_url", None)
    safe["discord"]["enabled"] = False
    safe["hermes"] = {"enabled": False, "token_hash": "", "token_hint": ""}
    return {
        "schema_version": BACKUP_SCHEMA_VERSION,
        "app": "PoCiSys Hash Monitor",
        "app_version": app_version,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sanitized": True,
        "excluded": ["discord.webhook_url", "hermes.token_hash", "hermes.token_hint"],
        "config": safe,
    }


def _number(value, low, high, default, integer=False):
    if value in (None, ""):
        return default
    try:
        parsed = int(value) if integer else float(value)
    except (TypeError, ValueError):
        raise ApiError(400, "Backup contains an invalid numeric setting")
    return max(low, min(high, parsed))


def _clean_app(imported: dict, current: dict):
    result = deepcopy(current)
    for key in APP_BOOL_KEYS:
        if key in imported:
            result[key] = bool(imported[key])
    for key, (low, high, default) in APP_INT_RANGES.items():
        if key in imported:
            result[key] = _number(imported[key], low, high, default, integer=True)
    for key, (low, high, default) in APP_FLOAT_RANGES.items():
        if key in imported:
            result[key] = _number(imported[key], low, high, default)
    if "dashboard_density" in imported:
        density = str(imported.get("dashboard_density") or "comfortable")
        if density not in {"comfortable", "compact"}:
            raise ApiError(400, "Backup contains an unsupported dashboard density")
        result["dashboard_density"] = density
    if "dashboard_base_url" in imported:
        value = str(imported.get("dashboard_base_url") or "").strip().rstrip("/")
        if value and not value.startswith(("http://", "https://")):
            raise ApiError(400, "Backup dashboard URL must begin with http:// or https://")
        result["dashboard_base_url"] = value[:1024]
    if "control_timezone" in imported:
        value = str(imported.get("control_timezone") or "auto").strip()[:80]
        if not re.fullmatch(r"[A-Za-z0-9_+./-]+", value):
            raise ApiError(400, "Backup contains an invalid schedule timezone")
        result["control_timezone"] = value
    return result


def _clean_items(items, cleaner, prefix, limit):
    if not isinstance(items, list):
        raise ApiError(400, f"Backup {prefix} list is invalid")
    if len(items) > limit:
        raise ApiError(400, f"Backup contains more than {limit} {prefix}s")
    result = []
    used_ids = set()
    for position, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ApiError(400, f"Backup {prefix} {position} is invalid")
        cleaned = cleaner(item)
        item_id = str(item.get("id") or "").strip()
        if not re.fullmatch(rf"{prefix}_[a-f0-9]{{12}}", item_id) or item_id in used_ids:
            item_id = make_id(prefix)
        used_ids.add(item_id)
        cleaned["id"] = item_id
        if prefix == "miner":
            cleaned["display_order"] = position
        result.append(cleaned)
    return result


def restore_safe_backup(payload: dict, current_config: dict):
    if not isinstance(payload, dict):
        raise ApiError(400, "Backup must be a JSON object")
    schema = payload.get("schema_version", BACKUP_SCHEMA_VERSION)
    if schema != BACKUP_SCHEMA_VERSION:
        raise ApiError(400, "Unsupported PoCiSys backup version")
    imported = payload.get("config", payload)
    if not isinstance(imported, dict):
        raise ApiError(400, "Backup config is missing")

    updated = deepcopy(current_config)
    updated["app"] = _clean_app(imported.get("app", {}), updated.get("app", {}))
    updated["miners"] = _clean_items(imported.get("miners", []), clean_miner, "miner", MAX_BACKUP_MINERS)
    updated["pools"] = _clean_items(imported.get("pools", []), clean_pool, "pool", MAX_BACKUP_POOLS)

    imported_discord = imported.get("discord", {})
    if not isinstance(imported_discord, dict):
        raise ApiError(400, "Backup Discord settings are invalid")
    discord = deepcopy(updated.get("discord", {}))
    for key in DISCORD_BOOL_KEYS:
        if key in imported_discord:
            discord[key] = bool(imported_discord[key])
    if not discord.get("webhook_url"):
        discord["enabled"] = False
    updated["discord"] = discord

    imported_odds = imported.get("odds", {})
    if not isinstance(imported_odds, dict):
        raise ApiError(400, "Backup coin settings are invalid")
    odds = deepcopy(updated.get("odds", {}))
    for key in ODDS_BOOL_KEYS:
        if key in imported_odds:
            odds[key] = bool(imported_odds[key])
    for key in ODDS_NUMBER_KEYS:
        if key not in imported_odds:
            continue
        value = imported_odds[key]
        if value in (None, ""):
            odds[key] = None
        else:
            odds[key] = _number(value, 0.0, 1e12, None)
    updated["odds"] = odds

    hermes = deepcopy(updated.get("hermes", {}))
    imported_hermes = imported.get("hermes", {})
    if isinstance(imported_hermes, dict) and "enabled" in imported_hermes:
        hermes["enabled"] = bool(imported_hermes["enabled"] and hermes.get("token_hash"))
    updated["hermes"] = hermes
    return normalize_config(updated)
