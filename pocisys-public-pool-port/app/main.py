import json
import logging
import mimetypes
import os
import pathlib
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .pool_db import PoolDatabase
from .rpc import BitcoinRpc, RpcError
from .store import Store
from .verification import confirmation_status, verify_proof


LOG = logging.getLogger("pocisys.pool.port")
ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB_ROOT = ROOT / "web"


def env(name, default=""):
    return os.getenv(name, default)


class Monitor:
    def __init__(self):
        data_dir = pathlib.Path(env("DATA_DIR", "/data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        self.pool_db = PoolDatabase(env("PUBLIC_POOL_DB", "/pool-db/public-pool.sqlite"))
        self.pool_api = env("PUBLIC_POOL_API_URL", "http://server:2019").rstrip("/")
        rpc_url = env("BITCOIN_RPC_URL", "http://bitcoin:8332")
        rpc_port = env("BITCOIN_RPC_PORT")
        if rpc_port and rpc_url.rsplit(":", 1)[-1].find("/") >= 0:
            rpc_url = f"{rpc_url.rstrip('/')}:{rpc_port}"
        elif rpc_port and "://" in rpc_url and rpc_url.count(":") == 1:
            rpc_url = f"{rpc_url}:{rpc_port}"
        self.rpc = BitcoinRpc(
            rpc_url, env("BITCOIN_RPC_USER"), env("BITCOIN_RPC_PASSWORD"),
            int(env("BITCOIN_RPC_TIMEOUT_SECONDS", "10")),
        )
        self.store = Store(str(data_dir / "pocisys-pool-port.sqlite"))
        self.interval = max(5, int(env("POLL_INTERVAL_SECONDS", "15")))
        self.worker_stale_seconds = max(
            self.interval * 2,
            int(env("WORKER_STALE_SECONDS", "150")),
        )
        self.state = {
            "updatedAt": None,
            "pool": {"online": False, "error": "Waiting for Public Pool"},
            "node": {"online": False, "error": "Waiting for Bitcoin Core"},
            "workers": [],
        }
        self.lock = threading.Lock()
        self.stop_event = threading.Event()

    def http_json(self, path):
        request = urllib.request.Request(f"{self.pool_api}{path}", headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=8) as response:
            return json.loads(response.read().decode())

    def poll(self):
        pool = {"online": False}
        node = {"online": False}
        workers = []
        try:
            pool.update(self.http_json("/api/pool"))
            pool["online"] = True
            pool["error"] = None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            pool["error"] = str(exc)
        try:
            workers = self.pool_db.workers(self.worker_stale_seconds)
        except Exception as exc:
            pool["databaseError"] = str(exc)
        try:
            chain = self.rpc.call("getblockchaininfo")
            network = self.rpc.call("getnetworkinfo")
            node = {
                "online": True,
                "chain": chain.get("chain"),
                "blocks": chain.get("blocks"),
                "headers": chain.get("headers"),
                "verificationProgress": chain.get("verificationprogress"),
                "initialBlockDownload": chain.get("initialblockdownload"),
                "connections": network.get("connections"),
                "subversion": network.get("subversion"),
                "error": None,
            }
        except RpcError as exc:
            node["error"] = str(exc)

        # Public Pool caches /api/pool for five minutes. Its per-session rows
        # are updated about once per minute, so they are the least stale source
        # available for the dashboard's current share-derived estimate.
        total_hashrate = sum(float(w.get("hashRate") or 0) for w in workers)
        total_miners = len(workers)
        height = pool.get("blockHeight") or node.get("blocks")
        self.store.snapshot(total_hashrate, total_miners, height)
        self.verify_candidates()
        with self.lock:
            self.state = {
                "updatedAt": int(time.time()), "pool": pool, "node": node,
                "workers": workers, "totalHashRate": total_hashrate,
                "totalMiners": total_miners, "blockHeight": height,
                "hashrateSource": "active-worker-share-estimate",
                "workerStaleSeconds": self.worker_stale_seconds,
            }

    def verify_candidates(self):
        try:
            blocks = self.pool_db.blocks()
        except Exception:
            return
        for block in blocks:
            try:
                proof = verify_proof(block["blockData"])
                status = "candidate" if proof["proofValid"] else "invalid"
                confirmations = None
                coinbase_txid = None
                error = None
                if proof["proofValid"]:
                    try:
                        chain_block = self.rpc.call("getblock", [proof["hash"], 1])
                        confirmations = chain_block.get("confirmations")
                        coinbase_txid = (chain_block.get("tx") or [None])[0]
                        status = confirmation_status(confirmations)
                    except RpcError as exc:
                        error = str(exc)
                self.store.candidate({
                    "upstream_id": block["id"], "height": block["height"],
                    "miner_address": block["minerAddress"], "worker": block["worker"],
                    "session_id": block["sessionId"], "block_hash": proof["hash"],
                    "detected_at": block.get("createdAt"),
                    "proof_valid": 1 if proof["proofValid"] else 0, "bits": proof["bits"],
                    "status": status, "confirmations": confirmations,
                    "coinbase_txid": coinbase_txid, "last_checked": int(time.time()), "error": error,
                })
            except (ValueError, KeyError) as exc:
                LOG.error("Unable to verify candidate %s: %s", block.get("id"), exc)

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.poll()
            except Exception:
                LOG.exception("monitor poll failed")
            self.stop_event.wait(self.interval)

    def status(self):
        with self.lock:
            result = dict(self.state)
        result["candidates"] = self.store.candidates()
        result["events"] = self.store.events()[:12]
        result["connection"] = {
            "localHost": env("DEVICE_DOMAIN_NAME", "umbrel.local"),
            "publicHost": env("PUBLIC_STRATUM_HOST", ""),
            "port": int(env("STRATUM_PORT", "3333")),
            "poolName": env("POOL_DISPLAY_NAME", "PoCiSys Public Pool Port"),
        }
        return result


MONITOR = None


class Handler(BaseHTTPRequestHandler):
    server_version = "PoCiSysPoolPort/0.1"

    def log_message(self, fmt, *args):
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def json_response(self, value, status=200):
        body = json.dumps(value, separators=(",", ":"), default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            return self.json_response({"ok": True})
        if parsed.path == "/api/status":
            return self.json_response(MONITOR.status())
        if parsed.path == "/api/blocks":
            return self.json_response(MONITOR.store.candidates())
        if parsed.path == "/api/events":
            return self.json_response(MONITOR.store.events())
        if parsed.path == "/api/history":
            hours = min(2160, max(1, int(parse_qs(parsed.query).get("hours", [24])[0])))
            return self.json_response(MONITOR.store.history(int(time.time()) - hours * 3600))
        if parsed.path == "/api/widget":
            status = MONITOR.status()
            candidates = status["candidates"]
            return self.json_response({
                "type": "four-stats", "items": [
                    {"title": "Hash Rate", "text": format_hashrate(status.get("totalHashRate", 0))},
                    {"title": "Workers", "text": str(status.get("totalMiners", 0))},
                    {"title": "Blocks", "text": str(len(candidates))},
                    {"title": "Height", "text": str(status.get("blockHeight") or "—")},
                ]
            })
        return self.serve_static(parsed.path)

    def serve_static(self, path):
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        file_path = (WEB_ROOT / relative).resolve()
        if WEB_ROOT.resolve() not in file_path.parents or not file_path.is_file():
            file_path = WEB_ROOT / "index.html"
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(file_path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def format_hashrate(value):
    value = float(value or 0)
    units = ["H/s", "kH/s", "MH/s", "GH/s", "TH/s", "PH/s"]
    index = 0
    while value >= 1000 and index < len(units) - 1:
        value /= 1000
        index += 1
    return f"{value:.2f} {units[index]}"


def main():
    global MONITOR
    logging.basicConfig(level=env("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    MONITOR = Monitor()
    worker = threading.Thread(target=MONITOR.run, name="pool-monitor", daemon=True)
    worker.start()
    address = ("0.0.0.0", int(env("PORT", "8080")))
    LOG.info("dashboard listening on %s:%s", *address)
    ThreadingHTTPServer(address, Handler).serve_forever()


if __name__ == "__main__":
    main()
