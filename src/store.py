"""事件存储：JSONL 追加写，按 event_id 幂等去重。

来源系统重试、扫码枪离线重传都会携带原 event_id，
存储层对重复 event_id 直接忽略，保证同一事实不会被记两次。
"""

from __future__ import annotations

import json
from pathlib import Path


class EventStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._events: list[dict] = []
        self._by_id: dict[str, dict] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    self._index(json.loads(line))

    def _index(self, event: dict) -> None:
        self._events.append(event)
        self._by_id[event["event_id"]] = event

    def __contains__(self, event_id: str) -> bool:
        return event_id in self._by_id

    def __len__(self) -> int:
        return len(self._events)

    def all(self) -> list[dict]:
        return list(self._events)

    def append(self, event: dict) -> bool:
        """追加事件；event_id 已存在时忽略并返回 False（幂等）。"""
        if event["event_id"] in self._by_id:
            return False
        self._index(event)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        return True
