# M8 Token 统计页

> 状态：已实现（2026-09-04）
> 优先级：交付
> 来源：功能评估需求（token 统计 + 多维度筛选）
> 依赖：M6（SQLite 存储层与 logs 落库数据）+ M7（GUI 日志页：/v1/logs 消费、筛选组件与图表/前端基建）；M8 需在 M6、M7 之后
> 定稿日期：2026-09-04

## 1. 背景与目标

M6 之后 logs 已含各 token（prompt/completion/cached/reasoning/total）与请求时间。用户希望有一个**独立统计页**，按模型/时间等多维度聚合 token，辅助核账与用量观察，筛选能力与日志页对齐。

### 目标

1. `/v1/stats` 支持按 模型 / 日 / 小时 / 类型 分组聚合 token 与请求数。
2. 提供与 `/v1/logs` 一致的时间区间筛选。
3. 新增独立「统计」导航页，可视化：折线（按天 token/请求走势）+ 柱状（按模型 token）。
4. 可切换维度并展示总计。

### 非目标

- 不重复实现数据落库（复用 M6）。

- 不做消息正文级统计（仅 token 元信息）。

- 不做导出报表 / 定时推送（如需后续单列）。

## 2. 设计方案

### 2.1 聚合查询（`db.py` 增补 / `routes.py` 新增）

`GET /v1/stats` 参数：

- `group_by`: `model` | `day` | `hour` | `kind`，必填。

- `time_from` / `time_to`: UTC ISO，可选；`kind` / `model` 可选过滤。

- 返回：

  ```
  { rows: [ { key, requests, success, failed,
              prompt_tokens, completion_tokens, cached_tokens, reasoning_tokens, total_tokens } ],
    total: { requests, success, failed, ...同 token 汇总 } }
  ```

SQL 用 `GROUP BY` 按维度聚合；`day` → `strftime('%Y-%m-%d', ts)`，`hour` → `%Y-%m-%d %H:00`。
请求数 = `count(*)`；`kind='chat_done'` 计成功、`kind='chat_error'` 计失败（在 WHERE 中聚合计 split）。

**被否决的备选**

- 小时级预聚合表：M8 首版直接对 logs `GROUP BY` 即可，量不大无需预聚合；若未来量大再补（记为扩展点）。

### 2.2 前端「统计」页 `frontend/src/views/Stats.vue`（新建）

- 图表：优先复用 M7 日志页已引入的图表方案（若尚未引入，ECharts 需在 M7 评估引入，M8 直接复用）。

- 顶部筛选栏：时间区间（日期选择）+ 模型 + 类型下拉（复用 M7 抽取的共享 `StatsFilter`/下拉数据源 `/v1/logs/models|kinds`）。

- 维度切换按钮：按 模型 / 日 / 小时。

- 指标卡：请求数 / 成功 / 失败 / total\_tokens 合计。

### 2.3 导航接入

- `Shell.vue` 左侧导航新增「统计」项（M7 已加「日志」历史入口，本节在其后追加）。

- `api.ts` 增加 `fetchStats`, `fetchModels`(复用), `fetchKinds`。

## 3. 已知边界

- 只统计已落库日志（M6 底座之后启动产生的）；之前内存期数据不追溯。

- 时间按**本机时区**聚合/展示（`strftime('%Y-%m-%d[ %H:00]', datetime(ts,'localtime'))` 把 UTC `ts` 换算本地），与 M7「日志」页本地显示口径一致。

- token 值为上游返回口径（LLM 侧计数），模型差异不做归一。

- 图表库：优先复用仓库已有的；若前端未装 ECharts，需在 M7 引入（评估体积），M8 不再重复引入。

- **聚合依赖独立列**：M8 的 `GROUP BY` 只针对 M6 `logs` 的 `kind/model/ts/tokens` 等**独立列**。未来出现新统计维度时，该字段必须先按 M6 扩展策略以**可空独立列**落库（启动 `ALTER TABLE ADD COLUMN` 自动补列），可参与聚合后再接入统计——`detail` JSON 不参与聚合。

## 4. 验收（实现前必填）

1. `/v1/stats` 按 model/day/hour/kind 分别聚合，数字与 logs 明细一致（抽样核对）。
2. 时间区间筛选与日志页（M7）一致；total 汇总正确。
3. 前端统计页可切换维度、筛选后图表刷新。
4. 按 day 折线、按 model 柱状正确渲染；切空数据不报错（空态）。
5. 打包 sidecar + frontend build 通过。
6. 前端类型检查（vue-tsc）通过。

## 5. 支撑脚本

- `tests/test_m8_stats.py`：聚合 SQL 正确性单测（构造已知数据断言）。

- 可选 `scripts/m8_smoke.py`：无真实登录态下造 logs → 校验 stats 查询。

