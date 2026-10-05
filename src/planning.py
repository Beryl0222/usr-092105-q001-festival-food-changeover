"""换线计划生成：依据前后产品配方与过敏原差异，从生效规程版本快照出本次要求。

生成的要求快照随 CHANGEOVER_REQUESTED 事件固化到计划中，
此后规程或阈值升级只影响新计划，不追溯本次换线。
"""

from __future__ import annotations

from .masterdata import MasterData, ProcedureVersion


def assess_risk(from_allergens: tuple[str, ...], to_allergens: tuple[str, ...]) -> dict:
    """比较前后产品过敏原：前产品有而后产品没有的，构成残留交叉污染风险。"""
    removed = sorted(set(from_allergens) - set(to_allergens))
    introduced = sorted(set(to_allergens) - set(from_allergens))
    if removed:
        level = "高"
        reason = f"前产品残留过敏原 {('、').join(removed)} 对后产品构成交叉污染风险"
    elif introduced:
        level = "中"
        reason = f"后产品新增过敏原 {('、').join(introduced)}，需防止前产品残留混入并核对标识"
    else:
        level = "低"
        reason = "前后产品过敏原谱一致，常规冲洗即可"
    return {"level": level, "removed_allergens": removed, "introduced_allergens": introduced, "reason": reason}


def build_requirements(master: MasterData, from_product_id: str, to_product_id: str, at) -> dict:
    """生成换线要求快照：拆洗/冲洗等步骤、检测项与阈值、首件确认、补填容忍度。"""
    from_product = master.products.get(from_product_id)
    to_product = master.products.get(to_product_id)
    if from_product is None:
        raise ValueError(f"未知的前产品：{from_product_id}")
    if to_product is None:
        raise ValueError(f"未知的后产品：{to_product_id}")

    procedure: ProcedureVersion = master.procedure_at(at)
    risk = assess_risk(from_product.allergens, to_product.allergens)
    level_spec = procedure.risk_levels[risk["level"]]

    verifications: list[dict] = []
    for spec in level_spec["verifications"]:
        if spec.get("per_removed_allergen"):
            for allergen in risk["removed_allergens"]:
                verifications.append({
                    "kind": spec["kind"],
                    "name": f"{allergen}残留擦拭检测",
                    "target_allergen": allergen,
                    "limit_ppm": spec["limit_ppm"],
                    "validity_minutes": spec["validity_minutes"],
                })
        else:
            verifications.append(dict(spec))

    return {
        "procedure_id": procedure.procedure_id,
        "procedure_title": procedure.title,
        "procedure_version": procedure.version,
        "risk_level": risk["level"],
        "risk_reason": risk["reason"],
        "removed_allergens": risk["removed_allergens"],
        "introduced_allergens": risk["introduced_allergens"],
        "steps": [dict(s) for s in level_spec["steps"]],
        "verifications": verifications,
        "first_piece_required": level_spec["first_piece_required"],
        "backfill_tolerance_minutes": procedure.backfill_tolerance_minutes,
        "from_product": {"id": from_product.product_id, "name": from_product.name},
        "to_product": {"id": to_product.product_id, "name": to_product.name},
    }
