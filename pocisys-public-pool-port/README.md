# PoCiSys Public Pool Port

PoCiSys Public Pool Port lets your ASICs solo mine through your own synchronized
Umbrel Bitcoin Node. It runs beside Umbrel's stock Public Pool and adds a clear
verification path from candidate share through Bitcoin Core acceptance and
100-block coinbase maturity.

## Before installing

- Install and fully synchronize **Bitcoin Node** on Umbrel.
- Keep the stock Public Pool on port `3333` if you use it.
- PoCiSys Public Pool Port uses the separate port `3334`.

## Install

1. Add [12Gauge's PoCiSys Store](https://github.com/12gaugekenshin/12Gauge-Umbrel-Community-Store#add-the-store-to-umbrel) to Umbrel.
2. Install **PoCiSys Public Pool Port**.
3. Open the app and confirm **Pool + node online**.
4. Point a miner at:

   ```text
   Host:     umbrel.local
   Port:     3334
   Username: <your Bitcoin payout address>.<worker name>
   Password: x
   ```

5. Wait for the worker to appear on the dashboard.

Use your Umbrel's LAN IP instead of `umbrel.local` if your miner cannot resolve
local hostnames.

## What the block states mean

- **Candidate proof:** the submitted header meets the recorded target.
- **Node acceptance:** Bitcoin Core accepted the reconstructed block.
- **Active chain:** the block is part of your node's best chain.
- **Mature:** the coinbase has reached 100 confirmations.

A difficult share is never labeled as a confirmed block. The complete ASIC to
Bitcoin Core submission path has also been validated with an isolated regtest
node without changing the production mainnet node.

## Network access

For local mining, no router changes are needed. If you deliberately accept
miners over the internet, forward only TCP port `3334`. Never expose Bitcoin
RPC, Public Pool's private API, or the Umbrel dashboard.

This app uses Benjamin Wilson's GPL-3.0 Public Pool engine. Exact upstream
attribution is recorded in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Source and support: [PoCiSys Public Pool Port on GitHub](https://github.com/12gaugekenshin/Pocisys-public-pool-port)
