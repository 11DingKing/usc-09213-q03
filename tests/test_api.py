"""HTTP API 端到端测试。"""
import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from service.main import create_service, make_handler


def iso(y, m=1, d=1):
    return f"{y:04d}-{m:02d}-{d:02d}T00:00:00Z"


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="api-")
        self.service = create_service(self.tmp)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                          make_handler(self.service))
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def call(self, method, path, body=None, raw=None, headers=None):
        conn = HTTPConnection("127.0.0.1", self.port)
        headers = dict(headers or {})
        if raw is not None:
            data = raw
        elif body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            data = None
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        payload = resp.read()
        ctype = resp.getheader("Content-Type", "")
        parsed = json.loads(payload) if "json" in ctype else payload
        conn.close()
        return resp.status, parsed

    def test_full_exchange_lifecycle(self):
        # 原稿
        status, art = self.call("POST", "/v1/originals", {
            "artifact_id": "a1", "partner_id": "pA",
            "event_time": iso(2026, 1, 1),
            "title": "发展故事", "language": "zh",
            "body": "第一段\n第二段\n第三段\n",
            "authors": [{"name": "甲"}], "context_note": "语境",
        })
        self.assertEqual(status, 201)
        self.assertTrue(art["is_current"])

        # 授权
        status, grant = self.call("POST", "/v1/grants", {
            "grant_id": "g1", "artifact_id": "a1", "partner_id": "pA",
            "grantee_partner_id": "pB",
            "actions": ["publish", "translate", "edit"],
            "regions": ["BR"], "channels": ["web"],
            "valid_from": iso(2026, 1, 1), "valid_until": iso(2027, 1, 1),
            "must_preserve": ["署名"], "event_time": iso(2026, 1, 1),
        })
        self.assertEqual(status, 201)

        # 译稿
        status, tr = self.call("POST", "/v1/translations", {
            "artifact_id": "a1-pt", "parent_id": "a1", "partner_id": "pB",
            "event_time": iso(2026, 2, 1),
            "body": "um\ndois\ntrês\n", "language": "pt",
        })
        self.assertEqual(status, 201)

        # 核验来源
        status, prov = self.call("GET", "/v1/artifacts/a1-pt/provenance")
        self.assertEqual(status, 200)
        self.assertEqual(prov["originals"][0]["artifact_id"], "a1")

        # 核验当前权利与署名义务
        status, rights = self.call(
            "GET", "/v1/artifacts/a1-pt/rights"
            "?partner_id=pB&region=BR&channel=web&at=" + iso(2026, 3, 1))
        self.assertEqual(status, 200)
        self.assertTrue(rights["permitted"])
        self.assertEqual(
            [a["name"] for a in rights["attribution_duty"]["must_attribute_all"]],
            ["甲"],
        )

        # 投递
        status, delivery = self.call("POST", "/v1/deliveries", {
            "delivery_id": "d1", "artifact_id": "a1-pt",
            "partner_id": "pB", "region": "BR", "channel": "web",
            "event_time": iso(2026, 3, 1),
        })
        self.assertEqual(status, 201)
        self.assertEqual(delivery["status"], "sent")

        # 接收方核验收据
        status, receipt = self.call("GET", "/v1/deliveries/d1/receipt")
        self.assertEqual(status, 200)
        self.assertTrue(receipt["currently_usable"])
        self.assertTrue(receipt["chain"]["ok"])
        self.assertTrue(receipt["delivered_version_is_current"])

        # 接收方回执（重复两次，均 200 且第二次幂等）
        s1, ack1 = self.call("POST", "/v1/deliveries/d1/ack",
                             {"partner_id": "pB", "event_time": iso(2026, 3, 2)})
        s2, ack2 = self.call("POST", "/v1/deliveries/d1/ack",
                             {"partner_id": "pB", "event_time": iso(2026, 3, 3)})
        self.assertEqual((s1, s2), (200, 200))
        self.assertFalse(ack1["idempotent"])
        self.assertTrue(ack2["idempotent"])

    def test_revocation_and_audit_over_http(self):
        self.call("POST", "/v1/originals", {
            "artifact_id": "a1", "partner_id": "pA",
            "event_time": iso(2026, 1, 1), "body": "正文\n",
        })
        self.call("POST", "/v1/grants", {
            "grant_id": "g1", "artifact_id": "a1", "partner_id": "pA",
            "grantee_partner_id": "pB", "actions": ["publish"],
            "regions": ["*"], "channels": ["*"],
            "valid_from": iso(2026, 1, 1), "event_time": iso(2026, 1, 1),
        })
        s, d = self.call("POST", "/v1/deliveries", {
            "delivery_id": "d1", "artifact_id": "a1", "partner_id": "pB",
            "region": "BR", "channel": "web", "event_time": iso(2026, 2, 1),
        })
        self.assertEqual(s, 201)
        # 撤销授权
        s, _ = self.call("POST", "/v1/grants/g1/revoke",
                         {"partner_id": "pA", "event_time": iso(2026, 3, 1)})
        self.assertEqual(s, 200)
        # 新投递 403
        s, err = self.call("POST", "/v1/deliveries", {
            "delivery_id": "d2", "artifact_id": "a1", "partner_id": "pB",
            "region": "BR", "channel": "web", "event_time": iso(2026, 4, 1),
        })
        self.assertEqual(s, 403)
        self.assertIn("error", err)
        # 旧收据仍在
        s, receipt = self.call("GET", "/v1/deliveries/d1/receipt")
        self.assertEqual(s, 200)
        self.assertFalse(receipt["currently_usable"])
        # 审计链完好
        s, audit = self.call("GET", "/audit/verify")
        self.assertEqual(s, 200)
        self.assertTrue(audit["ok"])

    def test_attachment_isolation_over_http(self):
        self.call("POST", "/v1/originals", {
            "artifact_id": "a1", "partner_id": "pA",
            "event_time": iso(2026, 1, 1), "body": "正文\n",
        })
        s, _ = self.call("POST", "/v1/artifacts/a1/attachments", {
            "attachment_id": "secret", "partner_id": "pA",
            "event_time": iso(2026, 1, 2), "filename": "s.bin",
            "media_type": "application/octet-stream", "scope": ["pB"],
        })
        self.assertEqual(s, 201)
        s, _ = self.call("PUT",
                         "/v1/artifacts/a1/attachments/secret/content",
                         raw=b"sensitive-bytes",
                         headers={"X-Partner-Id": "pA"})
        self.assertEqual(s, 200)
        # pB 可读
        s, data = self.call(
            "GET", "/v1/artifacts/a1/attachments/secret/content?partner_id=pB")
        self.assertEqual(s, 200)
        self.assertEqual(data, b"sensitive-bytes")
        # pC 不可读（404，不暴露存在性）
        s, _ = self.call(
            "GET", "/v1/artifacts/a1/attachments/secret/content?partner_id=pC")
        self.assertEqual(s, 404)

    def test_service_restart_continues_pending_queue(self):
        from service.engine import ExchangeService, DispatchRetry
        state = {"block": True}

        def flaky(delivery, artifact):
            if state["block"]:
                raise DispatchRetry()

        self.service.dispatcher = flaky
        self.call("POST", "/v1/originals", {
            "artifact_id": "a1", "partner_id": "pA",
            "event_time": iso(2026, 1, 1), "body": "正文\n",
        })
        self.call("POST", "/v1/grants", {
            "grant_id": "g1", "artifact_id": "a1", "partner_id": "pA",
            "grantee_partner_id": "pB", "actions": ["publish"],
            "regions": ["*"], "channels": ["*"],
            "valid_from": iso(2026, 1, 1), "event_time": iso(2026, 1, 1),
        })
        s, d = self.call("POST", "/v1/deliveries", {
            "delivery_id": "d1", "artifact_id": "a1", "partner_id": "pB",
            "region": "BR", "channel": "web", "event_time": iso(2026, 2, 1),
        })
        self.assertEqual(d["status"], "queued")
        # “重启”：用同一数据目录新建服务实例（默认投递器可用）
        from service.store import EventStore
        restarted = ExchangeService(EventStore(self.tmp))
        self.assertEqual(restarted._delivery("d1")["status"], "sent")

    def test_unknown_route_and_bad_request(self):
        s, _ = self.call("GET", "/nope")
        self.assertEqual(s, 404)
        s, err = self.call("POST", "/v1/originals", {"artifact_id": "x"})
        self.assertEqual(s, 400)
        self.assertIn("error", err)


if __name__ == "__main__":
    unittest.main()
