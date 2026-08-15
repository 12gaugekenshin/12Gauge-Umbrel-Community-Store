from __future__ import annotations

import re
from datetime import datetime, timezone


MAX_DIAGNOSTIC_MINERS = 100
MAX_DIAGNOSTIC_POOLS = 100
MAX_DIAGNOSTIC_ALERTS = 25
MAX_DIAGNOSTIC_POOL_EVENTS = 50
MAX_DIAGNOSTIC_HEALTH_TRANSITIONS = 50
MAX_DIAGNOSTIC_CONTROL_ACTIONS = 25
MAX_COLLECTION_ITEMS = 100
MAX_STRING_LENGTH = 500

SENSITIVE_KEYS = {
    "ip",
    "host",
    "hostname",
    "api_url",
    "log_path",
    "bitcoin_address",
    "wallet",
    "wallet_address",
    "webhook_url",
    "token",
    "token_hash",
    "token_hint",
    "authorization",
    "password",
    "user",
    "username",
}
SKIP_KEYS = {"raw", "response", "full_response", "api_response"}
IP_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?(?![\d.])")
URL_RE = re.compile(r"\b(?:https?|stratum\+tcp)://[^\s<>'\"]+", re.IGNORECASE)


def _sanitize(value, key="", depth=0):
    if depth > 8:
        return "[depth capped]"
    lowered = str(key).casefold()
    if lowered in SENSITIVE_KEYS or "webhook" in lowered or lowered.endswith("_token"):
        return "[redacted]" if value not in (None, "", [], {}) else value
    if isinstance(value, dict):
        result = {}
        for index, (item_key, item_value) in enumerate(value.items()):
            if index >= MAX_COLLECTION_ITEMS:
                result["_truncated"] = True
                break
            if str(item_key).casefold() in SKIP_KEYS:
                continue
            result[str(item_key)[:80]] = _sanitize(item_value, item_key, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, key, depth + 1) for item in list(value)[:MAX_COLLECTION_ITEMS]]
    if isinstance(value, str):
        text = URL_RE.sub("[redacted-url]", value)
        text = IP_RE.sub("[redacted-address]", text)
        return text[:MAX_STRING_LENGTH]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_STRING_LENGTH]


def build_diagnostics(
    *,
    app_version: str,
    config: dict,
    system: dict,
    summary: dict,
    miner_statuses: list,
    pool_statuses: list,
    alert_status: dict,
    pool_events: list,
    health_status: dict,
    control_status: dict,
):
    safe_config = {
        "app": config.get("app", {}),
        "miners": config.get("miners", [])[:MAX_DIAGNOSTIC_MINERS],
        "pools": config.get("pools", [])[:MAX_DIAGNOSTIC_POOLS],
        "discord": config.get("discord", {}),
        "odds": config.get("odds", {}),
        "hermes": config.get("hermes", {}),
    }
    alerts = dict(alert_status or {})
    alerts["recent"] = list(alerts.get("recent") or [])[-MAX_DIAGNOSTIC_ALERTS:]
    health = dict(health_status or {})
    health["transitions"] = list(health.get("transitions") or [])[-MAX_DIAGNOSTIC_HEALTH_TRANSITIONS:]
    control = dict(control_status or {})
    control["recent_actions"] = list(control.get("recent_actions") or [])[-MAX_DIAGNOSTIC_CONTROL_ACTIONS:]
    return _sanitize({
        "report": {
            "app": "PoCiSys Hash Monitor",
            "version": app_version,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "privacy": "Sanitized on demand; not stored by PoCiSys",
            "limits": {
                "miners": MAX_DIAGNOSTIC_MINERS,
                "pools": MAX_DIAGNOSTIC_POOLS,
                "alerts": MAX_DIAGNOSTIC_ALERTS,
                "pool_events": MAX_DIAGNOSTIC_POOL_EVENTS,
                "health_transitions": MAX_DIAGNOSTIC_HEALTH_TRANSITIONS,
                "control_actions": MAX_DIAGNOSTIC_CONTROL_ACTIONS,
            },
        },
        "system": system,
        "summary": summary,
        "configuration": safe_config,
        "live": {
            "miners": list(miner_statuses or [])[:MAX_DIAGNOSTIC_MINERS],
            "pools": list(pool_statuses or [])[:MAX_DIAGNOSTIC_POOLS],
        },
        "recent": {
            "alerts": alerts,
            "pool_events": list(pool_events or [])[-MAX_DIAGNOSTIC_POOL_EVENTS:],
            "health": health,
            "control": control,
        },
    })
