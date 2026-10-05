"""主数据：产品配方/过敏原、换线规程版本、有权签署人。

主数据存放于 data/ 下的 JSON 文件，服务启动时加载。
规程按版本管理，只有 effective_from 之后发起的换线才使用新版本。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def parse_ts(value: str) -> datetime:
    """解析 ISO 时间戳；naive 输入视为错误，避免跨时区歧义。"""
    ts = datetime.fromisoformat(value)
    if ts.tzinfo is None:
        raise ValueError(f"时间必须带时区：{value}")
    return ts


@dataclass(frozen=True)
class Product:
    product_id: str
    name: str
    allergens: tuple[str, ...]
    recipe_note: str = ""


@dataclass(frozen=True)
class ProcedureVersion:
    procedure_id: str
    title: str
    version: str
    effective_from: datetime
    backfill_tolerance_minutes: int
    risk_levels: dict


@dataclass(frozen=True)
class Signer:
    signer_id: str
    name: str
    role: str
    authorized_lines: tuple[str, ...]
    effective_from: datetime


class MasterData:
    def __init__(self, data_dir: Path = DATA_DIR):
        self.data_dir = Path(data_dir)
        products = json.loads((self.data_dir / "products.json").read_text(encoding="utf-8"))
        self.products: dict[str, Product] = {
            p["product_id"]: Product(
                product_id=p["product_id"],
                name=p["name"],
                allergens=tuple(p.get("allergens", [])),
                recipe_note=p.get("recipe_note", ""),
            )
            for p in products
        }
        proc = json.loads((self.data_dir / "procedures.json").read_text(encoding="utf-8"))
        self.procedure_id = proc["procedure_id"]
        self.procedure_title = proc["title"]
        self.procedure_versions: list[ProcedureVersion] = sorted(
            (
                ProcedureVersion(
                    procedure_id=proc["procedure_id"],
                    title=proc["title"],
                    version=v["version"],
                    effective_from=parse_ts(v["effective_from"]),
                    backfill_tolerance_minutes=v["backfill_tolerance_minutes"],
                    risk_levels=v["risk_levels"],
                )
                for v in proc["versions"]
            ),
            key=lambda v: v.effective_from,
        )
        signers = json.loads((self.data_dir / "signers.json").read_text(encoding="utf-8"))
        self.signers: dict[str, Signer] = {
            s["signer_id"]: Signer(
                signer_id=s["signer_id"],
                name=s["name"],
                role=s["role"],
                authorized_lines=tuple(s.get("authorized_lines", [])),
                effective_from=parse_ts(s["effective_from"]),
            )
            for s in signers
        }

    def procedure_at(self, moment: datetime) -> ProcedureVersion:
        """返回指定时刻生效的规程版本；新版本不追溯影响已生成的计划。"""
        effective = [v for v in self.procedure_versions if v.effective_from <= moment]
        if not effective:
            raise ValueError(f"{moment.isoformat()} 之前没有已生效的规程版本")
        return effective[-1]

    def signer_authorized(self, signer_id: str, line_id: str, moment: datetime) -> str | None:
        """返回 None 表示有权，否则返回中文拒绝原因。"""
        signer = self.signers.get(signer_id)
        if signer is None:
            return f"签署人 {signer_id} 不在有权人员名单中"
        if signer.effective_from > moment:
            return f"签署人 {signer.name} 的授权尚未生效"
        if line_id not in signer.authorized_lines:
            return f"签署人 {signer.name}（{signer.role}）无产线 {line_id} 的放行权限"
        return None
