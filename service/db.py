"""持久化层：SQLite 架构与连接。

所有状态变化都会追加审计日志；审计日志只增不改。
"""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS works (
    work_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('original', 'translation', 'excerpt', 'reedit')),
    language TEXT NOT NULL,
    parent_id TEXT REFERENCES works (work_id),
    head_version INTEGER NOT NULL,
    created_event_time TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    work_id TEXT NOT NULL REFERENCES works (work_id),
    version_no INTEGER NOT NULL,
    parents TEXT NOT NULL,            -- JSON 数组：父版本号，合并版本有两个
    body TEXT NOT NULL,
    context_note TEXT NOT NULL DEFAULT '',
    byline TEXT NOT NULL,
    editor_id TEXT NOT NULL,
    is_merge INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL,
    event_time TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (work_id, version_no)
);

CREATE TABLE IF NOT EXISTS licenses (
    license_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works (work_id),
    partner_id TEXT NOT NULL,
    regions TEXT NOT NULL,            -- JSON 数组，空数组表示不限地区
    channels TEXT NOT NULL,           -- JSON 数组，空数组表示不限渠道
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    permissions TEXT NOT NULL,        -- JSON 对象：可修改范围（translation/excerpt/reedit）
    attribution_text TEXT NOT NULL DEFAULT '',
    revoked_at TEXT,
    created_event_time TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vouchers (
    voucher_id TEXT PRIMARY KEY,
    license_id TEXT NOT NULL REFERENCES licenses (license_id),
    version_no INTEGER NOT NULL,
    region TEXT NOT NULL,
    channel TEXT NOT NULL,
    published_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,     -- {work_id}:{partner_id}:v{version_no}，天然幂等
    work_id TEXT NOT NULL REFERENCES works (work_id),
    version_no INTEGER NOT NULL,
    partner_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'dispatched')),
    dispatched_at TEXT,
    created_event_time TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS receipts (
    delivery_id TEXT NOT NULL REFERENCES deliveries (delivery_id),
    receipt_key TEXT NOT NULL,        -- 接收方提供的幂等键
    event_time TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (delivery_id, receipt_key)
);

CREATE TABLE IF NOT EXISTS attachments (
    attachment_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works (work_id),
    name TEXT NOT NULL,
    content BLOB NOT NULL,
    sensitive INTEGER NOT NULL DEFAULT 0,
    allowed_partners TEXT NOT NULL,   -- JSON 数组：敏感附件可见的伙伴
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    entity TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL,             -- JSON
    event_time TEXT NOT NULL,         -- 调用方声明的事件时间
    recorded_at TEXT NOT NULL         -- 服务端接收时间
);
"""


def connect(path: str) -> sqlite3.Connection:
    """打开（必要时创建）数据库并保证架构存在。"""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
