from __future__ import annotations

from collections import deque
from datetime import datetime, timezone


HEALTH_THRESHOLDS = {
    "warning_confirmations": 2,
    "unhealthy_confirmations": 3,
    "recovery_confirmations": 2,
    "hardware_error_warning_percent": 2.0,
    "hardware_error_unhealthy_percent": 5.0,
    "severe_hashrate_fraction": 0.25,
}
MAX_HEALTH_TRANSITIONS = 50
MAX_DIAGNOSTICS_PER_MINER = 32


def _number(value):
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _reason(code, label, severity, value=None, threshold=None, source=None, detail=None):
    result = {"code": code, "label": label, "severity": severity}
    if value is not None:
        result["value"] = value
    if threshold is not None:
        result["threshold"] = threshold
    if source:
        result["source"] = source
    if detail:
        result["detail"] = detail
    return result


class HealthEngine:
    """Firmware-aware bounded health classifier with confirmation and recovery hysteresis."""

    def __init__(self):
        self.trackers = {}
        self.transitions = deque(maxlen=MAX_HEALTH_TRANSITIONS)

    def reconfigure(self, miners):
        valid = {
            str(item.get("id") or f"{item.get('type', 'miner')}:{item.get('ip')}")
            for item in miners
        }
        self.trackers = {key: value for key, value in self.trackers.items() if key in valid}

    @staticmethod
    def _curtailed(status):
        control = status.get("luxos_control") or {}
        schedule = control.get("schedule") or {}
        return bool(schedule.get("enabled") and schedule.get("desired") == "low")

    def _signals(self, status, miner):
        candidates = []
        diagnostics = []
        supported = 0

        hashrate = _number(status.get("hashrate_ths"))
        configured_minimum = _number(miner.get("min_hashrate_ths"))
        expected = _number(status.get("expected_hashrate_ths"))
        threshold = configured_minimum
        threshold_source = "configured minimum"
        if threshold is None and expected is not None and expected > 0:
            threshold = expected * 0.75
            threshold_source = "75% of device-reported current-profile expectation"
        if hashrate is not None:
            supported += 1
            diagnostic = {"signal": "hashrate_ths", "value": hashrate, "source": "miner API"}
            if threshold is not None and threshold > 0:
                diagnostic.update(threshold=threshold, rule=threshold_source)
                if hashrate < threshold and not self._curtailed(status):
                    severe = hashrate <= threshold * HEALTH_THRESHOLDS["severe_hashrate_fraction"]
                    candidates.append(_reason(
                        "hashrate_below_expected",
                        "Hashrate Below Expected",
                        "unhealthy" if severe else "warning",
                        hashrate,
                        threshold,
                        "miner API",
                        threshold_source,
                    ))
            diagnostics.append(diagnostic)
        else:
            diagnostics.append({"signal": "hashrate_ths", "value": None, "state": "unknown", "detail": "Not reported by this API"})

        direct_error_percent = _number(status.get("hardware_error_percent"))
        hardware_errors = _number(status.get("hardware_errors"))
        valid_shares = _number((status.get("shares") or {}).get("valid"))
        error_percent = direct_error_percent
        error_source = "device-reported error percentage"
        if error_percent is None and hardware_errors is not None and valid_shares is not None and valid_shares > 0:
            error_percent = hardware_errors / (hardware_errors + valid_shares) * 100
            error_source = "hardware errors / (hardware errors + accepted shares)"
        if error_percent is not None:
            supported += 1
            diagnostics.append({
                "signal": "hardware_error_percent",
                "value": error_percent,
                "warning_threshold": HEALTH_THRESHOLDS["hardware_error_warning_percent"],
                "unhealthy_threshold": HEALTH_THRESHOLDS["hardware_error_unhealthy_percent"],
                "source": error_source,
            })
            if error_percent >= HEALTH_THRESHOLDS["hardware_error_warning_percent"]:
                severe = error_percent >= HEALTH_THRESHOLDS["hardware_error_unhealthy_percent"]
                candidates.append(_reason(
                    "high_hardware_error_rate",
                    "High HW Error Rate",
                    "unhealthy" if severe else "warning",
                    round(error_percent, 4),
                    HEALTH_THRESHOLDS["hardware_error_unhealthy_percent" if severe else "hardware_error_warning_percent"],
                    error_source,
                ))
        else:
            diagnostics.append({"signal": "hardware_error_percent", "value": None, "state": "unknown", "detail": "No reliable rate available"})

        warning_temperature = _number(miner.get("temp_warning_c"))
        critical_temperature = _number(miner.get("temp_critical_c"))
        temperatures = status.get("temps") or {}
        valid_temperatures = []
        for name, raw_value in temperatures.items():
            value = _number(raw_value)
            if value is not None and 0 < value < 200:
                valid_temperatures.append((name, value))
        if valid_temperatures:
            supported += 1
            sensor, highest = max(valid_temperatures, key=lambda item: item[1])
            diagnostics.append({
                "signal": "temperature_c",
                "sensor": sensor,
                "value": highest,
                "warning_threshold": warning_temperature,
                "critical_threshold": critical_temperature,
                "source": "miner API",
            })
            if critical_temperature is not None and highest >= critical_temperature:
                candidates.append(_reason(
                    "asic_temperature_critical", "ASIC Temperature Above Critical Limit", "unhealthy",
                    highest, critical_temperature, f"{sensor} sensor",
                ))
            elif warning_temperature is not None and highest >= warning_temperature:
                candidates.append(_reason(
                    "asic_temperature_warning", "ASIC Temperature Above Limit", "warning",
                    highest, warning_temperature, f"{sensor} sensor",
                ))
        else:
            diagnostics.append({"signal": "temperature_c", "value": None, "state": "unknown", "detail": "No supported sensor value"})

        chip_health = status.get("chip_health") or {}
        chip_items = chip_health.get("items") if isinstance(chip_health.get("items"), list) else []
        known_chip_signals = 0
        for index, item in enumerate(chip_items[:16]):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or f"ASIC group {index + 1}")[:80]
            item_state = str(item.get("status") or "unknown").strip().lower()
            healthy = _number(item.get("chips_healthy"))
            total = _number(item.get("chips_total"))
            detail = {
                "signal": "chip_responsiveness",
                "component": name,
                "value": item_state if item_state else "unknown",
                "chips_healthy": healthy,
                "chips_total": total,
                "source": str(item.get("source") or "miner API")[:120],
            }
            diagnostics.append(detail)
            explicit_bad = item_state in {"unhealthy", "dead", "missing", "failed", "not responding", "not_responding"}
            explicit_warning = item_state in {"warning", "degraded"}
            count_bad = healthy is not None and total is not None and total > 0 and healthy < total
            if item_state in {"healthy", "alive", "warning", "degraded", "unhealthy", "dead", "missing", "failed", "not responding", "not_responding"} or count_bad:
                known_chip_signals += 1
            if self._curtailed(status) and item_state in {"disabled", "off", "stopped"}:
                continue
            if explicit_bad or count_bad:
                count_detail = f"{int(healthy)}/{int(total)} responsive" if healthy is not None and total is not None else item_state
                candidates.append(_reason(
                    f"chip_not_responding:{name}", "Chip Not Responding", "unhealthy",
                    count_detail, "all reported chips responsive", name,
                ))
            elif explicit_warning:
                candidates.append(_reason(
                    f"chip_warning:{name}", "Chip Telemetry Warning", "warning",
                    item_state, "healthy", name,
                ))
        if known_chip_signals:
            supported += 1
        elif chip_health.get("reported"):
            diagnostics.append({
                "signal": "chip_health",
                "value": None,
                "state": "unknown",
                "detail": "Firmware returned chip records without a known health state",
            })

        frequency = _number(status.get("frequency_mhz"))
        voltage = _number(status.get("voltage_mv"))
        diagnostics.extend([
            {"signal": "frequency_mhz", "value": frequency, "state": "reported" if frequency is not None else "unknown"},
            {"signal": "voltage_mv", "value": voltage, "state": "reported" if voltage is not None else "unknown"},
        ])
        return candidates, diagnostics[:MAX_DIAGNOSTICS_PER_MINER], supported

    def evaluate(self, status, miner):
        key = str(status.get("id") or miner.get("id") or f"{status.get('type', 'miner')}:{status.get('ip')}")
        tracker = self.trackers.setdefault(key, {
            "state": "Unknown",
            "rule_counts": {},
            "good_count": 0,
            "reasons": [],
        })
        previous_state = tracker["state"]
        transition = None

        if not (status.get("online") and status.get("api_ok")):
            state = "Offline"
            reasons = [_reason("api_offline", "Miner API Offline", "unhealthy", source="network/API")]
            diagnostics = [{"signal": "api", "value": "unreachable", "source": "poller"}]
            tracker["rule_counts"] = {}
            tracker["good_count"] = 0
        else:
            candidates, diagnostics, supported = self._signals(status, miner)
            candidate_map = {item["code"]: item for item in candidates}
            old_counts = tracker.get("rule_counts", {})
            counts = {
                code: old_counts.get(code, 0) + 1
                for code in candidate_map
            }
            tracker["rule_counts"] = counts
            confirmed_warning = [
                candidate_map[code] for code, count in counts.items()
                if count >= HEALTH_THRESHOLDS["warning_confirmations"]
            ]
            confirmed_unhealthy = [
                candidate_map[code] for code, count in counts.items()
                if count >= HEALTH_THRESHOLDS["unhealthy_confirmations"]
                and candidate_map[code]["severity"] == "unhealthy"
            ]
            if confirmed_unhealthy:
                state = "Unhealthy"
                reasons = confirmed_unhealthy + [item for item in confirmed_warning if item not in confirmed_unhealthy]
                tracker["good_count"] = 0
            elif confirmed_warning:
                state = "Warning"
                reasons = confirmed_warning
                tracker["good_count"] = 0
            elif candidates:
                tracker["good_count"] = 0
                if previous_state in {"Warning", "Unhealthy"}:
                    state = previous_state
                    reasons = tracker.get("reasons", [])
                else:
                    state = "Healthy" if supported else "Unknown"
                    reasons = []
            else:
                tracker["good_count"] = tracker.get("good_count", 0) + 1
                if previous_state in {"Warning", "Unhealthy"} and tracker["good_count"] < HEALTH_THRESHOLDS["recovery_confirmations"]:
                    state = previous_state
                    reasons = [_reason(
                        "recovery_pending", "Recovery Confirmation Pending", "warning",
                        tracker["good_count"], HEALTH_THRESHOLDS["recovery_confirmations"], "health hysteresis",
                    )]
                else:
                    state = "Healthy" if supported else "Unknown"
                    reasons = []

        if state != previous_state:
            transition = {
                "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "miner_id": key,
                "miner": status.get("name") or miner.get("name") or key,
                "from": previous_state,
                "to": state,
                "reasons": [item.get("label") for item in reasons],
            }
            self.transitions.appendleft(transition)

        tracker["state"] = state
        tracker["reasons"] = reasons
        health = {
            "state": state,
            "reasons": reasons,
            "diagnostics": diagnostics,
            "transition": transition,
            "thresholds": dict(HEALTH_THRESHOLDS),
        }
        status["health"] = health
        status["status"] = state
        warnings = status.setdefault("warnings", [])
        for item in reasons:
            label = item.get("label")
            if label and label not in warnings:
                warnings.append(label)
        return status

    def status(self):
        return {"thresholds": dict(HEALTH_THRESHOLDS), "transitions": list(self.transitions)}
