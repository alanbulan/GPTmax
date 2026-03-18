#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Concurrent load test for CLIProxyAPI OpenAI-compatible endpoint")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--model", default="gpt-5-codex")
    parser.add_argument("--total", type=int, default=40)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=15.0)
    return parser.parse_args()


def run_one(base_url: str, api_key: str, model: str, timeout: float, idx: int) -> dict:
    started = time.time()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": f"hi {idx}"}],
        "max_tokens": 16,
    }
    try:
        resp = requests.post(
            f"{base_url.rstrip('/')}/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=timeout,
        )
        elapsed_ms = round((time.time() - started) * 1000, 2)
        selected_auth = resp.headers.get("X-CPA-Selected-Auth-Id", "")
        body = resp.text
        outcome = "other"
        error_code = ""
        try:
            data = resp.json()
            if isinstance(data, dict):
                error = data.get("error")
                if isinstance(error, dict):
                    error_code = str(error.get("code") or error.get("type") or "")
                    if resp.status_code == 429:
                        outcome = "quota"
                    elif resp.status_code >= 400:
                        outcome = "error"
                elif resp.status_code == 200:
                    outcome = "success"
        except Exception:
            if resp.status_code == 200:
                outcome = "success"
        return {
            "ok": resp.ok,
            "status": resp.status_code,
            "elapsed_ms": elapsed_ms,
            "selected_auth": selected_auth,
            "outcome": outcome,
            "error_code": error_code,
            "body_preview": body[:300],
        }
    except Exception as exc:
        elapsed_ms = round((time.time() - started) * 1000, 2)
        return {
            "ok": False,
            "status": 0,
            "elapsed_ms": elapsed_ms,
            "selected_auth": "",
            "outcome": "exception",
            "error_code": type(exc).__name__,
            "body_preview": str(exc),
        }


def main() -> int:
    args = parse_args()
    started = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [
            executor.submit(
                run_one,
                args.base_url,
                args.api_key,
                args.model,
                args.timeout,
                idx,
            )
            for idx in range(args.total)
        ]
        for future in as_completed(futures):
            results.append(future.result())

    elapsed = round(time.time() - started, 2)
    by_status: dict[str, int] = {}
    by_outcome: dict[str, int] = {}
    auth_hits: dict[str, int] = {}
    timings = [item["elapsed_ms"] for item in results if item["elapsed_ms"] > 0]
    for item in results:
        by_status[str(item["status"])] = by_status.get(str(item["status"]), 0) + 1
        by_outcome[item["outcome"]] = by_outcome.get(item["outcome"], 0) + 1
        if item["selected_auth"]:
            auth_hits[item["selected_auth"]] = auth_hits.get(item["selected_auth"], 0) + 1

    summary = {
        "base_url": args.base_url,
        "model": args.model,
        "total": args.total,
        "concurrency": args.concurrency,
        "timeout_sec": args.timeout,
        "elapsed_sec": elapsed,
        "status_counts": by_status,
        "outcome_counts": by_outcome,
        "latency_ms": {
            "min": min(timings) if timings else None,
            "p50": statistics.median(timings) if timings else None,
            "max": max(timings) if timings else None,
            "avg": round(sum(timings) / len(timings), 2) if timings else None,
        },
        "selected_auth_top": sorted(auth_hits.items(), key=lambda kv: kv[1], reverse=True)[:20],
        "sample_failures": [item for item in results if not item["ok"]][:10],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
