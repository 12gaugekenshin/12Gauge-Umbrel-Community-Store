import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import urlopen
from unittest.mock import patch

from app.main import Handler


class FakeMonitor:
    def status(self):
        return {
            "totalHashRate": 57_130_000_000_000,
            "totalMiners": 2,
            "blockHeight": 963451,
            "unrelated": "not exposed by compatibility routes",
        }


class CompatibilityRouteTests(unittest.TestCase):
    def setUp(self):
        self.monitor_patch = patch("app.main.MONITOR", FakeMonitor())
        self.monitor_patch.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.monitor_patch.stop()

    def get_json(self, path):
        with urlopen(
            f"http://127.0.0.1:{self.server.server_port}{path}", timeout=2
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            return json.load(response)

    def test_pool_route_exposes_public_pool_compatible_summary(self):
        self.assertEqual(self.get_json("/api/pool"), {
            "totalHashRate": 57_130_000_000_000,
            "totalMiners": 2,
            "blockHeight": 963451,
        })

    def test_info_route_exposes_public_pool_compatible_user_agents(self):
        self.assertEqual(self.get_json("/api/info"), {
            "userAgents": [{
                "userAgent": "PoCiSys",
                "count": 2,
                "totalHashRate": 57_130_000_000_000,
            }]
        })


if __name__ == "__main__":
    unittest.main()
