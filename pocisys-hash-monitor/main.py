from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import shutil
import threading
import urllib.parse
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from miners import get_driver
from services.alerts import AlertEngine
from services.config_store import apply_in_place, load_config, make_id, public_config, save_config
from services.hermes_mcp import (
    HermesMcpService,
    create_connection_token,
    sanitize_miner,
    sanitize_pool,
    token_digest,
    token_matches,
)
from services.health import HealthEngine
from services.network_data import NetworkDataService
from services.odds import calculate_odds
from services.poller import MinerPoller
from services.pool_logs import PoolLogService, probe_public_pool
from services.pool_probe import PoolConnectionProbe, PoolProbeCooldown
from services.system_stats import SystemStatsService
from services.luxos_control import LuxOSControlError, LuxOSControlService
from services.validation import ApiError, as_float, as_int, clean_host, clean_miner, clean_pool


APP_VERSION = "1.8.2"
ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
CONFIG_PATH = Path(os.environ.get("POCISYS_CONFIG_PATH", ROOT / "config.json")).resolve()
DEFAULT_CONFIG_PATH = ROOT / "config.default.json"
MAX_REQUEST_BYTES = 1024 * 1024


if not CONFIG_PATH.exists():
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(DEFAULT_CONFIG_PATH, CONFIG_PATH)

config = load_config(CONFIG_PATH)
alerts = AlertEngine(config)
health_engine = HealthEngine()
luxos_control = LuxOSControlService(config, alerts)
network_data = NetworkDataService()
poller = MinerPoller(config, alerts, luxos_control.enrich_status, health_engine)
pool_logs = PoolLogService(
    config.get("pools", []),
    alerts,
    poller.statuses,
    CONFIG_PATH.parent / "accepted-shares.json",
    network_data.snapshot,
)
pool_probe = PoolConnectionProbe()
system_stats = SystemStatsService()
config_lock = asyncio.Lock()


def commit_config(updated):
    save_config(CONFIG_PATH, updated)
    apply_in_place(config, updated)
    try:
        alerts.reconfigure()
        luxos_control.reconfigure()
        pool_logs.reconfigure(config["pools"])
        poller.reconfigure()
        health_engine.reconfigure(config["miners"])
    except Exception as exc:
        print(f"PoCiSys warning: saved config but service reconfigure failed: {exc}", flush=True)
    print(
        f"PoCiSys config saved: miners={len(config.get('miners', []))} pools={len(config.get('pools', []))}",
        flush=True,
    )


def summary():
    statuses = poller.statuses()
    online = [item for item in statuses if item.get("online") and item.get("api_ok")]
    temperatures = [
        temp
        for item in statuses
        for temp in item.get("temps", {}).values()
        if temp is not None
    ]
    pings = [item["ping_ms"] for item in online if item.get("ping_ms") is not None]
    enabled_count = sum(1 for item in config.get("miners", []) if item.get("enabled", True))
    return {
        "total_hashrate_ths": sum(float(item.get("hashrate_ths") or 0) for item in online),
        "online_miners": len(online),
        "total_miners": enabled_count,
        "configured_miners": len(config.get("miners", [])),
        "average_ping_ms": round(sum(pings) / len(pings), 1) if pings else None,
        "highest_temperature_c": max(temperatures, default=None),
        "total_valid_shares": sum(item.get("shares", {}).get("valid", 0) for item in statuses),
        "total_bad_shares": sum(
            item.get("shares", {}).get(key, 0)
            for item in statuses
            for key in ("invalid", "stale", "rejected")
        ),
        "last_poll": poller.last_poll,
    }


def current_settings():
    app_config = config.get("app", {})
    discord = config.get("discord", {})
    hermes = config.get("hermes", {})
    odds = config.get("odds", {})
    return {
        "poll_interval_seconds": app_config.get("poll_interval_seconds", 10),
        "dashboard_port": app_config.get("dashboard_port", 8765),
        "alert_cooldown_seconds": app_config.get("alert_cooldown_seconds", 600),
        "offline_alert_grace_seconds": app_config.get("offline_alert_grace_seconds", 60),
        "request_timeout_seconds": app_config.get("request_timeout_seconds", 4),
        "dashboard_density": app_config.get("dashboard_density", "comfortable"),
        "difficulty_rain_enabled": app_config.get("difficulty_rain_enabled", True),
        "dashboard_base_url": app_config.get("dashboard_base_url", ""),
        "lan_access_enabled": app_config.get("lan_access_enabled", False),
        "luxos_control_enabled": app_config.get("luxos_control_enabled", False),
        "control_timezone": app_config.get("control_timezone", "auto"),
        "control_utc_offset_minutes": app_config.get("control_utc_offset_minutes", 0),
        "discord_enabled": discord.get("enabled", False),
        "webhook_configured": bool(discord.get("webhook_url")),
        "send_offline_alerts": discord.get("send_offline_alerts", True),
        "send_recovery_alerts": discord.get("send_recovery_alerts", True),
        "send_hashrate_alerts": discord.get("send_hashrate_alerts", True),
        "send_temperature_alerts": discord.get("send_temperature_alerts", True),
        "send_chip_health_alerts": discord.get("send_chip_health_alerts", True),
        "send_control_alerts": discord.get("send_control_alerts", True),
        "send_best_diff_alerts": discord.get("send_best_diff_alerts", True),
        "send_block_found_alerts": discord.get("send_block_found_alerts", True),
        "send_pool_alerts": discord.get("send_pool_alerts", True),
        "send_pool_switch_alerts": discord.get("send_pool_switch_alerts", True),
        "send_share_alerts": discord.get("send_share_alerts", True),
        "verbose_pool_events": discord.get("verbose_pool_events", False),
        "hermes_enabled": hermes.get("enabled", False),
        "hermes_token_configured": bool(hermes.get("token_hash")),
        "hermes_token_hint": hermes.get("token_hint", ""),
        "hermes_mcp_path": "/mcp",
        "btc_enabled": odds.get("btc_enabled", True),
        "bch_enabled": odds.get("bch_enabled", True),
        "bsv_enabled": odds.get("bsv_enabled", True),
        "xec_enabled": odds.get("xec_enabled", True),
        "dgb_enabled": odds.get("dgb_enabled", True),
        "chta_enabled": odds.get("chta_enabled", True),
        "auto_network_data": odds.get("auto_network_data", True),
        "manual_btc_network_hashrate_eh": odds.get("manual_btc_network_hashrate_eh"),
        "manual_bch_network_hashrate_eh": odds.get("manual_bch_network_hashrate_eh"),
        "manual_bsv_network_hashrate_eh": odds.get("manual_bsv_network_hashrate_eh"),
        "manual_xec_network_hashrate_eh": odds.get("manual_xec_network_hashrate_eh"),
        "manual_dgb_network_hashrate_eh": odds.get("manual_dgb_network_hashrate_eh"),
        "manual_chta_network_hashrate_eh": odds.get("manual_chta_network_hashrate_eh"),
    }


def pool_statuses():
    try:
        return pool_logs.status()
    except Exception as exc:
        print(f"PoCiSys warning: pool status failed for Hermes: {exc}", flush=True)
        return []


def hermes_miners():
    return [sanitize_miner(item) for item in poller.statuses()]


def hermes_pools():
    return [sanitize_pool(item) for item in pool_statuses()]


def odds_status():
    return calculate_odds(poller.statuses(), config, network_data.snapshot())


def hermes_overview():
    miners = hermes_miners()
    pools = hermes_pools()
    return {
        "app": {
            "name": "PoCiSys Hash Monitor",
            "version": APP_VERSION,
            "health": "ok",
            "last_poll": poller.last_poll,
        },
        "system": system_stats.snapshot(),
        "summary": summary(),
        "miners": [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "group": item.get("group"),
                "mining_target": item.get("mining_target"),
                "online": item.get("online"),
                "api_ok": item.get("api_ok"),
                "status": item.get("status"),
                "hashrate_ths": item.get("hashrate_ths"),
                "highest_temperature_c": max(
                    (value for value in item.get("temps", {}).values() if value is not None),
                    default=None,
                ),
                "warnings": item.get("warnings", []),
            }
            for item in miners
        ],
        "pools": pools,
    }


hermes_mcp = HermesMcpService(
    version=APP_VERSION,
    overview_provider=hermes_overview,
    miners_provider=hermes_miners,
    pools_provider=hermes_pools,
    odds_provider=odds_status,
    system_provider=system_stats.snapshot,
)


async def discover_public_pool(host=None):
    hosts = []
    if host:
        hosts.append(clean_host(host))
    for status in poller.statuses():
        pool_url = str(status.get("pool", {}).get("url") or "")
        parsed = urllib.parse.urlparse(pool_url if "://" in pool_url else f"stratum://{pool_url}")
        if parsed.hostname and parsed.hostname not in hosts:
            hosts.append(parsed.hostname)
    attempts = []
    for candidate_host in hosts[:20]:
        for port in (2020, 2019, 40557):
            candidate = f"http://{candidate_host}:{port}"
            attempts.append(candidate)
            try:
                api_url, response = await asyncio.wait_for(
                    asyncio.to_thread(probe_public_pool, candidate, 1.5),
                    timeout=2.0,
                )
                return {
                    "ok": True,
                    "api_url": api_url,
                    "host": candidate_host,
                    "total_miners": response.get("totalMiners"),
                    "block_height": response.get("blockHeight"),
                }
            except (asyncio.TimeoutError, OSError, ValueError):
                continue
    return {"ok": False, "error": "Local Public Pool API not found", "attempted": attempts}


async def api_dispatch(method, path, data):
    parts = [urllib.parse.unquote(item) for item in path.strip("/").split("/") if item]
    statuses = [luxos_control.enrich_status(item) for item in poller.statuses()]

    if method == "GET" and path == "/health":
        return {"ok": True, "version": APP_VERSION}
    if method == "GET" and path == "/api/system-health":
        return {"system": system_stats.snapshot()}
    if method == "GET" and path == "/api/status":
        try:
            pool_statuses = pool_logs.status()
        except Exception as exc:
            print(f"PoCiSys warning: pool status failed during /api/status: {exc}", flush=True)
            pool_statuses = []
        return {
            "summary": summary(),
            "miners": statuses,
            "odds": calculate_odds(statuses, config, network_data.snapshot()),
            "discord": alerts.status(),
            "pools": pool_statuses,
            "pool_event_count": len(pool_logs.events),
            "control": luxos_control.status(),
            "health": health_engine.status(),
            "ui": {
                "dashboard_density": config.get("app", {}).get("dashboard_density", "comfortable"),
                "difficulty_rain_enabled": config.get("app", {}).get("difficulty_rain_enabled", True),
                "luxos_control_enabled": config.get("app", {}).get("luxos_control_enabled", False),
            },
        }
    if method == "GET" and path == "/api/miners":
        current = {item.get("id"): item for item in statuses}
        return {
            "miners": [
                {"config": deepcopy(miner), "status": current.get(miner.get("id"))}
                for miner in sorted(config.get("miners", []), key=lambda item: item.get("display_order", 0))
            ]
        }
    if method == "POST" and path == "/api/miners/test":
        candidate = clean_miner({
            "name": "Connection test",
            "ip": data.get("ip"),
            "type": data.get("type"),
            "group": "Test",
        })
        timeout = config.get("app", {}).get("request_timeout_seconds", 4)
        try:
            status = await asyncio.to_thread(get_driver(candidate, timeout).poll)
            status.pop("raw", None)
            return {"ok": bool(status.get("api_ok")), "status": status}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    if method == "POST" and path == "/api/miners":
        async with config_lock:
            updated = deepcopy(config)
            miner = clean_miner(data)
            existing_index = next(
                (
                    index for index, item in enumerate(updated["miners"])
                    if item.get("ip") == miner["ip"] and item.get("type") == miner["type"]
                ),
                None,
            )
            if existing_index is not None:
                miner["id"] = updated["miners"][existing_index].get("id") or make_id("miner")
                if not miner["display_order"]:
                    miner["display_order"] = updated["miners"][existing_index].get("display_order") or existing_index + 1
                updated["miners"][existing_index] = miner
            else:
                miner["id"] = make_id("miner")
                if not miner["display_order"]:
                    miner["display_order"] = len(updated["miners"]) + 1
                updated["miners"].append(miner)
            commit_config(updated)
        return {"ok": True, "miner": miner}
    if method == "POST" and path == "/api/miners/reorder":
        ids = data.get("ids") if isinstance(data.get("ids"), list) else []
        async with config_lock:
            updated = deepcopy(config)
            current_ids = {item["id"] for item in updated["miners"]}
            if len(ids) != len(current_ids) or set(ids) != current_ids:
                raise ApiError(400, "Reorder list must contain every configured miner exactly once")
            order = {miner_id: index + 1 for index, miner_id in enumerate(ids)}
            for miner in updated["miners"]:
                miner["display_order"] = order[miner["id"]]
            updated["miners"].sort(key=lambda item: item["display_order"])
            commit_config(updated)
        return {"ok": True}
    if len(parts) == 3 and parts[:2] == ["api", "miners"]:
        miner_id = parts[2]
        existing = next((item for item in config.get("miners", []) if item.get("id") == miner_id), None)
        if not existing:
            raise ApiError(404, "Miner not found")
        if method == "GET":
            status = next((item for item in statuses if item.get("id") == miner_id), None)
            return {"config": deepcopy(existing), "status": status}
        if method == "PUT":
            async with config_lock:
                updated = deepcopy(config)
                index = next(i for i, item in enumerate(updated["miners"]) if item.get("id") == miner_id)
                miner = clean_miner(data)
                miner["id"] = miner_id
                updated["miners"][index] = miner
                commit_config(updated)
            return {"ok": True, "miner": miner}
        if method == "DELETE":
            async with config_lock:
                updated = deepcopy(config)
                updated["miners"] = [item for item in updated["miners"] if item.get("id") != miner_id]
                for index, miner in enumerate(updated["miners"], 1):
                    miner["display_order"] = index
                commit_config(updated)
            return {"ok": True}

    if method == "GET" and path == "/api/control":
        return luxos_control.status()
    if len(parts) == 4 and parts[:2] == ["api", "miners"] and parts[3] == "luxos-profiles":
        miner_id = parts[2]
        if method != "GET":
            raise ApiError(405, "Method not allowed")
        try:
            return await luxos_control.list_profiles(miner_id, force=True)
        except LuxOSControlError as exc:
            raise ApiError(409, str(exc))
    if len(parts) == 4 and parts[:2] == ["api", "miners"] and parts[3] == "control":
        miner_id = parts[2]
        if method == "GET":
            return luxos_control.miner_status(miner_id)
        if method == "POST":
            try:
                return await luxos_control.execute(
                    miner_id,
                    str(data.get("action") or ""),
                    board_id=data.get("board_id"),
                    source="manual dashboard action",
                )
            except (LuxOSControlError, TypeError, ValueError) as exc:
                raise ApiError(409, str(exc))

    if method == "GET" and path == "/api/pools":
        try:
            live = {item["name"]: item for item in pool_logs.status()}
        except Exception as exc:
            print(f"PoCiSys warning: pool status failed during /api/pools: {exc}", flush=True)
            live = {}
        return {
            "pools": [
                {"config": deepcopy(pool), "status": live.get(pool.get("name"))}
                for pool in config.get("pools", [])
            ]
        }
    if method == "POST" and path == "/api/pools/discover":
        return await discover_public_pool(data.get("host"))
    if method == "POST" and path == "/api/pools":
        async with config_lock:
            updated = deepcopy(config)
            pool = clean_pool(data)
            existing_index = next(
                (
                    index for index, item in enumerate(updated["pools"])
                    if item.get("mode") == pool["mode"]
                    and (
                        item.get("api_url") == pool.get("api_url")
                        or item.get("log_path") == pool.get("log_path")
                        or item.get("name") == pool.get("name")
                    )
                ),
                None,
            )
            if existing_index is not None:
                pool["id"] = updated["pools"][existing_index].get("id") or make_id("pool")
                updated["pools"][existing_index] = pool
            else:
                pool["id"] = make_id("pool")
                updated["pools"].append(pool)
            commit_config(updated)
        return {"ok": True, "pool": pool}
    if len(parts) == 3 and parts[:2] == ["api", "pools"]:
        pool_id = parts[2]
        existing = next((item for item in config.get("pools", []) if item.get("id") == pool_id), None)
        if not existing:
            raise ApiError(404, "Pool not found")
        if method == "PUT":
            async with config_lock:
                updated = deepcopy(config)
                index = next(i for i, item in enumerate(updated["pools"]) if item.get("id") == pool_id)
                pool = clean_pool(data)
                pool["id"] = pool_id
                updated["pools"][index] = pool
                commit_config(updated)
            return {"ok": True, "pool": pool}
        if method == "DELETE":
            async with config_lock:
                updated = deepcopy(config)
                updated["pools"] = [item for item in updated["pools"] if item.get("id") != pool_id]
                commit_config(updated)
            return {"ok": True}
    if method == "GET" and path == "/api/pool-events":
        return {"events": list(pool_logs.events)}
    if method == "GET" and path == "/api/health-transitions":
        return health_engine.status()
    if method == "GET" and path == "/api/pool-connection-test":
        return pool_probe.snapshot()
    if method == "POST" and path == "/api/pool-connection-test":
        try:
            return await pool_probe.test(data.get("target"))
        except PoolProbeCooldown as exc:
            raise ApiError(429, f"Wait {exc.remaining_seconds} seconds before testing another pool")
        except ValueError as exc:
            raise ApiError(400, str(exc))
    if method == "GET" and path == "/api/odds":
        return calculate_odds(statuses, config, network_data.snapshot())
    if method == "GET" and path == "/api/network-data":
        return network_data.snapshot()
    if method == "GET" and path == "/api/config":
        return public_config(config)
    if method == "GET" and path == "/api/settings":
        return current_settings()
    if method == "POST" and path == "/api/hermes/token":
        async with config_lock:
            token = create_connection_token()
            updated = deepcopy(config)
            updated.setdefault("hermes", {})
            updated["hermes"]["token_hash"] = token_digest(token)
            updated["hermes"]["token_hint"] = token[-6:]
            commit_config(updated)
        return {
            "ok": True,
            "token": token,
            "token_hint": token[-6:],
            "message": "Copy this token now. PoCiSys stores only its hash and cannot reveal it again.",
        }
    if method == "DELETE" and path == "/api/hermes/token":
        async with config_lock:
            updated = deepcopy(config)
            updated.setdefault("hermes", {})
            updated["hermes"]["enabled"] = False
            updated["hermes"]["token_hash"] = ""
            updated["hermes"]["token_hint"] = ""
            commit_config(updated)
        return {"ok": True, "settings": current_settings()}
    if method == "PUT" and path == "/api/settings":
        async with config_lock:
            updated = deepcopy(config)
            app_config = updated["app"]
            old_port = app_config.get("dashboard_port", 8765)
            old_lan = app_config.get("lan_access_enabled", False)
            density = str(data.get("dashboard_density") or "comfortable")
            if density not in {"comfortable", "compact"}:
                raise ApiError(400, "Unsupported dashboard density")
            dashboard_url = str(data.get("dashboard_base_url") or "").strip().rstrip("/")
            if dashboard_url and not dashboard_url.startswith(("http://", "https://")):
                raise ApiError(400, "Dashboard link must begin with http:// or https://")
            control_timezone = str(data.get("control_timezone") or "auto").strip()[:80]
            if not re.fullmatch(r"[A-Za-z0-9_+./-]+", control_timezone):
                raise ApiError(400, "Invalid control schedule timezone")
            app_config.update(
                poll_interval_seconds=max(2, min(3600, as_int(data.get("poll_interval_seconds"), 10))),
                dashboard_port=max(1024, min(65535, as_int(data.get("dashboard_port"), 8765))),
                alert_cooldown_seconds=max(0, min(86400, as_int(data.get("alert_cooldown_seconds"), 600))),
                offline_alert_grace_seconds=max(0, min(3600, as_int(data.get("offline_alert_grace_seconds"), 60))),
                request_timeout_seconds=max(0.5, min(30, as_float(data.get("request_timeout_seconds"), 4))),
                dashboard_density=density,
                difficulty_rain_enabled=bool(data.get("difficulty_rain_enabled", True)),
                dashboard_base_url=dashboard_url,
                lan_access_enabled=bool(data.get("lan_access_enabled", False)),
                luxos_control_enabled=bool(data.get("luxos_control_enabled", False)),
                control_timezone=control_timezone,
                control_utc_offset_minutes=max(-840, min(840, as_int(data.get("control_utc_offset_minutes"), 0))),
            )
            discord = updated["discord"]
            webhook = str(data.get("webhook_url") or "").strip()
            allowed = (
                "https://discord.com/api/webhooks/",
                "https://discordapp.com/api/webhooks/",
                "https://canary.discord.com/api/webhooks/",
            )
            if webhook and not webhook.startswith(allowed):
                raise ApiError(400, "Enter a Discord webhook URL")
            discord.update(
                enabled=bool(data.get("discord_enabled", False)),
                send_offline_alerts=bool(data.get("send_offline_alerts", True)),
                send_recovery_alerts=bool(data.get("send_recovery_alerts", True)),
                send_hashrate_alerts=bool(data.get("send_hashrate_alerts", True)),
                send_temperature_alerts=bool(data.get("send_temperature_alerts", True)),
                send_chip_health_alerts=bool(data.get("send_chip_health_alerts", True)),
                send_control_alerts=bool(data.get("send_control_alerts", True)),
                send_best_diff_alerts=bool(data.get("send_best_diff_alerts", True)),
                send_block_found_alerts=bool(data.get("send_block_found_alerts", True)),
                send_pool_alerts=bool(data.get("send_pool_alerts", True)),
                send_pool_switch_alerts=bool(data.get("send_pool_switch_alerts", True)),
                send_share_alerts=bool(data.get("send_share_alerts", True)),
                verbose_pool_events=bool(data.get("verbose_pool_events", False)),
            )
            hermes = updated.setdefault("hermes", {})
            hermes["enabled"] = bool(data.get("hermes_enabled", False))
            if hermes["enabled"] and not hermes.get("token_hash"):
                raise ApiError(400, "Generate a Hermes connection token before enabling AI access")
            if webhook:
                discord["webhook_url"] = webhook
            elif data.get("clear_webhook"):
                discord["webhook_url"] = ""
            if discord["enabled"] and not discord.get("webhook_url"):
                raise ApiError(400, "Add a Discord webhook URL before enabling Discord")
            updated["odds"].update(
                btc_enabled=bool(data.get("btc_enabled", True)),
                bch_enabled=bool(data.get("bch_enabled", True)),
                bsv_enabled=bool(data.get("bsv_enabled", True)),
                xec_enabled=bool(data.get("xec_enabled", True)),
                dgb_enabled=bool(data.get("dgb_enabled", True)),
                chta_enabled=bool(data.get("chta_enabled", True)),
                auto_network_data=bool(data.get("auto_network_data", True)),
                manual_btc_network_hashrate_eh=as_float(data.get("manual_btc_network_hashrate_eh"), None),
                manual_bch_network_hashrate_eh=as_float(data.get("manual_bch_network_hashrate_eh"), None),
                manual_bsv_network_hashrate_eh=as_float(data.get("manual_bsv_network_hashrate_eh"), None),
                manual_xec_network_hashrate_eh=as_float(data.get("manual_xec_network_hashrate_eh"), None),
                manual_dgb_network_hashrate_eh=as_float(data.get("manual_dgb_network_hashrate_eh"), None),
                manual_chta_network_hashrate_eh=as_float(data.get("manual_chta_network_hashrate_eh"), None),
            )
            commit_config(updated)
        return {
            "ok": True,
            "restart_required": old_port != app_config["dashboard_port"] or old_lan != app_config["lan_access_enabled"],
            "settings": current_settings(),
        }
    if method == "GET" and path == "/api/alerts":
        return alerts.status()
    if method == "POST" and path == "/api/alerts/clear":
        return alerts.clear_recent()
    if method == "POST" and path == "/api/alerts/snooze":
        try:
            return alerts.snooze(as_int(data.get("seconds"), 0))
        except ValueError as exc:
            raise ApiError(400, str(exc))
    if method == "POST" and path == "/api/alerts/resume":
        return alerts.resume()
    if method == "POST" and path == "/api/test-discord":
        return await alerts.test_discord()
    if method == "POST" and path == "/api/poll-now":
        await poller.poll_now()
        return {"ok": True, "summary": summary()}
    raise ApiError(404, "API route not found")


event_loop = asyncio.new_event_loop()
loop_started = threading.Event()


def run_event_loop():
    asyncio.set_event_loop(event_loop)

    async def boot():
        poller.start()
        luxos_control.start()
        pool_logs.start()
        network_data.start()

    event_loop.run_until_complete(boot())
    loop_started.set()
    event_loop.run_forever()


def run_api(method, path, data=None):
    future = asyncio.run_coroutine_threadsafe(api_dispatch(method, path, data or {}), event_loop)
    return future.result(timeout=90)


class PoCiSysHandler(BaseHTTPRequestHandler):
    server_version = f"PoCiSys/{APP_VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        return

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def send_empty(self, status=202):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ApiError(400, "Invalid request length")
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ApiError(413, "Request body is too large")
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "Request body must be valid JSON")
        if not isinstance(value, dict):
            raise ApiError(400, "Request body must be a JSON object")
        return value

    def serve_frontend(self, path):
        if path.startswith("/static/"):
            relative = path[len("/static/"):]
            candidate = (WEB_ROOT / relative).resolve()
            if not str(candidate).startswith(str(WEB_ROOT.resolve())) or not candidate.is_file():
                self.send_error(404)
                return
        else:
            candidate = WEB_ROOT / "index.html"
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if candidate.name == "index.html":
            html = candidate.read_text(encoding="utf-8")
            try:
                css = (WEB_ROOT / "style.css").read_text(encoding="utf-8")
                js = (WEB_ROOT / "dashboard.js").read_text(encoding="utf-8")
                html = html.replace(
                    '<link rel="stylesheet" href="/static/style.css">',
                    f"<style>\n{css}\n</style>",
                )
                html = html.replace(
                    '<script src="/static/dashboard.js"></script>',
                    f"<script>\n{js}\n</script>",
                )
            except OSError as exc:
                print(f"PoCiSys warning: inline asset injection failed: {exc}", flush=True)
            body = html.encode("utf-8")
        else:
            body = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def dispatch(self, method):
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        try:
            if path.startswith("/api/") or path in {"/health", "/mcp"}:
                print(f"PoCiSys request: {method} {path}", flush=True)
            if path == "/mcp":
                if method != "POST":
                    raise ApiError(405, "The PoCiSys MCP endpoint accepts POST requests")
                origin = str(self.headers.get("Origin") or "").strip()
                if origin:
                    origin_host = urllib.parse.urlparse(origin).hostname
                    request_host = urllib.parse.urlparse(f"//{self.headers.get('Host', '')}").hostname
                    if not origin_host or not request_host or origin_host.casefold() != request_host.casefold():
                        raise ApiError(403, "Cross-origin MCP requests are not allowed")
                hermes_config = config.get("hermes", {})
                if not hermes_config.get("enabled"):
                    raise ApiError(503, "Hermes access is disabled in PoCiSys settings")
                authorization = str(self.headers.get("Authorization") or "")
                scheme, _, bearer = authorization.partition(" ")
                if scheme.lower() != "bearer" or not token_matches(bearer.strip(), hermes_config.get("token_hash", "")):
                    raise ApiError(401, "Valid PoCiSys bearer token required")
                response = hermes_mcp.handle(self.read_json())
                if response is None:
                    self.send_empty(202)
                else:
                    self.send_json(response)
                return
            if not path.startswith("/api/") and path != "/health":
                if method != "GET":
                    raise ApiError(405, "Method not allowed")
                self.serve_frontend(path)
                return
            data = self.read_json() if method in {"POST", "PUT"} else {}
            self.send_json(run_api(method, path, data))
        except ApiError as exc:
            self.send_json({"detail": exc.message}, exc.status)
        except TimeoutError:
            self.send_json({"detail": "Request timed out"}, 504)
        except Exception as exc:
            print(f"PoCiSys request error: {method} {path}: {exc}", flush=True)
            self.send_json({"detail": f"Internal error: {exc}"}, 500)

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")


class PoCiSysServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def shutdown_services():
    async def stop():
        await poller.stop()
        await luxos_control.stop()
        await pool_logs.stop()
        await network_data.stop()

    try:
        asyncio.run_coroutine_threadsafe(stop(), event_loop).result(timeout=10)
    finally:
        system_stats.stop()
        event_loop.call_soon_threadsafe(event_loop.stop)


if __name__ == "__main__":
    print(f"PoCiSys Hash Monitor {APP_VERSION} starting", flush=True)
    print(f"Config path: {CONFIG_PATH}", flush=True)
    thread = threading.Thread(target=run_event_loop, name="pocisys-services", daemon=True)
    thread.start()
    system_stats.start()
    loop_started.wait(10)
    host = os.environ.get("POCISYS_HOST", "0.0.0.0")
    port = int(os.environ.get("POCISYS_PORT", "8765"))
    server = PoCiSysServer((host, port), PoCiSysHandler)
    print(f"PoCiSys Hash Monitor ready on {host}:{port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        shutdown_services()
