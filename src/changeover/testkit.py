"""测试与演示共用的典型流程构造。"""

from __future__ import annotations

from datetime import timedelta

from .catalog import parse_dt


def t(base: str, minutes: int) -> str:
    return (parse_dt(base) + timedelta(minutes=minutes)).isoformat()


def passing_swab_plan_limits(plan: dict) -> dict[str, dict[str, float]]:
    """按计划快照限值构造一组全部合格、但关键项贴近限值的涂抹结果。"""
    results = {}
    for item in plan["test_requirements"][0]["items"]:
        limit = item["limit_ppm"]
        value = max(1, limit - 2)
        results[item["allergen"]] = {pt: value for pt in item["points"]}
    return results


def run_happy_changeover(app, changeover_id: str, base: str = "2026-09-20T12:00:00+08:00",
                         from_product: str = "P-WR5", to_product: str = "P-LOTUS",
                         first_batch: str = "OB-0920-001",
                         swab_overrides: dict | None = None,
                         atp_value: int | None = None,
                         borrowed: str | None = "CRATE-B-031",
                         operators=("OP-WU", "OP-HUANG"),
                         signer: str = "QM-LIN", confirmer: str = "QA-ZHENG") -> dict:
    """走通一次完整换线：计划→清洁（含跨线借还/交接）→检测→签署→首件→恢复。"""
    plan = app.request_changeover(changeover_id, "L1", from_product, to_product, base,
                                  "LEAD-ZHOU")
    app.start_cleaning(changeover_id, t(base, 5), operators[0])
    if borrowed:
        app.loan_equipment(changeover_id, borrowed, t(base, 6), operators[0])

    # 前两步由交班人完成，随后交接；剩余步骤接班人完成
    step_count = len(plan["cleaning_steps"])
    for seq in range(1, min(2, step_count) + 1):
        app.confirm_step(changeover_id, seq, t(base, 6 + seq * 6), operators[0])
    app.record_handover(changeover_id, operators[0], operators[1], t(base, 24),
                        note="午高峰交班，剩余步骤与器具扫码交接")
    last_step_minute = 24
    for seq in range(3, step_count + 1):
        last_step_minute = 12 + seq * 6
        app.confirm_step(changeover_id, seq, t(base, last_step_minute), operators[1])

    # 器具逐件扫码（本线范围 + 跨线借入件），全部晚于最后一步
    scan_minute = last_step_minute + 2
    for code in plan["equipment_scope"]:
        app.scan_equipment(changeover_id, code, t(base, scan_minute), operators[1])
        scan_minute += 1
    if borrowed:
        app.scan_equipment(changeover_id, borrowed, t(base, scan_minute), operators[1])
        scan_minute += 1

    app.record_cleaning_complete(changeover_id, t(base, scan_minute), operators[1])

    # 检测：采样略早于出结果
    swabs = passing_swab_plan_limits(plan)
    if swab_overrides:
        swabs.update(swab_overrides)
    sample_at = t(base, scan_minute + 4)
    report_at = t(base, scan_minute + 6)
    for allergen, points in swabs.items():
        app.record_allergen_swab(changeover_id, allergen, points, report_at, sample_at,
                                 confirmer)
    extra = scan_minute + 8
    if plan["rinse_required"]:
        app.record_rinse_water(changeover_id, 30, t(base, extra), sample_at, confirmer)
    app.record_atp(changeover_id,
                   atp_value if atp_value is not None else plan["procedure_snapshot"]["atp_limit_rlu"] - 10,
                   t(base, extra + 2), sample_at, confirmer)

    # 跨线器具在放行签署前归还出线
    if borrowed:
        app.return_equipment(changeover_id, borrowed, t(base, extra + 4), operators[1])

    app.sign_release(changeover_id, t(base, extra + 8), signer)
    app.confirm_first_piece(changeover_id, t(base, extra + 18), first_batch, confirmer,
                            label_ok=True, appearance_ok=True)
    app.resume_production(changeover_id, t(base, extra + 22), first_batch, signer)
    return {"plan": plan, "base": base, "resumed_at": t(base, extra + 22)}


def blocker_texts(result: RejectedEvent) -> list[str]:
    return result.reasons
