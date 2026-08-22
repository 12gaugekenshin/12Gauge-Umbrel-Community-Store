import asyncio
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from services.pool_logs import PoolLogService


class Alerts:
    async def pool_event(self, _event):
        return None


class PoCiSysPortApi(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path == "/api/pool":
            payload = {
                "totalHashRate": 42_000_000_000_000,
                "totalMiners": 1,
                "blockHeight": 900_001,
            }
        elif self.path == "/api/status":
            payload = {
                "connection": {"pool": True},
                "workers": [{"clientName": "loki", "hashRate": 42_000_000_000_000, "bestDifficulty": 99}],
                "totalHashRate": 42_000_000_000_000,
                "totalMiners": 1,
                "blockHeight": 900_001,
                "candidates": [],
                "acceptedShares": [],
                "shareFeed": {"available": True},
            }
        elif self.path == "/api/shares":
            payload = [
                {"header_hash": "one", "received_at": 1000, "worker": "loki", "difficulty": 40},
                {"header_hash": "two", "received_at": 1001, "worker": "gamma", "difficulty": 80},
            ]
        else:
            self.send_error(404)
            return
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class PoolShareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "accepted-shares.json"
        self.pools = [
            {"id": "alpha", "name": "Alpha", "mode": "public_pool_api", "enabled": True, "api_url": "http://alpha"},
            {"id": "beta", "name": "Beta", "mode": "public_pool_api", "enabled": True, "api_url": "http://beta"},
        ]
        self.service = PoolLogService(
            self.pools,
            Alerts(),
            history_path=self.path,
            network_provider=lambda: {"btc": {"difficulty": 1000}},
        )

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def share(index, timestamp=1000):
        return {
            "id": f"header-{index}",
            "received_at": timestamp + index,
            "worker": f"worker-{index % 2}",
            "difficulty": index + 1,
        }

    def test_ten_per_pool_with_combined_multi_pool_inputs(self):
        self.service._merge_shares(self.pools[0], [self.share(index) for index in range(12)])
        self.service._merge_shares(self.pools[1], [self.share(index, 2000) for index in range(15)])
        statuses = self.service.status()
        self.assertEqual(len(statuses[0]["accepted_shares"]), 10)
        self.assertEqual(len(statuses[1]["accepted_shares"]), 10)
        self.assertNotEqual(statuses[0]["accepted_shares"][0]["id"], statuses[1]["accepted_shares"][0]["id"])
        self.assertEqual(statuses[0]["accepted_shares"][0]["network_percent"], 1.2)
        self.assertEqual(statuses[1]["all_time_best_difficulty"], 15)
        self.assertEqual(statuses[1]["all_time_best_worker"], "worker-0")
        self.assertEqual(statuses[1]["session_best_worker"], "worker-0")

    def test_same_timestamp_from_multiple_workers_is_not_lost(self):
        shares = [
            {"id": "a", "received_at": 1000, "worker": "one", "difficulty": 5},
            {"id": "b", "received_at": 1000, "worker": "two", "difficulty": 6},
        ]
        self.service._merge_shares(self.pools[0], shares)
        self.assertEqual(len(self.service.status()[0]["accepted_shares"]), 2)

    def test_bounded_history_and_all_time_best_survive_restart(self):
        self.service._merge_shares(self.pools[0], [self.share(index) for index in range(12)])
        reloaded = PoolLogService(self.pools, Alerts(), history_path=self.path, network_provider=lambda: {})
        alpha = reloaded.status()[0]
        self.assertEqual(len(alpha["accepted_shares"]), 10)
        self.assertEqual(alpha["all_time_best_difficulty"], 12)
        self.assertEqual(alpha["all_time_best_worker"], "worker-1")
        self.assertIsNone(alpha["session_best_difficulty"])
        self.assertIsNone(alpha["session_best_worker"])

    def test_legacy_best_worker_is_backfilled_when_matching_share_is_seen(self):
        self.path.write_text(json.dumps({
            "version": 1,
            "pools": {
                "alpha": {
                    "shares": [],
                    "all_time_best": 500,
                    "reset_after_ms": None,
                }
            },
        }), encoding="utf-8")
        service = PoolLogService(self.pools, Alerts(), history_path=self.path, network_provider=lambda: {})
        service._merge_shares(self.pools[0], [{
            "id": "matching-best", "received_at": 2000, "worker": "loki3", "difficulty": 500,
        }])
        status = service.status()[0]
        self.assertEqual(status["all_time_best_worker"], "loki3")
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["pools"]["alpha"]["all_time_best_worker"], "loki3")

    def test_clear_history_ignores_pre_reset_shares_and_accepts_new_ones(self):
        now = int(time.time())
        old_share = {"id": "old", "received_at": now - 30, "worker": "one", "difficulty": 500}
        self.service._merge_shares(self.pools[0], [old_share])

        result = self.service.clear_share_history()
        cleared = self.service.status()[0]
        self.assertTrue(result["ok"])
        self.assertEqual(result["cleared_pools"], 2)
        self.assertEqual(cleared["accepted_shares"], [])
        self.assertIsNone(cleared["session_best_difficulty"])
        self.assertIsNone(cleared["all_time_best_difficulty"])
        self.assertIsNone(cleared["all_time_best_worker"])

        self.service._merge_shares(self.pools[0], [old_share])
        self.assertEqual(self.service.status()[0]["accepted_shares"], [])

        new_share = {"id": "new", "received_at": now + 2, "worker": "two", "difficulty": 900}
        self.service._merge_shares(self.pools[0], [new_share])
        refreshed = self.service.status()[0]
        self.assertEqual(len(refreshed["accepted_shares"]), 1)
        self.assertEqual(refreshed["session_best_difficulty"], 900)
        self.assertEqual(refreshed["all_time_best_difficulty"], 900)
        self.assertEqual(refreshed["session_best_worker"], "two")
        self.assertEqual(refreshed["all_time_best_worker"], "two")

        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertGreater(saved["pools"]["alpha"]["reset_after_ms"], 0)

    def test_pocisys_port_adapter_reads_actual_accepted_shares(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), PoCiSysPortApi)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            pool = dict(self.pools[0], api_url=f"http://127.0.0.1:{server.server_port}")
            service = PoolLogService(
                [pool], Alerts(), history_path=self.path,
                network_provider=lambda: {"btc": {"difficulty": 1000}},
            )
            asyncio.run(service._poll_public_pool(pool))
            status = service.status()[0]
            self.assertEqual(status["adapter"], "pocisys_pool_port")
            self.assertTrue(status["share_feed_available"])
            self.assertEqual(len(status["accepted_shares"]), 2)
            self.assertEqual(status["accepted_shares"][0]["worker"], "gamma")
            self.assertEqual(status["accepted_shares"][0]["network_percent"], 8)
            self.assertEqual(status["session_best_worker"], "loki")
            self.assertEqual(status["all_time_best_worker"], "loki")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
