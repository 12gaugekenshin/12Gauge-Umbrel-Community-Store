from __future__ import annotations

from .base import MinerDriver, first, integer, normalize_hashrate_ths, number, truthy


class AxeOSDriver(MinerDriver):
    api_paths = ("/api/system/info", "/api/system", "/api/info")

    def poll(self):
        result = self.empty_status()
        try:
            data, latency = self.fetch_first_json()
        except Exception as exc:
            result["warnings"].append(f"API unreachable: {exc}")
            result["status"] = "API unreachable"
            return result

        hashrate_value = first(data, "hashRate", "hashrate", "hashRateGhs", "hashrateGhs")
        hashrate_hint = "ghs" if first(data, "hashRateGhs", "hashrateGhs") is not None else "ghs"
        pool_url = first(data, "stratumURL", "poolURL", "poolUrl", "pool")
        pool_status = first(data, "stratumStatus", "poolStatus", default="unknown")
        pool_connected = truthy(first(data, "stratumConnected", "poolConnected"), None)
        using_fallback = truthy(first(data, "isUsingFallbackStratum"), False)
        if using_fallback:
            pool_url = first(data, "fallbackStratumURL", default=pool_url)
            pool_status = "fallback"
        fan_rpm = number(first(data, "fanrpm", "fanRPM", "fanSpeed"))
        fan2_rpm = number(first(data, "fan2rpm", "fanRPM2"))
        fan_values = [value for value in (fan_rpm, fan2_rpm) if value is not None and value > 0]
        asic_count = integer(first(data, "asicCount"), 1)
        asic_count = max(1, min(asic_count, 64))
        asic_temps = first(data, "asicTemps", default=[])
        asic_temps = asic_temps if isinstance(asic_temps, list) else []
        error_percent = number(first(data, "errorPercentage"), None)
        model = first(data, "ASICModel", "asicModel", default="ASIC")
        explicit_health = first(data, "asicHealth", "chipHealth", "asicStatus", "chipStatus")
        if isinstance(explicit_health, bool):
            explicit_state = "healthy" if explicit_health else "unhealthy"
        elif isinstance(explicit_health, (str, int, float)):
            text = str(explicit_health).strip().lower()
            if text in {"healthy", "alive", "ok", "running", "true", "1"}:
                explicit_state = "healthy"
            elif text in {"unhealthy", "dead", "failed", "missing", "false", "0"}:
                explicit_state = "unhealthy"
            elif text in {"warning", "degraded"}:
                explicit_state = "warning"
            else:
                explicit_state = "unknown"
        else:
            explicit_state = "unknown"
        chip_items = []
        for index in range(asic_count):
            chip_temp = number(asic_temps[index]) if index < len(asic_temps) else None
            chip_items.append({
                "name": f"{model} #{index + 1}" if asic_count > 1 else str(model),
                "status": explicit_state,
                "chips_healthy": 1 if explicit_state == "healthy" else 0 if explicit_state == "unhealthy" else None,
                "chips_total": 1,
                "temperature_c": chip_temp if chip_temp and chip_temp > 0 else None,
                "hardware_error_percent": error_percent,
                "cores": integer(first(data, "smallCoreCount"), None) if asic_count == 1 else None,
                "source": "explicit ASIC health field" if explicit_state != "unknown" else "AxeOS telemetry (health unsupported)",
            })

        result.update(
            online=True,
            api_ok=True,
            ping_ms=latency,
            hashrate_ths=normalize_hashrate_ths(hashrate_value, hashrate_hint),
            expected_hashrate_ths=normalize_hashrate_ths(first(data, "expectedHashrate"), "ghs"),
            temps={
                "asic_c": number(first(data, "temp", "temperature", "asicTemp", "asicTemperature")),
                "vrm_c": number(first(data, "vrTemp", "vrmTemp", "vrmTemperature")),
                "board_c": number(first(data, "boardTemp")),
                "chip_c": None,
            },
            chip_health={
                "reported": True,
                "healthy": asic_count if explicit_state == "healthy" else 0 if explicit_state == "unhealthy" else None,
                "total": asic_count,
                "items": chip_items,
            },
            fans=[{"name": f"fan{index + 1}", "rpm": rpm} for index, rpm in enumerate(fan_values)],
            pool={
                "url": pool_url,
                "user": first(
                    data,
                    "fallbackStratumUser" if using_fallback else "stratumUser",
                    "stratumUser",
                ),
                "connected": pool_connected,
                "status": str(pool_status),
                "source": "fallback" if using_fallback else "primary",
            },
            shares={
                "valid": integer(first(data, "sharesAccepted", "validShares", "accepted")),
                "invalid": integer(first(data, "sharesInvalid", "invalidShares")),
                "stale": integer(first(data, "sharesStale", "staleShares")),
                "rejected": integer(first(data, "sharesRejected", "rejectedShares", "rejected")),
            },
            difficulty={
                "best_session": first(data, "bestSessionDiff", "bestSessionDifficulty"),
                "best_all_time": first(data, "bestDiff", "bestDifficulty", "bestAllTimeDiff"),
            },
            uptime_seconds=integer(first(data, "uptimeSeconds", "uptime", "uptimeSecs"), None),
            firmware=first(data, "version", "firmwareVersion", "firmware", default="AxeOS"),
            frequency_mhz=number(first(data, "frequency", "frequencyMHz")),
            voltage_mv=number(first(data, "coreVoltage", "voltage", "voltageMv")),
            wifi_rssi=number(first(data, "wifiRSSI", "rssi", "wifiSignal")),
            hardware_error_percent=error_percent,
            blocks_found=integer(first(data, "blockFound"), 0),
            status=str(first(data, "status", "state", default="Healthy")),
            raw=data,
        )
        if result["pool"]["connected"] is None and str(pool_status).lower() != "unknown":
            result["pool"]["connected"] = str(pool_status).lower() in {"connected", "alive", "active"}
        return result
