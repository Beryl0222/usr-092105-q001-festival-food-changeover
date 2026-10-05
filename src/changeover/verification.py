"""检测结果事件：过敏原涂抹、末次冲洗水、ATP。

检测单按换线开立；同一项目可复检，每次上报都是独立事实，状态保留全部尝试并以
最新一次为准。判定阈值取自 PLAN_ISSUED 的规程快照，不读主数据当前值——
事后收紧或放宽阈值都不改变当时的合格结论。
"""

from __future__ import annotations


def verification_id(changeover_id: str) -> str:
    return f"vr-{changeover_id}"


def allergen_swab_event(changeover_id: str, allergen: str, allergen_name: str,
                        point_results: dict[str, float], at: str, sampled_at: str,
                        operator: str, method: str = "侧流免疫快检") -> dict:
    """point_results: 测点 -> ppm。"""
    return {
        "event_type": "VERIFICATION_RECEIVED",
        "aggregate_type": "verification_result",
        "aggregate_id": verification_id(changeover_id),
        "occurred_at": at,
        "summary": (
            f"{allergen_name}涂抹快检上报：{len(point_results)}个测点，"
            f"峰值 {max(point_results.values())}ppm（{operator}）"
        ),
        "operator": operator,
        "payload": {
            "changeover_id": changeover_id,
            "kind": "allergen_swab",
            "allergen": allergen,
            "allergen_name": allergen_name,
            "point_results": point_results,
            "value": max(point_results.values()),
            "unit": "ppm",
            "sampled_at": sampled_at,
            "method": method,
        },
    }


def rinse_water_event(changeover_id: str, value_ppm: float, at: str, sampled_at: str,
                      operator: str) -> dict:
    return {
        "event_type": "VERIFICATION_RECEIVED",
        "aggregate_type": "verification_result",
        "aggregate_id": verification_id(changeover_id),
        "occurred_at": at,
        "summary": f"末次冲洗水残留上报：{value_ppm}ppm（{operator}）",
        "operator": operator,
        "payload": {
            "changeover_id": changeover_id,
            "kind": "rinse_water",
            "value": value_ppm,
            "unit": "ppm",
            "sampled_at": sampled_at,
            "method": "冲洗水电导/残留快检",
        },
    }


def atp_event(changeover_id: str, value_rlu: int, at: str, sampled_at: str,
              operator: str) -> dict:
    return {
        "event_type": "VERIFICATION_RECEIVED",
        "aggregate_type": "verification_result",
        "aggregate_id": verification_id(changeover_id),
        "occurred_at": at,
        "summary": f"ATP洁净度上报：{value_rlu}RLU（{operator}）",
        "operator": operator,
        "payload": {
            "changeover_id": changeover_id,
            "kind": "atp",
            "value": value_rlu,
            "unit": "RLU",
            "sampled_at": sampled_at,
            "method": "ATP荧光快检",
        },
    }


def build_verification_state(events: list[dict]) -> dict:
    """重建检测状态：每个项目保留尝试序列，latest 指向最新一次。"""
    state: dict = {"attempts": {}}
    for e in events:
        if e["event_type"] != "VERIFICATION_RECEIVED":
            continue
        p = e["payload"]
        key = f"allergen:{p['allergen']}" if p["kind"] == "allergen_swab" else p["kind"]
        state["attempts"].setdefault(key, []).append(
            {
                "at": e["occurred_at"],
                "received_at": e.get("received_at"),
                "sampled_at": p["sampled_at"],
                "operator": e.get("operator"),
                "payload": p,
            }
        )
    state["latest"] = {k: v[-1] for k, v in state["attempts"].items()}
    return state
