from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from miners.axeos import AxeOSDriver
from miners.nerdaxe import NerdAxeDriver
from services.luxos_control import LuxOSClient


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FixtureClient(LuxOSClient):
    def __init__(self, responses):
        super().__init__("fixture.local")
        self.responses = list(responses)

    def call(self, command, parameter=None):
        if not self.responses:
            raise AssertionError(f"Unexpected LuxOS fixture call: {command}")
        return self.responses.pop(0)


class DriverFixtureTests(unittest.TestCase):
    def test_axeos_fixture_reports_explicit_health(self):
        driver = AxeOSDriver({"ip": "fixture.local", "name": "Axe fixture", "type": "axeos"})
        with patch.object(driver, "fetch_first_json", return_value=(fixture("axeos_status.json"), 12.5)):
            status = driver.poll()
        self.assertTrue(status["api_ok"])
        self.assertEqual(status["chip_health"]["items"][0]["status"], "healthy")
        self.assertAlmostEqual(status["hashrate_ths"], 1.2755)

    def test_nerdqaxe_missing_health_is_unknown_not_failed(self):
        driver = NerdAxeDriver({"ip": "fixture.local", "name": "Nerd fixture", "type": "nerdqaxe"})
        with patch.object(driver, "fetch_first_json", return_value=(fixture("nerdqaxe_status.json"), 14.0)):
            status = driver.poll()
        self.assertTrue(status["api_ok"])
        self.assertEqual(status["chip_health"]["total"], 4)
        self.assertTrue(all(item["status"] == "unknown" for item in status["chip_health"]["items"]))

    def test_luxos_low_score_does_not_reduce_responsive_chip_count(self):
        data = fixture("luxos_chip_health.json")
        client = FixtureClient([data["asccount"], data["health"], data["config"]])
        status = client.chip_health(90)
        board = status["items"][0]
        self.assertEqual(board["status"], "warning")
        self.assertEqual(board["chips_healthy"], 2)
        self.assertEqual(board["chips_total"], 3)
        self.assertEqual(board["chips_unknown"], 1)
        self.assertEqual(board["unhealthy_chip_count"], 0)
        self.assertEqual(board["low_score_count"], 1)
        self.assertEqual(board["low_chips"][0]["reason"], "score_below_threshold")


if __name__ == "__main__":
    unittest.main()
