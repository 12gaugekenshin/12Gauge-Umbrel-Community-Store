import unittest

from services.alerts import AlertEngine


def luxos_status(*, chip_status="warning", recovery_armed=True, pending=True, observing=False):
    return {
        "id": "miner_1",
        "name": "Test LuxOS",
        "ip": "miner.test",
        "type": "luxos",
        "online": True,
        "api_ok": True,
        "warnings": [],
        "health": {
            "state": "Warning" if chip_status == "warning" else "Healthy",
            "reasons": ([
                {"code": "hashrate_below_expected", "label": "Hashrate Below Expected"},
                {"code": "chip_warning:Hashboard 1", "label": "Chip Telemetry Warning"},
            ] if chip_status == "warning" else []),
        },
        "hashrate_ths": 1.0,
        "expected_hashrate_ths": 10.0,
        "temps": {},
        "chip_health": {
            "reported": True,
            "healthy": 0 if chip_status == "warning" else 1,
            "total": 1,
            "items": [{
                "name": "Hashboard 1",
                "status": chip_status,
                "chips_healthy": 75,
                "chips_total": 77,
            }],
        },
        "luxos_control": {
            "recovery_armed": recovery_armed,
            "recovery_pending": pending,
            "recovery_observing": observing,
        },
        "pool": {"connected": False, "url": "stratum+tcp://pool.test:3333", "source": "primary"},
        "shares": {"valid": 0, "invalid": 0, "stale": 0, "rejected": 0},
        "difficulty": {},
        "blocks_found": 0,
    }


class AlertRecoveryNoiseTests(unittest.IsolatedAsyncioTestCase):
    def engine(self):
        return AlertEngine({
            "app": {"alert_cooldown_seconds": 1, "offline_alert_grace_seconds": 60},
            "discord": {
                "send_hashrate_alerts": True,
                "send_chip_health_alerts": True,
                "send_pool_alerts": True,
                "send_pool_switch_alerts": True,
                "send_share_alerts": True,
                "send_recovery_alerts": True,
            },
        })

    async def test_managed_recovery_suppresses_routine_restart_noise(self):
        engine = self.engine()
        status = luxos_status()
        await engine.evaluate_miner(status, {"id": "miner_1", "type": "luxos"})
        self.assertEqual(list(engine.alert_feed), [])
        self.assertIn("Hashrate Below Expected", status["warnings"])
        self.assertIn("LuxOS chip health degraded", status["warnings"])
        self.assertIn("Pool disconnected", status["warnings"])

    async def test_unknown_does_not_create_a_false_recovery_alert(self):
        engine = self.engine()
        engine.previous["miner_1"] = {
            "online": True,
            "offline_alerted": False,
            "chip_health_degraded": True,
            "shares": {},
            "pool_identity": (None, None),
            "blocks_found": 0,
        }
        status = luxos_status(chip_status="unknown", recovery_armed=False, pending=False)
        status["hashrate_ths"] = status["expected_hashrate_ths"]
        status["pool"]["connected"] = True
        await engine.evaluate_miner(status, {"id": "miner_1", "type": "luxos"})
        self.assertEqual(list(engine.alert_feed), [])
        self.assertTrue(engine.previous["miner_1"]["chip_health_degraded"])


if __name__ == "__main__":
    unittest.main()
