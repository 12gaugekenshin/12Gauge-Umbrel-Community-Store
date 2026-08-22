#!/usr/bin/env python3
"""Fail-safe P100 fan control through a Corsair Commander DUO/OpenLinkHub."""

from __future__ import annotations

import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


STATUS_FILE = Path(
    os.environ.get(
        "POCISYS_FAN_STATUS_FILE", "/run/pocisys-gpu-runtime/fan-status.json"
    )
)
COMMAND_FILE = Path(
    os.environ.get(
        "POCISYS_FAN_COMMAND_FILE", "/run/pocisys-gpu-runtime/fan-command.json"
    )
)
SETTINGS_FILE = Path(
    os.environ.get("POCISYS_FAN_SETTINGS_FILE", "/data/fan-controller.json")
)
CALIBRATION_FILE = Path(
    os.environ.get("POCISYS_FAN_CALIBRATION_FILE", "/data/fan-calibration.json")
)
OPENLINKHUB_URL = os.environ.get(
    "POCISYS_OPENLINKHUB_URL", "http://127.0.0.1:27003"
).rstrip("/")
POLL_SECONDS = max(1.0, float(os.environ.get("POCISYS_FAN_POLL_SECONDS", "2")))
BUSY_THRESHOLD_PERCENT = 10.0
BUSY_HOLD_SECONDS = 60.0
RECONNECT_HOLD_SECONDS = 10.0
COMMAND_HEARTBEAT_SECONDS = 30.0
RPM_SETTLE_SECONDS = 5.0
CALIBRATION_DUTIES = {100, 70, 50, 40}
CALIBRATION_ORDER = (100, 70, 50, 40)
EXPECTED_PRODUCT = "iCUE COMMANDER DUO"


def atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def read_json(path: Path, maximum_bytes: int = 64 * 1024) -> dict[str, Any]:
    try:
        if path.stat().st_size > maximum_bytes:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def numeric(value: object) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def temperature_duty(temperature_c: float) -> int:
    """Return the linearly interpolated bounded P100 temperature curve."""
    points = ((45.0, 40), (50.0, 50), (55.0, 60), (60.0, 75), (65.0, 90), (70.0, 100))
    if temperature_c <= points[0][0]:
        return points[0][1]
    if temperature_c >= points[-1][0]:
        return points[-1][1]
    for (low_temp, low_duty), (high_temp, high_duty) in zip(points, points[1:]):
        if low_temp <= temperature_c <= high_temp:
            fraction = (temperature_c - low_temp) / (high_temp - low_temp)
            return round(low_duty + fraction * (high_duty - low_duty))
    return 100


class NvidiaSmiReader:
    def __init__(self, configured_uuid: str = "") -> None:
        self.uuid = configured_uuid.strip()

    @staticmethod
    def _run(query: str, gpu_uuid: str = "") -> list[list[str]]:
        command = ["nvidia-smi"]
        if gpu_uuid:
            command.append(f"--id={gpu_uuid}")
        command.extend([f"--query-gpu={query}", "--format=csv,noheader,nounits"])
        result = subprocess.run(
            command, check=True, capture_output=True, text=True, timeout=4
        )
        return [
            [item.strip() for item in line.split(",")]
            for line in result.stdout.splitlines()
            if line.strip()
        ]

    def discover(self) -> dict[str, Any]:
        rows = self._run("name,uuid,pci.bus_id,temperature.gpu,utilization.gpu")
        candidates = []
        for row in rows:
            if len(row) < 5:
                continue
            candidates.append(
                {
                    "name": row[0],
                    "uuid": row[1],
                    "pci_bus_id": row[2],
                    "temperature_c": numeric(row[3]),
                    "utilization_percent": numeric(row[4]),
                }
            )
        if self.uuid:
            selected = next((gpu for gpu in candidates if gpu["uuid"] == self.uuid), None)
            if selected is None:
                raise RuntimeError("Configured P100 UUID was not reported by nvidia-smi")
        else:
            p100s = [gpu for gpu in candidates if "P100" in gpu["name"].upper()]
            if len(p100s) != 1:
                raise RuntimeError(f"Expected exactly one Tesla P100, found {len(p100s)}")
            selected = p100s[0]
            self.uuid = str(selected["uuid"])
        return selected

    def sample(self) -> dict[str, Any]:
        if not self.uuid:
            self.discover()
        rows = self._run(
            "name,uuid,pci.bus_id,temperature.gpu,utilization.gpu", self.uuid
        )
        if len(rows) != 1 or len(rows[0]) < 5:
            raise RuntimeError("Selected P100 telemetry is unavailable")
        row = rows[0]
        temperature = numeric(row[3])
        utilization = numeric(row[4])
        if row[1] != self.uuid or temperature is None or utilization is None:
            raise RuntimeError("Selected P100 telemetry is invalid")
        if not (0 <= temperature <= 120 and 0 <= utilization <= 100):
            raise RuntimeError("Selected P100 telemetry is outside valid bounds")
        return {
            "name": row[0],
            "uuid": row[1],
            "pci_bus_id": row[2],
            "temperature_c": temperature,
            "utilization_percent": utilization,
            "observed_at": time.time(),
        }


@dataclass(frozen=True)
class FanTarget:
    serial: str
    product: str
    channel_id: int
    physical_port: int
    name: str
    rpm: int
    temperature_probes: tuple[dict[str, Any], ...] = ()


class OpenLinkHubClient:
    def __init__(self, base_url: str = OPENLINKHUB_URL) -> None:
        self.base_url = base_url.rstrip("/")

    def _json(
        self, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        body = None
        method = "GET"
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            method = "POST"
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=body, headers=headers, method=method
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            value = json.load(response)
        if not isinstance(value, dict):
            raise RuntimeError("OpenLinkHub returned an invalid response")
        return value

    def health(self) -> bool:
        response = self._json("/api/")
        return int(response.get("code", 0)) == 200

    def discover(self, configured_serial: str = "", configured_channel: int | None = None) -> FanTarget:
        response = self._json("/api/devices/")
        devices = response.get("devices")
        if int(response.get("code", 0)) != 200 or not isinstance(devices, dict):
            raise RuntimeError("OpenLinkHub device API is unavailable")

        duos: list[tuple[str, dict[str, Any]]] = []
        for key, outer in devices.items():
            if not isinstance(outer, dict):
                continue
            detail = outer.get("GetDevice") or outer.get("getDevice") or {}
            if not isinstance(detail, dict):
                continue
            product = str(outer.get("Product") or detail.get("product") or "")
            serial = str(outer.get("Serial") or detail.get("serial") or key)
            if product == EXPECTED_PRODUCT and (not configured_serial or serial == configured_serial):
                duos.append((serial, detail))
        if len(duos) != 1:
            raise RuntimeError(f"Expected exactly one {EXPECTED_PRODUCT}, found {len(duos)}")

        serial, detail = duos[0]
        channel_map = detail.get("devices")
        if not isinstance(channel_map, dict):
            raise RuntimeError("Commander DUO returned no PWM channel data")
        probes: list[dict[str, Any]] = []
        for raw in channel_map.values():
            if not isinstance(raw, dict):
                continue
            is_probe = raw.get(
                "IsTemperatureProbe", raw.get("isTemperatureProbe", False)
            )
            temperature = numeric(raw.get("temperature"))
            if is_probe and temperature is not None:
                probes.append(
                    {
                        "channel_id": int(raw.get("channelId", -1)),
                        "name": str(raw.get("name") or "Temperature Probe"),
                        "label": str(raw.get("label") or ""),
                        "temperature_c": temperature,
                    }
                )
        probes.sort(key=lambda probe: probe["channel_id"])
        channels: list[FanTarget] = []
        for raw in channel_map.values():
            if not isinstance(raw, dict):
                continue
            has_speed = raw.get("HasSpeed", raw.get("hasSpeed", False))
            if not has_speed:
                continue
            channel = int(raw.get("channelId", -1))
            if configured_channel is not None and channel != configured_channel:
                continue
            channels.append(
                FanTarget(
                    serial=serial,
                    product=EXPECTED_PRODUCT,
                    channel_id=channel,
                    physical_port=channel + 1,
                    name=str(raw.get("name") or f"Fan Channel {channel + 1}"),
                    rpm=int(numeric(raw.get("rpm")) or 0),
                    temperature_probes=tuple(probes),
                )
            )
        if configured_channel is None:
            spinning = [channel for channel in channels if channel.rpm > 0]
            if len(spinning) == 1:
                return spinning[0]
        if len(channels) != 1:
            raise RuntimeError(
                "Unable to identify one connected Commander DUO PWM fan channel"
            )
        return channels[0]

    def set_speed(self, target: FanTarget, duty: int) -> dict[str, Any]:
        response = self._json(
            "/api/speed/manual",
            {
                "deviceId": target.serial,
                "channelId": target.channel_id,
                "value": max(40, min(100, int(duty))),
            },
        )
        if int(response.get("status", 0)) != 1:
            raise RuntimeError(
                f"OpenLinkHub rejected fan command: {response.get('message', 'status 0')}"
            )
        return response


def corsair_usb_status() -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["lsusb"], check=True, capture_output=True, text=True, timeout=3
        )
    except (OSError, subprocess.SubprocessError):
        return {"detected": False, "vendor_id": "1b1c", "product_id": None}
    matches = re.findall(
        r"ID\s+1b1c:([0-9a-fA-F]{4})\s+([^\r\n]+)", result.stdout
    )
    if not matches:
        return {"detected": False, "vendor_id": "1b1c", "product_id": None}
    preferred = next((item for item in matches if item[0].lower() == "0c56"), matches[0])
    return {
        "detected": True,
        "vendor_id": "1b1c",
        "product_id": preferred[0].lower(),
        "description": preferred[1].strip(),
        "expected_product_id": preferred[0].lower() == "0c56",
    }


class FanController:
    def __init__(
        self,
        gpu: NvidiaSmiReader | Any,
        hub: OpenLinkHubClient | Any,
        *,
        status_file: Path = STATUS_FILE,
        command_file: Path = COMMAND_FILE,
        settings_file: Path = SETTINGS_FILE,
        calibration_file: Path = CALIBRATION_FILE,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.gpu = gpu
        self.hub = hub
        self.status_file = status_file
        self.command_file = command_file
        self.settings_file = settings_file
        self.calibration_file = calibration_file
        self.clock = clock
        self.wall_clock = wall_clock
        self.settings = read_json(settings_file)
        self.settings.setdefault("automatic_enabled", False)
        self.calibration = read_json(calibration_file)
        self.last_command_id = ""
        self.last_hub_online = False
        self.last_sent_duty: int | None = None
        self.last_sent_at = 0.0
        self.busy_until = 0.0
        self.reconnect_until = 0.0
        self.manual: dict[str, Any] | None = None
        self.last_status: dict[str, Any] = {}
        self.gpu_temperatures: deque[tuple[float, float]] = deque(maxlen=15)

    def _gpu_is_stable(self) -> bool:
        recent = [sample for sample in self.gpu_temperatures if self.clock() - sample[0] <= 15]
        if len(recent) < 4 or recent[-1][0] - recent[0][0] < 6:
            return False
        values = [temperature for _, temperature in recent]
        return max(values) - min(values) <= 2

    def _save_settings(self) -> None:
        atomic_write_json(self.settings_file, self.settings)

    def _consume_command(self, gpu_sample: dict[str, Any] | None) -> None:
        command = read_json(self.command_file, 4096)
        command_id = str(command.get("id") or "")
        if not command_id or command_id == self.last_command_id:
            return
        self.last_command_id = command_id
        action = command.get("action")
        if action == "manual":
            duty = int(command.get("duty", 100))
            duration = float(command.get("duration_seconds", 25))
            if duty not in CALIBRATION_DUTIES or not 20 <= duration <= 30:
                return
            prerequisite_duties = CALIBRATION_ORDER[: CALIBRATION_ORDER.index(duty)]
            if any(
                not bool(self.calibration.get(str(prior), {}).get("success"))
                for prior in prerequisite_duties
            ):
                return
            if duty < 100 and not self._gpu_is_stable():
                return
            temperature = (gpu_sample or {}).get("temperature_c")
            self.manual = {
                "duty": duty,
                "calibration": True,
                "started_at": self.clock(),
                "expires_at": self.clock() + duration,
                "commanded_at": None,
                "start_temperature_c": temperature,
                "min_rpm": None,
                "max_rpm": None,
                "max_temperature_c": temperature,
            }
            self.last_sent_duty = None
        elif action == "automatic":
            enabled = bool(command.get("enabled", False))
            calibrated = all(
                str(duty) in self.calibration
                and bool(self.calibration[str(duty)].get("success"))
                for duty in CALIBRATION_DUTIES
            )
            self.settings["automatic_enabled"] = enabled and calibrated
            self._save_settings()
        elif action == "force_100":
            self.manual = {
                "duty": 100,
                "calibration": False,
                "started_at": self.clock(),
                "expires_at": self.clock() + 30,
                "commanded_at": None,
                "start_temperature_c": (gpu_sample or {}).get("temperature_c"),
                "min_rpm": None,
                "max_rpm": None,
                "max_temperature_c": (gpu_sample or {}).get("temperature_c"),
            }
            self.last_sent_duty = None

    def _finish_manual(self, success: bool, reason: str) -> None:
        if not self.manual:
            return
        if not self.manual.get("calibration"):
            self.manual = None
            return
        duty = str(self.manual["duty"])
        self.calibration[duty] = {
            "success": success,
            "reason": reason,
            "completed_at": self.wall_clock(),
            "min_rpm": self.manual.get("min_rpm"),
            "max_rpm": self.manual.get("max_rpm"),
            "start_temperature_c": self.manual.get("start_temperature_c"),
            "max_temperature_c": self.manual.get("max_temperature_c"),
        }
        atomic_write_json(self.calibration_file, self.calibration)
        self.manual = None

    def _update_manual_observations(
        self, target: FanTarget, gpu_sample: dict[str, Any] | None
    ) -> None:
        if not self.manual:
            return
        commanded_at = numeric(self.manual.get("commanded_at"))
        if commanded_at is None or self.clock() - commanded_at < RPM_SETTLE_SECONDS:
            return
        rpm = target.rpm
        if rpm > 0:
            current_min = self.manual.get("min_rpm")
            current_max = self.manual.get("max_rpm")
            self.manual["min_rpm"] = rpm if current_min is None else min(current_min, rpm)
            self.manual["max_rpm"] = rpm if current_max is None else max(current_max, rpm)
        temperature = (gpu_sample or {}).get("temperature_c")
        if temperature is not None:
            current = self.manual.get("max_temperature_c")
            self.manual["max_temperature_c"] = (
                temperature if current is None else max(current, temperature)
            )

    def _select_duty(
        self, now: float, gpu_sample: dict[str, Any] | None, target: FanTarget
    ) -> tuple[int, str]:
        if gpu_sample is None:
            return 100, "invalid_gpu_telemetry"
        temperature = float(gpu_sample["temperature_c"])
        utilization = float(gpu_sample["utilization_percent"])
        if utilization >= BUSY_THRESHOLD_PERCENT:
            self.busy_until = now + BUSY_HOLD_SECONDS
            if self.manual and int(self.manual["duty"]) < 100:
                self._finish_manual(False, "GPU utilization reached 10%")
            return 100, "gpu_busy"
        if temperature >= 70:
            if self.manual and int(self.manual["duty"]) < 100:
                self._finish_manual(False, "GPU reached 70 C")
            return 100, "temperature_failsafe"
        if now < self.busy_until:
            return 100, "gpu_cooldown"
        if now < self.reconnect_until:
            return 100, "controller_reconnect"

        if self.manual:
            start_temp = numeric(self.manual.get("start_temperature_c"))
            if (
                int(self.manual["duty"]) < 100
                and start_temp is not None
                and temperature >= start_temp + 5
            ):
                self._finish_manual(False, "GPU temperature rose by 5 C")
                return 100, "calibration_aborted"
            if now >= float(self.manual["expires_at"]):
                if not self.manual.get("calibration"):
                    self._finish_manual(True, "completed")
                    return 100, "forced_full_speed_complete"
                success = self.manual.get("min_rpm") not in (None, 0)
                self._finish_manual(success, "completed" if success else "no RPM reported")
                return 100, "calibration_complete" if success else "rpm_failsafe"
            return int(self.manual["duty"]), "manual_calibration"

        if bool(self.settings.get("automatic_enabled")):
            return temperature_duty(temperature), "automatic_temperature_curve"
        return 100, "automatic_disabled"

    def tick(self) -> dict[str, Any]:
        now = self.clock()
        usb = corsair_usb_status()
        gpu_sample: dict[str, Any] | None = None
        gpu_error = ""
        try:
            gpu_sample = self.gpu.sample()
            self.gpu_temperatures.append(
                (now, float(gpu_sample["temperature_c"]))
            )
            if not self.settings.get("gpu_uuid"):
                self.settings["gpu_uuid"] = gpu_sample["uuid"]
                self._save_settings()
        except Exception as error:  # fail-safe boundary around nvidia-smi
            gpu_error = str(error)

        self._consume_command(gpu_sample)
        target: FanTarget | None = None
        hub_error = ""
        try:
            configured_channel = self.settings.get("fan_channel")
            target = self.hub.discover(
                str(self.settings.get("fan_serial") or ""),
                int(configured_channel) if configured_channel is not None else None,
            )
            if not self.last_hub_online:
                self.hub.set_speed(target, 100)
                self.last_sent_duty = 100
                self.last_sent_at = now
                if (
                    self.manual
                    and int(self.manual["duty"]) == 100
                    and self.manual.get("commanded_at") is None
                ):
                    self.manual["commanded_at"] = now
                self.reconnect_until = now + RECONNECT_HOLD_SECONDS
            self.last_hub_online = True
            if not self.settings.get("fan_serial"):
                self.settings["fan_serial"] = target.serial
                self.settings["fan_channel"] = target.channel_id
                self._save_settings()
        except Exception as error:  # fail-safe boundary around OpenLinkHub
            hub_error = str(error)
            self.last_hub_online = False

        duty = 100
        mode = "controller_unavailable"
        command_ok = False
        if target is not None:
            self._update_manual_observations(target, gpu_sample)
            duty, mode = self._select_duty(now, gpu_sample, target)
            if (
                target.rpm <= 0
                and self.last_sent_at
                and now - self.last_sent_at >= RPM_SETTLE_SECONDS
            ):
                if self.manual and int(self.manual["duty"]) < 100:
                    self._finish_manual(False, "Fan RPM fell to zero")
                duty, mode = 100, "rpm_failsafe"
            if (
                duty != self.last_sent_duty
                or now - self.last_sent_at >= COMMAND_HEARTBEAT_SECONDS
            ):
                try:
                    self.hub.set_speed(target, duty)
                    self.last_sent_duty = duty
                    self.last_sent_at = now
                    if (
                        self.manual
                        and int(self.manual["duty"]) == duty
                        and self.manual.get("commanded_at") is None
                    ):
                        self.manual["commanded_at"] = now
                    command_ok = True
                except Exception as error:
                    hub_error = str(error)
                    duty, mode = 100, "command_failed"
                    try:
                        self.hub.set_speed(target, 100)
                        self.last_sent_duty = 100
                        self.last_sent_at = now
                    except Exception:
                        pass
            else:
                command_ok = True

        calibrated = {
            str(duty): bool(self.calibration.get(str(duty), {}).get("success"))
            for duty in sorted(CALIBRATION_DUTIES, reverse=True)
        }
        status = {
            "online": target is not None and command_ok,
            "healthy": (
                target is not None
                and command_ok
                and target.rpm > 0
                and gpu_sample is not None
            ),
            "mode": mode,
            "target_percent": duty,
            "reported_rpm": target.rpm if target else 0,
            "automatic_enabled": bool(self.settings.get("automatic_enabled")),
            "busy_hold_remaining_seconds": max(0, round(self.busy_until - now)),
            "gpu": gpu_sample,
            "gpu_stable": self._gpu_is_stable(),
            "gpu_error": gpu_error,
            "openlinkhub": {
                "online": target is not None,
                "error": hub_error,
                "product": target.product if target else None,
                "serial": target.serial if target else self.settings.get("fan_serial"),
                "channel_id": target.channel_id if target else self.settings.get("fan_channel"),
                "physical_port": target.physical_port if target else None,
                "channel_name": target.name if target else None,
                "temperature_probes": (
                    list(target.temperature_probes) if target else []
                ),
            },
            "usb": usb,
            "manual_test": self.manual,
            "manual_remaining_seconds": (
                max(0, round(float(self.manual["expires_at"]) - now))
                if self.manual
                else 0
            ),
            "calibration": self.calibration,
            "calibrated_duties": calibrated,
            "calibration_complete": all(calibrated.values()),
            "updated_at": self.wall_clock(),
        }
        atomic_write_json(self.status_file, status)
        self.last_status = status
        return status

    def force_100(self) -> bool:
        try:
            configured_channel = self.settings.get("fan_channel")
            target = self.hub.discover(
                str(self.settings.get("fan_serial") or ""),
                int(configured_channel) if configured_channel is not None else None,
            )
            self.hub.set_speed(target, 100)
            return True
        except Exception:
            return False

    def run(self, stop: threading.Event) -> None:
        try:
            while not stop.is_set():
                started = self.clock()
                try:
                    self.tick()
                except Exception as error:
                    atomic_write_json(
                        self.status_file,
                        {
                            "online": False,
                            "healthy": False,
                            "mode": "internal_error",
                            "target_percent": 100,
                            "error": str(error),
                            "updated_at": self.wall_clock(),
                        },
                    )
                    self.force_100()
                stop.wait(max(0.1, POLL_SECONDS - (self.clock() - started)))
        finally:
            self.force_100()


def main() -> None:
    settings = read_json(SETTINGS_FILE)
    configured_uuid = str(
        os.environ.get("POCISYS_GPU_UUID") or settings.get("gpu_uuid") or ""
    )
    controller = FanController(
        NvidiaSmiReader(configured_uuid), OpenLinkHubClient()
    )
    if len(sys.argv) > 1 and sys.argv[1] == "--force-100":
        raise SystemExit(0 if controller.force_100() else 1)
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    controller.run(stop)


if __name__ == "__main__":
    main()
