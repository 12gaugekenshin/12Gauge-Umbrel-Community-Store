const byId = (id) => document.getElementById(id);

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
    setText("gpu-temp", `${gpu.temperature_c} °C`);
    setText(
      "gpu-memory",
      `${gpu.memory_used_mib} / ${gpu.memory_total_mib} MiB`
    );
    setText("gpu-util", `${gpu.utilization_percent}%`);
    setText("gpu-power", `${gpu.power_w} / ${gpu.power_limit_w} W`);
    setText("gpu-driver", gpu.driver_version);
  } else {
    ["gpu-name", "gpu-temp", "gpu-memory", "gpu-util", "gpu-power"].forEach(
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
