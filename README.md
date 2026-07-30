# 12Gauge's Community Store

One Umbrel community-store source for the apps maintained by
[`12gaugekenshin`](https://github.com/12gaugekenshin).

## Add to Umbrel

Open the Umbrel App Store, choose **Community App Stores**, and add:

```text
https://github.com/12gaugekenshin/12Gauge-Umbrel-Community-Store
```

## Included apps

| App | App ID | Source repository |
| --- | --- | --- |
| PoCiSys Gateway | `pocisys-gateway` | [PoCisys-Gateway](https://github.com/12gaugekenshin/PoCisys-Gateway) |
| PoCiSys GPU Runtime | `pocisys-gpu-runtime` | [PoCiSys-GPU-Runtime](https://github.com/12gaugekenshin/PoCiSys-GPU-Runtime) |
| PoCiSys Hash Monitor | `pocisys-hash-monitor` | [PoCi-Hash-Monitor](https://github.com/12gaugekenshin/PoCi-Hash-Monitor) |
| PoCiSys Public Pool Port | `pocisys-public-pool-port` | [Pocisys-public-pool-port](https://github.com/12gaugekenshin/Pocisys-public-pool-port) |
| Miner Watchdog | `minerwatch-discord-watchdog` | [minerwatch-discord-watchdog](https://github.com/12gaugekenshin/minerwatch-discord-watchdog) |

The application source and container builds remain in their individual
repositories. This repository contains only the Umbrel app packages needed for
discovery, installation, and updates through one store URL.

## Maintenance

When an app releases a new version, copy its matching Umbrel package directory
into this repository and commit the change. Existing Umbrel installations keep
their data because the app IDs do not change.
