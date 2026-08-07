from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from miners import get_driver
from .ping import tcp_ping


class MinerPoller:
    def __init__(self, config: dict, alert_engine, status_enricher=None):
        self.config = config
        self.alert_engine = alert_engine
        self.status_enricher = status_enricher
        self.latest = {}
        self.last_poll = None
        self.running = False
        self.task = None
        self.wake_event = asyncio.Event()

    async def _poll_one(self, miner):
        timeout = self.config.get("app", {}).get("request_timeout_seconds", 4)
        try:
            driver = get_driver(miner, timeout)
            status = await asyncio.to_thread(driver.poll)
        except Exception as exc:
            status = {
                "name": miner.get("name", miner.get("ip", "Unknown")),
                "ip": miner.get("ip"),
                "type": miner.get("type"),
                "group": miner.get("group", "Ungrouped"),
                "online": False,
                "api_ok": False,
                "ping_ms": None,
                "hashrate_ths": None,
                "expected_hashrate_ths": None,
                "temps": {"asic_c": None, "vrm_c": None, "board_c": None, "chip_c": None},
                "chip_health": {"reported": False, "healthy": None, "total": None, "items": []},
                "fans": [],
                "pool": {"url": None, "user": None, "connected": None, "status": "unknown", "source": None},
                "shares": {"valid": 0, "invalid": 0, "stale": 0, "rejected": 0},
                "difficulty": {"best_session": None, "best_all_time": None},
                "uptime_seconds": None,
                "firmware": None,
                "blocks_found": 0,
                "status": "Driver error",
                "warnings": [str(exc)],
                "raw": {},
            }

        if status.get("api_ok"):
            status["online"] = True
        else:
            reachable, latency, port = await asyncio.to_thread(tcp_ping, miner["ip"], (80, 443, 4028), min(timeout, 1.5))
            status["online"] = reachable
            status["ping_ms"] = latency
            if reachable:
                status["status"] = f"Reachable on port {port}; API failed"
        # A full API payload is useful only while normalizing one poll. Never
        # retain it in latest_status or expose it to the dashboard.
        status.pop("raw", None)
        status["id"] = miner.get("id")
        status["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if self.status_enricher:
            status = self.status_enricher(status)
        await self.alert_engine.evaluate_miner(status, miner)
        return miner, status

    async def poll_now(self):
        miners = [dict(item) for item in self.config.get("miners", []) if item.get("enabled", True)]
        results = await asyncio.gather(*(self._poll_one(miner) for miner in miners))
        self.latest = {
            miner.get("id") or f"{miner.get('type', 'miner')}:{miner['ip']}": status
            for miner, status in results
        }
        self.last_poll = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return self.statuses()

    def statuses(self):
        order = {
            miner.get("id") or f"{miner.get('type', 'miner')}:{miner.get('ip')}": miner.get("display_order", index)
            for index, miner in enumerate(self.config.get("miners", []))
        }
        return sorted(
            self.latest.values(),
            key=lambda item: order.get(item.get("id") or f"{item.get('type', 'miner')}:{item.get('ip')}", 9999),
        )

    async def run(self):
        self.running = True
        while self.running:
            self.wake_event.clear()
            await self.poll_now()
            interval = max(2, self.config.get("app", {}).get("poll_interval_seconds", 10))
            try:
                await asyncio.wait_for(self.wake_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    def reconfigure(self):
        configured_ids = {
            item.get("id") or f"{item.get('type', 'miner')}:{item.get('ip')}"
            for item in self.config.get("miners", [])
            if item.get("enabled", True)
        }
        self.latest = {miner_id: status for miner_id, status in self.latest.items() if miner_id in configured_ids}
        self.wake_event.set()

    def start(self):
        if not self.task:
            self.task = asyncio.create_task(self.run())

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
