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
        self.assertIn("W setup est.", javascript)
        self.assertIn("W catalog est.", javascript)
        self.assertIn('name="chip_health_score_threshold" type="number" min="0" max="100"', html)
        self.assertIn("Arm manual LuxOS controls", html)
        self.assertIn("Arm automatic curtailing schedule", html)
        self.assertIn("Arm automatic hashboard recovery", html)
        self.assertIn("Unknown chips and brief warnings are ignored", html)

    def test_miner_setup_has_automatic_mining_target(self):
        web_root = Path(__file__).resolve().parents[1] / "web"
        html = (web_root / "index.html").read_text(encoding="utf-8")
        javascript = (web_root / "dashboard.js").read_text(encoding="utf-8")
        formatters = (web_root / "formatters.js").read_text(encoding="utf-8")
        self.assertIn('name="mining_target"', html)
        self.assertIn('value="btc">BTC Solo', html)
        self.assertIn('value="bch">BCH Solo', html)
        self.assertIn('value="pool">Pool mining', html)
        self.assertIn("mining_target: form.elements.mining_target.value", javascript)
        self.assertIn("function fleetGroup(miner)", formatters)
        self.assertIn("function shareNetworkMeaning(value)", javascript)
        self.assertIn("% of network difficulty", javascript)
        self.assertLess(
            html.index('/static/formatters.js'),
            html.index('/static/dashboard.js'),
            "Shared formatters must load before the dashboard",
        )


if __name__ == "__main__":
    unittest.main()
