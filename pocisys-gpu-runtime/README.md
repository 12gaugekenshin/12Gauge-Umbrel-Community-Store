# PoCiSys GPU Runtime

PoCiSys GPU Runtime runs a private Ollama service on a supported NVIDIA Tesla
P100 inside Umbrel. Its dashboard handles driver setup, GPU health, local model
storage, and safe model tests.

Version 0.1.10 also provides fail-safe P100 cooling through a Corsair Commander
DUO and OpenLinkHub. The app will not start Ollama unless the configured fan is
confirmed at 100% with nonzero RPM.

## Hardware requirements

- x86-64 Umbrel server
- NVIDIA Tesla P100 PCIe 16 GB (`10de:15f8`)
- Working forced-air cooling and the correct P100 power cable
- Corsair iCUE COMMANDER DUO connected over internal USB and SATA power
- P100 cooling fan connected to Commander DUO physical PWM Fan Port 1
- Secure Boot disabled
- Matching kernel headers available on the Umbrel host
- External Docker volume named `hermes-agent-ssd`
- Internet access during the first setup

This hardware-specific release intentionally refuses unsupported NVIDIA cards.

## Install

1. Add [12Gauge's PoCiSys Store](https://github.com/12gaugekenshin/12Gauge-Umbrel-Community-Store#add-the-store-to-umbrel) to Umbrel.
2. Install **PoCiSys GPU Runtime**.
3. Open its dashboard and leave the server running while setup completes.
4. Wait for **READY**. The first driver build and Ollama download can take
   several minutes.
5. Confirm the dashboard shows the P100, temperature, power, and 16 GB VRAM.
6. Confirm the dashboard identifies the Commander DUO, serial, physical Fan
   Port 1, channel, live RPM, and both connected temperature probes.
7. With inference stopped, run the 100%, 70%, 50%, and 40% calibration tests in
   that order. Every 25-second lease automatically returns to 100%.
8. Enable automatic fan control, install a small quantized model, and run
   **Safe Test**.

Do not move Hermes or another important service to the runtime until Safe Test
passes reliably.

## Local endpoint

Other Umbrel apps can use:

```text
http://pocisys-gpu-runtime_runtime_1:11434
```

The endpoint is private to Umbrel's app network. It is not published to your
LAN or the internet.

## Runtime memory policy

The runtime unloads the model and its active context immediately after every
response. Each request is capped at a 4K context, model concurrency is limited
to one, and the pending queue is bounded. The container has a 2 GB physical-RAM
limit with up to 2 GB of host-swap fallback when swap is available.
The dashboard can select a bounded 1K, 2K, or 4K context and immediate,
30-second, or 2-minute unload timing; a runtime restart applies changes.

## P100 fan safety policy

- Startup and controller reconnection: 100%
- GPU utilization at or above 10%: 100%
- Cooldown after utilization falls: 60 seconds at 100%
- Temperature curve: 40% at 45 °C or below, 50% at 50 °C, 60% at 55 °C,
  75% at 60 °C, 90% at 65 °C, and 100% at 70 °C or above
- Invalid GPU telemetry, missing controller data, or zero fan RPM: 100%
- Controller shutdown: best-effort 100%

Automatic control remains disabled until all four live calibration steps pass.
Safe Test also commands 100% and refuses to begin unless nonzero RPM is
confirmed. No initial calibration command can go below 40%.

The integration downloads the checksum-pinned OpenLinkHub 0.9.0 AMD64 release
from the [official OpenLinkHub project](https://github.com/jurkovic-nikola/OpenLinkHub).
OpenLinkHub is licensed separately under GPL-3.0-or-later by its author.

## Persistent models

Models, logs, and cached driver files live under the external
`hermes-agent-ssd` volume. Updating or reinstalling the app does not normally
remove that model library.

Source and support: [PoCiSys GPU Runtime on GitHub](https://github.com/12gaugekenshin/PoCiSys-GPU-Runtime)
