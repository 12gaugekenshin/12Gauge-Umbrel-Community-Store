#!/usr/bin/env python3
"""Read-only status server for the PoCiSys GPU Runtime."""

from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import threading
import urllib.error
import urllib.request
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


def read_field(name: str, default: str = "") -> str:
    try:
        return (STATUS_DIR / name).read_text(encoding="utf-8").strip()
    except OSError:
        return default


def gpu_status() -> dict[str, Any] | None:
    query = (
        "name,uuid,driver_version,temperature.gpu,power.draw,power.limit,"
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
        values = [item.strip() for item in result.stdout.splitlines()[0].split(",")]
        keys = [
            "name",
            "uuid",
            "driver_version",
            "temperature_c",
            "power_w",
            "power_limit_w",
            "memory_used_mib",
            "memory_total_mib",
            "utilization_percent",
        ]
        return dict(zip(keys, values, strict=False))
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
        "gpu": gpu_status(),
        "ollama": ollama_status(),
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


def run_safe_test() -> dict[str, Any]:
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
        "keep_alive": "2m",
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
        if path != "/api/safe-test":
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
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
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
