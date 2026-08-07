# PoCiSys Hash Monitor

PoCiSys Hash Monitor is a lightweight dashboard for SHA-256 miners. It combines
hashrate, temperatures, cooling, shares, pool status, network data, and optional
Discord alerts in one place. Monitoring is read-only by default.

## Install

1. Add [12Gauge's PoCiSys Store](https://github.com/12gaugekenshin/12Gauge-Umbrel-Community-Store#add-the-store-to-umbrel) to Umbrel.
2. Install **PoCiSys Hash Monitor**.
3. Open **Settings** and add each miner's local IP address.
4. Save, return to the dashboard, and confirm the miners report online.

Supported telemetry includes AxeOS/NerdAxe-style devices and LuxOS miners.
Non-LuxOS miners always remain read-only.

## Optional setup

- Add your local Public Pool URL for pool worker and share data.
- Add a Discord webhook for outage, recovery, temperature, hashrate, pool,
  share-quality, best-difficulty, and block alerts.
- Enable the authenticated read-only MCP connection if Hermes should answer
  questions about local miner, pool, block-odds, or system telemetry.
- Explicitly arm LuxOS Control Mode for selected LuxOS miners to use existing
  native profiles, scheduled curtailment, individual hashboard Sleep/wake, or
  guarded hashboard recovery. It never creates profiles or directly sets
  frequency and voltage values.

MCP tokens are shown once. Store one somewhere safe before closing the setup
screen.

## Storage and privacy

The app stores only its small configuration file. Current telemetry is kept in
bounded memory; there is no growing long-term hashrate database. Miner controls,
wallet data, and shell access are not exposed through MCP. Recent LuxOS control
activity is also held in a fixed-size in-memory queue.

## Latest release — v1.6.0

Adds opt-in LuxOS Control Mode with a per-miner normal-profile ceiling,
peak-period curtailment, per-hashboard Sleep/wake controls, and separately
enabled guarded chip-health recovery. Sleep turns off each hashboard while
leaving the LuxOS controller online so it can wake on schedule.

Source and support: [PoCiSys Hash Monitor on GitHub](https://github.com/12gaugekenshin/PoCi-Hash-Monitor)

Built by [12GaugeKenshin](https://github.com/12gaugekenshin) ·
[PoCiSys](https://pocisys.io/) · [X](https://x.com/12gaugekenshin)
