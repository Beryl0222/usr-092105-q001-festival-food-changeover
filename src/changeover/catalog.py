"""主数据目录：产品配方/过敏原、版本化规程、签署人、器具台账。

规程与阈值按 effective_at 版本化：effective_at <= 选用时点 的最新版本生效，
新版本只约束生效后的换线；计划生成时把所选版本整体快照进 PLAN_ISSUED，
此后即便主数据发布新版本，本换线仍按快照执行与判定，保证可追溯、可复核。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


def parse_dt(value: str) -> datetime:
    """解析 RFC3339 时间（必须带时区）。"""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"时间缺少时区信息：{value}")
    return dt


@dataclass(frozen=True)
class Procedure:
    code: str
    title: str
    effective_at: datetime
    raw: dict

    @property
    def allergen_limits(self) -> dict[str, int]:
        return self.raw["allergen_limits_ppm"]

    @property
    def verification_valid_hours(self) -> int:
        return self.raw["verification_valid_hours"]

    @property
    def late_report_grace_minutes(self) -> int:
        return self.raw["late_report_grace_minutes"]


class Catalog:
    def __init__(self, data: dict):
        self.data = data

    @classmethod
    def default(cls) -> "Catalog":
        path = Path(__file__).resolve().parents[2] / "data" / "master_data.json"
        return cls.from_file(path)

    @classmethod
    def from_file(cls, path: str | Path) -> "Catalog":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    @property
    def line_code(self) -> str:
        return self.data["line"]["code"]

    def product(self, code: str) -> dict:
        try:
            return self.data["products"][code]
        except KeyError:
            raise KeyError(f"主数据中不存在产品：{code}") from None

    def allergen_name(self, code: str) -> str:
        return self.data["allergens"][code]["name"]

    def equipment(self, code: str) -> dict | None:
        for item in self.data["equipment"]:
            if item["code"] == code:
                return item
        return None

    def line_scope(self, line: str) -> list[str]:
        return list(self.data["line_equipment_scope"].get(line, []))

    def signer(self, employee_id: str) -> dict | None:
        for person in self.data["signers"]:
            if person["employee_id"] == employee_id:
                return person
        return None

    def has_role(self, employee_id: str, role: str) -> bool:
        person = self.signer(employee_id)
        return bool(person and role in person["roles"])

    def procedure_effective_at(self, at: datetime) -> Procedure:
        """选取 at 时点已生效的最新规程版本。"""
        candidates = [
            p for p in self.data["procedure_versions"] if parse_dt(p["effective_at"]) <= at
        ]
        if not candidates:
            raise ValueError(f"{at.isoformat()} 之前没有任何已生效规程版本")
        chosen = max(candidates, key=lambda p: parse_dt(p["effective_at"]))
        return Procedure(
            code=chosen["code"],
            title=chosen["title"],
            effective_at=parse_dt(chosen["effective_at"]),
            raw=chosen,
        )

    def risk_level(self, removed_allergens: set[str]) -> str:
        """根据被清掉的过敏原给出清洁强度等级。"""
        high = {"nut", "peanut"}
        if removed_allergens & high:
            return "wet_full"  # 完整拆洗+冲洗（湿法）
        if removed_allergens:
            return "wet_part"  # 含蛋乳等：湿法拆洗，测点可减
        return "sanitation"  # 同族同过敏原：常规卫生清洁
