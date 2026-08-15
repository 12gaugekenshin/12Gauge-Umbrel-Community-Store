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

- Add a stock Public Pool API or PoCiSys Public Pool Port URL for pool worker
  data. PoCiSys Public Pool Port uses `http://<umbrel-ip>:2020` and supplies
  exact accepted-share difficulty.
- Add a Discord webhook for outage, recovery, temperature, hashrate, pool,
  share-quality, best-difficulty, and block alerts.
- Enable the authenticated read-only MCP connection if Hermes should answer
  questions about local miner, pool, block-odds, or system telemetry.
- Explicitly arm LuxOS Control Mode for selected LuxOS miners to use existing
  native profiles, scheduled curtailment, individual hashboard Sleep/wake, or
  guarded hashboard recovery. It never creates profiles or directly sets
  frequency and voltage values.
- Download a safe configuration backup or sanitized bounded diagnostic report
  from Settings. Backup restore validates every miner and pool before saving.
- Add PoCiSys to a phone or desktop home screen. Full PWA installation and its
  fixed-size offline shell require HTTPS, such as an HTTPS Tailscale address;
  the normal dashboard remains available over local HTTP.

MCP tokens are shown once. Store one somewhere safe before closing the setup
screen.

## Storage and privacy

The app stores its small configuration plus at most ten accepted-share records
per configured pool. Current miner telemetry is kept in bounded memory; there is
no growing long-term hashrate database. Miner controls, wallet data, and shell
access are not exposed through MCP. Recent LuxOS control activity and health
transitions are held in fixed-size in-memory queues.

## Safety and liability

**PoCiSys Hash Monitor is provided “as is” without warranty.** Monitoring data
may be delayed, incomplete, or inaccurate and must not be treated as a hardware
safety system. Enabling miner controls, native LuxOS profiles, curtailment, or
hashboard recovery is done entirely at the user's risk. To the maximum extent
permitted by law, PoCiSys, 12GaugeKenshin, and contributors are not responsible
for hardware damage, overheating, downtime, lost mining revenue, increased power
costs, data loss, or other direct or indirect damages. Users are responsible for
verifying power, cooling, firmware, and manufacturer limits.

## Latest release — v1.9.1

Adds fleet-wide total-share, session-best, and best-recorded difficulty metrics
to the main dashboard and Screen page. Settings can now reset PoCiSys's bounded
local pool share and difficulty records without changing miner or upstream pool
counters.

Source and support: [PoCiSys Hash Monitor on GitHub](https://github.com/12gaugekenshin/PoCi-Hash-Monitor)

Built by [12GaugeKenshin](https://github.com/12gaugekenshin) ·
[PoCiSys](https://pocisys.io/) · [X](https://x.com/12gaugekenshin)
