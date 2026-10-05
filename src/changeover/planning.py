"""排产前计划生成：依据前后产品配方与过敏原差异，生成拆洗、冲洗、检测、首件确认要求。

生成结果随 PLAN_ISSUED 事件整体快照：当时生效的规程版本与阈值、清洁步骤、检测点与
限值、首件要求。规程之后发布新版本不影响已生成的换线（新版本只约束生效后的换线）。
"""

from __future__ import annotations

from datetime import datetime

from .catalog import Catalog, parse_dt

# 清洁模式 -> 对班组的中文说明
RISK_MODE_TEXT = {
    "wet_full": "含坚果/花生高风险过敏原：完整拆洗+冲洗湿法清洁，逐点涂抹确认",
    "wet_part": "含蛋乳等过敏原：湿法拆洗冲洗，对差异过敏原逐点涂抹",
    "sanitation": "前后产品过敏原声明一致：常规卫生清洁与首件确认",
}


def _allergen_names(catalog: Catalog, codes: set[str]) -> list[str]:
    return [catalog.allergen_name(c) for c in sorted(codes)]


def build_plan(
    catalog: Catalog,
    changeover_id: str,
    line: str,
    from_product: str,
    to_product: str,
    scheduled_at: str,
    requested_by: str,
) -> dict:
    """生成换线计划草案（尚未落事件）。"""
    at = parse_dt(scheduled_at)
    before = catalog.product(from_product)
    after = catalog.product(to_product)
    before_set = set(before["allergens"])
    after_set = set(after["allergens"])

    removed = before_set - after_set      # 前产品有、后产品不声明：必须清掉并证实无残留
    introduced = after_set - before_set   # 后产品新增：首件核对标签投料
    shared = before_set & after_set

    procedure = catalog.procedure_effective_at(at)
    mode = catalog.risk_level(removed)

    if mode in ("wet_full", "wet_part"):
        cleaning_steps = [
            {
                "seq": i + 1,
                "name": name,
                "required": True,
                "evidence": "扫码确认+操作人",
            }
            for i, name in enumerate(procedure.raw["wet_steps"])
        ]
        rinse_required = True
    else:
        cleaning_steps = [
            {
                "seq": i + 1,
                "name": name,
                "required": True,
                "evidence": "扫码确认+操作人",
            }
            for i, name in enumerate(procedure.raw["dry_steps"])
        ]
        rinse_required = False

    # 检测要求：以前产品含有的过敏原为残留源，差异项（后产品不声明）标为关键项
    swab_points = list(procedure.raw["swab_points_common"])
    allergen_tests = []
    for allergen in sorted(before_set):
        focus = allergen in removed
        allergen_tests.append(
            {
                "allergen": allergen,
                "allergen_name": catalog.allergen_name(allergen),
                "focus": focus,
                "limit_ppm": procedure.allergen_limits[allergen],
                "points": list(swab_points),
                "requirement": (
                    f"各测点残留 ≤ {procedure.allergen_limits[allergen]}ppm"
                    + ("（关键：后产品不声明该过敏原）" if focus else "")
                ),
            }
        )

    test_requirements = [
        {
            "kind": "allergen_swab",
            "name": "过敏原涂抹快检",
            "items": allergen_tests,
        }
    ]
    if rinse_required:
        test_requirements.append(
            {
                "kind": "rinse_water",
                "name": "末次冲洗水残留",
                "limit_ppm": procedure.raw["rinse_residual_limit_ppm"],
                "requirement": f"冲洗水残留 ≤ {procedure.raw['rinse_residual_limit_ppm']}ppm",
            }
        )
    test_requirements.append(
        {
            "kind": "atp",
            "name": "ATP洁净度",
            "limit_rlu": procedure.raw["atp_limit_rlu"],
            "requirement": f"ATP ≤ {procedure.raw['atp_limit_rlu']}RLU",
        }
    )

    first_piece = {
        "required": True,
        "checks": ["首炉成品外观与规格", "外包装过敏原声明核对", "投料与标签版本核对"],
        "carryover_watch": _allergen_names(catalog, removed),
        "new_allergen_labels": _allergen_names(catalog, introduced),
    }

    equipment_scope = catalog.line_scope(line)

    snapshot = {
        "procedure_code": procedure.code,
        "procedure_title": procedure.title,
        "procedure_effective_at": procedure.effective_at.isoformat(),
        "allergen_limits_ppm": dict(procedure.allergen_limits),
        "atp_limit_rlu": procedure.raw["atp_limit_rlu"],
        "rinse_residual_limit_ppm": procedure.raw["rinse_residual_limit_ppm"],
        "verification_valid_hours": procedure.verification_valid_hours,
        "late_report_grace_minutes": procedure.late_report_grace_minutes,
    }

    return {
        "changeover_id": changeover_id,
        "line": line,
        "from_product": {"code": before["code"], "name": before["name"], "allergens": sorted(before_set)},
        "to_product": {"code": after["code"], "name": after["name"], "allergens": sorted(after_set)},
        "scheduled_at": scheduled_at,
        "allergen_diff": {
            "must_remove": sorted(removed),
            "must_remove_names": _allergen_names(catalog, removed),
            "introduced": sorted(introduced),
            "introduced_names": _allergen_names(catalog, introduced),
            "shared": sorted(shared),
        },
        "risk_mode": mode,
        "risk_mode_text": RISK_MODE_TEXT[mode],
        "cleaning_steps": cleaning_steps,
        "rinse_required": rinse_required,
        "test_requirements": test_requirements,
        "first_piece": first_piece,
        "equipment_scope": equipment_scope,
        "procedure_snapshot": snapshot,
        "requested_by": requested_by,
        "issued_at": scheduled_at,
    }


def request_event(plan: dict) -> dict:
    removed_names = "、".join(plan["allergen_diff"]["must_remove_names"]) or "无"
    return {
        "event_type": "CHANGEOVER_REQUESTED",
        "aggregate_type": "changeover_plan",
        "aggregate_id": plan["changeover_id"],
        "occurred_at": plan["scheduled_at"],
        "summary": (
            f"{plan['line']}线 {plan['from_product']['name']} 换产 "
            f"{plan['to_product']['name']}；须清除过敏原：{removed_names}"
        ),
        "operator": plan["requested_by"],
        "payload": {
            "line": plan["line"],
            "from_product": plan["from_product"]["code"],
            "to_product": plan["to_product"]["code"],
            "scheduled_at": plan["scheduled_at"],
            "must_remove_allergens": plan["allergen_diff"]["must_remove"],
        },
    }


def issued_event(plan: dict) -> dict:
    return {
        "event_type": "PLAN_ISSUED",
        "aggregate_type": "changeover_plan",
        "aggregate_id": plan["changeover_id"],
        "occurred_at": plan["issued_at"],
        "summary": (
            f"按 {plan['procedure_snapshot']['procedure_code']} 下发{plan['risk_mode_text']}："
            f"{len(plan['cleaning_steps'])}个清洁步骤、{len(plan['test_requirements'])}类检测、首件确认"
        ),
        "operator": plan["requested_by"],
        "payload": {"plan": plan},
    }
