import tempfile
import unittest
from pathlib import Path

from src.cli import make_event
from src.masterdata import DATA_DIR, MasterData
from src.service import ChangeoverService
from src.store import EventStore
from src.trace import load_batches, trace_batch

T = "2026-09-20T12:00:00+08:00"


def make_service(tmp: str) -> ChangeoverService:
    return ChangeoverService(EventStore(Path(tmp) / "events.jsonl"), MasterData())


class ChangeoverCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.service = make_service(self._tmp.name)
        self._seq = 0

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def ev(self, event_type, plan, occurred, payload, recorded_at=None):
        self._seq += 1
        # 默认即做即录；仅补填场景显式传入更晚的 recorded_at
        event = make_event(self.service, event_type, plan, occurred,
                           f"{event_type} 测试", payload, f"test-{self._seq:04d}",
                           recorded_at or occurred)
        return self.service.apply(event)

    def request_plan(self, plan="P1", line="L3", from_p="wuren-yuebing", to_p="lianrong-yuebing", at=T):
        result = self.ev("CHANGEOVER_REQUESTED", plan, at,
                         {"line_id": line, "from_product_id": from_p, "to_product_id": to_p})
        self.assertTrue(result.accepted, result.errors)
        return result

    def clean_all(self, plan="P1"):
        state = self.service.states[plan]
        for i, step in enumerate(state.requirements["steps"]):
            ts = f"2026-09-20T12:{10 + i * 5:02d}:00+08:00"
            result = self.ev("CLEANING_RECORDED", plan, ts, {"step_id": step["step_id"], "operator_id": "op-a"})
            self.assertTrue(result.accepted, result.errors)

    def verify_all(self, plan="P1", performed="2026-09-20T13:00:00+08:00", at="2026-09-20T13:05:00+08:00"):
        state = self.service.states[plan]
        for req in state.requirements["verifications"]:
            payload = {"kind": req["kind"], "value": 0.1 if req["kind"] == "allergen_swab" else 50,
                       "unit": "ppm" if req["kind"] == "allergen_swab" else "RLU",
                       "performed_at": performed, "lab_id": "lab-01"}
            if req.get("target_allergen"):
                payload["target_allergen"] = req["target_allergen"]
            result = self.ev("VERIFICATION_RECEIVED", plan, at, payload)
            self.assertTrue(result.accepted, result.errors)

    def finish_prerequisites(self, plan="P1"):
        self.clean_all(plan)
        self.verify_all(plan)
        state = self.service.states[plan]
        if state.requirements["first_piece_required"]:
            self.ev("FIRST_PIECE_CONFIRMED", plan, "2026-09-20T13:10:00+08:00", {"inspector_id": "qc-zhao"})
        self.ev("PERSONNEL_HANDOVER_RECORDED", plan, "2026-09-20T13:11:00+08:00",
                {"outgoing_id": "op-a", "incoming_id": "op-b"})


class TestPlanGeneration(ChangeoverCase):
    def test_high_risk_generates_teardown_swabs_and_first_piece(self):
        self.request_plan()
        req = self.service.states["P1"].requirements
        self.assertEqual(req["risk_level"], "高")
        self.assertEqual(req["procedure_version"], "3.0")
        self.assertEqual([s["step_id"] for s in req["steps"]],
                         ["teardown", "wash", "rinse", "dry", "inspect"])
        swabs = {v["target_allergen"] for v in req["verifications"] if v["kind"] == "allergen_swab"}
        self.assertEqual(swabs, {"坚果", "芝麻", "蛋"})
        self.assertTrue(any(v["kind"] == "atp" for v in req["verifications"]))
        self.assertTrue(req["first_piece_required"])

    def test_procedure_version_selected_by_request_time(self):
        self.request_plan(plan="OLD", at="2026-08-31T10:00:00+08:00")
        self.request_plan(plan="NEW", at="2026-09-20T10:00:00+08:00")
        old = self.service.states["OLD"].requirements
        new = self.service.states["NEW"].requirements
        self.assertEqual(old["procedure_version"], "2.1")
        self.assertEqual(new["procedure_version"], "3.0")
        # 新版本不追溯：旧计划的阈值与补填容忍度保持 v2.1 快照
        old_swab = next(v for v in old["verifications"] if v["kind"] == "allergen_swab")
        new_swab = next(v for v in new["verifications"] if v["kind"] == "allergen_swab")
        self.assertEqual(old_swab["limit_ppm"], 2.0)
        self.assertEqual(new_swab["limit_ppm"], 1.0)
        self.assertEqual(old["backfill_tolerance_minutes"], 30)
        self.assertEqual(new["backfill_tolerance_minutes"], 15)

    def test_verification_judged_by_plan_snapshot_threshold(self):
        # v2.1 计划：1.5 ppm 合格（限值 2.0）
        self.request_plan(plan="OLD", at="2026-08-31T10:00:00+08:00")
        result = self.ev("VERIFICATION_RECEIVED", "OLD", "2026-08-31T11:00:00+08:00",
                         {"kind": "allergen_swab", "target_allergen": "坚果", "value": 1.5,
                          "unit": "ppm", "performed_at": "2026-08-31T10:50:00+08:00", "lab_id": "lab-01"})
        self.assertTrue(result.accepted, result.errors)
        self.assertTrue(self.service.states["OLD"].verifications[0]["passed"])
        # v3.0 计划：同样 1.5 ppm 不合格（限值 1.0）
        self.request_plan(plan="NEW", at="2026-09-20T10:00:00+08:00")
        result = self.ev("VERIFICATION_RECEIVED", "NEW", "2026-09-20T11:00:00+08:00",
                         {"kind": "allergen_swab", "target_allergen": "坚果", "value": 1.5,
                          "unit": "ppm", "performed_at": "2026-09-20T10:50:00+08:00", "lab_id": "lab-01"})
        self.assertTrue(result.accepted, result.errors)
        self.assertFalse(self.service.states["NEW"].verifications[0]["passed"])


class TestIdempotency(ChangeoverCase):
    def test_same_event_id_retransmit_ignored(self):
        self.request_plan()
        event = make_event(self.service, "CLEANING_RECORDED", "P1", "2026-09-20T12:10:00+08:00",
                           "拆洗完成", {"step_id": "teardown", "operator_id": "op-a"}, "dup-0001",
                           "2026-09-20T12:10:00+08:00")
        first = self.service.apply(event)
        self.assertTrue(first.accepted)
        size = len(self.service.store)
        second = self.service.apply(dict(event))
        self.assertTrue(second.accepted)
        self.assertTrue(second.duplicate)
        self.assertEqual(len(self.service.store), size)
        self.assertEqual(len(self.service.states["P1"].steps_done), 1)

    def test_duplicate_step_scan_rejected(self):
        self.request_plan()
        self.ev("CLEANING_RECORDED", "P1", "2026-09-20T12:10:00+08:00",
                {"step_id": "teardown", "operator_id": "op-a"})
        result = self.ev("CLEANING_RECORDED", "P1", "2026-09-20T12:12:00+08:00",
                         {"step_id": "teardown", "operator_id": "op-b"})
        self.assertFalse(result.accepted)
        self.assertIn("重复扫码", result.errors[0])
        self.assertEqual(len(self.service.states["P1"].steps_done), 1)


class TestReleaseBlocking(ChangeoverCase):
    def test_unreturned_tool_blocks_release(self):
        self.request_plan()
        self.finish_prerequisites()
        self.ev("TOOL_BORROWED", "P1", "2026-09-20T12:30:00+08:00",
                {"tool_id": "M-12", "tool_kind": "成型模具", "from_line_id": "L2", "borrower_id": "op-a"})
        result = self.ev("RELEASE_SIGNED", "P1", "2026-09-20T13:20:00+08:00",
                         {"signer_id": "qm-chen", "decision": "release"})
        self.assertFalse(result.accepted)
        self.assertTrue(any("器具未归还" in e and "M-12" in e for e in result.errors))
        self.assertIsNone(self.service.states["P1"].release)
        # 归还后放行成功
        self.ev("TOOL_RETURNED", "P1", "2026-09-20T13:25:00+08:00", {"tool_id": "M-12"})
        result = self.ev("RELEASE_SIGNED", "P1", "2026-09-20T13:30:00+08:00",
                         {"signer_id": "qm-chen", "decision": "release"})
        self.assertTrue(result.accepted, result.errors)

    def test_expired_verification_blocks_release(self):
        self.request_plan()
        self.finish_prerequisites()
        # v3.0 检测有效期 90 分钟：13:00 取样，14:40 签署时已过期
        result = self.ev("RELEASE_SIGNED", "P1", "2026-09-20T14:40:00+08:00",
                         {"signer_id": "qm-chen", "decision": "release"})
        self.assertFalse(result.accepted)
        self.assertTrue(any("过期" in e for e in result.errors))

    def test_backfilled_record_blocks_release(self):
        self.request_plan(plan="P2", line="L1", from_p="lianrong-yuebing", to_p="danhuang-lianrong")
        self.clean_all("P2")
        self.verify_all("P2")
        self.ev("FIRST_PIECE_CONFIRMED", "P2", "2026-09-20T13:10:00+08:00", {"inspector_id": "qc-zhao"})
        # 交接实际 12:30 发生，13:30 才补录，超过 v3.0 容忍度 15 分钟
        backfill = self.ev("PERSONNEL_HANDOVER_RECORDED", "P2", "2026-09-20T12:30:00+08:00",
                           {"outgoing_id": "op-a", "incoming_id": "op-b"},
                           recorded_at="2026-09-20T13:30:00+08:00")
        self.assertTrue(backfill.backfilled)
        result = self.ev("RELEASE_SIGNED", "P2", "2026-09-20T13:35:00+08:00",
                         {"signer_id": "qm-chen", "decision": "release"})
        self.assertFalse(result.accepted)
        self.assertTrue(any("事后补填" in e for e in result.errors))

    def test_unauthorized_signer_rejected(self):
        self.request_plan()
        self.finish_prerequisites()
        result = self.ev("RELEASE_SIGNED", "P1", "2026-09-20T13:20:00+08:00",
                         {"signer_id": "op-wang", "decision": "release"})
        self.assertFalse(result.accepted)
        self.assertTrue(any("权限" in e or "有权" in e for e in result.errors))

    def test_no_resume_before_release(self):
        self.request_plan()
        self.finish_prerequisites()
        result = self.ev("PRODUCTION_RESUMED", "P1", "2026-09-20T13:20:00+08:00",
                         {"line_id": "L3", "operator_id": "op-a"})
        self.assertFalse(result.accepted)
        self.assertTrue(any("放行" in e for e in result.errors))

    def test_missing_handover_blocks_release(self):
        self.request_plan()
        self.clean_all()
        self.verify_all()
        self.ev("FIRST_PIECE_CONFIRMED", "P1", "2026-09-20T13:10:00+08:00", {"inspector_id": "qc-zhao"})
        result = self.ev("RELEASE_SIGNED", "P1", "2026-09-20T13:20:00+08:00",
                         {"signer_id": "qm-chen", "decision": "release"})
        self.assertFalse(result.accepted)
        self.assertTrue(any("交接" in e for e in result.errors))


class TestHappyPathAndTrace(ChangeoverCase):
    def test_full_flow_and_batch_trace(self):
        self.request_plan()
        self.finish_prerequisites()
        result = self.ev("RELEASE_SIGNED", "P1", "2026-09-20T13:20:00+08:00",
                         {"signer_id": "qm-chen", "decision": "release"})
        self.assertTrue(result.accepted, result.errors)
        result = self.ev("PRODUCTION_RESUMED", "P1", "2026-09-20T13:30:00+08:00",
                         {"line_id": "L3", "operator_id": "op-b"})
        self.assertTrue(result.accepted, result.errors)

        state = self.service.states["P1"]
        self.assertEqual(state.status(state.requested_at), "resumed")
        self.assertEqual(state.blocking_reasons(state.requested_at), [])
        labels = [label for label, _ in state.downtime_breakdown(state.requested_at)]
        self.assertIn("清洁执行", labels)
        self.assertIn("等待放行签署", labels)

        batches = load_batches(DATA_DIR / "batches.jsonl")
        trace = trace_batch("L3-20260920-02", self.service.states, batches)
        self.assertEqual(trace["plan_id"], "P1")
        self.assertEqual(trace["procedure"]["version"], "3.0")
        self.assertEqual(trace["release"]["signer"], "陈质检")
        self.assertTrue(all(s["done"] for s in trace["steps"]))
        self.assertTrue(all(v["passed"] for v in trace["verifications"]))

    def test_state_rebuilt_from_store(self):
        self.request_plan()
        self.finish_prerequisites()
        self.ev("RELEASE_SIGNED", "P1", "2026-09-20T13:20:00+08:00",
                {"signer_id": "qm-chen", "decision": "release"})
        rebuilt = make_service(self._tmp.name)
        self.assertIsNotNone(rebuilt.states["P1"].release)


if __name__ == "__main__":
    unittest.main()
