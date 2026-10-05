"""投影：从事件流还原每次换线的当前状态、阻断原因与停线构成。

状态完全由事件推导，可随时重建；本模块不做规则判断（规则在 service.py），
只负责把已接受的事实聚合成班组和质量人员要看的视图。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .masterdata import parse_ts

STATUS_LABELS = {
    "requested": "已登记，待清洁",
    "cleaning": "清洁执行中",
    "verifying": "待检测/首件确认",
    "ready": "待放行签署",
    "released": "已放行，待恢复生产",
    "resumed": "已恢复生产",
}


def _ts(event: dict) -> datetime:
    return parse_ts(event["occurred_at"])


class ChangeoverState:
    """单次换线的投影状态，aggregate_id 即换线计划号。"""

    def __init__(self, plan_id: str):
        self.plan_id = plan_id
        self.line_id: str | None = None
        self.requirements: dict | None = None
        self.requested_at: datetime | None = None
        self.steps_done: dict[str, dict] = {}
        self.tools_outstanding: dict[str, dict] = {}
        self.tool_returns: list[dict] = []
        self.handovers: list[dict] = []
        self.verifications: list[dict] = []
        self.first_piece: dict | None = None
        self.release: dict | None = None
        self.resumed: dict | None = None
        self.backfilled: list[dict] = []
        self.cleaning_started_at: datetime | None = None
        self.cleaning_finished_at: datetime | None = None
        self.verification_finished_at: datetime | None = None

    # ---- 事件应用（仅投影，规则校验在 service 层已完成） ----

    def apply(self, event: dict, backfilled: bool = False) -> None:
        etype = event["event_type"]
        payload = event.get("payload", {})
        if backfilled:
            self.backfilled.append(event)
        if etype == "CHANGEOVER_REQUESTED":
            self.line_id = payload["line_id"]
            self.requirements = payload["requirements"]
            self.requested_at = _ts(event)
        elif etype == "CLEANING_RECORDED":
            self.steps_done[payload["step_id"]] = event
            at = _ts(event)
            self.cleaning_started_at = self.cleaning_started_at or at
            if self._required_step_ids() <= set(self.steps_done):
                self.cleaning_finished_at = self.cleaning_finished_at or at
        elif etype == "TOOL_BORROWED":
            self.tools_outstanding[payload["tool_id"]] = event
        elif etype == "TOOL_RETURNED":
            self.tools_outstanding.pop(payload["tool_id"], None)
            self.tool_returns.append(event)
        elif etype == "PERSONNEL_HANDOVER_RECORDED":
            self.handovers.append(event)
        elif etype == "VERIFICATION_RECEIVED":
            record = dict(payload)
            record["event"] = event
            self.verifications.append(record)
            if self._verifications_satisfied():
                at = _ts(event)
                self.verification_finished_at = self.verification_finished_at or at
        elif etype == "FIRST_PIECE_CONFIRMED":
            self.first_piece = event
        elif etype == "RELEASE_SIGNED":
            self.release = event
        elif etype == "PRODUCTION_RESUMED":
            self.resumed = event

    # ---- 派生状态 ----

    def _required_step_ids(self) -> set[str]:
        if not self.requirements:
            return set()
        return {s["step_id"] for s in self.requirements["steps"]}

    def _verification_key(self, v: dict) -> tuple[str, str | None]:
        return (v["kind"], v.get("target_allergen"))

    def _verifications_satisfied(self) -> bool:
        if not self.requirements:
            return False
        for req in self.requirements["verifications"]:
            key = (req["kind"], req.get("target_allergen"))
            if not any(self._verification_key(v) == key and v.get("passed") for v in self.verifications):
                return False
        return True

    def verification_validity(self, at: datetime) -> list[str]:
        """返回已过期或早于清洁完成的检测项描述（用于阻断原因）。"""
        problems: list[str] = []
        if not self.requirements:
            return problems
        for req in self.requirements["verifications"]:
            key = (req["kind"], req.get("target_allergen"))
            passed = [v for v in self.verifications if self._verification_key(v) == key and v.get("passed")]
            if not passed:
                continue
            latest = max(passed, key=lambda v: parse_ts(v["performed_at"]))
            performed = parse_ts(latest["performed_at"])
            if self.cleaning_finished_at and performed < self.cleaning_finished_at:
                problems.append(f"{latest['name']} 早于清洁完成时间，需重新取样")
            valid_until = performed + timedelta(minutes=req["validity_minutes"])
            if valid_until < at:
                problems.append(
                    f"{latest['name']} 已于 {valid_until.strftime('%H:%M')} 过期（有效期 {req['validity_minutes']} 分钟），需重新检测"
                )
        return problems

    def status(self, at: datetime) -> str:
        if self.resumed is not None:
            return "resumed"
        if self.release is not None:
            return "released"
        if not self.requirements:
            return "requested"
        steps_ok = self._required_step_ids() <= set(self.steps_done)
        if not steps_ok:
            return "cleaning" if self.steps_done else "requested"
        if not self._verifications_satisfied() or (self.requirements["first_piece_required"] and not self.first_piece):
            return "verifying"
        return "ready"

    def blocking_reasons(self, at: datetime) -> list[str]:
        """当前阻止恢复生产的全部原因，直接展示给班组。"""
        if self.release is not None or not self.requirements:
            return []
        reasons: list[str] = []
        missing = [s["name"] for s in self.requirements["steps"] if s["step_id"] not in self.steps_done]
        reasons.extend(f"清洁步骤未完成：{name}" for name in missing)
        for req in self.requirements["verifications"]:
            key = (req["kind"], req.get("target_allergen"))
            if not any(self._verification_key(v) == key and v.get("passed") for v in self.verifications):
                reasons.append(f"检测缺失或未通过：{req['name']}")
        reasons.extend(self.verification_validity(at))
        if self.requirements["first_piece_required"] and not self.first_piece:
            reasons.append("首件确认未完成")
        for tool in self.tools_outstanding.values():
            p = tool["payload"]
            reasons.append(f"器具未归还：{p['tool_kind']} {p['tool_id']}（借自 {p['from_line_id']}，借用人 {p['borrower_id']}）")
        if not self.handovers:
            reasons.append("跨班人员交接未记录")
        if self.backfilled:
            ids = "、".join(e["event_id"] for e in self.backfilled)
            reasons.append(f"存在事后补填记录（{ids}），按规程不得恢复生产")
        return reasons

    def downtime_breakdown(self, at: datetime) -> list[tuple[str, timedelta]]:
        """停线构成：各阶段耗时，当前所处阶段计到 at。"""
        segments: list[tuple[str, datetime | None, datetime | None]] = [
            ("换线准备", self.requested_at, self.cleaning_started_at),
            ("清洁执行", self.cleaning_started_at, self.cleaning_finished_at),
            ("等待检测与首件", self.cleaning_finished_at, self.verification_finished_at),
            ("等待放行签署", self.verification_finished_at, _ts(self.release) if self.release else None),
            ("恢复生产", _ts(self.release) if self.release else None, _ts(self.resumed) if self.resumed else None),
        ]
        out: list[tuple[str, timedelta]] = []
        cursor: datetime | None = None
        for label, start, end in segments:
            start = start or cursor
            if start is None:
                continue
            effective_end = end or (at if self.resumed is None else None)
            if effective_end is None:
                continue
            out.append((label + ("" if end else "（进行中）"), effective_end - start))
            cursor = end
        return out


def build_states(events: list[dict], backfill_flags: dict[str, bool]) -> dict[str, ChangeoverState]:
    """从事件流重建全部换线状态。backfill_flags 由 service 层在受理时计算。"""
    states: dict[str, ChangeoverState] = {}
    for event in events:
        plan_id = event["aggregate_id"]
        state = states.setdefault(plan_id, ChangeoverState(plan_id))
        state.apply(event, backfilled=backfill_flags.get(event["event_id"], False))
    return states


def format_duration(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分钟" if hours else f"{minutes} 分钟"
