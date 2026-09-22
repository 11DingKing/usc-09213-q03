"""稿件交换业务能力测试：来源图、授权、撤回、合并、回执、附件、队列与核验。"""
import base64
import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection

from service import main

T0 = "2026-03-01T09:00:00+00:00"
T1 = "2026-03-01T10:00:00+00:00"
T2 = "2026-03-01T11:00:00+00:00"
T3 = "2026-03-01T12:00:00+00:00"
T4 = "2026-03-01T13:00:00+00:00"


class ExchangeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "exchange.db")
        self.server = main.make_server("127.0.0.1", 0, self.db_path)
        self.addCleanup(self._stop_server)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.server.app.close()

    def call(self, method, path, body=None):
        conn = HTTPConnection("127.0.0.1", self.server.server_port)
        try:
            conn.request(method, path,
                         json.dumps(body) if body is not None else None,
                         {"Content-Type": "application/json"})
            response = conn.getresponse()
            raw = response.read()
            return response.status, (json.loads(raw) if raw else {})
        finally:
            conn.close()

    def create_work(self, work_id="w1", kind="original", parent_id=None, **over):
        payload = {"work_id": work_id, "kind": kind, "language": "zh",
                   "parent_id": parent_id, "body": f"{work_id} 正文",
                   "context_note": "背景说明", "byline": "记者甲",
                   "editor_id": "ed1", "event_time": T0}
        payload.update(over)
        return self.call("POST", "/works", payload)

    def derivative(self, work_id, kind, partner_id=None, parent_id="w1"):
        payload = {"work_id": work_id, "kind": kind, "language": "sw",
                   "parent_id": parent_id, "body": f"{work_id} 正文",
                   "byline": "译者乙", "editor_id": "ed2", "event_time": T1}
        if partner_id is not None:
            payload["partner_id"] = partner_id
        return payload

    def grant(self, license_id="L1", work_id="w1", partner_id="partner-a", **over):
        payload = {"license_id": license_id, "work_id": work_id,
                   "partner_id": partner_id,
                   "regions": ["EA"], "channels": ["web"],
                   "valid_from": "2026-01-01T00:00:00+00:00",
                   "valid_until": "2030-12-31T23:59:59+00:00",
                   "permissions": {"translation": True},
                   "attribution_text": "转载请注明记者甲",
                   "event_time": T0}
        payload.update(over)
        return self.call("POST", "/licenses", payload)


class ProvenanceGraphTest(ExchangeTestCase):
    def test_derivative_chain_records_lineage(self):
        self.assertEqual(self.create_work()[0], 201)
        self.assertEqual(self.create_work("w2", "translation", "w1",
                                          language="sw", byline="译者乙")[0], 201)
        self.assertEqual(self.create_work("w3", "excerpt", "w2")[0], 201)
        self.assertEqual(self.create_work("w4", "reedit", "w3")[0], 201)
        status, payload = self.call("GET", "/works/w4/verification")
        self.assertEqual(status, 200)
        chain = payload["relationship_to_original"]["chain"]
        self.assertEqual([n["work_id"] for n in chain], ["w4", "w3", "w2", "w1"])
        self.assertEqual([n["kind"] for n in chain],
                         ["reedit", "excerpt", "translation", "original"])
        self.assertEqual(payload["relationship_to_original"]["depth"], 3)
        self.assertFalse(payload["relationship_to_original"]["is_original"])

    def test_derivative_requires_parent(self):
        status, _ = self.call("POST", "/works",
                              self.derivative("w2", "translation"))
        self.assertEqual(status, 404)  # 父稿件不存在
        payload = self.derivative("w2", "translation")
        del payload["parent_id"]
        status, _ = self.call("POST", "/works", payload)
        self.assertEqual(status, 400)  # 衍生稿件必须指明父稿件

    def test_original_rejects_parent(self):
        self.create_work()
        status, _ = self.call("POST", "/works",
                              self.derivative("w2", "original"))
        self.assertEqual(status, 400)

    def test_partner_derivative_respects_license_scope(self):
        self.create_work()
        status, _ = self.call("POST", "/works",
                              self.derivative("w-t", "translation", "partner-b"))
        self.assertEqual(status, 403)  # 未持有授权
        self.grant()  # partner-a 仅可 translation
        status, _ = self.call("POST", "/works",
                              self.derivative("w-r", "reedit", "partner-a"))
        self.assertEqual(status, 403)  # 可修改范围不含 reedit
        status, _ = self.call("POST", "/works",
                              self.derivative("w-t", "translation", "partner-a"))
        self.assertEqual(status, 201)


class LicenseScopeTest(ExchangeTestCase):
    def check(self, **kw):
        payload = {"region": "EA", "channel": "web",
                   "at": "2026-06-01T00:00:00+00:00"}
        payload.update(kw)
        return self.call("POST", "/licenses/L1/check", payload)

    def test_region_channel_and_term(self):
        self.create_work()
        self.grant(valid_until="2026-12-31T23:59:59+00:00")
        status, result = self.check()
        self.assertEqual(status, 200)
        self.assertTrue(result["allowed"])
        self.assertEqual(self.check(region="AF")[1]["reasons"],
                         ["region_not_covered"])
        self.assertEqual(self.check(channel="print")[1]["reasons"],
                         ["channel_not_covered"])
        self.assertEqual(self.check(at="2025-12-31T23:59:59+00:00")[1]["reasons"],
                         ["not_yet_valid"])
        self.assertEqual(self.check(at="2027-01-01T00:00:00+00:00")[1]["reasons"],
                         ["expired"])


class RevocationTest(ExchangeTestCase):
    def test_revocation_blocks_future_but_keeps_vouchers(self):
        self.create_work()
        self.grant()
        status, voucher = self.call(
            "POST", "/licenses/L1/vouchers",
            {"voucher_id": "V1", "version_no": 1, "region": "EA", "channel": "web",
             "published_at": "2026-06-01T00:00:00+00:00", "event_time": T1})
        self.assertEqual(status, 201)
        self.assertTrue(voucher["honored"])
        status, lic = self.call("POST", "/licenses/L1/revoke",
                                {"event_time": "2026-09-01T00:00:00+00:00"})
        self.assertEqual(status, 200)
        self.assertEqual(lic["status"], "revoked")
        # 撤回之后的未来使用被阻止
        status, check = self.call(
            "POST", "/licenses/L1/check",
            {"region": "EA", "channel": "web", "at": "2026-09-02T00:00:00+00:00"})
        self.assertFalse(check["allowed"])
        self.assertIn("revoked", check["reasons"])
        # 撤回时点之前的使用仍然有效
        status, check = self.call(
            "POST", "/licenses/L1/check",
            {"region": "EA", "channel": "web", "at": "2026-06-01T00:00:00+00:00"})
        self.assertTrue(check["allowed"])
        # 已发布凭证保留且继续有效
        status, vouchers = self.call("GET", "/licenses/L1/vouchers")
        self.assertEqual(status, 200)
        self.assertEqual([v["voucher_id"] for v in vouchers["vouchers"]], ["V1"])
        self.assertTrue(vouchers["vouchers"][0]["honored"])
        # 撤回后登记新凭证被拒绝
        status, _ = self.call(
            "POST", "/licenses/L1/vouchers",
            {"voucher_id": "V2", "version_no": 1, "region": "EA", "channel": "web",
             "published_at": "2026-09-02T00:00:00+00:00", "event_time": T2})
        self.assertEqual(status, 409)
        # 撤回后禁止再分发
        status, _ = self.call("POST", "/works/w1/dispatch",
                              {"partner_ids": ["partner-a"], "event_time": T2})
        self.assertEqual(status, 409)


class MergeTest(ExchangeTestCase):
    def test_concurrent_edits_need_explicit_merge(self):
        self.create_work()  # v1 为头版本
        status, a = self.call("POST", "/works/w1/versions",
                              {"editor_id": "ed-a", "base_version": 1,
                               "body": "A 的修订", "event_time": T1})
        self.assertEqual(status, 201)
        self.assertTrue(a["is_head"])
        # 编辑 B 同样基于 v1：成为分支，头版本不变
        status, b = self.call("POST", "/works/w1/versions",
                              {"editor_id": "ed-b", "base_version": 1,
                               "body": "B 的修订", "event_time": T2})
        self.assertEqual(status, 201)
        self.assertFalse(b["is_head"])
        self.assertTrue(b["requires_merge"])
        _, current = self.call("GET", "/works/w1")
        self.assertEqual(current["work"]["head_version"], 2)
        # 显式合并后产生带两个父版本的新头版本
        status, m = self.call("POST", "/works/w1/merges",
                              {"editor_id": "ed-c", "source_version": 3,
                               "body": "合并稿", "event_time": T3})
        self.assertEqual(status, 201)
        self.assertEqual(m["version_no"], 4)
        _, current = self.call("GET", "/works/w1")
        self.assertEqual(current["work"]["head_version"], 4)
        self.assertEqual(current["head"]["parents"], [2, 3])
        self.assertTrue(current["head"]["is_merge"])
        # 已并入的分支不得重复合并
        status, _ = self.call("POST", "/works/w1/merges",
                              {"editor_id": "ed-c", "source_version": 3,
                               "body": "再次合并", "event_time": T4})
        self.assertEqual(status, 409)


class ReceiptTest(ExchangeTestCase):
    def dispatch(self):
        status, out = self.call("POST", "/works/w1/dispatch",
                                {"partner_ids": ["partner-a"], "event_time": T1})
        self.assertEqual(status, 201)
        return out["deliveries"][0]

    def test_duplicate_receipts_do_not_inflate_count(self):
        self.create_work()
        self.grant()
        delivery_id = self.dispatch()["delivery_id"]
        # 未发出前不能登记回执
        status, _ = self.call("POST", f"/deliveries/{delivery_id}/receipts",
                              {"receipt_key": "r1", "event_time": T2})
        self.assertEqual(status, 409)
        self.server.app.drain_outbox()
        # 同一回执键重复提交：首次 201，之后 200 且次数不变
        status, receipt = self.call("POST", f"/deliveries/{delivery_id}/receipts",
                                    {"receipt_key": "r1", "event_time": T2})
        self.assertEqual(status, 201)
        self.assertEqual(receipt["receipt_count"], 1)
        for _ in range(2):
            status, receipt = self.call("POST",
                                        f"/deliveries/{delivery_id}/receipts",
                                        {"receipt_key": "r1", "event_time": T3})
            self.assertEqual(status, 200)
            self.assertTrue(receipt["duplicate"])
            self.assertEqual(receipt["receipt_count"], 1)
        # 不同回执键正常计数
        status, receipt = self.call("POST", f"/deliveries/{delivery_id}/receipts",
                                    {"receipt_key": "r2", "event_time": T4})
        self.assertEqual(receipt["receipt_count"], 2)
        _, delivery = self.call("GET", f"/deliveries/{delivery_id}")
        self.assertEqual(delivery["receipt_count"], 2)

    def test_dispatch_requires_active_license(self):
        self.create_work()
        status, _ = self.call("POST", "/works/w1/dispatch",
                              {"partner_ids": ["stranger"], "event_time": T1})
        self.assertEqual(status, 409)

    def test_dispatch_is_idempotent_per_version(self):
        self.create_work()
        self.grant()
        first = self.dispatch()
        second = self.dispatch()
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["delivery_id"], second["delivery_id"])


class AttachmentIsolationTest(ExchangeTestCase):
    def test_sensitive_attachment_isolated_per_partner(self):
        self.create_work()
        secret = base64.b64encode("线人名单".encode("utf-8")).decode("ascii")
        status, _ = self.call("POST", "/attachments",
                              {"attachment_id": "att1", "work_id": "w1",
                               "name": "线人名单.csv", "content_b64": secret,
                               "sensitive": True,
                               "allowed_partners": ["partner-a"],
                               "event_time": T0})
        self.assertEqual(status, 201)
        status, payload = self.call("GET", "/attachments/att1?partner_id=partner-a")
        self.assertEqual(status, 200)
        self.assertEqual(base64.b64decode(payload["content_b64"]).decode("utf-8"),
                         "线人名单")
        status, _ = self.call("GET", "/attachments/att1?partner_id=partner-b")
        self.assertEqual(status, 403)
        # 非敏感附件不受隔离限制
        self.call("POST", "/attachments",
                  {"attachment_id": "att2", "work_id": "w1", "name": "配图.jpg",
                   "content_b64": secret, "sensitive": False, "event_time": T0})
        status, _ = self.call("GET", "/attachments/att2?partner_id=partner-b")
        self.assertEqual(status, 200)


class OutboxRestartTest(ExchangeTestCase):
    def test_pending_queue_survives_restart(self):
        self.create_work()
        self.grant()
        _, out = self.call("POST", "/works/w1/dispatch",
                           {"partner_ids": ["partner-a"], "event_time": T1})
        delivery_id = out["deliveries"][0]["delivery_id"]
        # 不发出，直接“重启”：关闭服务后用同一数据库文件重新启动
        self._stop_server()
        self.server = main.make_server("127.0.0.1", 0, self.db_path)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        status, delivery = self.call("GET", f"/deliveries/{delivery_id}")
        self.assertEqual(status, 200)
        self.assertEqual(delivery["status"], "pending")
        sent = self.server.app.drain_outbox()
        self.assertEqual(sent, [delivery_id])
        _, delivery = self.call("GET", f"/deliveries/{delivery_id}")
        self.assertEqual(delivery["status"], "dispatched")


class VerificationTest(ExchangeTestCase):
    def test_recipient_can_verify_version_attribution_and_relation(self):
        self.create_work()
        self.create_work("w2", "translation", "w1", language="sw", byline="译者乙")
        self.grant(license_id="L2", work_id="w2",
                   attribution_text="须署名 记者甲 / 译者乙")
        status, payload = self.call("GET", "/works/w2/verification")
        self.assertEqual(status, 200)
        self.assertTrue(payload["is_current"])
        self.assertEqual(payload["head_version"], 1)
        # 署名义务：从原稿作者到译者，完整链式呈现
        self.assertEqual([a["byline"] for a in payload["attribution_chain"]],
                         ["记者甲", "译者乙"])
        self.assertEqual(payload["licenses"][0]["attribution_text"],
                         "须署名 记者甲 / 译者乙")
        self.assertEqual(payload["relationship_to_original"]["chain"][1]["kind"],
                         "original")
        # 内容哈希可核验完整性
        _, good = self.call(
            "GET", f"/works/w2/verification?version=1&hash={payload['content_hash']}")
        self.assertTrue(good["hash_match"])
        _, bad = self.call("GET", "/works/w2/verification?version=1&hash=deadbeef")
        self.assertFalse(bad["hash_match"])
        # 修订后旧版本不再是最新版本
        self.call("POST", "/works/w2/versions",
                  {"editor_id": "ed2", "base_version": 1, "body": "修订译文",
                   "event_time": T1})
        _, stale = self.call("GET", "/works/w2/verification?version=1")
        self.assertFalse(stale["is_current"])
        self.assertEqual(stale["head_version"], 2)


class AuditTest(ExchangeTestCase):
    def test_audit_trail_appends_with_dual_timestamps(self):
        self.create_work()
        self.call("POST", "/works/w1/versions",
                  {"editor_id": "ed2", "base_version": 1, "body": "修订",
                   "event_time": T1})
        status, payload = self.call("GET", "/audit?entity=work&entity_id=w1")
        self.assertEqual(status, 200)
        entries = payload["entries"]
        self.assertEqual([e["action"] for e in entries], ["create", "revise"])
        for entry in entries:
            self.assertTrue(entry["event_time"])
            self.assertTrue(entry["recorded_at"])
        self.assertEqual(entries[1]["detail"]["editor_id"], "ed2")


if __name__ == "__main__":
    unittest.main()
