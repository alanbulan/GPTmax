"""
web_app.py - ChatGPT 注册机 Web UI 后端

FastAPI 主程序：
- REST API：配置、注册、账号池、代理、结果查看
- WebSocket：注册日志、池管理日志实时推送

启动：
    python3 -m uvicorn web_app:app --host 0.0.0.0 --port 20080 --reload
    或 PORT=20080 python3 web_app.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Any

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Body
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

import register as reg

# ============================================================
# 应用初始化
# ============================================================

app = FastAPI(title="ChatGPT 注册机 Web UI", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_BASE_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _BASE_DIR / "templates"

# ============================================================
# 全局任务状态
# ============================================================

# 注册任务
_reg_state: Dict[str, Any] = {
    "running": False,
    "success": 0,
    "fail": 0,
    "total": 0,
    "stop_event": None,
    "start_time": None,
}
_reg_ws_clients: List[WebSocket] = []

# 账号池任务
_pool_state: Dict[str, Any] = {
    "running": False,
    "task": "",
    "job_id": "",
    "phase": "",
    "progress_current": 0,
    "progress_total": 0,
    "invalid_count": 0,
    "valid_count": 0,
    "gap": 0,
    "result": None,
    "error": "",
    "started_at": None,
    "finished_at": None,
}
_pool_ws_clients: List[WebSocket] = []
_pool_state_lock = threading.Lock()

_PROBE_RESULT_TTL_SEC = 120

# 事件循环引用
_event_loop: Optional[asyncio.AbstractEventLoop] = None
_reg_log_send_lock: Optional[asyncio.Lock] = None
_pool_log_send_lock: Optional[asyncio.Lock] = None

_SYNC_STATUS_CACHE_TTL_SEC = 10
_SYNC_STATUS_CACHE_MAX = 8
_sync_status_cache: Dict[str, Dict[str, Any]] = {}  # key -> {"ts": float, "data": dict}
_sync_status_cache_lock = threading.Lock()


@app.on_event("startup")
async def _startup():
    global _event_loop, _reg_log_send_lock, _pool_log_send_lock
    _event_loop = asyncio.get_event_loop()
    _reg_log_send_lock = asyncio.Lock()
    _pool_log_send_lock = asyncio.Lock()


# ============================================================
# 辅助：线程安全地推送日志到在线 WebSocket 客户端
# ============================================================

async def _broadcast_log_async(clients: List[WebSocket], msg: str, lock: Optional[asyncio.Lock]):
    if not clients:
        return
    payload = json.dumps({"type": "log", "msg": msg})
    if lock is None:
        return
    stale_clients: List[WebSocket] = []
    async with lock:
        for client in list(clients):
            try:
                await client.send_text(payload)
            except Exception:
                stale_clients.append(client)
    for client in stale_clients:
        if client in clients:
            clients.remove(client)


def _push_log_sync(clients: List[WebSocket], msg: str, lock: Optional[asyncio.Lock]):
    """从同步线程将日志推送到当前在线客户端，不积压离线历史日志"""
    if not msg or not clients:
        return
    if _event_loop and not _event_loop.is_closed():
        try:
            asyncio.run_coroutine_threadsafe(_broadcast_log_async(clients, msg, lock), _event_loop)
        except Exception:
            pass


def _make_reg_log_cb():
    return lambda msg: _push_log_sync(_reg_ws_clients, msg, _reg_log_send_lock)


def _make_pool_log_cb():
    return lambda msg: _push_log_sync(_pool_ws_clients, msg, _pool_log_send_lock)


def _pool_lifecycle_disabled() -> None:
    raise HTTPException(
        status_code=410,
        detail="注册机已切为纯注册上传模式；账号探测、清理、同步、守护改由 CLIProxyAPI 负责",
    )


def _token_fingerprint(token: str) -> str:
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _build_probe_signature(base_url: str, token: str, target_type: str, proxy: str) -> str:
    raw = "|".join([
        str(base_url or "").strip().rstrip("/"),
        str(target_type or "").strip().lower(),
        _token_fingerprint(str(token or "").strip()),
        str(proxy or "").strip(),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def _sync_status_cache_clear():
    with _sync_status_cache_lock:
        _sync_status_cache.clear()


def _sync_status_cache_get(key: str) -> Optional[dict]:
    now = time.time()
    with _sync_status_cache_lock:
        ent = _sync_status_cache.get(key)
        if not ent:
            return None
        if now - float(ent.get("ts", 0)) > _SYNC_STATUS_CACHE_TTL_SEC:
            _sync_status_cache.pop(key, None)
            return None
        return ent.get("data")


def _sync_status_cache_set(key: str, data: dict):
    now = time.time()
    with _sync_status_cache_lock:
        _sync_status_cache[key] = {"ts": now, "data": data}
        if len(_sync_status_cache) <= _SYNC_STATUS_CACHE_MAX:
            return
        # drop oldest entries
        items = sorted(_sync_status_cache.items(), key=lambda kv: float(kv[1].get("ts", 0)))
        for k, _ in items[:-_SYNC_STATUS_CACHE_MAX]:
            _sync_status_cache.pop(k, None)


def _start_pool_job(task: str, phase: str = "") -> str:
    job_id = f"{task}-{int(time.time() * 1000)}"
    with _pool_state_lock:
        _pool_state.update({
            "running": True,
            "task": task,
            "job_id": job_id,
            "phase": phase or task,
            "progress_current": 0,
            "progress_total": 0,
            "invalid_count": 0,
            "valid_count": 0,
            "gap": 0,
            "result": None,
            "error": "",
            "started_at": int(time.time()),
            "finished_at": None,
        })
    return job_id


def _update_pool_job(**updates):
    with _pool_state_lock:
        _pool_state.update(updates)


def _finish_pool_job(result: Optional[dict] = None, error: str = ""):
    with _pool_state_lock:
        _pool_state["running"] = False
        _pool_state["finished_at"] = int(time.time())
        _pool_state["result"] = result
        _pool_state["error"] = error


def _make_reg_progress_cb():
    def cb(success: int, fail: int, total: int):
        _reg_state["success"] = success
        _reg_state["fail"] = fail
        _reg_state["total"] = total
    return cb


# ============================================================
# 静态文件 & 首页
# ============================================================

@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def index():
    html_path = _TEMPLATES_DIR / "index.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="index.html 不存在")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.api_route("/healthz", methods=["GET", "HEAD"], response_class=PlainTextResponse)
async def healthz():
    return PlainTextResponse("ok")


# ============================================================
# 配置 API
# ============================================================

@app.get("/api/config")
async def get_config():
    return reg.load_config()


@app.post("/api/config")
async def save_config(config: dict = Body(...)):
    # 移除 _comment 字段保护
    config.pop("_comment", None)
    ok = reg.save_config(config)
    if not ok:
        raise HTTPException(status_code=500, detail="保存失败")

    # 如果守护进程已启用，同步更新其运行时配置（下次周期生效）
    pool = config.get("pool", {})
    if _pool_daemon["enabled"] and pool:
        _pool_daemon["config"].update({
            "base_url":     pool.get("base_url",     _pool_daemon["config"].get("base_url", "")),
            "token":        pool.get("token",         _pool_daemon["config"].get("token", "")),
            "target_type":  pool.get("target_type",  _pool_daemon["config"].get("target_type", "codex")),
            "target_count": int(pool.get("target_count", _pool_daemon["config"].get("target_count", 10))),
            "proxy":        pool.get("proxy",         _pool_daemon["config"].get("proxy", "")),
        })
        _pool_daemon["interval_min"] = max(1, int(pool.get("interval_min", _pool_daemon["interval_min"])))

    return {"ok": True}


# ============================================================
# 注册任务 API
# ============================================================

@app.post("/api/register/start")
async def register_start(body: dict = Body(...)):
    if _reg_state["running"]:
        raise HTTPException(status_code=409, detail="已有注册任务运行中")

    count = int(body.get("count", 1))
    workers = int(body.get("workers", 3))
    proxy = str(body.get("proxy", "")).strip()
    config = reg.load_config()

    # 重置状态
    _reg_state.update({
        "running": True,
        "success": 0,
        "fail": 0,
        "total": count,
        "start_time": time.time(),
    })
    stop_event = threading.Event()
    _reg_state["stop_event"] = stop_event

    log_cb = _make_reg_log_cb()
    progress_cb = _make_reg_progress_cb()

    def run_task():
        try:
            result = reg.run_batch_register(
                count=count,
                workers=workers,
                proxy=proxy,
                stop_event=stop_event,
                log_cb=log_cb,
                progress_cb=progress_cb,
                config=config,
            )
            _reg_state.update(result)
        except Exception as e:
            log_cb(f"[ERROR] 注册任务异常: {e}")
        finally:
            _reg_state["running"] = False
            _reg_state["stop_event"] = None
            _sync_status_cache_clear()
            log_cb("[注册] 任务结束")

    thread = threading.Thread(target=run_task, daemon=True)
    thread.start()

    return {"ok": True, "count": count, "workers": workers}


@app.post("/api/register/stop")
async def register_stop():
    stop_event = _reg_state.get("stop_event")
    if stop_event:
        stop_event.set()
    _reg_state["running"] = False
    return {"ok": True}


@app.get("/api/register/status")
async def register_status():
    elapsed = None
    if _reg_state.get("start_time"):
        elapsed = int(time.time() - _reg_state["start_time"])
    return {
        "running": _reg_state["running"],
        "success": _reg_state["success"],
        "fail": _reg_state["fail"],
        "total": _reg_state["total"],
        "elapsed": elapsed,
    }


# ============================================================
# 注册日志 WebSocket
# ============================================================

@app.websocket("/ws/register/logs")
async def ws_register_logs(ws: WebSocket):
    await ws.accept()
    _reg_ws_clients.append(ws)
    try:
        while True:
            try:
                await asyncio.sleep(15)
                await ws.send_text(json.dumps({"type": "ping"}))
            except Exception:
                break
    except WebSocketDisconnect:
        pass
    finally:
        if ws in _reg_ws_clients:
            _reg_ws_clients.remove(ws)


# ============================================================
# 账号池 API
# ============================================================

@app.post("/api/pool/probe")
async def pool_probe(body: dict = Body(...)):
    _pool_lifecycle_disabled()


@app.post("/api/pool/clean")
async def pool_clean(body: dict = Body(...)):
    _pool_lifecycle_disabled()


@app.post("/api/pool/fill")
async def pool_fill(body: dict = Body(...)):
    if _pool_state["running"]:
        raise HTTPException(status_code=409, detail="已有池任务运行中")

    count = int(body.get("count", 1))
    base_url = body.get("base_url", "").strip()
    pool_token = body.get("token", "").strip()
    proxy = body.get("proxy", "").strip()
    target_type = body.get("target_type", "codex")
    target_count = int(body.get("target_count", 0))
    config = reg.load_config()

    job_id = _start_pool_job("fill", "filling")
    log_cb = _make_pool_log_cb()
    stop_event = threading.Event()
    _update_pool_job(stop_event=stop_event, progress_current=0, progress_total=count)

    def progress_cb(s, f, t):
        _update_pool_job(progress_current=int(s + f), progress_total=int(t))

    def run_task():
        try:
            result = reg.run_pool_fill(
                fill_count=count,
                base_url=base_url,
                pool_token=pool_token,
                stop_event=stop_event,
                log_cb=log_cb,
                progress_cb=progress_cb,
                config=config,
                proxy=proxy,
                target_count=target_count,
                target_type=target_type,
            )
            log_cb(f"[Pool] 补号完成: 成功={result.get('success')}, 失败={result.get('fail')}")
            _finish_pool_job(result=result)
        except Exception as e:
            log_cb(f"[ERROR] 补号异常: {e}")
            _finish_pool_job(error=str(e))
        finally:
            _update_pool_job(stop_event=None)
            _sync_status_cache_clear()

    threading.Thread(target=run_task, daemon=True).start()
    return {"ok": True, "task": "fill", "count": count, "job_id": job_id}


@app.post("/api/pool/status")
async def pool_status_api(body: dict = Body(...)):
    base_url = body.get("base_url", "").strip()
    token = body.get("token", "").strip()
    target_type = body.get("target_type", "codex")
    proxy = body.get("proxy", "").strip()

    if not base_url or not token:
        raise HTTPException(status_code=400, detail="base_url 和 token 不能为空")

    result = await asyncio.get_event_loop().run_in_executor(
        None, lambda: reg.get_pool_status(base_url, token, target_type, proxy)
    )
    return result


@app.get("/api/pool/accounts")
async def pool_accounts(base_url: str, token: str,
                        target_type: str = "codex", proxy: str = ""):
    _pool_lifecycle_disabled()


@app.get("/api/pool/sync-status")
async def pool_sync_status(base_url: str, token: str,
                           target_type: str = "codex", proxy: str = "",
                           page: Optional[int] = None, page_size: int = 200):
    _pool_lifecycle_disabled()


@app.post("/api/pool/sync")
async def pool_sync(body: dict = Body(...)):
    _pool_lifecycle_disabled()


@app.get("/api/pool/task-status")
async def pool_task_status():
    with _pool_state_lock:
        return {
            "running": _pool_state["running"],
            "task": _pool_state.get("task", ""),
            "job_id": _pool_state.get("job_id", ""),
            "phase": _pool_state.get("phase", ""),
            "progress_current": _pool_state.get("progress_current", 0),
            "progress_total": _pool_state.get("progress_total", 0),
            "invalid_count": _pool_state.get("invalid_count", 0),
            "valid_count": _pool_state.get("valid_count", 0),
            "gap": _pool_state.get("gap", 0),
            "result": _pool_state.get("result"),
            "error": _pool_state.get("error", ""),
            "started_at": _pool_state.get("started_at"),
            "finished_at": _pool_state.get("finished_at"),
        }


@app.post("/api/pool/inspect")
async def pool_inspect(body: dict = Body(...)):
    _pool_lifecycle_disabled()


# ============================================================
# 池管理日志 WebSocket
# ============================================================

@app.websocket("/ws/pool/logs")
async def ws_pool_logs(ws: WebSocket):
    await ws.accept()
    _pool_ws_clients.append(ws)
    try:
        while True:
            try:
                await asyncio.sleep(15)
                await ws.send_text(json.dumps({"type": "ping"}))
            except Exception:
                break
    except WebSocketDisconnect:
        pass
    finally:
        if ws in _pool_ws_clients:
            _pool_ws_clients.remove(ws)


# ============================================================
# 代理管理 API
# ============================================================

@app.get("/api/proxy/fetch")
async def proxy_fetch():
    config = reg.load_config()
    fallback_proxy = config.get("proxy", "")
    proxies = await asyncio.get_event_loop().run_in_executor(
        None, lambda: reg.fetch_free_proxies(proxy=fallback_proxy)
    )
    return {"ok": True, "proxies": proxies, "count": len(proxies)}


@app.post("/api/proxy/test")
async def proxy_test(body: dict = Body(...)):
    proxies = body.get("proxies", [])
    target_url = body.get("target_url", "https://httpbin.org/ip")
    timeout = int(body.get("timeout", 5))

    if not proxies:
        raise HTTPException(status_code=400, detail="proxies 不能为空")
    if len(proxies) > 50:
        proxies = proxies[:50]

    results = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: reg.test_proxies_concurrent(proxies, target_url, timeout, max_workers=20),
    )
    return {"ok": True, "results": results}


# ============================================================
# 结果查看 API
# ============================================================

@app.get("/api/results")
async def get_results(page: int = 1, page_size: int = 200):
    config = reg.load_config()
    accounts = reg.read_registered_accounts(config)
    total = len(accounts)
    page = max(1, int(page))
    page_size = max(1, min(1000, int(page_size)))
    total_pages = max(1, (total + page_size - 1) // page_size)
    if page > total_pages:
        page = total_pages
    start = (page - 1) * page_size
    end = start + page_size
    return {
        "ok": True,
        "accounts": accounts[start:end],
        "count": total,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


@app.get("/api/tokens")
async def get_tokens():
    config = reg.load_config()
    tokens = reg.list_codex_tokens(config)
    return {"ok": True, "tokens": tokens, "count": len(tokens)}


@app.get("/api/tokens/ak")
async def get_ak():
    config = reg.load_config()
    content = reg.read_token_file("ak.txt", config)
    return PlainTextResponse(content=content)


@app.get("/api/tokens/rk")
async def get_rk():
    config = reg.load_config()
    content = reg.read_token_file("rk.txt", config)
    return PlainTextResponse(content=content)


@app.get("/api/tokens/download/ak")
async def download_ak():
    config = reg.load_config()
    ak_file = config.get("ak_file", "ak.txt")
    if not os.path.isabs(ak_file):
        ak_file = str(_BASE_DIR / ak_file)
    if not os.path.exists(ak_file):
        raise HTTPException(status_code=404, detail="ak.txt 不存在")
    return FileResponse(ak_file, filename="ak.txt", media_type="text/plain")


@app.get("/api/tokens/download/rk")
async def download_rk():
    config = reg.load_config()
    rk_file = config.get("rk_file", "rk.txt")
    if not os.path.isabs(rk_file):
        rk_file = str(_BASE_DIR / rk_file)
    if not os.path.exists(rk_file):
        raise HTTPException(status_code=404, detail="rk.txt 不存在")
    return FileResponse(rk_file, filename="rk.txt", media_type="text/plain")


# ============================================================
# 号池守护进程
# ============================================================

_pool_daemon: Dict[str, Any] = {
    "enabled": False,
    "interval_min": 30,
    "next_run_ts": None,
    "last_run_ts": None,
    "running_now": False,
    "config": {},  # {base_url, token, target_type, target_count, proxy}
}
_pool_daemon_timer: Optional[threading.Timer] = None


def _run_daemon_once():
    return


@app.post("/api/pool/daemon/start")
async def pool_daemon_start(body: dict = Body(...)):
    _pool_lifecycle_disabled()


@app.post("/api/pool/daemon/stop")
async def pool_daemon_stop():
    _pool_lifecycle_disabled()


@app.get("/api/pool/daemon/status")
async def pool_daemon_status():
    _pool_lifecycle_disabled()


@app.post("/api/pool/daemon/run-once")
async def pool_daemon_run_once(body: dict = Body(default={})):
    _pool_lifecycle_disabled()


# ============================================================
# 代理池端点
# ============================================================

@app.post("/api/proxy/pool/update")
async def proxy_pool_update(body: dict = Body(...)):
    """接收前端测试结果，更新内存代理池"""
    results = body.get("results", [])
    if not isinstance(results, list):
        raise HTTPException(status_code=400, detail="results 必须是列表")
    reg._proxy_pool.update(results)
    best = reg._proxy_pool.get_best()
    return {"ok": True, "count": len(results), "best": best}


@app.get("/api/proxy/active")
async def proxy_active():
    """返回当前最优代理"""
    config = reg.load_config()
    fallback = config.get("proxy", "")
    best = reg._proxy_pool.get_best(fallback)
    source = "free_pool" if reg._proxy_pool.get_best() else ("user_config" if fallback else "direct")
    return {
        "proxy": best,
        "source": source,
        "pool_size": len(reg._proxy_pool.get_all()),
    }


# ============================================================
# 主入口
# ============================================================

if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "20080"))
    uvicorn.run(
        "web_app:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )
