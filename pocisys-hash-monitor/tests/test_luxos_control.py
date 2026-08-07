import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from services.luxos_control import LuxOSClient, LuxOSControlError, LuxOSControlService


class RecordingClient(LuxOSClient):
    def __init__(self, responses):
        super().__init__("test-miner")
        self.responses = list(responses)
        self.calls = []

    def call(self, command, parameter=None):
        self.calls.append((command, parameter))
        if not self.responses:
            raise AssertionError(f"Unexpected LuxOS call: {command}")
        return self.responses.pop(0)


class LuxOSClientTests(unittest.TestCase):
    def test_profiles_adjust_catalog_power_to_the_current_setup(self):
        client = RecordingClient([
            {"PROFILES": [
                {"Profile Name": "low", "Frequency": 245, "Watts": 1200},
                {"Profile Name": "Loki", "Frequency": 325, "Watts": 1590},
            ]},
            {"CONFIG": [{"Profile": "Loki"}]},
            {"POWER": [{"PSU": False, "Watts": 1110}]},
            {"ASCS": [{"Count": 2}]},
        ])

        result = client.profiles()

        self.assertEqual(result["current_power_watts"], 1110)
        self.assertFalse(result["power_reported_by_psu"])
        self.assertEqual(result["detected_boards"], 2)
        self.assertAlmostEqual(result["profile_power_scale"], 1110 / 1590)
        self.assertEqual(result["profiles"][0]["setup_watts"], 838)
        self.assertEqual(result["profiles"][1]["setup_watts"], 1110)

    def test_session_batch_uses_and_releases_own_session(self):
        client = RecordingClient([
            {"SESSION": [{"SessionID": "abc"}]},
            {"STATUS": [{"STATUS": "S"}]},
            {"STATUS": [{"STATUS": "S"}]},
            {"STATUS": [{"STATUS": "S"}]},
        ])
        client.session_batch([("reboot", "0,0"), ("reboot", "1,0")])
        self.assertEqual(client.calls, [
            ("logon", None),
            ("reboot", "abc,0,0"),
            ("reboot", "abc,1,0"),
            ("logoff", "abc"),
        ])

    def test_busy_session_is_never_stolen(self):
        client = RecordingClient([{"SESSION": [{}]}])
        with self.assertRaisesRegex(LuxOSControlError, "will not steal"):
            client.session_call("profileset", "normal")
        self.assertEqual(client.calls, [("logon", None)])


class FakeAlerts:
    def __init__(self):
        self.events = []

    async def emit(self, *args, **kwargs):
        self.events.append((args, kwargs))
        return True

    def dashboard_link(self, path=""):
        return path


class FakeControlClient:
    instances = []
    timeline = []

    def __init__(self, ip, timeout=5):
        self.ip = ip
        self.timeout = timeout
        self.calls = []
        type(self).instances.append(self)

    def profiles(self):
        self.calls.append(("profiles",))
        type(self).timeline.append(("profiles",))
        return {
            "profiles": [
                {"name": "normal", "watts": 3000},
                {"name": "low", "watts": 1800},
            ],
            "current_profile": "normal",
        }

    def set_profile(self, profile):
        self.calls.append(("set_profile", profile))
        type(self).timeline.append(("set_profile", profile))

    def board_count(self):
        self.calls.append(("board_count",))
        type(self).timeline.append(("board_count",))
        return 3

    def set_boards(self, board_ids, delay_seconds):
        self.calls.append(("set_boards", list(board_ids), delay_seconds))
        type(self).timeline.append(("set_boards", list(board_ids), delay_seconds))

    def restart_board(self, board_id, delay_seconds=10):
        self.calls.append(("restart_board", board_id, delay_seconds))
        type(self).timeline.append(("restart_board", board_id, delay_seconds))

    def chip_health(self, threshold):
        self.calls.append(("chip_health", threshold))
        type(self).timeline.append(("chip_health", threshold))
        return {
            "reported": True,
            "healthy": 2,
            "total": 3,
            "items": [
                {"board_id": 0, "name": "Hashboard 1", "status": "warning", "low_chip_count": 1},
                {"board_id": 1, "name": "Hashboard 2", "status": "healthy", "low_chip_count": 0},
                {"board_id": 2, "name": "Hashboard 3", "status": "healthy", "low_chip_count": 0},
            ],
            "score_threshold": threshold,
            "current_profile": "normal",
            "checked_at": "2026-08-07T00:00:00+00:00",
        }


def control_config(low_mode="boards_off"):
    return {
        "app": {
            "luxos_control_enabled": True,
            "request_timeout_seconds": 4,
            "control_timezone": "UTC",
        },
        "discord": {"send_control_alerts": True},
        "miners": [{
            "id": "miner_1",
            "name": "Test LuxOS",
            "ip": "miner.test",
            "type": "luxos",
            "enabled": True,
            "control_enabled": True,
            "control_schedule_enabled": False,
            "control_low_mode": low_mode,
            "control_low_profile": "low",
            "control_full_profile": "normal",
            "auto_recover_hashboards": False,
            "chip_health_score_threshold": 90,
        }],
    }


class LuxOSControlServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeControlClient.instances.clear()
        FakeControlClient.timeline.clear()

    async def test_sleep_turns_all_boards_off_without_curtail(self):
        service = LuxOSControlService(control_config(), FakeAlerts())
        with patch("services.luxos_control.LuxOSClient", FakeControlClient):
            result = await service.execute("miner_1", "low")
        calls = [call for client in FakeControlClient.instances for call in client.calls]
        self.assertIn(("set_boards", [0, 1, 2], 0), calls)
        self.assertFalse(any(call[0] in {"curtail", "sleep"} for call in calls))
        self.assertIn("whole-miner curtail sleep was not used", result["event"]["message"])

    async def test_wake_skips_matching_profile_then_starts_every_board(self):
        service = LuxOSControlService(control_config(), FakeAlerts())
        with patch("services.luxos_control.LuxOSClient", FakeControlClient):
            await service.execute("miner_1", "full")
        calls = FakeControlClient.timeline
        self.assertNotIn(("set_profile", "normal"), calls)
        self.assertIn(("set_boards", [0, 1, 2], 10), calls)

    async def test_matching_profile_action_does_not_send_profileset(self):
        service = LuxOSControlService(control_config(low_mode="profile"), FakeAlerts())
        with patch("services.luxos_control.LuxOSClient", FakeControlClient):
            result = await service.execute("miner_1", "full")
        self.assertNotIn(("set_profile", "normal"), FakeControlClient.timeline)
        self.assertIn("already active", result["event"]["message"])

    async def test_schedule_enable_does_not_reapply_matching_profile(self):
        config = control_config(low_mode="profile")
        config["miners"][0]["control_schedule_enabled"] = True
        service = LuxOSControlService(config, FakeAlerts())
        service.health_cache["miner_1"] = {"current_profile": "normal"}
        with (
            patch.object(service, "_desired_target", return_value="full"),
            patch("services.luxos_control.LuxOSClient", FakeControlClient),
        ):
            await service._evaluate_schedules()
        self.assertNotIn(("set_profile", "normal"), FakeControlClient.timeline)
        self.assertEqual(service.last_schedule_target["miner_1"], "full")

    async def test_curtailed_profile_cannot_exceed_normal_ceiling(self):
        config = control_config(low_mode="profile")
        config["miners"][0]["control_low_profile"] = "normal"
        config["miners"][0]["control_full_profile"] = "low"
        service = LuxOSControlService(config, FakeAlerts())
        with patch("services.luxos_control.LuxOSClient", FakeControlClient):
            with self.assertRaisesRegex(LuxOSControlError, "above the Normal Operating Profile"):
                await service.execute("miner_1", "low")

    async def test_arbitrary_profile_action_is_not_supported(self):
        service = LuxOSControlService(control_config(low_mode="profile"), FakeAlerts())
        with patch("services.luxos_control.LuxOSClient", FakeControlClient):
            with self.assertRaisesRegex(LuxOSControlError, "Unsupported"):
                await service.execute("miner_1", "profile")

    async def test_peak_schedule_supports_same_day_and_overnight_windows(self):
        config = control_config()
        miner = config["miners"][0]
        service = LuxOSControlService(config, FakeAlerts())
        miner.update(control_low_time="16:00", control_full_time="21:00")
        self.assertTrue(service._low_window(miner, datetime(2026, 8, 7, 18, 0, tzinfo=timezone.utc)))
        self.assertFalse(service._low_window(miner, datetime(2026, 8, 7, 22, 0, tzinfo=timezone.utc)))
        miner.update(control_low_time="22:00", control_full_time="07:00")
        self.assertTrue(service._low_window(miner, datetime(2026, 8, 7, 23, 0, tzinfo=timezone.utc)))
        self.assertTrue(service._low_window(miner, datetime(2026, 8, 7, 6, 0, tzinfo=timezone.utc)))
        self.assertFalse(service._low_window(miner, datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)))

    async def test_armed_control_enforces_normal_profile_ceiling_without_schedule(self):
        config = control_config(low_mode="profile")
        config["miners"][0]["control_full_profile"] = "low"
        service = LuxOSControlService(config, FakeAlerts())
        service.health_cache["miner_1"] = {"current_profile": "normal"}
        with patch("services.luxos_control.LuxOSClient", FakeControlClient):
            await service._evaluate_schedules()
        self.assertIn(("set_profile", "low"), FakeControlClient.timeline)

    async def test_control_action_memory_is_strictly_bounded(self):
        config = control_config()
        config["discord"]["send_control_alerts"] = False
        service = LuxOSControlService(config, FakeAlerts())
        miner = config["miners"][0]
        for index in range(100):
            await service._record_action(miner, "test", True, f"event {index}", "unit test")
        self.assertEqual(len(service.recent_actions), 25)
        self.assertEqual(service.recent_actions[0]["message"], "event 99")
        self.assertEqual(service.recent_actions[-1]["message"], "event 75")

    async def test_three_bad_health_checks_restart_only_affected_board(self):
        config = control_config()
        config["miners"][0]["auto_recover_hashboards"] = True
        service = LuxOSControlService(config, FakeAlerts())
        service.started_at = time.monotonic() - 600
        snapshot = FakeControlClient("miner.test").chip_health(90)
        with patch("services.luxos_control.LuxOSClient", FakeControlClient):
            await service._process_auto_recovery(config["miners"][0], snapshot)
            await service._process_auto_recovery(config["miners"][0], snapshot)
            await service._process_auto_recovery(config["miners"][0], snapshot)
        calls = [call for client in FakeControlClient.instances for call in client.calls]
        self.assertIn(("restart_board", 0, 10), calls)
        self.assertNotIn(("restart_board", 1, 10), calls)
        self.assertNotIn(("restart_board", 2, 10), calls)
        self.assertLessEqual(len(service.recent_actions), 25)

    async def test_zero_health_threshold_is_not_replaced_by_default(self):
        config = control_config()
        config["miners"][0]["chip_health_score_threshold"] = 0
        service = LuxOSControlService(config, FakeAlerts())
        with patch("services.luxos_control.LuxOSClient", FakeControlClient):
            await service.refresh_health(config["miners"][0], force=True)
        self.assertIn(("chip_health", 0.0), FakeControlClient.timeline)


if __name__ == "__main__":
    unittest.main()
