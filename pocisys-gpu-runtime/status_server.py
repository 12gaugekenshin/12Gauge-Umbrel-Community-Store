#!/usr/bin/env python3
"""Read-only status server for the PoCiSys GPU Runtime."""

from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlsplit
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


STATUS_DIR = Path(os.environ.get("POCISYS_STATUS_DIR", "/run/pocisys-gpu-runtime"))
WEB_DIR = Path(os.environ.get("POCISYS_WEB_DIR", Path(__file__).with_name("web")))
PORT = int(os.environ.get("POCISYS_STATUS_PORT", "8780"))
TEST_MODEL = os.environ.get("POCISYS_TEST_MODEL", "qwen3.5:9b-64k")
TEST_TIMEOUT_SECONDS = 45
TEST_LOCK = threading.Lock()
SETTINGS_LOCK = threading.Lock()
SETTINGS_FILE = Path(os.environ.get("POCISYS_SETTINGS_FILE", "/data/runtime-settings.json"))
FAN_STATUS_FILE = Path(
    os.environ.get(
        "POCISYS_FAN_STATUS_FILE", "/run/pocisys-gpu-runtime/fan-status.json"
    )
)
FAN_COMMAND_FILE = Path(
    os.environ.get(
        "POCISYS_FAN_COMMAND_FILE", "/run/pocisys-gpu-runtime/fan-command.json"
    )
)
FAN_SETTINGS_FILE = Path(
    os.environ.get("POCISYS_FAN_SETTINGS_FILE", "/data/fan-controller.json")
)
ALLOWED_CONTEXT_LENGTHS = {1024, 2048, 4096}
ALLOWED_KEEP_ALIVE = {"0", "30s", "2m"}


def default_runtime_settings() -> dict[str, int | str]:
    try:
        context_length = int(os.environ.get("OLLAMA_CONTEXT_LENGTH", "4096"))
    except ValueError:
        context_length = 4096
    if context_length not in ALLOWED_CONTEXT_LENGTHS:
        context_length = 4096
    keep_alive = os.environ.get("OLLAMA_KEEP_ALIVE", "0")
    if keep_alive not in ALLOWED_KEEP_ALIVE:
        keep_alive = "0"
    return {"context_length": context_length, "keep_alive": keep_alive}


def validate_runtime_settings(payload: object) -> dict[str, int | str]:
    if not isinstance(payload, dict):
        raise ValueError("Settings must be a JSON object.")
    try:
        context_length = int(payload.get("context_length"))
    except (TypeError, ValueError) as error:
        raise ValueError("Choose a valid context length.") from error
    keep_alive = str(payload.get("keep_alive", ""))
    if context_length not in ALLOWED_CONTEXT_LENGTHS:
        raise ValueError("Context length must be 1024, 2048, or 4096.")
    if keep_alive not in ALLOWED_KEEP_ALIVE:
        raise ValueError("Unload timing must be immediate, 30 seconds, or 2 minutes.")
    return {"context_length": context_length, "keep_alive": keep_alive}


def read_runtime_settings() -> dict[str, int | str]:
    try:
        return validate_runtime_settings(
            json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError, ValueError):
        return default_runtime_settings()


def write_runtime_settings(settings: dict[str, int | str]) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(settings, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, SETTINGS_FILE)


def read_field(name: str, default: str = "") -> str:
    try:
        return (STATUS_DIR / name).read_text(encoding="utf-8").strip()
    except OSError:
        return default


def read_json_file(path: Path, maximum_bytes: int = 64 * 1024) -> dict[str, Any]:
    try:
        if path.stat().st_size > maximum_bytes:
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_fan_command(action: str, **values: object) -> dict[str, Any]:
    command = {
        "id": str(uuid.uuid4()),
        "action": action,
        "requested_at": time.time(),
        **values,
    }
    FAN_COMMAND_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = FAN_COMMAND_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(command) + "\n", encoding="utf-8")
    os.replace(temporary, FAN_COMMAND_FILE)
    return command


def fan_status() -> dict[str, Any]:
    status = read_json_file(FAN_STATUS_FILE)
    updated_at = status.get("updated_at")
    try:
        stale = time.time() - float(updated_at) > 8
    except (TypeError, ValueError):
        stale = True
    if stale:
        status.update(
            {
                "online": False,
                "healthy": False,
                "mode": "stale_controller_status",
                "target_percent": 100,
            }
        )
    return status


def wait_for_fan_100(timeout_seconds: float = 8.0) -> dict[str, Any]:
    command = write_fan_command("force_100")
    requested_at = float(command["requested_at"])
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = read_json_file(FAN_STATUS_FILE)
        if (
            latest.get("healthy") is True
            and int(latest.get("target_percent") or 0) == 100
            and int(latest.get("reported_rpm") or 0) > 0
            and float(latest.get("updated_at") or 0) >= requested_at
        ):
            return latest
        time.sleep(0.25)
    raise RuntimeError(
        "Fan controller did not confirm 100% with nonzero RPM; inference was not started."
    )


def gpu_status() -> dict[str, Any] | None:
    query = (
        "name,uuid,pci.bus_id,driver_version,temperature.gpu,power.draw,power.limit,"
        "memory.used,memory.total,utilization.gpu"
    )
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                f"--query-gpu={query}",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        rows = [
            [item.strip() for item in line.split(",")]
            for line in result.stdout.splitlines()
            if line.strip()
        ]
        keys = [
            "name",
            "uuid",
            "pci_bus_id",
            "driver_version",
            "temperature_c",
            "power_w",
            "power_limit_w",
            "memory_used_mib",
            "memory_total_mib",
            "utilization_percent",
        ]
        configured_uuid = str(read_json_file(FAN_SETTINGS_FILE).get("gpu_uuid") or "")
        selected = next(
            (
                dict(zip(keys, values, strict=False))
                for values in rows
                if len(values) >= len(keys)
                and (not configured_uuid or values[1] == configured_uuid)
                and "P100" in values[0].upper()
            ),
            None,
        )
        return selected
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def ollama_status() -> dict[str, Any]:
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:11434/api/tags", timeout=2
        ) as response:
            payload = json.load(response)
        models = [
            {
                "name": model.get("name", ""),
                "size": model.get("size", 0),
                "modified_at": model.get("modified_at", ""),
            }
            for model in payload.get("models", [])
        ]
        return {"online": True, "models": models}
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return {"online": False, "models": []}


def snapshot() -> dict[str, Any]:
    return {
        "state": read_field("state", "starting"),
        "message": read_field("message", "Starting."),
        "updated_at": read_field("updated_at"),
        "app_version": read_field("app_version"),
        "kernel": read_field("kernel"),
        "kernel_headers": read_field("kernel_headers"),
        "kernel_source": read_field("kernel_source"),
        "kernel_output": read_field("kernel_output"),
        "gpu_bdf": read_field("gpu_bdf"),
        "pci_class": read_field("pci_class"),
        "original_driver": read_field("original_driver"),
        "secure_boot": read_field("secure_boot"),
        "max_gpu_temp_c": read_field("max_gpu_temp_c"),
        "driver_version": read_field("driver_version"),
        "ollama_version": read_field("ollama_version"),
        "ollama_endpoint": read_field("ollama_endpoint"),
        "data_root": read_field("data_root"),
        "runtime_settings": read_runtime_settings(),
        "fan": fan_status(),
        "gpu": gpu_status(),
        "ollama": ollama_status(),
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


def run_safe_test() -> dict[str, Any]:
    fan = wait_for_fan_100()
    payload = {
        "model": TEST_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are running a bounded hardware test. Do not reason aloud. "
                    "Follow the user's exact output instruction."
                ),
            },
            {
                "role": "user",
                "content": "Reply with exactly: PoCiSys local GPU online.",
            },
        ],
        "stream": False,
        "think": False,
        "keep_alive": 0,
        "options": {
            "num_ctx": 2048,
            "num_predict": 64,
            "temperature": 0,
        },
    }
    request = urllib.request.Request(
        "http://127.0.0.1:11434/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(
        request, timeout=TEST_TIMEOUT_SECONDS
    ) as response:
        result = json.load(response)

    eval_count = int(result.get("eval_count") or 0)
    eval_duration = int(result.get("eval_duration") or 0)
    tokens_per_second = (
        round(eval_count / (eval_duration / 1_000_000_000), 2)
        if eval_count and eval_duration
        else 0
    )
    return {
        "model": result.get("model", TEST_MODEL),
        "response": str(result.get("message", {}).get("content", ""))[:1000],
        "thinking_returned": bool(result.get("message", {}).get("thinking")),
        "eval_tokens": eval_count,
        "tokens_per_second": tokens_per_second,
        "total_seconds": round(int(result.get("total_duration") or 0) / 1_000_000_000, 2),
        "done_reason": result.get("done_reason", ""),
        "fan_preflight": {
            "target_percent": fan.get("target_percent"),
            "reported_rpm": fan.get("reported_rpm"),
            "gpu_uuid": (fan.get("gpu") or {}).get("uuid"),
        },
        "limits": {
            "thinking": False,
            "max_output_tokens": 64,
            "context_tokens": 2048,
            "timeout_seconds": TEST_TIMEOUT_SECONDS,
        },
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "PoCiSysGPUStatus/0.1"

    def log_message(self, format_string: str, *args: object) -> None:
        print(
            f"{self.client_address[0]} - {format_string % args}",
            flush=True,
        )

    def send_bytes(
        self,
        body: bytes,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self.send_bytes(b'{"status":"ok"}\n', "application/json")
            return
        if path == "/api/status":
            body = json.dumps(snapshot(), separators=(",", ":")).encode("utf-8")
            self.send_bytes(body, "application/json")
            return

        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        candidate = (WEB_DIR / relative).resolve()
        try:
            candidate.relative_to(WEB_DIR.resolve())
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_bytes(candidate.read_bytes(), content_type)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path not in (
            "/api/safe-test",
            "/api/settings",
            "/api/fan/manual",
            "/api/fan/automatic",
            "/api/fan/force-100",
        ):
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        origin = self.headers.get("Origin")
        host = self.headers.get("Host")
        if origin and (not host or urlsplit(origin).netloc != host):
            self.send_bytes(
                b'{"error":"Cross-origin test requests are not allowed."}\n',
                "application/json",
                HTTPStatus.FORBIDDEN,
            )
            return

        if path in (
            "/api/settings",
            "/api/fan/manual",
            "/api/fan/automatic",
            "/api/fan/force-100",
        ):
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                content_length = 0
            if path == "/api/fan/force-100" and content_length == 0:
                payload: object = {}
            elif content_length <= 0 or content_length > 4096:
                self.send_bytes(
                    b'{"error":"Settings request must be between 1 and 4096 bytes."}\n',
                    "application/json",
                    HTTPStatus.BAD_REQUEST,
                )
                return
            else:
                try:
                    payload = json.loads(self.rfile.read(content_length))
                except json.JSONDecodeError as error:
                    body = json.dumps({"error": str(error)}).encode("utf-8")
                    self.send_bytes(body, "application/json", HTTPStatus.BAD_REQUEST)
                    return
            try:
                if path == "/api/settings":
                    settings = validate_runtime_settings(payload)
                    with SETTINGS_LOCK:
                        write_runtime_settings(settings)
                    result: dict[str, Any] = {
                        "settings": settings,
                        "restart_required": True,
                    }
                elif path == "/api/fan/manual":
                    if not isinstance(payload, dict):
                        raise ValueError("Fan test must be a JSON object.")
                    duty = int(payload.get("duty"))
                    duration = int(payload.get("duration_seconds", 25))
                    if duty not in {100, 70, 50, 40}:
                        raise ValueError("Fan test must be 100%, 70%, 50%, or 40%.")
                    if not 20 <= duration <= 30:
                        raise ValueError("Fan test duration must be 20–30 seconds.")
                    fan = fan_status()
                    if not fan.get("healthy"):
                        raise ValueError("Fan and GPU telemetry must be healthy before calibration.")
                    if duty < 100 and not fan.get("gpu_stable"):
                        raise ValueError(
                            "Wait for a stable P100 temperature before reducing fan speed."
                        )
                    calibration = fan.get("calibrated_duties") or {}
                    order = [100, 70, 50, 40]
                    for prior in order[: order.index(duty)]:
                        if not calibration.get(str(prior)):
                            raise ValueError(f"Complete the {prior}% test first.")
                    result = {
                        "command": write_fan_command(
                            "manual", duty=duty, duration_seconds=duration
                        )
                    }
                elif path == "/api/fan/automatic":
                    if not isinstance(payload, dict):
                        raise ValueError("Automatic control request must be a JSON object.")
                    enabled = payload.get("enabled") is True
                    fan = fan_status()
                    if enabled and not fan.get("calibration_complete"):
                        raise ValueError(
                            "Complete the 100%, 70%, 50%, and 40% tests first."
                        )
                    result = {
                        "command": write_fan_command("automatic", enabled=enabled)
                    }
                else:
                    result = {"command": write_fan_command("force_100")}
                body = json.dumps(
                    result,
                    separators=(",", ":"),
                ).encode("utf-8")
                self.send_bytes(body, "application/json")
            except (OSError, json.JSONDecodeError, ValueError) as error:
                body = json.dumps({"error": str(error)}).encode("utf-8")
                self.send_bytes(body, "application/json", HTTPStatus.BAD_REQUEST)
            return

        if not TEST_LOCK.acquire(blocking=False):
            self.send_bytes(
                b'{"error":"A safe test is already running."}\n',
                "application/json",
                HTTPStatus.CONFLICT,
            )
            return

        try:
            result = run_safe_test()
            body = json.dumps(result, separators=(",", ":")).encode("utf-8")
            self.send_bytes(body, "application/json")
        except urllib.error.HTTPError as error:
            body = json.dumps(
                {"error": f"Ollama returned HTTP {error.code}."}
            ).encode("utf-8")
            self.send_bytes(body, "application/json", HTTPStatus.BAD_GATEWAY)
        except (OSError, RuntimeError, urllib.error.URLError, json.JSONDecodeError) as error:
            body = json.dumps(
                {"error": f"Safe test failed: {error}"}
            ).encode("utf-8")
            self.send_bytes(body, "application/json", HTTPStatus.GATEWAY_TIMEOUT)
        finally:
            TEST_LOCK.release()


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"PoCiSys GPU status server listening on {PORT}", flush=True)
    server.serve_forever()
