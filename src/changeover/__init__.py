"""节令食品换线放行领域。

四类聚合沿用公共信封语义：
- changeover_plan    换线计划：排产前依据前后产品配方/过敏原差异生成要求，并快照生效规程
- cleaning_execution 清洁执行：拆洗冲洗步骤、跨线器具借用归还、人员交接
- verification_result 检测结果：涂抹/冲洗水/ATP，按快照阈值判定、按有效期失效
- release_decision   放行决定：有权人员签署、首件确认、恢复生产、质量阻断
"""

from .app import WorkshopApp
from .catalog import Catalog
from .store import EventStore, RejectedEvent

__all__ = ["WorkshopApp", "Catalog", "EventStore", "RejectedEvent"]
