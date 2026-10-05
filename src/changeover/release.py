"""放行决定事件：质量签署、首件确认、恢复生产、质量阻断。

签署事实不可删除：恢复后若再发现问题，另发 PRODUCTION_HELD；签署人必须具备
release_authority 角色。恢复生产同时是停线结束时点。
"""

from __future__ import annotations

from .execution import decision_id


def signed_event(changeover_id: str, at: str, signer: str, signer_name: str,
                 procedure_code: str, basis: list[str]) -> dict:
    return {
        "event_type": "RELEASE_SIGNED",
        "aggregate_type": "release_decision",
        "aggregate_id": decision_id(changeover_id),
        "occurred_at": at,
        "summary": f"质量放行签署：{signer_name}（{signer}）依据 {procedure_code} 签署同意放行",
        "operator": signer,
        "payload": {
            "changeover_id": changeover_id,
            "signer": signer,
            "signer_name": signer_name,
            "procedure_code": procedure_code,
            "basis": basis,
        },
    }


def first_piece_event(changeover_id: str, at: str, oven_batch_id: str, product: str,
                      confirmer: str, confirmer_name: str, label_ok: bool,
                      appearance_ok: bool, note: str = "") -> dict:
    return {
        "event_type": "FIRST_PIECE_CONFIRMED",
        "aggregate_type": "release_decision",
        "aggregate_id": decision_id(changeover_id),
        "occurred_at": at,
        "summary": (
            f"首件确认：首炉 {oven_batch_id}（{product}）外观{'合格' if appearance_ok else '不合格'}、"
            f"标签过敏原{'核对一致' if label_ok else '不一致'}（{confirmer_name}）"
        ),
        "operator": confirmer,
        "payload": {
            "changeover_id": changeover_id,
            "oven_batch_id": oven_batch_id,
            "product": product,
            "label_ok": label_ok,
            "appearance_ok": appearance_ok,
            "note": note,
        },
    }


def resumed_event(changeover_id: str, at: str, first_oven_batch_id: str, product: str,
                  approver: str, approver_name: str, blockers_cleared: list[str]) -> dict:
    return {
        "event_type": "PRODUCTION_RESUMED",
        "aggregate_type": "release_decision",
        "aggregate_id": decision_id(changeover_id),
        "occurred_at": at,
        "summary": (
            f"恢复生产：首炉 {first_oven_batch_id}（{product}）起放行，"
            f"批准人 {approver_name}；已解除阻断 {len(blockers_cleared)} 项"
        ),
        "operator": approver,
        "payload": {
            "changeover_id": changeover_id,
            "first_oven_batch_id": first_oven_batch_id,
            "product": product,
            "approver": approver,
            "approver_name": approver_name,
            "blockers_cleared": blockers_cleared,
        },
    }


def held_event(changeover_id: str, at: str, reasons: list[str], held_by: str,
               held_by_name: str, affected_batches: list[str] | None = None) -> dict:
    return {
        "event_type": "PRODUCTION_HELD",
        "aggregate_type": "release_decision",
        "aggregate_id": decision_id(changeover_id),
        "occurred_at": at,
        "summary": f"质量阻断停线：{'；'.join(reasons)}（{held_by_name}）",
        "operator": held_by,
        "payload": {
            "changeover_id": changeover_id,
            "reasons": reasons,
            "held_by": held_by,
            "held_by_name": held_by_name,
            "affected_batches": affected_batches or [],
        },
    }
