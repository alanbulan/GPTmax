#!/usr/bin/env python3
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


UPSTREAM_BASE = "http://127.0.0.1:8317/v0/management"
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 20081
CACHE_TTL_SECONDS = 60
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 5000

_cache_lock = threading.Lock()
_cache_cond = threading.Condition(_cache_lock)
_cache = {
    "auth_header": "",
    "timestamp": 0.0,
    "payload": None,
    "fetching": False,
}


def _normalize_type(value: str) -> str:
    return (value or "").strip().lower()


def _parse_positive_int(raw: str, default: int, maximum: int) -> int:
    try:
        value = int((raw or "").strip())
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return min(value, maximum)


def _build_search_text(entry: dict) -> str:
    parts = [
        str(entry.get("name", "")),
        str(entry.get("type", "")),
        str(entry.get("provider", "")),
        str(entry.get("email", "")),
    ]
    return "\n".join(part.strip().lower() for part in parts if part)


def _with_cors(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
    handler.send_header("Access-Control-Allow-Methods", "GET, DELETE, OPTIONS")


def _write_json(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    _with_cors(handler)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _upstream_request(method: str, path: str, auth_header: str, timeout: int = 120) -> tuple[int, bytes]:
    req = urllib.request.Request(
        f"{UPSTREAM_BASE}{path}",
        method=method,
        headers={"Authorization": auth_header} if auth_header else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()


def _get_full_auth_files(auth_header: str) -> tuple[int, dict]:
    now = time.time()
    with _cache_cond:
        if (
            _cache["payload"] is not None
            and _cache["auth_header"] == auth_header
            and now - float(_cache["timestamp"]) < CACHE_TTL_SECONDS
        ):
            return 200, _cache["payload"]

        while _cache["fetching"] and _cache["auth_header"] == auth_header:
            _cache_cond.wait(timeout=30)
            now = time.time()
            if (
                _cache["payload"] is not None
                and _cache["auth_header"] == auth_header
                and now - float(_cache["timestamp"]) < CACHE_TTL_SECONDS
            ):
                return 200, _cache["payload"]

        _cache["fetching"] = True
        _cache["auth_header"] = auth_header

    status, body = _upstream_request("GET", "/auth-files", auth_header)
    if status != 200:
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            payload = {"error": body.decode("utf-8", errors="replace")}
        with _cache_cond:
            _cache["fetching"] = False
            _cache_cond.notify_all()
        return status, payload

    payload = json.loads(body.decode("utf-8"))
    with _cache_cond:
        _cache["auth_header"] = auth_header
        _cache["timestamp"] = now
        _cache["payload"] = payload
        _cache["fetching"] = False
        _cache_cond.notify_all()
    return 200, payload


def _clear_cache() -> None:
    with _cache_cond:
        _cache["auth_header"] = ""
        _cache["timestamp"] = 0.0
        _cache["payload"] = None
        _cache["fetching"] = False
        _cache_cond.notify_all()


class AuthFilesProxyHandler(BaseHTTPRequestHandler):
    server_version = "AuthFilesProxy/1.0"

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        _with_cors(self)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            _write_json(self, 200, {"status": "ok"})
            return
        if parsed.path != "/auth-files":
            _write_json(self, 404, {"error": "not found"})
            return

        auth_header = self.headers.get("Authorization", "").strip()
        if not auth_header:
            _write_json(self, 401, {"error": "missing management key"})
            return

        status, payload = _get_full_auth_files(auth_header)
        if status != 200:
            _write_json(self, status, payload if isinstance(payload, dict) else {"error": "upstream failed"})
            return

        params = urllib.parse.parse_qs(parsed.query)
        page = _parse_positive_int(params.get("page", ["1"])[0], 1, 1000000)
        page_size = _parse_positive_int(params.get("page_size", [str(DEFAULT_PAGE_SIZE)])[0], DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE)
        type_filter = _normalize_type(params.get("type", [""])[0])
        search = params.get("search", [""])[0].strip().lower()

        raw_files = payload.get("files")
        files = raw_files if isinstance(raw_files, list) else []

        type_counts: dict[str, int] = {"all": 0}
        normalized = []
        for entry in files:
            if not isinstance(entry, dict):
                continue
            entry_type = _normalize_type(str(entry.get("type") or entry.get("provider") or "unknown")) or "unknown"
            type_counts["all"] += 1
            type_counts[entry_type] = type_counts.get(entry_type, 0) + 1
            normalized.append((entry, entry_type, _build_search_text(entry)))

        filtered = []
        for entry, entry_type, search_text in normalized:
            if type_filter and entry_type != type_filter:
                continue
            if search and search not in search_text:
                continue
            filtered.append(entry)

        filtered.sort(key=lambda item: str(item.get("name", "")).strip().lower())
        total = len(filtered)
        total_pages = max(1, (total + page_size - 1) // page_size)
        if page > total_pages:
            page = total_pages
        start = (page - 1) * page_size
        end = start + page_size
        page_items = filtered[start:end]

        _write_json(
            self,
            200,
            {
                "files": page_items,
                "total": total,
                "all_total": type_counts["all"],
                "type_counts": type_counts,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
            },
        )

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/auth-files":
            _write_json(self, 404, {"error": "not found"})
            return

        auth_header = self.headers.get("Authorization", "").strip()
        if not auth_header:
            _write_json(self, 401, {"error": "missing management key"})
            return

        params = urllib.parse.parse_qs(parsed.query)
        delete_all = params.get("all", ["false"])[0].lower() in {"1", "true", "*"}
        type_filter = _normalize_type(params.get("type", [""])[0])
        search = params.get("search", [""])[0].strip().lower()

        if not delete_all or (not type_filter and not search):
            status, body = _upstream_request("DELETE", f"/auth-files?{parsed.query}", auth_header)
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                payload = {"error": body.decode("utf-8", errors="replace")}
            if status == 200:
                _clear_cache()
            _write_json(self, status, payload if isinstance(payload, dict) else {"error": "upstream failed"})
            return

        status, payload = _get_full_auth_files(auth_header)
        if status != 200:
            _write_json(self, status, payload if isinstance(payload, dict) else {"error": "upstream failed"})
            return

        raw_files = payload.get("files")
        files = raw_files if isinstance(raw_files, list) else []
        deleted = 0
        for entry in files:
            if not isinstance(entry, dict):
                continue
            entry_type = _normalize_type(str(entry.get("type") or entry.get("provider") or "unknown")) or "unknown"
            if type_filter and entry_type != type_filter:
                continue
            if search and search not in _build_search_text(entry):
                continue
            name = str(entry.get("name", "")).strip()
            if not name:
                continue
            query = urllib.parse.urlencode({"name": name})
            status, _ = _upstream_request("DELETE", f"/auth-files?{query}", auth_header, timeout=30)
            if status == 200:
                deleted += 1

        _clear_cache()
        _write_json(self, 200, {"status": "ok", "deleted": deleted})

    def log_message(self, fmt: str, *args) -> None:
        message = "%s - - [%s] %s\n" % (
            self.address_string(),
            self.log_date_time_string(),
            fmt % args,
        )
        print(message, end="")


if __name__ == "__main__":
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), AuthFilesProxyHandler)
    print(f"auth-files proxy listening on {LISTEN_HOST}:{LISTEN_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
