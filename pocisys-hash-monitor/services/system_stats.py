from __future__ import annotations

import os
import platform
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


def _read_first_line(path: Path):
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[0].strip()
    except (OSError, IndexError):
        return None


def _read_cpu_times():
    line = _read_first_line(Path("/proc/stat"))
    if not line or not line.startswith("cpu "):
        return None
    try:
        values = [int(value) for value in line.split()[1:]]
    except ValueError:
        return None
    if len(values) < 4:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def _read_memory():
    values = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8", errors="replace").splitlines():
            key, _, raw = line.partition(":")
            if not raw:
                continue
            try:
                values[key] = int(raw.strip().split()[0]) * 1024
            except (ValueError, IndexError):
                continue
    except OSError:
        return {}
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if not total or available is None:
        return {}
    used = max(0, total - available)
    return {
        "total_bytes": total,
        "used_bytes": used,
        "available_bytes": available,
        "used_percent": round(used * 100 / total, 1),
    }


def _read_uptime():
    raw = _read_first_line(Path("/proc/uptime"))
    try:
        return round(float(raw.split()[0]), 1) if raw else None
    except (ValueError, IndexError):
        return None


def _read_process_rss():
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


class SystemStatsService:
    """Cache container-visible host metrics without blocking API requests."""

    def __init__(self, interval_seconds: float = 2.0):
        self.interval_seconds = max(0.5, float(interval_seconds))
        self._lock = threading.Lock()
        self._latest = {}
        self._previous_cpu = None
        self._stop = threading.Event()
        self._thread = None

    def _sample(self):
        cpu = _read_cpu_times()
        cpu_percent = None
        if cpu and self._previous_cpu:
            total_delta = cpu[0] - self._previous_cpu[0]
            idle_delta = cpu[1] - self._previous_cpu[1]
            if total_delta > 0:
                cpu_percent = round(max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100)), 1)
        if cpu:
            self._previous_cpu = cpu

        disk_path = Path("/data") if Path("/data").exists() else Path("/")
        try:
            disk = shutil.disk_usage(disk_path)
            disk_payload = {
                "path": str(disk_path),
                "total_bytes": disk.total,
                "used_bytes": disk.used,
                "free_bytes": disk.free,
                "used_percent": round(disk.used * 100 / disk.total, 1) if disk.total else None,
            }
        except OSError:
            disk_payload = {}

        try:
            load = os.getloadavg()
            load_average = {"1m": round(load[0], 2), "5m": round(load[1], 2), "15m": round(load[2], 2)}
        except (AttributeError, OSError):
            load_average = {}

        return {
            "scope": "container-visible host metrics",
            "cpu": {
                "usage_percent": cpu_percent,
                "logical_cores": os.cpu_count(),
                "load_average": load_average,
            },
            "memory": _read_memory(),
            "disk": disk_payload,
            "uptime_seconds": _read_uptime(),
            "process_rss_bytes": _read_process_rss(),
            "hostname": platform.node() or None,
            "platform": platform.platform(),
            "sampled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def _run(self):
        while not self._stop.is_set():
            sample = self._sample()
            with self._lock:
                self._latest = sample
            self._stop.wait(self.interval_seconds)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="pocisys-system-stats", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None

    def snapshot(self):
        with self._lock:
            return dict(self._latest)
