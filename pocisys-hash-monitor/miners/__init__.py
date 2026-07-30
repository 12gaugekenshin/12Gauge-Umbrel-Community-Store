from .axeos import AxeOSDriver
from .cgminer import AvalonDriver, CgminerDriver
from .luxos import LuxOSDriver
from .nerdaxe import NerdAxeDriver

DRIVERS = {
    "avalon": AvalonDriver,
    "axeos": AxeOSDriver,
    "bitaxe": AxeOSDriver,
    "canaan_avalon": AvalonDriver,
    "cgminer": CgminerDriver,
    "luxos": LuxOSDriver,
    "nerdaxe": NerdAxeDriver,
    "nerdqaxe": NerdAxeDriver,
}


def get_driver(miner, timeout=4.0):
    driver_class = DRIVERS.get(str(miner.get("type", "")).lower())
    if not driver_class:
        raise ValueError(f"Unsupported miner type: {miner.get('type')}")
    return driver_class(miner, timeout)
