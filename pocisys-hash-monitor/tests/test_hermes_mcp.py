from __future__ import annotations

import unittest

from services.config_store import normalize_config, public_config
from services.hermes_mcp import (
    HermesMcpService,
    TOOLS,
    create_connection_token,
    sanitize_miner,
    sanitize_pool,
    token_digest,
    token_matches,
)
from services.system_stats import SystemStatsService


class HermesMcpTests(unittest.TestCase):
    def setUp(self):
        self.miners = [
            {
                "id": "miner_1",
                "name": "Loki1",
                "group": "BTC Solo",
                "online": True,
                "hashrate_ths": 42.5,
            },
            {
                "id": "miner_2",
                "name": "Loki2",
                "group": "BTC Solo",
                "online": False,
                "hashrate_ths": None,
            },
        ]
        self.service = HermesMcpService(
            version="1.6.1",
            overview_provider=lambda: {"summary": {"online_miners": 1}},
            miners_provider=lambda: self.miners,
            pools_provider=lambda: [{"name": "My Public Pool", "available": True}],
            odds_provider=lambda: {"coins": [{"symbol": "BTC", "daily_probability_percent": 0.01}]},
            system_provider=lambda: {"cpu": {"usage_percent": 12.3}},
        )

    def call(self, method, params=None, request_id=1):
        return self.service.handle(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        )

    def test_token_is_one_way_and_verifiable(self):
        token = create_connection_token()
        digest = token_digest(token)
        self.assertTrue(token.startswith("pocisys_"))
        self.assertNotIn(token, digest)
        self.assertTrue(token_matches(token, digest))
        self.assertFalse(token_matches(f"{token}x", digest))

    def test_config_migration_adds_hermes_defaults_and_hides_digest(self):
        config = normalize_config({"app": {}, "miners": [], "pools": [], "discord": {}, "odds": {}})
        self.assertEqual(
            config["hermes"],
            {"enabled": False, "token_hash": "", "token_hint": ""},
        )
        config["hermes"]["token_hash"] = "abc123"
        self.assertEqual(public_config(config)["hermes"]["token_hash"], "configured (hidden)")

    def test_initialize_and_tool_list(self):
        initialized = self.call("initialize", {"protocolVersion": "2025-06-18"})
        self.assertEqual(initialized["result"]["serverInfo"]["version"], "1.6.1")
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")
        listed = self.call("tools/list")
        self.assertEqual(len(listed["result"]["tools"]), len(TOOLS))
        self.assertTrue(all(tool["annotations"]["readOnlyHint"] for tool in listed["result"]["tools"]))

    def test_tool_calls_are_bounded_to_configured_data(self):
        response = self.call(
            "tools/call",
            {"name": "get_pocisys_miner", "arguments": {"miner": "loki1"}},
        )
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"]["miner"]["id"], "miner_1")

        missing = self.call(
            "tools/call",
            {"name": "get_pocisys_miner", "arguments": {"miner": "not-configured"}},
        )
        self.assertTrue(missing["result"]["isError"])

    def test_batch_notifications_do_not_generate_responses(self):
        response = self.service.handle(
            [
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 9, "method": "ping"},
            ]
        )
        self.assertEqual(response, [{"jsonrpc": "2.0", "id": 9, "result": {}}])

    def test_unknown_tool_does_not_execute(self):
        response = self.call("tools/call", {"name": "run_shell", "arguments": {"command": "id"}})
        self.assertEqual(response["error"]["code"], -32601)

    def test_sensitive_network_and_payout_fields_are_removed(self):
        miner = sanitize_miner(
            {
                "id": "miner_1",
                "name": "Loki1",
                "ip": "192.168.1.99",
                "hashrate_ths": 42.5,
                "pool": {
                    "url": "stratum+tcp://192.168.1.10:3333",
                    "user": "bc1q-private.worker",
                    "connected": True,
                    "status": "connected",
                },
            }
        )
        self.assertNotIn("ip", miner)
        self.assertNotIn("url", miner["pool"])
        self.assertNotIn("user", miner["pool"])
        self.assertEqual(miner["hashrate_ths"], 42.5)

        pool = sanitize_pool(
            {
                "name": "Public Pool",
                "api_url": "http://192.168.1.10:2019",
                "log_path": "/data/pool.log",
                "available": True,
            }
        )
        self.assertNotIn("api_url", pool)
        self.assertNotIn("log_path", pool)
        self.assertTrue(pool["available"])

    def test_direct_coin_odds_shape_can_be_filtered(self):
        self.service.odds_provider = lambda: {
            "total_hashrate_ths": 42.5,
            "btc": {"symbol": "BTC", "enabled": True, "daily_chance": 0.001},
        }
        response = self.call(
            "tools/call",
            {"name": "get_pocisys_block_odds", "arguments": {"coin": "BTC"}},
        )
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"]["coin"]["symbol"], "BTC")


class SystemStatsTests(unittest.TestCase):
    def test_snapshot_has_safe_shape(self):
        service = SystemStatsService(interval_seconds=0.5)
        sample = service._sample()
        self.assertEqual(sample["scope"], "container-visible host metrics")
        self.assertIn("cpu", sample)
        self.assertIn("memory", sample)
        self.assertIn("disk", sample)
        self.assertIn("sampled_at", sample)


if __name__ == "__main__":
    unittest.main()
