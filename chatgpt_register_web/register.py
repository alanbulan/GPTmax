"""
register.py - ChatGPT 注册机 Web UI 后端库

将 chatgpt_register.py 核心逻辑以 library 形式暴露：
- log_callback: 替代 print，将日志路由到调用方
- stop_event: threading.Event，支持中止批量任务
- progress_callback: (success, fail, total) 进度回调
"""

from __future__ import annotations

import concurrent.futures
import io
import json
import logging
import os
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Any

import requests as _requests
import urllib3
from urllib3.exceptions import InsecureRequestWarning

urllib3.disable_warnings(InsecureRequestWarning)

POOL_PAGE_SIZE = 5000
POOL_PROBE_MAX_ACCOUNTS = 500
POOL_PROBE_WORKERS = 4
POOL_PROBE_TIMEOUT = 5

# ============================================================
# 基础路径
# ============================================================

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ============================================================
# stdout 捕获 → log_callback
# ============================================================

class _LogCapture(io.RawIOBase):
    """将 sys.stdout 写入重定向到 callback"""

    def __init__(self, callback: Callable[[str], None]):
        self._callback = callback
        self._buf = ""
        self._lock = threading.Lock()

    def write(self, text: str) -> int:  # type: ignore[override]
        with self._lock:
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                line = line.rstrip("\r")
                if line and self._callback:
                    try:
                        self._callback(line)
                    except Exception:
                        pass
        return len(text)

    def flush(self):
        pass

    def readable(self):
        return False

    def writable(self):
        return True


class _LoggingHandler(logging.Handler):
    """将 logging 模块输出路由到 callback"""

    def __init__(self, callback: Callable[[str], None]):
        super().__init__()
        self._callback = callback

    def emit(self, record):
        try:
            msg = self.format(record)
            if self._callback:
                self._callback(msg)
        except Exception:
            pass


# ============================================================
# 延迟导入 chatgpt_register（抑制初始化输出）
# ============================================================

_cr_lock = threading.Lock()
_cr = None


def _get_cr():
    """获取 chatgpt_register 模块（懒加载，抑制导入输出）"""
    global _cr
    if _cr is not None:
        return _cr
    with _cr_lock:
        if _cr is not None:
            return _cr
        devnull = open(os.devnull, "w")
        old_stdout = sys.stdout
        sys.stdout = devnull
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "chatgpt_register",
                os.path.join(_BASE_DIR, "chatgpt_register.py")
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore
            _cr = mod
        finally:
            sys.stdout = old_stdout
            devnull.close()
    return _cr


def _apply_config(mod, config: dict):
    """将 config dict 覆盖到 chatgpt_register 模块全局变量"""
    if not config:
        return
    if "duckmail_bearer" in config:
        mod.DUCKMAIL_BEARER = config["duckmail_bearer"]
    if "duckmail_api_base" in config:
        mod.DUCKMAIL_API_BASE = config["duckmail_api_base"].rstrip("/")
    if "enable_oauth" in config:
        mod.ENABLE_OAUTH = mod._as_bool(config["enable_oauth"])
    if "oauth_required" in config:
        mod.OAUTH_REQUIRED = mod._as_bool(config["oauth_required"])
    if "oauth_issuer" in config:
        mod.OAUTH_ISSUER = config["oauth_issuer"].rstrip("/")
    if "oauth_client_id" in config:
        mod.OAUTH_CLIENT_ID = config["oauth_client_id"]
    if "oauth_redirect_uri" in config:
        mod.OAUTH_REDIRECT_URI = config["oauth_redirect_uri"]
    if "ak_file" in config:
        ak = config["ak_file"]
        mod.AK_FILE = ak if os.path.isabs(ak) else os.path.join(_BASE_DIR, ak)
    if "rk_file" in config:
        rk = config["rk_file"]
        mod.RK_FILE = rk if os.path.isabs(rk) else os.path.join(_BASE_DIR, rk)
    if "token_json_dir" in config:
        td = config["token_json_dir"]
        mod.TOKEN_JSON_DIR = td if os.path.isabs(td) else os.path.join(_BASE_DIR, td)


# ============================================================
# 批量注册
# ============================================================

def run_batch_register(
    count: int,
    workers: int,
    proxy: str,
    stop_event: threading.Event,
    log_cb: Callable[[str], None],
    progress_cb: Callable[[int, int, int], None],
    config: Optional[dict] = None,
) -> dict:
    """
    批量注册主函数（在线程中运行，通过回调输出日志）

    Returns:
        {"success": int, "fail": int, "total": int}
    """
    mod = _get_cr()
    _apply_config(mod, config or {})

    output_file = (config or {}).get("output_file", "registered_accounts.txt")
    if not os.path.isabs(output_file):
        output_file = os.path.join(_BASE_DIR, output_file)

    effective_proxy = proxy or (config or {}).get("proxy", "") or mod.DEFAULT_PROXY

    success_count = 0
    fail_count = 0
    total = count
    _counter_lock = threading.Lock()

    def register_one(idx: int):
        nonlocal success_count, fail_count

        if stop_event and stop_event.is_set():
            return False, None, "已停止"

        capture = _LogCapture(log_cb)
        old_stdout = sys.stdout
        sys.stdout = capture
        try:
            ok, email, err = mod._register_one(idx, total, effective_proxy, output_file)
        except Exception as e:
            ok, email, err = False, None, str(e)
        finally:
            sys.stdout = old_stdout

        with _counter_lock:
            if ok:
                success_count += 1
            else:
                fail_count += 1
            if progress_cb:
                try:
                    progress_cb(success_count, fail_count, total)
                except Exception:
                    pass

        return ok, email, err

    actual_workers = min(workers, count)
    if log_cb:
        log_cb(f"[注册] 开始批量注册: 数量={count}, 并发={actual_workers}, 代理={effective_proxy or '无'}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=actual_workers) as executor:
        futures = []
        for i in range(1, count + 1):
            if stop_event and stop_event.is_set():
                break
            futures.append(executor.submit(register_one, i))

        for fut in concurrent.futures.as_completed(futures):
            if stop_event and stop_event.is_set():
                break
            try:
                fut.result()
            except Exception as e:
                if log_cb:
                    log_cb(f"[FAIL] 线程异常: {e}")

    if log_cb:
        log_cb(f"[注册] 完成: 成功={success_count}, 失败={fail_count}, 总计={total}")

    return {"success": success_count, "fail": fail_count, "total": total}


# ============================================================
# 账号池管理（直接 HTTP 调用）
# ============================================================

def _pool_session(proxy: str = "", timeout: int = 10) -> _requests.Session:
    s = _requests.Session()
    s.verify = False
    return s


def _fetch_pool_auth_files(
    base_url: str,
    token: str,
    target_type: str = "codex",
    timeout: int = 10,
    page: Optional[int] = None,
    page_size: Optional[int] = None,
    search: str = "",
) -> dict:
    helper_url = f"{base_url.rstrip('/')}/v0/management/auth-files"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    params = {"type": target_type}
    params["compact"] = "1"
    if page is not None:
        params["page"] = str(page)
    if page_size is not None:
        params["page_size"] = str(page_size)
    if search:
        params["search"] = search

    session = _pool_session("", timeout)
    resp = session.get(helper_url, headers=headers, params=params, timeout=max(timeout, 30))
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def _normalize_pool_name(name: str) -> str:
    raw = str(name or "").strip()
    if raw.endswith(".json"):
        raw = raw[:-5]
    return raw.strip().lower()


def _batch_lookup_pool_auth_files(
    base_url: str,
    token: str,
    names: List[str],
    target_type: str = "codex",
    timeout: int = 10,
) -> dict:
    clean_names: List[str] = []
    seen = set()
    for raw in names:
        name = str(raw or "").strip()
        key = _normalize_pool_name(name)
        if not name or not key or key in seen:
            continue
        seen.add(key)
        clean_names.append(name)

    if not clean_names:
        return {"ok": True, "items": [], "lookup": {}}

    helper_url = f"{base_url.rstrip('/')}/v0/management/auth-files/lookup"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    payload = {"names": clean_names, "type": target_type, "compact": True}
    session = _pool_session("", timeout)
    resp = session.post(helper_url, headers=headers, json=payload, timeout=max(timeout, 30))
    resp.raise_for_status()
    data = resp.json() if resp.content else {}
    items = data.get("items", []) if isinstance(data, dict) else []
    lookup = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        key = _normalize_pool_name(item.get("query_name") or item.get("name"))
        if key:
            lookup[key] = item
    return {"ok": True, "items": items, "lookup": lookup}


def get_pool_status(
    base_url: str,
    token: str,
    target_type: str = "codex",
    proxy: str = "",
    timeout: int = 10,
) -> dict:
    """获取账号池状态（总数、目标类型数）"""
    try:
        raw = _fetch_pool_auth_files(base_url, token, target_type, timeout=timeout, page=1, page_size=1)
        files = raw.get("files", []) if isinstance(raw, dict) else []
        total = int(raw.get("all_total") or raw.get("total") or len(files))
        type_counts = raw.get("type_counts", {}) if isinstance(raw, dict) else {}
        target_count = int(type_counts.get(target_type.lower(), 0)) if isinstance(type_counts, dict) else 0
        return {"ok": True, "total": total, "target": target_count, "target_type": target_type}
    except Exception as e:
        return {"ok": False, "error": str(e), "total": 0, "target": 0}


def get_pool_accounts(
    base_url: str,
    token: str,
    target_type: str = "codex",
    proxy: str = "",
    timeout: int = 10,
    max_accounts: int = 0,
    page_size: Optional[int] = None,
) -> dict:
    """返回 CliProxyAPI 上指定类型的账号列表（名称）"""
    try:
        request_page_size = int(page_size or POOL_PAGE_SIZE)
        request_page_size = max(1, min(request_page_size, POOL_PAGE_SIZE))
        if max_accounts > 0:
            request_page_size = min(request_page_size, max_accounts)

        first = _fetch_pool_auth_files(
            base_url, token, target_type, timeout=timeout, page=1, page_size=request_page_size
        )
        if not isinstance(first, dict):
            return {"ok": False, "error": "账号池返回格式错误", "accounts": []}

        total = int(first.get("total") or 0)
        total_pages = int(first.get("total_pages") or 1)
        accounts = list(first.get("files", []) or [])
        if max_accounts > 0 and len(accounts) >= max_accounts:
            accounts = accounts[:max_accounts]
            return {"ok": True, "accounts": accounts, "total": total or len(accounts)}

        fetch_total_pages = total_pages
        if max_accounts > 0:
            fetch_total_pages = min(total_pages, max(1, (max_accounts + request_page_size - 1) // request_page_size))

        for next_page in range(2, fetch_total_pages + 1):
            page_data = _fetch_pool_auth_files(
                base_url, token, target_type, timeout=timeout, page=next_page, page_size=request_page_size
            )
            if isinstance(page_data, dict):
                accounts.extend(page_data.get("files", []) or [])
            if max_accounts > 0 and len(accounts) >= max_accounts:
                accounts = accounts[:max_accounts]
                break
        return {"ok": True, "accounts": accounts, "total": total or len(accounts)}
    except Exception as e:
        return {"ok": False, "error": str(e), "accounts": []}


def list_invalid_pool_accounts(
    base_url: str,
    token: str,
    target_type: str = "codex",
    timeout: int = 10,
    page_size: int = 1000,
) -> dict:
    """扫描远程账号池，返回已被后端标记为无效/禁用的账号名"""
    invalid = []
    seen = set()
    try:
        first = _fetch_pool_auth_files(
            base_url,
            token,
            target_type,
            timeout=timeout,
            page=1,
            page_size=max(1, min(page_size, POOL_PAGE_SIZE)),
        )
        if not isinstance(first, dict):
            return {"ok": False, "error": "账号池返回格式错误", "invalid": []}
        total_pages = int(first.get("total_pages") or 1)

        def collect(files):
            for item in files or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "") or "").strip()
                if not name or name in seen:
                    continue
                status = str(item.get("status", "") or "").strip().lower()
                status_message = str(item.get("status_message", "") or "").strip().lower()
                disabled = bool(item.get("disabled"))
                token_invalid = bool(item.get("token_invalid"))
                if disabled or token_invalid or status == "disabled" or status_message == "token_invalid":
                    seen.add(name)
                    invalid.append({"name": name, "reason": "backend_invalid"})

        collect(first.get("files", []) or [])
        for page in range(2, total_pages + 1):
            data = _fetch_pool_auth_files(
                base_url,
                token,
                target_type,
                timeout=timeout,
                page=page,
                page_size=max(1, min(page_size, POOL_PAGE_SIZE)),
            )
            if isinstance(data, dict):
                collect(data.get("files", []) or [])

        return {"ok": True, "invalid": invalid, "invalid_count": len(invalid)}
    except Exception as e:
        return {"ok": False, "error": str(e), "invalid": []}


def get_sync_status(
    base_url: str,
    pool_token: str,
    target_type: str = "codex",
    config: Optional[dict] = None,
    proxy: str = "",
    page: Optional[int] = None,
    page_size: int = 200,
) -> dict:
    """只读：对比本地文件与远程账号，返回每个账号的同步状态"""
    tokens_dir = os.path.join(_BASE_DIR, "codex_tokens")
    uploaded_dir = os.path.join(tokens_dir, "uploaded")

    # 枚举本地文件
    root_names: set = set()
    if os.path.isdir(tokens_dir):
        for f in os.listdir(tokens_dir):
            if f.endswith(".json") and os.path.isfile(os.path.join(tokens_dir, f)):
                root_names.add(f[:-5])  # 去掉 .json 后缀

    uploaded_names: set = set()
    if os.path.isdir(uploaded_dir):
        for f in os.listdir(uploaded_dir):
            if f.endswith(".json") and os.path.isfile(os.path.join(uploaded_dir, f)):
                uploaded_names.add(f[:-5])

    if page is None:
        remote_result = get_pool_accounts(base_url, pool_token, target_type, proxy)
        if not remote_result.get("ok"):
            return {"ok": False, "error": remote_result.get("error", "获取远程账号失败")}

        remote_accounts_raw = remote_result.get("accounts", [])

        def _strip_json(n: str) -> str:
            return n[:-5] if n.endswith(".json") else n

        remote_names: set = {_strip_json(a["name"]) for a in remote_accounts_raw if a.get("name")}
        all_names = root_names | uploaded_names | remote_names

        STATUS_ORDER = {"pending_move": 0, "pending_upload": 1, "remote_only": 2, "synced": 3, "local_only": 4}

        accounts = []
        summary = {"synced": 0, "pending_upload": 0, "pending_move": 0, "remote_only": 0, "local_only": 0}

        for name in all_names:
            in_root = name in root_names
            in_uploaded = name in uploaded_names
            in_remote = name in remote_names

            if in_uploaded and in_remote:
                status = "synced"
                location = "uploaded"
            elif in_root and not in_remote:
                status = "pending_upload"
                location = "root"
            elif in_root and in_remote:
                status = "pending_move"
                location = "root"
            elif in_remote and not in_uploaded and not in_root:
                status = "remote_only"
                location = "remote"
            else:
                status = "local_only"
                location = "uploaded"

            summary[status] = summary.get(status, 0) + 1
            accounts.append({
                "name": name,
                "status": status,
                "type": target_type,
                "location": location,
            })

        accounts.sort(key=lambda x: (STATUS_ORDER.get(x["status"], 99), x.get("name", "")))
        return {"ok": True, "accounts": accounts, "summary": summary}

    try:
        page_i = max(1, int(page))
    except Exception:
        page_i = 1

    try:
        page_size_i = int(page_size)
    except Exception:
        page_size_i = 200
    page_size_i = max(1, min(page_size_i, 2000))

    local_names = sorted(root_names | uploaded_names)
    total = len(local_names)
    total_pages = max(1, (total + page_size_i - 1) // page_size_i)
    if page_i > total_pages:
        page_i = total_pages
    start = (page_i - 1) * page_size_i
    end = min(start + page_size_i, total)
    page_names = local_names[start:end]

    lookup_result = _batch_lookup_pool_auth_files(
        base_url=base_url,
        token=pool_token,
        names=page_names,
        target_type=target_type,
        timeout=10,
    )
    if not lookup_result.get("ok"):
        return {"ok": False, "error": lookup_result.get("error", "批量查询远程账号失败")}

    lookup = lookup_result.get("lookup", {}) or {}
    page_accounts = []
    summary = {"synced": 0, "pending_upload": 0, "pending_move": 0, "remote_only": 0, "local_only": 0}

    for name in page_names:
        in_root = name in root_names
        in_uploaded = name in uploaded_names
        remote_item = lookup.get(_normalize_pool_name(name), {})
        in_remote = bool(isinstance(remote_item, dict) and remote_item.get("exists"))

        if in_uploaded and in_remote:
            status = "synced"
            location = "uploaded"
        elif in_root and in_remote:
            status = "pending_move"
            location = "root"
        elif in_root:
            status = "pending_upload"
            location = "root"
        else:
            status = "local_only"
            location = "uploaded"

        summary[status] = summary.get(status, 0) + 1
        page_accounts.append({
            "name": name,
            "status": status,
            "type": target_type,
            "location": location,
        })

    return {
        "ok": True,
        "accounts": page_accounts,
        "summary": summary,
        "summary_scope": "page",
        "page": page_i,
        "page_size": page_size_i,
        "total": total,
        "total_pages": total_pages,
    }


def sync_local_remote(
    base_url: str,
    pool_token: str,
    target_type: str = "codex",
    config: Optional[dict] = None,
    proxy: str = "",
    log_cb: Optional[Callable[[str], None]] = None,
    target_count: int = 0,
    upload_only: bool = False,
) -> dict:
    """同步本地与远程：移动根目录中远程已有文件，按目标数补齐上传/下载账号数据
    upload_only=True 时跳过 remote_only 下载，仅上传本地存量（补号场景使用）"""

    def log(msg):
        if log_cb:
            log_cb(msg)

    tokens_dir = os.path.join(_BASE_DIR, "codex_tokens")
    uploaded_dir = os.path.join(tokens_dir, "uploaded")
    os.makedirs(uploaded_dir, exist_ok=True)

    sync_result = get_sync_status(base_url, pool_token, target_type, config, proxy)
    if not sync_result.get("ok"):
        return {"ok": False, "error": sync_result.get("error", "获取同步状态失败"), "moved": 0, "downloaded": 0, "uploaded": 0, "errors": []}

    summary = sync_result.get("summary", {})
    current_remote = summary.get("synced", 0) + summary.get("pending_move", 0) + summary.get("remote_only", 0)
    gap = max(0, target_count - current_remote) if target_count > 0 else None
    log(f"[同步] 远程当前 {current_remote} 个，目标 {target_count}，待上传缺口 {gap if gap is not None else '无限制'}")

    session = _pool_session(proxy)
    headers = {"Authorization": f"Bearer {pool_token}", "Content-Type": "application/json", "Accept": "application/json"}
    base = base_url.rstrip("/")

    moved = 0
    downloaded = 0
    uploaded = 0
    errors = []

    for acc in sync_result.get("accounts", []):
        name = acc["name"]
        status = acc["status"]

        if status == "pending_move":
            # 根目录已在远程 → 仅移到 uploaded/
            src = os.path.join(tokens_dir, f"{name}.json")
            dst = os.path.join(uploaded_dir, f"{name}.json")
            try:
                os.replace(src, dst)
                log(f"[同步] 移动: {name}.json → uploaded/")
                moved += 1
            except Exception as e:
                msg = f"[同步] 移动 {name} 失败: {e}"
                log(msg)
                errors.append(msg)

        elif status in ("pending_upload", "local_only"):
            # 本地有但远程无 → 上传（受目标数限制）
            if gap is not None and uploaded >= gap:
                continue
            fpath = (os.path.join(tokens_dir, f"{name}.json") if status == "pending_upload"
                     else os.path.join(uploaded_dir, f"{name}.json"))
            fname = f"{name}.json"
            try:
                with open(fpath, "rb") as f:
                    file_bytes = f.read()
                upload_headers = {"Authorization": f"Bearer {pool_token}"}
                r = session.post(
                    f"{base}/v0/management/auth-files",
                    files={"file": (fname, file_bytes, "application/json")},
                    headers=upload_headers,
                    timeout=10,
                )
                if r.status_code in (200, 201):
                    log(f"[同步] 上传成功: {name}")
                    uploaded += 1
                    if status == "pending_upload":
                        dst = os.path.join(uploaded_dir, fname)
                        os.replace(fpath, dst)
                elif r.status_code == 409:
                    log(f"[同步] 已存在跳过: {name}")
                    if status == "pending_upload":
                        dst = os.path.join(uploaded_dir, fname)
                        os.replace(fpath, dst)
                else:
                    try:
                        detail = r.json()
                    except Exception:
                        detail = r.text[:200]
                    msg = f"[同步] 上传失败: {name} ({r.status_code}) {detail}"
                    log(msg)
                    errors.append(msg)
            except Exception as e:
                msg = f"[同步] 上传异常: {name} - {e}"
                log(msg)
                errors.append(msg)

        elif status == "remote_only":
            if upload_only:
                continue
            # 远程有本地无 → 下载完整数据
            try:
                resp = session.get(f"{base}/v0/management/auth-files/download",
                                   params={"name": f"{name}.json"},
                                   headers={"Authorization": f"Bearer {pool_token}", "Accept": "application/json"},
                                   timeout=15)
                resp.raise_for_status()
                acc_data = resp.json()
                dst = os.path.join(uploaded_dir, f"{name}.json")
                with open(dst, "w", encoding="utf-8") as f:
                    json.dump(acc_data, f, ensure_ascii=False, indent=2)
                log(f"[同步] 下载: {name} → uploaded/{name}.json")
                downloaded += 1
            except Exception as e:
                msg = f"[同步] 下载 {name} 失败: {e}"
                log(msg)
                errors.append(msg)

    log(f"[同步] 完成：上传 {uploaded} 个，移动 {moved} 个，下载 {downloaded} 个，错误 {len(errors)} 个")
    return {"ok": True, "moved": moved, "downloaded": downloaded, "uploaded": uploaded, "errors": errors}


def run_pool_probe(
    base_url: str,
    token: str,
    target_type: str = "codex",
    proxy: str = "",
    timeout: int = 10,
    log_cb: Optional[Callable[[str], None]] = None,
    progress_cb: Optional[Callable[[Dict[str, int]], None]] = None,
) -> dict:
    """探测账号池，找出 401 失效账号"""

    def log(msg):
        if log_cb:
            log_cb(msg)

    log("[Pool] 获取账号列表...")
    pool_accounts = get_pool_accounts(
        base_url,
        token,
        target_type,
        proxy="",
        timeout=max(timeout, 30),
        max_accounts=POOL_PROBE_MAX_ACCOUNTS,
        page_size=POOL_PROBE_MAX_ACCOUNTS,
    )
    if not pool_accounts.get("ok"):
        err = pool_accounts.get("error", "获取远程账号失败")
        log(f"[Pool] 获取失败: {err}")
        return {"ok": False, "error": str(err), "invalid_401": [], "total": 0, "target": 0}

    target_files = pool_accounts.get("accounts", []) or []
    total = int(pool_accounts.get("total") or len(target_files))
    scanned_total = len(target_files)
    if scanned_total > POOL_PROBE_MAX_ACCOUNTS:
        log(f"[Pool] 账号过多，当前仅抽样检查前 {POOL_PROBE_MAX_ACCOUNTS} 个，避免 CPU 过载")
        target_files = target_files[:POOL_PROBE_MAX_ACCOUNTS]
    log(f"[Pool] {target_type} 账号: {len(target_files)}")

    invalid_401 = []
    probe_lock = threading.Lock()
    checked = 0
    checked_lock = threading.Lock()
    probe_session_local = threading.local()

    def get_probe_session() -> _requests.Session:
        sess = getattr(probe_session_local, "session", None)
        if sess is None:
            sess = _pool_session("", timeout)
            probe_session_local.session = sess
        return sess

    def extract_chatgpt_account_id(item: dict) -> str:
        id_token = item.get("id_token")
        if not isinstance(id_token, dict):
            return ""
        v = id_token.get("chatgpt_account_id")
        return str(v) if v else ""

    def build_probe_payload(auth_index: str, chatgpt_account_id: str) -> dict:
        call_header = {
            "Authorization": "Bearer $TOKEN$",
            "Content-Type": "application/json",
            "User-Agent": "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal",
        }
        if chatgpt_account_id:
            call_header["Chatgpt-Account-Id"] = chatgpt_account_id
        return {
            "authIndex": auth_index,
            "method": "GET",
            "url": "https://chatgpt.com/backend-api/wham/usage",
            "header": call_header,
        }

    def probe_one(f):
        nonlocal checked
        name = f.get("name") or f.get("id", "")
        auth_index = f.get("auth_index")
        if not auth_index:
            log(f"[Pool] 跳过(缺少 auth_index): {name}")
            with checked_lock:
                checked += 1
                if progress_cb:
                    try:
                        progress_cb({
                            "checked": checked,
                            "total": len(target_files),
                            "invalid_count": len(invalid_401),
                        })
                    except Exception:
                        pass
                if checked == 1 or checked % 20 == 0 or checked == len(target_files):
                    log(f"[Pool] 探测进度: {checked}/{len(target_files)}, 401={len(invalid_401)}")
            return
        try:
            payload = build_probe_payload(str(auth_index), extract_chatgpt_account_id(f))
            probe_session = get_probe_session()
            r = probe_session.post(
                f"{base_url.rstrip('/')}/v0/management/api-call",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=payload,
                timeout=timeout,
            )
            r.raise_for_status()
            data = r.json() if r.content else {}
            status_code = data.get("status_code") if isinstance(data, dict) else None
            if status_code == 401:
                with probe_lock:
                    invalid_401.append({"name": name, "status": 401})
                log(f"[Pool] 401: {name}")
            elif status_code is None:
                log(f"[Pool] 探测返回缺少 status_code: {name}")
        except Exception as e:
            log(f"[Pool] 探测异常: {name} - {e}")
        finally:
            with checked_lock:
                checked += 1
                if progress_cb:
                    try:
                        progress_cb({
                            "checked": checked,
                            "total": len(target_files),
                            "invalid_count": len(invalid_401),
                        })
                    except Exception:
                        pass
                if checked == 1 or checked % 20 == 0 or checked == len(target_files):
                    log(f"[Pool] 探测进度: {checked}/{len(target_files)}, 401={len(invalid_401)}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=POOL_PROBE_WORKERS) as ex:
        list(ex.map(probe_one, target_files))

    log(f"[Pool] 探测完成: 401 失效 {len(invalid_401)} 个")
    return {
        "ok": True,
        "total": total,
        "target": scanned_total,
        "scanned": len(target_files),
        "invalid_401": invalid_401,
        "invalid_count": len(invalid_401),
    }


def run_pool_clean(
    base_url: str,
    token: str,
    target_type: str = "codex",
    proxy: str = "",
    timeout: int = 10,
    log_cb: Optional[Callable[[str], None]] = None,
) -> dict:
    """清理 401 失效账号（探测 + 删除）"""

    def log(msg):
        if log_cb:
            log_cb(msg)

    probe_result = run_pool_probe(base_url, token, target_type, proxy, timeout, log_cb)
    if not probe_result.get("ok"):
        return probe_result

    return run_pool_clean_with_probe_result(
        base_url=base_url,
        token=token,
        probe_result=probe_result,
        proxy=proxy,
        timeout=timeout,
        log_cb=log_cb,
    )


def _delete_invalid_accounts(
    base_url: str,
    token: str,
    invalid_401: list,
    proxy: str = "",
    timeout: int = 10,
    log_cb: Optional[Callable[[str], None]] = None,
) -> dict:
    """根据 invalid_401 列表执行删除，返回删除统计"""
    def log(msg):
        if log_cb:
            log_cb(msg)

    if not invalid_401:
        log("[Pool] 无需清理，没有 401 账号")
        return {"deleted": 0, "delete_fail": 0}

    log(f"[Pool] 开始删除 {len(invalid_401)} 个失效账号...")
    base = base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}
    deleted = 0
    delete_fail = 0
    del_lock = threading.Lock()
    delete_session_local = threading.local()

    def get_delete_session() -> _requests.Session:
        sess = getattr(delete_session_local, "session", None)
        if sess is None:
            sess = _pool_session(proxy, timeout)
            delete_session_local.session = sess
        return sess

    def delete_one(item):
        nonlocal deleted, delete_fail
        name = item.get("name", "")
        try:
            delete_session = get_delete_session()
            # CLIProxyAPI 使用查询参数删除文件，而不是 /auth-files/{name}
            r = delete_session.delete(
                f"{base}/v0/management/auth-files",
                headers=headers,
                params={"name": name},
                timeout=timeout,
            )
            with del_lock:
                if r.status_code in (200, 204):
                    deleted += 1
                    log(f"[Pool] 删除成功: {name}")
                    # 同步删除本地副本（根目录和 uploaded/）
                    clean_name = name[:-5] if name.endswith(".json") else name
                    for local_path in [
                        os.path.join(_BASE_DIR, "codex_tokens", f"{clean_name}.json"),
                        os.path.join(_BASE_DIR, "codex_tokens", "uploaded", f"{clean_name}.json"),
                    ]:
                        if os.path.isfile(local_path):
                            try:
                                os.remove(local_path)
                                log(f"[Pool] 本地删除: {clean_name}.json")
                            except Exception as ex:
                                log(f"[Pool] 本地删除失败: {clean_name} - {ex}")
                else:
                    delete_fail += 1
                    detail = (r.text or "").strip()
                    detail = detail[:200] if detail else ""
                    log(f"[Pool] 删除失败: {name} ({r.status_code}){f' {detail}' if detail else ''}")
        except Exception as e:
            with del_lock:
                delete_fail += 1
            log(f"[Pool] 删除异常: {name} - {e}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(delete_one, invalid_401))

    log(f"[Pool] 清理完成: 删除成功={deleted}, 失败={delete_fail}")
    return {"deleted": deleted, "delete_fail": delete_fail}


def run_pool_clean_with_probe_result(
    base_url: str,
    token: str,
    probe_result: Dict[str, Any],
    proxy: str = "",
    timeout: int = 10,
    log_cb: Optional[Callable[[str], None]] = None,
) -> dict:
    """使用现成探测结果清理 401 账号（不重复探测）"""
    if not isinstance(probe_result, dict):
        return {"ok": False, "error": "invalid probe_result", "invalid_401": [], "total": 0, "target": 0}

    invalid_401 = probe_result.get("invalid_401", [])
    if not isinstance(invalid_401, list):
        return {"ok": False, "error": "invalid probe_result.invalid_401", "invalid_401": [], "total": 0, "target": 0}

    delete_result = _delete_invalid_accounts(
        base_url=base_url,
        token=token,
        invalid_401=invalid_401,
        proxy=proxy,
        timeout=timeout,
        log_cb=log_cb,
    )
    return {**probe_result, **delete_result, "ok": True}


def _upload_tokens_to_pool(
    base_url: str,
    pool_token: str,
    config: Optional[dict] = None,
    proxy: str = "",
    log_cb: Optional[Callable[[str], None]] = None,
) -> int:
    """将 token_json_dir 下的 JSON 文件上传到账号池，返回上传成功数"""
    def log(msg):
        if log_cb:
            log_cb(msg)

    token_dir = (config or {}).get("token_json_dir", "codex_tokens")
    if not os.path.isabs(token_dir):
        token_dir = os.path.join(_BASE_DIR, token_dir)

    if not os.path.isdir(token_dir):
        log(f"[Pool] token 目录不存在: {token_dir}")
        return 0

    uploaded = 0
    base = base_url.rstrip("/")
    upload_headers = {"Authorization": f"Bearer {pool_token}"}
    session = _pool_session(proxy, 10)
    for fname in os.listdir(token_dir):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(token_dir, fname)
        try:
            with open(fpath, "rb") as f:
                file_bytes = f.read()
            r = session.post(
                f"{base}/v0/management/auth-files",
                files={"file": (fname, file_bytes, "application/json")},
                headers=upload_headers,
                timeout=10,
            )
            if r.status_code in (200, 201):
                uploaded += 1
                log(f"[Pool] 上传成功: {fname}")
                uploaded_dir = os.path.join(token_dir, "uploaded")
                os.makedirs(uploaded_dir, exist_ok=True)
                os.replace(fpath, os.path.join(uploaded_dir, fname))
            elif r.status_code == 409:
                log(f"[Pool] 已存在跳过: {fname}")
                uploaded_dir = os.path.join(token_dir, "uploaded")
                os.makedirs(uploaded_dir, exist_ok=True)
                os.replace(fpath, os.path.join(uploaded_dir, fname))
            else:
                log(f"[Pool] 上传失败: {fname} ({r.status_code})")
        except Exception as e:
            log(f"[Pool] 上传异常: {fname} - {e}")

    log(f"[Pool] 上传完成: {uploaded} 个 token")
    return uploaded


def run_pool_fill(
    fill_count: int,
    base_url: str,
    pool_token: str,
    stop_event: threading.Event,
    log_cb: Callable[[str], None],
    progress_cb: Callable[[int, int, int], None],
    config: Optional[dict] = None,
    proxy: str = "",
    target_count: int = 0,
    target_type: str = "codex",
) -> dict:
    """纯注册上传：只注册新账号并上传到账号池，不做探测/清理/同步"""

    def log(msg):
        if log_cb:
            log_cb(msg)

    log(f"[Pool] 开始补号: 目标数量={fill_count}")
    log("[Pool] 当前模式: 纯注册上传，不做探测/清理/同步")

    if fill_count == 0:
        return {"success": 0, "fail": 0, "total": 0, "uploaded": 0}

    result = run_batch_register(
        count=fill_count,
        workers=min(3, fill_count),
        proxy=proxy or (config or {}).get("proxy", ""),
        stop_event=stop_event,
        log_cb=log_cb,
        progress_cb=progress_cb,
        config=config,
    )

    registered = result.get("success", 0)
    if registered > 0 and base_url and pool_token:
        log("[Pool] 尝试上传新 token 到账号池...")
        uploaded = _upload_tokens_to_pool(base_url, pool_token, config, proxy, log_cb)
        result["uploaded"] = uploaded
    else:
        result["uploaded"] = 0

    return result


# ============================================================
# 免费代理工具
# ============================================================

_PROXY_SOURCES = [
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
]


def fetch_free_proxies(timeout: int = 10, proxy: str = "") -> List[str]:
    """从公开代理源获取免费代理列表，proxy 用于访问 GitHub（国内环境需要）"""
    session = _requests.Session()
    session.verify = False
    if proxy:
        p = proxy if "://" in proxy else f"http://{proxy}"
        session.proxies = {"http": p, "https": p}

    proxies = set()
    for url in _PROXY_SOURCES:
        try:
            resp = session.get(url, timeout=timeout)
            if resp.status_code == 200:
                for line in resp.text.splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        if ":" in line and not line.startswith("http"):
                            proxies.add(f"http://{line}")
                        elif line.startswith("http"):
                            proxies.add(line)
        except Exception:
            pass
    return sorted(proxies)[:200]  # 最多返回 200 个


def test_proxy(
    proxy: str,
    target_url: str = "https://httpbin.org/ip",
    timeout: int = 5,
) -> dict:
    """测试代理可用性，返回延迟和状态"""
    if not proxy:
        return {"proxy": proxy, "ok": False, "error": "空代理"}

    proxy_url = proxy if "://" in proxy else f"http://{proxy}"
    proxies = {"http": proxy_url, "https": proxy_url}

    start = time.time()
    try:
        resp = _requests.get(target_url, proxies=proxies, timeout=timeout, verify=False)
        elapsed = int((time.time() - start) * 1000)
        if resp.status_code == 200:
            return {"proxy": proxy, "ok": True, "latency_ms": elapsed, "status": resp.status_code}
        return {"proxy": proxy, "ok": False, "latency_ms": elapsed, "status": resp.status_code}
    except _requests.exceptions.Timeout:
        return {"proxy": proxy, "ok": False, "error": "超时", "latency_ms": timeout * 1000}
    except Exception as e:
        return {"proxy": proxy, "ok": False, "error": str(e)[:80]}


def test_proxies_concurrent(
    proxy_list: List[str],
    target_url: str = "https://httpbin.org/ip",
    timeout: int = 5,
    max_workers: int = 20,
) -> List[dict]:
    """并发测试多个代理"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        results = list(ex.map(lambda p: test_proxy(p, target_url, timeout), proxy_list))
    return results


# ============================================================
# 配置文件读写
# ============================================================

def load_config() -> dict:
    """读取 config.json"""
    cfg_path = os.path.join(_BASE_DIR, "config.json")
    defaults = {
        "total_accounts": 3,
        "duckmail_api_base": "https://api.duckmail.sbs",
        "duckmail_bearer": "",
        "proxy": "",
        "output_file": "registered_accounts.txt",
        "enable_oauth": True,
        "oauth_required": True,
        "oauth_issuer": "https://auth.openai.com",
        "oauth_client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
        "oauth_redirect_uri": "http://localhost:1455/auth/callback",
        "ak_file": "ak.txt",
        "rk_file": "rk.txt",
        "token_json_dir": "codex_tokens",
        "pool": {
            "base_url": "",
            "token": "",
            "target_type": "codex",
            "min_candidates": 100,
        },
    }
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
                defaults.update(file_cfg)
        except Exception:
            pass
    return defaults


def save_config(config: dict) -> bool:
    """保存 config.json"""
    cfg_path = os.path.join(_BASE_DIR, "config.json")
    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


# ============================================================
# 结果文件读取
# ============================================================

def read_registered_accounts(config: Optional[dict] = None) -> List[dict]:
    """解析 registered_accounts.txt"""
    output_file = (config or {}).get("output_file", "registered_accounts.txt")
    if not os.path.isabs(output_file):
        output_file = os.path.join(_BASE_DIR, output_file)

    accounts = []
    if not os.path.exists(output_file):
        return accounts

    try:
        with open(output_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split("----")
                if len(parts) >= 2:
                    accounts.append({
                        "email": parts[0],
                        "password": parts[1],
                        "email_password": parts[2] if len(parts) > 2 else "",
                        "oauth": parts[3] if len(parts) > 3 else "",
                    })
    except Exception:
        pass
    return accounts


def read_token_file(filename: str, config: Optional[dict] = None) -> str:
    """读取 ak.txt 或 rk.txt 内容"""
    key = "ak_file" if "ak" in filename.lower() else "rk_file"
    path = (config or {}).get(key, filename)
    if not os.path.isabs(path):
        path = os.path.join(_BASE_DIR, path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def list_codex_tokens(config: Optional[dict] = None) -> List[dict]:
    """列出 codex_tokens/ 目录下的所有 JSON token"""
    token_dir = (config or {}).get("token_json_dir", "codex_tokens")
    if not os.path.isabs(token_dir):
        token_dir = os.path.join(_BASE_DIR, token_dir)

    tokens = []
    if not os.path.isdir(token_dir):
        return tokens

    for fname in os.listdir(token_dir):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(token_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
                tokens.append(data)
        except Exception:
            pass

    tokens.sort(key=lambda x: x.get("last_refresh", ""), reverse=True)
    return tokens


# ============================================================
# 代理池单例
# ============================================================

class _ProxyPool:
    """免费代理池单例，存储最近测试结果，提供最优代理"""

    def __init__(self):
        self._proxies: List[dict] = []  # [{proxy, ok, latency_ms}, ...]
        self._lock = threading.Lock()

    def update(self, results: List[dict]):
        """更新测试结果，按延迟排序（可用的排前面）"""
        with self._lock:
            working = sorted(
                [r for r in results if r.get("ok")],
                key=lambda x: x.get("latency_ms", 99999),
            )
            failed = [r for r in results if not r.get("ok")]
            self._proxies = working + failed

    def get_best(self, fallback: str = "") -> str:
        """返回延迟最低的可用免费代理，若无则返回 fallback"""
        with self._lock:
            working = [p for p in self._proxies if p.get("ok")]
            if working:
                return working[0]["proxy"]
        return fallback

    def get_all(self) -> List[dict]:
        """返回所有代理的副本"""
        with self._lock:
            return list(self._proxies)


_proxy_pool = _ProxyPool()


# ============================================================
# 号池自动维护周期
# ============================================================

def run_pool_maintain_cycle(
    base_url: str,
    token: str,
    target_type: str,
    target_count: int,
    stop_event: threading.Event,
    log_cb: Callable[[str], None],
    config: Optional[dict] = None,
    proxy: str = "",
) -> dict:
    """
    号池维护一次完整周期：
    1. 获取当前池状态
    2. 清理 401 失效账号
    3. 计算缺口，若缺口 > 0 则注册新账号并上传到池
    4. 返回统计结果
    """
    def log(msg):
        if log_cb:
            log_cb(msg)

    log(f"[Daemon] 开始维护周期: 目标类型={target_type}, 目标数量={target_count}")

    # 1. 获取当前状态
    status = get_pool_status(base_url, token, target_type, proxy)
    if not status.get("ok"):
        log(f"[Daemon] 获取池状态失败: {status.get('error')}")
        return {"ok": False, "error": status.get("error")}

    log(f"[Daemon] 当前 {target_type} 账号数: {status['target']}")

    # 2. 先清理已被后端明确标记无效/禁用的账号
    backend_invalid = list_invalid_pool_accounts(
        base_url=base_url,
        token=token,
        target_type=target_type,
        timeout=POOL_PROBE_TIMEOUT,
    )
    backend_deleted = 0
    if backend_invalid.get("ok") and backend_invalid.get("invalid_count", 0) > 0:
        log(f"[Daemon] 发现后端已标记无效账号 {backend_invalid['invalid_count']} 个，先执行物理删除...")
        backend_delete_result = _delete_invalid_accounts(
            base_url=base_url,
            token=token,
            invalid_401=backend_invalid.get("invalid", []),
            proxy=proxy,
            timeout=POOL_PROBE_TIMEOUT,
            log_cb=log_cb,
        )
        backend_deleted = int(backend_delete_result.get("deleted", 0))
        log(f"[Daemon] 后端无效账号清理完成: 删除 {backend_deleted} 个")
    elif not backend_invalid.get("ok"):
        log(f"[Daemon] 后端无效账号扫描失败: {backend_invalid.get('error')}")

    # 3. 再清理 401 账号
    clean_result = run_pool_clean(base_url, token, target_type, proxy, timeout=POOL_PROBE_TIMEOUT, log_cb=log_cb)
    if not clean_result.get("ok"):
        log(f"[Daemon] 清理失败，跳过补号: {clean_result.get('error')}")
        return {"ok": False, "error": clean_result.get("error")}

    deleted = int(clean_result.get("deleted", 0)) + backend_deleted
    log(f"[Daemon] 清理完成: 删除 {deleted} 个失效账号")

    # 4. 重新获取有效数量
    status_after = get_pool_status(base_url, token, target_type, proxy)
    valid_count = status_after.get("target", 0) if status_after.get("ok") else (status["target"] - deleted)
    gap = target_count - valid_count

    log(f"[Daemon] 清理后有效账号: {valid_count}, 目标: {target_count}, 缺口: {gap}")

    # 5. 若有缺口则先同步本地存量，再注册补充
    registered = 0
    uploaded = 0
    if gap > 0:
        if stop_event and stop_event.is_set():
            log("[Daemon] 任务已停止，跳过补号")
        else:
            log("[Daemon] 先同步本地存量到远程...")
            sync_r = sync_local_remote(base_url, token, target_type, config, proxy, log_cb, target_count)
            pre_uploaded = sync_r.get("uploaded", 0)
            if pre_uploaded > 0:
                status_synced = get_pool_status(base_url, token, target_type, proxy)
                valid_count = status_synced.get("target", valid_count) if status_synced.get("ok") else valid_count + pre_uploaded
                gap = max(0, target_count - valid_count)
                log(f"[Daemon] 存量上传 {pre_uploaded} 个，同步后有效账号: {valid_count}，剩余缺口: {gap}")

            if gap > 0:
                log(f"[Daemon] 开始注册 {gap} 个账号...")
                reg_result = run_batch_register(
                    count=gap,
                    workers=min(3, gap),
                    proxy=proxy,
                    stop_event=stop_event,
                    log_cb=log_cb,
                    progress_cb=lambda s, f, t: None,
                    config=config,
                )
                registered = reg_result.get("success", 0)
                log(f"[Daemon] 注册完成: 成功={registered}, 失败={reg_result.get('fail', 0)}")

                if registered > 0 and base_url and token:
                    log("[Daemon] 上传新 token 到账号池...")
                    uploaded = _upload_tokens_to_pool(base_url, token, config, proxy, log_cb)
            else:
                log("[Daemon] 存量补齐，无需注册新账号")
    else:
        log("[Daemon] 账号数量充足，无需补号")

    log(f"[Daemon] 维护周期完成: 删除={deleted}, 注册={registered}, 上传={uploaded}")
    return {
        "ok": True,
        "valid_before": status["target"],
        "valid_after": valid_count,
        "deleted": deleted,
        "registered": registered,
        "uploaded": uploaded,
        "gap": gap,
    }
