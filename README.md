# 节令食品换线放行

面向节令食品（如中秋月饼）排产的换线放行领域服务。排产前依据前后产品的配方与过敏原差异生成拆洗、冲洗、检测与首件确认要求；执行中管控跨线借用的模具、周转筐与人员交接；器具未归还、检测过期、记录事后补填时不得恢复生产；放行保留有权人员签署，规程新版本只约束生效后的换线。

## 领域资料

- `contracts/domain.schema.json`：事件信封与本领域允许的聚合、事件类型。
- `data/products.json`：产品配方与过敏原。
- `data/procedures.json`：换线清洁与放行规程（按版本管理，`effective_from` 之后的新计划才用新版本）。
- `data/signers.json`：有权放行签署人及其产线权限。
- `data/batches.jsonl`：成品炉次记录（生产执行侧数据，用于追溯）。
- `data/sample.json`：一条可用于本地联调的中文样例。

## 代码结构

- `src/validator.py`：事件信封公共字段校验。
- `src/masterdata.py`：主数据加载；按时刻解析生效规程版本与签署权限。
- `src/planning.py`：过敏原差异 → 风险等级 → 拆洗/冲洗/检测/首件要求快照。
- `src/store.py`：JSONL 事件存储，按 `event_id` 幂等去重（离线重传不产生第二条记录）。
- `src/service.py`：规则引擎。重复扫码拒绝入账；放行前检查清洁缺项、检测缺失/过期、器具未归还、交接未记录、事后补填；签署人权限校验。
- `src/projections.py`：从事件流还原换线状态、阻断原因与停线构成。
- `src/trace.py`：从任一成品炉次还原当时采用的规程版本、检测与放行决定。
- `src/cli.py`：命令行入口。

事件由 `event_id` 唯一标识，`aggregate_id` 以换线计划号贯穿 `changeover_plan`、`cleaning_execution`、`verification_result`、`release_decision` 四个聚合，`version` 从 1 开始递增，`occurred_at` 保留真实发生时间，`recorded_at` 为记录入系统时间（超过规程容忍度即判定补填）。来源系统重试时必须沿用原事件标识。

## 本地运行

```bash
python3 -m unittest discover -s tests          # 全部规则测试
python3 -m src.cli demo                        # 中秋换线演示（阻断→解除→放行→追溯）
python3 -m src.cli board --line L3             # 班组看板：状态、阻断原因、停线构成
python3 -m src.cli trace --batch L3-20260920-02  # 炉次追溯
```
