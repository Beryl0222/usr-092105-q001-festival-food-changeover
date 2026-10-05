"""校验领域事件信封的公共字段。

事件类型与聚合归属是公共约定的一部分：
changeover_plan    <- CHANGEOVER_REQUESTED / PLAN_ISSUED
cleaning_execution <- CLEANING_STARTED / EQUIPMENT_SCANNED / EQUIPMENT_LOANED
                      / EQUIPMENT_RETURNED / HANDOVER_RECORDED / CLEANING_RECORDED
verification_result<- VERIFICATION_RECEIVED
release_decision   <- RELEASE_SIGNED / FIRST_PIECE_CONFIRMED
                      / PRODUCTION_RESUMED / PRODUCTION_HELD
"""

REQUIRED = ("event_id", "event_type", "aggregate_id", "occurred_at", "version", "summary")

EVENT_TYPES = (
    "CHANGEOVER_REQUESTED",
    "PLAN_ISSUED",
    "CLEANING_STARTED",
    "STEP_CONFIRMED",
    "EQUIPMENT_SCANNED",
    "EQUIPMENT_LOANED",
    "EQUIPMENT_RETURNED",
    "HANDOVER_RECORDED",
    "OFFLINE_SESSION_OPENED",
    "CLEANING_RECORDED",
    "VERIFICATION_RECEIVED",
    "RELEASE_SIGNED",
    "FIRST_PIECE_CONFIRMED",
    "PRODUCTION_RESUMED",
    "PRODUCTION_HELD",
)

AGGREGATE_TYPES = (
    "changeover_plan",
    "cleaning_execution",
    "verification_result",
    "release_decision",
)

EVENT_AGGREGATE = {
    "CHANGEOVER_REQUESTED": "changeover_plan",
    "PLAN_ISSUED": "changeover_plan",
    "CLEANING_STARTED": "cleaning_execution",
    "STEP_CONFIRMED": "cleaning_execution",
    "EQUIPMENT_SCANNED": "cleaning_execution",
    "EQUIPMENT_LOANED": "cleaning_execution",
    "EQUIPMENT_RETURNED": "cleaning_execution",
    "HANDOVER_RECORDED": "cleaning_execution",
    "OFFLINE_SESSION_OPENED": "cleaning_execution",
    "CLEANING_RECORDED": "cleaning_execution",
    "VERIFICATION_RECEIVED": "verification_result",
    "RELEASE_SIGNED": "release_decision",
    "FIRST_PIECE_CONFIRMED": "release_decision",
    "PRODUCTION_RESUMED": "release_decision",
    "PRODUCTION_HELD": "release_decision",
}


def validate_event(record: dict) -> list[str]:
    """返回可以直接展示给接入方的中文错误。"""
    errors = [f"缺少字段：{name}" for name in REQUIRED if name not in record]
    if "version" in record and (not isinstance(record["version"], int) or record["version"] < 1):
        errors.append("version 必须是正整数")
    event_type = record.get("event_type")
    if event_type is not None and event_type not in EVENT_TYPES:
        errors.append(f"未知事件类型：{event_type}")
    aggregate_type = record.get("aggregate_type")
    if aggregate_type is not None and aggregate_type not in AGGREGATE_TYPES:
        errors.append(f"未知聚合类型：{aggregate_type}")
    if event_type in EVENT_AGGREGATE and aggregate_type is not None:
        expected = EVENT_AGGREGATE[event_type]
        if aggregate_type != expected:
            errors.append(f"事件 {event_type} 必须归属聚合 {expected}，收到 {aggregate_type}")
    return errors
