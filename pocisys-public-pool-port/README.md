# PoCiSys Public Pool Port

An umbrelOS-native solo Bitcoin pool that keeps the upstream Public Pool mining
engine and adds PoCiSys operations, history, and block assurance.

## What is wired

- Automatic Bitcoin Node dependency through Umbrel's injected RPC host,
  port, username, and password.
- Upstream Public Pool server pinned to the same image digest used by the
  current Umbrel App Store package.
- Stratum V1 on TCP port `3333`.
- A private Public Pool API; it is not published on a host port.
- A read-only mount of Public Pool's SQLite ledger into the PoCiSys verifier.
- Persistent PoCiSys history in a separate SQLite/WAL database.
- Responsive PoCiSys dashboard and four-stat Umbrel widget.
- Multi-architecture application image source with no third-party Python
  packages; `python:3.12-alpine` supports amd64 and arm64.

## Block assurance

Public Pool persists raw candidate blocks after it calls Bitcoin Core's
`submitblock`. The verifier independently:

1. hashes the serialized 80-byte header twice with SHA-256;
2. decodes `nBits` and checks that the header meets the network target;
3. asks the connected Bitcoin Node for the exact block hash;
4. reads Bitcoin Core's active-chain confirmation count;
5. records `candidate`, `confirming`, `confirmed`, `mature`, `orphaned`, or
   `invalid` as durable transitions; and
6. follows an accepted block through 100 confirmations.

The mining engine has write access to its own ledger. The verifier mounts that
ledger read-only and cannot alter pool sessions, jobs, shares, or candidates.

## Repository layout

```text
docker-compose.yml       Umbrel stack: proxy, dashboard/verifier, pool engine
umbrel-app.yml           Umbrel manifest and Bitcoin Node dependency
Dockerfile               Portable amd64/arm64 dashboard image
app/                     Standard-library Python service
web/                     Dependency-free responsive dashboard
tests/                   Proof, schema, persistence, and mocked integration tests
```

## Validate locally

The tests do not need Docker, Bitcoin Core, or internet access:

```powershell
cd pocisys-public-pool-port
python -m unittest discover -s tests -v
python -m compileall -q app tests
```

If Docker is available:

```powershell
docker build -t pocisys-public-pool-port:dev .
docker run --rm -p 8080:8080 -e DATA_DIR=/data pocisys-public-pool-port:dev
```

The standalone container will show the dashboard with offline pool/node states;
the full live integration exists only inside the Umbrel Compose stack.

## Publish the portable image

The manifest currently expects:

```text
ghcr.io/12gaugekenshin/pocisys-public-pool-port:v0.1.0
```

Publish both Umbrel CPU architectures before installation:

```bash
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t ghcr.io/12gaugekenshin/pocisys-public-pool-port:v0.1.0 \
  --push .
```

For a reproducible release, replace the dashboard image tag in
`docker-compose.yml` with the resulting `@sha256:` multi-architecture digest.

## Install on Umbrel

Place the `pocisys-public-pool-port` directory in a community app-store
repository beside the root `umbrel-app-store.yml`, push it to GitHub, then add
that repository URL as an Umbrel community app store.

The app declares `bitcoin` as a dependency. Umbrel will require the Bitcoin
Node app and inject its private RPC credentials automatically.

Do not run the stock Public Pool app at the same time: both default to a host
Stratum port and maintain separate worker ledgers. This app does not modify or
delete Bitcoin Node data.

## Miner settings

```text
Host:     umbrel.local
Port:     3333
Username: <bitcoin payout address>.<worker name>
Password: x
```

To show a public DNS name in the dashboard, set `PUBLIC_STRATUM_HOST` on the
dashboard service. DNS and router port forwarding are infrastructure settings,
not changed automatically by the app. Only forward TCP `3333`; never expose
Bitcoin RPC, Public Pool API `2019`, or the Umbrel dashboard.

## Preview status

The mocked end-to-end poll covers Public Pool API ingestion, TypeORM SQLite
schema discovery, Bitcoin RPC, proof verification, worker ingestion, and
100-confirmation maturity. A live Umbrel/Bitcoin Node installation has not yet
been available for hardware validation, so `0.1.0` is intentionally a preview.

## License and upstream

PoCiSys Public Pool Port is intended for release under GPL-3.0-or-later.
The bundled service uses Benjamin Wilson's GPL-3.0 Public Pool container:
<https://github.com/benjamin-wilson/public-pool>.
