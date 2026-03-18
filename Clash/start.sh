#!/usr/bin/env bash
set -euo pipefail
BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
nohup "$BASE_DIR/bin/mihomo" -d "$BASE_DIR/config" -f "$BASE_DIR/config/config.yaml" > "$BASE_DIR/clash.log" 2>&1 &
echo "Clash started with PID $!"
