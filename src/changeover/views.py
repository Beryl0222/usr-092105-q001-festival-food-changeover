"""把领域结果渲染成班组/质量能直接看的中文文本。"""

from __future__ import annotations


def render_blockers(assessment: dict) -> str:
    head = (
        f"【放行看板】换线 {assessment['changeover_id']}｜"
        f"规程 {assessment['procedure_code']}｜评估时点 {assessment['as_of']}\n"
    )
    if not assessment["blocked"]:
        return head + "状态：可放行（无阻断）"
    lines = [head + f"状态：阻断（{len(assessment['blockers'])} 项，未解除不得恢复生产）"]
    for i, b in enumerate(assessment["blockers"], 1):
        lines.append(f"  {i}. [{b['code']}] {b['text']}")
    lines.append("检测明细：")
    for t in assessment["tests"]:
        mark = {"pass": "合格", "fail": "不合格", "expired": "已过期",
                "missing": "缺结果"}[t["status"]]
        lines.append(f"  · {t['label']}：{mark}｜{t['text']}")
    return "\n".join(lines)


def render_downtime(report: dict) -> str:
    if not report["started"]:
        return f"换线 {report['changeover_id']}：清洁尚未开始，暂无停线构成"
    status = "仍在停线" if report["ongoing"] else f"已恢复，停线共 {report['total_minutes']} 分钟"
    lines = [f"【停线构成】换线 {report['changeover_id']}：{status}",
             f"  停线区间 {report['line_stop_start']} → "
             f"{report['line_stop_end'] or '（进行中）'}"]
    lines.append("  常规构成：")
    for s in report["segments"]:
        minutes = f"{s['minutes']}分钟" if s["minutes"] is not None else "进行中"
        lines.append(f"    · {s['name']}：{minutes}（{s['start']} → {s['end'] or '…'}）")
    if report["blocks"]:
        lines.append("  质量阻断时段：")
        for b in report["blocks"]:
            minutes = f"{b['minutes']}分钟" if b["minutes"] is not None else "未解除"
            lines.append(f"    · {('；'.join(b['reasons']))}：{minutes}"
                         f"（{b['start']} → {b['end'] or '…'}）")
            if b["affected_batches"]:
                lines.append(f"      影响隔离炉次：{b['affected_batches']}")
    if report["ongoing"] and report["current_blockers"]:
        lines.append("  当前阻断原因：")
        for b in report["current_blockers"]:
            lines.append(f"    · [{b['code']}] {b['text']}")
    return "\n".join(lines)


def render_trace(bundle: dict) -> str:
    plan = bundle["plan"]
    snap = plan["procedure_snapshot"]
    diff = plan["allergen_diff"]
    lines = [
        f"【炉次追溯卡】成品炉次 {bundle['batch_id']}（匹配方式：{bundle['matched_via']}）",
        f"  所属换线：{bundle['changeover_id']}｜{plan['line']}线",
        f"  产品路径：{plan['from_product']['name']} → {plan['to_product']['name']}",
        f"  过敏原清退：{diff['must_remove_names'] or '无'}；"
        f"后产品新增：{diff['introduced_names'] or '无'}",
        f"  清洁等级：{plan['risk_mode']}（{plan['risk_mode_text']}）",
        f"  当时采用规程：{snap['procedure_code']}《{snap['procedure_title']}》"
        f"（生效 {snap['procedure_effective_at']}）",
        f"  当时阈值：坚果/花生限值 {snap['allergen_limits_ppm']['nut']}ppm；"
        f"ATP ≤ {snap['atp_limit_rlu']}RLU；"
        f"检测有效期 {snap['verification_valid_hours']} 小时",
        "  检测记录：",
    ]
    for key, attempts in bundle["verification"]["attempts"].items():
        last = attempts[-1]
        lines.append(f"    · {key}：{last['payload']['value']}{last['payload']['unit']}"
                     f"（采样 {last['sampled_at']}，上报 {last['at']}，共 {len(attempts)} 次尝试）")
    rel = bundle["release"]
    if rel["signed"]:
        lines.append(f"  放行签署：{rel['signed']['signer_name']}（{rel['signed']['signer']}）"
                     f"于 {rel['signed']['at']} 签署，依据：{rel['signed']['basis']}")
    if rel["first_piece"]:
        lines.append(f"  首件确认：炉次 {rel['first_piece']['oven_batch_id']}，"
                     f"{rel['first_piece']['at']}")
    for h in rel["holds"]:
        lines.append(f"  质量阻断：{h['at']}｜{'；'.join(h['reasons'])}"
                     f"｜隔离炉次 {h['affected_batches'] or '未登记'}"
                     f"｜解除 {h.get('cleared_at', '未解除')}")
    if rel["resumed"]:
        lines.append(f"  恢复决定：{rel['resumed']['approver_name']} 于 "
                     f"{rel['resumed']['at']} 批准，首炉 {rel['resumed']['first_oven_batch_id']}")
    return "\n".join(lines)
