"""项目服务入口：全球南方稿件交换 HTTP API。"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from service import db, store


class ExchangeApp:
    """应用状态：数据库连接与串行化访问锁。"""

    def __init__(self, db_path):
        self.conn = db.connect(db_path)
        self.lock = threading.RLock()

    def execute(self, fn, **kwargs):
        with self.lock:
            return fn(self.conn, **kwargs)

    def drain_outbox(self):
        """发出待发队列中的分发；重启后再次调用即可继续处理。"""
        return self.execute(store.drain_outbox)

    def close(self):
        self.conn.close()


# ---------------------------------------------------------------- 路由处理

def _need(body, field):
    value = body.get(field)
    if value is None:
        raise store.StoreError(f"缺少必填字段: {field}")
    return value


def _create_work(app, match, query, body):
    work = app.execute(
        store.create_work,
        work_id=_need(body, "work_id"), kind=_need(body, "kind"),
        language=_need(body, "language"), parent_id=body.get("parent_id"),
        body=_need(body, "body"), context_note=body.get("context_note", ""),
        byline=_need(body, "byline"), editor_id=_need(body, "editor_id"),
        event_time=_need(body, "event_time"), partner_id=body.get("partner_id"))
    return 201, work


def _get_work(app, match, query, body):
    work_id = match.group(1)
    work = app.execute(store.get_work, work_id=work_id)
    head = app.execute(store.get_version, work_id=work_id,
                       version_no=work["head_version"])
    head = dict(head)
    head["parents"] = json.loads(head["parents"])
    head["is_merge"] = bool(head["is_merge"])
    return 200, {"work": work, "head": head}


def _add_version(app, match, query, body):
    result = app.execute(
        store.add_version, work_id=match.group(1),
        editor_id=_need(body, "editor_id"), base_version=_need(body, "base_version"),
        body=_need(body, "body"), context_note=body.get("context_note"),
        byline=body.get("byline"), event_time=_need(body, "event_time"))
    return 201, result


def _merge(app, match, query, body):
    result = app.execute(
        store.merge_versions, work_id=match.group(1),
        editor_id=_need(body, "editor_id"),
        source_version=_need(body, "source_version"), body=_need(body, "body"),
        context_note=body.get("context_note"), byline=body.get("byline"),
        event_time=_need(body, "event_time"))
    return 201, result


def _verify(app, match, query, body):
    version = query.get("version", [None])[0]
    if version is not None:
        try:
            version = int(version)
        except ValueError:
            raise store.StoreError(f"version 参数不是整数: {version!r}")
    result = app.execute(
        store.verify_work, work_id=match.group(1), version_no=version,
        content_hash=query.get("hash", [None])[0])
    return 200, result


def _dispatch_work(app, match, query, body):
    deliveries = app.execute(
        store.create_deliveries, work_id=match.group(1),
        partner_ids=_need(body, "partner_ids"),
        event_time=_need(body, "event_time"))
    return 201, {"deliveries": deliveries}


def _create_license(app, match, query, body):
    lic = app.execute(
        store.create_license, license_id=_need(body, "license_id"),
        work_id=_need(body, "work_id"), partner_id=_need(body, "partner_id"),
        regions=body.get("regions"), channels=body.get("channels"),
        valid_from=_need(body, "valid_from"), valid_until=_need(body, "valid_until"),
        permissions=body.get("permissions"),
        attribution_text=body.get("attribution_text", ""),
        event_time=_need(body, "event_time"))
    return 201, lic


def _get_license(app, match, query, body):
    return 200, app.execute(store.get_license, license_id=match.group(1))


def _revoke_license(app, match, query, body):
    lic = app.execute(store.revoke_license, license_id=match.group(1),
                      event_time=_need(body, "event_time"))
    return 200, lic


def _check_license(app, match, query, body):
    return 200, app.execute(
        store.check_license, license_id=match.group(1),
        region=body.get("region"), channel=body.get("channel"),
        at=_need(body, "at"))


def _record_voucher(app, match, query, body):
    voucher = app.execute(
        store.record_voucher, voucher_id=_need(body, "voucher_id"),
        license_id=match.group(1), version_no=_need(body, "version_no"),
        region=_need(body, "region"), channel=_need(body, "channel"),
        published_at=_need(body, "published_at"),
        event_time=_need(body, "event_time"))
    return 201, voucher


def _list_vouchers(app, match, query, body):
    vouchers = app.execute(store.list_vouchers, license_id=match.group(1))
    return 200, {"vouchers": vouchers}


def _get_delivery(app, match, query, body):
    return 200, app.execute(store.get_delivery, delivery_id=match.group(1))


def _record_receipt(app, match, query, body):
    result = app.execute(
        store.record_receipt, delivery_id=match.group(1),
        receipt_key=_need(body, "receipt_key"),
        event_time=_need(body, "event_time"))
    return (200 if result["duplicate"] else 201), result


def _add_attachment(app, match, query, body):
    try:
        content = base64.b64decode(_need(body, "content_b64"), validate=True)
    except ValueError:
        raise store.StoreError("content_b64 不是合法的 base64 编码")
    result = app.execute(
        store.add_attachment, attachment_id=_need(body, "attachment_id"),
        work_id=_need(body, "work_id"), name=_need(body, "name"), content=content,
        sensitive=bool(body.get("sensitive", False)),
        allowed_partners=body.get("allowed_partners"),
        event_time=_need(body, "event_time"))
    return 201, result


def _get_attachment(app, match, query, body):
    result = app.execute(
        store.get_attachment, attachment_id=match.group(1),
        partner_id=query.get("partner_id", [None])[0])
    result = dict(result)
    result["content_b64"] = base64.b64encode(result.pop("content")).decode("ascii")
    return 200, result


def _list_audit(app, match, query, body):
    entries = app.execute(
        store.list_audit, entity=query.get("entity", [None])[0],
        entity_id=query.get("entity_id", [None])[0])
    return 200, {"entries": entries}


_ROUTES = [
    ("POST", re.compile(r"^/works$"), _create_work),
    ("GET", re.compile(r"^/works/([^/]+)$"), _get_work),
    ("POST", re.compile(r"^/works/([^/]+)/versions$"), _add_version),
    ("POST", re.compile(r"^/works/([^/]+)/merges$"), _merge),
    ("GET", re.compile(r"^/works/([^/]+)/verification$"), _verify),
    ("POST", re.compile(r"^/works/([^/]+)/dispatch$"), _dispatch_work),
    ("POST", re.compile(r"^/licenses$"), _create_license),
    ("GET", re.compile(r"^/licenses/([^/]+)$"), _get_license),
    ("POST", re.compile(r"^/licenses/([^/]+)/revoke$"), _revoke_license),
    ("POST", re.compile(r"^/licenses/([^/]+)/check$"), _check_license),
    ("POST", re.compile(r"^/licenses/([^/]+)/vouchers$"), _record_voucher),
    ("GET", re.compile(r"^/licenses/([^/]+)/vouchers$"), _list_vouchers),
    ("GET", re.compile(r"^/deliveries/([^/]+)$"), _get_delivery),
    ("POST", re.compile(r"^/deliveries/([^/]+)/receipts$"), _record_receipt),
    ("POST", re.compile(r"^/attachments$"), _add_attachment),
    ("GET", re.compile(r"^/attachments/([^/]+)$"), _get_attachment),
    ("GET", re.compile(r"^/audit$"), _list_audit),
]


def _route(app, method, path, query, body):
    for route_method, pattern, fn in _ROUTES:
        if route_method != method:
            continue
        match = pattern.match(path)
        if match:
            return fn(app, match, query, body)
    return None


# ---------------------------------------------------------------- HTTP 层

class Handler(BaseHTTPRequestHandler):
    """稿件交换 API 与基础健康检查。"""

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    @property
    def app(self):
        app = getattr(self.server, "app", None)
        if app is None:
            app = _default_app()
            self.server.app = app
        return app

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        try:
            if method == "GET" and parsed.path == "/health":
                self._json(200, {"status": "ok"})
                return
            body = self._read_json() if method == "POST" else {}
            result = _route(self.app, method, parsed.path,
                            parse_qs(parsed.query), body)
            if result is None:
                self._json(404, {"error": "not_found", "message": "资源不存在"})
                return
            status, payload = result
            self._json(status, payload)
        except store.StoreError as exc:
            self._json(exc.status, {"error": exc.code, "message": str(exc)})

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise store.StoreError("请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise store.StoreError("请求体必须是 JSON 对象")
        return data

    def _json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


_default = None
_default_lock = threading.Lock()


def _default_app():
    """直接构造 Handler 时使用的惰性内存应用（兼容基础健康检查用法）。"""
    global _default
    with _default_lock:
        if _default is None:
            _default = ExchangeApp(":memory:")
    return _default


def make_server(host, port, db_path):
    app = ExchangeApp(db_path)
    server = ThreadingHTTPServer((host, port), Handler)
    server.app = app
    return server


def _outbox_loop(app, stop, interval):
    while not stop.is_set():
        app.drain_outbox()
        stop.wait(interval)


def run(host="127.0.0.1", port=8000, db_path=None, outbox_interval=1.0):
    """启动本地服务；待发队列持久化于数据库，重启后由后台线程继续发出。"""
    db_path = db_path or os.environ.get("EXCHANGE_DB") or "exchange.db"
    server = make_server(host, port, db_path)
    stop = threading.Event()
    worker = threading.Thread(target=_outbox_loop,
                              args=(server.app, stop, outbox_interval),
                              daemon=True)
    worker.start()
    try:
        server.serve_forever()
    finally:
        stop.set()
        worker.join(timeout=2)
        server.app.close()


if __name__ == "__main__":
    run()
