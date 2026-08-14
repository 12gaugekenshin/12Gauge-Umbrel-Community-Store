from __future__ import annotations

import math


COINS = (
    ("BTC", "btc_enabled", "manual_btc_network_hashrate_eh", 144),
    ("BCH", "bch_enabled", "manual_bch_network_hashrate_eh", 144),
    ("BSV", "bsv_enabled", "manual_bsv_network_hashrate_eh", 144),
    ("XEC", "xec_enabled", "manual_xec_network_hashrate_eh", 144),
    ("DGB", "dgb_enabled", "manual_dgb_network_hashrate_eh", 1152),
    ("CHTA", "chta_enabled", "manual_chta_network_hashrate_eh", 720),
)


def _network_odds(symbol: str, enabled: bool, total_ths: float, network_eh, metadata=None, default_blocks_per_day=144):
    if not enabled:
        return {"symbol": symbol, "enabled": False}
    if not network_eh or network_eh <= 0:
        return {
            "symbol": symbol,
            "enabled": True,
            "available": False,
            "message": "Live network data is unavailable. Add a manual fallback in Settings.",
        }
    metadata = metadata or {}
    network_ths = float(network_eh) * 1_000_000
    blocks_per_day = float(metadata.get("blocks_per_day") or default_blocks_per_day)
    expected_per_day = total_ths / network_ths * blocks_per_day

    def chance(days):
        return 1 - math.exp(-(expected_per_day * days))

    return {
        "symbol": symbol,
        "enabled": True,
        "available": True,
        "miner_hashrate_ths": total_ths,
        "network_hashrate_eh": float(network_eh),
        "network_source": metadata.get("source", "Manual fallback"),
        "difficulty": metadata.get("difficulty"),
        "price_usd": metadata.get("price_usd"),
        "updated_at": metadata.get("updated_at"),
        "blocks_per_day": blocks_per_day,
        "estimate_note": metadata.get("estimate_note"),
        "expected_blocks_per_day": expected_per_day,
        "daily_chance": chance(1),
        "weekly_chance": chance(7),
        "monthly_chance": chance(30),
        "estimated_days_to_block": 1 / expected_per_day if expected_per_day else None,
    }


def calculate_odds(statuses: list[dict], config: dict, network_snapshot=None):
    total_ths = sum(float(item.get("hashrate_ths") or 0) for item in statuses if item.get("online"))
    assigned_ths = {"btc": 0.0, "bch": 0.0}
    for item in statuses:
        if not item.get("online"):
            continue
        target = str(item.get("mining_target") or "btc").lower()
        if target in assigned_ths:
            assigned_ths[target] += float(item.get("hashrate_ths") or 0)
    odds_config = config.get("odds", {})
    live = network_snapshot or {}
    result = {"total_hashrate_ths": total_ths, "assigned_hashrate_ths": assigned_ths}

    for symbol, enabled_key, manual_key, blocks_per_day in COINS:
        item = live.get(symbol.lower(), {}) if odds_config.get("auto_network_data", True) else {}
        if item.get("available") and item.get("network_hashrate_eh"):
            network_hashrate, metadata = item.get("network_hashrate_eh"), item
        else:
            network_hashrate, metadata = odds_config.get(manual_key), None
        coin_key = symbol.lower()
        odds_hashrate_ths = assigned_ths[coin_key] if coin_key in assigned_ths else total_ths
        result[coin_key] = _network_odds(
            symbol,
            odds_config.get(enabled_key, True),
            odds_hashrate_ths,
            network_hashrate,
            metadata,
            blocks_per_day,
        )
    return result
