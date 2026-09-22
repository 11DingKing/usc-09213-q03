"""项目服务入口：稿件交换 HTTP API。

数据目录由环境变量 SERVICE_DATA_DIR 指定（默认 ./.data）。所有状态变化落为
只追加事件日志，服务重启后自动重放重建状态，并续发待发投递队列。

路由一览（POST/PUT 均为 JSON；附件内容上传为字节流，用 X-Partner-Id 头）：

  GET  /health
  GET  /audit/verify
  POST /v1/originals|translations|excerpts|revisions|merges
  GET  /v1/artifacts/{id}
  POST /v1/artifacts/{id}/withdraw
  GET  /v1/artifacts/{id}/provenance
  GET  /v1/artifacts/{id}/rights?partner_id&region&channel&at
  POST /v1/artifacts/{id}/attachments           （附件元数据）
  GET  /v1/artifacts/{id}/attachments?partner_id
  PUT  /v1/artifacts/{id}/attachments/{att}/content
  GET  /v1/artifacts/{id}/attachments/{att}/content?partner_id
  GET  /v1/works/{id}
  POST /v1/grants
  POST /v1/grants/{id}/revoke
  POST /v1/deliveries
  POST /v1/queue/drain
  POST /v1/deliveries/{id}/ack
  GET  /v1/deliveries/{id}/receipt
"""
from __future__ import annotations

import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .engine import ExchangeService
from .models import DomainError, WILDCARD
from .store import EventStore


def create_service(data_dir: str | None = None) -> ExchangeService:
    data_dir = data_dir or os.environ.get("SERVICE_DATA_DIR", "./.data")
    return ExchangeService(EventStore(data_dir))


class Handler(BaseHTTPRequestHandler):
    service: ExchangeService  # 由工厂注入到子类

    # ---- 请求工具 ---------------------------------------------------------

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise DomainError(f"请求体不是合法 JSON: {exc}")
        if not isinstance(data, dict):
            raise DomainError("请求体必须是 JSON 对象")
        return data

    def _send(self, status: int, body) -> None:
        data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, data: bytes, media_type: str, filename: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Disposition",
                         f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---- 分发 -------------------------------------------------------------

    def _handle(self, method: str) -> None:
        try:
            parsed = urllib.parse.urlsplit(self.path)
            parts = [urllib.parse.unquote(x)
                     for x in parsed.path.strip("/").split("/") if x]
            query = {k: v[-1]
                     for k, v in urllib.parse.parse_qs(parsed.query).items()}
            body = self._route(method, parts, query)
            if body is not None:
                status, payload = body
                self._send(status, payload)
        except DomainError as exc:
            self._send(exc.status, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": f"内部错误: {exc}"})

    def _route(self, method: str, parts: list[str], query: dict):
        s = self.service
        n = len(parts)
        p = parts

        if method == "GET" and p == ["health"]:
            return 200, {"status": "ok"}
        if method == "GET" and p == ["audit", "verify"]:
            return 200, s.store.verify_chain()

        if method == "POST" and n == 2 and p[0] == "v1":
            creators = {
                "originals": s.register_original,
                "translations": s.translate,
                "excerpts": s.excerpt,
                "revisions": s.revise,
                "merges": s.merge_heads,
            }
            fn = creators.get(p[1])
            if fn:
                return 201, fn(self._read_json())

        if method == "GET" and n == 3 and p[:2] == ["v1", "artifacts"]:
            return 200, s.artifact_view(p[2])
        if method == "GET" and n == 3 and p[:2] == ["v1", "works"]:
            return 200, s.work_view(p[2])

        if method == "POST" and n == 4 and p[:2] == ["v1", "artifacts"]:
            if p[3] == "withdraw":
                return 200, s.withdraw(p[2], self._read_json())
            if p[3] == "attachments":
                return 201, s.register_attachment(p[2], self._read_json())
        if method == "GET" and n == 4 and p[:2] == ["v1", "artifacts"]:
            if p[3] == "provenance":
                return 200, s.provenance(p[2])
            if p[3] == "rights":
                return 200, s.rights_report(
                    p[2], query.get("partner_id", ""),
                    query.get("region", WILDCARD),
                    query.get("channel", WILDCARD),
                    query.get("at"),
                )
            if p[3] == "attachments":
                return 200, s.list_attachments(p[2],
                                               query.get("partner_id", ""))

        if method in ("PUT", "GET") and n == 6 and p[:2] == ["v1", "artifacts"] \
                and p[3] == "attachments" and p[5] == "content":
            if method == "PUT":
                partner = self.headers.get("X-Partner-Id", "")
                length = int(self.headers.get("Content-Length", 0))
                return 200, s.store_attachment_bytes(
                    p[2], p[4], partner, self.rfile.read(length))
            meta, data = s.read_attachment_bytes(
                p[2], p[4], query.get("partner_id", ""))
            self._send_bytes(data, meta["media_type"], meta["filename"])
            return None

        if method == "POST" and p == ["v1", "grants"]:
            return 201, s.issue_grant(self._read_json())
        if method == "POST" and n == 4 and p[:2] == ["v1", "grants"] \
                and p[3] == "revoke":
            return 200, s.revoke_grant(p[2], self._read_json())

        if method == "POST" and p == ["v1", "deliveries"]:
            return 201, s.enqueue_delivery(self._read_json())
        if method == "POST" and p == ["v1", "queue", "drain"]:
            return 200, s.drain_queue()
        if method == "POST" and n == 4 and p[:2] == ["v1", "deliveries"] \
                and p[3] == "ack":
            return 200, s.acknowledge(p[2], self._read_json())
        if method == "GET" and n == 4 and p[:2] == ["v1", "deliveries"] \
                and p[3] == "receipt":
            return 200, s.get_receipt(p[2])

        raise DomainError("未找到", 404)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def log_message(self, format, *args):
        return


def make_handler(service: ExchangeService) -> type[BaseHTTPRequestHandler]:
    return type("BoundHandler", (Handler,), {"service": service})


def run(host: str = "127.0.0.1", port: int = 8000):
    service = create_service()
    ThreadingHTTPServer((host, port), make_handler(service)).serve_forever()


if __name__ == "__main__":
    run()
