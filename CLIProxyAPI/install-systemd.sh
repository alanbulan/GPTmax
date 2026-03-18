#!/usr/bin/env bash
set -euo pipefail

ACTION="install"
SERVICE_NAME="cliproxyapi-web.service"

for arg in "$@"; do
  case "$arg" in
    --uninstall)
      ACTION="uninstall"
      ;;
    *)
      echo "Unknown argument: $arg"
      echo "Usage: ./install-systemd.sh [--uninstall]"
      exit 1
      ;;
  esac
done

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"
RUN_USER="${SUDO_USER:-${USER}}"
RUN_GROUP="$(id -gn "${RUN_USER}")"

if ! command -v systemctl >/dev/null 2>&1; then
  echo "错误: 未找到 systemctl"
  exit 1
fi

if [[ "${ACTION}" == "uninstall" ]]; then
  sudo systemctl disable --now "${SERVICE_NAME}" >/dev/null 2>&1 || true
  sudo rm -f "${SERVICE_PATH}"
  sudo systemctl daemon-reload
  echo "已卸载 ${SERVICE_NAME}"
  exit 0
fi

cat > /tmp/${SERVICE_NAME} <<EOF
[Unit]
Description=CLIProxyAPI Web Docker Service
After=network-online.target docker.service
Wants=network-online.target docker.service

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_GROUP}
WorkingDirectory=${ROOT_DIR}
ExecStart=/usr/bin/env bash ${ROOT_DIR}/start-web-docker.sh --no-browser
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo mv /tmp/${SERVICE_NAME} "${SERVICE_PATH}"
sudo chmod 644 "${SERVICE_PATH}"
sudo systemctl daemon-reload
sudo systemctl enable --now "${SERVICE_NAME}"
echo "安装完成: ${SERVICE_NAME}"
echo "查看状态: sudo systemctl status ${SERVICE_NAME}"
