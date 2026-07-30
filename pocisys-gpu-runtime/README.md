# PoCiSys GPU Runtime

PoCiSys GPU Runtime runs a private Ollama service on a supported NVIDIA Tesla
P100 inside Umbrel. Its dashboard handles driver setup, GPU health, local model
storage, and safe model tests.

## Hardware requirements

- x86-64 Umbrel server
- NVIDIA Tesla P100 PCIe 16 GB (`10de:15f8`)
- Working forced-air cooling and the correct P100 power cable
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
6. Install a small quantized model and run **Safe Test**.

Do not move Hermes or another important service to the runtime until Safe Test
passes reliably.

## Local endpoint

Other Umbrel apps can use:

```text
http://pocisys-gpu-runtime_runtime_1:11434
```

The endpoint is private to Umbrel's app network. It is not published to your
LAN or the internet.

## Persistent models

Models, logs, and cached driver files live under the external
`hermes-agent-ssd` volume. Updating or reinstalling the app does not normally
remove that model library.

Source and support: [PoCiSys GPU Runtime on GitHub](https://github.com/12gaugekenshin/PoCiSys-GPU-Runtime)
