from __future__ import annotations

import hashlib
import hmac
import json
import secrets


MCP_PROTOCOL_VERSION = "2025-06-18"


def sanitize_miner(item):
    """Return useful telemetry without network locations or payout identities."""
    pool = item.get("pool") if isinstance(item.get("pool"), dict) else {}
    return {
        key: item.get(key)
        for key in (
            "id",
            "name",
            "type",
            "group",
            "online",
            "api_ok",
            "ping_ms",
            "hashrate_ths",
            "expected_hashrate_ths",
            "temps",
            "chip_health",
            "fans",
            "shares",
            "difficulty",
            "uptime_seconds",
            "firmware",
            "frequency_mhz",
            "voltage_mv",
            "wifi_rssi",
            "hardware_errors",
            "blocks_found",
            "status",
            "warnings",
            "checked_at",
        )
        if key in item
    } | {
        "pool": {
            key: pool.get(key)
            for key in ("connected", "status", "source")
            if key in pool
        }
    }


def sanitize_pool(item):
    """Return pool performance while excluding API URLs and local log paths."""
    return {
        key: item.get(key)
        for key in (
            "name",
            "mode",
            "enabled",
            "available",
            "message",
            "total_hashrate_ths",
            "block_height",
            "total_miners",
            "blocks_found",
            "best_difficulty",
            "workers_count",
            "workers",
            "address_detected",
            "size_bytes",
            "updated_at",
        )
        if key in item
    }


def create_connection_token():
    return f"pocisys_{secrets.token_urlsafe(32)}"


def token_digest(token: str):
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def token_matches(token: str, expected_digest: str):
    if not token or not expected_digest:
        return False
    return hmac.compare_digest(token_digest(token), str(expected_digest))


def _tool(name, description, properties=None, required=None):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties or {},
            "required": required or [],
            "additionalProperties": False,
        },
        "annotations": {
            "title": description.split(".", 1)[0],
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    }


TOOLS = [
    _tool(
        "get_pocisys_overview",
        "Get a compact read-only overview of the Umbrel host, miner fleet, and configured local pools.",
    ),
    _tool(
        "list_pocisys_miners",
        "List current read-only health and performance telemetry for every configured miner.",
        {"group": {"type": "string", "description": "Optional exact miner group name."}},
    ),
    _tool(
        "get_pocisys_miner",
        "Get detailed current telemetry for one configured miner by its name or internal ID.",
        {"miner": {"type": "string", "description": "Configured miner name or ID."}},
        ["miner"],
    ),
    _tool(
        "get_pocisys_pools",
        "Get current read-only Public Pool and local pool status, workers, hashrate, best difficulty, and block height.",
    ),
    _tool(
        "get_pocisys_block_odds",
        "Get PoCiSys fleet block-odds estimates for supported SHA-256 networks.",
        {
            "coin": {
                "type": "string",
                "enum": ["BTC", "BCH", "BSV", "XEC", "DGB", "CHTA"],
                "description": "Optional network symbol. Omit to return every enabled network.",
            }
        },
    ),
    _tool(
        "get_pocisys_system_health",
        "Get read-only CPU, memory, disk, load-average, and uptime metrics visible to the PoCiSys Umbrel container.",
    ),
]


class HermesMcpService:
    def __init__(
        self,
        version: str,
        overview_provider,
        miners_provider,
        pools_provider,
        odds_provider,
        system_provider,
    ):
        self.version = version
        self.overview_provider = overview_provider
        self.miners_provider = miners_provider
        self.pools_provider = pools_provider
        self.odds_provider = odds_provider
        self.system_provider = system_provider

    @staticmethod
    def _result(request_id, result):
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id, code, message):
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    @staticmethod
    def _tool_result(payload, is_error=False):
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        result = {"content": [{"type": "text", "text": text}], "isError": bool(is_error)}
        if not is_error and isinstance(payload, dict):
            result["structuredContent"] = payload
        return result

    def _call_tool(self, name, arguments):
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object")
        if name == "get_pocisys_overview":
            return self.overview_provider()
        if name == "list_pocisys_miners":
            miners = list(self.miners_provider())
            group = str(arguments.get("group") or "").strip()
            if group:
                miners = [item for item in miners if str(item.get("group") or "").casefold() == group.casefold()]
            return {"miners": miners, "count": len(miners)}
        if name == "get_pocisys_miner":
            query = str(arguments.get("miner") or "").strip()
            if not query:
                raise ValueError("miner is required")
            match = next(
                (
                    item
                    for item in self.miners_provider()
                    if query.casefold() in {
                        str(item.get("id") or "").casefold(),
                        str(item.get("name") or "").casefold(),
                    }
                ),
                None,
            )
            if not match:
                raise ValueError(f"No configured miner matched '{query}'")
            return {"miner": match}
        if name == "get_pocisys_pools":
            pools = list(self.pools_provider())
            return {"pools": pools, "count": len(pools)}
        if name == "get_pocisys_block_odds":
            payload = self.odds_provider()
            coin = str(arguments.get("coin") or "").upper()
            if not coin:
                return payload
            if isinstance(payload, dict):
                direct = payload.get(coin.lower())
                if isinstance(direct, dict) and direct.get("enabled", True):
                    return {"coin": direct}
                candidates = payload.get("coins") if isinstance(payload.get("coins"), list) else payload.get("networks")
                if isinstance(candidates, list):
                    match = next(
                        (item for item in candidates if str(item.get("symbol") or item.get("coin") or "").upper() == coin),
                        None,
                    )
                    if match:
                        return {"coin": match}
            raise ValueError(f"No enabled odds data is available for {coin}")
        if name == "get_pocisys_system_health":
            return {"system": self.system_provider()}
        raise KeyError(name)

    def _handle_one(self, request):
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return self._error(request.get("id") if isinstance(request, dict) else None, -32600, "Invalid Request")
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}

        if method == "initialize":
            requested = params.get("protocolVersion") if isinstance(params, dict) else None
            return self._result(
                request_id,
                {
                    "protocolVersion": requested or MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "pocisys-hash-monitor", "version": self.version},
                    "instructions": (
                        "PoCiSys exposes current read-only host, miner, pool, and block-odds telemetry. "
                        "It cannot change miners, pools, Umbrel apps, files, wallets, or system configuration."
                    ),
                },
            )
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return None
        if method == "ping":
            return self._result(request_id, {})
        if method == "tools/list":
            return self._result(request_id, {"tools": TOOLS})
        if method == "tools/call":
            if not isinstance(params, dict):
                return self._error(request_id, -32602, "Invalid params")
            name = params.get("name")
            try:
                payload = self._call_tool(name, params.get("arguments") or {})
                return self._result(request_id, self._tool_result(payload))
            except KeyError:
                return self._error(request_id, -32601, f"Unknown tool: {name}")
            except (TypeError, ValueError) as exc:
                return self._result(request_id, self._tool_result({"error": str(exc)}, is_error=True))
            except Exception:
                return self._result(
                    request_id,
                    self._tool_result({"error": "PoCiSys could not read the requested telemetry"}, is_error=True),
                )
        return self._error(request_id, -32601, f"Method not found: {method}")

    def handle(self, payload):
        if isinstance(payload, list):
            if not payload:
                return self._error(None, -32600, "Invalid Request")
            responses = [response for response in (self._handle_one(item) for item in payload) if response is not None]
            return responses or None
        return self._handle_one(payload)
