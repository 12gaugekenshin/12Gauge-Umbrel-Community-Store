# PoCiSys BCHN&SP

A self-contained, pruned Bitcoin Cash Node plus BCH-native solo Stratum pool
for UmbrelOS. It is designed for direct ASIC connections, including LuxOS.

## Connect a miner

- URL: `stratum+tcp://YOUR_UMBREL_IP:3335`
- Username: `bitcoincash:YOUR_CASHADDR.worker-name`
- Password: `x`

Each worker's CashAddr is validated and embedded directly in its coinbase job.
PoCiSys never receives or stores a private key and never holds a payout balance.

## Resource design

- BCHN automatically prunes raw block data to a 10 GiB target.
- BCHN cache is 128 MiB and mempool is capped at 50 MiB.
- Containers have hard memory limits: node 1 GiB, engine 256 MiB, dashboard 128 MiB.
- Only 512 worker identities and 50 accepted-share summaries are retained in RAM.
- The dashboard displays the newest 10; share history resets with the engine.
- RPC is private to the app network. Only P2P 8337, Stratum 3335, and read-only
  telemetry 2022 are published on the host.

Expect roughly 11–20 GiB total storage after synchronization: the 10 GiB prune
target does not include chainstate, indexes, logs, or container layers.

## Safety

Solo mining is probabilistic. A reachable Stratum service is not proof that a
share or block will be accepted. Test configurations carefully. This software
is provided without warranty; its authors are not responsible for mining
revenue loss, misconfiguration, data loss, or hardware damage.

## Attribution and license

This distribution is GPL-3.0 because it modifies GoStratumEngine. BCHN runs as
a separate MIT-licensed service. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Built by [12GaugeKenshin](https://github.com/12gaugekenshin) ·
[PoCiSys.io](https://pocisys.io/)
