"""清洁执行：步骤扫码、跨线器具借还、人员交接、完成记录与离线登记。

所有命令只产出事件事实，判定在 app 层完成；状态由事件流重建，保证可回放。
重复扫同一个步骤、用新 event_id 重放同一次完成，都不会制造"第二次清洁"：
步骤与完成以业务键（步骤序号、完成事实）去重，event_id 去重由事件存储负责。
"""

from __future__ import annotations


def execution_id(changeover_id: str) -> str:
    return f"exec-{changeover_id}"


def decision_id(changeover_id: str) -> str:
    return f"release-{changeover_id}"


def start_event(changeover_id: str, at: str, operator: str, line: str) -> dict:
    return {
        "event_type": "CLEANING_STARTED",
        "aggregate_type": "cleaning_execution",
        "aggregate_id": execution_id(changeover_id),
        "occurred_at": at,
        "summary": f"{line}线开始换线清洁，操作人 {operator}",
        "operator": operator,
        "payload": {"changeover_id": changeover_id, "line": line},
    }


def step_event(changeover_id: str, seq: int, step_name: str, at: str, operator: str,
               cycle: int) -> dict:
    return {
        "event_type": "STEP_CONFIRMED",
        "aggregate_type": "cleaning_execution",
        "aggregate_id": execution_id(changeover_id),
        "occurred_at": at,
        "summary": f"第{cycle}轮清洁步骤{seq}「{step_name}」扫码确认（{operator}）",
        "operator": operator,
        "payload": {"changeover_id": changeover_id, "cycle": cycle, "seq": seq,
                    "step_name": step_name},
    }


def scan_event(changeover_id: str, equipment_code: str, name: str, at: str, operator: str,
               cross_line: bool, origin_line: str | None, cycle: int) -> dict:
    tag = f"跨线借入({origin_line})" if cross_line else "本线"
    return {
        "event_type": "EQUIPMENT_SCANNED",
        "aggregate_type": "cleaning_execution",
        "aggregate_id": execution_id(changeover_id),
        "occurred_at": at,
        "summary": f"第{cycle}轮器具 {equipment_code} {name} 上线扫码清洁确认【{tag}】（{operator}）",
        "operator": operator,
        "payload": {
            "changeover_id": changeover_id,
            "cycle": cycle,
            "equipment_code": equipment_code,
            "name": name,
            "cross_line": cross_line,
            "origin_line": origin_line,
        },
    }


def loan_event(changeover_id: str, equipment_code: str, name: str, from_line: str,
               to_line: str, at: str, operator: str) -> dict:
    return {
        "event_type": "EQUIPMENT_LOANED",
        "aggregate_type": "cleaning_execution",
        "aggregate_id": execution_id(changeover_id),
        "occurred_at": at,
        "summary": f"跨线借用登记：{equipment_code} {name} 自 {from_line} 线借入 {to_line} 线（{operator}）",
        "operator": operator,
        "payload": {
            "changeover_id": changeover_id,
            "equipment_code": equipment_code,
            "name": name,
            "from_line": from_line,
            "to_line": to_line,
        },
    }


def return_event(changeover_id: str, equipment_code: str, name: str, from_line: str,
                 to_line: str, at: str, operator: str) -> dict:
    return {
        "event_type": "EQUIPMENT_RETURNED",
        "aggregate_type": "cleaning_execution",
        "aggregate_id": execution_id(changeover_id),
        "occurred_at": at,
        "summary": f"跨线器具归还：{equipment_code} {name} 由 {from_line} 线还回 {to_line} 线（{operator}）",
        "operator": operator,
        "payload": {
            "changeover_id": changeover_id,
            "equipment_code": equipment_code,
            "name": name,
            "from_line": from_line,
            "to_line": to_line,
        },
    }


def handover_event(changeover_id: str, from_operator: str, to_operator: str, at: str,
                   pending_steps: list[int], note: str = "") -> dict:
    return {
        "event_type": "HANDOVER_RECORDED",
        "aggregate_type": "cleaning_execution",
        "aggregate_id": execution_id(changeover_id),
        "occurred_at": at,
        "summary": (
            f"人员交接：{from_operator} → {to_operator}，"
            f"待完成步骤 {pending_steps or '无'}，{note}".rstrip("，")
        ),
        "operator": to_operator,
        "payload": {
            "changeover_id": changeover_id,
            "from_operator": from_operator,
            "to_operator": to_operator,
            "pending_steps": pending_steps,
            "note": note,
        },
    }


def offline_session_event(changeover_id: str, start: str, end: str, operator: str,
                          reason: str) -> dict:
    return {
        "event_type": "OFFLINE_SESSION_OPENED",
        "aggregate_type": "cleaning_execution",
        "aggregate_id": execution_id(changeover_id),
        "occurred_at": start,
        "summary": f"登记离线作业时段 {start} 至 {end}：{reason}（{operator}）",
        "operator": operator,
        "payload": {
            "changeover_id": changeover_id,
            "session_start": start,
            "session_end": end,
            "reason": reason,
        },
    }


def completion_event(changeover_id: str, at: str, operator: str, step_count: int,
                     cycle: int) -> dict:
    return {
        "event_type": "CLEANING_RECORDED",
        "aggregate_type": "cleaning_execution",
        "aggregate_id": execution_id(changeover_id),
        "occurred_at": at,
        "summary": f"第{cycle}轮换线清洁完成记录：{step_count} 个步骤全部扫码确认（{operator}）",
        "operator": operator,
        "payload": {"changeover_id": changeover_id, "cycle": cycle, "step_count": step_count},
    }


def build_execution_state(events: list[dict]) -> dict:
    """从清洁执行事件流重建当前状态。

    质量阻断后允许重新清洁：再次 CLEANING_STARTED 开启新周期，步骤/扫码/完成以
    最新周期为准；借用、交接、离线登记与全部历史事件仍保留在流中可供追溯。
    loans 为借用-归还配对列表，支持多次借还。
    """
    state: dict = {
        "cycle": 0,
        "started_at": None,
        "steps": {},          # seq -> {at, operator}
        "scans": {},          # equipment_code -> 最近一次上线扫码
        "loans": [],          # [{"loan": {...}, "return": {...}|None}]
        "handovers": [],
        "offline_sessions": [],
        "completed_at": None,
        "completion_operator": None,
    }
    for e in events:
        et = e["event_type"]
        p = e.get("payload", {})
        if et == "CLEANING_STARTED":
            state["cycle"] += 1
            state["started_at"] = e["occurred_at"]
            state["steps"] = {}
            state["scans"] = {}
            state["completed_at"] = None
            state["completion_operator"] = None
        elif et == "STEP_CONFIRMED":
            state["steps"][p["seq"]] = {"at": e["occurred_at"], "operator": e.get("operator")}
        elif et == "EQUIPMENT_SCANNED":
            state["scans"][p["equipment_code"]] = {
                "at": e["occurred_at"],
                "operator": e.get("operator"),
                "cross_line": p.get("cross_line", False),
                "origin_line": p.get("origin_line"),
            }
        elif et == "EQUIPMENT_LOANED":
            state["loans"].append(
                {"loan": {"at": e["occurred_at"], "from_line": p["from_line"],
                          "to_line": p["to_line"]}, "return": None,
                 "equipment_code": p["equipment_code"]}
            )
        elif et == "EQUIPMENT_RETURNED":
            for rec in reversed(state["loans"]):
                if rec["equipment_code"] == p["equipment_code"] and rec["return"] is None:
                    rec["return"] = {"at": e["occurred_at"], "from_line": p["from_line"],
                                     "to_line": p["to_line"]}
                    break
        elif et == "HANDOVER_RECORDED":
            state["handovers"].append(
                {"at": e["occurred_at"], "from": p["from_operator"], "to": p["to_operator"]}
            )
        elif et == "OFFLINE_SESSION_OPENED":
            state["offline_sessions"].append(
                {"start": p["session_start"], "end": p["session_end"]}
            )
        elif et == "CLEANING_RECORDED":
            state["completed_at"] = e["occurred_at"]
            state["completion_operator"] = e.get("operator")
    return state
