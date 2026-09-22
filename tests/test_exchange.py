"""稿件来源图与授权流转测试。"""
import unittest

from service.engine import ExchangeService, DispatchRetry
from service.models import (
    ACTION_PUBLISH,
    KIND_EXCERPT,
    KIND_ORIGINAL,
    KIND_REVISION,
    KIND_TRANSLATION,
)
from service.store import EventStore
from tests.support import make_service, iso


def seed(svc):
    """pA 原稿 + 向 pB 的授权（巴西/web，2026 年内）。"""
    svc.register_original({
        "artifact_id": "art-1", "partner_id": "pA", "event_time": iso(2026, 1, 1),
        "title": "发展故事", "language": "zh",
        "body": "第一段\n第二段\n第三段\n",
        "authors": [{"name": "甲", "role": "作者"},
                    {"name": "乙", "role": "记者"}],
        "context_note": "本稿语境说明",
    })
    svc.issue_grant({
        "grant_id": "g1", "artifact_id": "art-1", "partner_id": "pA",
        "grantee_partner_id": "pB",
        "actions": ["publish", "translate", "excerpt", "edit"],
        "regions": ["BR"], "channels": ["web"],
        "valid_from": iso(2026, 1, 1), "valid_until": iso(2027, 1, 1),
        "must_preserve": ["保留原作者署名", "不得歪曲语境"],
        "event_time": iso(2026, 1, 1),
    })


class ProvenanceTest(unittest.TestCase):
    def test_translation_lineage_and_attribution(self):
        svc, _ = make_service()
        seed(svc)
        tr = svc.translate({
            "artifact_id": "art-1-pt", "parent_id": "art-1",
            "partner_id": "pB", "event_time": iso(2026, 2, 1),
            "body": "um\ndois\ntrês\n", "language": "pt",
        })
        self.assertEqual(tr["kind"], KIND_TRANSLATION)
        self.assertEqual(tr["work_id"], "art-1-pt")  # 译稿是新作品
        prov = svc.provenance("art-1-pt")
        self.assertEqual(prov["originals"][0]["artifact_id"], "art-1")
        self.assertEqual(len(prov["lineage"]), 2)
        self.assertIn("译自", prov["relationship_to_original"][0])
        # 署名义务沿来源图汇聚：译者与原作者都要署名
        report = svc.rights_report("art-1-pt", "pB", "BR", "web",
                                   iso(2026, 3, 1))
        names = [a["name"] for a in report["attribution_duty"]["must_attribute_all"]]
        self.assertEqual(set(names), {"甲", "乙"})
        self.assertTrue(report["attribution_duty"]["declare_derivation"])
        self.assertIn("保留原作者署名", report["required_notices"])
        self.assertIn("不得歪曲语境", report["required_notices"])

    def test_excerpt_keeps_lineage(self):
        svc, _ = make_service()
        seed(svc)
        ex = svc.excerpt({
            "artifact_id": "art-1-ex", "parent_id": "art-1",
            "partner_id": "pB", "event_time": iso(2026, 2, 1),
            "body": "第二段\n",
        })
        self.assertEqual(ex["kind"], KIND_EXCERPT)
        prov = svc.provenance("art-1-ex")
        self.assertIn("节选自", prov["relationship_to_original"][0])

    def test_revision_same_work_and_version_chain(self):
        svc, _ = make_service()
        seed(svc)
        svc.revise({
            "artifact_id": "art-1-r1", "parent_id": "art-1",
            "partner_id": "pA", "event_time": iso(2026, 2, 1),
            "body": "第一段改\n第二段\n第三段\n",
        })
        view = svc.artifact_view("art-1-r1")
        self.assertEqual(view["kind"], KIND_REVISION)
        self.assertEqual(view["work_id"], "art-1")  # 修订沿同一作品
        self.assertTrue(view["is_current"])
        self.assertFalse(svc.artifact_view("art-1")["is_current"])


class AuthorizationTest(unittest.TestCase):
    def test_scope_region_channel_period(self):
        svc, _ = make_service()
        seed(svc)
        # 错误地区
        from service.models import Forbidden
        with self.assertRaises(Forbidden):
            svc.enqueue_delivery({
                "delivery_id": "d-bad-region", "artifact_id": "art-1",
                "partner_id": "pB", "region": "IN", "channel": "web",
                "event_time": iso(2026, 3, 1),
            })
        # 错误渠道
        with self.assertRaises(Forbidden):
            svc.enqueue_delivery({
                "delivery_id": "d-bad-channel", "artifact_id": "art-1",
                "partner_id": "pB", "region": "BR", "channel": "print",
                "event_time": iso(2026, 3, 1),
            })
        # 授权生效前
        with self.assertRaises(Forbidden):
            svc.enqueue_delivery({
                "delivery_id": "d-early", "artifact_id": "art-1",
                "partner_id": "pB", "region": "BR", "channel": "web",
                "event_time": iso(2025, 12, 31),
            })
        # 授权过期后
        with self.assertRaises(Forbidden):
            svc.enqueue_delivery({
                "delivery_id": "d-late", "artifact_id": "art-1",
                "partner_id": "pB", "region": "BR", "channel": "web",
                "event_time": iso(2027, 1, 2),
            })
        # 窗口内成功
        d = svc.enqueue_delivery({
            "delivery_id": "d-ok", "artifact_id": "art-1",
            "partner_id": "pB", "region": "BR", "channel": "web",
            "event_time": iso(2026, 6, 1),
        })
        self.assertEqual(d["status"], "sent")

    def test_grants_flow_down_provenance(self):
        svc, _ = make_service()
        seed(svc)
        svc.revise({
            "artifact_id": "art-1-r1", "parent_id": "art-1",
            "partner_id": "pA", "event_time": iso(2026, 2, 1),
            "body": "第一段\n第二段\n第三段（更新）\n",
        })
        # 挂在原稿上的授权对后继修订有效
        report = svc.rights_report("art-1-r1", "pB", "BR", "web",
                                   iso(2026, 3, 1))
        self.assertTrue(report["permitted"])
        self.assertEqual(report["grants"][0]["grant_id"], "g1")

    def test_modification_scope_reported(self):
        svc, _ = make_service()
        seed(svc)
        report = svc.rights_report("art-1", "pB", "BR", "web",
                                   iso(2026, 3, 1))
        scope = report["modification_scope"]
        self.assertTrue(scope["may_edit"])
        self.assertTrue(scope["may_translate"])
        self.assertTrue(scope["may_excerpt"])
        self.assertFalse(scope["may_sublicense"])
        self.assertFalse(scope["verbatim_publish_only"])

    def test_sublicense_cannot_exceed_held_scope(self):
        from service.models import Forbidden
        svc, _ = make_service()
        seed(svc)
        # pB 无 sublicense 权利
        with self.assertRaises(Forbidden):
            svc.issue_grant({
                "grant_id": "g2", "artifact_id": "art-1", "partner_id": "pB",
                "grantee_partner_id": "pC", "actions": ["publish"],
                "regions": ["BR"], "channels": ["web"],
                "valid_from": iso(2026, 1, 1),
                "event_time": iso(2026, 1, 2),
            })

    def test_revoke_blocks_future_but_keeps_receipt(self):
        svc, _ = make_service()
        seed(svc)
        d = svc.enqueue_delivery({
            "delivery_id": "d-before", "artifact_id": "art-1",
            "partner_id": "pB", "region": "BR", "channel": "web",
            "event_time": iso(2026, 3, 1),
        })
        self.assertEqual(d["status"], "sent")
        svc.revoke_grant("g1", {"partner_id": "pA",
                                "event_time": iso(2026, 4, 1)})
        # 撤销后新投递被阻止
        from service.models import Forbidden
        with self.assertRaises(Forbidden):
            svc.enqueue_delivery({
                "delivery_id": "d-after", "artifact_id": "art-1",
                "partner_id": "pB", "region": "BR", "channel": "web",
                "event_time": iso(2026, 5, 1),
            })
        # 既有收据仍可核验
        receipt = svc.get_receipt("d-before")
        self.assertEqual(receipt["receipt_id"], "receipt:d-before")
        self.assertFalse(receipt["currently_usable"])  # 当前已不可继续使用
        self.assertTrue(receipt["chain"]["ok"])

    def test_withdraw_blocks_future_keeps_evidence(self):
        from service.models import Conflict
        svc, _ = make_service()
        seed(svc)
        svc.enqueue_delivery({
            "delivery_id": "d1", "artifact_id": "art-1",
            "partner_id": "pB", "region": "BR", "channel": "web",
            "event_time": iso(2026, 3, 1),
        })
        svc.withdraw("art-1", {"partner_id": "pA",
                               "event_time": iso(2026, 4, 1),
                               "reason": "事实性更正"})
        # 撤回阻止新投递与新派生
        with self.assertRaises(Conflict):
            svc.enqueue_delivery({
                "delivery_id": "d2", "artifact_id": "art-1",
                "partner_id": "pB", "region": "BR", "channel": "web",
                "event_time": iso(2026, 4, 2),
            })
        with self.assertRaises(Conflict):
            svc.translate({
                "artifact_id": "art-1-pt", "parent_id": "art-1",
                "partner_id": "pB", "event_time": iso(2026, 4, 2),
                "body": "x\n",
            })
        # 历史收据仍在
        receipt = svc.get_receipt("d1")
        self.assertEqual(receipt["artifact_id"], "art-1")
        view = svc.artifact_view("art-1")
        self.assertTrue(view["withdrawn"])


class MergeTest(unittest.TestCase):
    def test_concurrent_revisions_require_explicit_merge(self):
        from service.models import Conflict
        svc, _ = make_service()
        seed(svc)
        svc.revise({
            "artifact_id": "rA", "parent_id": "art-1", "partner_id": "pA",
            "event_time": iso(2026, 2, 1),
            "body": "第一段A\n第二段\n第三段\n",
        })
        svc.revise({
            "artifact_id": "rB", "parent_id": "art-1", "partner_id": "pA",
            "event_time": iso(2026, 2, 2),
            "body": "第一段\n第二段B\n第三段\n",
        })
        work = svc.work_view("art-1")
        self.assertTrue(work["diverged"])
        self.assertEqual(set(work["current_heads"]), {"rA", "rB"})
        # 分叉期间禁止投递任一头节点
        with self.assertRaises(Conflict):
            svc.enqueue_delivery({
                "delivery_id": "d", "artifact_id": "rA",
                "partner_id": "pB", "region": "BR", "channel": "web",
                "event_time": iso(2026, 3, 1),
            })
        # 自动干净合并
        merged = svc.merge_heads({
            "artifact_id": "rM", "parents": ["rA", "rB"],
            "partner_id": "pA", "event_time": iso(2026, 2, 3),
        })
        self.assertEqual(merged["body"], "第一段A\n第二段B\n第三段\n")
        self.assertEqual(svc.work_view("art-1")["current_heads"], ["rM"])
        # 合并后投递恢复
        d = svc.enqueue_delivery({
            "delivery_id": "d", "artifact_id": "rM",
            "partner_id": "pB", "region": "BR", "channel": "web",
            "event_time": iso(2026, 3, 1),
        })
        self.assertEqual(d["status"], "sent")

    def test_overlapping_conflict_requires_resolved_body(self):
        from service.models import Conflict
        svc, _ = make_service()
        seed(svc)
        svc.revise({
            "artifact_id": "rA", "parent_id": "art-1", "partner_id": "pA",
            "event_time": iso(2026, 2, 1),
            "body": "第一段A\n第二段\n第三段\n",
        })
        svc.revise({
            "artifact_id": "rB", "parent_id": "art-1", "partner_id": "pA",
            "event_time": iso(2026, 2, 2),
            "body": "第一段C\n第二段\n第三段\n",
        })
        with self.assertRaises(Conflict):
            svc.merge_heads({
                "artifact_id": "rM", "parents": ["rA", "rB"],
                "partner_id": "pA", "event_time": iso(2026, 2, 3),
            })
        # 人工解决后显式提交
        merged = svc.merge_heads({
            "artifact_id": "rM", "parents": ["rA", "rB"],
            "partner_id": "pA", "event_time": iso(2026, 2, 4),
            "body": "第一段（人工定稿）\n第二段\n第三段\n",
        })
        self.assertTrue(merged["is_current"])

    def test_merge_requires_all_heads(self):
        from service.models import Conflict
        svc, _ = make_service()
        seed(svc)
        svc.revise({"artifact_id": "rA", "parent_id": "art-1",
                    "partner_id": "pA", "event_time": iso(2026, 2, 1),
                    "body": "A\n第二段\n第三段\n"})
        svc.revise({"artifact_id": "rB", "parent_id": "art-1",
                    "partner_id": "pA", "event_time": iso(2026, 2, 2),
                    "body": "B\n第二段\n第三段\n"})
        with self.assertRaises(Conflict):
            svc.merge_heads({"artifact_id": "rM", "parents": ["rA"],
                             "partner_id": "pA", "event_time": iso(2026, 2, 3)})


class DeliveryTest(unittest.TestCase):
    def test_idempotent_enqueue_and_ack(self):
        svc, _ = make_service()
        seed(svc)
        payload = {
            "delivery_id": "d1", "artifact_id": "art-1",
            "partner_id": "pB", "region": "BR", "channel": "web",
            "event_time": iso(2026, 3, 1),
        }
        first = svc.enqueue_delivery(payload)
        again = svc.enqueue_delivery(payload)  # 同稳定标识重复提交
        self.assertTrue(again.get("idempotent"))
        self.assertEqual(len(svc.store.records),
                         # original + grant + queued + sent
                         4)
        ack1 = svc.acknowledge("d1", {"partner_id": "pB",
                                      "event_time": iso(2026, 3, 2)})
        self.assertFalse(ack1.get("idempotent"))
        ack2 = svc.acknowledge("d1", {"partner_id": "pB",
                                      "event_time": iso(2026, 3, 3)})
        self.assertTrue(ack2.get("idempotent"))
        # 重复回执不产生新事件
        self.assertEqual(len(svc.store.records), 5)
        self.assertEqual(svc._delivery("d1")["dispatch_attempts"], 1)

    def test_queue_resumes_after_restart(self):
        pending = {"retry": True}

        def flaky(delivery, artifact):
            if pending["retry"]:
                raise DispatchRetry()

        svc, tmp = make_service(dispatcher=flaky)
        seed(svc)
        d = svc.enqueue_delivery({
            "delivery_id": "d1", "artifact_id": "art-1",
            "partner_id": "pB", "region": "BR", "channel": "web",
            "event_time": iso(2026, 3, 1),
        })
        self.assertEqual(d["status"], "queued")
        # 模拟服务重启：重放日志后待发队列仍在，投递器恢复后续发
        pending["retry"] = False
        svc2 = ExchangeService(EventStore(tmp))
        self.assertEqual(svc2._delivery("d1")["status"], "sent")
        receipt = svc2.get_receipt("d1")
        self.assertTrue(receipt["chain"]["ok"])

    def test_receipt_content_hash_tamper_detection(self):
        svc, _ = make_service()
        seed(svc)
        svc.enqueue_delivery({
            "delivery_id": "d1", "artifact_id": "art-1",
            "partner_id": "pB", "region": "BR", "channel": "web",
            "event_time": iso(2026, 3, 1),
        })
        receipt = svc.get_receipt("d1")
        self.assertTrue(receipt["content_hash_matches_current"])
        # 出修订版后，收据指向的旧内容哈希不再是当前版本
        svc.revise({"artifact_id": "art-1-r1", "parent_id": "art-1",
                    "partner_id": "pA", "event_time": iso(2026, 4, 1),
                    "body": "全新内容\n"})
        receipt = svc.get_receipt("d1")
        self.assertFalse(receipt["content_hash_matches_current"])
        self.assertFalse(receipt["currently_usable"])


class AttachmentTest(unittest.TestCase):
    def _register(self, svc):
        seed(svc)
        return svc.register_attachment("art-1", {
            "attachment_id": "secret-map",
            "partner_id": "pA", "event_time": iso(2026, 2, 1),
            "filename": "map.png", "media_type": "image/png",
            "scope": ["pB"],  # 仅 pA 与 pB 可见
        })

    def test_partner_isolation(self):
        from service.models import Forbidden, NotFound
        svc, _ = make_service()
        self._register(svc)
        svc.store_attachment_bytes("art-1", "secret-map", "pA",
                                   b"\x89PNG binary")
        # 授权伙伴可读
        _, data = svc.read_attachment_bytes("art-1", "secret-map", "pB")
        self.assertEqual(data, b"\x89PNG binary")
        # 非授权伙伴：等同不存在
        with self.assertRaises(NotFound):
            svc.read_attachment_bytes("art-1", "secret-map", "pC")
        # 列表同样过滤
        self.assertEqual(
            svc.list_attachments("art-1", "pC"), [])
        self.assertEqual(
            len(svc.list_attachments("art-1", "pB")), 1)
        # 非所有者不能上传
        with self.assertRaises(Forbidden):
            svc.store_attachment_bytes("art-1", "secret-map", "pB", b"x")

    def test_attachment_only_delivered_to_authorized(self):
        from service.models import Forbidden
        svc, _ = make_service()
        self._register(svc)
        # pC 无权随投递携带该附件
        with self.assertRaises(Forbidden):
            svc.enqueue_delivery({
                "delivery_id": "d1", "artifact_id": "art-1",
                "partner_id": "pC", "region": "BR", "channel": "web",
                "event_time": iso(2026, 3, 1),
                "attachment_ids": ["secret-map"],
            })


class AuditTest(unittest.TestCase):
    def test_event_and_received_times_both_recorded(self):
        svc, _ = make_service()
        seed(svc)
        records = svc.store.records
        for rec in records:
            self.assertIn("event_time", rec)
            self.assertIn("received_time", rec)
            self.assertTrue(rec["received_time"].endswith("Z")
                            or "+00:00" in rec["received_time"])

    def test_append_only_chain_detects_tampering(self):
        svc, tmp = make_service()
        seed(svc)
        self.assertTrue(svc.store.verify_chain()["ok"])
        # 外部篡改日志
        import os
        path = os.path.join(tmp, "event.log")
        with open(path, "a", encoding="utf-8") as fh:
            pass
        # 直接改写一行内容
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        import json
        rec = json.loads(lines[0])
        rec["payload"]["body"] = "被篡改"
        lines[0] = json.dumps(rec, ensure_ascii=False) + "\n"
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        # 重放即校验哈希链，篡改会在启动时被发现
        with self.assertRaises(RuntimeError):
            ExchangeService(EventStore(tmp))


if __name__ == "__main__":
    unittest.main()
