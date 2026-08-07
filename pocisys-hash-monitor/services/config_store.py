from __future__ import annotations

import json
import os
import uuid
from copy import deepcopy
from pathlib import Path


def make_id(prefix: str):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def normalize_config(value: dict):
    config = deepcopy(value)
    config.setdefault("app", {})
    config.setdefault("miners", [])
    config.setdefault("pools", [])
    config.setdefault("discord", {})
    config.setdefault("odds", {})
    config.setdefault("hermes", {})
    config["app"].setdefault("dashboard_density", "comfortable")
    config["app"].setdefault("difficulty_rain_enabled", True)
    config["app"].setdefault("dashboard_base_url", "")
    config["app"].setdefault("lan_access_enabled", False)
    config["app"].setdefault("offline_alert_grace_seconds", 60)
    config["hermes"].setdefault("enabled", False)
    config["hermes"].setdefault("token_hash", "")
    config["hermes"].setdefault("token_hint", "")
    config["app"].setdefault("luxos_control_enabled", False)
    config["app"].setdefault("control_timezone", "auto")
    config["app"].setdefault("control_utc_offset_minutes", 0)
    config["odds"].setdefault("auto_network_data", True)
    config["odds"].setdefault("btc_enabled", True)
    config["odds"].setdefault("bch_enabled", True)
    config["odds"].setdefault("bsv_enabled", True)
    config["odds"].setdefault("xec_enabled", True)
    config["odds"].setdefault("dgb_enabled", True)
    config["odds"].setdefault("chta_enabled", True)
    discord_defaults = {
        "send_offline_alerts": True,
        "send_recovery_alerts": True,
        "send_hashrate_alerts": True,
        "send_temperature_alerts": True,
        "send_chip_health_alerts": True,
        "send_control_alerts": True,
        "send_best_diff_alerts": True,
        "send_block_found_alerts": True,
        "send_pool_alerts": True,
        "send_pool_switch_alerts": True,
        "send_share_alerts": True,
        "verbose_pool_events": False,
    }
    for key, default in discord_defaults.items():
        config["discord"].setdefault(key, default)
    for miner in config["miners"]:
        miner.setdefault("id", make_id("miner"))
        if "min_hashrate_ths" not in miner:
            expected = miner.get("expected_hashrate_ths")
            percent = miner.get("hashrate_warning_percent", 75)
            miner["min_hashrate_ths"] = (
                round(float(expected) * float(percent) / 100, 6)
                if expected not in (None, "")
                else None
            )
        miner.pop("expected_hashrate_ths", None)
        miner.pop("hashrate_warning_percent", None)
        miner.pop("fan_min_rpm", None)
        if str(miner.get("type") or "").lower() == "luxos":
            miner.setdefault("control_enabled", False)
            miner.setdefault("control_schedule_enabled", False)
            miner.setdefault("control_low_mode", "profile")
            miner.setdefault("control_low_time", "16:00")
            miner.setdefault("control_full_time", "21:00")
            miner.setdefault("control_low_profile", "")
            miner.setdefault("control_full_profile", "")
            miner.setdefault("auto_recover_hashboards", False)
            miner.setdefault("chip_health_score_threshold", 90)
    for pool in config["pools"]:
        pool.setdefault("id", make_id("pool"))
        pool.setdefault("mode", "local_log")
        pool.setdefault("log_path", "")
        pool.setdefault("api_url", "")
        pool.setdefault("bitcoin_address", "")
    return config


def load_config(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return normalize_config(json.load(handle))


def save_config(path: Path, config: dict):
    """Write the small app config.

    Umbrel app-data mounts can be backed by appliance-managed filesystems where
    fsync/atomic-replace behavior is less predictable than a plain tiny write.
    V1 stores only config.json, so keep this deliberately boring and portable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(config, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()


def apply_in_place(target: dict, updated: dict):
    """Preserve service references while applying a newly validated config."""
    for section in ("app", "discord", "odds", "hermes"):
        current = target.setdefault(section, {})
        current.clear()
        current.update(deepcopy(updated.get(section, {})))
    for section in ("miners", "pools"):
        current = target.setdefault(section, [])
        current[:] = deepcopy(updated.get(section, []))


def public_config(config: dict):
    safe = deepcopy(config)
    if safe.get("discord", {}).get("webhook_url"):
        safe["discord"]["webhook_url"] = "configured (hidden)"
    if safe.get("hermes", {}).get("token_hash"):
        safe["hermes"]["token_hash"] = "configured (hidden)"
    return safe
