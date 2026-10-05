"""换线放行规则引擎：受理事件、执行阻断规则、维护投影。

规则一览（全部在事件落库前判断，被拒绝的事件不进入事件流）：
- 幂等：event_id 重复（离线重传/来源重试）直接忽略；同一清洁步骤重复扫码拒绝；
- 计划：按换线发起时刻生效的规程版本生成要求快照，新版本不追溯；
- 阻断放行：清洁缺项、检测缺失/未通过/已过期/早于清洁完成、首件未确认、
  借用器具未归还、人员交接未记录、存在事后补填记录；
- 放行：必须由对产线有权限的人员签署；
- 恢复生产：必须先完成放行签署。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .masterdata import MasterData, parse_ts
from .planning import build_requirements
from .projections import ChangeoverState
from .store import EventStore
from .validator import validate_event

AGGREGATE_OF = {
    "CHANGEOVER_REQUESTED": "changeover_plan",
    "CLEANING_RECORDED": "cleaning_execution",
    "TOOL_BORROWED": "cleaning_execution",
    "TOOL_RETURNED": "cleaning_execution",
    "PERSONNEL_HANDOVER_RECORDED": "cleaning_execution",
    "VERIFICATION_RECEIVED": "verification_result",
    "FIRST_PIECE_CONFIRMED": "verification_result",
    "RELEASE_SIGNED": "release_decision",
    "PRODUCTION_RESUMED": "release_decision",
}


@dataclass
class ApplyResult:
    accepted: bool
    duplicate: bool = False
    backfilled: bool = False
    errors: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)


class ChangeoverService:
    def __init__(self, store: EventStore, master: MasterData):
        self.store = store
        self.master = master
        self.states: dict[str, ChangeoverState] = {}
        self._backfill_flags: dict[str, bool] = {}
        self._versions: dict[tuple[str, str], int] = {}
        for event in store.all():
            # 重放时按同一规则重新判定补填，保证重启后阻断状态不丢失
            if self._is_backfilled(event):
                self._backfill_flags[event["event_id"]] = True
            self._project(event)

    # ---- 对外入口 ----

    def next_version(self, aggregate_type: str, aggregate_id: str) -> int:
        return self._versions.get((aggregate_type, aggregate_id), 0) + 1

    def apply(self, event: dict) -> ApplyResult:
        errors = validate_event(event)
        if errors:
            return ApplyResult(accepted=False, errors=errors)
        expected_agg = AGGREGATE_OF.get(event["event_type"])
        if expected_agg != event["aggregate_type"]:
            return ApplyResult(
                accepted=False,
                errors=[f"事件类型 {event['event_type']} 应挂在聚合 {expected_agg}，而非 {event['aggregate_type']}"],
            )

        # 幂等：同一 event_id 的离线重传/重试不重复入账
        if event["event_id"] in self.store:
            return ApplyResult(accepted=True, duplicate=True, notices=["重复事件（event_id 已存在），按幂等忽略"])

        # 版本连续性：防止离线设备伪造流顺序
        key = (event["aggregate_type"], event["aggregate_id"])
        expected_version = self._versions.get(key, 0) + 1
        if event["version"] != expected_version:
            return ApplyResult(
                accepted=False,
                errors=[f"版本不连续：{event['aggregate_type']}/{event['aggregate_id']} 期望 version={expected_version}，收到 {event['version']}"],
            )

        handler = getattr(self, f"_on_{event['event_type'].lower()}")
        result = handler(event)
        if result.errors:
            return result

        # 受理时固化记录时间，保证重放判定一致
        if not event.get("recorded_at"):
            event["recorded_at"] = datetime.now(timezone.utc).isoformat()
        # 事后补填判定：记录时间晚于事实发生时间超过规程容忍度
        if self._is_backfilled(event):
            result.backfilled = True
            result.notices.append(
                f"记录时间晚于发生时间超过 {self._tolerance_minutes(event['aggregate_id'], parse_ts(event['occurred_at']))} 分钟，"
                "判定为事后补填，本次换线将被阻断"
            )
            self._backfill_flags[event["event_id"]] = True

        self.store.append(event)
        self._project(event)
        result.accepted = True
        return result

    def _is_backfilled(self, event: dict) -> bool:
        occurred = parse_ts(event["occurred_at"])
        recorded = parse_ts(event["recorded_at"])
        tolerance = self._tolerance_minutes(event["aggregate_id"], occurred)
        return recorded - occurred > timedelta(minutes=tolerance)

    # ---- 投影 ----

    def _project(self, event: dict) -> None:
        plan_id = event["aggregate_id"]
        state = self.states.setdefault(plan_id, ChangeoverState(plan_id))
        state.apply(event, backfilled=self._backfill_flags.get(event["event_id"], False))
        key = (event["aggregate_type"], event["aggregate_id"])
        self._versions[key] = max(self._versions.get(key, 0), event["version"])

    def _tolerance_minutes(self, plan_id: str, at: datetime) -> int:
        state = self.states.get(plan_id)
        if state and state.requirements:
            return state.requirements["backfill_tolerance_minutes"]
        return self.master.procedure_at(at).backfill_tolerance_minutes

    def _state(self, event: dict) -> tuple[ChangeoverState | None, ApplyResult | None]:
        state = self.states.get(event["aggregate_id"])
        if state is None or state.requirements is None:
            return None, ApplyResult(accepted=False, errors=[f"换线计划 {event['aggregate_id']} 不存在，请先登记 CHANGEOVER_REQUESTED"])
        if state.release is not None:
            return None, ApplyResult(accepted=False, errors=[f"换线 {event['aggregate_id']} 已放行，执行类记录不再受理"])
        return state, None

    # ---- 各事件处理器 ----

    def _on_changeover_requested(self, event: dict) -> ApplyResult:
        payload = event.get("payload", {})
        plan_id = event["aggregate_id"]
        if plan_id in self.states and self.states[plan_id].requirements is not None:
            return ApplyResult(accepted=False, errors=[f"换线计划 {plan_id} 已存在，不能重复登记"])
        for field_name in ("line_id", "from_product_id", "to_product_id"):
            if field_name not in payload:
                return ApplyResult(accepted=False, errors=[f"payload 缺少字段：{field_name}"])
        try:
            requirements = build_requirements(
                self.master,
                payload["from_product_id"],
                payload["to_product_id"],
                parse_ts(event["occurred_at"]),
            )
        except ValueError as exc:
            return ApplyResult(accepted=False, errors=[str(exc)])
        payload["requirements"] = requirements
        event["payload"] = payload
        return ApplyResult(
            accepted=True,
            notices=[
                f"风险等级 {requirements['risk_level']}：{requirements['risk_reason']}",
                f"按规程 {requirements['procedure_id']} v{requirements['procedure_version']} 生成 "
                f"{len(requirements['steps'])} 个清洁步骤、{len(requirements['verifications'])} 项检测"
                + ("、首件确认" if requirements["first_piece_required"] else ""),
            ],
        )

    def _on_cleaning_recorded(self, event: dict) -> ApplyResult:
        state, err = self._state(event)
        if err:
            return err
        step_id = event.get("payload", {}).get("step_id")
        required = {s["step_id"]: s["name"] for s in state.requirements["steps"]}
        if step_id not in required:
            return ApplyResult(accepted=False, errors=[f"步骤 {step_id} 不在本次换线要求中"])
        if step_id in state.steps_done:
            return ApplyResult(
                accepted=False,
                errors=[f"步骤「{required[step_id]}」已记录过，重复扫码不会生成第二次清洁"],
            )
        return ApplyResult(accepted=True)

    def _on_tool_borrowed(self, event: dict) -> ApplyResult:
        state, err = self._state(event)
        if err:
            return err
        payload = event.get("payload", {})
        for field_name in ("tool_id", "tool_kind", "from_line_id", "borrower_id"):
            if field_name not in payload:
                return ApplyResult(accepted=False, errors=[f"payload 缺少字段：{field_name}"])
        if payload["tool_id"] in state.tools_outstanding:
            return ApplyResult(accepted=False, errors=[f"器具 {payload['tool_id']} 已处于借出未还状态"])
        return ApplyResult(accepted=True)

    def _on_tool_returned(self, event: dict) -> ApplyResult:
        state, err = self._state(event)
        if err:
            return err
        tool_id = event.get("payload", {}).get("tool_id")
        if tool_id not in state.tools_outstanding:
            return ApplyResult(accepted=False, errors=[f"器具 {tool_id} 没有未归还的借用记录"])
        return ApplyResult(accepted=True)

    def _on_personnel_handover_recorded(self, event: dict) -> ApplyResult:
        state, err = self._state(event)
        if err:
            return err
        payload = event.get("payload", {})
        for field_name in ("outgoing_id", "incoming_id"):
            if field_name not in payload:
                return ApplyResult(accepted=False, errors=[f"payload 缺少字段：{field_name}"])
        return ApplyResult(accepted=True)

    def _on_verification_received(self, event: dict) -> ApplyResult:
        state, err = self._state(event)
        if err:
            return err
        payload = event.get("payload", {})
        for field_name in ("kind", "value", "performed_at", "lab_id"):
            if field_name not in payload:
                return ApplyResult(accepted=False, errors=[f"payload 缺少字段：{field_name}"])
        key = (payload["kind"], payload.get("target_allergen"))
        spec = next(
            (v for v in state.requirements["verifications"] if (v["kind"], v.get("target_allergen")) == key),
            None,
        )
        if spec is None:
            return ApplyResult(accepted=False, errors=[f"检测项 {payload['kind']} 不在本次换线要求中"])
        # 用计划快照中的阈值判定，规程升级不影响本次换线
        if payload["kind"] == "allergen_swab":
            passed = float(payload["value"]) <= spec["limit_ppm"]
            payload["limit"] = f"≤{spec['limit_ppm']} ppm"
        elif payload["kind"] == "atp":
            passed = float(payload["value"]) <= spec["limit_rlu"]
            payload["limit"] = f"≤{spec['limit_rlu']} RLU"
        else:
            return ApplyResult(accepted=False, errors=[f"未知检测类型：{payload['kind']}"])
        payload["passed"] = passed
        payload["name"] = spec["name"]
        event["payload"] = payload
        notices = [f"{spec['name']} 结果 {payload['value']}（限值 {payload['limit']}）：{'合格' if passed else '不合格'}"]
        if not passed:
            notices.append("检测不合格，需重新清洁后再次取样")
        return ApplyResult(accepted=True, notices=notices)

    def _on_first_piece_confirmed(self, event: dict) -> ApplyResult:
        state, err = self._state(event)
        if err:
            return err
        if state.first_piece is not None:
            return ApplyResult(accepted=False, errors=["首件确认已完成，重复确认无效"])
        return ApplyResult(accepted=True)

    def _on_release_signed(self, event: dict) -> ApplyResult:
        state = self.states.get(event["aggregate_id"])
        if state is None or state.requirements is None:
            return ApplyResult(accepted=False, errors=[f"换线计划 {event['aggregate_id']} 不存在"])
        if state.release is not None:
            return ApplyResult(accepted=False, errors=["本次换线已有放行决定，不能重复签署"])
        payload = event.get("payload", {})
        at = parse_ts(event["occurred_at"])
        errors: list[str] = []
        auth_error = self.master.signer_authorized(payload.get("signer_id", ""), state.line_id, at)
        if auth_error:
            errors.append(auth_error)
        errors.extend(state.blocking_reasons(at))
        if errors:
            return ApplyResult(accepted=False, errors=["放行被阻断："] + errors)
        payload["signer_name"] = self.master.signers[payload["signer_id"]].name
        event["payload"] = payload
        return ApplyResult(accepted=True, notices=[f"放行签署人：{payload['signer_name']}"])

    def _on_production_resumed(self, event: dict) -> ApplyResult:
        state = self.states.get(event["aggregate_id"])
        if state is None or state.requirements is None:
            return ApplyResult(accepted=False, errors=[f"换线计划 {event['aggregate_id']} 不存在"])
        if state.release is None:
            return ApplyResult(accepted=False, errors=["未完成放行签署，不得恢复生产"])
        if state.resumed is not None:
            return ApplyResult(accepted=False, errors=["已恢复生产，重复操作无效"])
        return ApplyResult(accepted=True)
