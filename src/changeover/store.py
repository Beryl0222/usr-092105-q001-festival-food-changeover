"""事件存储：只追加的领域事件流。

公共约定落到代码：
- event_id 全局唯一；同一 event_id 再次提交视为来源重试/离线重传，直接返回已存事件，
  不追加、不产生第二次清洁或第二次签署（duplicate=True 标识命中）。
- version 在每个聚合内从 1 递增，由存储分配。
- occurred_at 在聚合内不得倒序，防止事后补填混进既有时间线；离线作业先登记离线时段，
  再在时段内按发生顺序重放。
"""

from __future__ import annotations

from copy import deepcopy

from ..validator import validate_event


class RejectedEvent(Exception):
    """事件被拒收，reasons 为可直接展示给班组的中文原因列表。"""

    def __init__(self, reasons: list[str]):
        super().__init__("；".join(reasons))
        self.reasons = reasons


class EventStore:
    def __init__(self) -> None:
        self._streams: dict[str, list[dict]] = {}
        self._event_index: dict[str, str] = {}  # event_id -> aggregate_id

    def append(self, event: dict) -> tuple[dict, bool]:
        """追加事件，返回 (已存事件, 是否重复提交)。version 缺省时由存储分配。"""
        event.setdefault("version", 1)
        errors = validate_event(event)
        if errors:
            raise RejectedEvent(errors)
        event_id = event["event_id"]
        aggregate_id = event["aggregate_id"]
        if event_id in self._event_index:
            owner = self._event_index[event_id]
            if owner != aggregate_id:
                raise RejectedEvent(
                    [f"事件 {event_id} 已属于聚合 {owner}，不得改用聚合 {aggregate_id} 重放"]
                )
            for stored in self._streams[owner]:
                if stored["event_id"] == event_id:
                    return deepcopy(stored), True
        stream = self._streams.setdefault(aggregate_id, [])
        out_of_order = bool(stream) and event["occurred_at"] < stream[-1]["occurred_at"]
        # 离线时段登记本身允许在网络恢复后补登（按会话开始时间归位）；
        # 清洁事实只有发生时间落在已登记会话窗内才允许倒序重放。
        is_session = event["event_type"] == "OFFLINE_SESSION_OPENED"
        if out_of_order and not is_session and not self._covered_by_offline_session(
                stream, event["occurred_at"]):
            raise RejectedEvent(
                [
                    f"事件发生时间 {event['occurred_at']} 早于该聚合最新记录 "
                    f"{stream[-1]['occurred_at']}，禁止倒序补填；离线作业请先登记离线时段，"
                    "并在时段内按事实重放"
                ]
            )
        stored = deepcopy(event)
        if out_of_order:
            # 离线重放：按发生时间归位，版本号在重放结束后统一重排
            stream.append(stored)
            stream.sort(key=lambda e: e["occurred_at"])
            for i, e in enumerate(stream, start=1):
                e["version"] = i
            stored_version = next(e["version"] for e in stream if e is stored)
        else:
            stream.append(stored)
            stored["version"] = len(stream)
            stored_version = stored["version"]
        self._event_index[event_id] = aggregate_id
        result = deepcopy(stored)
        result["version"] = stored_version
        return result, False

    @staticmethod
    def _covered_by_offline_session(stream: list[dict], occurred_at: str) -> bool:
        for e in stream:
            if e["event_type"] != "OFFLINE_SESSION_OPENED":
                continue
            p = e.get("payload", {})
            if p.get("session_start") <= occurred_at <= p.get("session_end"):
                return True
        return False

    def events(self, aggregate_id: str) -> list[dict]:
        return [deepcopy(e) for e in self._streams.get(aggregate_id, [])]

    def all_events(self) -> list[dict]:
        result: list[dict] = []
        for stream in self._streams.values():
            result.extend(deepcopy(e) for e in stream)
        return sorted(result, key=lambda e: (e["occurred_at"], e["aggregate_id"], e["version"]))
