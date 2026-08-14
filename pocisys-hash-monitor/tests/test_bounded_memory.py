from __future__ import annotations

import unittest

from services.alerts import AlertEngine, MAX_ALERT_COOLDOWN_KEYS
from services.health import HealthEngine, MAX_HEALTH_TRANSITIONS


def healthy_status(miner_id, online=True):
    return {
        "id": miner_id,
        "name": miner_id,
        "ip": "fixture.local",
        "type": "luxos",
        "online": online,
        "api_ok": online,
        "hashrate_ths": 10 if online else None,
        "expected_hashrate_ths": 10,
        "temps": {"chip_c": 50} if online else {},
        "shares": {"valid": 100},
        "hardware_errors": 0,
        "chip_health": {"reported": False, "items": []},
        "warnings": [],
    }


class BoundedMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_alert_feed_and_cooldown_keys_remain_bounded(self):
        engine = AlertEngine({"app": {"alert_cooldown_seconds": 0}, "discord": {"enabled": False}})
        for index in range(MAX_ALERT_COOLDOWN_KEYS + 200):
            await engine.emit(f"event:{index}", "Test", "bounded", force=True)
        self.assertEqual(len(engine.alert_feed), 25)
        self.assertLessEqual(len(engine.last_sent), MAX_ALERT_COOLDOWN_KEYS)

    async def test_health_transitions_and_removed_miners_are_bounded(self):
        engine = HealthEngine()
        miner = {"id": "one", "name": "one", "type": "luxos"}
        for index in range(MAX_HEALTH_TRANSITIONS * 4):
            engine.evaluate(healthy_status("one", online=index % 2 == 0), miner)
        self.assertLessEqual(len(engine.transitions), MAX_HEALTH_TRANSITIONS)
        engine.reconfigure([])
        self.assertEqual(engine.trackers, {})

    async def test_alert_reconfigure_drops_removed_miner_state(self):
        config = {"app": {}, "discord": {}, "miners": [{"id": "one"}]}
        engine = AlertEngine(config)
        engine.previous = {"one": {"online": True}, "removed": {"online": False}}
        engine.best_diff = {"one": 10, "removed": 20}
        engine.reconfigure()
        self.assertEqual(set(engine.previous), {"one"})
        self.assertEqual(set(engine.best_diff), {"one"})


if __name__ == "__main__":
    unittest.main()
