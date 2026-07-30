import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from app.main import Monitor
from test_verification import GENESIS_BLOCK


GENESIS_HASH = "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f"


class FakeServices(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _send(self, value):
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send({"totalHashRate": 500000000000, "totalMiners": 1, "blockHeight": 900000, "blocksFound": []})

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        method = request["method"]
        if method == "getblockchaininfo":
            result = {"chain": "main", "blocks": 900000, "headers": 900000, "verificationprogress": 1, "initialblockdownload": False}
        elif method == "getnetworkinfo":
            result = {"connections": 12, "subversion": "/Satoshi:test/"}
        elif method == "getblock" and request["params"][0] == GENESIS_HASH:
            result = {"confirmations": 100, "tx": ["coinbase"]}
        else:
            return self._send({"result": None, "error": {"code": -5, "message": "not found"}, "id": request["id"]})
        self._send({"result": result, "error": None, "id": request["id"]})


class MonitorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.pool_db = os.path.join(self.temp.name, "public-pool.sqlite")
        with closing(sqlite3.connect(self.pool_db)) as db, db:
            db.executescript("""
              CREATE TABLE blocks_entity(id INTEGER,height INTEGER,minerAddress TEXT,worker TEXT,sessionId TEXT,blockData TEXT,createdAt TEXT,updatedAt TEXT,deletedAt TEXT);
              CREATE TABLE client_entity(address TEXT,clientName TEXT,sessionId TEXT,userAgent TEXT,startTime TEXT,bestDifficulty REAL,hashRate REAL,updatedAt TEXT,deletedAt TEXT);
            """)
            db.execute("INSERT INTO blocks_entity VALUES(1,0,'1A1zP1','genesis','00000001',?,'2009-01-03','2009-01-03',NULL)", (GENESIS_BLOCK,))
            db.execute("INSERT INTO client_entity VALUES('bc1qtest','garage','abc12345','Bitaxe','now',10,500000000000,'now',NULL)")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeServices)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.temp.cleanup()

    def test_full_poll_reads_pool_node_workers_and_matures_block(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        values = {
            "DATA_DIR": self.temp.name, "PUBLIC_POOL_DB": self.pool_db,
            "PUBLIC_POOL_API_URL": base, "BITCOIN_RPC_URL": base,
            "BITCOIN_RPC_PORT": "", "BITCOIN_RPC_USER": "user", "BITCOIN_RPC_PASSWORD": "pass",
        }
        with patch.dict(os.environ, values, clear=False):
            monitor = Monitor()
            monitor.poll()
            status = monitor.status()
        self.assertTrue(status["pool"]["online"])
        self.assertTrue(status["node"]["online"])
        self.assertEqual(status["workers"][0]["clientName"], "garage")
        self.assertEqual(status["candidates"][0]["status"], "mature")


if __name__ == "__main__":
    unittest.main()
