"""领域模型与通用约定。

遵守 docs/domain.md：

* 事件时间（event_time，事情发生的时间）与接收时间（received_time，服务端
  落账时间）分离记录；
* 所有业务身份（artifact_id / grant_id / delivery_id / partner_id 等）由
  调用方提供稳定标识；
* 审计记录只追加，更正通过新事件体现（如撤销、再编辑），不得覆盖旧记录。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone


# 授动作：转载 / 翻译 / 节选 / 编辑 / 再许可
ACTION_PUBLISH = "publish"
ACTION_TRANSLATE = "translate"
ACTION_EXCERPT = "excerpt"
ACTION_EDIT = "edit"
ACTION_SUBLICENSE = "sublicense"

VALID_ACTIONS = {
    ACTION_PUBLISH,
    ACTION_TRANSLATE,
    ACTION_EXCERPT,
    ACTION_EDIT,
    ACTION_SUBLICENSE,
}

# 稿件种类
KIND_ORIGINAL = "original"
KIND_TRANSLATION = "translation"
KIND_EXCERPT = "excerpt"
KIND_REVISION = "revision"
DERIVED_KINDS = {KIND_TRANSLATION, KIND_EXCERPT, KIND_REVISION}

# 来源关系
REL_TRANSLATED_FROM = "translated_from"
REL_EXCERPTED_FROM = "excerpted_from"
REL_REVISED_FROM = "revised_from"
REL_MERGED_WITH = "merged_with"

WILDCARD = "*"


class DomainError(Exception):
    """带 HTTP 状态码的领域错误。"""

    status = 400

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        if status is not None:
            self.status = status


class NotFound(DomainError):
    status = 404


class Conflict(DomainError):
    status = 409


class InvalidRequest(DomainError):
    status = 400


class Forbidden(DomainError):
    status = 403


def canonical_json(data) -> str:
    """稳定序列化：排序键、无空白、非 ASCII 原样输出。"""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_hash(text: str) -> str:
    """稿件正文内容的指纹；接收方据此核验当前版本内容。"""
    return sha256_hex(text.encode("utf-8"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None, field: str = "time") -> datetime:
    if not value:
        raise InvalidRequest(f"缺少 {field}")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise InvalidRequest(f"{field} 不是合法 ISO-8601 时间: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def require(payload: dict, key: str):
    value = payload.get(key)
    if value is None or value == "" or value == []:
        raise InvalidRequest(f"缺少必填字段: {key}")
    return value
