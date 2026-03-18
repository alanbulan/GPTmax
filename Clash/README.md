# Clash（Mihomo）部署说明

## 已完成
- 内核：Mihomo Meta `v1.19.20`（Linux arm64）
- 可执行文件：`/Project/Clash/bin/mihomo`
- 主配置：`/Project/Clash/config/config.yaml`
- 启动脚本：`/Project/Clash/start.sh`

## 当前配置
- 已按你提供的 4 个节点写入（VLESS Reality / VMess WS / Hysteria2 / TUIC）
- 已配置 3 个策略组：`负载均衡`、`自动选择`、`🌍选择代理节点`
- 默认总入口是 `🌍选择代理节点`

## 启动
```bash
cd /Project/Clash
chmod +x ./start.sh
./start.sh
```

## 使用代理
- HTTP 代理：`127.0.0.1:7890`
- SOCKS5 代理：`127.0.0.1:7891`
- Mixed 代理：`127.0.0.1:7893`

## 为什么这个配置更利于美国
- `自动选择` 会基于 `https://www.gstatic.com/generate_204` 自动测延迟并切换
- `负载均衡` 可在多条美国链路上轮询，降低单节点抖动影响
- `🌍选择代理节点` 支持随时切到 `自动选择`、`负载均衡` 或单节点
