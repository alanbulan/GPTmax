#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import requests
import yaml


DEFAULT_CONFIG = Path("/Project/Clash/config/config.yaml")
DEFAULT_LOG = Path("/Project/Clash/clash.log")
DEFAULT_BIN = Path("/Project/Clash/bin/mihomo")
DEFAULT_TEST_URL = "https://chatgpt.com/cdn-cgi/trace"
DEFAULT_CONTROLLER = "127.0.0.1:19090"
DEFAULT_HTTP_PORT = 17890
DEFAULT_SOCKS_PORT = 7891
DEFAULT_MIXED_PORT = 7893
DEFAULT_REGION_DENY = ["HK", "香港", "TW", "台湾", "Taiwan", "CN", "中国"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update Mihomo config from a VLESS subscription and filter regions.")
    parser.add_argument("--url", required=True, help="Subscription URL")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Target config path")
    parser.add_argument("--mihomo-bin", default=str(DEFAULT_BIN), help="Mihomo binary path")
    parser.add_argument("--log-file", default=str(DEFAULT_LOG), help="Mihomo log path")
    parser.add_argument("--controller", default=DEFAULT_CONTROLLER, help="External controller address")
    parser.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT)
    parser.add_argument("--socks-port", type=int, default=DEFAULT_SOCKS_PORT)
    parser.add_argument("--mixed-port", type=int, default=DEFAULT_MIXED_PORT)
    parser.add_argument("--test-url", default=DEFAULT_TEST_URL)
    parser.add_argument("--deny-region", action="append", default=[], help="Region keyword to remove; can repeat")
    parser.add_argument("--group-name", default="🤖OpenAI自动")
    parser.add_argument("--lb-group-name", default="🤖OpenAI负载")
    parser.add_argument("--entry-group-name", default="🌍选择代理节点")
    parser.add_argument("--no-restart", action="store_true", help="Only rewrite config without restarting mihomo")
    return parser.parse_args()


def fetch_subscription(url: str) -> str:
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text.strip()


def decode_subscription(raw: str) -> list[str]:
    padded = raw + "=" * (-len(raw) % 4)
    decoded = base64.b64decode(padded).decode("utf-8", "replace")
    return [line.strip() for line in decoded.splitlines() if line.strip()]


def normalize_name(fragment: str, host: str, port: int) -> str:
    fragment = re.sub(r"[^A-Za-z0-9._-]+", "-", fragment).strip("-") or "NODE"
    return f"{fragment}-{host}-{port}"


def parse_vless_lines(lines: list[str], deny_region: list[str]) -> list[dict[str, Any]]:
    deny = [item.lower() for item in deny_region]
    proxies: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for line in lines:
        if not line.startswith("vless://"):
            continue
        parsed = urlparse(line)
        host = (parsed.hostname or "").strip()
        port = parsed.port
        uuid = (parsed.username or "").strip()
        if not host or not port or not uuid:
            continue
        if host in {"127.0.0.1", "localhost"}:
            continue
        query = parse_qs(parsed.query)
        if (query.get("type", [""])[0] or "").lower() != "ws":
            continue
        fragment = unquote(parsed.fragment or "").strip()
        name = normalize_name(fragment, host, port)
        if any(tag in name.lower() for tag in deny):
            continue
        key = (host, port, uuid)
        if key in seen:
            continue
        seen.add(key)
        proxies.append(
            {
                "name": name,
                "type": "vless",
                "server": host,
                "port": port,
                "uuid": uuid,
                "network": "ws",
                "udp": True,
                "tls": True,
                "servername": (query.get("sni", [""])[0] or query.get("host", [""])[0] or "").strip(),
                "client-fingerprint": (query.get("fp", ["chrome"])[0] or "chrome").strip(),
                "ws-opts": {
                    "path": unquote(query.get("path", ["/"])[0] or "/"),
                    "headers": {"Host": (query.get("host", [""])[0] or query.get("sni", [""])[0] or "").strip()},
                },
            }
        )
    return proxies


def build_config(args: argparse.Namespace, proxies: list[dict[str, Any]]) -> dict[str, Any]:
    names = [item["name"] for item in proxies]
    return {
        "port": args.http_port,
        "socks-port": args.socks_port,
        "mixed-port": args.mixed_port,
        "allow-lan": True,
        "mode": "rule",
        "log-level": "info",
        "unified-delay": True,
        "ipv6": True,
        "external-controller": args.controller,
        "dns": {
            "enable": True,
            "ipv6": True,
            "enhanced-mode": "fake-ip",
            "fake-ip-range": "198.18.0.1/16",
            "nameserver": ["https://dns.google/dns-query", "https://1.1.1.1/dns-query"],
        },
        "proxies": proxies,
        "proxy-groups": [
            {
                "name": args.group_name,
                "type": "url-test",
                "url": args.test_url,
                "interval": 300,
                "tolerance": 80,
                "lazy": True,
                "proxies": names,
            },
            {
                "name": args.lb_group_name,
                "type": "load-balance",
                "url": args.test_url,
                "interval": 300,
                "lazy": True,
                "strategy": "round-robin",
                "proxies": names,
            },
            {
                "name": args.entry_group_name,
                "type": "select",
                "proxies": [args.lb_group_name, args.group_name, *names, "DIRECT"],
            },
        ],
        "rules": [f"MATCH,{args.entry_group_name}"],
    }


def backup_file(path: Path) -> None:
    if not path.exists():
        return
    timestamp = time.strftime("%Y%m%d%H%M%S")
    backup = path.with_name(f"{path.name}.bak.{timestamp}")
    shutil.copy2(path, backup)


def write_config(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)


def stop_existing_mihomo(bin_path: Path) -> None:
    try:
        output = subprocess.check_output(["pgrep", "-f", str(bin_path)], text=True)
    except subprocess.CalledProcessError:
        return
    for raw_pid in output.splitlines():
        raw_pid = raw_pid.strip()
        if not raw_pid:
            continue
        try:
            os.kill(int(raw_pid), signal.SIGTERM)
        except OSError:
            continue
    time.sleep(1)


def start_mihomo(bin_path: Path, config_path: Path, log_path: Path) -> None:
    with log_path.open("ab") as log_file:
        subprocess.Popen(
            [str(bin_path), "-d", str(config_path.parent), "-f", str(config_path)],
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )


def main() -> int:
    args = parse_args()
    config_path = Path(args.config)
    bin_path = Path(args.mihomo_bin)
    log_path = Path(args.log_file)
    deny_region = [*DEFAULT_REGION_DENY, *args.deny_region]

    raw = fetch_subscription(args.url)
    lines = decode_subscription(raw)
    proxies = parse_vless_lines(lines, deny_region)
    if not proxies:
        print("no usable proxies after filtering", file=sys.stderr)
        return 1

    backup_file(config_path)
    config = build_config(args, proxies)
    write_config(config_path, config)

    print(f"wrote {len(proxies)} proxies to {config_path}")
    if args.no_restart:
        return 0

    stop_existing_mihomo(bin_path)
    start_mihomo(bin_path, config_path, log_path)
    print("mihomo restarted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
