"""只追加事件存储。

所有状态变化都以事件落盘（JSONL，每行一条），每条事件带：

* seq：单调序号；
* event_time：调用方声明的事件发生时间；
* received_time：服务端接收落账时间（UTC）；
* prev_hash / hash：前一条哈希与本条哈希，形成防篡改哈希链。

内存索引在启动时通过重放事件重建，因此待发投递等状态在服务重启后自动恢复。
附件二进制不进事件日志，单独存放在 attachments 目录，元数据仍以事件记录。
"""
from __future__ import annotations

import json
import os
import threading
from typing import Callable

from .models import canonical_json, now_iso


class EventStore:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.log_path = os.path.join(data_dir, "event.log")
        self.attach_dir = os.path.join(data_dir, "attachments")
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.attach_dir, exist_ok=True)
        self._lock = threading.RLock()
        self._records: list[dict] = []
        self._indexes: dict[str, dict] = {}
        self._reducers: dict[str, Callable[[dict, dict], None]] = {}

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    # ---- 重放与索引 -------------------------------------------------------

    def replay(self, reducers: dict[str, Callable[[dict, dict], None]]):
        """从磁盘重放事件日志，重建 indexes。

        reducers: {事件类型: callback(record, indexes)}
        注册后，后续 append 的事件也会实时应用同一组归约器。
        """
        self._reducers = reducers
        with self._lock:
            self._records.clear()
            indexes: dict[str, dict] = {
                "artifacts": {},
                "children": {},
                "grants": {},
                "deliveries": {},
                "attachments": {},
            }
            if os.path.exists(self.log_path):
                with open(self.log_path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        record = json.loads(line)
                        self._verify_chain_link(record, self._records[-1] if self._records else None)
                        self._records.append(record)
                        reducer = reducers.get(record["type"])
                        if reducer:
                            reducer(record, indexes)
            self._indexes = indexes
            return indexes

    @staticmethod
    def _verify_chain_link(record: dict, previous: dict | None):
        prev_hash = previous["hash"] if previous else None
        if record.get("prev_hash") != prev_hash:
            raise RuntimeError(f"事件日志哈希链在 seq={record.get('seq')} 处断裂")
        expected = compute_record_hash(record, prev_hash)
        if record.get("hash") != expected:
            raise RuntimeError(f"事件 seq={record.get('seq')} 哈希校验失败")

    def verify_chain(self) -> dict:
        """从头复核整条哈希链，供运维/接收方核验审计完整性。"""
        with self._lock:
            prev_hash = None
            for record in self._records:
                if record.get("prev_hash") != prev_hash:
                    return {"ok": False, "broken_at": record["seq"]}
                if record.get("hash") != compute_record_hash(record, prev_hash):
                    return {"ok": False, "broken_at": record["seq"]}
                prev_hash = record["hash"]
            return {"ok": True, "events": len(self._records), "head": prev_hash}

    # ---- 追加 -------------------------------------------------------------

    def append(self, etype: str, payload: dict, event_time: str) -> dict:
        with self._lock:
            seq = len(self._records) + 1
            prev_hash = self._records[-1]["hash"] if self._records else None
            record = {
                "seq": seq,
                "type": etype,
                "event_time": event_time,
                "received_time": now_iso(),
                "prev_hash": prev_hash,
                "payload": payload,
            }
            record["hash"] = compute_record_hash(record, prev_hash)
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._records.append(record)
            reducer = self._reducers.get(etype)
            if reducer:
                reducer(record, self._indexes)
        return record

    @property
    def records(self) -> list[dict]:
        with self._lock:
            return list(self._records)

    @property
    def indexes(self) -> dict[str, dict]:
        return self._indexes

    def head_hash(self) -> str | None:
        with self._lock:
            return self._records[-1]["hash"] if self._records else None

    # ---- 附件二进制 -------------------------------------------------------

    def attachment_path(self, artifact_id: str, attachment_id: str) -> str:
        directory = os.path.join(self.attach_dir, artifact_id)
        os.makedirs(directory, exist_ok=True)
        return os.path.join(directory, attachment_id + ".bin")


def compute_record_hash(record: dict, prev_hash: str | None) -> str:
    body = {
        "seq": record["seq"],
        "type": record["type"],
        "event_time": record["event_time"],
        "received_time": record["received_time"],
        "prev_hash": prev_hash,
        "payload": record["payload"],
    }
    import hashlib

    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
