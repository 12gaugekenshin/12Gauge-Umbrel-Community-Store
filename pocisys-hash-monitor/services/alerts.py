from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timezone

from .discord import DiscordWebhook


NERDQAXE_OFFLINE_GRACE_SECONDS = 180


def _difficulty_number(value):
    if value is None:
        return None
    text = str(value).strip().upper().replace(",", "")
    factors = {"K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15}
    try:
        if text[-1:] in factors:
            return float(text[:-1]) * factors[text[-1]]
        return float(text)
    except (ValueError, TypeError):
        return None


def format_difficulty(value):
    parsed = _difficulty_number(value)
    if parsed is None:
        return "--"
    for suffix, threshold in (("P", 1e15), ("T", 1e12), ("G", 1e9), ("M", 1e6), ("K", 1e3)):
        if parsed >= threshold:
            compact = f"{parsed / threshold:.2f}".rstrip("0").rstrip(".")
            return f"{compact}{suffix}"
    return f"{parsed:.0f}"


class AlertEngine:
    def __init__(self, config: dict):
        self.config = config
        self.discord = DiscordWebhook(config.get("discord", {}))
        self.cooldown = config.get("app", {}).get("alert_cooldown_seconds", 600)
        self.offline_grace = config.get("app", {}).get("offline_alert_grace_seconds", 60)
        self.last_sent = {}
        self.previous = {}
        self.best_diff = {}
        self.alert_feed = deque(maxlen=25)
        self.discord_last_result = None
        self.snooze_until = 0.0

    def reconfigure(self):
        self.cooldown = self.config.get("app", {}).get("alert_cooldown_seconds", 600)
        self.offline_grace = self.config.get("app", {}).get("offline_alert_grace_seconds", 60)
        self.discord.config = self.config.get("discord", {})

    def dashboard_link(self, path=""):
        app = self.config.get("app", {})
        base = str(app.get("dashboard_base_url") or "").strip().rstrip("/")
        if not base:
            base = f"http://127.0.0.1:{app.get('dashboard_port', 8765)}"
        return f"{base}{path}"

    def _record(self, title, message, severity, source):
        event = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "title": title,
            "message": message,
            "severity": severity,
            "source": source,
        }
        self.alert_feed.appendleft(event)
        return event

    def snooze_status(self):
        remaining = max(0, int(self.snooze_until - time.monotonic() + 0.999)) if self.snooze_until else 0
        if self.snooze_until and not remaining:
            self.snooze_until = 0.0
            self._record(
                "Discord Alerts Resumed",
                "Routine Discord alerts resumed automatically after the snooze timer expired.",
                "success",
                "system",
            )
        return {
            "snoozed": remaining > 0,
            "snooze_remaining_seconds": remaining,
        }

    def snooze(self, seconds):
        duration = int(seconds)
        if duration not in {900, 1800, 3600}:
            raise ValueError("Choose a 15, 30, or 60 minute alert snooze")
        self.snooze_until = time.monotonic() + duration
        self._record(
            "Discord Alerts Snoozed",
            f"Routine Discord alerts snoozed for {duration // 60} minutes. Critical block and local-pool events remain armed.",
            "info",
            "system",
        )
        return self.status()

    def resume(self):
        was_snoozed = self.snooze_status()["snoozed"]
        self.snooze_until = 0.0
        if was_snoozed:
            self._record("Discord Alerts Resumed", "Routine Discord alerts resumed manually.", "success", "system")
        return self.status()

    async def emit(self, key, title, message, severity="warning", source="system", force=False, url=None, bypass_snooze=False):
        now = time.monotonic()
        if not force and now - self.last_sent.get(key, -self.cooldown) < self.cooldown:
            return False
        self.last_sent[key] = now
        self._record(title, message, severity, source)
        if self.snooze_status()["snoozed"] and not bypass_snooze:
            self.discord_last_result = {"sent": False, "reason": "Routine Discord alerts are snoozed"}
            return True
        self.discord_last_result = await self.discord.send(title, message, severity, url=url)
        return True

    async def evaluate_miner(self, status: dict, miner_config: dict):
        discord = self.config.get("discord", {})
        name = status["name"]
        miner_key = status.get("id") or miner_config.get("id") or f"{status.get('type', 'miner')}:{status.get('ip')}"
        miner_url = self.dashboard_link(f"/miners/{miner_key}")
        prior = self.previous.get(miner_key)
        online = bool(status.get("online") and status.get("api_ok"))
        ip_line = f"{name}\nIP: {status['ip']}"
        now = time.monotonic()
        offline_since = None
        offline_alerted = False
        miner_type = str(status.get("type") or miner_config.get("type") or "").strip().lower()
        luxos_control = status.get("luxos_control") or {}
        recovery_managed = bool(miner_type == "luxos" and luxos_control.get("recovery_armed"))
        recovery_noise_suppressed = bool(
            recovery_managed
            and (luxos_control.get("recovery_pending") or luxos_control.get("recovery_observing"))
        )
        chip_state = None
        offline_grace = max(
            self.offline_grace,
            NERDQAXE_OFFLINE_GRACE_SECONDS if miner_type in {"nerdaxe", "nerdqaxe"} else 0,
        )

        if not online:
            offline_since = (
                prior.get("offline_since")
                if prior and not prior.get("online") and prior.get("offline_since") is not None
                else now
            )
            offline_alerted = bool(prior.get("offline_alerted")) if prior else False
            offline_for = max(0, now - offline_since)
            status["offline_for_seconds"] = round(offline_for, 1)
            if offline_for < offline_grace:
                status["warnings"].append(
                    f"Offline grace period ({int(offline_grace - offline_for)}s remaining)"
                )
            elif discord.get("send_offline_alerts", True):
                sent = await self.emit(
                    f"{miner_key}:offline",
                    "Miner Offline",
                    f"{ip_line}\nAPI: failed\nOffline for: {int(offline_for)} seconds",
                    "critical",
                    name,
                    url=miner_url,
                )
                offline_alerted = offline_alerted or sent
        elif (
            prior is not None
            and not prior.get("online")
            and prior.get("offline_alerted")
            and discord.get("send_recovery_alerts", True)
            and not recovery_noise_suppressed
        ):
            await self.emit(
                f"{miner_key}:recovered", "Miner Recovered", f"{ip_line}\nAPI: healthy",
                "success", name, True, miner_url,
            )


        if online:
            hashrate = status.get("hashrate_ths")
            configured_minimum = miner_config.get("min_hashrate_ths")
            device_expected = status.get("expected_hashrate_ths")
            threshold = configured_minimum
            threshold_source = "Configured minimum"
            if threshold is None and device_expected:
                threshold = device_expected * 0.75
                threshold_source = "75% of device-reported expected"
            if hashrate is not None and threshold and hashrate < threshold:
                status["warnings"].append("Hashrate below threshold")
                if discord.get("send_hashrate_alerts", True) and not recovery_noise_suppressed:
                    await self.emit(
                        f"{miner_key}:hashrate",
                        "WARN Hashrate Low",
                        (
                            f"{name}\nCurrent: {hashrate:.3g} TH/s\n"
                            f"Minimum: {threshold:.3g} TH/s\nRule: {threshold_source}"
                        ),
                        "warning", name, url=miner_url,
                    )

            temps = [value for value in status.get("temps", {}).values() if value is not None]
            highest = max(temps, default=None)
            critical = miner_config.get("temp_critical_c")
            warning = miner_config.get("temp_warning_c")
            if highest is not None and critical and highest >= critical:
                status["warnings"].append("Temperature critical")
                if discord.get("send_temperature_alerts", True):
                    await self.emit(
                        f"{miner_key}:temp-critical", "TEMP Temperature Critical",
                        f"{name}\nCurrent: {highest:g} C", "critical", name, url=miner_url,
                    )
            elif highest is not None and warning and highest >= warning:
                status["warnings"].append("Temperature warning")
                if discord.get("send_temperature_alerts", True):
                    await self.emit(
                        f"{miner_key}:temp-warning", "TEMP Temperature Warning",
                        f"{name}\nCurrent: {highest:g} C", "warning", name, url=miner_url,
                    )

            chip_health = status.get("chip_health") or {}
            chip_items = chip_health.get("items") or []
            chip_health_reported = bool(chip_health.get("reported") and chip_items)
            item_states = {str(item.get("status") or "unknown").lower() for item in chip_items}
            if chip_health_reported and "warning" in item_states:
                chip_state = "degraded"
            elif chip_health_reported and item_states and item_states == {"healthy"}:
                chip_state = "healthy"
            if chip_state == "degraded":
                status["warnings"].append("LuxOS chip health degraded")
            previous_chip_degraded = prior.get("chip_health_degraded") if prior else False
            if (
                miner_type == "luxos"
                and chip_state == "degraded"
                and not previous_chip_degraded
                and not recovery_managed
                and discord.get("send_chip_health_alerts", True)
            ):
                affected = []
                for item in chip_items:
                    if str(item.get("status") or "").lower() == "healthy":
                        continue
                    healthy = item.get("chips_healthy")
                    total = item.get("chips_total")
                    count = f" ({healthy}/{total} ASICs)" if healthy is not None and total is not None else ""
                    affected.append(f"{item.get('name') or 'Hashboard'}: {item.get('status') or 'warning'}{count}")
                detail = "\n".join(affected[:8]) or "LuxOS reported fewer healthy hashboards than expected."
                await self.emit(
                    f"{miner_key}:chip-health",
                    "WARN LuxOS Chip Health Low",
                    f"{name}\n{detail}\nA board restart may be required if the fault persists.",
                    "warning", name, True, miner_url,
                )
            elif (
                miner_type == "luxos"
                and chip_state == "healthy"
                and previous_chip_degraded
                and not recovery_managed
                and discord.get("send_chip_health_alerts", True)
                and discord.get("send_recovery_alerts", True)
            ):
                await self.emit(
                    f"{miner_key}:chip-health-recovered",
                    "LuxOS Chip Health Recovered",
                    f"{name}\nAll {chip_health.get('total')} reported hashboards are healthy.",
                    "success", name, True, miner_url,
                )

            pool = status.get("pool", {})
            pool_identity = (pool.get("url"), pool.get("source"))
            if pool.get("connected") is False:
                status["warnings"].append("Pool disconnected")
                if discord.get("send_pool_alerts", True) and not recovery_noise_suppressed:
                    await self.emit(
                        f"{miner_key}:pool", "ALERT Pool Disconnected",
                        f"{name}\nPool: {pool.get('url') or 'unknown'}",
                        "critical", name, url=miner_url,
                    )
            if (
                prior
                and prior.get("pool_identity", (None, None))[0]
                and pool_identity[0]
                and pool_identity != prior.get("pool_identity")
                and discord.get("send_pool_switch_alerts", True)
                and not recovery_noise_suppressed
            ):
                old_pool = prior["pool_identity"][0]
                await self.emit(
                    f"{miner_key}:pool-switch",
                    "POOL Miner Pool Switched",
                    f"{name}\nFrom: {old_pool}\nTo: {pool_identity[0]}",
                    "warning", name, True, miner_url,
                )

            shares = status.get("shares", {})
            if prior:
                old_shares = prior.get("shares", {})
                increase = sum(
                    max(0, shares.get(key, 0) - old_shares.get(key, 0))
                    for key in ("invalid", "stale", "rejected")
                )
                if increase and discord.get("send_share_alerts", True) and not recovery_noise_suppressed:
                    await self.emit(
                        f"{miner_key}:shares", "WARN Bad Shares Increased",
                        f"{name}\nNew invalid/stale/rejected: {increase}",
                        "warning", name, url=miner_url,
                    )

            blocks_found = int(status.get("blocks_found") or 0)
            previous_blocks = int(prior.get("blocks_found") or 0) if prior else blocks_found
            if blocks_found > previous_blocks and discord.get("send_block_found_alerts", True):
                await self.emit(
                    f"{miner_key}:block-found",
                    "BLOCK Block Found",
                    f"{name}\nMiner block count: {blocks_found}\nCheck the miner and pool immediately.",
                    "critical", name, True, miner_url, True,
                )

            best = status.get("difficulty", {}).get("best_all_time") or status.get("difficulty", {}).get("best_session")
            parsed_best = _difficulty_number(best)
            old_best = self.best_diff.get(miner_key)
            if parsed_best is not None:
                if old_best is not None and parsed_best > old_best and discord.get("send_best_diff_alerts", True):
                    await self.emit(
                        f"{miner_key}:best", "BEST New Best Difficulty",
                        f"{name}\nBest difficulty: {format_difficulty(best)}",
                        "info", name, True, miner_url,
                    )
                self.best_diff[miner_key] = max(parsed_best, old_best or 0)

        self.previous[miner_key] = {
            "online": online,
            "shares": dict(status.get("shares", {})),
            "pool_identity": (
                status.get("pool", {}).get("url"),
                status.get("pool", {}).get("source"),
            ),
            "blocks_found": int(status.get("blocks_found") or 0),
            "offline_since": offline_since,
            "offline_alerted": offline_alerted,
            # Unknown LuxOS chip states are transitional, not a recovery. Keep
            # the last known state until LuxOS reports a known result.
            "chip_health_degraded": (
                chip_state == "degraded"
                if chip_state in {"degraded", "healthy"}
                else bool(prior.get("chip_health_degraded")) if prior else False
            ),
        }

    async def pool_event(self, event: dict):
        discord = self.config.get("discord", {})
        critical_pool_event = event.get("category") in {"block", "rpc", "security"} or event.get("severity") == "critical"
        should_send = critical_pool_event or (
            event.get("category") == "activity" and discord.get("verbose_pool_events", False)
        )
        if should_send and discord.get("send_pool_alerts", True):
            await self.emit(
                f"pool:{event['pool']}:{event['category']}",
                event["title"],
                f"{event['pool']}\n{event['line'][:500]}",
                event.get("severity", "warning"),
                event["pool"],
                force=event.get("category") == "block",
                url=self.dashboard_link("/pools"),
                bypass_snooze=critical_pool_event,
            )

    async def test_discord(self):
        result = await self.discord.send(
            "TEST Test Alert",
            "PoCiSys Hash Monitor webhook is working.",
            "success",
            url=self.dashboard_link(),
            require_enabled=False,
        )
        self.discord_last_result = result
        self._record("Discord Test", result.get("reason", "Webhook request completed."), "info", "system")
        return result

    def clear_recent(self):
        self.alert_feed.clear()
        return self.status()

    def status(self):
        discord = self.config.get("discord", {})
        snooze = self.snooze_status()
        return {
            "discord_enabled": bool(discord.get("enabled")),
            "discord_configured": bool(discord.get("webhook_url")),
            "last_discord_result": self.discord_last_result,
            "recent": list(self.alert_feed),
            **snooze,
        }
