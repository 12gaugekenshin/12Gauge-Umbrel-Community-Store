import unittest

from services.health import HealthEngine


def miner(miner_id="one"):
    return {
        "id": miner_id,
        "name": miner_id,
        "ip": "miner.local",
        "type": "luxos",
        "min_hashrate_ths": 10,
        "temp_warning_c": 70,
        "temp_critical_c": 80,
    }


def status(miner_id="one", hashrate=10, chip_status="healthy"):
    return {
        "id": miner_id,
        "name": miner_id,
        "ip": "miner.local",
        "type": "luxos",
        "online": True,
        "api_ok": True,
        "hashrate_ths": hashrate,
        "expected_hashrate_ths": 10,
        "temps": {"chip_c": 50},
        "shares": {"valid": 100, "invalid": 0, "stale": 0, "rejected": 0},
        "hardware_errors": 0,
        "chip_health": {
            "reported": True,
            "healthy": 1 if chip_status == "healthy" else 0 if chip_status == "unhealthy" else None,
            "total": 1,
            "items": [{"name": "Hashboard 1", "status": chip_status, "chips_healthy": 1 if chip_status == "healthy" else 0 if chip_status == "unhealthy" else None, "chips_total": 1}],
        },
        "warnings": [],
    }


class HealthEngineTests(unittest.TestCase):
    def test_single_bad_sample_does_not_mark_unhealthy(self):
        engine = HealthEngine()
        result = engine.evaluate(status(hashrate=0), miner())
        self.assertEqual(result["health"]["state"], "Healthy")
        self.assertEqual(result["health"]["reasons"], [])

    def test_persistent_severe_fault_warns_then_becomes_unhealthy(self):
        engine = HealthEngine()
        self.assertEqual(engine.evaluate(status(hashrate=0), miner())["health"]["state"], "Healthy")
        self.assertEqual(engine.evaluate(status(hashrate=0), miner())["health"]["state"], "Warning")
        result = engine.evaluate(status(hashrate=0), miner())
        self.assertEqual(result["health"]["state"], "Unhealthy")
        self.assertEqual(result["health"]["reasons"][0]["label"], "Hashrate Below Expected")

    def test_recovery_requires_two_clean_samples(self):
        engine = HealthEngine()
        for _ in range(3):
            engine.evaluate(status(chip_status="unhealthy"), miner())
        first = engine.evaluate(status(), miner())
        second = engine.evaluate(status(), miner())
        self.assertEqual(first["health"]["state"], "Unhealthy")
        self.assertEqual(first["health"]["reasons"][0]["code"], "recovery_pending")
        self.assertEqual(second["health"]["state"], "Healthy")

    def test_missing_telemetry_is_unknown_not_unhealthy(self):
        engine = HealthEngine()
        value = status()
        value.update(hashrate_ths=None, expected_hashrate_ths=None, temps={}, hardware_errors=None)
        value["shares"]["valid"] = 0
        value["chip_health"] = {"reported": False, "healthy": None, "total": None, "items": []}
        result = engine.evaluate(value, miner())
        self.assertEqual(result["health"]["state"], "Unknown")
        self.assertFalse(result["health"]["reasons"])

    def test_miners_have_independent_confirmation_counters(self):
        engine = HealthEngine()
        engine.evaluate(status("one", 0), miner("one"))
        engine.evaluate(status("one", 0), miner("one"))
        healthy = engine.evaluate(status("two", 10), miner("two"))
        self.assertEqual(healthy["health"]["state"], "Healthy")
        self.assertEqual(engine.trackers["one"]["state"], "Warning")


if __name__ == "__main__":
    unittest.main()
