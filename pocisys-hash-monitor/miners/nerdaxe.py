from __future__ import annotations

from .axeos import AxeOSDriver


class NerdAxeDriver(AxeOSDriver):
    """Separate extension point for NerdAxe/NerdQaxe API variations."""

    api_paths = ("/api/system/info", "/api/info", "/api/system")

    def poll(self):
        result = super().poll()
        if result["api_ok"] and not result["firmware"]:
            result["firmware"] = "NerdAxe"
        return result
