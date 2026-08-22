import importlib
import json
import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)
main = importlib.import_module("dashboard.main")


class DashboardTests(unittest.TestCase):
    @patch.object(main, "rpc")
    @patch.object(main, "fetch_json")
    def test_status_is_bounded_and_combines_services(self, fetch, rpc):
        rpc.side_effect = [
            {"chain": "main", "blocks": 10, "headers": 10, "verificationprogress": 1, "pruned": True, "size_on_disk": 20, "difficulty": 30},
            {"connections": 4, "subversion": "/BCHN:29.1.0/"},
        ]
        fetch.side_effect = [
            {"coins": {"BCH": {"shares_accepted": 2}}},
            {"miners": {"BCH": [{"worker_name": "q.worker"}]}},
            {"shares": {"BCH": [{"worker_name": "q.worker", "difficulty": i, "accepted_at": "2026-01-01T00:00:00Z"} for i in range(20)]}},
        ]
        data = main.status()
        self.assertTrue(data["node"]["online"])
        self.assertTrue(data["pool"]["online"])
        self.assertEqual(len(data["shares"]), 10)
        self.assertEqual(data["bounded"], {"workers": 512, "shares": 50})


if __name__ == "__main__":
    unittest.main()
