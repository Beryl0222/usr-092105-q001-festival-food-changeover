"""命令行入口：

    python3 -m src.cli demo                 运行中秋换线演示场景（含阻断与恢复）
    python3 -m src.cli board [--line L3]    查看产线换线看板：状态、阻断原因、停线构成
    python3 -m src.cli trace --batch ID     从成品炉次还原规程版本、检测与放行决定
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from .masterdata import DATA_DIR, MasterData, parse_ts
from .projections import STATUS_LABELS, ChangeoverState, format_duration
from .service import AGGREGATE_OF, ChangeoverService
from .store import EventStore
from .trace import load_batches, trace_batch

DEFAULT_STORE = DATA_DIR / "events.jsonl"


def make_event(service: ChangeoverService, event_type: str, plan_id: str, occurred_at: str,
               summary: str, payload: dict, event_id: str, recorded_at: str | None = None) -> dict:
    aggregate_type = AGGREGATE_OF[event_type]
    event = {
        "event_id": event_id,
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": plan_id,
        "occurred_at": occurred_at,
        "version": service.next_version(aggregate_type, plan_id),
        "summary": summary,
        "payload": payload,
    }
    if recorded_at:
        event["recorded_at"] = recorded_at
    return event


def report(result) -> None:
    for notice in result.notices:
        print(f"    ℹ {notice}")
    for error in result.errors:
        print(f"    ✗ {error}")
    if result.duplicate:
        print("    ↺ 幂等忽略，未产生新记录")


def print_board(service: ChangeoverService, line_id: str | None, at: datetime) -> None:
    plans = [s for s in service.states.values() if s.requirements and (line_id is None or s.line_id == line_id)]
    if not plans:
        print("该产线当前没有换线记录。")
        return
    for state in sorted(plans, key=lambda s: s.requested_at):
        req = state.requirements
        print(f"\n══ 产线 {state.line_id} · 换线 {state.plan_id} ══")
        print(f"  {req['from_product']['name']} → {req['to_product']['name']}"
              f"｜风险 {req['risk_level']}｜规程 {req['procedure_id']} v{req['procedure_version']}")
        print(f"  状态：{STATUS_LABELS[state.status(at)]}")
        reasons = state.blocking_reasons(at)
        if reasons:
            print("  阻断原因：")
            for reason in reasons:
                print(f"    ✗ {reason}")
        elif state.release is None:
            print("  阻断原因：无，可提请放行签署")
        print("  停线构成：")
        for label, duration in state.downtime_breakdown(at):
            print(f"    · {label}：{format_duration(duration)}")


def print_trace(trace: dict) -> None:
    batch = trace["batch"]
    proc = trace["procedure"]
    print(f"\n══ 炉次追溯：{batch['batch_id']} ══")
    print(f"  产线 {batch['line_id']}｜投料 {batch['started_at']}｜换线计划 {trace['plan_id']}")
    print(f"  当时规程：{proc['title']}（{proc['id']}）v{proc['version']}｜风险 {proc['risk_level']}")
    print(f"  风险判定：{proc['risk_reason']}")
    print(f"  产品切换：{trace['products']['from']['name']} → {trace['products']['to']['name']}")
    print("  清洁执行：")
    for step in trace["steps"]:
        mark = "✓" if step["done"] else "✗"
        print(f"    {mark} {step['name']}（{step['at'] or '未做'}，操作人 {step['operator'] or '-'}）")
    print("  检测记录：")
    for v in trace["verifications"]:
        print(f"    {'✓' if v['passed'] else '✗'} {v['name']}：{v['value']}（限值 {v['limit']}，{v['performed_at']}，{v['lab_id']}）")
    fp = trace["first_piece"]
    print(f"  首件确认：{'已确认' if fp['confirmed_at'] else '未确认'}（要求 {'需要' if fp['required'] else '不需要'}，确认人 {fp['inspector'] or '-'}）")
    rel = trace["release"]
    print(f"  放行决定：{rel['signed_at']} 由 {rel['signer']}（{rel['signer_id']}）签署")
    print(f"  恢复生产：{trace['resumed_at']}")
    if trace["backfilled_events"]:
        print(f"  ⚠ 本次换线存在事后补填记录：{'、'.join(trace['backfilled_events'])}")


def run_demo(service: ChangeoverService) -> None:
    now = datetime(2026, 9, 20, 13, 30, tzinfo=parse_ts("2026-09-20T00:00:00+08:00").tzinfo)
    plan = "CO-20260920-L3-01"
    seq = iter(range(1, 100))

    def ev(event_type, plan_id, occurred, summary, payload, recorded_at=None):
        eid = f"evt-0920-{next(seq):03d}"
        # 正常操作即做即录：记录时间等于发生时间；仅补录场景显式晚于发生时间
        event = make_event(service, event_type, plan_id, occurred, summary, payload, eid,
                           recorded_at or occurred)
        print(f"\n[{occurred[11:16]}] {summary}")
        result = service.apply(event)
        report(result)
        return event, result

    print("═══ 屯昌联合工坊 · 中秋排产换线演示（产线 L3：五仁月饼 → 纯白莲蓉月饼）═══")

    ev("CHANGEOVER_REQUESTED", plan, "2026-09-20T12:00:00+08:00",
       "登记换线计划：五仁月饼 → 纯白莲蓉月饼",
       {"line_id": "L3", "from_product_id": "wuren-yuebing", "to_product_id": "lianrong-yuebing"})

    ev("CLEANING_RECORDED", plan, "2026-09-20T12:10:00+08:00", "扫码记录：拆洗完成",
       {"step_id": "teardown", "operator_id": "op-wang"})
    ev("CLEANING_RECORDED", plan, "2026-09-20T12:25:00+08:00", "扫码记录：碱洗完成",
       {"step_id": "wash", "operator_id": "op-wang"})
    ev("CLEANING_RECORDED", plan, "2026-09-20T12:40:00+08:00", "扫码记录：碱洗完成（员工重复扫码）",
       {"step_id": "wash", "operator_id": "op-wang"})
    rinse_event, _ = ev("CLEANING_RECORDED", plan, "2026-09-20T12:40:00+08:00", "扫码记录：冲洗完成",
                        {"step_id": "rinse", "operator_id": "op-wang"})
    ev("CLEANING_RECORDED", plan, "2026-09-20T12:50:00+08:00", "扫码记录：吹干完成",
       {"step_id": "dry", "operator_id": "op-li"})
    ev("CLEANING_RECORDED", plan, "2026-09-20T12:55:00+08:00", "扫码记录：目视检查完成",
       {"step_id": "inspect", "operator_id": "op-li"})

    ev("TOOL_BORROWED", plan, "2026-09-20T12:30:00+08:00", "跨线借用：从 L2 借入成型模具 M-12",
       {"tool_id": "M-12", "tool_kind": "成型模具", "from_line_id": "L2", "borrower_id": "op-wang"})
    ev("TOOL_BORROWED", plan, "2026-09-20T12:35:00+08:00", "跨线借用：从 L2 借入周转筐 B-08",
       {"tool_id": "B-08", "tool_kind": "周转筐", "from_line_id": "L2", "borrower_id": "op-li"})
    ev("PERSONNEL_HANDOVER_RECORDED", plan, "2026-09-20T13:00:00+08:00", "人员交接：早班王班长 → 中班李班长",
       {"outgoing_id": "op-wang", "incoming_id": "op-li", "shift": "早班→中班"})

    ev("VERIFICATION_RECEIVED", plan, "2026-09-20T13:05:00+08:00", "检测回传：坚果残留擦拭 0.4 ppm",
       {"kind": "allergen_swab", "target_allergen": "坚果", "value": 0.4, "unit": "ppm",
        "performed_at": "2026-09-20T12:58:00+08:00", "lab_id": "lab-01"})
    ev("VERIFICATION_RECEIVED", plan, "2026-09-20T13:06:00+08:00", "检测回传：芝麻残留擦拭 0.2 ppm",
       {"kind": "allergen_swab", "target_allergen": "芝麻", "value": 0.2, "unit": "ppm",
        "performed_at": "2026-09-20T12:59:00+08:00", "lab_id": "lab-01"})
    ev("VERIFICATION_RECEIVED", plan, "2026-09-20T13:06:00+08:00", "检测回传：蛋残留擦拭 0.1 ppm",
       {"kind": "allergen_swab", "target_allergen": "蛋", "value": 0.1, "unit": "ppm",
        "performed_at": "2026-09-20T13:00:00+08:00", "lab_id": "lab-01"})
    ev("VERIFICATION_RECEIVED", plan, "2026-09-20T13:07:00+08:00", "检测回传：ATP 62 RLU",
       {"kind": "atp", "value": 62, "unit": "RLU",
        "performed_at": "2026-09-20T13:01:00+08:00", "lab_id": "lab-01"})
    ev("FIRST_PIECE_CONFIRMED", plan, "2026-09-20T13:10:00+08:00", "首件确认：白莲蓉首件外观与标识合格",
       {"inspector_id": "qc-zhao"})

    ev("RELEASE_SIGNED", plan, "2026-09-20T13:15:00+08:00", "质量经理尝试放行签署（模具、周转筐未归还）",
       {"signer_id": "qm-chen", "decision": "release"})

    print("\n── 阻断时的班组看板 ──")
    print_board(service, "L3", parse_ts("2026-09-20T13:15:00+08:00"))

    ev("TOOL_RETURNED", plan, "2026-09-20T13:20:00+08:00", "归还模具 M-12 至 L2", {"tool_id": "M-12"})
    ev("TOOL_RETURNED", plan, "2026-09-20T13:22:00+08:00", "归还周转筐 B-08 至 L2", {"tool_id": "B-08"})
    ev("RELEASE_SIGNED", plan, "2026-09-20T13:25:00+08:00", "质量经理放行签署",
       {"signer_id": "qm-chen", "decision": "release"})
    ev("PRODUCTION_RESUMED", plan, "2026-09-20T13:30:00+08:00", "产线 L3 恢复生产白莲蓉月饼",
       {"line_id": "L3", "operator_id": "op-li"})

    print("\n[重传] 扫码枪离线缓存的「冲洗完成」记录联网重传（同一 event_id）")
    report(service.apply(dict(rinse_event)))

    print("\n── 恢复生产后的班组看板 ──")
    print_board(service, "L3", now)

    print("\n═══ 场景二：L1 换线记录事后补填（白莲蓉 → 双黄莲蓉）═══")
    plan2 = "CO-20260920-L1-01"
    ev("CHANGEOVER_REQUESTED", plan2, "2026-09-20T12:00:00+08:00",
       "登记换线计划：白莲蓉 → 双黄莲蓉",
       {"line_id": "L1", "from_product_id": "lianrong-yuebing", "to_product_id": "danhuang-lianrong"})
    ev("CLEANING_RECORDED", plan2, "2026-09-20T12:20:00+08:00", "扫码记录：碱洗完成",
       {"step_id": "wash", "operator_id": "op-zhou"})
    ev("CLEANING_RECORDED", plan2, "2026-09-20T12:30:00+08:00", "扫码记录：冲洗完成",
       {"step_id": "rinse", "operator_id": "op-zhou"})
    ev("CLEANING_RECORDED", plan2, "2026-09-20T12:35:00+08:00",
       "补录：目视检查（实际 12:35 完成，13:30 才补录进系统）",
       {"step_id": "inspect", "operator_id": "op-zhou"},
       recorded_at="2026-09-20T13:30:00+08:00")
    ev("VERIFICATION_RECEIVED", plan2, "2026-09-20T12:50:00+08:00", "检测回传：ATP 80 RLU",
       {"kind": "atp", "value": 80, "unit": "RLU",
        "performed_at": "2026-09-20T12:45:00+08:00", "lab_id": "lab-01"})
    ev("FIRST_PIECE_CONFIRMED", plan2, "2026-09-20T12:55:00+08:00", "首件确认合格",
       {"inspector_id": "qc-zhao"})
    ev("PERSONNEL_HANDOVER_RECORDED", plan2, "2026-09-20T12:56:00+08:00", "人员交接记录",
       {"outgoing_id": "op-zhou", "incoming_id": "op-wu", "shift": "早班→中班"})
    ev("RELEASE_SIGNED", plan2, "2026-09-20T13:35:00+08:00", "质量经理尝试放行签署（存在补填记录）",
       {"signer_id": "qm-chen", "decision": "release"})

    print("\n── L1 班组看板 ──")
    print_board(service, "L1", parse_ts("2026-09-20T13:40:00+08:00"))

    print("\n═══ 质量追溯：从成品炉次还原证据链 ═══")
    batches = load_batches(DATA_DIR / "batches.jsonl")
    print_trace(trace_batch("L3-20260920-02", service.states, batches))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="changeover", description="节令食品换线放行")
    parser.add_argument("--store", default=str(DEFAULT_STORE), help="事件存储文件（JSONL）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo", help="运行演示场景（使用独立演示存储，不影响正式数据）")
    board = sub.add_parser("board", help="查看产线换线看板")
    board.add_argument("--line", default=None, help="产线编号，缺省显示全部")
    board.add_argument("--at", default=None, help="观察时刻（ISO），缺省为当前时间")
    trace = sub.add_parser("trace", help="从成品炉次追溯换线证据链")
    trace.add_argument("--batch", required=True, help="炉次号")
    args = parser.parse_args(argv)

    if args.command == "demo":
        store_path = DATA_DIR / "demo-events.jsonl"
        Path(store_path).unlink(missing_ok=True)
        service = ChangeoverService(EventStore(store_path), MasterData())
        run_demo(service)
        print(f"\n演示事件已写入 {store_path}")
        return 0

    service = ChangeoverService(EventStore(args.store), MasterData())
    if args.command == "board":
        at = parse_ts(args.at) if args.at else datetime.now(timezone.utc)
        print_board(service, args.line, at)
        return 0
    if args.command == "trace":
        batches = load_batches(DATA_DIR / "batches.jsonl")
        try:
            print_trace(trace_batch(args.batch, service.states, batches))
        except ValueError as exc:
            print(f"追溯失败：{exc}")
            return 1
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
