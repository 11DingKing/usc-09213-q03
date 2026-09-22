"""稿件交换的领域逻辑：权利与来源图、授权流转、分发回执与核验。

约定：
- 所有写操作同时记录调用方提供的事件时间（event_time）与服务端接收时间
  （recorded_at），并追加到只增不改的审计日志。
- 业务身份（稿件、授权、凭证、分发、回执、附件）均由调用方提供稳定标识。
- 并发修订：基于旧版本的修改只产生分支版本，必须显式合并才能成为头版本。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

WORK_KINDS = ("original", "translation", "excerpt", "reedit")
DERIVATIVE_KINDS = ("translation", "excerpt", "reedit")


class StoreError(Exception):
    """业务规则错误（默认映射 HTTP 400）。"""

    status = 400
    code = "bad_request"


class NotFound(StoreError):
    status = 404
    code = "not_found"


class Conflict(StoreError):
    status = 409
    code = "conflict"


class Forbidden(StoreError):
    status = 403
    code = "forbidden"


# ---------------------------------------------------------------- 基础工具

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value, field):
    if not isinstance(value, str) or not value:
        raise StoreError(f"字段 {field} 缺失或不是字符串")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise StoreError(f"字段 {field} 不是合法的 ISO 时间: {value!r}")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _str_list(value, field):
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise StoreError(f"字段 {field} 必须是字符串数组")
    return list(value)


def _hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _audit(conn, entity, entity_id, action, detail, event_time):
    conn.execute(
        "INSERT INTO audit_log (entity, entity_id, action, detail, event_time,"
        " recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
        (entity, entity_id, action,
         json.dumps(detail, ensure_ascii=False, sort_keys=True), event_time, _now()),
    )


# ---------------------------------------------------------------- 稿件与版本

def get_work(conn, work_id):
    row = conn.execute("SELECT * FROM works WHERE work_id = ?", (work_id,)).fetchone()
    if row is None:
        raise NotFound(f"稿件不存在: {work_id}")
    return dict(row)


def get_version(conn, work_id, version_no):
    row = conn.execute(
        "SELECT * FROM versions WHERE work_id = ? AND version_no = ?",
        (work_id, version_no)).fetchone()
    if row is None:
        raise NotFound(f"版本不存在: {work_id}@{version_no}")
    return dict(row)


def _next_version_no(conn, work_id):
    row = conn.execute(
        "SELECT COALESCE(MAX(version_no), 0) + 1 AS n FROM versions WHERE work_id = ?",
        (work_id,)).fetchone()
    return row["n"]


def _insert_version(conn, *, work_id, version_no, parents, body, context_note,
                    byline, editor_id, is_merge, event_time):
    conn.execute(
        "INSERT INTO versions (work_id, version_no, parents, body, context_note,"
        " byline, editor_id, is_merge, content_hash, event_time, recorded_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (work_id, version_no, json.dumps(parents), body, context_note, byline,
         editor_id, 1 if is_merge else 0, _hash(body), event_time, _now()))


def create_work(conn, *, work_id, kind, language, parent_id=None, body,
                context_note="", byline, editor_id, event_time, partner_id=None):
    """登记稿件并建立来源图节点；衍生稿件必须指明父稿件。

    若以伙伴身份创建译稿/节选/再编辑，须持有父稿件覆盖该修改范围的授权。
    """
    _parse_time(event_time, "event_time")
    if kind not in WORK_KINDS:
        raise StoreError(f"未知稿件类型: {kind!r}")
    if kind == "original" and parent_id:
        raise StoreError("原稿不得指定父稿件")
    if kind != "original":
        if not parent_id:
            raise StoreError(f"{kind} 必须指定父稿件以建立来源关系")
        parent = get_work(conn, parent_id)
        if partner_id is not None:
            _assert_derivative_allowed(conn, parent, kind, partner_id, event_time)
    with conn:
        try:
            conn.execute(
                "INSERT INTO works (work_id, kind, language, parent_id, head_version,"
                " created_event_time, recorded_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
                (work_id, kind, language, parent_id, event_time, _now()))
        except sqlite3.IntegrityError:
            raise Conflict(f"稿件标识已存在: {work_id}")
        _insert_version(conn, work_id=work_id, version_no=1, parents=[], body=body,
                        context_note=context_note, byline=byline,
                        editor_id=editor_id, is_merge=False, event_time=event_time)
        _audit(conn, "work", work_id, "create",
               {"kind": kind, "language": language, "parent_id": parent_id,
                "editor_id": editor_id}, event_time)
    return get_work(conn, work_id)


def add_version(conn, *, work_id, editor_id, base_version, body,
                context_note=None, byline=None, event_time):
    """基于指定版本提交修订。

    基版本即当前头版本时直接快进；否则产生分支版本，头版本不变，
    需要显式合并才能生效——并发修订不会被静默覆盖。
    """
    _parse_time(event_time, "event_time")
    work = get_work(conn, work_id)
    base = get_version(conn, work_id, base_version)
    head = work["head_version"]
    version_no = _next_version_no(conn, work_id)
    is_head = base_version == head
    with conn:
        _insert_version(
            conn, work_id=work_id, version_no=version_no, parents=[base_version],
            body=body,
            context_note=base["context_note"] if context_note is None else context_note,
            byline=base["byline"] if byline is None else byline,
            editor_id=editor_id, is_merge=False, event_time=event_time)
        if is_head:
            conn.execute("UPDATE works SET head_version = ? WHERE work_id = ?",
                         (version_no, work_id))
        _audit(conn, "work", work_id, "revise",
               {"version_no": version_no, "base_version": base_version,
                "editor_id": editor_id, "fast_forward": is_head}, event_time)
    return {"work_id": work_id, "version_no": version_no, "is_head": is_head,
            "requires_merge": not is_head}


def merge_versions(conn, *, work_id, editor_id, source_version, body,
                   context_note=None, byline=None, event_time):
    """把分支版本显式合并进头版本，产生带两个父版本的合并版本。"""
    _parse_time(event_time, "event_time")
    work = get_work(conn, work_id)
    head = work["head_version"]
    if source_version == head:
        raise Conflict("源版本已是当前头版本，无需合并")
    get_version(conn, work_id, source_version)
    head_row = get_version(conn, work_id, head)
    if _is_ancestor(conn, work_id, head, source_version):
        raise Conflict(f"版本 {source_version} 已并入头版本，无需重复合并")
    version_no = _next_version_no(conn, work_id)
    with conn:
        _insert_version(
            conn, work_id=work_id, version_no=version_no,
            parents=[head, source_version], body=body,
            context_note=(head_row["context_note"] if context_note is None
                          else context_note),
            byline=head_row["byline"] if byline is None else byline,
            editor_id=editor_id, is_merge=True, event_time=event_time)
        conn.execute("UPDATE works SET head_version = ? WHERE work_id = ?",
                     (version_no, work_id))
        _audit(conn, "work", work_id, "merge",
               {"version_no": version_no, "parents": [head, source_version],
                "editor_id": editor_id}, event_time)
    return {"work_id": work_id, "version_no": version_no, "is_head": True,
            "merged": [head, source_version]}


def _is_ancestor(conn, work_id, from_no, target_no):
    """target_no 是否已经是 from_no 的祖先（沿父版本链回溯）。"""
    seen, stack = set(), [from_no]
    while stack:
        no = stack.pop()
        if no == target_no:
            return True
        if no in seen:
            continue
        seen.add(no)
        stack.extend(json.loads(get_version(conn, work_id, no)["parents"]))
    return False


# ---------------------------------------------------------------- 授权与凭证

def _license_dict(row):
    d = dict(row)
    d["regions"] = json.loads(d["regions"])
    d["channels"] = json.loads(d["channels"])
    d["permissions"] = json.loads(d["permissions"])
    d["status"] = "revoked" if d["revoked_at"] else "active"
    return d


def get_license(conn, license_id):
    row = conn.execute("SELECT * FROM licenses WHERE license_id = ?",
                       (license_id,)).fetchone()
    if row is None:
        raise NotFound(f"授权不存在: {license_id}")
    return _license_dict(row)


def _find_license(conn, work_id, partner_id):
    row = conn.execute(
        "SELECT * FROM licenses WHERE work_id = ? AND partner_id = ?"
        " ORDER BY recorded_at DESC LIMIT 1",
        (work_id, partner_id)).fetchone()
    return dict(row) if row is not None else None


def _license_time_ok(lic, moment):
    """授权在某时刻是否生效（未撤回且在期限内）。"""
    if lic["revoked_at"] and moment >= _parse_time(lic["revoked_at"], "revoked_at"):
        return False
    return (_parse_time(lic["valid_from"], "valid_from") <= moment
            <= _parse_time(lic["valid_until"], "valid_until"))


def _assert_derivative_allowed(conn, parent, kind, partner_id, event_time):
    """伙伴基于父稿件创建衍生稿件前，校验其授权的可修改范围。"""
    lic = _find_license(conn, parent["work_id"], partner_id)
    if lic is None:
        raise Forbidden(f"伙伴 {partner_id} 未持有父稿件 {parent['work_id']} 的授权")
    if not _license_time_ok(lic, _parse_time(event_time, "event_time")):
        raise Forbidden("授权在事件时间不在有效期内或已撤回")
    if not json.loads(lic["permissions"]).get(kind, False):
        raise Forbidden(f"授权的可修改范围不包含 {kind}")


def create_license(conn, *, license_id, work_id, partner_id, regions, channels,
                   valid_from, valid_until, permissions=None, attribution_text="",
                   event_time):
    """授予伙伴按地区、渠道与期限受限的使用权，并写明可修改范围。"""
    get_work(conn, work_id)
    start = _parse_time(valid_from, "valid_from")
    end = _parse_time(valid_until, "valid_until")
    _parse_time(event_time, "event_time")
    if end < start:
        raise StoreError("valid_until 早于 valid_from")
    unknown = set(permissions or {}) - set(DERIVATIVE_KINDS)
    if unknown:
        raise StoreError(f"未知授权权限项: {sorted(unknown)}")
    permissions = {k: bool((permissions or {}).get(k, False))
                   for k in DERIVATIVE_KINDS}
    regions = _str_list(regions, "regions")
    channels = _str_list(channels, "channels")
    with conn:
        try:
            conn.execute(
                "INSERT INTO licenses (license_id, work_id, partner_id, regions,"
                " channels, valid_from, valid_until, permissions, attribution_text,"
                " revoked_at, created_event_time, recorded_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                (license_id, work_id, partner_id, json.dumps(regions),
                 json.dumps(channels), start.isoformat(), end.isoformat(),
                 json.dumps(permissions), attribution_text, event_time, _now()))
        except sqlite3.IntegrityError:
            raise Conflict(f"授权标识已存在: {license_id}")
        _audit(conn, "license", license_id, "grant",
               {"work_id": work_id, "partner_id": partner_id, "regions": regions,
                "channels": channels, "valid_from": start.isoformat(),
                "valid_until": end.isoformat(), "permissions": permissions},
               event_time)
    return get_license(conn, license_id)


def revoke_license(conn, *, license_id, event_time):
    """撤回授权：只阻止撤回时点之后的未来使用，已发布凭证保留且继续有效。"""
    moment = _parse_time(event_time, "event_time")
    lic = get_license(conn, license_id)
    if lic["revoked_at"]:
        raise Conflict("授权已撤回，不可重复操作")
    with conn:
        conn.execute("UPDATE licenses SET revoked_at = ? WHERE license_id = ?",
                     (moment.isoformat(), license_id))
        _audit(conn, "license", license_id, "revoke",
               {"revoked_at": moment.isoformat()}, event_time)
    return get_license(conn, license_id)


def check_license(conn, *, license_id, region=None, channel=None, at):
    """核验授权在某时刻、某地区、某渠道是否允许使用。"""
    lic = get_license(conn, license_id)
    moment = _parse_time(at, "at")
    reasons = []
    if lic["revoked_at"] and moment >= _parse_time(lic["revoked_at"], "revoked_at"):
        reasons.append("revoked")
    if moment < _parse_time(lic["valid_from"], "valid_from"):
        reasons.append("not_yet_valid")
    if moment > _parse_time(lic["valid_until"], "valid_until"):
        reasons.append("expired")
    if region is not None and lic["regions"] and region not in lic["regions"]:
        reasons.append("region_not_covered")
    if channel is not None and lic["channels"] and channel not in lic["channels"]:
        reasons.append("channel_not_covered")
    return {"license_id": license_id, "allowed": not reasons, "reasons": reasons,
            "status": lic["status"]}


def record_voucher(conn, *, voucher_id, license_id, version_no, region, channel,
                   published_at, event_time):
    """登记发布凭证：证明某次发布发生在授权有效覆盖的时点。

    凭证一旦登记即永久保留；授权撤回不影响已登记凭证的效力。
    """
    lic = get_license(conn, license_id)
    get_version(conn, lic["work_id"], version_no)
    _parse_time(event_time, "event_time")
    check = check_license(conn, license_id=license_id, region=region,
                          channel=channel, at=published_at)
    if not check["allowed"]:
        raise Conflict("发布时点授权未覆盖该使用，无法登记发布凭证: "
                       + ",".join(check["reasons"]))
    with conn:
        try:
            conn.execute(
                "INSERT INTO vouchers (voucher_id, license_id, version_no, region,"
                " channel, published_at, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (voucher_id, license_id, version_no, region, channel,
                 _parse_time(published_at, "published_at").isoformat(), _now()))
        except sqlite3.IntegrityError:
            raise Conflict(f"凭证标识已存在: {voucher_id}")
        _audit(conn, "license", license_id, "publish",
               {"voucher_id": voucher_id, "version_no": version_no}, event_time)
    return get_voucher(conn, voucher_id)


def get_voucher(conn, voucher_id):
    row = conn.execute("SELECT * FROM vouchers WHERE voucher_id = ?",
                       (voucher_id,)).fetchone()
    if row is None:
        raise NotFound(f"发布凭证不存在: {voucher_id}")
    d = dict(row)
    d["honored"] = True
    return d


def list_vouchers(conn, *, license_id):
    get_license(conn, license_id)
    rows = conn.execute(
        "SELECT * FROM vouchers WHERE license_id = ? ORDER BY recorded_at",
        (license_id,)).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["honored"] = True
        out.append(d)
    return out


# ---------------------------------------------------------------- 分发与回执

def create_deliveries(conn, *, work_id, partner_ids, event_time):
    """把当前头版本分发给伙伴，写入持久化待发队列。

    分发以授权为前提：伙伴须持有未撤回且在期限内的授权。
    分发标识由稿件、伙伴与版本决定，重复分发同一版本不会产生新记录。
    """
    work = get_work(conn, work_id)
    _parse_time(event_time, "event_time")
    partner_ids = _str_list(partner_ids, "partner_ids")
    if not partner_ids:
        raise StoreError("partner_ids 不能为空")
    now = _parse_time(_now(), "now")
    results = []
    with conn:
        for partner_id in dict.fromkeys(partner_ids):
            lic = _find_license(conn, work_id, partner_id)
            if lic is None:
                raise Conflict(f"伙伴 {partner_id} 未持有稿件 {work_id} 的授权，禁止分发")
            if not _license_time_ok(lic, now):
                raise Conflict(f"伙伴 {partner_id} 的授权已撤回或不在有效期内，禁止分发")
            delivery_id = f"{work_id}:{partner_id}:v{work['head_version']}"
            cur = conn.execute(
                "INSERT OR IGNORE INTO deliveries (delivery_id, work_id, version_no,"
                " partner_id, status, dispatched_at, created_event_time, recorded_at)"
                " VALUES (?, ?, ?, ?, 'pending', NULL, ?, ?)",
                (delivery_id, work_id, work["head_version"], partner_id,
                 event_time, _now()))
            created = cur.rowcount > 0
            if created:
                _audit(conn, "delivery", delivery_id, "enqueue",
                       {"work_id": work_id, "partner_id": partner_id,
                        "version_no": work["head_version"]}, event_time)
            results.append({"delivery_id": delivery_id, "partner_id": partner_id,
                            "version_no": work["head_version"], "created": created})
    return results


def drain_outbox(conn):
    """发出待发队列中的全部分发；服务重启后再次调用即可继续处理。"""
    rows = conn.execute(
        "SELECT delivery_id FROM deliveries WHERE status = 'pending'"
        " ORDER BY recorded_at, delivery_id").fetchall()
    sent = []
    with conn:
        for row in rows:
            conn.execute(
                "UPDATE deliveries SET status = 'dispatched', dispatched_at = ?"
                " WHERE delivery_id = ? AND status = 'pending'",
                (_now(), row["delivery_id"]))
            _audit(conn, "delivery", row["delivery_id"], "dispatch", {}, _now())
            sent.append(row["delivery_id"])
    return sent


def get_delivery(conn, delivery_id):
    row = conn.execute("SELECT * FROM deliveries WHERE delivery_id = ?",
                       (delivery_id,)).fetchone()
    if row is None:
        raise NotFound(f"分发记录不存在: {delivery_id}")
    d = dict(row)
    d["receipt_count"] = conn.execute(
        "SELECT COUNT(*) AS c FROM receipts WHERE delivery_id = ?",
        (delivery_id,)).fetchone()["c"]
    return d


def record_receipt(conn, *, delivery_id, receipt_key, event_time):
    """登记接收方回执；同一回执键重复提交不计入次数。"""
    _parse_time(event_time, "event_time")
    delivery = get_delivery(conn, delivery_id)
    if delivery["status"] != "dispatched":
        raise Conflict("分发尚未发出，不能登记回执")
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO receipts (delivery_id, receipt_key, event_time,"
            " recorded_at) VALUES (?, ?, ?, ?)",
            (delivery_id, receipt_key, event_time, _now()))
        duplicate = cur.rowcount == 0
        if not duplicate:
            _audit(conn, "delivery", delivery_id, "receipt",
                   {"receipt_key": receipt_key}, event_time)
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM receipts WHERE delivery_id = ?",
        (delivery_id,)).fetchone()["c"]
    return {"delivery_id": delivery_id, "duplicate": duplicate,
            "receipt_count": count}


# ---------------------------------------------------------------- 附件

def add_attachment(conn, *, attachment_id, work_id, name, content, sensitive,
                   allowed_partners, event_time):
    """上传附件；敏感附件按伙伴名单隔离。"""
    get_work(conn, work_id)
    _parse_time(event_time, "event_time")
    allowed_partners = _str_list(allowed_partners, "allowed_partners")
    with conn:
        try:
            conn.execute(
                "INSERT INTO attachments (attachment_id, work_id, name, content,"
                " sensitive, allowed_partners, recorded_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (attachment_id, work_id, name, content, 1 if sensitive else 0,
                 json.dumps(allowed_partners), _now()))
        except sqlite3.IntegrityError:
            raise Conflict(f"附件标识已存在: {attachment_id}")
        _audit(conn, "attachment", attachment_id, "attach",
               {"work_id": work_id, "name": name, "sensitive": bool(sensitive)},
               event_time)
    return {"attachment_id": attachment_id, "work_id": work_id, "name": name,
            "sensitive": bool(sensitive), "allowed_partners": allowed_partners}


def get_attachment(conn, *, attachment_id, partner_id):
    """读取附件；敏感附件仅对名单内伙伴开放。"""
    row = conn.execute("SELECT * FROM attachments WHERE attachment_id = ?",
                       (attachment_id,)).fetchone()
    if row is None:
        raise NotFound(f"附件不存在: {attachment_id}")
    allowed = json.loads(row["allowed_partners"])
    if row["sensitive"] and partner_id not in allowed:
        raise Forbidden("敏感附件仅对授权伙伴隔离开放")
    return {"attachment_id": row["attachment_id"], "work_id": row["work_id"],
            "name": row["name"], "sensitive": bool(row["sensitive"]),
            "allowed_partners": allowed, "content": row["content"]}


# ---------------------------------------------------------------- 核验与审计

def verify_work(conn, *, work_id, version_no=None, content_hash=None):
    """供任何接收方核验：当前版本、署名义务、与原文的关系及授权状态。"""
    work = get_work(conn, work_id)
    head = work["head_version"]
    version = get_version(conn, work_id, version_no if version_no is not None else head)
    lineage = []
    current = work
    while True:
        lineage.append({"work_id": current["work_id"], "kind": current["kind"],
                        "language": current["language"],
                        "parent_id": current["parent_id"]})
        if not current["parent_id"]:
            break
        current = get_work(conn, current["parent_id"])
    attribution_chain = []
    for item in reversed(lineage):
        head_no = get_work(conn, item["work_id"])["head_version"]
        head_ver = get_version(conn, item["work_id"], head_no)
        attribution_chain.append({"work_id": item["work_id"], "kind": item["kind"],
                                  "byline": head_ver["byline"]})
    licenses = [_license_dict(row) for row in conn.execute(
        "SELECT * FROM licenses WHERE work_id = ? ORDER BY recorded_at",
        (work_id,)).fetchall()]
    result = {
        "work_id": work_id,
        "kind": work["kind"],
        "language": work["language"],
        "head_version": head,
        "version_no": version["version_no"],
        "is_current": version["version_no"] == head,
        "content_hash": version["content_hash"],
        "byline": version["byline"],
        "context_note": version["context_note"],
        "is_merge": bool(version["is_merge"]),
        "parents": json.loads(version["parents"]),
        "relationship_to_original": {
            "is_original": work["kind"] == "original",
            "chain": lineage,
            "depth": len(lineage) - 1,
        },
        "attribution_chain": attribution_chain,
        "licenses": licenses,
    }
    if content_hash is not None:
        result["hash_match"] = content_hash == version["content_hash"]
    return result


def list_audit(conn, *, entity=None, entity_id=None):
    clauses, params = [], []
    if entity:
        clauses.append("entity = ?")
        params.append(entity)
    if entity_id:
        clauses.append("entity_id = ?")
        params.append(entity_id)
    sql = "SELECT * FROM audit_log"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY seq"
    out = []
    for row in conn.execute(sql, params).fetchall():
        d = dict(row)
        d["detail"] = json.loads(d["detail"])
        out.append(d)
    return out
