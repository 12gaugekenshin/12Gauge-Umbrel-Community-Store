# 12Gauge's PoCiSys Store

One Umbrel community store for the PoCiSys local-AI and mining toolkit.

## Add the store to Umbrel

1. Open **App Store** in Umbrel.
2. Open **Community App Stores**.
3. Add this URL:

   ```text
   https://github.com/12gaugekenshin/12Gauge-Umbrel-Community-Store
   ```

4. Open **12Gauge's PoCiSys Store** and install the apps you want.

## Apps

| App | What it does | Main requirement |
| --- | --- | --- |
| [PoCiSys Hash Monitor](pocisys-hash-monitor/README.md) | Monitors miners, pools, temperatures, shares, and alerts | Network access to your miners |
| [PoCiSys Public Pool Port](pocisys-public-pool-port/README.md) | Runs a verified solo-mining pool through your own node | A synchronized Umbrel Bitcoin Node |
| [PoCiSys GPU Runtime](pocisys-gpu-runtime/README.md) | Runs private Ollama inference on a Tesla P100 | Supported P100 server hardware |
| [PoCiSys Gateway](pocisys-gateway/README.md) | Measures local-AI requests without storing their text | PoCiSys GPU Runtime |

Each app has its own short setup guide. Apps keep the same IDs across updates,
so normal Umbrel updates preserve their configured data.

## Support and source

- Project: [pocisys.io](https://pocisys.io/)
- Maintainer: [12gaugekenshin](https://github.com/12gaugekenshin)
- Problems or feature requests: use the **Support** link on the app's Umbrel page.
