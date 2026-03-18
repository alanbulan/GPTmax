#!/usr/bin/env bash
set -euo pipefail

SKIP_UPDATE=false
BUILD_LOCAL=false
NO_BROWSER=false

for arg in "$@"; do
  case "$arg" in
    --skip-update)
      SKIP_UPDATE=true
      ;;
    --build-local)
      BUILD_LOCAL=true
      ;;
    --no-browser)
      NO_BROWSER=true
      ;;
    *)
      echo "Unknown argument: $arg"
      echo "Usage: ./start-web-docker.sh [--skip-update] [--build-local] [--no-browser]"
      exit 1
      ;;
  esac
done

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_PATH="${ROOT_DIR}/config.yaml"

write_step() { printf "\n[%s] %s\n" "$1" "$2"; }
write_ok() { printf "  %s\n" "$1"; }
write_info() { printf "  %s\n" "$1"; }

retry_command() {
  local max_attempts="$1"
  local delay_seconds="$2"
  shift 2

  local attempt=1
  local exit_code=0
  until "$@"; do
    exit_code=$?
    if (( attempt >= max_attempts )); then
      return "${exit_code}"
    fi
    write_info "命令失败（退出码 ${exit_code}），${delay_seconds}s 后重试（${attempt}/${max_attempts}）: $*"
    sleep "${delay_seconds}"
    attempt=$((attempt + 1))
  done
}

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
  port="$(grep -E "^[[:space:]]*port:[[:space:]]*[\"']?[0-9]+[\"']?[[:space:]]*($|#)" "$1" | sed -E "s/^[[:space:]]*port:[[:space:]]*[\"']?([0-9]+)[\"']?.*$/\1/" | head -n1 || true)"
  if [[ -z "${port}" ]]; then
    echo "8317"
  else
    echo "${port}"
  fi
}

echo "=== CLIProxyAPI Web 版 Docker 一键部署（Linux） ==="
write_info "项目目录: ${ROOT_DIR}"

ensure_tool docker
if [[ "${SKIP_UPDATE}" == "false" || "${BUILD_LOCAL}" == "true" ]]; then
  ensure_tool git
fi
if ! docker info >/dev/null 2>&1; then
  echo "错误: 无法连接 Docker daemon，请先启动 Docker 服务后重试"
  exit 1
fi
write_ok "环境检查通过"

cd "${ROOT_DIR}"

if [[ "${SKIP_UPDATE}" == "false" ]]; then
  write_step "1/5" "拉取并合并最新代码"
  if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    write_info "检测到本地已跟踪文件存在未提交改动，跳过自动更新"
  else
    retry_command 3 2 git fetch origin main --tags --progress
    behind="$(git rev-list --count HEAD..origin/main)"
    if [[ "${behind}" -gt 0 ]]; then
      if git merge-base --is-ancestor HEAD origin/main; then
        git merge --ff-only origin/main
      else
        write_info "检测到当前分支与 origin/main 已分叉，执行普通合并"
        git merge --no-edit origin/main
      fi
      write_ok "已更新到最新代码"
    else
      write_ok "当前已是最新代码"
    fi
  fi
else
  write_step "1/5" "跳过更新"
  write_info "已启用 --skip-update"
fi

write_step "2/5" "准备配置文件和目录"
if [[ ! -f "${CONFIG_PATH}" ]]; then
  cp "${ROOT_DIR}/config.example.yaml" "${CONFIG_PATH}"
  write_ok "已创建 config.yaml，请按需填写 key 后重启"
else
  write_ok "检测到现有 config.yaml"
fi
mkdir -p "${ROOT_DIR}/auths" "${ROOT_DIR}/logs"

if [[ "${BUILD_LOCAL}" == "true" ]]; then
  write_step "3/5" "构建本地镜像"
  VERSION="$(git describe --tags --always --dirty 2>/dev/null || echo dev)"
  COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo none)"
  BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  export CLI_PROXY_IMAGE="cli-proxy-api:local"
  docker build \
    --build-arg VERSION="${VERSION}" \
    --build-arg COMMIT="${COMMIT}" \
    --build-arg BUILD_DATE="${BUILD_DATE}" \
    -t "${CLI_PROXY_IMAGE}" \
    -f Dockerfile .
  write_ok "本地镜像构建完成: ${CLI_PROXY_IMAGE}"
else
  write_step "3/5" "拉取最新预构建镜像"
  if [[ -z "${CLI_PROXY_IMAGE:-}" ]]; then
    export CLI_PROXY_IMAGE="docker.1ms.run/eceasy/cli-proxy-api:latest"
  fi
  retry_command 3 2 docker pull "${CLI_PROXY_IMAGE}"
  write_ok "预构建镜像已更新"
fi

write_step "4/5" "启动容器"
docker rm -f cli-proxy-api >/dev/null 2>&1 || true
docker run -d \
  --name cli-proxy-api \
  --restart unless-stopped \
  -e DEPLOY="${DEPLOY:-}" \
  -p 8317:8317 \
  -p 8085:8085 \
  -p 1455:1455 \
  -p 54545:54545 \
  -p 51121:51121 \
  -p 11451:11451 \
  -v "${ROOT_DIR}/config.yaml:/CLIProxyAPI/config.yaml" \
  -v "${ROOT_DIR}/auths:/root/.cli-proxy-api" \
  -v "${ROOT_DIR}/logs:/CLIProxyAPI/logs" \
  "${CLI_PROXY_IMAGE}" >/dev/null
write_ok "服务已启动"

write_step "5/5" "打开管理页"
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
write_info "管理页静态资源由后端在运行时自动检查并更新"
write_info "查看日志: docker logs -f cli-proxy-api"
