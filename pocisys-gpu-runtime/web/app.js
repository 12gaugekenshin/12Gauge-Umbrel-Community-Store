const byId = (id) => document.getElementById(id);
let settingsLoaded = false;
let settingsDirty = false;
let fanBusy = false;

function formatBytes(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return "None installed";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let current = number;
  let unit = 0;
  while (current >= 1024 && unit < units.length - 1) {
    current /= 1024;
    unit += 1;
  }
  return `${current.toFixed(unit >= 3 ? 1 : 0)} ${units[unit]}`;
}

function setText(id, value, fallback = "—") {
  byId(id).textContent = value || fallback;
}

function humanMode(value) {
  return String(value || "unknown")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function renderFan(fan) {
  const state = byId("fan-state");
  const healthy = fan?.healthy === true;
  state.textContent = healthy ? "HEALTHY" : fan?.online ? "CHECK FAN" : "UNAVAILABLE";
  state.className = `pill ${healthy ? "ready" : "error"}`;

  const usb = fan?.usb || {};
  setText(
    "fan-usb",
    usb.detected
      ? `${usb.vendor_id}:${usb.product_id}${usb.expected_product_id ? "" : " · detected PID"}`
      : "Not detected"
  );
  const hub = fan?.openlinkhub || {};
  setText("fan-product", hub.product);
  setText("fan-serial", hub.serial);
  setText(
    "fan-port",
    hub.physical_port
      ? `Physical port ${hub.physical_port} · channel ${hub.channel_id}`
      : ""
  );
  const probes = hub.temperature_probes || [];
  [0, 1].forEach((index) => {
    const probe = probes[index];
    setText(
      `fan-probe-${index + 1}`,
      probe
        ? `${probe.temperature_c} °C · API channel ${probe.channel_id}`
        : "Not reported"
    );
  });
  setText("fan-rpm", fan?.reported_rpm ? `${fan.reported_rpm} RPM` : "No RPM");
  setText("fan-target", fan?.target_percent ? `${fan.target_percent}%` : "100% fail-safe");
  setText("fan-mode", humanMode(fan?.mode));
  setText("fan-error", hub.error, healthy ? "None" : "Waiting for controller detail");
  setText("fan-automatic", fan?.automatic_enabled ? "Enabled" : "Disabled");

  const calibrated = fan?.calibrated_duties || {};
  const manualRunning = Boolean(fan?.manual_test);
  const order = [100, 70, 50, 40];
  document.querySelectorAll("[data-fan-duty]").forEach((button) => {
    const duty = Number(button.dataset.fanDuty);
    const priorComplete = order
      .slice(0, order.indexOf(duty))
      .every((prior) => calibrated[String(prior)] === true);
    button.disabled = fanBusy || manualRunning || !healthy || !priorComplete;
    button.textContent = calibrated[String(duty)]
      ? `✓ ${duty}% passed`
      : `Test ${duty}%`;
  });

  const automatic = byId("toggle-automatic");
  automatic.disabled = fanBusy || manualRunning || (!fan?.calibration_complete && !fan?.automatic_enabled);
  automatic.textContent = fan?.automatic_enabled
    ? "Disable Automatic Control"
    : "Enable Automatic Control";
  byId("force-fan").disabled = fanBusy;

  if (manualRunning) {
    const remaining = Number(fan.manual_remaining_seconds || 0);
    const result = byId("fan-result");
    result.hidden = false;
    result.textContent = `${fan.manual_test.duty}% test running · returns to 100% in about ${remaining}s · ${fan.reported_rpm || 0} RPM`;
  }
}

function render(data) {
  const state = data.state || "starting";
  const stateNode = byId("state");
  stateNode.textContent = state.replaceAll("_", " ").toUpperCase();
  stateNode.className = `pill ${state}`;
  setText("summary", data.message, "Starting.");
  setText("version", data.app_version ? `v${data.app_version}` : "");
  setText("kernel", data.kernel);
  setText("secure-boot", data.secure_boot);
  setText("gpu-bdf", data.gpu_bdf);
  setText("storage", data.data_root);
  setText("endpoint", data.ollama_endpoint);
  setText("ollama", data.ollama?.online ? "Online" : "Offline");
  renderFan(data.fan || {});

  if (!settingsLoaded || !settingsDirty) {
    const settings = data.runtime_settings || {};
    byId("context-length").value = String(settings.context_length || 4096);
    byId("keep-alive").value = settings.keep_alive || "0";
    settingsLoaded = true;
  }

  const models = data.ollama?.models || [];
  if (models.length) {
    const size = models.reduce((sum, model) => sum + Number(model.size || 0), 0);
    setText("models", `${models.length} installed · ${formatBytes(size)}`);
  } else {
    setText("models", "None installed");
  }

  const gpu = data.gpu;
  if (gpu) {
    setText("gpu-name", gpu.name);
    setText("gpu-uuid", gpu.uuid);
    setText("gpu-pci", gpu.pci_bus_id);
    setText("gpu-temp", `${gpu.temperature_c} °C`);
    setText(
      "gpu-memory",
      `${gpu.memory_used_mib} / ${gpu.memory_total_mib} MiB`
    );
    setText("gpu-util", `${gpu.utilization_percent}%`);
    setText("gpu-power", `${gpu.power_w} / ${gpu.power_limit_w} W`);
    setText("gpu-driver", gpu.driver_version);
  } else {
    ["gpu-name", "gpu-uuid", "gpu-pci", "gpu-temp", "gpu-memory", "gpu-util", "gpu-power"].forEach(
      (id) => setText(id, "")
    );
    setText("gpu-driver", data.driver_version);
  }

  const updated = data.updated_at
    ? new Date(data.updated_at).toLocaleString()
    : "Waiting for first update";
  setText("updated", `Updated ${updated}`);
}

async function refresh() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
  } catch (error) {
    const node = byId("state");
    node.textContent = "OFFLINE";
    node.className = "pill error";
    setText("summary", `Status connection failed: ${error.message}`);
  }
}

refresh();
setInterval(refresh, 5000);

["context-length", "keep-alive"].forEach((id) => {
  byId(id).addEventListener("change", () => {
    settingsDirty = true;
  });
});

byId("save-settings").addEventListener("click", async () => {
  const button = byId("save-settings");
  const result = byId("settings-result");
  button.disabled = true;
  button.textContent = "Saving...";
  result.hidden = false;
  result.textContent = "Saving bounded runtime settings...";

  try {
    const response = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        context_length: Number(byId("context-length").value),
        keep_alive: byId("keep-alive").value,
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    settingsDirty = false;
    result.textContent = "Saved. Restart GPU Runtime from Umbrel to apply.";
  } catch (error) {
    result.textContent = `Save failed: ${error.message}`;
  } finally {
    button.disabled = false;
    button.textContent = "Save Memory Settings";
  }
});

byId("safe-test-button").addEventListener("click", async () => {
  const button = byId("safe-test-button");
  const result = byId("safe-test-result");
  button.disabled = true;
  button.textContent = "Testing...";
  result.hidden = false;
  result.textContent = "Loading the bounded model test...";

  try {
    const response = await fetch("/api/safe-test", { method: "POST" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    result.textContent = [
      `Response: ${data.response}`,
      `Model: ${data.model}`,
      `Thinking returned: ${data.thinking_returned ? "YES" : "No"}`,
      `Output tokens: ${data.eval_tokens} / ${data.limits.max_output_tokens}`,
      `Speed: ${data.tokens_per_second} tokens/sec`,
      `Total time: ${data.total_seconds} seconds`,
    ].join("\n");
  } catch (error) {
    result.textContent = `Test stopped: ${error.message}`;
  } finally {
    button.disabled = false;
    button.textContent = "Run Safe Test";
    refresh();
  }
});

async function fanPost(path, payload = undefined) {
  fanBusy = true;
  const result = byId("fan-result");
  result.hidden = false;
  result.textContent = "Sending bounded fan command…";
  try {
    const options = { method: "POST", headers: {} };
    if (payload !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(payload);
    }
    const response = await fetch(path, options);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    result.textContent = "Command accepted. Live telemetry will confirm the result.";
  } catch (error) {
    result.textContent = `Fan command refused: ${error.message}`;
  } finally {
    fanBusy = false;
    await refresh();
  }
}

document.querySelectorAll("[data-fan-duty]").forEach((button) => {
  button.addEventListener("click", () =>
    fanPost("/api/fan/manual", {
      duty: Number(button.dataset.fanDuty),
      duration_seconds: 25,
    })
  );
});

byId("force-fan").addEventListener("click", () =>
  fanPost("/api/fan/force-100")
);

byId("toggle-automatic").addEventListener("click", async () => {
  const status = await fetch("/api/status", { cache: "no-store" }).then((response) => response.json());
  return fanPost("/api/fan/automatic", {
    enabled: !Boolean(status.fan?.automatic_enabled),
  });
});
