from __future__ import annotations

import unittest

from services.config_transfer import make_safe_backup, restore_safe_backup
from services.config_store import normalize_config
from services.validation import ApiError


class ConfigTransferTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "app": {"poll_interval_seconds": 10, "luxos_control_enabled": False},
            "miners": [{
                "id": "miner_0123456789ab",
                "name": "Garage",
                "ip": "192.168.1.50",
                "type": "axeos",
                "group": "BTC",
                "mining_target": "btc",
                "enabled": True,
                "display_order": 1,
                "min_hashrate_ths": None,
                "temp_warning_c": 70,
                "temp_critical_c": 80,
            }],
            "pools": [],
            "discord": {"enabled": True, "webhook_url": "https://discord.com/api/webhooks/example/secret"},
            "odds": {"btc_enabled": True},
            "hermes": {"enabled": True, "token_hash": "secret-digest", "token_hint": "123456"},
        }

    def test_backup_excludes_reusable_or_authentication_values(self):
        backup = make_safe_backup(self.config, "test")
        serialized = str(backup)
        self.assertNotIn("api/webhooks", serialized)
        self.assertNotIn("secret-digest", serialized)
        self.assertEqual(backup["config"]["hermes"]["token_hint"], "")
        self.assertFalse(backup["config"]["discord"]["enabled"])
        self.assertEqual(backup["config"]["miners"][0]["ip"], "192.168.1.50")

    def test_restore_validates_and_preserves_current_secrets(self):
        backup = make_safe_backup(self.config, "test")
        restored = restore_safe_backup(backup, self.config)
        self.assertEqual(restored["miners"][0]["name"], "Garage")
        self.assertEqual(restored["discord"]["webhook_url"], self.config["discord"]["webhook_url"])
        self.assertEqual(restored["hermes"]["token_hash"], "secret-digest")

    def test_restore_rejects_invalid_or_unbounded_lists(self):
        with self.assertRaises(ApiError):
            restore_safe_backup({"schema_version": 99, "config": {}}, self.config)
        with self.assertRaises(ApiError):
            restore_safe_backup({"config": {"miners": [{}] * 501, "pools": []}}, self.config)

    def test_existing_armed_install_migrates_as_acknowledged(self):
        migrated = normalize_config({"app": {"luxos_control_enabled": True}})
        self.assertTrue(migrated["app"]["luxos_control_acknowledged"])
        fresh = normalize_config({"app": {"luxos_control_enabled": False}})
        self.assertFalse(fresh["app"]["luxos_control_acknowledged"])


if __name__ == "__main__":
    unittest.main()
