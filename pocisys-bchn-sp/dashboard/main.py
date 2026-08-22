import base64
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore
from urllib.parse import urlparse

PORT = int(os.getenv("PORT", "8080"))
ENGINE_URL = os.getenv("ENGINE_URL", "http://engine:8081").rstrip("/")
RPC_URL = os.getenv("BCH_RPC_URL", "http://node:8332")
RPC_USER = os.getenv("BCH_RPC_USER", "pocisys")
RPC_PASSWORD = os.getenv("BCH_RPC_PASSWORD", "pocisys-bchn-internal-only")
STRATUM_PORT = int(os.getenv("STRATUM_PORT", "3335"))
STATIC = Path(__file__).with_name("static")
STARTED = time.monotonic()
REQUEST_SLOTS = BoundedSemaphore(16)


def fetch_json(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def rpc(method, params=None):
    body = json.dumps({"jsonrpc": "1.0", "id": "pocisys", "method": method, "params": params or []}).encode()
    request = urllib.request.Request(RPC_URL, data=body, headers={"Content-Type": "application/json"})
    token = base64.b64encode(f"{RPC_USER}:{RPC_PASSWORD}".encode()).decode()
    request.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(request, timeout=4) as response:
        result = json.load(response)
    if result.get("error"):
        raise RuntimeError(result["error"])
    return result.get("result")


def status():
    result = {
        "name": "PoCiSys BCHN&SP", "version": "0.1.0", "uptime": int(time.monotonic() - STARTED),
        "stratumPort": STRATUM_PORT, "node": {"online": False}, "pool": {"online": False},
        "miners": [], "shares": [], "bounded": {"workers": 512, "shares": 50},
    }
    try:
        chain = rpc("getblockchaininfo")
        net = rpc("getnetworkinfo")
        result["node"] = {
            "online": True, "chain": chain.get("chain"), "blocks": chain.get("blocks", 0),
            "headers": chain.get("headers", 0), "progress": chain.get("verificationprogress", 0),
            "pruned": chain.get("pruned", False), "sizeOnDisk": chain.get("size_on_disk", 0),
            "difficulty": chain.get("difficulty", 0), "connections": net.get("connections", 0),
            "version": net.get("subversion", "BCHN"),
        }
    except Exception as exc:
        result["node"]["error"] = type(exc).__name__
    try:
        stats = fetch_json(f"{ENGINE_URL}/api/v1/stats")
        miners = fetch_json(f"{ENGINE_URL}/api/v1/miners").get("miners", {}).get("BCH", [])
        shares = fetch_json(f"{ENGINE_URL}/api/v1/shares").get("shares", {}).get("BCH", [])
        coin = stats.get("coins", {}).get("BCH", {})
        now = datetime.now(timezone.utc)
        total_hashrate = 0.0
        for miner in miners:
            relevant = [share for share in shares if share.get("worker_name") == miner.get("worker_name")]
            work = sum(float(share.get("difficulty", 0) or 0) * 4294967296 for share in relevant)
            ages = []
            for share in relevant:
                try:
                    ages.append((now - datetime.fromisoformat(share["accepted_at"].replace("Z", "+00:00"))).total_seconds())
                except (KeyError, TypeError, ValueError):
                    pass
            window = min(300.0, max(30.0, max(ages, default=30.0)))
            miner["hashrate"] = work / window
            total_hashrate += miner["hashrate"]
        result["pool"] = {"online": True, "estimated_hashrate": total_hashrate, **coin}
        result["miners"] = miners
        result["shares"] = list(reversed(shares[-10:]))
    except Exception as exc:
        result["pool"]["error"] = type(exc).__name__
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = "PoCiSys-BCHN-SP/0.1"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, payload, code=200):
        data = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path, mime):
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not REQUEST_SLOTS.acquire(blocking=False):
            return self.send_json({"error": "busy"}, 503)
        try:
            path = urlparse(self.path).path
            if path == "/healthz":
                return self.send_json({"status": "ok"})
            if path in ("/api/status", "/api/pool"):
                data = status()
                if path == "/api/pool":
                    return self.send_json({
                        "totalHashRate": data["pool"].get("estimated_hashrate", 0),
                        "totalMiners": len(data["miners"]), "blockHeight": data["node"].get("blocks"),
                        "shares": data["shares"], "coin": "BCH",
                    })
                return self.send_json(data)
            if path == "/api/widget":
                data = status()
                node = data["node"]
                pool = data["pool"]
                return self.send_json({"type": "four-stats", "items": [
                    {"title": "Node", "text": "Online" if node.get("online") else "Syncing"},
                    {"title": "Height", "text": f"{node.get('blocks', 0):,}"},
                    {"title": "Miners", "text": str(len(data["miners"]))},
                    {"title": "Shares", "text": f"{pool.get('shares_accepted', 0):,}"},
                ]})
            if path == "/api/info":
                data = status()
                return self.send_json({"userAgents": [{"userAgent": "PoCiSys BCHN&SP", "count": len(data["miners"]), "totalHashRate": data["pool"].get("estimated_hashrate", 0)}]})
            files = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css"), "/brand-cat.png": ("brand-cat.png", "image/png")}
            if path in files:
                name, mime = files[path]
                return self.send_file(STATIC / name, mime)
            self.send_error(404)
        finally:
            REQUEST_SLOTS.release()


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
