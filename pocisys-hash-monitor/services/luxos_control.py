from __future__ import annotations

import asyncio
import json
import socket
import time
from collections import deque
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
HEALTH_INTERVAL_SECONDS = 60
CONTROL_LOOP_SECONDS = 30
CONTROL_ACTION_COOLDOWN_SECONDS = 15
SCHEDULE_RETRY_SECONDS = 300
AUTO_RECOVERY_WARMUP_SECONDS = 300
AUTO_RECOVERY_CONFIRMATIONS = 3
AUTO_RECOVERY_RESTART_COOLDOWN_SECONDS = 6 * 60 * 60
AUTO_RECOVERY_POST_RESTART_SECONDS = 10 * 60
PROFILE_CHANGE_OBSERVATION_SECONDS = 10 * 60
MAX_HASHBOARDS = 8
MAX_PROFILES = 64
MAX_LOW_CHIPS_PER_BOARD = 8


class LuxOSControlError(RuntimeError):
    pass


def _number(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(str(value).replace(",", "").split()[0])
    except (TypeError, ValueError):
        return default


def _integer(value, default=None):
    parsed = _number(value)
    return int(parsed) if parsed is not None else default


def _first_row(payload, key):
    rows = payload.get(key, []) if isinstance(payload, dict) else []
    return rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}


def _status_message(payload):
    row = _first_row(payload, "STATUS")
    return str(row.get("Msg") or row.get("Description") or "LuxOS rejected the command")


def _command_succeeded(payload):
    statuses = payload.get("STATUS", []) if isinstance(payload, dict) else []
    return any(str(row.get("STATUS") or "").upper() == "S" for row in statuses if isinstance(row, dict))


class LuxOSClient:
    """Small bounded LuxOS TCP client.

    Responses exist only while one command is normalized. The client never
    stores raw LuxOS payloads or long-running telemetry.
    """

    def __init__(self, ip: str, timeout: float = 5.0):
        self.ip = ip
        self.timeout = max(0.5, min(float(timeout), 30.0))

    def call(self, command: str, parameter=None):
        request = {"command": command}
        if parameter not in (None, ""):
            request["parameter"] = str(parameter)
        body = json.dumps(request, separators=(",", ":")).encode("utf-8")
        with socket.create_connection((self.ip, 4028), timeout=self.timeout) as sock:
            sock.settimeout(self.timeout)
            sock.sendall(body)
            response = bytearray()
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                response.extend(chunk)
                if len(response) > MAX_RESPONSE_BYTES:
                    raise LuxOSControlError("LuxOS response exceeded the 2 MB safety limit")
        if not response:
            raise LuxOSControlError("LuxOS returned an empty response")
        try:
            payload = json.loads(bytes(response).rstrip(b"\x00").decode("utf-8", errors="replace"))
        except (TypeError, ValueError) as exc:
            raise LuxOSControlError(f"LuxOS returned invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise LuxOSControlError("LuxOS response was not an object")
        return payload

    def session_call(self, command: str, parameter=None):
        return self.session_batch([(command, parameter)])[0]

    def session_batch(self, commands):
        logon = self.call("logon")
        session_id = str(_first_row(logon, "SESSION").get("SessionID") or "").strip()
        if not session_id:
            raise LuxOSControlError(
                "LuxOS control session is busy. PoCiSys will not steal another controller's session."
            )
        try:
            results = []
            for command, parameter in commands:
                combined = session_id if parameter in (None, "") else f"{session_id},{parameter}"
                result = self.call(command, combined)
                if not _command_succeeded(result):
                    raise LuxOSControlError(_status_message(result))
                results.append(result)
            return results
        finally:
            try:
                self.call("logoff", session_id)
            except Exception:
                pass

    def profiles(self):
        payload = self.call("profiles")
        rows = payload.get("PROFILES", []) if isinstance(payload, dict) else []
        compact = []
        for row in rows[:MAX_PROFILES] if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("Profile Name") or row.get("Profile") or "").strip()
            if not name:
                continue
            compact.append({
                "name": name[:80],
                "frequency_mhz": _number(row.get("Frequency")),
                "voltage_v": _number(row.get("Voltage")),
                "hashrate_ths": _number(row.get("Hashrate")),
                "watts": _number(row.get("Watts")),
                "step": str(row.get("Step") or "")[:20],
            })
        current_profile = None
        current_power_watts = None
        power_reported_by_psu = False
        detected_boards = None
        try:
            config = self.call("config")
            current_profile = str(_first_row(config, "CONFIG").get("Profile") or "").strip() or None
        except Exception:
            pass
        try:
            power = _first_row(self.call("power"), "POWER")
            current_power_watts = _number(power.get("Watts"))
            power_reported_by_psu = bool(power.get("PSU"))
        except Exception:
            pass
        try:
            count = _integer(_first_row(self.call("asccount"), "ASCS").get("Count"))
            if count is not None:
                detected_boards = max(0, min(count, MAX_HASHBOARDS))
        except Exception:
            pass

        current = next((row for row in compact if row.get("name") == current_profile), None)
        catalog_watts = _number(current.get("watts")) if current else None
        scale = None
        if current_power_watts and current_power_watts >= 100 and catalog_watts and catalog_watts > 0:
            candidate = current_power_watts / catalog_watts
            if 0.2 <= candidate <= 1.5:
                scale = candidate
        if scale is not None:
            for row in compact:
                watts = _number(row.get("watts"))
                row["setup_watts"] = round(watts * scale) if watts is not None else None

        return {
            "profiles": compact,
            "current_profile": current_profile,
            "current_power_watts": current_power_watts,
            "power_reported_by_psu": power_reported_by_psu,
            "detected_boards": detected_boards,
            "profile_power_scale": scale,
        }

    def chip_health(self, score_threshold: float):
        count_payload = self.call("asccount")
        board_count = _integer(_first_row(count_payload, "ASCS").get("Count"), 0) or 0
        board_count = max(0, min(board_count, MAX_HASHBOARDS))
        items = []
        for board_id in range(board_count):
            payload = self.call("healthchipget", board_id)
            chips = payload.get("CHIPS", []) if isinstance(payload, dict) else []
            chips = [chip for chip in chips if isinstance(chip, dict)] if isinstance(chips, list) else []
            low_chips = []
            known_count = 0
            unknown_count = 0
            native_healthy_count = 0
            unhealthy_count = 0
            low_score_count = 0
            minimum_score = None
            for chip in chips:
                health = str(chip.get("Healthy") or "Unknown").strip().upper()
                score = _number(chip.get("Score"))
                if score is not None:
                    minimum_score = score if minimum_score is None else min(minimum_score, score)
                if health == "UNKNOWN":
                    unknown_count += 1
                else:
                    known_count += 1
                    if health == "N":
                        unhealthy_count += 1
                    else:
                        native_healthy_count += 1
                score_low = bool(
                    score_threshold > 0
                    and health not in {"UNKNOWN", "N"}
                    and score is not None
                    and score < score_threshold
                )
                if score_low:
                    low_score_count += 1
                low = health == "N" or score_low
                if low and len(low_chips) < MAX_LOW_CHIPS_PER_BOARD:
                    low_chips.append({
                        "chip": _integer(chip.get("Chip"), len(low_chips)),
                        "score": score,
                        "healthy": health,
                        "reason": "native_unhealthy" if health == "N" else "score_below_threshold",
                    })
            low_count = unhealthy_count + low_score_count
            total = len(chips)
            if unhealthy_count:
                board_status = "unhealthy"
            elif low_score_count:
                board_status = "warning"
            else:
                board_status = "healthy" if known_count > 0 else "unknown"
            items.append({
                "board_id": board_id,
                "name": f"Hashboard {board_id + 1}",
                "status": board_status,
                # This is LuxOS's native Healthy flag count. A known healthy
                # chip whose score crosses a user-defined warning threshold is
                # still responsive and must not be presented as missing.
                "chips_healthy": native_healthy_count,
                "chips_total": total,
                "chips_unknown": unknown_count,
                "chips_evaluated": known_count,
                "low_chip_count": low_count,
                "unhealthy_chip_count": unhealthy_count,
                "low_score_count": low_score_count,
                "minimum_score": round(minimum_score, 2) if minimum_score is not None else None,
                "low_chips": low_chips,
                "score_threshold": score_threshold,
                "source": "LuxOS healthchipget",
            })
        healthy_boards = sum(1 for item in items if item["status"] == "healthy")
        current_profile = None
        try:
            config = self.call("config")
            current_profile = str(_first_row(config, "CONFIG").get("Profile") or "").strip() or None
        except Exception:
            pass
        return {
            "reported": bool(items),
            "healthy": healthy_boards if items else None,
            "total": len(items) if items else None,
            "items": items,
            "score_threshold": score_threshold,
            "current_profile": current_profile,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def set_profile(self, profile: str):
        return self.session_call("profileset", profile)

    def board_count(self):
        payload = self.call("asccount")
        count = _integer(_first_row(payload, "ASCS").get("Count"), 0) or 0
        return max(0, min(count, MAX_HASHBOARDS))

    def set_boards(self, board_ids, delay_seconds: int):
        commands = [("reboot", f"{int(board_id)},{int(delay_seconds)}") for board_id in board_ids]
        if not commands:
            raise LuxOSControlError("LuxOS did not report any hashboards")
        return self.session_batch(commands)

    def restart_board(self, board_id: int, delay_seconds: int = 10):
        return self.session_call("reboot", f"{board_id},{int(delay_seconds)}")


class LuxOSControlService:
    def __init__(self, config: dict, alerts):
        self.config = config
        self.alerts = alerts
        self.health_cache = {}
        self.profile_cache = {}
        self.health_due = {}
        self.bad_confirmations = {}
        self.last_auto_attempt = {}
        self.auto_suppress_until = {}
        self.recovery_incidents = {}
        self.profile_suppress_until = {}
        self.last_action_attempt = {}
        self.last_schedule_target = {}
        self.schedule_retry_after = {}
        self.ceiling_retry_after = {}
        self.recent_actions = deque(maxlen=25)
        self.locks = {}
        self.running = False
        self.task = None
        self.wake_event = asyncio.Event()
        self.started_at = time.monotonic()

    def _miners(self):
        return [
            miner
            for miner in self.config.get("miners", [])
            if miner.get("enabled", True) and str(miner.get("type") or "").lower() == "luxos"
        ]

    def _miner(self, miner_id):
        return next((miner for miner in self._miners() if miner.get("id") == miner_id), None)

    def _global_enabled(self):
        return bool(self.config.get("app", {}).get("luxos_control_enabled", False))

    def _local_now(self):
        app = self.config.get("app", {})
        timezone_name = str(app.get("control_timezone") or "auto")
        try:
            return datetime.now(ZoneInfo(timezone_name))
        except (ZoneInfoNotFoundError, ValueError):
            minutes = max(-840, min(840, int(app.get("control_utc_offset_minutes") or 0)))
            return datetime.now(timezone(timedelta(minutes=minutes)))

    @staticmethod
    def _minutes(value):
        try:
            hour, minute = str(value).split(":", 1)
            return int(hour) * 60 + int(minute)
        except (TypeError, ValueError):
            return 0

    def _low_window(self, miner, now=None):
        now = now or self._local_now()
        start = self._minutes(miner.get("control_low_time", "16:00"))
        end = self._minutes(miner.get("control_full_time", "21:00"))
        current = now.hour * 60 + now.minute
        if start == end:
            return False
        return start <= current < end if start < end else current >= start or current < end

    def _desired_target(self, miner):
        return "low" if self._low_window(miner) else "full"

    def _schedule_summary(self, miner):
        if not miner.get("control_schedule_enabled", False):
            return {"enabled": False, "desired": None}
        desired = self._desired_target(miner)
        return {
            "enabled": True,
            "desired": desired,
            "mode": miner.get("control_low_mode", "profile"),
            "low_time": miner.get("control_low_time", "16:00"),
            "full_time": miner.get("control_full_time", "21:00"),
            "last_applied": self.last_schedule_target.get(miner.get("id")),
        }

    def enrich_status(self, status: dict):
        if str(status.get("type") or "").lower() != "luxos":
            return status
        miner_id = status.get("id")
        snapshot = self.health_cache.get(miner_id)
        if snapshot:
            status["chip_health"] = deepcopy({
                key: value
                for key, value in snapshot.items()
                if key not in {"current_profile"}
            })
            status["current_profile"] = snapshot.get("current_profile")
        status["luxos_control"] = self.miner_status(miner_id)
        return status

    def miner_status(self, miner_id):
        miner = self._miner(miner_id)
        snapshot = self.health_cache.get(miner_id, {})
        actions = [event for event in self.recent_actions if event.get("miner_id") == miner_id][:5]
        manual_enabled = bool(miner and miner.get("control_enabled", False))
        schedule_enabled = bool(miner and miner.get("control_schedule_enabled", False))
        recovery_enabled = bool(miner and miner.get("auto_recover_hashboards", False))
        now = time.monotonic()
        recovery_pending = any(
            key[0] == miner_id and count > 0
            for key, count in self.bad_confirmations.items()
        )
        recovery_observing = any(
            key[0] == miner_id and (
                key in self.recovery_incidents or now < self.auto_suppress_until.get(key, 0)
            )
            for key in set(self.auto_suppress_until) | set(self.recovery_incidents)
        )
        return {
            "available": bool(miner),
            "global_enabled": self._global_enabled(),
            "miner_enabled": bool(manual_enabled or schedule_enabled or recovery_enabled),
            "armed": bool(self._global_enabled() and manual_enabled),
            "manual_armed": bool(self._global_enabled() and manual_enabled),
            "schedule_armed": bool(self._global_enabled() and schedule_enabled),
            "recovery_armed": bool(self._global_enabled() and recovery_enabled),
            "recovery_pending": recovery_pending,
            "recovery_observing": recovery_observing,
            "current_profile": snapshot.get("current_profile"),
            "normal_profile_ceiling": miner.get("control_full_profile") if miner else None,
            "health_checked_at": snapshot.get("checked_at"),
            "health_error": snapshot.get("error"),
            "schedule": self._schedule_summary(miner) if miner else {"enabled": False, "desired": None},
            "recent_actions": actions,
            "safety": {
                "health_confirmations": AUTO_RECOVERY_CONFIRMATIONS,
                "restart_cooldown_seconds": AUTO_RECOVERY_RESTART_COOLDOWN_SECONDS,
                "post_restart_observation_seconds": AUTO_RECOVERY_POST_RESTART_SECONDS,
                "profile_change_observation_seconds": PROFILE_CHANGE_OBSERVATION_SECONDS,
                "normal_profile_is_hard_ceiling": True,
            },
        }

    def status(self):
        return {
            "enabled": self._global_enabled(),
            "timezone": self.config.get("app", {}).get("control_timezone", "auto"),
            "recent_actions": list(self.recent_actions),
            "miners": {miner.get("id"): self.miner_status(miner.get("id")) for miner in self._miners()},
        }

    async def list_profiles(self, miner_id, force=False):
        miner = self._miner(miner_id)
        if not miner:
            raise LuxOSControlError("Enabled LuxOS miner not found")
        cached = self.profile_cache.get(miner_id)
        if cached and not force and time.monotonic() - cached["at"] < 60:
            return deepcopy(cached["value"])
        timeout = self.config.get("app", {}).get("request_timeout_seconds", 4)
        value = await asyncio.to_thread(LuxOSClient(miner["ip"], timeout).profiles)
        self.profile_cache[miner_id] = {"at": time.monotonic(), "value": value}
        return deepcopy(value)

    async def refresh_health(self, miner, force=False):
        miner_id = miner.get("id")
        now = time.monotonic()
        if not force and now < self.health_due.get(miner_id, 0):
            return self.health_cache.get(miner_id)
        self.health_due[miner_id] = now + HEALTH_INTERVAL_SECONDS
        configured_threshold = miner.get("chip_health_score_threshold")
        threshold = float(90 if configured_threshold in (None, "") else configured_threshold)
        timeout = max(5, self.config.get("app", {}).get("request_timeout_seconds", 4))
        try:
            snapshot = await asyncio.to_thread(LuxOSClient(miner["ip"], timeout).chip_health, threshold)
            self.health_cache[miner_id] = snapshot
            await self._process_auto_recovery(miner, snapshot)
            return snapshot
        except Exception as exc:
            previous = deepcopy(self.health_cache.get(miner_id, {}))
            previous["error"] = str(exc)[:240]
            previous["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.health_cache[miner_id] = previous
            return previous

    async def _process_auto_recovery(self, miner, snapshot):
        miner_id = miner.get("id")
        active_keys = set()
        now = time.monotonic()
        observing_profile_change = now < self.profile_suppress_until.get(miner_id, 0)
        recovery_active = bool(
            self._global_enabled()
            and miner.get("auto_recover_hashboards", False)
            and not observing_profile_change
            and not (miner.get("control_schedule_enabled", False) and self._low_window(miner))
        )
        for board in snapshot.get("items", []):
            board_id = board.get("board_id")
            if board_id is None:
                continue
            key = (miner_id, int(board_id))
            active_keys.add(key)
            incident = self.recovery_incidents.get(key)
            if incident:
                self.bad_confirmations[key] = 0
                if now < incident["observe_until"] or board.get("status") == "unknown":
                    continue
                healthy = board.get("status") == "healthy"
                if self.config.get("discord", {}).get("send_chip_health_alerts", True):
                    title = "LuxOS Hashboard Recovered" if healthy else "WARN LuxOS Hashboard Still Unhealthy"
                    detail = (
                        "LuxOS now reports known healthy chip status. Routine low-hashrate, pool, and recovery "
                        "alerts caused by the controlled restart were suppressed."
                        if healthy else
                        "LuxOS still reports degraded chip health after the restart observation period. "
                        "The fixed six-hour board cooldown remains active to prevent a restart loop."
                    )
                    await self.alerts.emit(
                        f"{miner_id}:auto-recovery:{board_id}:complete",
                        title,
                        f"{miner.get('name')}\nHashboard {int(board_id) + 1} restart completed.\n{detail}",
                        "success" if healthy else "warning",
                        miner.get("name"),
                        force=True,
                        url=self.alerts.dashboard_link(f"/miners/{miner_id}"),
                    )
                self.recovery_incidents.pop(key, None)
                continue
            if board.get("status") in {"warning", "unhealthy"} and recovery_active:
                self.bad_confirmations[key] = self.bad_confirmations.get(key, 0) + 1
            else:
                self.bad_confirmations[key] = 0

            if not (
                recovery_active
                and self.bad_confirmations.get(key, 0) >= AUTO_RECOVERY_CONFIRMATIONS
                and time.monotonic() - self.started_at >= AUTO_RECOVERY_WARMUP_SECONDS
            ):
                continue
            if now < self.auto_suppress_until.get(key, 0):
                continue
            if now - self.last_auto_attempt.get(key, -AUTO_RECOVERY_RESTART_COOLDOWN_SECONDS) < AUTO_RECOVERY_RESTART_COOLDOWN_SECONDS:
                continue
            # Count the attempt before any network write so a failed command
            # cannot create a retry/Discord loop on subsequent health polls.
            self.last_auto_attempt[key] = now
            try:
                if self.config.get("discord", {}).get("send_chip_health_alerts", True):
                    low_count = int(board.get("low_chip_count") or 0)
                    minimum = board.get("minimum_score")
                    score_line = f"\nLowest chip score: {minimum:g}/100" if isinstance(minimum, (int, float)) else ""
                    await self.alerts.emit(
                        f"{miner_id}:auto-recovery:{board_id}:detected",
                        "WARN LuxOS Hashboard Unhealthy",
                        (
                            f"{miner.get('name')}\nHashboard {int(board_id) + 1} remained unhealthy for "
                            f"{AUTO_RECOVERY_CONFIRMATIONS} checks.\nAffected chips: {low_count}{score_line}\n"
                            "PoCiSys will restart only this hashboard and observe it before reporting the outcome."
                        ),
                        "warning",
                        miner.get("name"),
                        force=True,
                        url=self.alerts.dashboard_link(f"/miners/{miner_id}"),
                    )
                await self.execute(miner_id, "restart_board", board_id=board_id, source="automatic chip recovery")
                self.auto_suppress_until[key] = now + AUTO_RECOVERY_POST_RESTART_SECONDS
                self.recovery_incidents[key] = {
                    "started_at": now,
                    "observe_until": now + AUTO_RECOVERY_POST_RESTART_SECONDS,
                }
                self.bad_confirmations[key] = 0
            except Exception as exc:
                self.auto_suppress_until[key] = now + HEALTH_INTERVAL_SECONDS
                if self.config.get("discord", {}).get("send_chip_health_alerts", True):
                    await self.alerts.emit(
                        f"{miner_id}:auto-recovery:{board_id}:failed",
                        "WARN LuxOS Hashboard Restart Failed",
                        f"{miner.get('name')}\nHashboard {int(board_id) + 1}\n{str(exc)[:240]}",
                        "warning",
                        miner.get("name"),
                        force=True,
                        url=self.alerts.dashboard_link(f"/miners/{miner_id}"),
                    )

        for key in list(self.bad_confirmations):
            if key[0] == miner_id and key not in active_keys:
                self.bad_confirmations.pop(key, None)

    def _observe_after_profile_change(self, miner_id):
        now = time.monotonic()
        self.profile_suppress_until[miner_id] = now + PROFILE_CHANGE_OBSERVATION_SECONDS
        for key in list(self.bad_confirmations):
            if key[0] == miner_id:
                self.bad_confirmations[key] = 0

    async def _record_action(self, miner, action, success, message, source):
        event = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "miner_id": miner.get("id"),
            "miner": miner.get("name"),
            "action": action,
            "success": bool(success),
            "source": source,
            "message": str(message)[:300],
        }
        self.recent_actions.appendleft(event)
        if (
            source != "automatic chip recovery"
            and self.config.get("discord", {}).get("send_control_alerts", True)
        ):
            title = "LuxOS Control Action" if success else "WARN LuxOS Control Failed"
            await self.alerts.emit(
                f"{miner.get('id')}:control:{action}:{'ok' if success else 'failed'}",
                title,
                f"{miner.get('name')}\nAction: {action}\nSource: {source}\n{event['message']}",
                "info" if success else "warning",
                miner.get("name"),
                force=True,
                url=self.alerts.dashboard_link(f"/miners/{miner.get('id')}"),
            )
        return event

    @staticmethod
    def _profile_value(profile, key):
        value = profile.get(key) if isinstance(profile, dict) else None
        try:
            return float(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def _compare_profile_level(self, candidate, normal):
        for key in ("watts", "hashrate_ths", "frequency_mhz"):
            low_value = self._profile_value(candidate, key)
            normal_value = self._profile_value(normal, key)
            if low_value is not None and normal_value is not None:
                return (low_value > normal_value) - (low_value < normal_value)
        low_step = self._profile_value(candidate, "step")
        normal_step = self._profile_value(normal, "step")
        if low_step is not None and normal_step is not None:
            return (low_step > normal_step) - (low_step < normal_step)
        return None

    def _assert_low_not_above_normal(self, low, normal):
        comparison = self._compare_profile_level(low, normal)
        if comparison is None:
            raise LuxOSControlError(
                "LuxOS did not report enough profile data to prove this is below the Normal Operating Profile. "
                "Choose another lower profile or use Sleep."
            )
        if comparison > 0:
            raise LuxOSControlError(
                "The selected curtailed profile is above the Normal Operating Profile ceiling"
            )

    async def _set_profile(self, miner, profile, role):
        profile = str(profile or "").strip()
        if not profile or len(profile) > 80 or "," in profile or any(ord(char) < 32 for char in profile):
            raise LuxOSControlError("Choose a valid LuxOS profile")
        available = await self.list_profiles(miner.get("id"), force=True)
        profiles = {item.get("name"): item for item in available.get("profiles", [])}
        if profile not in profiles:
            raise LuxOSControlError(f"LuxOS profile '{profile}' is not currently available on this miner")
        normal_name = str(miner.get("control_full_profile") or "").strip()
        if role == "full" and profile != normal_name:
            raise LuxOSControlError("PoCiSys will not command above or outside the selected Normal Operating Profile")
        if role == "low":
            if profile != str(miner.get("control_low_profile") or "").strip():
                raise LuxOSControlError("Only the selected curtailed profile can be applied")
            normal = profiles.get(normal_name)
            if not normal:
                raise LuxOSControlError("The selected Normal Operating Profile is no longer available on LuxOS")
            self._assert_low_not_above_normal(profiles[profile], normal)
        if str(available.get("current_profile") or "").strip() == profile:
            self.health_cache.setdefault(miner.get("id"), {})["current_profile"] = profile
            return f"Native LuxOS profile {profile} is already active; no profile command was sent."
        timeout = self.config.get("app", {}).get("request_timeout_seconds", 4)
        await asyncio.to_thread(LuxOSClient(miner["ip"], timeout).set_profile, profile)
        miner_id = miner.get("id")
        self.health_cache.setdefault(miner_id, {})["current_profile"] = profile
        self._observe_after_profile_change(miner_id)
        return f"Applied native LuxOS profile {profile}."

    async def _restore_full(self, miner):
        full_profile = str(miner.get("control_full_profile") or "").strip()
        if not full_profile:
            raise LuxOSControlError("Select a normal LuxOS profile before restoring full power")
        timeout = self.config.get("app", {}).get("request_timeout_seconds", 4)
        client = LuxOSClient(miner["ip"], timeout)
        if miner.get("control_low_mode", "profile") == "boards_off":
            board_count = await asyncio.to_thread(client.board_count)
            message = await self._set_profile(miner, full_profile, "full")
            await asyncio.to_thread(client.set_boards, list(range(board_count)), 10)
            self._observe_after_profile_change(miner.get("id"))
            return f"All {board_count} hashboards were scheduled to start. {message}"
        return await self._set_profile(miner, full_profile, "full")

    async def execute(self, miner_id, action, board_id=None, source="manual"):
        miner = self._miner(miner_id)
        if not miner:
            raise LuxOSControlError("Enabled LuxOS miner not found")
        if not self._global_enabled():
            raise LuxOSControlError("Enable LuxOS Control Mode in Settings first")
        if source == "automatic chip recovery":
            if not miner.get("auto_recover_hashboards", False):
                raise LuxOSControlError("Arm automatic hashboard recovery for this miner first")
        elif source == "daily LuxOS schedule":
            if not miner.get("control_schedule_enabled", False):
                raise LuxOSControlError("Arm automatic curtailing for this miner first")
        elif source == "Normal Operating Profile ceiling":
            if not miner.get("control_enabled", False):
                raise LuxOSControlError("Arm manual LuxOS controls for this miner first")
        elif not miner.get("control_enabled", False):
            raise LuxOSControlError("Arm manual LuxOS controls for this miner first")
        now = time.monotonic()
        if now - self.last_action_attempt.get(miner_id, -CONTROL_ACTION_COOLDOWN_SECONDS) < CONTROL_ACTION_COOLDOWN_SECONDS:
            raise LuxOSControlError("Wait a few seconds before sending another control action")
        lock = self.locks.setdefault(miner_id, asyncio.Lock())
        if lock.locked():
            raise LuxOSControlError("Another control action is already running for this miner")
        async with lock:
            self.last_action_attempt[miner_id] = time.monotonic()
            timeout = self.config.get("app", {}).get("request_timeout_seconds", 4)
            client = LuxOSClient(miner["ip"], timeout)
            try:
                if action == "restart_board":
                    board_id = int(board_id)
                    if not 0 <= board_id < MAX_HASHBOARDS:
                        raise LuxOSControlError("Invalid LuxOS hashboard number")
                    board_count = await asyncio.to_thread(client.board_count)
                    if board_id >= board_count:
                        raise LuxOSControlError("LuxOS did not report that hashboard")
                    await asyncio.to_thread(client.restart_board, board_id)
                    self.auto_suppress_until[(miner_id, board_id)] = time.monotonic() + AUTO_RECOVERY_POST_RESTART_SECONDS
                    self.bad_confirmations[(miner_id, board_id)] = 0
                    message = f"Hashboard {board_id + 1} restart scheduled with a 10-second board delay."
                elif action == "board_off":
                    board_id = int(board_id)
                    if not 0 <= board_id < MAX_HASHBOARDS:
                        raise LuxOSControlError("Invalid LuxOS hashboard number")
                    board_count = await asyncio.to_thread(client.board_count)
                    if board_id >= board_count:
                        raise LuxOSControlError("LuxOS did not report that hashboard")
                    await asyncio.to_thread(client.restart_board, board_id, 0)
                    message = f"Hashboard {board_id + 1} was turned off. The control board remains online."
                elif action == "board_on":
                    board_id = int(board_id)
                    if not 0 <= board_id < MAX_HASHBOARDS:
                        raise LuxOSControlError("Invalid LuxOS hashboard number")
                    board_count = await asyncio.to_thread(client.board_count)
                    if board_id >= board_count:
                        raise LuxOSControlError("LuxOS did not report that hashboard")
                    await asyncio.to_thread(client.restart_board, board_id, 10)
                    self.auto_suppress_until[(miner_id, board_id)] = time.monotonic() + AUTO_RECOVERY_POST_RESTART_SECONDS
                    self.bad_confirmations[(miner_id, board_id)] = 0
                    message = f"Hashboard {board_id + 1} was scheduled to start in 10 seconds."
                elif action == "low":
                    if miner.get("control_low_mode", "profile") == "boards_off":
                        board_count = await asyncio.to_thread(client.board_count)
                        await asyncio.to_thread(client.set_boards, list(range(board_count)), 0)
                        message = (
                            f"Sleep turned off all {board_count} hashboards individually. "
                            "The LuxOS control board remains online; whole-miner curtail sleep was not used."
                        )
                    else:
                        message = await self._set_profile(miner, miner.get("control_low_profile"), "low")
                elif action == "full":
                    message = await self._restore_full(miner)
                else:
                    raise LuxOSControlError("Unsupported LuxOS control action")
                event = await self._record_action(miner, action, True, message, source)
                return {"ok": True, "event": event, "control": self.miner_status(miner_id)}
            except Exception as exc:
                await self._record_action(miner, action, False, str(exc), source)
                if isinstance(exc, LuxOSControlError):
                    raise
                raise LuxOSControlError(str(exc)) from exc

    async def _evaluate_schedules(self):
        if not self._global_enabled():
            return
        now = time.monotonic()
        for miner in self._miners():
            if not (miner.get("control_enabled", False) or miner.get("control_schedule_enabled", False)):
                continue
            miner_id = miner.get("id")
            if not miner.get("control_schedule_enabled", False):
                if not miner.get("control_enabled", False):
                    continue
                if now < self.ceiling_retry_after.get(miner_id, 0):
                    continue
                current_profile = self.health_cache.get(miner_id, {}).get("current_profile")
                normal_name = str(miner.get("control_full_profile") or "").strip()
                if not current_profile or not normal_name or current_profile == normal_name:
                    continue
                try:
                    catalog = await self.list_profiles(miner_id)
                    profiles = {item.get("name"): item for item in catalog.get("profiles", [])}
                    comparison = self._compare_profile_level(profiles.get(current_profile), profiles.get(normal_name))
                    if comparison is not None and comparison > 0:
                        await self.execute(miner_id, "full", source="Normal Operating Profile ceiling")
                    self.ceiling_retry_after.pop(miner_id, None)
                except Exception:
                    self.ceiling_retry_after[miner_id] = now + SCHEDULE_RETRY_SECONDS
                continue
            desired = self._desired_target(miner)
            snapshot = self.health_cache.get(miner_id, {})
            current_profile = snapshot.get("current_profile")
            expected_profile = (
                miner.get("control_low_profile")
                if desired == "low" and miner.get("control_low_mode", "profile") == "profile"
                else miner.get("control_full_profile") if desired == "full" else None
            )
            watchdog_mismatch = bool(
                expected_profile
                and current_profile
                and current_profile != expected_profile
            )
            if (
                miner.get("control_low_mode", "profile") == "profile"
                and expected_profile
                and current_profile == expected_profile
            ):
                self.last_schedule_target[miner_id] = desired
                self.schedule_retry_after.pop(miner_id, None)
                continue
            if self.last_schedule_target.get(miner_id) == desired and not watchdog_mismatch:
                continue
            if now < self.schedule_retry_after.get(miner_id, 0):
                continue
            try:
                await self.execute(miner_id, desired, source="daily LuxOS schedule")
                self.last_schedule_target[miner_id] = desired
                self.schedule_retry_after.pop(miner_id, None)
            except Exception:
                self.schedule_retry_after[miner_id] = now + SCHEDULE_RETRY_SECONDS

    def reconfigure(self):
        valid_ids = {miner.get("id") for miner in self._miners()}
        for mapping in (
            self.health_cache,
            self.profile_cache,
            self.health_due,
            self.last_action_attempt,
            self.last_schedule_target,
            self.schedule_retry_after,
            self.ceiling_retry_after,
            self.profile_suppress_until,
            self.locks,
        ):
            for key in list(mapping):
                if key not in valid_ids:
                    mapping.pop(key, None)
        for mapping in (
            self.bad_confirmations,
            self.last_auto_attempt,
            self.auto_suppress_until,
            self.recovery_incidents,
        ):
            for key in list(mapping):
                if key[0] not in valid_ids:
                    mapping.pop(key, None)
        self.wake_event.set()

    async def run(self):
        self.running = True
        while self.running:
            self.wake_event.clear()
            for miner in self._miners():
                await self.refresh_health(miner)
            await self._evaluate_schedules()
            try:
                await asyncio.wait_for(self.wake_event.wait(), timeout=CONTROL_LOOP_SECONDS)
            except asyncio.TimeoutError:
                pass

    def start(self):
        if not self.task:
            self.task = asyncio.create_task(self.run())

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
