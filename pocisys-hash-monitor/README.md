# PoCiSys Hash Monitor

PoCiSys Hash Monitor is a lightweight, read-only dashboard for SHA-256 miners.
It combines hashrate, temperatures, cooling, shares, pool status, network data,
and optional Discord alerts in one place.

## Install

1. Add [12Gauge's PoCiSys Store](https://github.com/12gaugekenshin/12Gauge-Umbrel-Community-Store#add-the-store-to-umbrel) to Umbrel.
2. Install **PoCiSys Hash Monitor**.
3. Open **Settings** and add each miner's local IP address.
4. Save, return to the dashboard, and confirm the miners report online.

Supported telemetry includes AxeOS/NerdAxe-style devices and LuxOS miners.
PoCiSys reads their status; it does not change miner settings.

## Optional setup

- Add your local Public Pool URL for pool worker and share data.
- Add a Discord webhook for outage, recovery, temperature, hashrate, pool,
  share-quality, best-difficulty, and block alerts.
- Enable the authenticated read-only MCP connection if Hermes should answer
  questions about local miner, pool, block-odds, or system telemetry.

MCP tokens are shown once. Store one somewhere safe before closing the setup
screen.

## Storage and privacy

The app stores only its small configuration file. Current telemetry is kept in
bounded memory; there is no growing long-term hashrate database. Miner controls,
wallet data, and shell access are not exposed through MCP.

Source and support: [PoCiSys Hash Monitor on GitHub](https://github.com/12gaugekenshin/PoCi-Hash-Monitor)
