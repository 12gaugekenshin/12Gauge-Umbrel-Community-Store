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

    def test_luxos_profiles_use_real_select_controls(self):
        web_root = Path(__file__).resolve().parents[1] / "web"
        html = (web_root / "index.html").read_text(encoding="utf-8")
        javascript = (web_root / "dashboard.js").read_text(encoding="utf-8")
        self.assertIn('<select name="control_full_profile">', html)
        self.assertIn('<select name="control_low_profile">', html)
        self.assertNotIn('list="luxos-full-profiles"', html)
        self.assertNotIn('list="luxos-low-profiles"', html)
        self.assertIn("populateLuxosProfileSelect", javascript)


if __name__ == "__main__":
    unittest.main()
