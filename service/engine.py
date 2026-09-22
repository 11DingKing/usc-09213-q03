"""稿件交换核心引擎。

在只追加事件存储之上实现：

* 稿件来源图（原稿 / 译稿 / 节选 / 修订 / 合并）与作品当前版本；
* 授权按伙伴、地区、渠道、动作与期限生效，沿来源图向下流动；
* 撤回只阻止未来使用，已签发收据作为凭证永久保留；
* 并发修订必须显式合并后才能投递；
* 投递持久化排队，重启续发，回执/确认幂等；
* 敏感附件按伙伴隔离。
"""
from __future__ import annotations

import difflib
import re
import threading
from typing import Callable, Optional

from .models import (
    ACTION_EDIT,
    ACTION_EXCERPT,
    ACTION_PUBLISH,
    ACTION_SUBLICENSE,
    ACTION_TRANSLATE,
    DERIVED_KINDS,
    KIND_EXCERPT,
    KIND_ORIGINAL,
    KIND_REVISION,
    KIND_TRANSLATION,
    REL_EXCERPTED_FROM,
    REL_MERGED_WITH,
    REL_REVISED_FROM,
    REL_TRANSLATED_FROM,
    WILDCARD,
    Conflict,
    DomainError,
    Forbidden,
    InvalidRequest,
    NotFound,
    canonical_json,
    content_hash,
    now_iso,
    parse_iso,
    require,
)
from .store import EventStore

ID_PATTERN = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")


def _check_id(value: str, field: str = "id") -> str:
    if not isinstance(value, str) or not ID_PATTERN.match(value):
        raise InvalidRequest(f"{field} 非法：仅允许字母数字及 . _ : -，最长 128")
    return value


def _as_list(value, field: str) -> list:
    if value is None:
        raise InvalidRequest(f"缺少 {field}")
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) and v for v in value):
        return value
    raise InvalidRequest(f"{field} 必须是字符串或字符串数组")


# ---------------------------------------------------------------------------
# 事件归约：重放事件日志重建索引
# ---------------------------------------------------------------------------

def _reducer_artifact(record: dict, indexes: dict):
    p = record["payload"]
    parents = list(p.get("parents", []))
    node = {
        "artifact_id": p["artifact_id"],
        "kind": p["kind"],
        "work_id": p["work_id"],
        "language": p.get("language"),
        "title": p.get("title"),
        "authors": p.get("authors", []),
        "context_note": p.get("context_note", ""),
        "body": p.get("body", ""),
        "content_hash": p["content_hash"],
        "parents": parents,
        "relation": p.get("relation"),
        "rights_holder": p.get("rights_holder"),
        "created_by": p["partner_id"],
        "event_time": record["event_time"],
        "received_time": record["received_time"],
        "superseded": False,
        "withdrawn": False,
        "withdrawn_event_time": None,
        "withdrawn_reason": None,
    }
    indexes["artifacts"][p["artifact_id"]] = node
    for parent in parents:
        indexes["children"].setdefault(parent, []).append(p["artifact_id"])
    work = indexes.setdefault("works", {}).setdefault(
        p["work_id"], {"original_id": None, "nodes": [], "heads": []}
    )
    if p["kind"] == KIND_ORIGINAL:
        work["original_id"] = p["artifact_id"]
    elif work["original_id"] is None and not any(
            pid in indexes["artifacts"]
            and indexes["artifacts"][pid]["work_id"] == p["work_id"]
            for pid in parents):
        # 译稿/节选另起新作品时，自身就是该作品的根版本
        work["original_id"] = p["artifact_id"]
    work["nodes"].append(p["artifact_id"])
    if parents:
        work["heads"] = [h for h in work["heads"] if h not in parents]
    work["heads"].append(p["artifact_id"])


def _reducer_withdrawal(record: dict, indexes: dict):
    p = record["payload"]
    node = indexes["artifacts"][p["artifact_id"]]
    node["withdrawn"] = True
    node["withdrawn_event_time"] = record["event_time"]
    node["withdrawn_reason"] = p.get("reason")


def _reducer_grant(record: dict, indexes: dict):
    p = record["payload"]
    indexes["grants"][p["grant_id"]] = {
        "grant_id": p["grant_id"],
        "artifact_id": p["artifact_id"],
        "partner_id": p["partner_id"],
        "actions": p["actions"],
        "regions": p["regions"],
        "channels": p["channels"],
        "valid_from": p["valid_from"],
        "valid_until": p.get("valid_until"),
        "must_preserve": p.get("must_preserve", []),
        "status": "active",
        "event_time": record["event_time"],
        "received_time": record["received_time"],
        "revoked_event_time": None,
    }


def _reducer_revoke(record: dict, indexes: dict):
    grant = indexes["grants"][record["payload"]["grant_id"]]
    grant["status"] = "revoked"
    grant["revoked_event_time"] = record["event_time"]


def _reducer_attachment(record: dict, indexes: dict):
    p = record["payload"]
    indexes["attachments"][p["attachment_id"]] = {
        "attachment_id": p["attachment_id"],
        "artifact_id": p["artifact_id"],
        "filename": p["filename"],
        "media_type": p.get("media_type", "application/octet-stream"),
        "sha256": p.get("sha256"),
        "size": p.get("size"),
        "scope": p["scope"],
        "owner_partner": p["partner_id"],
        "event_time": record["event_time"],
    }


def _reducer_delivery(record: dict, indexes: dict):
    table = indexes["deliveries"]
    p = record["payload"]
    t = record["type"]
    if t == "delivery_queued":
        table[p["delivery_id"]] = {
            "delivery_id": p["delivery_id"],
            "artifact_id": p["artifact_id"],
            "partner_id": p["partner_id"],
            "region": p["region"],
            "channel": p["channel"],
            "attachment_ids": p.get("attachment_ids", []),
            "grants_relied_upon": p.get("grants_relied_upon", []),
            "status": "queued",
            "queued_event_time": record["event_time"],
            "dispatch_attempts": 0,
            "sent_event_time": None,
            "receipt": None,
            "acknowledged": False,
            "ack_event_time": None,
        }
    elif t == "delivery_sent":
        d = table[p["delivery_id"]]
        d["status"] = "sent"
        d["dispatch_attempts"] += 1
        d["sent_event_time"] = record["event_time"]
        d["receipt"] = {
            **p["receipt"],
            "record_seq": record["seq"],
            "record_hash": record["hash"],
        }
    elif t == "delivery_dispatch_failed":
        table[p["delivery_id"]]["dispatch_attempts"] += 1
    elif t == "delivery_acknowledged":
        d = table[p["delivery_id"]]
        if not d["acknowledged"]:  # 重复回执不改变状态
            d["acknowledged"] = True
            d["ack_event_time"] = record["event_time"]


def _reducer_attachment_content(record: dict, indexes: dict):
    p = record["payload"]
    meta = indexes["attachments"].get(p["attachment_id"])
    if meta is not None:
        meta["sha256"] = p["sha256"]
        meta["size"] = p["size"]


REDUCERS = {
    "artifact_registered": _reducer_artifact,
    "grant_issued": _reducer_grant,
    "grant_revoked": _reducer_revoke,
    "artifact_withdrawn": _reducer_withdrawal,
    "attachment_registered": _reducer_attachment,
    "attachment_content_stored": _reducer_attachment_content,
    "delivery_queued": _reducer_delivery,
    "delivery_sent": _reducer_delivery,
    "delivery_dispatch_failed": _reducer_delivery,
    "delivery_acknowledged": _reducer_delivery,
}


# 投递器：真实部署中负责把稿件送到伙伴渠道；成功返回 None，暂不可用抛 Retry。
class DispatchRetry(Exception):
    pass


Dispatcher = Callable[[dict, dict], None]


def default_dispatcher(delivery: dict, artifact: dict) -> None:
    """本地模拟投递：始终成功。"""
    return None


class ExchangeService:
    def __init__(self, store: EventStore, dispatcher: Optional[Dispatcher] = None):
        self.store = store
        self.dispatcher = dispatcher or default_dispatcher
        self._dispatch_lock = threading.Lock()
        self.store.replay(REDUCERS)
        self.drain_queue()

    # ======================================================================
    # 内部工具
    # ======================================================================

    @property
    def _idx(self) -> dict:
        return self.store.indexes

    def _node(self, artifact_id: str) -> dict:
        node = self._idx["artifacts"].get(artifact_id)
        if node is None:
            raise NotFound(f"稿件不存在: {artifact_id}")
        return node

    def _grant(self, grant_id: str) -> dict:
        grant = self._idx["grants"].get(grant_id)
        if grant is None:
            raise NotFound(f"授权不存在: {grant_id}")
        return grant

    def _delivery(self, delivery_id: str) -> dict:
        delivery = self._idx["deliveries"].get(delivery_id)
        if delivery is None:
            raise NotFound(f"投递不存在: {delivery_id}")
        return delivery

    def _ancestors(self, artifact_id: str) -> list[dict]:
        """按从远到近返回所有祖先节点（去重，含自身）。"""
        ordered: list[dict] = []
        seen = set()

        def visit(aid: str):
            node = self._node(aid)
            for parent in node["parents"]:
                if parent not in seen:
                    visit(parent)
            if aid not in seen:
                seen.add(aid)
                ordered.append(node)

        visit(artifact_id)
        return ordered

    def _originals(self, artifact_id: str) -> list[dict]:
        return [n for n in self._ancestors(artifact_id) if n["kind"] == KIND_ORIGINAL]

    # ======================================================================
    # 稿件登记与来源图
    # ======================================================================

    def register_original(self, payload: dict) -> dict:
        artifact_id = _check_id(require(payload, "artifact_id"))
        partner_id = _check_id(require(payload, "partner_id"), "partner_id")
        event_time = require(payload, "event_time")
        parse_iso(event_time)
        body = require(payload, "body")
        if not isinstance(body, str):
            raise InvalidRequest("body 必须是字符串")
        with self.store.lock:
            if artifact_id in self._idx["artifacts"]:
                raise Conflict(f"稿件标识已存在: {artifact_id}")
            record = self.store.append(
                "artifact_registered",
                {
                    "artifact_id": artifact_id,
                    "kind": KIND_ORIGINAL,
                    "work_id": artifact_id,
                    "relation": None,
                    "parents": [],
                    "partner_id": partner_id,
                    "rights_holder": partner_id,
                    "title": payload.get("title", ""),
                    "language": payload.get("language"),
                    "authors": _clean_authors(payload.get("authors", [])),
                    "context_note": payload.get("context_note", ""),
                    "body": body,
                    "content_hash": content_hash(body),
                },
                event_time,
            )
        return (self.artifact_view(artifact_id)
                | {"event_seq": record["seq"]})

    def _derive(self, payload: dict, kind: str, relation: str, new_work: bool) -> dict:
        artifact_id = _check_id(require(payload, "artifact_id"))
        parent_id = _check_id(require(payload, "parent_id"), "parent_id")
        partner_id = _check_id(require(payload, "partner_id"), "partner_id")
        event_time = require(payload, "event_time")
        parse_iso(event_time)
        body = require(payload, "body")
        if not isinstance(body, str):
            raise InvalidRequest("body 必须是字符串")
        needed_action = {
            KIND_TRANSLATION: ACTION_TRANSLATE,
            KIND_EXCERPT: ACTION_EXCERPT,
            KIND_REVISION: ACTION_EDIT,
        }[kind]
        with self.store.lock:
            if artifact_id in self._idx["artifacts"]:
                raise Conflict(f"稿件标识已存在: {artifact_id}")
            parent = self._node(parent_id)
            self._assert_usable(parent_id)
            self._assert_holds_action(parent_id, partner_id,
                                      needed_action, event_time)
            # 基于非当前头节点的修订会形成分叉（多编辑并发），该作品将在
            # 显式合并全部头节点前禁止投递；译稿/节选另起新作品不受影响。
            work_id = artifact_id if new_work else parent["work_id"]
            record = self.store.append(
                "artifact_registered",
                {
                    "artifact_id": artifact_id,
                    "kind": kind,
                    "work_id": work_id,
                    "relation": relation,
                    "parents": [parent_id],
                    "partner_id": partner_id,
                    "rights_holder": parent["rights_holder"],
                    "title": payload.get("title", parent.get("title")),
                    "language": payload.get("language", parent.get("language")),
                    "authors": _clean_authors(payload.get("authors", []))
                    or parent["authors"],
                    "context_note": payload.get(
                        "context_note", parent.get("context_note", "")
                    ),
                    "body": body,
                    "content_hash": content_hash(body),
                },
                event_time,
            )
        return (self.artifact_view(artifact_id)
                | {"event_seq": record["seq"]})

    def translate(self, payload: dict) -> dict:
        return self._derive(payload, KIND_TRANSLATION, REL_TRANSLATED_FROM, True)

    def excerpt(self, payload: dict) -> dict:
        return self._derive(payload, KIND_EXCERPT, REL_EXCERPTED_FROM, True)

    def revise(self, payload: dict) -> dict:
        return self._derive(payload, KIND_REVISION, REL_REVISED_FROM, False)

    def merge_heads(self, payload: dict) -> dict:
        """显式合并同一作品的多个并发首节点。

        body 由调用方提供时视为冲突已人工解决；否则服务端做基于共同祖先的
        三方自动合并，遇到重叠修改返回 409，要求调用方显式给出解决文本。
        """
        artifact_id = _check_id(require(payload, "artifact_id"))
        partner_id = _check_id(require(payload, "partner_id"), "partner_id")
        heads = require(payload, "parents")
        if not isinstance(heads, list) or not heads:
            raise InvalidRequest("parents 必须是非空数组")
        heads = [_check_id(h, "parents") for h in heads]
        event_time = require(payload, "event_time")
        parse_iso(event_time)
        with self.store.lock:
            if artifact_id in self._idx["artifacts"]:
                raise Conflict(f"稿件标识已存在: {artifact_id}")
            nodes = [self._node(h) for h in heads]
            works = {n["work_id"] for n in nodes}
            if len(works) != 1:
                raise InvalidRequest("只能合并同一作品下的版本")
            for head in heads:
                self._assert_usable(head)  # 撤回链上的版本不得合并为新使用
            work_id = works.pop()
            current_heads = set(self._idx["works"][work_id]["heads"])
            if set(heads) != current_heads or len(heads) < 2:
                raise Conflict(
                    f"必须显式合并该作品的全部当前首节点: {sorted(current_heads)}"
                )
            for head in heads:
                self._assert_holds_action(head, partner_id,
                                          ACTION_EDIT, event_time)
            body = payload.get("body")
            if body is None:
                if len(heads) != 2:
                    raise InvalidRequest(
                        "超过两个首节点的合并必须显式提供解决后的 body"
                    )
                body = self._auto_merge(heads[0], heads[1])
            elif not isinstance(body, str):
                raise InvalidRequest("body 必须是字符串")
            base = nodes[0]
            record = self.store.append(
                "artifact_registered",
                {
                    "artifact_id": artifact_id,
                    "kind": KIND_REVISION,
                    "work_id": work_id,
                    "relation": REL_MERGED_WITH,
                    "parents": heads,
                    "partner_id": partner_id,
                    "rights_holder": base["rights_holder"],
                    "title": payload.get("title", base.get("title")),
                    "language": payload.get("language", base.get("language")),
                    "authors": _clean_authors(payload.get("authors", []))
                    or _union_authors(nodes),
                    "context_note": payload.get(
                        "context_note", base.get("context_note", "")
                    ),
                    "body": body,
                    "content_hash": content_hash(body),
                },
                event_time,
            )
        return (self.artifact_view(artifact_id)
                | {"event_seq": record["seq"]})

    def _auto_merge(self, left_id: str, right_id: str) -> str:
        left = self._node(left_id)
        right = self._node(right_id)
        # 共同祖先：两条祖先链上最近的共有节点
        la = self._ancestors(left_id)
        ra = {n["artifact_id"]: n for n in self._ancestors(right_id)}
        base = None
        for node in reversed(la):
            if node["artifact_id"] in ra and node["artifact_id"] not in (left_id, right_id):
                base = node
                break
        if base is None and self._originals(left_id)[0]["artifact_id"] == self._originals(right_id)[0]["artifact_id"]:
            base = self._node(self._originals(left_id)[0]["artifact_id"])
        if base is None:
            raise Conflict("两个首节点没有共同祖先，无法自动合并；请显式提供 body")
        merged, conflicts = three_way_merge(
            base["body"], left["body"], right["body"]
        )
        if conflicts:
            raise Conflict(
                f"存在 {len(conflicts)} 处重叠修改，必须人工解决后显式提交 body；"
                f"首处冲突行: {conflicts[0]}"
            )
        return merged

    def withdraw(self, artifact_id: str, payload: dict) -> dict:
        _check_id(artifact_id)
        partner_id = _check_id(require(payload, "partner_id"), "partner_id")
        event_time = require(payload, "event_time")
        parse_iso(event_time)
        with self.store.lock:
            node = self._node(artifact_id)
            if node["withdrawn"]:
                raise Conflict("该稿件已撤回")
            self._assert_rights_holder(node, partner_id)
            record = self.store.append(
                "artifact_withdrawn",
                {"artifact_id": artifact_id, "partner_id": partner_id,
                 "reason": payload.get("reason", "")},
                event_time,
            )
        return {
            "artifact_id": artifact_id,
            "withdrawn": True,
            "event_time": record["event_time"],
            "note": "撤回仅阻止此后的新投递与派生；已签发收据仍然有效",
        }

    # ======================================================================
    # 授权
    # ======================================================================

    def issue_grant(self, payload: dict) -> dict:
        grant_id = _check_id(require(payload, "grant_id"))
        artifact_id = _check_id(require(payload, "artifact_id"))
        partner_id = _check_id(require(payload, "partner_id"), "partner_id")
        grantee = _check_id(require(payload, "grantee_partner_id"), "grantee_partner_id")
        event_time = require(payload, "event_time")
        parse_iso(event_time)
        actions = sorted(set(_as_list(payload.get("actions"), "actions")))
        bad = [a for a in actions if a not in {
            ACTION_PUBLISH, ACTION_TRANSLATE, ACTION_EXCERPT, ACTION_EDIT,
            ACTION_SUBLICENSE,
        }]
        if bad:
            raise InvalidRequest(f"未知授权动作: {bad}")
        regions = _as_list(payload.get("regions"), "regions")
        channels = _as_list(payload.get("channels"), "channels")
        valid_from = require(payload, "valid_from")
        parse_iso(valid_from)
        valid_until = payload.get("valid_until")
        if valid_until:
            if parse_iso(valid_until) <= parse_iso(valid_from):
                raise InvalidRequest("valid_until 必须晚于 valid_from")
        with self.store.lock:
            if grant_id in self._idx["grants"]:
                raise Conflict(f"授权标识已存在: {grant_id}")
            node = self._node(artifact_id)
            self._assert_rights_admin(node, partner_id, actions, regions, channels,
                                      valid_from, valid_until, event_time)
            record = self.store.append(
                "grant_issued",
                {
                    "grant_id": grant_id,
                    "artifact_id": artifact_id,
                    "partner_id": grantee,
                    "issued_by": partner_id,
                    "actions": actions,
                    "regions": regions,
                    "channels": channels,
                    "valid_from": valid_from,
                    "valid_until": valid_until,
                    "must_preserve": payload.get("must_preserve", []),
                },
                event_time,
            )
        return self._grant(grant_id) | {"event_seq": record["seq"]}

    def revoke_grant(self, grant_id: str, payload: dict) -> dict:
        _check_id(grant_id)
        partner_id = _check_id(require(payload, "partner_id"), "partner_id")
        event_time = require(payload, "event_time")
        parse_iso(event_time)
        with self.store.lock:
            grant = self._grant(grant_id)
            issuer_node = self._node(grant["artifact_id"])
            self._assert_rights_holder(issuer_node, partner_id)
            if grant["status"] == "revoked":
                # 撤销幂等：不追加重复事件
                return {"grant_id": grant_id, "status": "revoked",
                        "revoked_event_time": grant["revoked_event_time"],
                        "idempotent": True}
            record = self.store.append(
                "grant_revoked",
                {"grant_id": grant_id, "partner_id": partner_id,
                 "reason": payload.get("reason", "")},
                event_time,
            )
        return {
            "grant_id": grant_id,
            "status": "revoked",
            "revoked_event_time": record["event_time"],
            "note": "撤销仅阻止此后的新使用，不影响已发出的投递收据",
        }

    def _assert_rights_holder(self, node: dict, partner_id: str):
        if node["rights_holder"] != partner_id:
            raise Forbidden(
                f"只有权利方 {node['rights_holder']} 可执行此操作"
            )

    def _assert_rights_admin(self, node: dict, partner_id: str, actions,
                             regions, channels, valid_from, valid_until, at):
        if node["rights_holder"] == partner_id:
            return  # 原始权利方在其作品任意节点上都可授权
        # 其他伙伴必须持有可再许可的有效授权，且新授权范围不超过其既有范围
        matches = self._effective_grants(
            node["artifact_id"], partner_id, ACTION_SUBLICENSE,
            region=WILDCARD, channel=WILDCARD, at=at,
        )
        if not matches:
            raise Forbidden("缺少 sublicense 有效授权，不能向他人授予权利")
        for action in actions:
            if not any(action in g["actions"] for g in matches):
                raise Forbidden(f"不能转授自身未持有的动作: {action}")
        if not self._covered_by(regions, channels, valid_from, valid_until, matches):
            raise Forbidden("新授权的地区/渠道/期限超出授权方持有范围")

    @staticmethod
    def _covered_by(regions, channels, valid_from, valid_until, grants) -> bool:
        def covered(values, key):
            # 每个想要的地区/渠道，至少被一份所持授权覆盖（该授权含通配也算）
            return all(
                any(WILDCARD in g[key] or value in g[key] for g in grants)
                for value in values
            )
        if not covered(regions, "regions") or not covered(channels, "channels"):
            return False
        vf = parse_iso(valid_from)
        vu = parse_iso(valid_until) if valid_until else None
        # 期限必须落入至少一份授权的窗口（通配窗口视为仅由权利方给出，已提前返回）
        windows = [(parse_iso(g["valid_from"]),
                    parse_iso(g["valid_until"]) if g["valid_until"] else None)
                   for g in grants]
        for start, end in windows:
            if vf >= start and (end is None or (vu is not None and vu <= end)):
                return True
        return False

    def _effective_grants(self, artifact_id: str, partner_id: str, action: str,
                          region: str, channel: str, at: str) -> list[dict]:
        """返回使伙伴可在给定地区/渠道/时间执行动作的全部有效授权。

        授权挂在来源图任意祖先节点上即对后继版本生效；任一授权覆盖即允许
        （并集语义），多份授权各自携带的署名/保全义务取并集。
        """
        moment = parse_iso(at)
        result = []
        for node in self._ancestors(artifact_id):
            for grant in self._idx["grants"].values():
                if grant["artifact_id"] != node["artifact_id"]:
                    continue
                if grant["partner_id"] != partner_id or grant["status"] != "active":
                    continue
                if action not in grant["actions"]:
                    continue
                if not _scope_covers(grant["regions"], region):
                    continue
                if not _scope_covers(grant["channels"], channel):
                    continue
                if moment < parse_iso(grant["valid_from"]):
                    continue
                if grant["valid_until"] and moment > parse_iso(grant["valid_until"]):
                    continue
                result.append(grant)
        return result

    def _assert_holds_action(self, artifact_id: str, partner_id: str,
                            action: str, at: str) -> None:
        """伙伴在某时持有某项动作授权即可（不限制地区/渠道）。

        地区与渠道在具体分发时再约束；翻译、节选、编辑等创作行为本身无地域属性。
        """
        node = self._node(artifact_id)
        if node["rights_holder"] == partner_id:
            return
        moment = parse_iso(at)
        for ancestor in self._ancestors(artifact_id):
            for grant in self._idx["grants"].values():
                if (grant["artifact_id"] == ancestor["artifact_id"]
                        and grant["partner_id"] == partner_id
                        and grant["status"] == "active"
                        and action in grant["actions"]
                        and moment >= parse_iso(grant["valid_from"])
                        and (not grant["valid_until"]
                             or moment <= parse_iso(grant["valid_until"]))):
                    return
        raise Forbidden(f"伙伴 {partner_id} 缺少有效的 {action} 授权")

    def _authorize(self, artifact_id, partner_id, action, region, channel, at):
        node = self._node(artifact_id)
        if node["rights_holder"] == partner_id:
            return []  # 原始权利方对自己作品无需自我授权
        grants = self._effective_grants(
            artifact_id, partner_id, action, region, channel, at
        )
        if not grants:
            raise Forbidden(
                f"伙伴 {partner_id} 无权在地区 {region} / 渠道 {channel} "
                f"于 {at} 执行 {action}"
            )
        return grants

    def _assert_usable(self, artifact_id: str):
        for node in self._ancestors(artifact_id):
            if node["withdrawn"]:
                raise Conflict(
                    f"来源链上的 {node['artifact_id']} 已撤回，"
                    f"禁止新的使用（既有收据不受影响）"
                )

    # ======================================================================
    # 附件（按伙伴隔离）
    # ======================================================================

    def register_attachment(self, artifact_id: str, payload: dict) -> dict:
        _check_id(artifact_id)
        attachment_id = _check_id(require(payload, "attachment_id"))
        partner_id = _check_id(require(payload, "partner_id"), "partner_id")
        event_time = require(payload, "event_time")
        parse_iso(event_time)
        scope = _as_list(payload.get("scope", []), "scope")
        if WILDCARD not in scope:
            scope = [_check_id(s, "scope") for s in scope]
        with self.store.lock:
            self._node(artifact_id)
            key = attachment_key(artifact_id, attachment_id)
            if key in self._idx["attachments"]:
                raise Conflict(f"附件已存在: {attachment_id}")
            record = self.store.append(
                "attachment_registered",
                {
                    "attachment_id": key,
                    "artifact_id": artifact_id,
                    "filename": require(payload, "filename"),
                    "media_type": payload.get("media_type", "application/octet-stream"),
                    "sha256": payload.get("sha256"),
                    "size": payload.get("size"),
                    "scope": scope,
                    "partner_id": partner_id,
                },
                event_time,
            )
        return self._idx["attachments"][key] | {"event_seq": record["seq"]}

    def store_attachment_bytes(self, artifact_id: str, attachment_id: str,
                               partner_id: str, data: bytes) -> dict:
        from .models import sha256_hex
        key = attachment_key(artifact_id, attachment_id)
        with self.store.lock:
            meta = self._idx["attachments"].get(key)
            if meta is None:
                raise NotFound("请先登记附件元数据")
            if meta["owner_partner"] != partner_id:
                raise Forbidden("只有附件所有伙伴可以上传内容")
            digest = sha256_hex(data)
            if meta.get("sha256") and meta["sha256"] != digest:
                raise Conflict("附件内容与登记的 SHA-256 不一致")
            if meta.get("size") and meta["size"] != len(data):
                raise Conflict("附件内容与登记的大小不一致")
            path = self.store.attachment_path(artifact_id, attachment_id)
            with open(path, "wb") as fh:
                fh.write(data)
            self.store.append(
                "attachment_content_stored",
                {"attachment_id": key, "sha256": digest, "size": len(data)},
                now_iso(),
            )
            return {"attachment_id": key, "stored": True,
                    "size": len(data), "sha256": digest}

    def read_attachment_bytes(self, artifact_id: str, attachment_id: str,
                              partner_id: str) -> tuple[dict, bytes]:
        _check_id(artifact_id)
        _check_id(attachment_id)
        _check_id(partner_id, "partner_id")
        key = attachment_key(artifact_id, attachment_id)
        with self.store.lock:
            meta = self._idx["attachments"].get(key)
            if meta is None:
                raise NotFound("附件不存在")
            if not self._attachment_visible(meta, partner_id):
                # 对无权方等同不存在，避免暴露附件存在性
                raise NotFound("附件不存在")
            path = self.store.attachment_path(artifact_id, attachment_id)
            try:
                with open(path, "rb") as fh:
                    return meta, fh.read()
            except FileNotFoundError as exc:
                raise NotFound("附件内容尚未上传") from exc

    def list_attachments(self, artifact_id: str, partner_id: str) -> list[dict]:
        _check_id(artifact_id)
        _check_id(partner_id, "partner_id")
        with self.store.lock:
            self._node(artifact_id)
            return [
                m for m in self._idx["attachments"].values()
                if m["artifact_id"] == artifact_id
                and self._attachment_visible(m, partner_id)
            ]

    @staticmethod
    def _attachment_visible(meta: dict, partner_id: str) -> bool:
        if meta["owner_partner"] == partner_id:
            return True
        return WILDCARD in meta["scope"] or partner_id in meta["scope"]

    # ======================================================================
    # 投递：排队、重启续发、幂等回执
    # ======================================================================

    def enqueue_delivery(self, payload: dict) -> dict:
        delivery_id = _check_id(require(payload, "delivery_id"))
        artifact_id = _check_id(require(payload, "artifact_id"))
        partner_id = _check_id(require(payload, "partner_id"), "partner_id")
        region = require(payload, "region")
        channel = require(payload, "channel")
        event_time = require(payload, "event_time")
        parse_iso(event_time)
        with self.store.lock:
            existing = self._idx["deliveries"].get(delivery_id)
            if existing is not None:
                # 幂等：同一稳定标识重复提交不产生新事件、不重复计数
                return existing | {"idempotent": True}
            node = self._node(artifact_id)
            # 只能投递作品的唯一当前首节点；多首节点必须先显式合并
            heads = self._idx["works"][node["work_id"]]["heads"]
            if heads != [artifact_id]:
                raise Conflict(
                    f"该作品存在并发版本 {sorted(heads)}，必须显式合并后才能投递"
                )
            self._assert_usable(artifact_id)
            grants = self._authorize(
                artifact_id, partner_id, ACTION_PUBLISH,
                region=region, channel=channel, at=event_time,
            )
            attachment_ids = payload.get("attachment_ids", [])
            for raw in attachment_ids:
                key = attachment_key(artifact_id, raw)
                meta = self._idx["attachments"].get(key)
                if meta is None or not self._attachment_visible(meta, partner_id):
                    raise Forbidden(f"附件无权随投递分发: {raw}")
            record = self.store.append(
                "delivery_queued",
                {
                    "delivery_id": delivery_id,
                    "artifact_id": artifact_id,
                    "partner_id": partner_id,
                    "region": region,
                    "channel": channel,
                    "attachment_ids": attachment_ids,
                    "grants_relied_upon": [g["grant_id"] for g in grants],
                },
                event_time,
            )
        # 入队后立即尝试发送；发送器暂不可用时保持 queued，等待续发
        self.drain_queue()
        return self._delivery(delivery_id) | {"event_seq": record["seq"]}

    def drain_queue(self) -> dict:
        """尝试发送所有待发投递。

        服务启动与每次入队后调用，重启后自动续发。投递器在状态锁内被调用，
        与“状态仍为 queued”的检查构成原子区间，因此同一条投递不会被并发
        线程发送两次。投递器实现必须快速返回且不得回调本服务；暂不可用时
        抛 DispatchRetry，投递保持 queued 等待后续续发。
        """
        sent, failed = [], []
        with self._dispatch_lock:
            while True:
                progressed = False
                with self.store.lock:
                    queued = [
                        d for d in list(self._idx["deliveries"].values())
                        if d["status"] == "queued"
                    ]
                for candidate in queued:
                    with self.store.lock:
                        cur = self._idx["deliveries"].get(candidate["delivery_id"])
                        if cur is None or cur["status"] != "queued":
                            continue  # 已被其他线程处理
                        node = self._node(cur["artifact_id"])
                        try:
                            self.dispatcher(cur, node)
                        except DispatchRetry:
                            self.store.append(
                                "delivery_dispatch_failed",
                                {"delivery_id": cur["delivery_id"]},
                                now_iso(),
                            )
                            failed.append(cur["delivery_id"])
                            continue
                        receipt = self._build_receipt(
                            cur, node, self.store.head_hash()
                        )
                        record = self.store.append(
                            "delivery_sent",
                            {"delivery_id": cur["delivery_id"],
                             "receipt": receipt},
                            now_iso(),
                        )
                        sent.append(cur["delivery_id"])
                        progressed = True
                if not progressed:
                    break
        return {"sent": sent, "pending": failed}

    def _build_receipt(self, delivery: dict, node: dict,
                       chain_anchor: str | None) -> dict:
        return {
            "receipt_id": f"receipt:{delivery['delivery_id']}",
            "delivery_id": delivery["delivery_id"],
            "artifact_id": node["artifact_id"],
            "work_id": node["work_id"],
            "content_hash": node["content_hash"],
            "partner_id": delivery["partner_id"],
            "region": delivery["region"],
            "channel": delivery["channel"],
            "grants_relied_upon": delivery["grants_relied_upon"],
            # 凭证锚定到发出时刻的审计链头：撤回/撤销后收据仍可独立验证
            "chain_anchor": chain_anchor,
        }

    def acknowledge(self, delivery_id: str, payload: dict) -> dict:
        """接收方回执确认。重复回执保持幂等，不增加任何次数。"""
        _check_id(delivery_id)
        partner_id = _check_id(require(payload, "partner_id"), "partner_id")
        event_time = require(payload, "event_time")
        parse_iso(event_time)
        with self.store.lock:
            delivery = self._delivery(delivery_id)
            if delivery["partner_id"] != partner_id:
                raise Forbidden("只有投递接收方可以确认")
            if delivery["status"] != "sent":
                raise Conflict("投递尚未发出，暂不能确认")
            duplicate = delivery["acknowledged"]
            if not duplicate:
                self.store.append(
                    "delivery_acknowledged",
                    {"delivery_id": delivery_id, "partner_id": partner_id},
                    event_time,
                )
            result = self._delivery(delivery_id)
            return result | {"idempotent": duplicate}

    def get_receipt(self, delivery_id: str) -> dict:
        _check_id(delivery_id)
        with self.store.lock:
            delivery = self._delivery(delivery_id)
            if delivery["receipt"] is None:
                raise NotFound("投递尚未发出，尚不存在收据")
            node = self._node(delivery["artifact_id"])
            # 收据是已发生事实的凭证：授权此后被撤销/撤回不使收据失效，
            # 但会影响“当前是否还能继续使用”。
            work = self._idx["works"][node["work_id"]]
            current_head_ids = list(work["heads"])
            current_head = (
                self._node(current_head_ids[0])
                if len(current_head_ids) == 1 else None
            )
            current = self.rights_report(
                delivery["artifact_id"], delivery["partner_id"],
                delivery["region"], delivery["channel"], at=now_iso(),
            )
            receipt = dict(delivery["receipt"])
            # 投递版本是否仍是作品当前版本，以及当前版本指纹
            receipt["delivered_version_is_current"] = (
                current_head_ids == [node["artifact_id"]]
            )
            receipt["current_version"] = (
                {"artifact_id": current_head["artifact_id"],
                 "content_hash": current_head["content_hash"]}
                if current_head else {"heads": current_head_ids}
            )
            receipt["content_hash_matches_current"] = (
                current_head is not None
                and current_head["content_hash"] == receipt["content_hash"]
            )
            # “当前能否继续使用所投递版本”按核验时刻判断：权利仍有效
            # 且投递版本仍是作品当前版本（转载须基于当前版本）；
            # 收据本身作为历史凭证始终保留
            receipt["currently_usable"] = (
                current["permitted"]
                and receipt["delivered_version_is_current"]
            )
            receipt["current_rights"] = {
                "permitted": current["permitted"],
                "allowed_actions": current["allowed_actions"],
                "withdrawn_ancestors": current["withdrawn_ancestors"],
            }
            receipt["acknowledged"] = delivery["acknowledged"]
            receipt["chain"] = self.store.verify_chain()
            return receipt

    # ======================================================================
    # 核验视图（任何接收方可读）
    # ======================================================================

    def artifact_view(self, artifact_id: str) -> dict:
        with self.store.lock:
            node = self._node(artifact_id)
            work = self._idx["works"][node["work_id"]]
            return {
                "artifact_id": node["artifact_id"],
                "kind": node["kind"],
                "work_id": node["work_id"],
                "title": node["title"],
                "language": node["language"],
                "authors": node["authors"],
                "context_note": node["context_note"],
                "body": node["body"],
                "content_hash": node["content_hash"],
                "parents": [
                    {"artifact_id": p, "relation": self._node(p)["relation"]}
                    for p in node["parents"]
                ],
                "relation": node["relation"],
                "rights_holder": node["rights_holder"],
                "withdrawn": node["withdrawn"],
                "event_time": node["event_time"],
                "received_time": node["received_time"],
                "is_current": work["heads"] == [artifact_id],
                "current_heads": list(work["heads"]),
            }

    def provenance(self, artifact_id: str) -> dict:
        with self.store.lock:
            self._node(artifact_id)
            chain = []
            for node in self._ancestors(artifact_id):
                chain.append({
                    "artifact_id": node["artifact_id"],
                    "kind": node["kind"],
                    "work_id": node["work_id"],
                    "title": node["title"],
                    "language": node["language"],
                    "authors": node["authors"],
                    "context_note": node["context_note"],
                    "content_hash": node["content_hash"],
                    "parents": node["parents"],
                    "relation_to_parents": node["relation"],
                    "withdrawn": node["withdrawn"],
                })
            originals = self._originals(artifact_id)
            return {
                "artifact_id": artifact_id,
                "originals": [
                    {"artifact_id": o["artifact_id"], "title": o["title"],
                     "content_hash": o["content_hash"],
                     "authors": o["authors"]}
                    for o in originals
                ],
                "relationship_to_original": _relationship_lines(chain),
                "lineage": chain,
            }

    def work_view(self, work_id: str) -> dict:
        with self.store.lock:
            work = self._idx["works"].get(work_id)
            if work is None:
                raise NotFound(f"作品不存在: {work_id}")
            return {
                "work_id": work_id,
                "original_id": work["original_id"],
                "current_heads": list(work["heads"]),
                "diverged": len(work["heads"]) > 1,
                "nodes": [
                    self.artifact_view(aid)
                    for aid in work["nodes"]
                ],
            }

    def rights_report(self, artifact_id: str, partner_id: str,
                      region: str = WILDCARD, channel: str = WILDCARD,
                      at: str | None = None) -> dict:
        _check_id(artifact_id)
        _check_id(partner_id, "partner_id")
        with self.store.lock:
            node = self._node(artifact_id)
            work = self._idx["works"][node["work_id"]]
            at = at or node["event_time"]
            parse_iso(at)
            grants = self._effective_grants(
                artifact_id, partner_id, ACTION_PUBLISH,
                region=region, channel=channel, at=at,
            )
            is_rights_holder = node["rights_holder"] == partner_id
            action_set = set()
            notices = []
            for g in self._matching_grants_any_action(
                artifact_id, partner_id, region, channel, at
            ):
                action_set.update(g["actions"])
                for notice in g["must_preserve"]:
                    if notice not in notices:
                        notices.append(notice)
            if is_rights_holder:
                # 原始权利方对自己作品天然持有全部动作
                action_set.update({
                    ACTION_PUBLISH, ACTION_TRANSLATE, ACTION_EXCERPT,
                    ACTION_EDIT, ACTION_SUBLICENSE,
                })
            withdrawn_on_path = [
                n["artifact_id"] for n in self._ancestors(artifact_id)
                if n["withdrawn"]
            ]
            return {
                "artifact_id": artifact_id,
                "partner_id": partner_id,
                "region": region,
                "channel": channel,
                "at": at,
                "is_rights_holder": is_rights_holder,
                "permitted": (is_rights_holder or bool(grants))
                and not withdrawn_on_path,
                "is_current": work["heads"] == [artifact_id],
                "current_heads": list(work["heads"]),
                "allowed_actions": sorted(action_set),
                "modification_scope": _modification_scope(action_set),
                "grants": [
                    {
                        "grant_id": g["grant_id"],
                        "attached_to": g["artifact_id"],
                        "actions": g["actions"],
                        "regions": g["regions"],
                        "channels": g["channels"],
                        "valid_from": g["valid_from"],
                        "valid_until": g["valid_until"],
                    }
                    for g in self._matching_grants_any_action(
                        artifact_id, partner_id, region, channel, at
                    )
                ],
                "attribution_duty": self._attribution_duty(artifact_id),
                "required_notices": notices,
                "withdrawn_ancestors": withdrawn_on_path,
            }

    def _matching_grants_any_action(self, artifact_id, partner_id,
                                    region, channel, at) -> list[dict]:
        moment = parse_iso(at)
        out = []
        for node in self._ancestors(artifact_id):
            for grant in self._idx["grants"].values():
                if (grant["artifact_id"] == node["artifact_id"]
                        and grant["partner_id"] == partner_id
                        and grant["status"] == "active"
                        and _scope_covers(grant["regions"], region)
                        and _scope_covers(grant["channels"], channel)
                        and moment >= parse_iso(grant["valid_from"])
                        and (not grant["valid_until"]
                             or moment <= parse_iso(grant["valid_until"]))):
                    out.append(grant)
        return out

    def _attribution_duty(self, artifact_id: str) -> dict:
        nodes = self._ancestors(artifact_id)
        authors, seen = [], set()
        for node in nodes:
            for author in node["authors"]:
                key = canonical_json(author)
                if key not in seen:
                    seen.add(key)
                    authors.append(author)
        current = self._node(artifact_id)
        return {
            "must_attribute_all": authors,
            "current_version_authors": current["authors"],
            "context_note": current["context_note"],
            "declare_derivation": current["kind"] in DERIVED_KINDS,
        }


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _scope_covers(allowed: list[str], value: str) -> bool:
    return WILDCARD in allowed or value in allowed


def _clean_authors(authors) -> list[dict]:
    if authors is None:
        return []
    if not isinstance(authors, list):
        raise InvalidRequest("authors 必须是数组")
    out = []
    for a in authors:
        if isinstance(a, str):
            out.append({"name": a})
        elif isinstance(a, dict) and a.get("name"):
            out.append({"name": str(a["name"]),
                        "role": a.get("role", "作者")})
        else:
            raise InvalidRequest("authors 每项须为字符串或 {name, role}")
    return out


def _union_authors(nodes: list[dict]) -> list[dict]:
    out, seen = [], set()
    for node in nodes:
        for author in node["authors"]:
            key = canonical_json(author)
            if key not in seen:
                seen.add(key)
                out.append(author)
    return out


def _modification_scope(actions: set[str]) -> dict:
    return {
        "verbatim_publish_only": ACTION_PUBLISH in actions
        and ACTION_EDIT not in actions,
        "may_edit": ACTION_EDIT in actions,
        "may_translate": ACTION_TRANSLATE in actions,
        "may_excerpt": ACTION_EXCERPT in actions,
        "may_sublicense": ACTION_SUBLICENSE in actions,
    }


def _relationship_lines(chain: list[dict]) -> list[str]:
    rel = {
        REL_TRANSLATED_FROM: "译自",
        REL_EXCERPTED_FROM: "节选自",
        REL_REVISED_FROM: "修订自",
        REL_MERGED_WITH: "合并自",
    }
    lines = []
    by_id = {n["artifact_id"]: n for n in chain}
    for node in chain:
        for parent in node["parents"]:
            p = by_id.get(parent)
            if p:
                relation = (node["relation_to_parents"]
                            if len(node["parents"]) == 1 else REL_MERGED_WITH)
                lines.append(
                    f"{node['artifact_id']}（{node['kind']}）"
                    f"{rel.get(relation, '源自')} "
                    f"{parent}（{p['kind']}）"
                )
    return lines


def attachment_key(artifact_id: str, attachment_id: str) -> str:
    return f"{artifact_id}/{attachment_id}"




def three_way_merge(base_text: str, left_text: str, right_text: str) -> tuple[str, list[int]]:
    """diff3 风格的行级三方合并。

    分别求左、右两侧相对共同祖先 base 的修改区间，按 base 坐标扫描：

    * 两侧都未改：保留 base；
    * 仅一侧修改：取该侧结果（含插入/删除）；
    * 两侧修改区间相交：若两侧结果相同取一次，否则产生冲突块
      （<<<<<<< / ======= / >>>>>>>），调用方解决后须显式提交合并版本。

    返回 (合并文本, 冲突块起始行号列表)。
    """
    base = base_text.splitlines(keepends=True)
    left = left_text.splitlines(keepends=True)
    right = right_text.splitlines(keepends=True)

    l_hunks = _change_hunks(base, left)
    r_hunks = _change_hunks(base, right)

    out: list[str] = []
    conflicts: list[int] = []
    i = 0  # base 扫描位置
    pl = pr = 0
    while pl < len(l_hunks) or pr < len(r_hunks):
        L = l_hunks[pl] if pl < len(l_hunks) else None
        R = r_hunks[pr] if pr < len(r_hunks) else None
        if L is None:
            out.extend(base[i:R[0]])
            out.extend(right[R[2]:R[3]])
            i, pr = R[1], pr + 1
            continue
        if R is None:
            out.extend(base[i:L[0]])
            out.extend(left[L[2]:L[3]])
            i, pl = L[1], pl + 1
            continue
        if L[0] < R[0] and R[0] >= L[1]:
            # 左侧修改在前，与右侧不相交
            out.extend(base[i:L[0]])
            out.extend(left[L[2]:L[3]])
            i, pl = L[1], pl + 1
            continue
        if R[0] < L[0] and L[0] >= R[1]:
            # 右侧修改在前，与左侧不相交
            out.extend(base[i:R[0]])
            out.extend(right[R[2]:R[3]])
            i, pr = R[1], pr + 1
            continue
        # 两侧修改区间（含同起点）严格相交：吸收相连区间为一组
        start = min(L[0], R[0])
        end = max(L[1], R[1])
        l_used = [L]
        r_used = [R]
        pl += 1
        pr += 1
        grew = True
        while grew:
            grew = False
            while pl < len(l_hunks) and l_hunks[pl][0] < end:
                l_used.append(l_hunks[pl])
                end = max(end, l_hunks[pl][1])
                pl += 1
                grew = True
            while pr < len(r_hunks) and r_hunks[pr][0] < end:
                r_used.append(r_hunks[pr])
                end = max(end, r_hunks[pr][1])
                pr += 1
                grew = True
        out.extend(base[i:start])
        left_chunk = _chunk_from_used(base, left, l_used, start, end)
        right_chunk = _chunk_from_used(base, right, r_used, start, end)
        if left_chunk == right_chunk:
            out.extend(left_chunk)  # 双方改动结果一致
        else:
            conflicts.append(len(out) + 1)
            out.append("<<<<<<< 并发修订 A\n")
            out.extend(_ensure_newline(left_chunk))
            out.append("=======\n")
            out.extend(_ensure_newline(right_chunk))
            out.append(">>>>>>> 并发修订 B\n")
        i = end
    out.extend(base[i:])
    return "".join(out), conflicts


def _change_hunks(base: list[str], side: list[str]):
    """一侧相对 base 的连续修改区间：(base_start, base_end, side_start, side_end)。

    相邻的非相等操作合并为一个 hunk。
    """
    matcher = difflib.SequenceMatcher(None, base, side, autojunk=False)
    hunks = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if hunks and hunks[-1][1] == i1 and hunks[-1][3] == j1:
            prev = hunks[-1]
            hunks[-1] = (prev[0], i2, prev[2], j2)
        else:
            hunks.append((i1, i2, j1, j2))
    return hunks


def _chunk_from_used(base, side, used_hunks, start: int, end: int) -> list[str]:
    """重建一侧在冲突组 [start, end) 内的最终行（含插入内容与组内公共行）。"""
    result: list[str] = []
    pos = start
    for i1, i2, j1, j2 in used_hunks:
        if i1 > pos:
            result.extend(base[pos:i1])
        result.extend(side[j1:j2])
        pos = max(pos, i2)
    if pos < end:
        result.extend(base[pos:end])
    return result


def _ensure_newline(lines: list[str]) -> list[str]:
    if lines and not lines[-1].endswith("\n"):
        return lines[:-1] + [lines[-1] + "\n"]
    return lines
