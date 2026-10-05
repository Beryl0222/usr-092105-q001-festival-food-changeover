"""校验领域事件信封的公共字段。"""

from datetime import datetime

REQUIRED = ("event_id", "event_type", "aggregate_type", "aggregate_id", "occurred_at", "version", "summary")

EVENT_TYPES = (
    "CHANGEOVER_REQUESTED",
    "CLEANING_RECORDED",
    "TOOL_BORROWED",
    "TOOL_RETURNED",
    "PERSONNEL_HANDOVER_RECORDED",
    "VERIFICATION_RECEIVED",
    "FIRST_PIECE_CONFIRMED",
    "RELEASE_SIGNED",
    "PRODUCTION_RESUMED",
)

AGGREGATE_TYPES = ("changeover_plan", "cleaning_execution", "verification_result", "release_decision")


def validate_event(record: dict) -> list[str]:
    """返回可以直接展示给接入方的中文错误。"""
    errors = [f"缺少字段：{name}" for name in REQUIRED if name not in record]
    if "version" in record and (not isinstance(record["version"], int) or record["version"] < 1):
        errors.append("version 必须是正整数")
    if "event_type" in record and record["event_type"] not in EVENT_TYPES:
        errors.append(f"未知事件类型：{record['event_type']}")
    if "aggregate_type" in record and record["aggregate_type"] not in AGGREGATE_TYPES:
        errors.append(f"未知聚合类型：{record['aggregate_type']}")
    for field_name in ("occurred_at", "recorded_at"):
        if field_name in record:
            try:
                ts = datetime.fromisoformat(record[field_name])
                if ts.tzinfo is None:
                    errors.append(f"{field_name} 必须带时区")
            except (TypeError, ValueError):
                errors.append(f"{field_name} 不是合法的 ISO 时间：{record[field_name]}")
    return errors
