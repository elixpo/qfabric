#!/usr/bin/env bash
set -euo pipefail

# Measure Bridge RPC latency at several router/MCU serial rates on the UNO Q.
# For each rate: flash the matching MCU image, restart the router at that rate,
# wait for the link, profile the Stage 12 workloads, then restore 115200.

campaign_id="${1:-baud-sweep-01}"
root="data/processed/baud/${campaign_id}"
bauds="${QF_BAUDS:-115200 230400 460800 921600 2000000}"
images="${QF_BAUD_IMAGES:-build/baud}"
iterations="${QF_ITERATIONS:-1000}"
warmup="${QF_WARMUP:-50}"
dropin_dir=/etc/systemd/system/arduino-router.service.d
dropin="$dropin_dir/zz-qfabric-baud.conf"

if [[ "$(id -u)" != "0" || -z "${SUDO_USER:-}" ]]; then
  echo "run with sudo -E env PATH=\"\$PATH\" so the router unit can be changed" >&2
  exit 1
fi
command -v qf >/dev/null 2>&1 || {
  echo "qf is unavailable; activate the project virtual environment" >&2
  exit 1
}
for baud in $bauds; do
  [[ -d "$images/$baud" ]] || { echo "missing MCU image: $images/$baud" >&2; exit 1; }
done

as_user() { sudo -u "$SUDO_USER" env PATH="$PATH" HOME="$(getent passwd "$SUDO_USER" | cut -d: -f6)" "$@"; }

flash() {
  as_user arduino-cli upload --fqbn arduino:zephyr:unoq --input-dir "$images/$1" >/dev/null
}

set_router_baud() {
  mkdir -p "$dropin_dir"
  cat >"$dropin" <<EOF
[Service]
ExecStart=
ExecStart=/usr/bin/arduino-router --unix-port /var/run/arduino-router.sock --serial-port /dev/ttyHS1 --serial-baudrate $1 --after-ready '/usr/bin/gpioset -c /dev/gpiochip1 -t0 70=1'
EOF
  systemctl daemon-reload
  systemctl restart arduino-router
}

wait_for_link() {
  as_user python - <<'EOF'
import sys, time
from qfabric.runtime import _connect_bridge
from qfabric.stage12 import encode_block
deadline = time.monotonic() + 40
while time.monotonic() < deadline:
    try:
        bridge = _connect_bridge("unix:///var/run/arduino-router.sock")
        try:
            reply = bridge.call("qf_stage12_invoke", f"add 1 {encode_block(bytes(64))}", timeout=1.0)
        finally:
            bridge.disconnect()
        if str(reply).split()[0] == "5":
            sys.exit(0)
    except Exception:
        pass
    time.sleep(1)
sys.exit(1)
EOF
}

restore() {
  echo "== restoring 115200"
  rm -f "$dropin"
  systemctl daemon-reload
  flash 115200 || echo "warning: could not re-flash the 115200 image" >&2
  systemctl restart arduino-router
  wait_for_link && echo "restored: Bridge answers at 115200" || echo "warning: Bridge not answering after restore" >&2
}
trap restore EXIT

as_user mkdir -p "$root"
summary="$root/summary.txt"
: >"$summary"
for baud in $bauds; do
  echo "== $baud baud"
  flash "$baud"
  set_router_baud "$baud"
  if ! wait_for_link; then
    echo "$baud unreachable" | tee -a "$summary"
    continue
  fi
  as_user qf workload profile \
    --domain rt --load idle --policy other \
    --iterations "$iterations" --warmup "$warmup" --timeout 1.0 \
    --output "$root/baud-$baud.json" | sed "s/^/$baud /" | tee -a "$summary"
done

{
  echo "campaign_id=$campaign_id"
  echo "bauds=$bauds"
  echo "iterations=$iterations"
  echo "warmup=$warmup"
  echo "router=$(/usr/bin/arduino-router --version 2>/dev/null | head -1 || echo unknown)"
  echo "kernel=$(uname -r)"
  echo "git_commit=$(git rev-parse HEAD)"
} | as_user tee "$root/campaign.txt" >/dev/null
echo "results: $root"
