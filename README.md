# 节令食品换线放行

屯昌联合工坊节令食品车间（月饼产线）的换线放行领域模型。目标是回答质量经理最担心的问题：
**抽检发现坚果残留时，能不能说清污染从哪次清洁开始、哪些炉次应隔离。**

系统采用事件溯源：所有事实都是只追加的领域事件，状态可完整回放；任一成品炉次都能还原
当时采用的规程版本、阈值、检测结果、签署人与恢复决定。

## 业务规则

**排产（changeover_plan）**
- 依据前后产品配方/过敏原差异生成要求：前产品有、后产品不声明的过敏原为“关键清退项”；
  含坚果/花生走完整拆洗+冲洗湿法清洁，含蛋乳走湿法减点，过敏原声明一致走常规卫生清洁。
- 生成清洁步骤（逐序扫码）、检测要求（过敏原逐点涂抹、末次冲洗水、ATP）、首件确认清单、
  本线器具范围。
- `PLAN_ISSUED` 整体快照当时生效的规程与阈值。**规程/阈值新版本只约束生效后的换线**，
  已下发换线始终按快照判定。

**执行（cleaning_execution）**
- 步骤未扫全不能记录清洁完成；清洁完成后不能补扫，返工必须先由质量发起阻断、重新开工。
- 跨线模具/周转筐必须先登记借用、上线扫码清洁，**放行签署前必须归还出线**；
  无借用登记的跨线器具不得上线，借而未还直接阻断。
- 人员交接记录交班人、接班人与当时剩余步骤清单。
- **重复扫码沿用首次确认；同一事件事实重传（相同 event_id）命中幂等；
  离线重传不会制造第二次清洁。**
- 事后补填：`received_at` 晚于 `occurred_at` 超过规程宽限（30 分钟）且无已登记离线
  作业时段的，一律拒收；先登记离线时段，方可在时段窗内按真实发生时间重放。

**检测（verification_result）**
- 过敏原涂抹逐点判定峰值与限值，冲洗水/ATP 各按快照限值判定。
- 检测有有效期（2025 版 12 小时、2026 版 8 小时），**签署时合格、拖到恢复时过期同样阻断**。
- 支持复检：保留全部尝试，判定以最新一次为准；质量阻断后的旧检测随之失效，必须复检。

**放行（release_decision）**
- 清洁、器具归还、检测全部满足后，须由具备 `release_authority` 的人员签署；
- 签署后由具备 `first_piece_authority` 的质量人员做首件确认（外观+过敏原标签核对）；
- 首件合格且全部阻断解除，有权人员方可批准恢复生产（停线结束时点）；
- 恢复后抽检异常可再次 `PRODUCTION_HELD` 并登记隔离炉次，返工后须**重新检测、重新签署、
  重新首件确认**才能再次恢复；重复签署/重复恢复无效。

## 运行

```bash
python3 demo.py                      # 五个中文场景演示
python3 -m unittest discover -s tests
```

## 代码结构

- `contracts/domain.schema.json`：事件信封、15 种事件类型与四类聚合归属。
- `data/master_data.json`：产品配方与过敏原、版本化规程阈值（COP-AH-2025 / COP-AH-2026）、
  授权签署人、器具台账。
- `data/sample.json`：中文信封样例。
- `src/validator.py`：公共信封与事件-聚合归属校验。
- `src/changeover/`：
  - `catalog.py` 主数据与规程版本时点选择；
  - `store.py` 只追加事件存储（event_id 幂等、版本递增、倒序/离线重放规则）；
  - `planning.py` 过敏原差异分析与计划/快照生成；
  - `execution.py` 清洁/器具/交接/离线事件与状态重建；
  - `verification.py` 检测事件与复检状态；
  - `release.py` 签署、首件、恢复、阻断事件；
  - `app.py` 命令受理、阻断评估（`evaluate`）、炉次追溯（`trace_oven_batch`）、
    停线构成（`downtime_report`）；
  - `views.py` 班组看板、停线构成、炉次追溯卡中文渲染；
  - `testkit.py` 测试与演示共用的典型流程。

## 事件约定

事件由 `event_id` 全局唯一标识；来源系统重试、离线重传必须沿用原标识（本实现按事件
事实内容确定性生成），接收方据此幂等去重。`version` 在每个聚合内从 1 递增，
`occurred_at` 为事实真实发生时间，`received_at` 为平台收到时间（可缺省），
`payload.changeover_id` 把执行、检测、放行事件回指换线计划。

| 聚合 | 事件 |
|---|---|
| changeover_plan | CHANGEOVER_REQUESTED, PLAN_ISSUED |
| cleaning_execution | CLEANING_STARTED, STEP_CONFIRMED, EQUIPMENT_SCANNED, EQUIPMENT_LOANED, EQUIPMENT_RETURNED, HANDOVER_RECORDED, OFFLINE_SESSION_OPENED, CLEANING_RECORDED |
| verification_result | VERIFICATION_RECEIVED |
| release_decision | RELEASE_SIGNED, FIRST_PIECE_CONFIRMED, PRODUCTION_RESUMED, PRODUCTION_HELD |
