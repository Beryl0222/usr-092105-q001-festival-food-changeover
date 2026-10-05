"""换线放行应用：命令受理、阻断评估、追溯与停线分析。

硬性规则集中在本模块：
- 器具未归还、检测过期、清洁步骤缺失/不合格、首件未确认：不得签署或恢复；
- 事后补填（received_at 晚于 occurred_at 超过规程宽限、又不在已登记离线时段内）：拒收；
- 重复扫码只确认同一步骤，重复 event_id 由存储幂等，离线重放不产生第二次清洁；
- 签署/恢复必须由 release_authority 角色人员完成；
- 恢复后抽检异常可再 HELD；重新恢复要求 HOLD 之后的复检与重新签署；
- 所有判定阈值取自 PLAN_ISSUED 快照，规程新版本只影响生效之后才排产的换线。
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

from . import execution as ex
from . import planning, release, verification as vr
from .catalog import Catalog, parse_dt
from .store import EventStore, RejectedEvent

# 事件标识按事件事实内容确定性生成：同一事实重试/离线重传时，event_id 不变，
# 存储层据此幂等去重——重传绝不产生第二次清洁、第二次签署。
# received_at（平台每次接收时间不同）与 version 属于传输/编排元数据，不参与指纹。
_EVENT_NS = uuid.UUID("6a9f3c52-7e2e-4d6b-9b3a-2f7d8a1c0e44")
_NON_FACT_FIELDS = ("event_id", "version", "received_at")


def event_fingerprint(event: dict) -> str:
    body = {k: v for k, v in event.items() if k not in _NON_FACT_FIELDS}
    digest = uuid.uuid5(_EVENT_NS, json.dumps(body, ensure_ascii=False, sort_keys=True,
                                              default=str)).hex
    return f"evt-{digest[:16]}"


def _minutes(a: str, b: str) -> int:
    return int(round((parse_dt(b) - parse_dt(a)).total_seconds() / 60))


class WorkshopApp:
    def __init__(self, catalog: Catalog | None = None, store: EventStore | None = None):
        self.catalog = catalog or Catalog.default()
        self.store = store or EventStore()

    # ------------------------------------------------------------------ 内部工具

    def _append(self, event: dict, received_at: str | None = None) -> tuple[dict, bool]:
        if received_at:
            event["received_at"] = received_at
        event.setdefault("event_id", event_fingerprint(event))
        return self.store.append(event)

    def _plan_stream(self, changeover_id: str) -> list[dict]:
        return self.store.events(changeover_id)

    def _plan(self, changeover_id: str) -> dict:
        for e in self._plan_stream(changeover_id):
            if e["event_type"] == "PLAN_ISSUED":
                return e["payload"]["plan"]
        raise RejectedEvent([f"换线 {changeover_id} 尚未下发计划（PLAN_ISSUED 不存在）"])

    def _exec_events(self, changeover_id: str) -> list[dict]:
        return self.store.events(ex.execution_id(changeover_id))

    def _vr_events(self, changeover_id: str) -> list[dict]:
        return self.store.events(vr.verification_id(changeover_id))

    def _rd_events(self, changeover_id: str) -> list[dict]:
        return self.store.events(ex.decision_id(changeover_id))

    def _check_late_backfill(self, changeover_id: str, event: dict,
                             received_at: str | None) -> None:
        """记录事后补填受理规则：超宽限且不在已登记离线时段内即拒收。"""
        if not received_at:
            return
        plan = self._plan(changeover_id)
        grace = plan["procedure_snapshot"]["late_report_grace_minutes"]
        occurred = parse_dt(event["occurred_at"])
        received = parse_dt(received_at)
        if received <= occurred + timedelta(minutes=grace):
            return
        state = ex.build_execution_state(self._exec_events(changeover_id))
        for session in state["offline_sessions"]:
            if parse_dt(session["start"]) <= occurred <= parse_dt(session["end"]):
                return
        raise RejectedEvent(
            [
                f"记录于 {received_at} 补填 {event['occurred_at']} 的事实，超过 {grace} 分钟宽限，"
                "且该时间不在已登记的离线作业时段内——事后补填不予承认，请重新执行并实时记录"
            ]
        )

    # ------------------------------------------------------------------ 排产计划

    def request_changeover(self, changeover_id: str, line: str, from_product: str,
                           to_product: str, scheduled_at: str, requested_by: str) -> dict:
        if self._plan_stream(changeover_id):
            raise RejectedEvent([f"换线 {changeover_id} 已存在，不得重复建单"])
        plan = planning.build_plan(
            self.catalog, changeover_id, line, from_product, to_product,
            scheduled_at, requested_by,
        )
        self._append(planning.request_event(plan))
        self._append(planning.issued_event(plan))
        return plan

    # ------------------------------------------------------------------ 清洁执行

    def start_cleaning(self, changeover_id: str, at: str, operator: str) -> dict:
        plan = self._plan(changeover_id)
        state = ex.build_execution_state(self._exec_events(changeover_id))
        if state["started_at"] and not state["completed_at"]:
            raise RejectedEvent(["清洁已在进行中，重复开工无效"])
        if state["completed_at"]:
            holds = self._release_state(changeover_id)["holds"]
            latest_hold = holds[-1]["at"] if holds else None
            if not latest_hold or latest_hold <= state["completed_at"]:
                raise RejectedEvent(
                    ["清洁已完成且其后没有质量阻断，不得重新开工；确需返工请先由质量发起阻断"]
                )
        return self._append(ex.start_event(changeover_id, at, operator, plan["line"]))[0]

    def confirm_step(self, changeover_id: str, seq: int, at: str, operator: str,
                     received_at: str | None = None) -> dict:
        plan = self._plan(changeover_id)
        valid = {s["seq"]: s["name"] for s in plan["cleaning_steps"]}
        if seq not in valid:
            raise RejectedEvent([f"步骤 {seq} 不在 {plan['procedure_snapshot']['procedure_code']} 要求内"])
        state = ex.build_execution_state(self._exec_events(changeover_id))
        if not state["started_at"]:
            raise RejectedEvent(["尚未开始清洁，不能扫码确认步骤"])
        if state["completed_at"]:
            raise RejectedEvent(["本轮清洁已完成记录，不得再补扫步骤；如需返工请由质量发起阻断后重新清洁"])
        if seq in state["steps"]:
            first = state["steps"][seq]
            raise RejectedEvent(
                [f"步骤{seq}「{valid[seq]}」已于 {first['at']} 由 {first['operator']} 扫码确认，"
                 "重复扫码沿用首次确认，不产生第二次清洁"]
            )
        ev = ex.step_event(changeover_id, seq, valid[seq], at, operator, state["cycle"])
        self._check_late_backfill(changeover_id, ev, received_at)
        return self._append(ev, received_at)[0]

    def loan_equipment(self, changeover_id: str, equipment_code: str, at: str,
                       operator: str) -> dict:
        plan = self._plan(changeover_id)
        item = self.catalog.equipment(equipment_code)
        if not item:
            raise RejectedEvent([f"器具台账中不存在：{equipment_code}"])
        if item["home_line"] == plan["line"]:
            raise RejectedEvent([f"{equipment_code} 本就是 {plan['line']} 线器具，无需跨线借用登记"])
        state = ex.build_execution_state(self._exec_events(changeover_id))
        for rec in reversed(state["loans"]):
            if rec["equipment_code"] == equipment_code and rec["return"] is None:
                raise RejectedEvent([f"{equipment_code} 已登记借入且尚未归还，不得重复借用"])
                break
        return self._append(
            ex.loan_event(changeover_id, equipment_code, item["name"],
                          item["home_line"], plan["line"], at, operator)
        )[0]

    def scan_equipment(self, changeover_id: str, equipment_code: str, at: str,
                       operator: str) -> dict:
        plan = self._plan(changeover_id)
        item = self.catalog.equipment(equipment_code)
        if not item:
            raise RejectedEvent([f"器具台账中不存在：{equipment_code}"])
        cross_line = item["home_line"] != plan["line"]
        state = ex.build_execution_state(self._exec_events(changeover_id))
        if not state["started_at"]:
            raise RejectedEvent(["尚未开始清洁，器具扫码无效"])
        if cross_line and not any(
                rec["equipment_code"] == equipment_code and rec["return"] is None
                for rec in state["loans"]):
            raise RejectedEvent(
                [f"跨线器具 {equipment_code}（{item['name']}）未见有效借用登记，不得直接上线使用"]
            )
        if equipment_code in state["scans"]:
            first = state["scans"][equipment_code]
            raise RejectedEvent(
                [f"器具 {equipment_code} 已于 {first['at']} 扫码清洁确认，"
                 "重复扫码沿用首次确认，不产生第二次清洁"]
            )
        return self._append(
            ex.scan_event(changeover_id, equipment_code, item["name"], at, operator,
                          cross_line, item["home_line"] if cross_line else None,
                          state["cycle"])
        )[0]

    def return_equipment(self, changeover_id: str, equipment_code: str, at: str,
                         operator: str) -> dict:
        plan = self._plan(changeover_id)
        item = self.catalog.equipment(equipment_code)
        if not item:
            raise RejectedEvent([f"器具台账中不存在：{equipment_code}"])
        state = ex.build_execution_state(self._exec_events(changeover_id))
        open_loan = next(
            (rec for rec in reversed(state["loans"])
             if rec["equipment_code"] == equipment_code and rec["return"] is None),
            None,
        )
        if open_loan is None:
            if any(rec["equipment_code"] == equipment_code for rec in state["loans"]):
                raise RejectedEvent([f"{equipment_code} 已归还，重复归还无效"])
            raise RejectedEvent([f"{equipment_code} 没有未结清的借用记录，不能归还"])
        return self._append(
            ex.return_event(changeover_id, equipment_code, item["name"],
                            plan["line"], item["home_line"], at, operator)
        )[0]

    def record_handover(self, changeover_id: str, from_operator: str, to_operator: str,
                        at: str, note: str = "") -> dict:
        self._plan(changeover_id)
        state = ex.build_execution_state(self._exec_events(changeover_id))
        all_seqs = {s["seq"] for s in self._plan(changeover_id)["cleaning_steps"]}
        pending_steps = sorted(all_seqs - set(state["steps"]))
        return self._append(
            ex.handover_event(changeover_id, from_operator, to_operator, at,
                              pending_steps, note)
        )[0]

    def open_offline_session(self, changeover_id: str, start: str, end: str,
                             operator: str, reason: str) -> dict:
        self._plan(changeover_id)
        if parse_dt(end) <= parse_dt(start):
            raise RejectedEvent(["离线时段结束时间必须晚于开始时间"])
        return self._append(
            ex.offline_session_event(changeover_id, start, end, operator, reason)
        )[0]

    def record_cleaning_complete(self, changeover_id: str, at: str, operator: str,
                                 received_at: str | None = None) -> dict:
        plan = self._plan(changeover_id)
        state = ex.build_execution_state(self._exec_events(changeover_id))
        if not state["started_at"]:
            raise RejectedEvent(["尚未开始清洁（缺 CLEANING_STARTED）"])
        missing = [s["seq"] for s in plan["cleaning_steps"] if s["seq"] not in state["steps"]]
        if missing:
            raise RejectedEvent([f"以下清洁步骤未扫码确认，不能记录清洁完成：{missing}"])
        if state["completed_at"]:
            raise RejectedEvent(["清洁完成记录已存在，重复上报不得制造第二次清洁"])
        ev = ex.completion_event(changeover_id, at, operator, len(plan["cleaning_steps"]),
                                 state["cycle"])
        self._check_late_backfill(changeover_id, ev, received_at)
        return self._append(ev, received_at)[0]

    # ------------------------------------------------------------------ 检测

    def _admit_verification(self, changeover_id: str, event: dict,
                            received_at: str | None) -> dict:
        self._check_late_backfill(changeover_id, event, received_at)
        return self._append(event, received_at)[0]

    def record_allergen_swab(self, changeover_id: str, allergen: str,
                             point_results: dict[str, float], at: str, sampled_at: str,
                             operator: str, received_at: str | None = None) -> dict:
        self._plan(changeover_id)
        ev = vr.allergen_swab_event(
            changeover_id, allergen, self.catalog.allergen_name(allergen),
            point_results, at, sampled_at, operator,
        )
        return self._admit_verification(changeover_id, ev, received_at)

    def record_rinse_water(self, changeover_id: str, value_ppm: float, at: str,
                           sampled_at: str, operator: str,
                           received_at: str | None = None) -> dict:
        self._plan(changeover_id)
        ev = vr.rinse_water_event(changeover_id, value_ppm, at, sampled_at, operator)
        return self._admit_verification(changeover_id, ev, received_at)

    def record_atp(self, changeover_id: str, value_rlu: int, at: str, sampled_at: str,
                   operator: str, received_at: str | None = None) -> dict:
        self._plan(changeover_id)
        ev = vr.atp_event(changeover_id, value_rlu, at, sampled_at, operator)
        return self._admit_verification(changeover_id, ev, received_at)

    # ------------------------------------------------------------------ 状态重建

    def _release_state(self, changeover_id: str) -> dict:
        state = {"signed": None, "first_piece": None, "resumed": None, "holds": []}
        for e in self._rd_events(changeover_id):
            p = e.get("payload", {})
            if e["event_type"] == "RELEASE_SIGNED":
                state["signed"] = {"at": e["occurred_at"], "signer": p["signer"],
                                   "signer_name": p["signer_name"], "basis": p["basis"]}
            elif e["event_type"] == "FIRST_PIECE_CONFIRMED":
                state["first_piece"] = {"at": e["occurred_at"], **p}
            elif e["event_type"] == "PRODUCTION_RESUMED":
                state["resumed"] = {"at": e["occurred_at"], **p}
                state["holds"] = [
                    {**h, "cleared_at": e["occurred_at"]} if "cleared_at" not in h else h
                    for h in state["holds"]
                ]
            elif e["event_type"] == "PRODUCTION_HELD":
                state["holds"].append(
                    {"at": e["occurred_at"], "reasons": p["reasons"],
                     "held_by": p["held_by"], "affected_batches": p.get("affected_batches", [])}
                )
        return state

    def _test_eval(self, changeover_id: str, as_of: str) -> list[dict]:
        """对每个应检项目给出最新一次尝试在 as_of 时点的判定。"""
        plan = self._plan(changeover_id)
        snap = plan["procedure_snapshot"]
        vstate = vr.build_verification_state(self._vr_events(changeover_id))
        results = []
        for req in plan["test_requirements"]:
            if req["kind"] == "allergen_swab":
                for item in req["items"]:
                    key = f"allergen:{item['allergen']}"
                    attempt = vstate["latest"].get(key)
                    results.append(self._judge_item(
                        f"{item['allergen_name']}过敏原涂抹",
                        key, attempt, item["limit_ppm"], "ppm", item["focus"], snap, as_of,
                    ))
            elif req["kind"] == "rinse_water":
                attempt = vstate["latest"].get("rinse_water")
                results.append(self._judge_item(
                    "末次冲洗水残留", "rinse_water", attempt,
                    req["limit_ppm"], "ppm", False, snap, as_of,
                ))
            elif req["kind"] == "atp":
                attempt = vstate["latest"].get("atp")
                results.append(self._judge_item(
                    "ATP洁净度", "atp", attempt, req["limit_rlu"], "RLU", False, snap, as_of,
                ))
        return results

    @staticmethod
    def _judge_item(label: str, key: str, attempt: dict | None, limit: float, unit: str,
                    focus: bool, snap: dict, as_of: str) -> dict:
        if not attempt:
            return {"key": key, "label": label, "status": "missing", "focus": focus,
                    "limit": limit, "unit": unit,
                    "text": f"{label}：缺检测结果（限值 {limit}{unit}）"}
        value = attempt["payload"]["value"]
        # 有效期以评估时点为准：签署时合格，拖到恢复时若已过期同样阻断
        expired = parse_dt(attempt["sampled_at"]) + timedelta(
            hours=snap["verification_valid_hours"]) < parse_dt(as_of)
        ok = value <= limit
        points = attempt["payload"].get("point_results")
        over_points = None
        if points:
            over_points = {pt: v for pt, v in points.items() if v > limit}
        if not ok:
            status = "fail"
            detail = f"实测峰值 {value}{unit} 超过限值 {limit}{unit}"
            if over_points:
                detail += f"；超标测点：{over_points}"
        elif expired:
            status = "expired"
            detail = (f"检测采样于 {attempt['sampled_at']}，有效期 "
                      f"{snap['verification_valid_hours']} 小时，"
                      f"截至 {as_of} 已过期，须重新检测")
        else:
            status = "pass"
            detail = f"{value}{unit} ≤ {limit}{unit}"
        tag = "（关键清退项）" if focus else ""
        return {"key": key, "label": label + tag, "status": status, "value": value,
                "limit": limit, "unit": unit, "focus": focus, "sampled_at": attempt["sampled_at"],
                "attempt_at": attempt["at"], "text": f"{label}{tag}：{detail}"}

    # ------------------------------------------------------------------ 阻断评估

    def evaluate(self, changeover_id: str, as_of: str | None = None) -> dict:
        """返回当前阻断清单。班组据此看到不能放行/恢复的全部具体原因。"""
        plan = self._plan(changeover_id)
        exec_state = ex.build_execution_state(self._exec_events(changeover_id))
        release_state = self._release_state(changeover_id)
        known = [plan["scheduled_at"]]
        known.append(exec_state["completed_at"] or "")
        known.append(exec_state["started_at"] or "")
        known += [s["at"] for s in exec_state["steps"].values()]
        known += [s["at"] for s in exec_state["scans"].values()]
        known += [e["occurred_at"] for e in self._vr_events(changeover_id)]
        known += [e["occurred_at"] for e in self._rd_events(changeover_id)]
        as_of = as_of or (release_state["resumed"]["at"] if release_state["resumed"]
                          else max(known))
        blockers: list[dict] = []

        if not exec_state["started_at"]:
            blockers.append({"code": "CLEAN_NOT_STARTED", "text": "清洁尚未开始"})
        elif not exec_state["completed_at"]:
            done = set(exec_state["steps"])
            missing = [s["seq"] for s in plan["cleaning_steps"] if s["seq"] not in done]
            if missing:
                names = {s["seq"]: s["name"] for s in plan["cleaning_steps"]}
                blockers.append({
                    "code": "CLEAN_STEPS_MISSING",
                    "text": f"清洁步骤未完成：{[f'{seq}-{names[seq]}' for seq in missing]}",
                })
            else:
                blockers.append({"code": "CLEAN_NOT_RECORDED",
                                 "text": "步骤已扫但缺清洁完成记录（CLEANING_RECORDED）"})

        # 器具：本线范围内器具须逐件扫码清洁确认；跨线借用借而未还
        if exec_state["completed_at"]:
            for code in plan["equipment_scope"]:
                if code not in exec_state["scans"]:
                    item = self.catalog.equipment(code)
                    blockers.append({
                        "code": "EQUIPMENT_NOT_SCANNED",
                        "text": f"器具 {code}（{item['name'] if item else ''}）未经扫码清洁确认，"
                                "不得放行上线",
                    })
        for rec in exec_state["loans"]:
            if rec["return"] is None:
                code = rec["equipment_code"]
                item = self.catalog.equipment(code)
                blockers.append({
                    "code": "EQUIPMENT_NOT_RETURNED",
                    "text": f"跨线借用器具 {code}（{item['name'] if item else ''}）尚未归还出线，"
                            "存在带残留流转他线风险",
                })

        # 检测判定（含缺结果/超标/过期）
        tests = self._test_eval(changeover_id, as_of)
        for t in tests:
            if t["status"] == "missing":
                blockers.append({"code": "TEST_MISSING", "text": t["text"]})
            elif t["status"] == "fail":
                blockers.append({"code": "TEST_FAILED", "text": t["text"]})
            elif t["status"] == "expired":
                blockers.append({"code": "TEST_EXPIRED", "text": t["text"]})

        last_hold = release_state["holds"][-1] if release_state["holds"] else None
        signed = release_state["signed"]
        first_piece = release_state["first_piece"]

        # 阻断后的复检必须在阻断之后；阻断前的合格检测随返工失效
        if last_hold:
            for t in tests:
                if t["status"] == "pass" and t["attempt_at"] <= last_hold["at"]:
                    blockers.append({
                        "code": "TEST_PREDATES_HOLD",
                        "text": f"{t['label']}：检测（{t['attempt_at']}）早于最近一次阻断"
                                f"（{last_hold['at']}），返工后须重新检测",
                    })

        if not signed:
            blockers.append({"code": "NOT_SIGNED",
                             "text": "尚无有权质量人员放行签署（RELEASE_SIGNED）"})
        elif last_hold and signed["at"] <= last_hold["at"]:
            blockers.append({
                "code": "SIGN_PREDATES_HOLD",
                "text": f"最近一次质量阻断（{last_hold['at']}）晚于现有签署（{signed['at']}），"
                        "须复检后重新签署",
            })

        if not first_piece:
            blockers.append({"code": "FIRST_PIECE_MISSING", "text": "首件尚未确认"})
        elif not (first_piece["label_ok"] and first_piece["appearance_ok"]):
            blockers.append({"code": "FIRST_PIECE_NG",
                             "text": "首件确认存在不合格项，不得恢复"})
        elif last_hold and first_piece["at"] <= last_hold["at"]:
            blockers.append({"code": "FIRST_PIECE_PREDATES_HOLD",
                             "text": "首件确认早于最近一次阻断，阻断后须重新首件确认"})

        return {
            "changeover_id": changeover_id,
            "as_of": as_of,
            "procedure_code": plan["procedure_snapshot"]["procedure_code"],
            "risk_mode": plan["risk_mode"],
            "blocked": bool(blockers),
            "blockers": blockers,
            "tests": tests,
            "plan": plan,
        }

    # ------------------------------------------------------------------ 签署/恢复

    def sign_release(self, changeover_id: str, at: str, signer: str) -> dict:
        if not self.catalog.has_role(signer, "release_authority"):
            person = self.catalog.signer(signer)
            name = person["name"] if person else signer
            raise RejectedEvent([f"{name}（{signer}）不具备放行签署权限 release_authority"])
        release_state = self._release_state(changeover_id)
        latest_hold = release_state["holds"][-1]["at"] if release_state["holds"] else None
        if release_state["signed"] and (
                not latest_hold or release_state["signed"]["at"] > latest_hold):
            raise RejectedEvent(["放行已签署且其后无质量阻断，重复签署无效"])
        assessment = self.evaluate(changeover_id, at)
        gate_codes = {"CLEAN_NOT_STARTED", "CLEAN_STEPS_MISSING", "CLEAN_NOT_RECORDED",
                      "EQUIPMENT_NOT_RETURNED", "EQUIPMENT_NOT_SCANNED",
                      "TEST_MISSING", "TEST_FAILED", "TEST_EXPIRED", "TEST_PREDATES_HOLD"}
        gates = [b for b in assessment["blockers"] if b["code"] in gate_codes]
        if gates:
            raise RejectedEvent(["存在阻断项，不能签署放行："] + [b["text"] for b in gates])
        plan = assessment["plan"]
        person = self.catalog.signer(signer)
        basis = [t["text"] for t in assessment["tests"] if t["status"] == "pass"]
        basis.append(f"清洁{len(plan['cleaning_steps'])}步骤完成、跨线器具均已归还")
        return self._append(
            release.signed_event(changeover_id, at, signer, person["name"],
                                 plan["procedure_snapshot"]["procedure_code"], basis)
        )[0]

    def confirm_first_piece(self, changeover_id: str, at: str, oven_batch_id: str,
                            confirmer: str, label_ok: bool, appearance_ok: bool,
                            note: str = "") -> dict:
        if not self.catalog.has_role(confirmer, "first_piece_authority"):
            person = self.catalog.signer(confirmer)
            name = person["name"] if person else confirmer
            raise RejectedEvent([f"{name}（{confirmer}）不具备首件确认权限"])
        plan = self._plan(changeover_id)
        release_state = self._release_state(changeover_id)
        signed = release_state["signed"]
        if not signed:
            raise RejectedEvent(["放行尚未签署，不能进行首件确认"])
        latest_hold = release_state["holds"][-1]["at"] if release_state["holds"] else None
        if latest_hold and signed["at"] <= latest_hold:
            raise RejectedEvent(["现有放行签署早于最近一次质量阻断，须复检重新签署后再做首件确认"])
        existing = release_state["first_piece"]
        if existing and (not latest_hold or existing["at"] > latest_hold):
            raise RejectedEvent([f"首件已于 {existing['at']} 确认，重复确认不产生第二次记录"])
        if not (label_ok and appearance_ok):
            raise RejectedEvent([
                f"首炉 {oven_batch_id} 首件不合格（标签核对={label_ok}，外观={appearance_ok}），"
                "不得确认；请保持停线并由质量发起 PRODUCTION_HELD"
            ])
        person = self.catalog.signer(confirmer)
        return self._append(
            release.first_piece_event(changeover_id, at, oven_batch_id,
                                      plan["to_product"]["name"], confirmer, person["name"],
                                      label_ok, appearance_ok, note)
        )[0]

    def resume_production(self, changeover_id: str, at: str, first_oven_batch_id: str,
                          approver: str) -> dict:
        if not self.catalog.has_role(approver, "release_authority"):
            person = self.catalog.signer(approver)
            name = person["name"] if person else approver
            raise RejectedEvent([f"{name}（{approver}）不具备恢复生产批准权限"])
        release_state = self._release_state(changeover_id)
        latest_hold = release_state["holds"][-1]["at"] if release_state["holds"] else None
        if release_state["resumed"] and (
                not latest_hold or release_state["resumed"]["at"] > latest_hold):
            raise RejectedEvent(["生产已恢复且其后无质量阻断，重复恢复无效"])
        assessment = self.evaluate(changeover_id, at)
        if assessment["blocked"]:
            raise RejectedEvent(
                ["阻断未解除，不得恢复生产："] + [b["text"] for b in assessment["blockers"]]
            )
        plan = assessment["plan"]
        person = self.catalog.signer(approver)
        cleared: list[str] = []
        for h in self._release_state(changeover_id)["holds"]:
            if "cleared_at" not in h:
                cleared.extend(h["reasons"])
        return self._append(
            release.resumed_event(changeover_id, at, first_oven_batch_id,
                                  plan["to_product"]["name"], approver, person["name"],
                                  cleared)
        )[0]

    def hold_production(self, changeover_id: str, at: str, reasons: list[str],
                        held_by: str, affected_batches: list[str] | None = None) -> dict:
        self._plan(changeover_id)
        if not self.catalog.has_role(held_by, "quality") and not self.catalog.has_role(
                held_by, "release_authority"):
            raise RejectedEvent([f"{held_by} 不具备质量阻断权限"])
        if not reasons:
            raise RejectedEvent(["质量阻断必须填写原因"])
        person = self.catalog.signer(held_by)
        return self._append(
            release.held_event(changeover_id, at, reasons, held_by, person["name"],
                               affected_batches)
        )[0]

    # ------------------------------------------------------------------ 追溯与分析

    def replay(self, changeover_id: str) -> dict:
        """组装一个换线的完整事实链，供质量复核。"""
        plan = self._plan(changeover_id)
        return {
            "changeover_id": changeover_id,
            "plan": plan,
            "cleaning": ex.build_execution_state(self._exec_events(changeover_id)),
            "cleaning_events": self._exec_events(changeover_id),
            "verification": vr.build_verification_state(self._vr_events(changeover_id)),
            "verification_events": self._vr_events(changeover_id),
            "release": self._release_state(changeover_id),
            "release_events": self._rd_events(changeover_id),
            "assessment": self.evaluate(changeover_id),
        }

    def locate_changeover(self, line: str, produced_at: str) -> str | None:
        """按炉次生产时点定位换线：该线最近一次已恢复、且恢复不晚于炉次时点的换线。"""
        candidates = []
        for e in self.store.all_events():
            if e["event_type"] != "PRODUCTION_RESUMED":
                continue
            cid = e["payload"]["changeover_id"]
            plan = self._plan(cid)
            if plan["line"] == line and e["occurred_at"] <= produced_at:
                candidates.append((e["occurred_at"], cid))
        return max(candidates)[1] if candidates else None

    def trace_oven_batch(self, batch_id: str, line: str | None = None,
                         produced_at: str | None = None) -> dict:
        """从任一成品炉次还原：当时采用的规程版本/阈值、检测、签署与恢复决定。

        定位优先级：首炉登记、HOLD 影响炉次清单、按线+生产时点匹配最近一次恢复。
        """
        matched = None
        match_via = None
        for e in self.store.all_events():
            p = e.get("payload", {})
            if p.get("changeover_id") is None:
                continue
            if e["event_type"] in ("FIRST_PIECE_CONFIRMED", "PRODUCTION_RESUMED"):
                if p.get("oven_batch_id") == batch_id or p.get("first_oven_batch_id") == batch_id:
                    matched = p["changeover_id"]
                    match_via = "首炉登记"
                    break
            if e["event_type"] == "PRODUCTION_HELD" and batch_id in p.get("affected_batches", []):
                matched = p["changeover_id"]
                match_via = "质量阻断影响炉次清单"
                break
        if matched is None and line and produced_at:
            matched = self.locate_changeover(line, produced_at)
            match_via = f"{line}线 {produced_at} 时点最近恢复" if matched else None
        if matched is None:
            raise RejectedEvent([f"找不到炉次 {batch_id} 对应的换线记录"])
        bundle = self.replay(matched)
        bundle["batch_id"] = batch_id
        bundle["matched_via"] = match_via
        return bundle

    def downtime_report(self, changeover_id: str) -> dict:
        """停线构成：清洁、检测等待、签署/首件等待、质量阻断分段。"""
        plan = self._plan(changeover_id)
        exec_state = ex.build_execution_state(self._exec_events(changeover_id))
        release_state = self._release_state(changeover_id)
        vr_events = self._vr_events(changeover_id)
        start = exec_state["started_at"]
        end = release_state["resumed"]["at"] if release_state["resumed"] else None
        if not start:
            return {"changeover_id": changeover_id, "started": False, "segments": [],
                    "blocks": [], "ongoing": True}

        last_test = max((e["occurred_at"] for e in vr_events), default=None)
        signed_at = release_state["signed"]["at"] if release_state["signed"] else None
        first_piece_at = (release_state["first_piece"]["at"]
                          if release_state["first_piece"] else None)
        completed_at = exec_state["completed_at"]

        segments = []
        if completed_at:
            segments.append(("换线清洁作业（拆洗/冲洗/扫码）", start, completed_at))
            if last_test:
                segments.append(("清洁后检测与等待结果", completed_at, last_test))
                gate_end = signed_at or last_test
                if gate_end > last_test:
                    segments.append(("等待质量签署/整改复检", last_test, gate_end))
            else:
                segments.append(("等待检测（尚无任何结果）", completed_at,
                                 signed_at or end))
            if signed_at and first_piece_at:
                segments.append(("首件生产与确认", signed_at, first_piece_at))
            if first_piece_at and end and end > first_piece_at:
                segments.append(("等待恢复批准", first_piece_at, end))
        else:
            segments.append(("清洁进行中/未完成", start, end))

        blocks = []
        holds = release_state["holds"]
        for i, h in enumerate(holds):
            block_end = h.get("cleared_at") or end
            blocks.append({
                "reasons": h["reasons"],
                "start": h["at"],
                "end": block_end,
                "minutes": _minutes(h["at"], block_end) if block_end else None,
                "affected_batches": h["affected_batches"],
            })

        rendered = [
            {"name": label, "start": a, "end": b, "minutes": _minutes(a, b) if b else None}
            for label, a, b in segments
        ]
        total = _minutes(start, end) if end else None
        return {
            "changeover_id": changeover_id,
            "started": True,
            "line_stop_start": start,
            "line_stop_end": end,
            "ongoing": end is None,
            "total_minutes": total,
            "segments": rendered,
            "blocks": blocks,
            "current_blockers": self.evaluate(changeover_id)["blockers"],
        }
