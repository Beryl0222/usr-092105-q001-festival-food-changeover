"""节令食品换线放行——可运行场景演示。

运行：python3 demo.py
场景：
A. 正常放行：五仁→纯正莲蓉，含坚果差异的完整湿法清洁、跨线借筐/归还、人员交接、签署、首件、恢复
B. 抽检坚果超标：阻断原因展示→隔离炉次→返工复检→重新签署恢复；重复扫码与重复 event_id 不制造第二次清洁
C. 新版本规程生效：9月25日后阈值收紧至5ppm、检测有效期8小时；旧规则下合格的拖报在新规下过期阻断
D. 事后补填拒收与离线重传受理
E. 从任一成品炉次还原当时规程/检测/签署/恢复；停线构成
"""

from src.changeover import WorkshopApp, RejectedEvent
from src.changeover.testkit import run_happy_changeover, t
from src.changeover.views import (
    render_blockers, render_downtime, render_trace,
)

SEP = "=" * 78


def show(title: str) -> None:
    print("\n" + SEP)
    print(title)
    print(SEP)


def expect_reject(label: str, fn) -> None:
    try:
        fn()
    except RejectedEvent as exc:
        print(f"【拒收】{label}：")
        for reason in exc.reasons:
            print(f"  - {reason}")
    else:
        raise AssertionError(f"{label} 本应被拒收，却成功了")


def main() -> None:
    app = WorkshopApp()

    # ---------------------------------------------------------------- 场景 A
    show("场景 A｜2026-09-20 五仁月饼 → 纯正莲蓉月饼（COP-AH-2025，坚果限值10ppm）")
    info = run_happy_changeover(app, "co-0920-01", base="2026-09-20T12:00:00+08:00",
                                first_batch="OB-0920-001")
    plan = info["plan"]
    print("排产差异：", plan["allergen_diff"]["must_remove_names"],
          "须清除；清洁等级", plan["risk_mode"])
    print("计划步骤：", [s["name"] for s in plan["cleaning_steps"]])
    print(render_downtime(app.downtime_report("co-0920-01")))

    # ---------------------------------------------------------------- 场景 B
    show("场景 B｜09-21 早班换线：首次涂抹坚果 18ppm 超标，阻断→返工→复检恢复")
    cid = "co-0921-02"
    b2 = "2026-09-21T08:00:00+08:00"
    p2 = app.request_changeover(cid, "L1", "P-WR5", "P-LOTUS", b2, "LEAD-ZHOU")
    app.start_cleaning(cid, t(b2, 5), "OP-WU")
    app.loan_equipment(cid, "CRATE-B-031", t(b2, 6), "OP-WU")
    for seq in range(1, len(p2["cleaning_steps"]) + 1):
        app.confirm_step(cid, seq, t(b2, 8 + seq * 6), "OP-WU")
    m0 = 8 + len(p2["cleaning_steps"]) * 6 + 2
    for code in p2["equipment_scope"]:
        app.scan_equipment(cid, code, t(b2, m0), "OP-WU"); m0 += 1
    app.scan_equipment(cid, "CRATE-B-031", t(b2, m0), "OP-WU"); m0 += 1
    app.record_cleaning_complete(cid, t(b2, m0), "OP-WU")

    sample, report = t(b2, m0 + 4), t(b2, m0 + 6)
    from src.changeover.testkit import passing_swab_plan_limits
    swabs = passing_swab_plan_limits(p2)
    swabs["nut"] = {"注馅嘴": 18, "成型模腔": 6, "传送带接口": 4, "周转筐内壁": 3}
    for allergen, points in swabs.items():
        app.record_allergen_swab(cid, allergen, points, report, sample, "QA-ZHENG")
    app.record_rinse_water(cid, 30, t(b2, m0 + 8), sample, "QA-ZHENG")
    app.record_atp(cid, 90, t(b2, m0 + 10), sample, "QA-ZHENG")

    # 器具未归还 + 坚果超标：签署被拒，看板给出全部阻断
    expect_reject("坚果超标且蓝筐未归还时签署",
                  lambda: app.sign_release(cid, t(b2, m0 + 20), "QM-LIN"))
    print(render_blockers(app.evaluate(cid, t(b2, m0 + 20))))

    # 重复扫码不制造第二次清洁
    expect_reject("重复扫注馅嘴",
                  lambda: app.scan_equipment(cid, "NOZZLE-FILL-01", t(b2, m0 + 21), "OP-WU"))
    # 无权限人员不能签署
    expect_reject("线长尝试签署",
                  lambda: app.sign_release(cid, t(b2, m0 + 22), "LEAD-ZHOU"))

    # 质量阻断、登记隔离炉次
    app.hold_production(cid, t(b2, m0 + 25),
                        ["注馅嘴坚果残留18ppm，超过COP-AH-2025限值10ppm"],
                        "QM-LIN", affected_batches=["OB-0921-101", "OB-0921-102"])
    # 返工重清洁（借用筐仍在线上，随返工重新扫码清洁）
    app.start_cleaning(cid, t(b2, m0 + 35), "OP-WU")
    for seq in range(1, len(p2["cleaning_steps"]) + 1):
        app.confirm_step(cid, seq, t(b2, m0 + 38 + seq * 6), "OP-WU")
    m1 = m0 + 38 + len(p2["cleaning_steps"]) * 6 + 2
    for code in p2["equipment_scope"] + ["CRATE-B-031"]:
        app.scan_equipment(cid, code, t(b2, m1), "OP-WU"); m1 += 1
    app.record_cleaning_complete(cid, t(b2, m1), "OP-WU")
    sample2, report2 = t(b2, m1 + 4), t(b2, m1 + 6)
    swabs2 = passing_swab_plan_limits(p2)
    for allergen, points in swabs2.items():
        app.record_allergen_swab(cid, allergen, points, report2, sample2, "QA-ZHENG")
    app.record_rinse_water(cid, 20, t(b2, m1 + 8), sample2, "QA-ZHENG")
    app.record_atp(cid, 80, t(b2, m1 + 10), sample2, "QA-ZHENG")

    # 阻断前的旧检测仍在流里，但评估要求阻断后复检；返工完成后归还借用筐
    expect_reject("返工完成但器具未归还时恢复",
                  lambda: app.resume_production(cid, t(b2, m1 + 12), "OB-0921-103", "QM-LIN"))
    app.return_equipment(cid, "CRATE-B-031", t(b2, m1 + 13), "OP-WU")
    expect_reject("未重新签署直接恢复",
                  lambda: app.resume_production(cid, t(b2, m1 + 15), "OB-0921-103", "QM-LIN"))
    app.sign_release(cid, t(b2, m1 + 14), "QM-LIN")
    expect_reject("未重新首件确认直接恢复",
                  lambda: app.resume_production(cid, t(b2, m1 + 16), "OB-0921-103", "QM-LIN"))
    app.confirm_first_piece(cid, t(b2, m1 + 24), "OB-0921-103", "QA-ZHENG",
                            label_ok=True, appearance_ok=True)
    app.resume_production(cid, t(b2, m1 + 28), "OB-0921-103", "QM-LIN")
    print("\n返工复检后恢复完成。")
    print(render_downtime(app.downtime_report(cid)))

    # ---------------------------------------------------------------- 场景 C
    show("场景 C｜2026-09-26 换线自动适用 COP-AH-2026（坚果5ppm、检测有效期8小时、新增冷却架测点）")
    cid3 = "co-0926-01"
    b3 = "2026-09-26T07:00:00+08:00"
    p3 = app.request_changeover(cid3, "L1", "P-WR5", "P-EGG-LOTUS", b3, "LEAD-ZHOU")
    snap3 = p3["procedure_snapshot"]
    print("适用规程：", snap3["procedure_code"], "｜坚果限值",
          snap3["allergen_limits_ppm"]["nut"], "ppm｜有效期",
          snap3["verification_valid_hours"], "小时")
    print("测点：", p3["test_requirements"][0]["items"][0]["points"])
    app.start_cleaning(cid3, t(b3, 5), "OP-HUANG")
    for seq in range(1, len(p3["cleaning_steps"]) + 1):
        app.confirm_step(cid3, seq, t(b3, 8 + seq * 6), "OP-HUANG")
    n0 = 8 + len(p3["cleaning_steps"]) * 6 + 2
    for code in p3["equipment_scope"]:
        app.scan_equipment(cid3, code, t(b3, n0), "OP-HUANG"); n0 += 1
    app.record_cleaning_complete(cid3, t(b3, n0), "OP-HUANG")

    # 采样在清洁后（08:30 左右），班组拖到 17:20 才上报并要求签署——已超 8 小时
    old_sample = t(b3, 95)
    late_report = t(b3, 620)
    swabs3 = passing_swab_plan_limits(p3)
    for allergen, points in swabs3.items():
        app.record_allergen_swab(cid3, allergen, points, late_report, old_sample, "QA-ZHENG")
    app.record_rinse_water(cid3, 20, late_report, old_sample, "QA-ZHENG")
    app.record_atp(cid3, 80, late_report, old_sample, "QA-ZHENG")
    expect_reject("检测过期后签署",
                  lambda: app.sign_release(cid3, t(b3, 625), "QM-LIN"))
    print(render_blockers(app.evaluate(cid3, t(b3, 625))))

    # ---------------------------------------------------------------- 场景 D
    show("场景 D｜事后补填拒收；登记离线时段后重传受理")
    cid4 = "co-0926-02"
    b4 = "2026-09-26T09:00:00+08:00"
    app.request_changeover(cid4, "L1", "P-LOTUS", "P-RED-BEAN", b4, "LEAD-ZHOU")
    app.start_cleaning(cid4, t(b4, 5), "OP-WU")
    # 10:00 扫的步骤，16:00 才补填上报（超30分钟宽限，无离线登记）
    expect_reject(
        "无离线登记的事后补填",
        lambda: app.confirm_step(cid4, 1, t(b4, 60), "OP-WU",
                                 received_at=t(b4, 420)),
    )
    # 正确做法：先登记离线时段（车间网络中断9:00-12:00），再按时点重放
    app.open_offline_session(cid4, t(b4, 0), t(b4, 180), "OP-WU",
                             "包装间网络改造，PDA离线作业")
    app.confirm_step(cid4, 1, t(b4, 60), "OP-WU", received_at=t(b4, 185))
    print("离线时段内事实重放成功：步骤1 按真实发生时间 09:55 归位")
    stream = app.store.events("exec-" + cid4)
    print("执行流时间线：", [(e["event_type"], e["occurred_at"][11:16]) for e in stream])

    # 同一事实重传：即便平台第二次接收时间不同（received_at 不参与指纹），仍命中幂等
    events_before = len(app.store.all_events())
    from src.changeover.app import event_fingerprint
    from src.changeover import execution as exmod
    plan4 = app._plan(cid4)
    ev = exmod.step_event(cid4, 1, plan4["cleaning_steps"][0]["name"],
                          t(b4, 60), "OP-WU", 1)
    ev["received_at"] = t(b4, 200)  # 网络恢复后又重传一次，接收时间不同
    ev["event_id"] = event_fingerprint(ev)
    stored, duplicate = app.store.append(ev)
    assert duplicate and len(app.store.all_events()) == events_before
    print("重传接收时间不同仍命中幂等：事件总数不变（", events_before, "条），"
          "沿用首次接收时间", stored["received_at"][11:16])

    # ---------------------------------------------------------------- 场景 E
    show("场景 E｜从任一成品炉次还原；停线构成")
    # 已恢复换线之后的第 15 炉，仅凭炉次号+生产时点反查
    bundle = app.trace_oven_batch("OB-0920-015", line="L1",
                                  produced_at="2026-09-20T16:30:00+08:00")
    print(render_trace(bundle))
    # 被隔离炉次直接命中阻断记录
    bundle2 = app.trace_oven_batch("OB-0921-102")
    print()
    print(render_trace(bundle2))


if __name__ == "__main__":
    main()
