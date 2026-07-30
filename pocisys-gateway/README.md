# PoCiSys Gateway

PoCiSys Gateway sits between an app and PoCiSys GPU Runtime. It forwards the
AI response unchanged while recording useful operational signals such as model
name, timing, token counts, failures, and body hashes. It does not retain prompt
or response text.

## Before installing

- Install **PoCiSys GPU Runtime** first.
- Wait until GPU Runtime reports **READY**.
- Install and test at least one local Ollama model.

## Install

1. Add [12Gauge's PoCiSys Store](https://github.com/12gaugekenshin/12Gauge-Umbrel-Community-Store#add-the-store-to-umbrel) to Umbrel.
2. Install **PoCiSys Gateway**.
3. Open the app and confirm the backend shows as connected.
4. Send a short message in **Chat** to confirm the complete path works.

No API key or public port is required. Gateway reaches GPU Runtime through
Umbrel's private app network.

## What the dashboard shows

- Recent measured AI requests and failures
- Latency, token usage, and reported model identity
- Learned performance baselines and clear attention reasons
- Private test chat with thinking and output-length controls

These measurements help detect changes and anomalies. They do not prove that
an answer was correct or that a particular physical GPU performed inference.

## Privacy and storage

Prompt and response text are not saved. Live receipts and baselines are bounded
and normally disappear when Gateway restarts. The optional persistent evidence
window remains disabled unless you deliberately enable it.

Source and support: [PoCiSys Gateway on GitHub](https://github.com/12gaugekenshin/PoCiSys-Gateway)
