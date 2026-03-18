#!/usr/bin/env bash
set -euo pipefail

SKIP_UPDATE=false
NO_BROWSER=false

for arg in "$@"; do
  case "$arg" in
    --skip-update)
      SKIP_UPDATE=true
      ;;
    --no-browser)
      NO_BROWSER=true
      ;;
    *)
      echo "Unknown argument: $arg"
      echo "Usage: ./start-web.sh [--skip-update] [--no-browser]"
      exit 1
      ;;
  esac
done

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXE_DIR="${ROOT_DIR}/dist"
EXE_PATH="${EXE_DIR}/cliproxyapi-web"
CONFIG_PATH="${ROOT_DIR}/config.yaml"

write_step() { printf "\n[%s] %s\n" "$1" "$2"; }
write_ok() { printf "  %s\n" "$1"; }
write_info() { printf "  %s\n" "$1"; }

ensure_tool() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "错误: 未找到 $1，请先安装后重试"
    exit 1
  fi
}

get_port_from_config() {
  if [[ ! -f "$1" ]]; then
    echo "8317"
    return
  fi
  local port
  port="$(grep -E '^[[:space:]]*port:[[:space:]]*["'"'"']?[0-9]+["'"'"']?[[:space:]]*($|#)' "$1" | sed -E 's/^[[:space:]]*port:[[:space:]]*["'"'"']?([0-9]+)["'"'"']?.*$/\1/' | head -n1 || true)"
  if [[ -z "${port}" ]]; then
    echo "8317"
  else
    echo "${port}"
  fi
}

echo "=== CLIProxyAPI Web 版一键启动（Linux） ==="
write_info "项目目录: ${ROOT_DIR}"

ensure_tool git
ensure_tool go
write_ok "环境检查通过 (git, go)"

cd "${ROOT_DIR}"

if [[ "${SKIP_UPDATE}" == "false" ]]; then
  write_step "1/4" "拉取并合并最新代码"
  if [[ -n "$(git status --porcelain)" ]]; then
    write_info "检测到本地未提交改动，跳过自动更新"
  else
    git fetch origin main --tags --progress
    behind="$(git rev-list --count HEAD..origin/main)"
    if [[ "${behind}" -gt 0 ]]; then
      git merge --ff-only origin/main
      write_ok "已更新到最新代码"
    else
      write_ok "当前已是最新代码"
    fi
  fi
else
  write_step "1/4" "跳过更新"
  write_info "已启用 --skip-update"
fi

write_step "2/4" "准备配置文件"
if [[ ! -f "${CONFIG_PATH}" ]]; then
  if [[ ! -f "${ROOT_DIR}/config.example.yaml" ]]; then
    echo "错误: 未找到 config.example.yaml"
    exit 1
  fi
  cp "${ROOT_DIR}/config.example.yaml" "${CONFIG_PATH}"
  write_ok "已创建 config.yaml，请按需填写 key 后重启"
else
  write_ok "检测到现有 config.yaml"
fi

write_step "3/4" "构建 Web 版可执行文件"
mkdir -p "${EXE_DIR}"
git_version="$(git describe --tags --always 2>/dev/null || echo dev)"
git_commit="$(git rev-parse --short HEAD 2>/dev/null || echo none)"
build_date="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
ldflags="-X main.Version=${git_version} -X main.Commit=${git_commit} -X main.BuildDate=${build_date}"
go build -trimpath -ldflags "${ldflags}" -o "${EXE_PATH}" ./cmd/server
chmod +x "${EXE_PATH}"
write_ok "构建完成: ${EXE_PATH}"

write_step "4/4" "启动 Web 服务"
port="$(get_port_from_config "${CONFIG_PATH}")"
url="http://127.0.0.1:${port}/management.html"
if [[ "${NO_BROWSER}" == "false" ]]; then
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "${url}" >/dev/null 2>&1 || true
    write_info "已尝试打开管理页: ${url}"
  else
    write_info "未检测到 xdg-open，管理页地址: ${url}"
  fi
else
  write_info "管理页地址: ${url}"
fi
write_info "后端管理页会在运行时自动检查并更新 management.html"
printf "\n按 Ctrl+C 可停止服务\n"
exec "${EXE_PATH}" -config "${CONFIG_PATH}"
