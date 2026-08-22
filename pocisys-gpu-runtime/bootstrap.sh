#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATUS_DIR="/run/pocisys-gpu-runtime"
DATA_ROOT="${POCISYS_DATA_ROOT:-/agent-storage/pocisys-gpu-runtime}"
CACHE_DIR="${DATA_ROOT}/cache"
LOG_DIR="${DATA_ROOT}/logs"
MODEL_DIR="${OLLAMA_MODELS:-${DATA_ROOT}/models}"
DRIVER_VERSION="${POCISYS_NVIDIA_DRIVER_VERSION:-570.144}"
DRIVER_URL="${POCISYS_NVIDIA_DRIVER_URL:?POCISYS_NVIDIA_DRIVER_URL is required}"
DRIVER_SHA256="${POCISYS_NVIDIA_DRIVER_SHA256:?POCISYS_NVIDIA_DRIVER_SHA256 is required}"
EXPECTED_PCI_ID="${POCISYS_EXPECTED_PCI_ID:-10de:15f8}"
MAX_GPU_TEMP_C="${POCISYS_GPU_MAX_TEMP_C:-82}"
INSTALLER="${CACHE_DIR}/NVIDIA-Linux-x86_64-${DRIVER_VERSION}-no-compat32.run"
OLLAMA_VERSION="${POCISYS_OLLAMA_VERSION:-0.32.0}"
OLLAMA_URL="${POCISYS_OLLAMA_URL:?POCISYS_OLLAMA_URL is required}"
OLLAMA_SHA256="${POCISYS_OLLAMA_SHA256:?POCISYS_OLLAMA_SHA256 is required}"
OLLAMA_ARCHIVE="${CACHE_DIR}/ollama-linux-amd64-${OLLAMA_VERSION}.tar.zst"
OLLAMA_ROOT="${DATA_ROOT}/ollama/${OLLAMA_VERSION}"
OLLAMA_BIN="${OLLAMA_ROOT}/bin/ollama"
OPENLINKHUB_VERSION="${POCISYS_OPENLINKHUB_VERSION:-0.9.0}"
OPENLINKHUB_URL="${POCISYS_OPENLINKHUB_URL:?POCISYS_OPENLINKHUB_URL is required}"
OPENLINKHUB_SHA256="${POCISYS_OPENLINKHUB_SHA256:?POCISYS_OPENLINKHUB_SHA256 is required}"
OPENLINKHUB_ARCHIVE="${CACHE_DIR}/OpenLinkHub_${OPENLINKHUB_VERSION}_amd64.tar.gz"
OPENLINKHUB_ROOT="${DATA_ROOT}/openlinkhub/${OPENLINKHUB_VERSION}"
OPENLINKHUB_BIN="${OPENLINKHUB_ROOT}/OpenLinkHub"
OLLAMA_PID=""
STATUS_PID=""
OPENLINKHUB_PID=""
FAN_PID=""
GPU_BDF=""
NOUVEAU_WAS_BOUND="false"

mkdir -p "$STATUS_DIR" "$CACHE_DIR" "$LOG_DIR" "$MODEL_DIR" /data
chmod 0755 "$STATUS_DIR"

write_field() {
  local name="$1"
  shift
  printf '%s\n' "$*" > "${STATUS_DIR}/${name}.tmp"
  mv "${STATUS_DIR}/${name}.tmp" "${STATUS_DIR}/${name}"
}

set_status() {
  local state="$1"
  shift
  write_field state "$state"
  write_field message "$*"
  write_field updated_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '[%s] %s: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$state" "$*" |
    tee -a "${LOG_DIR}/runtime.log"
}

find_host_headers() {
  local kernel="$1"
  local output="/usr/src/linux-headers-${kernel}"
  local source

  [ -f "${output}/Makefile" ] || return 1
  source="$(
    awk '
      $1 == "include" &&
      $2 ~ /^\/usr\/src\/linux-headers-.*-common\/Makefile$/ {
        sub(/\/Makefile$/, "", $2)
        print $2
        exit
      }
    ' "${output}/Makefile"
  )"

  [ -n "$source" ] || return 1
  [ -f "${source}/Makefile" ] || return 1
  [ -f "${source}/include/linux/kernel.h" ] || return 1
  [ -f "${source}/scripts/Kbuild.include" ] || return 1
  [ -f "${output}/include/generated/autoconf.h" ] || return 1

  printf '%s\n%s\n' "$source" "$output"
}

find_gpu_bdf() {
  lspci -Dnnd "$EXPECTED_PCI_ID" 2>/dev/null |
    awk 'NR == 1 {print $1}'
}

current_gpu_driver() {
  local bdf="$1"
  if [ -L "/sys/bus/pci/devices/${bdf}/driver" ]; then
    basename "$(readlink "/sys/bus/pci/devices/${bdf}/driver")"
  else
    printf 'unbound\n'
  fi
}

load_host_module() {
  local module="$1"
  modprobe -d /host "$module"
}

rebind_nouveau() {
  [ -n "$GPU_BDF" ] || return 0
  if [ "$(current_gpu_driver "$GPU_BDF")" = "nouveau" ]; then
    return 0
  fi

  rmmod nvidia_uvm 2>/dev/null || true
  rmmod nvidia_modeset 2>/dev/null || true
  rmmod nvidia_drm 2>/dev/null || true
  rmmod nvidia 2>/dev/null || true

  if ! grep -q '^nouveau ' /proc/modules; then
    load_host_module nouveau 2>/dev/null || true
  fi
  if [ -w /sys/bus/pci/drivers/nouveau/bind ]; then
    printf '%s' "$GPU_BDF" > /sys/bus/pci/drivers/nouveau/bind 2>/dev/null || true
  fi
}

force_fan_100() {
  python3 "${APP_DIR}/fan_controller.py" --force-100 \
    >> "${LOG_DIR}/fan-controller.log" 2>&1 || true
}

cleanup() {
  set +e
  if [ -n "$FAN_PID" ]; then
    kill "$FAN_PID" 2>/dev/null
    wait "$FAN_PID" 2>/dev/null
  fi
  if [ -n "$OPENLINKHUB_PID" ] && kill -0 "$OPENLINKHUB_PID" 2>/dev/null; then
    force_fan_100
  fi
  if [ -n "$OLLAMA_PID" ]; then
    kill "$OLLAMA_PID" 2>/dev/null
    wait "$OLLAMA_PID" 2>/dev/null
  fi
  if [ "$NOUVEAU_WAS_BOUND" = "true" ]; then
    rebind_nouveau
  fi
  if [ -n "$STATUS_PID" ]; then
    kill "$STATUS_PID" 2>/dev/null
    wait "$STATUS_PID" 2>/dev/null
  fi
  if [ -n "$OPENLINKHUB_PID" ]; then
    kill "$OPENLINKHUB_PID" 2>/dev/null
    wait "$OPENLINKHUB_PID" 2>/dev/null
  fi
}
trap cleanup EXIT INT TERM

mapfile -t RUNTIME_SETTINGS < <(
  python3 - /data/runtime-settings.json "${OLLAMA_CONTEXT_LENGTH:-4096}" "${OLLAMA_KEEP_ALIVE:-0}" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
default_context = int(sys.argv[2])
default_keep_alive = sys.argv[3]
allowed_contexts = {1024, 2048, 4096}
allowed_keep_alive = {"0", "30s", "2m"}

try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    payload = {}

try:
    context = int(payload.get("context_length", default_context))
except (TypeError, ValueError):
    context = default_context
keep_alive = str(payload.get("keep_alive", default_keep_alive))

print(context if context in allowed_contexts else 4096)
print(keep_alive if keep_alive in allowed_keep_alive else "0")
PY
)
export OLLAMA_CONTEXT_LENGTH="${RUNTIME_SETTINGS[0]:-4096}"
export OLLAMA_KEEP_ALIVE="${RUNTIME_SETTINGS[1]:-0}"

write_field app_version "0.1.11"
write_field ollama_context_length "$OLLAMA_CONTEXT_LENGTH"
write_field ollama_keep_alive "$OLLAMA_KEEP_ALIVE"
write_field driver_version "$DRIVER_VERSION"
write_field expected_pci_id "$EXPECTED_PCI_ID"
write_field ollama_endpoint "http://pocisys-gpu-runtime_runtime_1:11434"
write_field data_root "$DATA_ROOT"
set_status starting "Starting the isolated PoCiSys GPU runtime."

POCISYS_STATUS_DIR="$STATUS_DIR" \
POCISYS_WEB_DIR="${APP_DIR}/web" \
POCISYS_STATUS_PORT="${POCISYS_STATUS_PORT:-8780}" \
python3 -u "${APP_DIR}/status_server.py" &
STATUS_PID="$!"

set_status preparing "Preparing the checksum-pinned OpenLinkHub ${OPENLINKHUB_VERSION} controller runtime."
if [ ! -x "$OPENLINKHUB_BIN" ]; then
  if [ ! -f "$OPENLINKHUB_ARCHIVE" ] ||
     ! printf '%s  %s\n' "$OPENLINKHUB_SHA256" "$OPENLINKHUB_ARCHIVE" |
       sha256sum --check --status; then
    rm -f "${OPENLINKHUB_ARCHIVE}.part" "$OPENLINKHUB_ARCHIVE"
    curl --fail --location --retry 3 --retry-delay 3 \
      --output "${OPENLINKHUB_ARCHIVE}.part" "$OPENLINKHUB_URL"
    printf '%s  %s\n' "$OPENLINKHUB_SHA256" "${OPENLINKHUB_ARCHIVE}.part" |
      sha256sum --check --status
    mv "${OPENLINKHUB_ARCHIVE}.part" "$OPENLINKHUB_ARCHIVE"
  fi
  rm -rf "${OPENLINKHUB_ROOT}.part"
  mkdir -p "${OPENLINKHUB_ROOT}.part"
  tar -xzf "$OPENLINKHUB_ARCHIVE" --strip-components=1 \
    -C "${OPENLINKHUB_ROOT}.part"
  rm -rf "$OPENLINKHUB_ROOT"
  mv "${OPENLINKHUB_ROOT}.part" "$OPENLINKHUB_ROOT"
  chmod 0700 "$OPENLINKHUB_BIN"
fi

cat > "${OPENLINKHUB_ROOT}/config.json" <<'JSON'
{
  "debug": false,
  "listenPort": 27003,
  "listenAddress": "127.0.0.1",
  "cpuSensorChip": "",
  "manual": true,
  "frontend": false,
  "metrics": false,
  "resumeDelay": 15000,
  "memory": false,
  "memorySmBus": "i2c-0",
  "memoryType": 5,
  "exclude": [],
  "decodeMemorySku": true,
  "memorySku": "",
  "logFile": "-",
  "logLevel": "info",
  "enhancementKits": [],
  "temperatureOffset": 0,
  "amdGpuIndex": 0,
  "amdsmiPath": "",
  "checkDevicePermission": false,
  "cpuTempFile": "",
  "graphProfiles": false,
  "ramTempViaHwmon": false,
  "nvidiaGpuIndex": [0],
  "defaultNvidiaGPU": 0,
  "openRGBPort": 6743,
  "enableOpenRGBTargetServer": false,
  "enableGamepad": false,
  "enableMotherboard": false,
  "motherboardBiosOnExit": false,
  "memoryRegisterOverride": []
}
JSON

(
  cd "$OPENLINKHUB_ROOT"
  exec > >(tee -a "${LOG_DIR}/openlinkhub.log") 2>&1
  exec "$OPENLINKHUB_BIN"
) &
OPENLINKHUB_PID="$!"

for _ in $(seq 1 30); do
  if python3 - <<'PY'
import urllib.request
urllib.request.urlopen("http://127.0.0.1:27003/api/", timeout=1).read(128)
PY
  then
    break
  fi
  if ! kill -0 "$OPENLINKHUB_PID" 2>/dev/null; then
    set_status error "OpenLinkHub exited during startup; the fan controller was not started."
    wait "$STATUS_PID"
  fi
  sleep 1
done

if ! python3 - <<'PY'
import urllib.request
urllib.request.urlopen("http://127.0.0.1:27003/api/", timeout=2).read(128)
PY
then
  set_status error "OpenLinkHub did not become ready within 30 seconds."
  wait "$STATUS_PID"
fi

(
  exec > >(tee -a "${LOG_DIR}/fan-controller.log") 2>&1
  exec python3 -u "${APP_DIR}/fan_controller.py"
) &
FAN_PID="$!"

if [ "$(uname -m)" != "x86_64" ]; then
  set_status blocked "This release supports x86-64 hosts only."
  wait "$STATUS_PID"
fi

KERNEL="$(uname -r)"
write_field kernel "$KERNEL"
write_field max_gpu_temp_c "$MAX_GPU_TEMP_C"

SECURE_BOOT="unknown"
SECURE_BOOT_FILE="$(find /sys/firmware/efi/efivars -maxdepth 1 -name 'SecureBoot-*' -print -quit 2>/dev/null || true)"
if [ -n "$SECURE_BOOT_FILE" ]; then
  SECURE_BOOT_VALUE="$(od -An -t u1 "$SECURE_BOOT_FILE" 2>/dev/null | awk 'NR == 1 {print $5}')"
  if [ "$SECURE_BOOT_VALUE" = "1" ]; then
    SECURE_BOOT="enabled"
  elif [ "$SECURE_BOOT_VALUE" = "0" ]; then
    SECURE_BOOT="disabled"
  fi
fi
write_field secure_boot "$SECURE_BOOT"
if [ "$SECURE_BOOT" = "enabled" ]; then
  set_status blocked "Secure Boot is enabled. Refusing to build an unsigned P100 kernel module."
  wait "$STATUS_PID"
fi

GPU_BDF="$(find_gpu_bdf || true)"
if [ -z "$GPU_BDF" ]; then
  set_status blocked "Tesla P100 ${EXPECTED_PCI_ID} was not detected. No driver changes were made."
  wait "$STATUS_PID"
fi
write_field gpu_bdf "$GPU_BDF"

PCI_CLASS="$(cat "/sys/bus/pci/devices/${GPU_BDF}/class" 2>/dev/null || true)"
write_field pci_class "$PCI_CLASS"
if [ "$PCI_CLASS" != "0x030200" ]; then
  set_status blocked "The NVIDIA device is not the expected compute-only 3D controller. Refusing to detach it."
  wait "$STATUS_PID"
fi

mapfile -t HEADER_PATHS < <(find_host_headers "$KERNEL" || true)
KERNEL_SOURCE="${HEADER_PATHS[0]:-}"
KERNEL_OUTPUT="${HEADER_PATHS[1]:-}"
if [ -z "$KERNEL_SOURCE" ] || [ -z "$KERNEL_OUTPUT" ]; then
  set_status blocked "Matching host headers for kernel ${KERNEL} were not found. The GPU was left untouched."
  wait "$STATUS_PID"
fi
write_field kernel_source "$KERNEL_SOURCE"
write_field kernel_output "$KERNEL_OUTPUT"
write_field kernel_headers "$KERNEL_OUTPUT"

set_status preparing "Preparing NVIDIA ${DRIVER_VERSION} for kernel ${KERNEL}."
if [ ! -f "$INSTALLER" ] ||
   ! printf '%s  %s\n' "$DRIVER_SHA256" "$INSTALLER" | sha256sum --check --status; then
  rm -f "${INSTALLER}.part" "$INSTALLER"
  set_status downloading "Downloading the verified NVIDIA ${DRIVER_VERSION} compute driver."
  curl --fail --location --retry 3 --retry-delay 3 \
    --output "${INSTALLER}.part" "$DRIVER_URL"
  printf '%s  %s\n' "$DRIVER_SHA256" "${INSTALLER}.part" |
    sha256sum --check --status
  mv "${INSTALLER}.part" "$INSTALLER"
  chmod 0700 "$INSTALLER"
fi

set_status preparing "Preparing the checksum-pinned Ollama ${OLLAMA_VERSION} runtime."
if [ ! -x "$OLLAMA_BIN" ]; then
  if [ ! -f "$OLLAMA_ARCHIVE" ] ||
     ! printf '%s  %s\n' "$OLLAMA_SHA256" "$OLLAMA_ARCHIVE" |
       sha256sum --check --status; then
    rm -f "${OLLAMA_ARCHIVE}.part" "$OLLAMA_ARCHIVE"
    set_status downloading "Downloading the verified Ollama ${OLLAMA_VERSION} Linux runtime."
    curl --fail --location --retry 3 --retry-delay 3 \
      --output "${OLLAMA_ARCHIVE}.part" "$OLLAMA_URL"
    printf '%s  %s\n' "$OLLAMA_SHA256" "${OLLAMA_ARCHIVE}.part" |
      sha256sum --check --status
    mv "${OLLAMA_ARCHIVE}.part" "$OLLAMA_ARCHIVE"
  fi
  rm -rf "${OLLAMA_ROOT}.part"
  mkdir -p "${OLLAMA_ROOT}.part"
  tar --zstd -xf "$OLLAMA_ARCHIVE" -C "${OLLAMA_ROOT}.part"
  rm -rf "$OLLAMA_ROOT"
  mv "${OLLAMA_ROOT}.part" "$OLLAMA_ROOT"
fi
export PATH="${OLLAMA_ROOT}/bin:${PATH}"
export LD_LIBRARY_PATH="${OLLAMA_ROOT}/lib/ollama:${OLLAMA_ROOT}/lib/ollama/cuda_v12:${LD_LIBRARY_PATH:-}"
write_field ollama_version "$("$OLLAMA_BIN" --version 2>&1 | head -1 || true)"

ORIGINAL_DRIVER="$(current_gpu_driver "$GPU_BDF")"
write_field original_driver "$ORIGINAL_DRIVER"
if [ "$ORIGINAL_DRIVER" = "nouveau" ]; then
  NOUVEAU_WAS_BOUND="true"
  set_status switching "Detaching the compute-only P100 from nouveau."
  printf '%s' "$GPU_BDF" > /sys/bus/pci/drivers/nouveau/unbind

  if find /sys/bus/pci/drivers/nouveau -maxdepth 1 -type l -name '*:*' |
     grep -q .; then
    set_status blocked "Another device still uses nouveau. Refusing to unload the shared driver."
    rebind_nouveau
    NOUVEAU_WAS_BOUND="false"
    wait "$STATUS_PID"
  fi
  rmmod nouveau
elif [ "$ORIGINAL_DRIVER" != "nvidia" ] && [ "$ORIGINAL_DRIVER" != "unbound" ]; then
  set_status blocked "The P100 is bound to unexpected driver ${ORIGINAL_DRIVER}; refusing to change it."
  wait "$STATUS_PID"
fi

mkdir -p "/lib/modules/${KERNEL}"
ln -sfn "$KERNEL_OUTPUT" "/lib/modules/${KERNEL}/build"

if ! command -v nvidia-smi >/dev/null 2>&1 ||
   [ ! -f "/lib/modules/${KERNEL}/kernel/drivers/video/nvidia.ko" ]; then
  INSTALL_LOG="${LOG_DIR}/nvidia-installer-${DRIVER_VERSION}-${KERNEL}.log"
  set_status building "Building the proprietary P100 driver. This can take several minutes."
  if ! sh "$INSTALLER" \
      --silent \
      --accept-license \
      --no-questions \
      --no-x-check \
      --no-nouveau-check \
      --no-cc-version-check \
      --no-opengl-files \
      --no-install-compat32-libs \
      --no-dkms \
      --kernel-source-path="$KERNEL_SOURCE" \
      --kernel-output-path="$KERNEL_OUTPUT" \
      --log-file-name="$INSTALL_LOG"; then
    set_status error "NVIDIA driver compilation failed. The P100 is being returned to nouveau; see ${INSTALL_LOG}."
    rebind_nouveau
    NOUVEAU_WAS_BOUND="false"
    wait "$STATUS_PID"
  fi
fi

depmod -a "$KERNEL"
set_status loading "Loading the NVIDIA compute modules."
if ! modprobe nvidia || ! modprobe nvidia_uvm; then
  set_status error "The compiled NVIDIA modules did not load. The P100 is being returned to nouveau."
  rebind_nouveau
  NOUVEAU_WAS_BOUND="false"
  wait "$STATUS_PID"
fi

nvidia-modprobe -u -c=0 2>/dev/null || true
if ! nvidia-smi > "${STATUS_DIR}/nvidia-smi.txt" 2>&1; then
  set_status error "nvidia-smi could not communicate with the P100. The card is being returned to nouveau."
  rebind_nouveau
  NOUVEAU_WAS_BOUND="false"
  wait "$STATUS_PID"
fi

FAN_READY="false"
for _ in $(seq 1 30); do
  if python3 - "${STATUS_DIR}/fan-status.json" <<'PY'
import json
import pathlib
import sys

try:
    status = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    raise SystemExit(1)
healthy = status.get("healthy") is True
at_full = int(status.get("target_percent") or 0) == 100
spinning = int(status.get("reported_rpm") or 0) > 0
raise SystemExit(0 if healthy and at_full and spinning else 1)
PY
  then
    FAN_READY="true"
    break
  fi
  if ! kill -0 "$OPENLINKHUB_PID" 2>/dev/null ||
     ! kill -0 "$FAN_PID" 2>/dev/null; then
    break
  fi
  sleep 1
done
if [ "$FAN_READY" != "true" ]; then
  set_status blocked "Commander DUO Fan Port 1 did not confirm 100% with nonzero RPM. Ollama was not started."
  wait "$STATUS_PID"
fi

set_status gpu_ready "Tesla P100 validated. Starting the private Ollama service."
"$OLLAMA_BIN" serve >> "${LOG_DIR}/ollama.log" 2>&1 &
OLLAMA_PID="$!"

for _ in $(seq 1 60); do
  if python3 - <<'PY'
import urllib.request
urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=1).read(64)
PY
  then
    set_status ready "P100 and Ollama are ready for local Umbrel apps."
    break
  fi
  if ! kill -0 "$OLLAMA_PID" 2>/dev/null; then
    set_status error "Ollama exited during startup. See the Ollama log on persistent storage."
    wait "$STATUS_PID"
  fi
  sleep 1
done

if [ "$(cat "${STATUS_DIR}/state")" != "ready" ]; then
  set_status error "Ollama did not become ready within 60 seconds."
  wait "$STATUS_PID"
fi

THERMAL_STOP="false"
CONTROL_FAILURE="false"
while kill -0 "$OLLAMA_PID" 2>/dev/null; do
  if ! kill -0 "$OPENLINKHUB_PID" 2>/dev/null ||
     ! kill -0 "$FAN_PID" 2>/dev/null; then
    THERMAL_STOP="true"
    CONTROL_FAILURE="true"
    set_status thermal_stop "The fail-safe fan-control service stopped; stopping Ollama and restarting the runtime."
    force_fan_100
    kill "$OLLAMA_PID" 2>/dev/null || true
    break
  fi
  GPU_TEMP="$(
    nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits 2>/dev/null |
      awk 'NR == 1 {print int($1)}'
  )"
  if [ -n "$GPU_TEMP" ] && [ "$GPU_TEMP" -ge "$MAX_GPU_TEMP_C" ]; then
    THERMAL_STOP="true"
    set_status thermal_stop "GPU reached ${GPU_TEMP} °C; stopping Ollama at the ${MAX_GPU_TEMP_C} °C safety limit."
    kill "$OLLAMA_PID" 2>/dev/null || true
    break
  fi
  sleep 5
done

set +e
wait "$OLLAMA_PID"
EXIT_CODE="$?"
set -e
if [ "$THERMAL_STOP" != "true" ]; then
  set_status error "Ollama exited with code ${EXIT_CODE}."
fi
if [ "$CONTROL_FAILURE" = "true" ]; then
  exit 1
fi
wait "$STATUS_PID"
