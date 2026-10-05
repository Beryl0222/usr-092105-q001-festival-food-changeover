"""炉次追溯：从任一成品炉次还原当时采用的规程、检测与恢复决定。

炉次记录来自生产执行侧（data/batches.jsonl），追溯时按产线与时间
定位该炉次投料前最近一次完成放行的换线，还原其完整证据链。
"""

from __future__ import annotations

import json
from pathlib import Path

from .masterdata import parse_ts
from .projections import ChangeoverState


def load_batches(path: str | Path) -> list[dict]:
    batches = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            batches.append(json.loads(line))
    return batches


def trace_batch(batch_id: str, states: dict[str, ChangeoverState], batches: list[dict]) -> dict:
    batch = next((b for b in batches if b["batch_id"] == batch_id), None)
    if batch is None:
        raise ValueError(f"未找到炉次：{batch_id}")
    started = parse_ts(batch["started_at"])

    candidates = [
        s for s in states.values()
        if s.line_id == batch["line_id"] and s.resumed is not None and parse_ts(s.resumed["occurred_at"]) <= started
    ]
    if not candidates:
        raise ValueError(f"炉次 {batch_id} 投料前，产线 {batch['line_id']} 没有已完成的换线放行记录")
    state = max(candidates, key=lambda s: parse_ts(s.resumed["occurred_at"]))
    req = state.requirements

    return {
        "batch": batch,
        "plan_id": state.plan_id,
        "procedure": {
            "id": req["procedure_id"],
            "title": req["procedure_title"],
            "version": req["procedure_version"],
            "risk_level": req["risk_level"],
            "risk_reason": req["risk_reason"],
        },
        "products": {"from": req["from_product"], "to": req["to_product"]},
        "steps": [
            {
                "name": s["name"],
                "done": s["step_id"] in state.steps_done,
                "at": state.steps_done[s["step_id"]]["occurred_at"] if s["step_id"] in state.steps_done else None,
                "operator": state.steps_done[s["step_id"]]["payload"].get("operator_id") if s["step_id"] in state.steps_done else None,
            }
            for s in req["steps"]
        ],
        "verifications": [
            {
                "name": v["name"],
                "value": v["value"],
                "limit": v.get("limit"),
                "passed": v.get("passed"),
                "performed_at": v["performed_at"],
                "lab_id": v["lab_id"],
            }
            for v in state.verifications
        ],
        "first_piece": {
            "required": req["first_piece_required"],
            "confirmed_at": state.first_piece["occurred_at"] if state.first_piece else None,
            "inspector": state.first_piece["payload"].get("inspector_id") if state.first_piece else None,
        },
        "release": {
            "signed_at": state.release["occurred_at"],
            "signer": state.release["payload"].get("signer_name"),
            "signer_id": state.release["payload"].get("signer_id"),
        },
        "resumed_at": state.resumed["occurred_at"],
        "backfilled_events": [e["event_id"] for e in state.backfilled],
    }
