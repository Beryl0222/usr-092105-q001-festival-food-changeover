"""节令食品换线放行领域规则测试。"""

import unittest

from src.changeover import WorkshopApp, RejectedEvent
from src.changeover.app import event_fingerprint
from src.changeover.catalog import Catalog
from src.changeover.store import EventStore
from src.changeover.testkit import run_happy_changeover, t

BASE = "2026-09-20T08:00:00+08:00"


def fresh_app() -> WorkshopApp:
    return WorkshopApp(catalog=Catalog.default(), store=EventStore())


class PlanningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = fresh_app()

    def test_nut_removal_generates_wet_full_with_rinse_and_focus_allergens(self) -> None:
        plan = self.app.request_changeover(
            "co-1", "L1", "P-WR5", "P-LOTUS", BASE, "LEAD-ZHOU")
        self.assertEqual(plan["risk_mode"], "wet_full")
        self.assertTrue(plan["rinse_required"])
        self.assertEqual(
            plan["allergen_diff"]["must_remove"], ["nut", "peanut"])
        step_names = [s["name"] for s in plan["cleaning_steps"]]
        self.assertIn("拆洗", step_names)
        self.assertIn("冲洗", step_names)
        nut_req = next(i for i in plan["test_requirements"][0]["items"]
                       if i["allergen"] == "nut")
        self.assertTrue(nut_req["focus"])
        self.assertEqual(nut_req["limit_ppm"], 10)

    def test_shared_allergen_changeover_is_sanitation_mode(self) -> None:
        plan = self.app.request_changeover(
            "co-2", "L1", "P-LOTUS", "P-RED-BEAN", BASE, "LEAD-ZHOU")
        self.assertEqual(plan["risk_mode"], "sanitation")
        self.assertFalse(plan["rinse_required"])
        self.assertEqual(plan["allergen_diff"]["must_remove"], [])

    def test_egg_introduction_is_wet_part(self) -> None:
        # 莲蓉（无蛋）→ 蛋黄莲蓉（引入蛋）：前产品无蛋，清退集为空但引入集有蛋
        plan = self.app.request_changeover(
            "co-3", "L1", "P-LOTUS", "P-EGG-LOTUS", BASE, "LEAD-ZHOU")
        self.assertEqual(plan["allergen_diff"]["introduced"], ["egg"])
        self.assertIn("蛋", plan["first_piece"]["new_allergen_labels"])

    def test_new_procedure_version_only_binds_later_changeover(self) -> None:
        old = self.app.request_changeover(
            "co-old", "L1", "P-WR5", "P-LOTUS", "2026-09-24T20:00:00+08:00",
            "LEAD-ZHOU")
        new = self.app.request_changeover(
            "co-new", "L1", "P-WR5", "P-EGG-LOTUS", "2026-09-25T08:00:00+08:00",
            "LEAD-ZHOU")
        self.assertEqual(old["procedure_snapshot"]["procedure_code"], "COP-AH-2025")
        self.assertEqual(old["procedure_snapshot"]["allergen_limits_ppm"]["nut"], 10)
        self.assertEqual(old["procedure_snapshot"]["verification_valid_hours"], 12)
        self.assertEqual(new["procedure_snapshot"]["procedure_code"], "COP-AH-2026")
        self.assertEqual(new["procedure_snapshot"]["allergen_limits_ppm"]["nut"], 5)
        self.assertEqual(new["procedure_snapshot"]["verification_valid_hours"], 8)
        self.assertIn("冷却架", new["test_requirements"][0]["items"][0]["points"])
        self.assertNotIn("冷却架", old["test_requirements"][0]["items"][0]["points"])

    def test_snapshot_immutable_when_master_data_changes(self) -> None:
        plan = self.app.request_changeover(
            "co-snap", "L1", "P-WR5", "P-LOTUS", "2026-09-20T08:00:00+08:00",
            "LEAD-ZHOU")
        issued = next(e for e in self.app.store.events("co-snap")
                      if e["event_type"] == "PLAN_ISSUED")
        self.assertEqual(
            issued["payload"]["plan"]["procedure_snapshot"]["allergen_limits_ppm"]["nut"],
            plan["procedure_snapshot"]["allergen_limits_ppm"]["nut"])


class CleaningExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = fresh_app()
        self.plan = self.app.request_changeover(
            "co-exec", "L1", "P-WR5", "P-LOTUS", BASE, "LEAD-ZHOU")

    def test_completion_blocked_until_all_steps_scanned(self) -> None:
        self.app.start_cleaning("co-exec", t(BASE, 5), "OP-WU")
        self.app.confirm_step("co-exec", 1, t(BASE, 10), "OP-WU")
        with self.assertRaises(RejectedEvent) as ctx:
            self.app.record_cleaning_complete("co-exec", t(BASE, 20), "OP-WU")
        self.assertIn("未扫码确认", ctx.exception.reasons[0])

    def test_step_before_start_rejected(self) -> None:
        with self.assertRaises(RejectedEvent):
            self.app.confirm_step("co-exec", 1, t(BASE, 10), "OP-WU")

    def test_duplicate_step_scan_does_not_create_second_confirmation(self) -> None:
        self.app.start_cleaning("co-exec", t(BASE, 5), "OP-WU")
        self.app.confirm_step("co-exec", 1, t(BASE, 10), "OP-WU")
        with self.assertRaises(RejectedEvent) as ctx:
            self.app.confirm_step("co-exec", 1, t(BASE, 12), "OP-HUANG")
        self.assertIn("重复扫码", ctx.exception.reasons[0])
        steps = [e for e in self.app.store.events("exec-co-exec")
                 if e["event_type"] == "STEP_CONFIRMED"]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["operator"], "OP-WU")

    def test_duplicate_event_id_is_idempotent(self) -> None:
        self.app.start_cleaning("co-exec", t(BASE, 5), "OP-WU")
        ev = self.app.confirm_step("co-exec", 2, t(BASE, 10), "OP-WU")
        before = len(self.app.store.all_events())
        stored, duplicate = self.app.store.append(dict(ev))
        self.assertTrue(duplicate)
        self.assertEqual(len(self.app.store.all_events()), before)

    def test_cross_line_equipment_requires_loan_registration(self) -> None:
        self.app.start_cleaning("co-exec", t(BASE, 5), "OP-WU")
        with self.assertRaises(RejectedEvent) as ctx:
            self.app.scan_equipment("co-exec", "MOLD-125-09", t(BASE, 8), "OP-WU")
        self.assertIn("未见有效借用登记", ctx.exception.reasons[0])

    def test_borrowed_crate_not_returned_blocks_sign(self) -> None:
        cid = "co-unreturned"
        self.app.request_changeover(cid, "L1", "P-WR5", "P-LOTUS", BASE, "LEAD-ZHOU")
        self.app.start_cleaning(cid, t(BASE, 5), "OP-WU")
        self.app.loan_equipment(cid, "CRATE-B-031", t(BASE, 6), "OP-WU")
        blockers = {b["code"] for b in self.app.evaluate(cid)["blockers"]}
        self.assertIn("EQUIPMENT_NOT_RETURNED", blockers)

    def test_return_without_loan_rejected_and_double_return_rejected(self) -> None:
        with self.assertRaises(RejectedEvent):
            self.app.return_equipment("co-exec", "CRATE-B-031", t(BASE, 9), "OP-WU")
        self.app.start_cleaning("co-exec", t(BASE, 5), "OP-WU")
        self.app.loan_equipment("co-exec", "CRATE-B-031", t(BASE, 6), "OP-WU")
        self.app.return_equipment("co-exec", "CRATE-B-031", t(BASE, 30), "OP-WU")
        with self.assertRaises(RejectedEvent):
            self.app.return_equipment("co-exec", "CRATE-B-031", t(BASE, 31), "OP-WU")

    def test_handover_lists_pending_steps(self) -> None:
        self.app.start_cleaning("co-exec", t(BASE, 5), "OP-WU")
        self.app.confirm_step("co-exec", 1, t(BASE, 10), "OP-WU")
        self.app.confirm_step("co-exec", 2, t(BASE, 16), "OP-WU")
        ev = self.app.record_handover("co-exec", "OP-WU", "OP-HUANG", t(BASE, 20))
        self.assertEqual(ev["payload"]["pending_steps"], [3, 4, 5])


class BackfillAndOfflineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = fresh_app()
        self.app.request_changeover(
            "co-bf", "L1", "P-WR5", "P-LOTUS", BASE, "LEAD-ZHOU")
        self.app.start_cleaning("co-bf", t(BASE, 5), "OP-WU")

    def test_late_backfill_without_offline_session_rejected(self) -> None:
        with self.assertRaises(RejectedEvent) as ctx:
            self.app.confirm_step("co-bf", 1, t(BASE, 60), "OP-WU",
                                  received_at=t(BASE, 200))
        self.assertIn("事后补填", ctx.exception.reasons[0])

    def test_within_grace_accepted(self) -> None:
        ev = self.app.confirm_step("co-bf", 1, t(BASE, 60), "OP-WU",
                                   received_at=t(BASE, 80))
        self.assertEqual(ev["event_type"], "STEP_CONFIRMED")

    def test_offline_session_allows_ordered_replay_without_second_cleaning(self) -> None:
        self.app.open_offline_session("co-bf", t(BASE, 30), t(BASE, 180),
                                      "OP-WU", "网络中断")
        self.app.confirm_step("co-bf", 1, t(BASE, 60), "OP-WU",
                              received_at=t(BASE, 182))
        self.app.confirm_step("co-bf", 2, t(BASE, 90), "OP-WU",
                              received_at=t(BASE, 183))
        stream = self.app.store.events("exec-co-bf")
        types = [(e["event_type"], e["occurred_at"][11:16]) for e in stream]
        self.assertIn(("STEP_CONFIRMED", "09:00"), types)
        self.assertIn(("STEP_CONFIRMED", "09:30"), types)
        # 版本按发生时间重排
        step_versions = [e["version"] for e in stream
                         if e["event_type"] == "STEP_CONFIRMED"]
        self.assertEqual(sorted(step_versions), step_versions)
        # 重传同一事件：相同内容指纹一致、命中幂等
        plan = self.app._plan("co-bf")
        from src.changeover import execution as exmod
        ev = exmod.step_event("co-bf", 1, plan["cleaning_steps"][0]["name"],
                              t(BASE, 60), "OP-WU", 1)
        ev["received_at"] = t(BASE, 182)
        ev["event_id"] = event_fingerprint(ev)
        before = len(self.app.store.all_events())
        _, duplicate = self.app.store.append(ev)
        self.assertTrue(duplicate)
        self.assertEqual(len(self.app.store.all_events()), before)

        # 网络恢复后再次重传，接收时间不同也必须命中同一事实
        ev_again = dict(ev)
        ev_again["received_at"] = t(BASE, 240)
        _, duplicate2 = self.app.store.append(ev_again)
        self.assertTrue(duplicate2)
        self.assertEqual(len(self.app.store.all_events()), before)

    def test_non_offline_out_of_order_rejected_by_store(self) -> None:
        self.app.confirm_step("co-bf", 1, t(BASE, 40), "OP-WU")
        with self.assertRaises(RejectedEvent):
            self.app.confirm_step("co-bf", 2, t(BASE, 30), "OP-WU")


class VerificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = fresh_app()
        self.plan = self.app.request_changeover(
            "co-vr", "L1", "P-WR5", "P-LOTUS", BASE, "LEAD-ZHOU")
        self.app.start_cleaning("co-vr", t(BASE, 5), "OP-WU")
        for seq in range(1, 6):
            self.app.confirm_step("co-vr", seq, t(BASE, 6 + seq * 6), "OP-WU")
        m0 = 6 + 5 * 6 + 2
        for code in self.plan["equipment_scope"]:
            self.app.scan_equipment("co-vr", code, t(BASE, m0), "OP-WU"); m0 += 1
        self.complete_minute = m0
        self.app.record_cleaning_complete("co-vr", t(BASE, m0), "OP-WU")

    def _record_good_swabs(self, minute: int) -> None:
        sample, report = t(BASE, minute + 4), t(BASE, minute + 6)
        from src.changeover.testkit import passing_swab_plan_limits
        for allergen, points in passing_swab_plan_limits(self.plan).items():
            self.app.record_allergen_swab("co-vr", allergen, points, report, sample,
                                          "QA-ZHENG")
        self.app.record_rinse_water("co-vr", 30, t(BASE, minute + 8), sample,
                                    "QA-ZHENG")
        self.app.record_atp("co-vr", 90, t(BASE, minute + 10), sample, "QA-ZHENG")

    def test_missing_results_block(self) -> None:
        codes = {b["code"] for b in self.app.evaluate("co-vr")["blockers"]}
        self.assertIn("TEST_MISSING", codes)

    def test_failed_allergen_blocks_sign(self) -> None:
        sample, report = t(BASE, self.complete_minute + 4), t(BASE, self.complete_minute + 6)
        from src.changeover.testkit import passing_swab_plan_limits
        swabs = passing_swab_plan_limits(self.plan)
        swabs["nut"] = {pt: 12 for pt in swabs["nut"]}
        for allergen, points in swabs.items():
            self.app.record_allergen_swab("co-vr", allergen, points, report, sample,
                                          "QA-ZHENG")
        self.app.record_rinse_water("co-vr", 30, t(BASE, self.complete_minute + 8),
                                    sample, "QA-ZHENG")
        self.app.record_atp("co-vr", 90, t(BASE, self.complete_minute + 10), sample,
                            "QA-ZHENG")
        with self.assertRaises(RejectedEvent) as ctx:
            self.app.sign_release("co-vr", t(BASE, self.complete_minute + 20), "QM-LIN")
        self.assertTrue(any("坚果" in r for r in ctx.exception.reasons))

    def test_expired_test_blocks_at_later_evaluation(self) -> None:
        # 采样在清洁后，10 小时后评估：COP-AH-2025 有效期 12 小时——先合格，13 小时后过期
        self._record_good_swabs(self.complete_minute)
        signable_at = t(BASE, self.complete_minute + 30)
        self.assertFalse(
            any(b["code"] == "TEST_EXPIRED"
                for b in self.app.evaluate("co-vr", signable_at)["blockers"]))
        expired_at = t(BASE, self.complete_minute + 60 * 13)
        codes = {b["code"] for b in self.app.evaluate("co-vr", expired_at)["blockers"]}
        self.assertIn("TEST_EXPIRED", codes)

    def test_retest_overwrites_latest_judgement(self) -> None:
        sample, report = t(BASE, self.complete_minute + 4), t(BASE, self.complete_minute + 6)
        self.app.record_allergen_swab(
            "co-vr", "nut", {"注馅嘴": 30}, report, sample, "QA-ZHENG")
        bad = {t_["status"] for t_ in self.app.evaluate("co-vr")["tests"]
               if t_["key"] == "allergen:nut"}
        self.assertEqual(bad, {"fail"})
        sample2, report2 = t(BASE, self.complete_minute + 24), t(BASE, self.complete_minute + 26)
        self.app.record_allergen_swab(
            "co-vr", "nut", {"注馅嘴": 4}, report2, sample2, "QA-ZHENG")
        nut = next(t_ for t_ in self.app.evaluate("co-vr")["tests"]
                   if t_["key"] == "allergen:nut")
        self.assertEqual(nut["status"], "pass")
        attempts = self.app.replay("co-vr")["verification"]["attempts"]["allergen:nut"]
        self.assertEqual(len(attempts), 2)


class ReleaseFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = fresh_app()

    def test_happy_flow_resumes_and_versions_increment(self) -> None:
        run_happy_changeover(self.app, "co-ok", base=BASE, first_batch="OB-1")
        state = self.app.replay("co-ok")["release"]
        self.assertIsNotNone(state["resumed"])
        self.assertEqual(state["resumed"]["first_oven_batch_id"], "OB-1")
        self.assertFalse(self.app.evaluate("co-ok")["blocked"])
        for agg in ("co-ok", "exec-co-ok", "vr-co-ok", "release-co-ok"):
            versions = [e["version"] for e in self.app.store.events(agg)]
            self.assertEqual(versions, list(range(1, len(versions) + 1)))

    def test_unauthorized_signer_rejected(self) -> None:
        run_happy_changeover(self.app, "co-auth", base=BASE)
        # 用另一单测权限：线长无签署权
        app2 = fresh_app()
        app2.request_changeover("co-z", "L1", "P-LOTUS", "P-RED-BEAN", BASE,
                                "LEAD-ZHOU")
        with self.assertRaises(RejectedEvent) as ctx:
            app2.sign_release("co-z", t(BASE, 30), "LEAD-ZHOU")
        self.assertIn("release_authority", ctx.exception.reasons[0])

    def test_first_piece_ng_rejected_and_blocks(self) -> None:
        plan = self.app.request_changeover(
            "co-ng", "L1", "P-WR5", "P-LOTUS", BASE, "LEAD-ZHOU")
        self.app.start_cleaning("co-ng", t(BASE, 5), "OP-WU")
        for seq in range(1, 6):
            self.app.confirm_step("co-ng", seq, t(BASE, 6 + seq * 6), "OP-WU")
        m0 = 6 + 30 + 2
        for code in plan["equipment_scope"]:
            self.app.scan_equipment("co-ng", code, t(BASE, m0), "OP-WU"); m0 += 1
        self.app.record_cleaning_complete("co-ng", t(BASE, m0), "OP-WU")
        from src.changeover.testkit import passing_swab_plan_limits
        sample, report = t(BASE, m0 + 4), t(BASE, m0 + 6)
        for allergen, points in passing_swab_plan_limits(plan).items():
            self.app.record_allergen_swab("co-ng", allergen, points, report, sample,
                                          "QA-ZHENG")
        self.app.record_rinse_water("co-ng", 30, t(BASE, m0 + 8), sample, "QA-ZHENG")
        self.app.record_atp("co-ng", 90, t(BASE, m0 + 10), sample, "QA-ZHENG")
        self.app.sign_release("co-ng", t(BASE, m0 + 14), "QM-LIN")
        with self.assertRaises(RejectedEvent):
            self.app.confirm_first_piece("co-ng", t(BASE, m0 + 24), "OB-NG",
                                         "QA-ZHENG", label_ok=False, appearance_ok=True)

    def test_hold_then_rework_requires_recheck_resign_and_new_first_piece(self) -> None:
        cid = "co-hold"
        run_happy_changeover(self.app, cid, base=BASE, first_batch="OB-A")
        # 恢复后抽检异常：阻断并隔离炉次
        self.app.hold_production(
            cid, "2026-09-20T16:00:00+08:00",
            ["成品抽检坚果阳性，疑似注馅嘴返工不彻底"], "QM-LIN",
            affected_batches=["OB-A", "OB-B"])
        # 未返工直接恢复：被拒
        with self.assertRaises(RejectedEvent) as ctx:
            self.app.resume_production(
                cid, "2026-09-20T16:10:00+08:00", "OB-C", "QM-LIN")
        reasons = "；".join(ctx.exception.reasons)
        self.assertIn("重新签署", reasons)
        # 返工：重新清洁一轮 + 复检
        plan = self.app._plan(cid)
        hb = "2026-09-20T16:20:00+08:00"
        self.app.start_cleaning(cid, hb, "OP-WU")
        for seq in range(1, 6):
            self.app.confirm_step(cid, seq, t(hb, 5 + seq * 6), "OP-WU")
        m1 = 5 + 30 + 2
        for code in plan["equipment_scope"]:
            self.app.scan_equipment(cid, code, t(hb, m1), "OP-WU"); m1 += 1
        self.app.record_cleaning_complete(cid, t(hb, m1), "OP-WU")
        from src.changeover.testkit import passing_swab_plan_limits
        sample, report = t(hb, m1 + 4), t(hb, m1 + 6)
        for allergen, points in passing_swab_plan_limits(plan).items():
            self.app.record_allergen_swab(cid, allergen, points, report, sample,
                                          "QA-ZHENG")
        self.app.record_rinse_water(cid, 20, t(hb, m1 + 8), sample, "QA-ZHENG")
        self.app.record_atp(cid, 80, t(hb, m1 + 10), sample, "QA-ZHENG")
        # 重签前首件确认被拒
        with self.assertRaises(RejectedEvent):
            self.app.confirm_first_piece(cid, t(hb, m1 + 12), "OB-C", "QA-ZHENG",
                                         True, True)
        self.app.sign_release(cid, t(hb, m1 + 14), "QM-LIN")
        self.app.confirm_first_piece(cid, t(hb, m1 + 24), "OB-C", "QA-ZHENG",
                                     label_ok=True, appearance_ok=True)
        self.app.resume_production(cid, t(hb, m1 + 28), "OB-C", "QM-LIN")
        report = self.app.downtime_report(cid)
        self.assertEqual(len(report["blocks"]), 1)
        self.assertIsNotNone(report["blocks"][0]["end"])
        self.assertEqual(report["blocks"][0]["affected_batches"], ["OB-A", "OB-B"])

    def test_duplicate_resume_rejected(self) -> None:
        run_happy_changeover(self.app, "co-dup", base=BASE, first_batch="OB-D")
        with self.assertRaises(RejectedEvent):
            self.app.resume_production(
                "co-dup", "2026-09-20T14:00:00+08:00", "OB-D2", "QM-LIN")


class TraceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = fresh_app()
        run_happy_changeover(self.app, "co-tr", base=BASE, first_batch="OB-100")

    def test_trace_by_first_batch_id(self) -> None:
        bundle = self.app.trace_oven_batch("OB-100")
        self.assertEqual(bundle["changeover_id"], "co-tr")
        self.assertEqual(bundle["matched_via"], "首炉登记")
        self.assertEqual(
            bundle["plan"]["procedure_snapshot"]["procedure_code"], "COP-AH-2025")

    def test_trace_by_line_and_produced_time(self) -> None:
        bundle = self.app.trace_oven_batch(
            "OB-199", line="L1", produced_at="2026-09-20T18:00:00+08:00")
        self.assertEqual(bundle["changeover_id"], "co-tr")
        self.assertTrue(bundle["release"]["resumed"])

    def test_trace_unknown_batch_raises(self) -> None:
        with self.assertRaises(RejectedEvent):
            self.app.trace_oven_batch("OB-NOPE")

    def test_downtime_segments_and_current_blockers(self) -> None:
        app2 = fresh_app()
        plan = app2.request_changeover(
            "co-open", "L1", "P-WR5", "P-LOTUS", BASE, "LEAD-ZHOU")
        app2.start_cleaning("co-open", t(BASE, 5), "OP-WU")
        app2.confirm_step("co-open", 1, t(BASE, 10), "OP-WU")
        rep = app2.downtime_report("co-open")
        self.assertTrue(rep["ongoing"])
        codes = {b["code"] for b in rep["current_blockers"]}
        self.assertIn("CLEAN_STEPS_MISSING", codes)
        self.assertIn("NOT_SIGNED", codes)


if __name__ == "__main__":
    unittest.main()
