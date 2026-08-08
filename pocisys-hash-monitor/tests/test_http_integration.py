from __future__ import annotations

import importlib
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path


class HttpIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        config_path = Path(cls.tempdir.name) / "config.json"
        shutil.copyfile(Path(__file__).resolve().parents[1] / "config.default.json", config_path)
        os.environ["POCISYS_CONFIG_PATH"] = str(config_path)
        cls.config_path = config_path
        cls.app = importlib.import_module("main")
        cls.event_thread = threading.Thread(target=cls.app.run_event_loop, daemon=True)
        cls.event_thread.start()
        if not cls.app.loop_started.wait(5):
            raise RuntimeError("PoCiSys event loop did not start")
        cls.app.system_stats.start()
        cls.server = cls.app.PoCiSysServer(("127.0.0.1", 0), cls.app.PoCiSysHandler)
        cls.http_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.http_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.app.shutdown_services()
        cls.http_thread.join(timeout=3)
        cls.event_thread.join(timeout=3)
        cls.tempdir.cleanup()

    @classmethod
    def request(cls, path, method="GET", body=None, token=None, extra_headers=None):
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if extra_headers:
            headers.update(extra_headers)
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{cls.base_url}{path}",
            data=payload,
            method=method,
            headers=headers,
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None

    def test_token_auth_and_mcp_tools_over_http(self):
        status, health = self.request("/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["version"], "1.7.1")

        _, generated = self.request("/api/hermes/token", method="POST", body={})
        token = generated["token"]
        stored = self.config_path.read_text(encoding="utf-8")
        self.assertNotIn(token, stored)

        _, settings = self.request("/api/settings")
        settings["hermes_enabled"] = True
        self.request("/api/settings", method="PUT", body=settings)

        tools_list = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request("/mcp", method="POST", body=tools_list)
        self.assertEqual(denied.exception.code, 401)

        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "integration-test", "version": "1.0"},
            },
        }
        _, initialized = self.request("/mcp", method="POST", body=initialize, token=token)
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "pocisys-hash-monitor")

        _, listed = self.request("/mcp", method="POST", body=tools_list, token=token)
        self.assertEqual(len(listed["result"]["tools"]), 6)

        with self.assertRaises(urllib.error.HTTPError) as cross_origin:
            self.request(
                "/mcp",
                method="POST",
                body=tools_list,
                token=token,
                extra_headers={"Origin": "https://attacker.example"},
            )
        self.assertEqual(cross_origin.exception.code, 403)

        system_call = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "get_pocisys_system_health", "arguments": {}},
        }
        _, system_result = self.request("/mcp", method="POST", body=system_call, token=token)
        self.assertFalse(system_result["result"]["isError"])
        self.assertEqual(
            system_result["result"]["structuredContent"]["system"]["scope"],
            "container-visible host metrics",
        )

    def test_used_hardware_can_save_a_low_chip_health_threshold(self):
        miner = self.app.clean_miner({
            "name": "Used LuxOS miner",
            "ip": "192.0.2.10",
            "type": "luxos",
            "chip_health_score_threshold": 20,
        })
        self.assertEqual(miner["chip_health_score_threshold"], 20)
        miner["chip_health_score_threshold"] = self.app.clean_miner({
            "name": "Used LuxOS miner",
            "ip": "192.0.2.10",
            "type": "luxos",
            "chip_health_score_threshold": 0,
        })["chip_health_score_threshold"]
        self.assertEqual(miner["chip_health_score_threshold"], 0)

    def test_recovery_only_does_not_require_curtailment_profiles(self):
        miner = self.app.clean_miner({
            "name": "Recovery-only LuxOS miner",
            "ip": "192.0.2.11",
            "type": "luxos",
            "control_enabled": False,
            "control_schedule_enabled": False,
            "auto_recover_hashboards": True,
        })
        self.assertFalse(miner["control_enabled"])
        self.assertFalse(miner["control_schedule_enabled"])
        self.assertTrue(miner["auto_recover_hashboards"])


if __name__ == "__main__":
    unittest.main()
