from __future__ import annotations

import unittest
from pathlib import Path


class UmbrelComposeTests(unittest.TestCase):
    def test_app_proxy_targets_unique_pocisys_alias(self):
        compose = (
            Path(__file__).resolve().parents[1] / "docker-compose.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("APP_HOST: pocisys-hash-monitor_server_1", compose)
        self.assertIn("- pocisys-hash-monitor_server_1", compose)
        self.assertNotIn("APP_HOST: server\n", compose)


if __name__ == "__main__":
    unittest.main()
