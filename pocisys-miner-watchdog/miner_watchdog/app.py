from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs
from urllib.parse import urljoin, urlparse


LOG = logging.getLogger("miner_watchdog")

LUXOS_HTTP_ENDPOINTS = [
    "/",
    "/api",
    "/api/v1/status",
    "/api/v1/summary",
    "/api/v1/chain",
    "/cgi-bin/luci/admin/status",
    "/api/summary",
    "/api/status",
    "/api/metrics",
]

AXEOS_ENDPOINTS = [
    "/",
    "/api/system/info",
    "/api/v1/system/info",
    "/api/system/status",
    "/api/mining/status",
    "/api/status",
    "/api/info",
    "/api/summary",
    "/api/metrics",
]

SECRET_KEY_RE = re.compile(
    r"(password|passwd|token|secret|authorization|auth|cookie|session|key|"
    r"seed|mnemonic|private|credential|apikey|api_key)",
    re.IGNORECASE,
)

POOL_UP_WORDS = {"alive", "active", "connected", "online", "up", "ok", "true", "1"}
POOL_DOWN_WORDS = {"dead", "disconnected", "offline", "down", "false", "0", "error"}


class HttpResponse:
    def __init__(self, status_code: int, headers: dict[str, str], body: bytes) -> None:
        self.status_code = status_code
        self.headers = headers
        self._body = body

    @property
    def text(self) -> str:
        return self._body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)


class HttpClient:
    def get(self, url: str, timeout: float) -> HttpResponse:
        request = urllib.request.Request(url, method="GET")
        return self._open(request, timeout)

    def post_json(self, url: str, payload: dict[str, Any], timeout: float) -> HttpResponse:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "miner-watchdog/0.1"},
        )
        return self._open(request, timeout)

    def _open(self, request: urllib.request.Request, timeout: float) -> HttpResponse:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    status_code=response.status,
                    headers={key.lower(): value for key, value in response.headers.items()},
                    body=response.read(),
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(
                status_code=exc.code,
                headers={key.lower(): value for key, value in exc.headers.items()},
                body=exc.read(),
            )


@dataclass
class MinerConfig:
    name: str
    type: str
    url: str
    min_hashrate_ths: float | None = None
    max_temp_c: float | None = None
    port: int = 4028
    endpoints: list[str] = field(default_factory=list)
    hashrate_unit: str = "auto"


@dataclass
class DiscordConfig:
    webhook_url: str = ""


@dataclass
class WatchdogConfig:
    interval_seconds: int = 60
    unhealthy_reminder_seconds: int = 1800
    status_summary_interval_seconds: int = 1800
    request_timeout_seconds: float = 5.0
    luxos_socket_timeout_seconds: float = 2.0
    debug: bool = False
    send_startup_summary: bool = False
    startup_grace_seconds: int = 0
    alert_existing_blocks_on_startup: bool = False


@dataclass
class AppConfig:
    miners: list[MinerConfig]
    discord: DiscordConfig
    watchdog: WatchdogConfig


@dataclass
class ProbeResult:
    reachable: bool
    kind: str
    endpoint: str
    data: Any = None
    status_code: int | None = None
    content_type: str | None = None
    error: str | None = None


@dataclass
class ParsedStatus:
    miner_name: str
    miner_type: str
    reachable: bool
    parser_ok: bool
    endpoint: str = ""
    hashrate_ths: float | None = None
    temp_c: float | None = None
    pool_connected: bool | None = None
    unhealthy_chips: int | None = None
    chip_healthy: bool | None = None
    uptime: str | None = None
    fan: str | None = None
    best_diff: str | None = None
    block_found: bool | None = None
    block_count: int | None = None
    block_source: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    discovered_keys: list[str] = field(default_factory=list)


def env_expand(value: Any) -> Any:
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        return os.getenv(match.group(1), "")

    return re.sub(r"\$\{([^}]+)\}", replace, value)


def load_config(path: str) -> AppConfig:
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)

    discord_raw = raw.get("discord") or {}
    webhook_url = env_expand(discord_raw.get("webhook_url", ""))
    if not webhook_url:
        webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "")

    watchdog_raw = raw.get("watchdog") or {}
    watchdog = WatchdogConfig(
        interval_seconds=int(watchdog_raw.get("interval_seconds", 60)),
        unhealthy_reminder_seconds=int(watchdog_raw.get("unhealthy_reminder_seconds", 1800)),
        status_summary_interval_seconds=int(
            watchdog_raw.get("status_summary_interval_seconds", 1800)
        ),
        request_timeout_seconds=float(watchdog_raw.get("request_timeout_seconds", 5.0)),
        luxos_socket_timeout_seconds=float(
            watchdog_raw.get("luxos_socket_timeout_seconds", 2.0)
        ),
        debug=bool(watchdog_raw.get("debug", False)),
        send_startup_summary=bool(watchdog_raw.get("send_startup_summary", False)),
        startup_grace_seconds=int(watchdog_raw.get("startup_grace_seconds", 0)),
        alert_existing_blocks_on_startup=bool(
            watchdog_raw.get("alert_existing_blocks_on_startup", False)
        ),
    )

    miners: list[MinerConfig] = []
    for index, item in enumerate(raw.get("miners") or [], start=1):
        miner_type = str(item.get("type", "")).strip().lower()
        if miner_type == "http":
            miner_type = "axeos"
        if miner_type not in {"luxos", "axeos"}:
            raise ValueError(f"Miner #{index} has unsupported type: {miner_type!r}")

        url = str(item.get("url") or item.get("host") or "").strip()
        if not url:
            raise ValueError(f"Miner #{index} is missing url")
        if "://" not in url:
            url = f"http://{url}"

        miners.append(
            MinerConfig(
                name=str(item.get("name") or f"Miner {index}"),
                type=miner_type,
                url=url,
                min_hashrate_ths=optional_float(item.get("min_hashrate_ths")),
                max_temp_c=optional_float(item.get("max_temp_c")),
                port=int(item.get("port", 4028)),
                endpoints=[str(ep) for ep in item.get("endpoints", [])],
                hashrate_unit=str(item.get("hashrate_unit", "auto")).lower(),
            )
        )

    if not miners:
        raise ValueError("Config has no miners")

    return AppConfig(
        miners=miners,
        discord=DiscordConfig(webhook_url=webhook_url),
        watchdog=watchdog,
    )


def optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def host_from_url(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"http://{url}")
    return parsed.hostname or parsed.path.split("/")[0]


def base_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme:
        parsed = urlparse(f"http://{url}")
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def candidate_urls(miner: MinerConfig) -> list[str]:
    defaults = LUXOS_HTTP_ENDPOINTS if miner.type == "luxos" else AXEOS_ENDPOINTS
    endpoints = [""] + miner.endpoints + defaults
    base = base_url(miner.url).rstrip("/")
    candidates: list[str] = []

    def add(candidate: str) -> None:
        if candidate not in candidates:
            candidates.append(candidate)

    add(miner.url)
    for endpoint in endpoints:
        if not endpoint:
            continue
        add(urljoin(base + "/", endpoint.lstrip("/")))
    return candidates


def luxos_command(host: str, port: int, command: str, timeout: float) -> dict[str, Any] | None:
    payload = json.dumps({"command": command}).encode("utf-8") + b"\n"
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(payload)
            sock.shutdown(socket.SHUT_WR)

            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)

        raw = b"".join(chunks).decode("utf-8", errors="ignore")
        raw = raw.replace("\x00", "").strip()
        if not raw:
            return None
        return json.loads(raw)
    except Exception as exc:
        LOG.debug("LuxOS socket command failed host=%s port=%s command=%s error=%s", host, port, command, exc)
        return None


def probe_luxos_socket(miner: MinerConfig, timeout: float) -> ProbeResult | None:
    host = host_from_url(miner.url)
    payload: dict[str, Any] = {}
    for command in ("stats", "summary", "pools"):
        data = luxos_command(host, miner.port, command, timeout)
        if data:
            payload[command] = data
            LOG.debug(
                "%s LuxOS socket %s:%s command=%s -> JSON keys=%s",
                miner.name,
                host,
                miner.port,
                command,
                ", ".join(sorted(data.keys())),
            )

    if payload.get("stats") or payload.get("summary"):
        return ProbeResult(
            reachable=True,
            kind="luxos-socket",
            endpoint=f"{host}:{miner.port} commands=stats,summary,pools",
            data=payload,
        )
    return None


def looks_like_json(content_type: str, text: str) -> bool:
    lower = content_type.lower()
    stripped = text.lstrip()
    return "json" in lower or stripped.startswith("{") or stripped.startswith("[")


def probe_http(miner: MinerConfig, session: HttpClient, cfg: WatchdogConfig) -> ProbeResult:
    first_reachable: ProbeResult | None = None
    last_error = "no endpoints tried"

    for url in candidate_urls(miner):
        try:
            response = session.get(url, timeout=cfg.request_timeout_seconds)
            content_type = response.headers.get("content-type", "")
            LOG.debug(
                "%s HTTP probe %s -> %s %s",
                miner.name,
                url,
                response.status_code,
                content_type or "<no content-type>",
            )

            if first_reachable is None and response.status_code < 500:
                first_reachable = ProbeResult(
                    reachable=True,
                    kind="http",
                    endpoint=url,
                    status_code=response.status_code,
                    content_type=content_type,
                    error="reachable but no JSON status endpoint found yet",
                )

            if response.status_code != 200:
                continue

            text = response.text
            if not looks_like_json(content_type, text):
                continue

            data = response.json()
            if cfg.debug:
                LOG.info(
                    "%s selected JSON endpoint %s sample=%s",
                    miner.name,
                    url,
                    compact_json(sanitize_json(data), limit=900),
                )
            else:
                LOG.info("%s selected JSON endpoint %s", miner.name, url)
            return ProbeResult(
                reachable=True,
                kind="http-json",
                endpoint=url,
                data=data,
                status_code=response.status_code,
                content_type=content_type,
            )
        except Exception as exc:
            last_error = str(exc)
            LOG.debug("%s HTTP probe %s -> error: %s", miner.name, url, exc)

    return first_reachable or ProbeResult(
        reachable=False,
        kind="http",
        endpoint=miner.url,
        error=last_error,
    )


def probe_miner(
    miner: MinerConfig,
    session: HttpClient,
    cfg: WatchdogConfig,
) -> ProbeResult:
    if miner.type == "luxos":
        socket_result = probe_luxos_socket(miner, cfg.luxos_socket_timeout_seconds)
        if socket_result:
            return socket_result
    return probe_http(miner, session, cfg)


def sanitize_json(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "<max-depth>"
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            if SECRET_KEY_RE.search(str(key)):
                clean[str(key)] = "<redacted>"
            else:
                clean[str(key)] = sanitize_json(item, depth + 1)
        return clean
    if isinstance(value, list):
        return [sanitize_json(item, depth + 1) for item in value[:20]]
    if isinstance(value, str) and len(value) > 180:
        return value[:177] + "..."
    return value


def compact_json(value: Any, limit: int = 500) -> str:
    text = json.dumps(value, separators=(",", ":"), ensure_ascii=True)
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def flatten_json(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    rows: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            rows.append((path, item))
            rows.extend(flatten_json(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            path = f"{prefix}[{index}]"
            rows.extend(flatten_json(item, path))
    return rows


def discovered_key_paths(value: Any) -> list[str]:
    paths = []
    for path, item in flatten_json(value):
        if isinstance(item, (dict, list)):
            continue
        paths.append(path)
    return sorted(set(paths))


def parse_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def parse_hashrate_with_unit(value: Any) -> tuple[float, str] | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lower().replace(" ", "")
    match = re.search(r"(\d+(?:\.\d+)?)([kmgtp]?h/s|[kmgtp]?hs|[kmgtp])", text)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2)
    if unit.startswith("th") or unit == "t":
        return number, "string-ths"
    if unit.startswith("gh") or unit == "g":
        return number / 1_000, "string-ghs"
    if unit.startswith("mh") or unit == "m":
        return number / 1_000_000, "string-mhs"
    if unit.startswith("kh") or unit == "k":
        return number / 1_000_000_000, "string-khs"
    if unit.startswith("ph") or unit == "p":
        return number * 1_000, "string-phs"
    if unit.startswith("h"):
        return number / 1_000_000_000_000, "string-hs"
    return None


def convert_hashrate_to_ths(
    value: float,
    key_path: str,
    miner: MinerConfig,
) -> tuple[float, str]:
    unit = miner.hashrate_unit.lower()
    normalized = key_path.lower().replace("_", "").replace(" ", "")

    if unit in {"ths", "th/s", "t"}:
        return value, "configured-ths"
    if unit in {"ghs", "gh/s", "g"}:
        return value / 1_000, "configured-ghs"
    if unit in {"mhs", "mh/s", "m"}:
        return value / 1_000_000, "configured-mhs"
    if unit in {"hs", "h/s", "h"}:
        return value / 1_000_000_000_000, "configured-hs"

    if "ths" in normalized or "th/s" in normalized:
        return value, "key-ths"
    if "ghs" in normalized or "gh/s" in normalized or normalized.endswith("gh"):
        return value / 1_000, "key-ghs"
    if "mhs" in normalized or "mh/s" in normalized or normalized.endswith("mh"):
        return value / 1_000_000, "key-mhs"
    if "khs" in normalized or "kh/s" in normalized:
        return value / 1_000_000_000, "key-khs"
    if "hs" in normalized and value > 1_000_000:
        return value / 1_000_000_000_000, "key-hs"

    if miner.min_hashrate_ths and value < 100:
        low = miner.min_hashrate_ths * 0.25
        high = miner.min_hashrate_ths * 10
        if low <= value <= high:
            return value, "auto-ths-near-threshold"

    if value >= 1_000_000_000:
        return value / 1_000_000_000_000, "auto-hs"
    if value >= 100:
        return value / 1_000, "auto-ghs"
    if miner.type == "axeos":
        return value, "auto-axeos-small-ths"
    return value / 1_000, "auto-ghs-small"


def diff_to_number(value: Any) -> float:
    if value is None:
        return 0.0
    text = str(value).strip().replace(" ", "").upper()
    match = re.match(r"([\d.]+)([KMGTPE]?)", text)
    if not match:
        return 0.0
    number = float(match.group(1))
    suffix = match.group(2)
    return number * {
        "": 1,
        "K": 1_000,
        "M": 1_000_000,
        "G": 1_000_000_000,
        "T": 1_000_000_000_000,
        "P": 1_000_000_000_000_000,
        "E": 1_000_000_000_000_000_000,
    }.get(suffix, 1)


def format_diff(value: Any) -> str:
    number = diff_to_number(value)
    if number <= 0:
        return "--"
    if number >= 1_000_000_000_000:
        return f"{number / 1_000_000_000_000:.2f}T"
    if number >= 1_000_000_000:
        return f"{number / 1_000_000_000:.2f}G"
    if number >= 1_000_000:
        return f"{number / 1_000_000:.2f}M"
    if number >= 1_000:
        return f"{number / 1_000:.2f}K"
    return f"{number:.0f}"


def format_uptime(value: Any) -> str | None:
    seconds = parse_number(value)
    if seconds is None:
        return str(value) if value not in (None, "") else None
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3_600)
    minutes = seconds // 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def find_best_diff(obj: Any) -> str | None:
    best = 0.0
    for path, value in flatten_json(obj):
        key = path.lower()
        if "best" in key and ("diff" in key or "share" in key):
            best = max(best, diff_to_number(value))
    return format_diff(best) if best > 0 else None


def find_block_signal(obj: Any) -> tuple[bool | None, int | None, str | None]:
    count_candidates: list[tuple[int, str]] = []
    bool_candidates: list[tuple[bool, str]] = []

    for path, value in flatten_json(obj):
        lower = path.lower().replace("-", "_")
        compact = lower.replace("_", "").replace(".", "")
        blockish = "block" in compact
        foundish = any(word in compact for word in ("found", "solved", "hit", "accepted", "valid"))

        if blockish and foundish:
            if isinstance(value, bool):
                bool_candidates.append((value, path))
                continue

            number = parse_number(value)
            if number is not None:
                count_candidates.append((int(number), path))
                continue

            text = str(value).strip().lower()
            if text in {"true", "yes", "found", "solved", "hit", "accepted"}:
                bool_candidates.append((True, path))
            elif text in {"false", "no", "none", "0"}:
                bool_candidates.append((False, path))

        if compact in {"blocks", "blockcount"} and foundish:
            number = parse_number(value)
            if number is not None:
                count_candidates.append((int(number), path))

    if count_candidates:
        count, path = max(count_candidates, key=lambda item: item[0])
        return count > 0, count, path
    if bool_candidates:
        value, path = bool_candidates[0]
        return value, None, path
    return None, None, None


def parse_pool_connected(obj: Any) -> bool | None:
    signals: list[bool] = []
    for path, value in flatten_json(obj):
        lower_path = path.lower()
        if not any(word in lower_path for word in ("pool", "stratum", "alive", "connected")):
            continue
        if isinstance(value, bool):
            if any(word in lower_path for word in ("alive", "connected", "active", "pool", "stratum")):
                signals.append(value)
            continue
        if isinstance(value, (str, int, float)):
            text = str(value).strip().lower()
            if text in POOL_UP_WORDS:
                signals.append(True)
            elif text in POOL_DOWN_WORDS:
                signals.append(False)

    if any(signals):
        return True
    if signals and not any(signals):
        return False
    return None


def parse_unhealthy_chips(obj: Any) -> tuple[int | None, bool | None]:
    unhealthy_counts: list[int] = []
    health_signals: list[bool] = []

    for path, value in flatten_json(obj):
        lower = path.lower()
        numeric = parse_number(value)

        if any(term in lower for term in ("unhealthy_chips", "bad_chips", "dead_chips")):
            if numeric is not None:
                unhealthy_counts.append(int(numeric))
            continue

        if "chain_status" in lower or "hashboard" in lower or "board" in lower:
            if isinstance(value, str):
                text = value.strip().lower()
                if text in {"ok", "alive", "good", "normal", "running", "healthy"}:
                    health_signals.append(True)
                elif text in {"bad", "dead", "error", "fault", "missing", "offline"}:
                    health_signals.append(False)

        if "chips" in lower and numeric is not None and any(
            word in lower for word in ("bad", "dead", "unhealthy", "missing")
        ):
            unhealthy_counts.append(int(numeric))

    if unhealthy_counts:
        count = max(unhealthy_counts)
        return count, count == 0
    if health_signals:
        return None, all(health_signals)
    return None, None


def parse_generic_status(obj: Any, miner: MinerConfig, endpoint: str) -> ParsedStatus:
    keys = discovered_key_paths(obj)
    rows = flatten_json(obj)

    hashrate_candidates: list[tuple[int, float, str, str]] = []
    for path, value in rows:
        lower = path.lower()
        normalized = lower.replace("_", "").replace(" ", "")
        if not any(
            token in normalized
            for token in (
                "hashrate",
                "hashrate10m",
                "hashrate30m",
                "mhs",
                "ghs",
                "ths",
                "chainrate",
            )
        ):
            continue

        parsed_with_unit = parse_hashrate_with_unit(value)
        if parsed_with_unit:
            ths, unit_note = parsed_with_unit
        else:
            number = parse_number(value)
            if number is None:
                continue
            ths, unit_note = convert_hashrate_to_ths(number, path, miner)

        score = 10
        if any(word in normalized for word in ("avg", "average", "10m", "30m")):
            score += 10
        if any(word in normalized for word in ("ideal", "target", "max")):
            score -= 10
        hashrate_candidates.append((score, ths, path, unit_note))

    hashrate_ths = None
    hashrate_source = None
    if hashrate_candidates:
        hashrate_candidates.sort(reverse=True, key=lambda item: item[0])
        _, hashrate_ths, hashrate_source, unit_note = hashrate_candidates[0]
        hashrate_source = f"{hashrate_source} ({unit_note})"

    temps: list[tuple[float, str]] = []
    for path, value in rows:
        lower = path.lower()
        if "temp" not in lower and "temperature" not in lower:
            continue
        if any(
            term in lower
            for term in (
                "target",
                "threshold",
                "limit",
                "overheat",
                "pid",
                "default",
                "max",
                "min",
            )
        ):
            continue
        if isinstance(value, list):
            for item in value:
                number = parse_number(item)
                if number is not None and 0 <= number <= 200:
                    temps.append((number, path))
        else:
            number = parse_number(value)
            if number is not None and 0 <= number <= 200:
                temps.append((number, path))
    temp_c = max((item[0] for item in temps), default=None)

    unhealthy_chips, chip_healthy = parse_unhealthy_chips(obj)
    pool_connected = parse_pool_connected(obj)
    best_diff = find_best_diff(obj)
    block_found, block_count, block_source = find_block_signal(obj)

    uptime = None
    fan = None
    extras: dict[str, Any] = {}
    for path, value in rows:
        lower = path.lower()
        if uptime is None and any(term in lower for term in ("uptime", "elapsed")):
            uptime = format_uptime(value)
        if fan is None and any(term in lower for term in ("fanrpm", "fan_rpm", "fanspeed", "fan")):
            if not isinstance(value, (dict, list)):
                fan = str(value)
        if "volt" in lower and "voltage" not in extras:
            if not isinstance(value, (dict, list)):
                extras["voltage"] = value
        if any(term in lower for term in ("watt", "power")) and "watts" not in extras:
            if not isinstance(value, (dict, list)):
                extras["watts"] = value
        if "share" in lower and not isinstance(value, (dict, list)):
            if "accepted" in lower:
                extras["shares_accepted"] = value
            elif "reject" in lower or "rejected" in lower:
                extras["shares_rejected"] = value

    if hashrate_source:
        extras["hashrate_source"] = hashrate_source

    parser_ok = any(
        item is not None
        for item in (hashrate_ths, temp_c, pool_connected, unhealthy_chips, chip_healthy, uptime)
    )

    return ParsedStatus(
        miner_name=miner.name,
        miner_type=miner.type,
        reachable=True,
        parser_ok=parser_ok,
        endpoint=endpoint,
        hashrate_ths=hashrate_ths,
        temp_c=temp_c,
        pool_connected=pool_connected,
        unhealthy_chips=unhealthy_chips,
        chip_healthy=chip_healthy,
        uptime=uptime,
        fan=fan,
        best_diff=best_diff,
        block_found=block_found,
        block_count=block_count,
        block_source=block_source,
        extras=extras,
        discovered_keys=keys,
        error=None if parser_ok else "reachable but parser could not find status fields",
    )


def merge_luxos_stats(stats_payload: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for item in stats_payload.get("STATS", []):
        if isinstance(item, dict):
            merged.update(item)
    return merged


def parse_luxos_socket_status(payload: dict[str, Any], miner: MinerConfig, endpoint: str) -> ParsedStatus:
    stats = payload.get("stats") or {}
    summary = payload.get("summary") or {}
    pools = payload.get("pools") or {}

    useful = merge_luxos_stats(stats)
    chain_rates: list[float] = []
    for key, value in useful.items():
        if str(key).lower().startswith("chain_rate"):
            number = parse_number(value)
            if number is not None:
                chain_rates.append(number)

    hashrate_ths = None
    hashrate_source = None
    if chain_rates:
        hashrate_ths = sum(chain_rates) / 1_000
        hashrate_source = "LuxOS chain_rate* (ghs)"
    else:
        for key in ("GHS 5s", "GHS av", "MHS 5s", "MHS av", "THS 5s", "THS av"):
            if key in useful:
                number = parse_number(useful[key])
                if number is None:
                    continue
                hashrate_ths, unit_note = convert_hashrate_to_ths(number, key, miner)
                hashrate_source = f"LuxOS {key} ({unit_note})"
                break

    temps = []
    for key, value in useful.items():
        if "temp" in str(key).lower():
            number = parse_number(value)
            if number is not None and 0 <= number <= 200:
                temps.append(number)
    temp_c = max(temps) if temps else None

    fans = []
    for key, value in useful.items():
        if str(key).lower().startswith("fan"):
            number = parse_number(value)
            if number is not None:
                fans.append(int(number))

    best_diff = find_best_diff(payload)
    block_found, block_count, block_source = find_block_signal(payload)
    unhealthy_chips, chip_healthy = parse_unhealthy_chips(payload)
    pool_connected = parse_pool_connected(pools)
    if pool_connected is None:
        pool_connected = parse_pool_connected(summary)

    obj_for_debug = {"stats": stats, "summary": summary, "pools": pools}
    generic = parse_generic_status(obj_for_debug, miner, endpoint)
    extras = dict(generic.extras)
    if hashrate_source:
        extras["hashrate_source"] = hashrate_source

    return ParsedStatus(
        miner_name=miner.name,
        miner_type=miner.type,
        reachable=True,
        parser_ok=True,
        endpoint=endpoint,
        hashrate_ths=hashrate_ths if hashrate_ths is not None else generic.hashrate_ths,
        temp_c=temp_c if temp_c is not None else generic.temp_c,
        pool_connected=pool_connected if pool_connected is not None else generic.pool_connected,
        unhealthy_chips=unhealthy_chips if unhealthy_chips is not None else generic.unhealthy_chips,
        chip_healthy=chip_healthy if chip_healthy is not None else generic.chip_healthy,
        uptime=format_uptime(useful.get("Elapsed") or useful.get("elapsed") or useful.get("Uptime"))
        or generic.uptime,
        fan=str(max(fans)) if fans else generic.fan,
        best_diff=best_diff or generic.best_diff,
        block_found=block_found if block_found is not None else generic.block_found,
        block_count=block_count if block_count is not None else generic.block_count,
        block_source=block_source or generic.block_source,
        extras=extras,
        discovered_keys=generic.discovered_keys,
    )


def parse_probe_result(probe: ProbeResult, miner: MinerConfig) -> ParsedStatus:
    if not probe.reachable:
        return ParsedStatus(
            miner_name=miner.name,
            miner_type=miner.type,
            reachable=False,
            parser_ok=False,
            endpoint=probe.endpoint,
            error=probe.error or "miner API unreachable",
        )
    if probe.kind == "luxos-socket" and isinstance(probe.data, dict):
        return parse_luxos_socket_status(probe.data, miner, probe.endpoint)
    if probe.data is not None:
        return parse_generic_status(probe.data, miner, probe.endpoint)
    return ParsedStatus(
        miner_name=miner.name,
        miner_type=miner.type,
        reachable=True,
        parser_ok=False,
        endpoint=probe.endpoint,
        error=probe.error or "reachable but no JSON status endpoint found",
    )


def issue_list(miner: MinerConfig, status: ParsedStatus) -> list[str]:
    issues: list[str] = []
    if not status.reachable:
        return ["api_unreachable"]
    if not status.parser_ok:
        issues.append("parser_failed")
    if miner.min_hashrate_ths is not None:
        if status.hashrate_ths is None:
            issues.append("hashrate_missing")
        elif status.hashrate_ths < miner.min_hashrate_ths:
            issues.append("hashrate_low")
    if miner.max_temp_c is not None and status.temp_c is not None:
        if status.temp_c > miner.max_temp_c:
            issues.append("temp_high")
    if status.pool_connected is False:
        issues.append("pool_disconnected")
    if status.unhealthy_chips is not None and status.unhealthy_chips > 0:
        issues.append("unhealthy_chips")
    elif status.chip_healthy is False:
        issues.append("chip_health_bad")
    return issues


def fingerprint(issues: list[str]) -> str:
    return "OK" if not issues else "|".join(sorted(issues))


def status_lines(miner: MinerConfig, status: ParsedStatus, issues: list[str]) -> list[str]:
    lines = [
        f"Miner: {miner.name} ({miner.type})",
        f"Endpoint: {status.endpoint or miner.url}",
        f"State: {'OK' if not issues else ', '.join(issues)}",
    ]
    if status.error:
        lines.append(f"Error: {status.error}")
    if status.hashrate_ths is not None:
        threshold = (
            f" / min {miner.min_hashrate_ths:.3f}"
            if miner.min_hashrate_ths is not None
            else ""
        )
        lines.append(f"Hashrate: {status.hashrate_ths:.3f} TH/s{threshold}")
    if status.temp_c is not None:
        threshold = f" / max {miner.max_temp_c:.1f}" if miner.max_temp_c is not None else ""
        lines.append(f"Temp: {status.temp_c:.1f} C{threshold}")
    if status.pool_connected is not None:
        lines.append(f"Pool: {'connected' if status.pool_connected else 'disconnected'}")
    if status.unhealthy_chips is not None:
        lines.append(f"Unhealthy chips: {status.unhealthy_chips}")
    elif status.chip_healthy is not None:
        lines.append(f"Chip health: {'healthy' if status.chip_healthy else 'bad'}")
    if status.fan:
        lines.append(f"Fan: {status.fan}")
    if status.uptime:
        lines.append(f"Uptime: {status.uptime}")
    if status.best_diff:
        lines.append(f"Best diff: {status.best_diff}")
    if status.block_count is not None:
        source = f" from {status.block_source}" if status.block_source else ""
        lines.append(f"Blocks found: {status.block_count}{source}")
    elif status.block_found is not None:
        source = f" from {status.block_source}" if status.block_source else ""
        lines.append(f"Block signal: {'found' if status.block_found else 'not found'}{source}")
    if status.extras:
        bits = [f"{key}={value}" for key, value in sorted(status.extras.items()) if value is not None]
        if bits:
            lines.append("Extra: " + ", ".join(bits[:8]))
    return lines


class DiscordWebhook:
    def __init__(self, webhook_url: str, session: HttpClient) -> None:
        self.webhook_url = webhook_url
        self.session = session

    def send(self, content: str) -> None:
        if not self.webhook_url:
            LOG.warning("Discord webhook not configured; would send: %s", content)
            return

        if len(content) > 1900:
            content = content[:1897] + "..."

        response = self.session.post_json(self.webhook_url, {"content": content}, timeout=10)
        if response.status_code not in {200, 204}:
            raise RuntimeError(f"Discord webhook returned HTTP {response.status_code}: {response.text[:200]}")


def ensure_config_exists(config_path: str, template_path: str | None = None) -> None:
    if os.path.exists(config_path):
        return

    os.makedirs(os.path.dirname(os.path.abspath(config_path)), exist_ok=True)
    if template_path and os.path.exists(template_path):
        shutil.copyfile(template_path, config_path)
        LOG.info("Created config from template: %s", config_path)
        return

    raise FileNotFoundError(
        f"Config file does not exist: {config_path}. Provide config.yml or CONFIG_TEMPLATE_PATH."
    )


def update_webhook_config(config_path: str, webhook_url: str) -> None:
    with open(config_path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    raw.setdefault("discord", {})["webhook_url"] = webhook_url
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump(raw, handle, indent=2)


def redacted_webhook(url: str) -> str:
    if not url:
        return "not configured"
    if len(url) <= 18:
        return "<configured>"
    return f"{url[:32]}...{url[-8:]}"


def issue_class(issues: list[str]) -> str:
    return "ok" if not issues else "bad"


def render_status_page(watchdog: "Watchdog", notice: str = "") -> bytes:
    with watchdog.results_lock:
        results = list(watchdog.latest_results)
        checked_at = watchdog.latest_checked_at

    rows = []
    for miner, status, issues in results:
        state = "OK" if not issues else ", ".join(issues)
        hashrate = f"{status.hashrate_ths:.3f} TH/s" if status.hashrate_ths is not None else "?"
        temp = f"{status.temp_c:.1f} C" if status.temp_c is not None else "?"
        pool = (
            "connected"
            if status.pool_connected is True
            else "disconnected"
            if status.pool_connected is False
            else "?"
        )
        blocks = (
            str(status.block_count)
            if status.block_count is not None
            else "yes"
            if status.block_found is True
            else "0"
            if status.block_found is False
            else "?"
        )
        rows.append(
            "<tr>"
            f"<td>{html.escape(miner.name)}</td>"
            f"<td class='{issue_class(issues)}'>{html.escape(state)}</td>"
            f"<td>{html.escape(hashrate)}</td>"
            f"<td>{html.escape(temp)}</td>"
            f"<td>{html.escape(pool)}</td>"
            f"<td>{html.escape(blocks)}</td>"
            f"<td>{html.escape(status.endpoint or miner.url)}</td>"
            "</tr>"
        )

    checked_text = (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(checked_at))
        if checked_at
        else "not checked yet"
    )
    webhook = redacted_webhook(watchdog.config.discord.webhook_url)
    notice_html = f"<p class='notice'>{html.escape(notice)}</p>" if notice else ""
    rows_html = "\n".join(rows) or "<tr><td colspan='7'>No status yet. Wait for the next poll.</td></tr>"

    page = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Miner Watchdog</title>
  <style>
    body {{ margin: 0; font-family: system-ui, sans-serif; background: #101114; color: #f5f5f5; }}
    main {{ max-width: 1100px; margin: 0 auto; padding: 28px; }}
    h1 {{ margin: 0 0 6px; font-size: 28px; }}
    p {{ color: #b8bdc7; }}
    table {{ width: 100%; border-collapse: collapse; margin: 22px 0; background: #17191f; }}
    th, td {{ text-align: left; padding: 10px 12px; border-bottom: 1px solid #2a2d36; }}
    th {{ color: #aeb7c8; font-weight: 600; }}
    input {{ width: min(680px, 100%); padding: 10px; border-radius: 6px; border: 1px solid #3a3e49; background: #0b0c10; color: #fff; }}
    button {{ padding: 10px 14px; margin-top: 10px; border: 0; border-radius: 6px; background: #46d369; color: #071008; font-weight: 700; cursor: pointer; }}
    form {{ margin: 18px 0; }}
    .ok {{ color: #55e073; font-weight: 700; }}
    .bad {{ color: #ff6b6b; font-weight: 700; }}
    .notice {{ color: #55e073; }}
    .muted {{ color: #8e95a3; }}
  </style>
</head>
<body>
<main>
  <h1>Miner Watchdog</h1>
  <p>Last pull: {html.escape(checked_text)}. Discord webhook: {html.escape(webhook)}.</p>
  {notice_html}
  <table>
    <thead>
      <tr><th>Miner</th><th>State</th><th>Hashrate</th><th>Temp</th><th>Pool</th><th>Blocks</th><th>Endpoint</th></tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>
  <h2>Discord Webhook</h2>
  <p class="muted">Paste your incoming Discord webhook here. It is saved only inside Umbrel app data, not in the public GitHub repo.</p>
  <form method="post" action="/webhook">
    <input type="password" name="webhook_url" placeholder="https://discord.com/api/webhooks/...">
    <br>
    <button type="submit">Save Webhook</button>
  </form>
  <form method="post" action="/test-discord">
    <button type="submit">Send Test Discord Message</button>
  </form>
</main>
</body>
</html>"""
    return page.encode("utf-8")


def start_web_server(watchdog: "Watchdog", host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            LOG.info("web: " + format, *args)

        def send_html(self, body: bytes, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/health.json":
                with watchdog.results_lock:
                    payload = [
                        {
                            "name": miner.name,
                            "type": miner.type,
                            "issues": issues,
                            "hashrate_ths": status.hashrate_ths,
                            "temp_c": status.temp_c,
                            "pool_connected": status.pool_connected,
                            "block_count": status.block_count,
                            "block_found": status.block_found,
                            "endpoint": status.endpoint,
                        }
                        for miner, status, issues in watchdog.latest_results
                    ]
                data = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            self.send_html(render_status_page(watchdog))

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            fields = parse_qs(body)

            if self.path == "/webhook":
                webhook_url = (fields.get("webhook_url") or [""])[0].strip()
                if not webhook_url.startswith("https://discord.com/api/webhooks/"):
                    self.send_html(render_status_page(watchdog, "Webhook was not saved: invalid Discord webhook URL."), 400)
                    return
                if not watchdog.config_path:
                    self.send_html(render_status_page(watchdog, "Webhook was not saved: config path unavailable."), 500)
                    return
                update_webhook_config(watchdog.config_path, webhook_url)
                watchdog.config.discord.webhook_url = webhook_url
                watchdog.discord.webhook_url = webhook_url
                self.send_html(render_status_page(watchdog, "Webhook saved."))
                return

            if self.path == "/test-discord":
                try:
                    watchdog.discord.send("Miner Watchdog test message: Discord webhook is working.")
                    self.send_html(render_status_page(watchdog, "Test Discord message sent."))
                except Exception as exc:
                    self.send_html(render_status_page(watchdog, f"Test failed: {exc}"), 500)
                return

            self.send_html(render_status_page(watchdog), 404)

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    LOG.info("Web UI listening on http://%s:%s", host, port)
    return server


class Watchdog:
    def __init__(self, config: AppConfig, config_path: str | None = None) -> None:
        self.config = config
        self.config_path = config_path
        self.session = HttpClient()
        self.discord = DiscordWebhook(config.discord.webhook_url, self.session)
        self.last_fingerprint: dict[str, str] = {}
        self.last_issue_alert_at: dict[tuple[str, str], float] = {}
        self.last_active_issues: dict[str, set[str]] = {}
        self.last_block_count: dict[str, int] = {}
        self.last_block_flag: dict[str, bool] = {}
        self.last_summary_at = time.time()
        self.latest_results: list[tuple[MinerConfig, ParsedStatus, list[str]]] = []
        self.latest_checked_at: float | None = None
        self.results_lock = threading.Lock()
        self.started_at = time.monotonic()

    def check_all(self, send_alerts: bool = True) -> list[tuple[MinerConfig, ParsedStatus, list[str]]]:
        results: list[tuple[MinerConfig, ParsedStatus, list[str]]] = []
        for miner in self.config.miners:
            LOG.info("Checking %s at %s", miner.name, miner.url)
            probe = probe_miner(miner, self.session, self.config.watchdog)
            status = parse_probe_result(probe, miner)

            if self.config.watchdog.debug and status.discovered_keys:
                LOG.info("%s discovered JSON keys: %s", miner.name, ", ".join(status.discovered_keys))

            issues = issue_list(miner, status)
            results.append((miner, status, issues))
            LOG.info("%s summary: %s", miner.name, " | ".join(status_lines(miner, status, issues)))

            if send_alerts:
                self.maybe_block_alert(miner, status)
                self.maybe_alert(miner, status, issues)
        with self.results_lock:
            self.latest_results = results
            self.latest_checked_at = time.time()
        return results

    def maybe_block_alert(self, miner: MinerConfig, status: ParsedStatus) -> None:
        if status.block_count is not None:
            previous = self.last_block_count.get(miner.name)
            self.last_block_count[miner.name] = status.block_count

            if previous is None:
                if status.block_count > 0 and self.config.watchdog.alert_existing_blocks_on_startup:
                    self.send_block_alert(miner, status, "existing block count observed on startup")
                return

            if status.block_count > previous:
                self.send_block_alert(
                    miner,
                    status,
                    f"block count increased from {previous} to {status.block_count}",
                )
            return

        if status.block_found is None:
            return

        previous_flag = self.last_block_flag.get(miner.name)
        self.last_block_flag[miner.name] = bool(status.block_found)

        if status.block_found and previous_flag is None:
            if self.config.watchdog.alert_existing_blocks_on_startup:
                self.send_block_alert(miner, status, "block-found signal already true on startup")
        elif status.block_found and previous_flag is False:
            self.send_block_alert(miner, status, "block-found signal changed to true")

    def send_block_alert(self, miner: MinerConfig, status: ParsedStatus, reason: str) -> None:
        message = "BLOCK HIT: miner/pool reports a block event\n"
        message += f"Reason: {reason}\n"
        message += "\n".join(status_lines(miner, status, []))
        self.discord.send(message)

    def maybe_alert(self, miner: MinerConfig, status: ParsedStatus, issues: list[str]) -> None:
        grace = self.config.watchdog.startup_grace_seconds
        if grace and time.monotonic() - self.started_at < grace:
            LOG.info("%s in startup grace period; suppressing alert", miner.name)
            self.last_fingerprint[miner.name] = fingerprint(issues)
            self.last_active_issues[miner.name] = set(issues)
            return

        now = time.time()
        current_issues = set(issues)
        previous_issues = self.last_active_issues.get(miner.name, set())
        self.last_fingerprint[miner.name] = fingerprint(issues)
        self.last_active_issues[miner.name] = current_issues

        if not current_issues:
            if previous_issues:
                message = "RECOVERY: miner returned to normal\n" + "\n".join(
                    status_lines(miner, status, issues)
                )
                self.discord.send(message)
                for issue in previous_issues:
                    self.last_issue_alert_at.pop((miner.name, issue), None)
            return

        new_issues = sorted(current_issues - previous_issues)
        if new_issues:
            message = "ALERT: miner issue detected\n"
            message += "New issue(s): " + ", ".join(new_issues) + "\n"
            message += "\n".join(status_lines(miner, status, issues))
            self.discord.send(message)
            for issue in new_issues:
                self.last_issue_alert_at[(miner.name, issue)] = now

        reminder_seconds = self.config.watchdog.unhealthy_reminder_seconds
        if reminder_seconds <= 0:
            return

        due_issues = []
        for issue in sorted(current_issues):
            if issue in new_issues:
                continue
            key = (miner.name, issue)
            last_issue_alert = self.last_issue_alert_at.get(key, 0)
            if now - last_issue_alert >= reminder_seconds:
                due_issues.append(issue)

        if due_issues:
            label = "STILL DOWN" if "api_unreachable" in due_issues else "STILL UNHEALTHY"
            message = f"{label}: reminder\n"
            message += "Active issue(s): " + ", ".join(due_issues) + "\n"
            message += "\n".join(status_lines(miner, status, issues))
            self.discord.send(message)
            for issue in due_issues:
                self.last_issue_alert_at[(miner.name, issue)] = now

    def send_summary(self, results: list[tuple[MinerConfig, ParsedStatus, list[str]]]) -> None:
        parts = ["Miner watchdog status summary"]
        for miner, status, issues in results:
            state = "OK" if not issues else ", ".join(issues)
            hashrate = f"{status.hashrate_ths:.3f} TH/s" if status.hashrate_ths is not None else "hashrate ?"
            temp = f"{status.temp_c:.1f} C" if status.temp_c is not None else "temp ?"
            pool = (
                "pool connected"
                if status.pool_connected is True
                else "pool disconnected"
                if status.pool_connected is False
                else "pool ?"
            )
            parts.append(f"- {miner.name}: {state}; {hashrate}; {temp}; {pool}")
        self.discord.send("\n".join(parts))

    def run_forever(self) -> None:
        LOG.info("Starting miner watchdog with %d miner(s)", len(self.config.miners))
        first = True
        while True:
            try:
                results = self.check_all(send_alerts=True)
                if first and self.config.watchdog.send_startup_summary:
                    self.send_summary(results)
                    self.last_summary_at = time.time()
                elif self.summary_due():
                    self.send_summary(results)
                    self.last_summary_at = time.time()
                first = False
            except Exception:
                LOG.exception("Watchdog loop failed")
            time.sleep(self.config.watchdog.interval_seconds)

    def summary_due(self) -> bool:
        interval = self.config.watchdog.status_summary_interval_seconds
        if interval <= 0:
            return False
        return time.time() - self.last_summary_at >= interval


def configure_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="UmbrelOS miner Discord alert watchdog")
    parser.add_argument(
        "--config",
        default=os.getenv("CONFIG_PATH", "config.yml"),
        help="Path to config.yml. Defaults to CONFIG_PATH or ./config.yml.",
    )
    parser.add_argument(
        "--status-once",
        action="store_true",
        help="Probe miners once, print a status summary, then exit.",
    )
    parser.add_argument(
        "--send-status",
        action="store_true",
        help="With --status-once, also send the summary to Discord.",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        default=os.getenv("WEB_UI", "").lower() in {"1", "true", "yes", "on"},
        help="Enable the lightweight setup/status web UI.",
    )
    parser.add_argument(
        "--web-host",
        default=os.getenv("WEB_HOST", "0.0.0.0"),
        help="Host for the web UI. Defaults to WEB_HOST or 0.0.0.0.",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=int(os.getenv("WEB_PORT", "8787")),
        help="Port for the web UI. Defaults to WEB_PORT or 8787.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv or sys.argv[1:])
    ensure_config_exists(args.config, os.getenv("CONFIG_TEMPLATE_PATH"))
    config = load_config(args.config)
    configure_logging(config.watchdog.debug)

    if not config.discord.webhook_url:
        LOG.warning("No Discord webhook configured; alerts will be logged but not sent")

    watchdog = Watchdog(config, config_path=args.config)
    if args.web:
        start_web_server(watchdog, args.web_host, args.web_port)

    if args.status_once:
        results = watchdog.check_all(send_alerts=False)
        print("\nMiner watchdog status summary")
        for miner, status, issues in results:
            print("")
            print("\n".join(status_lines(miner, status, issues)))
        if args.send_status:
            watchdog.send_summary(results)
        return

    watchdog.run_forever()
