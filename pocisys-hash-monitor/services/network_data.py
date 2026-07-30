from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from datetime import datetime, timezone


MAX_NETWORK_RESPONSE_BYTES = 512 * 1024
REFRESH_SECONDS = 600
COINS = ("btc", "bch", "bsv", "xec", "dgb", "chta")


def _read_text(url: str, timeout: float = 8.0):
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json, text/plain", "User-Agent": "PoCiSys-Hash-Monitor/1.4"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(MAX_NETWORK_RESPONSE_BYTES + 1)
    if len(body) > MAX_NETWORK_RESPONSE_BYTES:
        raise ValueError("Network data response exceeded the 512 KB safety limit")
    return body.decode("utf-8", errors="replace").strip()


def _read_json(url: str, timeout: float = 8.0):
    return json.loads(_read_text(url, timeout))


class NetworkDataService:
    """Keeps one current SHA-256 coin snapshot; no history is retained."""

    def __init__(self):
        self.latest = {coin: {"available": False} for coin in COINS}
        self.latest["updated_at"] = None
        self.running = False
        self.task = None

    def snapshot(self):
        result = {coin: dict(self.latest.get(coin, {})) for coin in COINS}
        result["updated_at"] = self.latest.get("updated_at")
        return result

    @staticmethod
    def _failed(previous, exc):
        return {**previous, "error": str(exc)[:240]}

    def _fetch(self):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        previous = self.latest
        result = {coin: {"available": False} for coin in COINS}
        result["updated_at"] = now

        prices = {}
        try:
            prices = _read_json(
                "https://api.coingecko.com/api/v3/simple/price"
                "?ids=bitcoin,bitcoin-cash,bitcoin-cash-sv,ecash,digibyte,cheetahcoin&vs_currencies=usd"
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            # Network odds remain useful when the optional market-price request fails.
            prices = {}

        try:
            mining = _read_json("https://mempool.space/api/v1/mining/hashrate/3d")
            hashrate_hs = float(mining.get("currentHashrate") or 0)
            result["btc"] = {
                "available": hashrate_hs > 0,
                "network_hashrate_eh": hashrate_hs / 1e18 if hashrate_hs else None,
                "difficulty": mining.get("currentDifficulty"),
                "price_usd": prices.get("bitcoin", {}).get("usd"),
                "blocks_per_day": 144,
                "source": "mempool.space",
                "updated_at": now,
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            result["btc"] = self._failed(previous.get("btc", {}), exc)

        try:
            payload = _read_json("https://api.blockchair.com/bitcoin-cash/stats")
            stats = payload.get("data", {}) if isinstance(payload, dict) else {}
            hashrate_hs = float(stats.get("hashrate_24h") or 0)
            result["bch"] = {
                "available": hashrate_hs > 0,
                "network_hashrate_eh": hashrate_hs / 1e18 if hashrate_hs else None,
                "difficulty": stats.get("difficulty"),
                "price_usd": prices.get("bitcoin-cash", {}).get("usd") or stats.get("market_price_usd"),
                "blocks_per_day": 144,
                "source": "Blockchair",
                "updated_at": now,
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            result["bch"] = self._failed(previous.get("bch", {}), exc)

        try:
            stats = _read_json("https://api.whatsonchain.com/v1/bsv/main/chain/info")
            difficulty = float(stats.get("difficulty") or 0)
            # Standard SHA-256 difficulty-to-hashrate estimate at a 10-minute target.
            hashrate_hs = difficulty * (2**32) / 600 if difficulty else 0
            result["bsv"] = {
                "available": hashrate_hs > 0,
                "network_hashrate_eh": hashrate_hs / 1e18 if hashrate_hs else None,
                "difficulty": difficulty,
                "price_usd": prices.get("bitcoin-cash-sv", {}).get("usd"),
                "blocks_per_day": 144,
                "source": "WhatsOnChain",
                "estimate_note": "Hashrate is estimated from current network difficulty.",
                "updated_at": now,
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            result["bsv"] = self._failed(previous.get("bsv", {}), exc)

        try:
            payload = _read_json("https://api.blockchair.com/ecash/stats")
            stats = payload.get("data", {}) if isinstance(payload, dict) else {}
            hashrate_hs = float(stats.get("hashrate_24h") or 0)
            result["xec"] = {
                "available": hashrate_hs > 0,
                "network_hashrate_eh": hashrate_hs / 1e18 if hashrate_hs else None,
                "difficulty": stats.get("difficulty"),
                "price_usd": prices.get("ecash", {}).get("usd") or stats.get("market_price_usd"),
                "blocks_per_day": 144,
                "source": "Blockchair",
                "updated_at": now,
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            result["xec"] = self._failed(previous.get("xec", {}), exc)

        try:
            payload = _read_json("https://digihash.digibyte.io/api/stats")
            pools = payload.get("pools", {}) if isinstance(payload, dict) else {}
            sha_pool = next(
                (item for item in pools.values() if str(item.get("algorithm", "")).lower() == "sha256d"),
                {},
            )
            stats = sha_pool.get("poolStats", {}) if isinstance(sha_pool, dict) else {}
            hashrate_hs = float(stats.get("networkHash") or 0)
            result["dgb"] = {
                "available": hashrate_hs > 0,
                "network_hashrate_eh": hashrate_hs / 1e18 if hashrate_hs else None,
                "difficulty": stats.get("networkDiff"),
                "price_usd": prices.get("digibyte", {}).get("usd"),
                # Each of DigiByte's five algorithms targets one block every 75 seconds.
                "blocks_per_day": 1152,
                "source": "DigiHash SHA-256",
                "updated_at": now,
            }
        except (OSError, ValueError, TypeError, KeyError, StopIteration, json.JSONDecodeError) as exc:
            result["dgb"] = self._failed(previous.get("dgb", {}), exc)

        try:
            base = "http://chtaexplorer.mooo.com:3002/api"
            hashrate_hs = float(_read_text(f"{base}/getnetworkhashps"))
            difficulty = float(_read_text(f"{base}/getdifficulty"))
            result["chta"] = {
                "available": hashrate_hs > 0,
                "network_hashrate_eh": hashrate_hs / 1e18 if hashrate_hs else None,
                "difficulty": difficulty,
                "price_usd": prices.get("cheetahcoin", {}).get("usd"),
                "blocks_per_day": 720,
                "source": "CHTA explorer",
                "estimate_note": "CEA / RandomSpike estimate; CHTA conditions can change abruptly.",
                "updated_at": now,
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            result["chta"] = self._failed(previous.get("chta", {}), exc)

        self.latest = result

    async def refresh(self):
        await asyncio.to_thread(self._fetch)
        return self.snapshot()

    async def run(self):
        self.running = True
        while self.running:
            started = time.monotonic()
            await self.refresh()
            delay = max(5, REFRESH_SECONDS - (time.monotonic() - started))
            await asyncio.sleep(delay)

    def start(self):
        if not self.task:
            self.task = asyncio.create_task(self.run())

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
