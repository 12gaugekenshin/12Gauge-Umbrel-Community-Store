from __future__ import annotations

import unittest

from services.config_store import normalize_config
from services.odds import calculate_odds


class OddsAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "odds": {
                "auto_network_data": False,
                "btc_enabled": True,
                "bch_enabled": True,
                "bsv_enabled": True,
                "manual_btc_network_hashrate_eh": 100,
                "manual_bch_network_hashrate_eh": 10,
                "manual_bsv_network_hashrate_eh": 1,
            }
        }

    def test_solo_odds_use_only_assigned_online_hashrate(self):
        result = calculate_odds([
            {"online": True, "hashrate_ths": 60, "mining_target": "btc"},
            {"online": True, "hashrate_ths": 40, "mining_target": "bch"},
            {"online": True, "hashrate_ths": 25, "mining_target": "pool"},
            {"online": False, "hashrate_ths": 500, "mining_target": "bch"},
        ], self.config)

        self.assertEqual(result["total_hashrate_ths"], 125)
        self.assertEqual(result["assigned_hashrate_ths"], {"btc": 60.0, "bch": 40.0})
        self.assertEqual(result["btc"]["miner_hashrate_ths"], 60)
        self.assertEqual(result["bch"]["miner_hashrate_ths"], 40)
        self.assertEqual(result["bsv"]["miner_hashrate_ths"], 125)
        self.assertGreater(result["btc"]["daily_chance"], 0)
        self.assertGreater(result["bch"]["daily_chance"], result["btc"]["daily_chance"])

    def test_legacy_status_without_assignment_preserves_btc_default(self):
        result = calculate_odds([
            {"online": True, "hashrate_ths": 12.5},
        ], self.config)
        self.assertEqual(result["btc"]["miner_hashrate_ths"], 12.5)
        self.assertEqual(result["bch"]["miner_hashrate_ths"], 0)

    def test_config_migration_infers_existing_target_groups(self):
        migrated = normalize_config({
            "miners": [
                {"name": "Cash", "group": "BCH Solo", "type": "luxos"},
                {"name": "Pooled", "group": "Pool Miners", "type": "luxos"},
                {"name": "Bitcoin", "group": "Garage", "type": "axeos"},
            ]
        })
        self.assertEqual(migrated["miners"][0]["mining_target"], "bch")
        self.assertEqual(migrated["miners"][1]["mining_target"], "pool")
        self.assertEqual(migrated["miners"][2]["mining_target"], "btc")


if __name__ == "__main__":
    unittest.main()
