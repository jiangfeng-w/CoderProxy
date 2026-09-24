# M7 GUI 日志页（历史浏览）

> 状态：已实现（2026-09-04，全文按落地口径回填）
> 优先级：交付
> 来源：M6 拆分（2026-09-04 用户拍板）：GUI 日志历史浏览从 M6 拆出，排在 Token 统计页（M8）之前
> 依赖：M6 数据底座（SQLite 落库 + `/v1/logs`、`/v1/logs/kinds`、`/v1/logs/models`、`DELETE /v1/logs`）
> 定稿日期：2026-09-04

## 1. 背景与目标

M6 之后日志已持久化到 SQLite 并提供 `/v1/logs` 分页 / 筛选 API，但 GUI [Logs.vue](../../../frontend/src/views/Logs.vue) 仍只消费 monitor 内存环形缓冲（实时增量拉取、500 条上限、重启即丢）。用户希望 GUI 能**浏览持久化历史**，能力与 `/v1/logs` 对齐。

### 目标

1. 日志页可浏览**跨重启保留**的历史记录（数据源 = `/v1/logs`）。
2. 保留**近实时**体验：新请求能在数秒内出现在列表顶部（轮询最新页）。
3. 支持分页 + 多条件筛选：类型 / 模型 / 时间区间，可组合。
4. 提供清空入口：全清与按条件清空（`DELETE /v1/logs`）。
5. chat_done 行展示 token 与耗时（M6 落库列）。

### 非目标

- 不做 token / 请求的**聚合统计**（图表、分组、总计）——归 M8 Token 统计页。
- 不改 monitor 后端（ring buffer 与 `/v1/monitor/*` 保留，供 CLI / 调试）。
- 不落消息正文（M6 边界延续）。

## 2. 设计方案

### 2.1 视图形态：历史为唯一日志视图 + 近实时轮询

改造现有 [Logs.vue](../../../frontend/src/views/Logs.vue)，**不再拉取 `/v1/monitor/events`**：

- 数据源：`GET /v1/logs`（SQLite），默认 `ts` 倒序。
- 近实时：每 2.5s 自动刷新第一页；新请求会出现在顶部（「暂停 / 继续」按钮保留，暂停时停轮询）。
- 筛选栏：类型（`/v1/logs/kinds`，白名单外 kind 不在 UI 展示但仍可被 API 查）、模型（`/v1/logs/models`，空 = 全部）、时间区间（本地 datetime 输入 → 转 UTC ISO 提交）。
- 分页：上一页 / 下一页 + 每页 20 / 50 / 100；展示 `total`。
- 清空：按钮 → 二次确认 → `DELETE /v1/logs`（带当前条件 = 条件清空，无条件 = 全清）→ 刷新。
- 汇总卡：由「本页范围 / 总条数」取代原 per-kind 计数（monitor.stats 重启即清零且 tool 计数不落库，不再作为 UI 依据；per-kind / 模型 / 时间统计沉淀到 M8 统计页）。
- 行详情：沿用现有 kind 标签配色；chat_done 行补充 `total_tokens` 与 `duration_ms`；chat_error 行展示 error 摘要。

**被否决的备选**

- **「实时 + 历史」双页签**：实时页签继续 monitor 事件流、历史页签走 `/v1/logs`。否决理由：两套数据源展示同类日志语义重叠（为何实时里没有昨天的记录？），双数据源双清空入口，维护与理解成本高。
- **保留 monitor 轮询 + 顶部追加历史页**：本质同上，否决。

### 2.2 API 与壳侧小改

- `api.ts`：`fetchLogs(params)`、`fetchLogKinds()`、`fetchLogModels()`、`deleteLogs(params)`；`relay()` 的 method 类型扩为 `GET | POST | DELETE`。
- **Rust 壳放行 DELETE（1 行小改）**：现状 [lib.rs relay 代理](../../../src-tauri/src/lib.rs) 仅放行 GET/POST，前端无法调 `DELETE /v1/logs`。在 method match 增加 `"DELETE"` 分支（仍限 `/v1/*`）。
  - 备选（否决）：后端另加 `POST /v1/logs/clear` 别名过壳——多一个冗余端点，且 DELETE 语义更标准；M6 已定义 DELETE，壳放行更直接。

### 2.3 文件改动清单

- `frontend/src/views/Logs.vue`：重写数据源与筛选 / 分页 / 清空交互（保留行样式体系）。
- `frontend/src/api.ts`：新增上述函数 + method 类型扩展。
- `src-tauri/src/lib.rs`：`relay` 代理 method 增加 `"DELETE"`。
- （M8 复用点）若筛选栏逻辑足够独立，抽 `frontend/src/components/StatsFilter.vue` 或等效组件，供 M8 统计页复用；量小则 M8 再抽。

## 3. 已知边界

- 时间一律 UTC 存储（M6），前端展示转本地时区；datetime 选择器输出本地 → 提交前转 UTC。
- 近实时 = 轮询间隔延迟（2.5s），非 SSE 推送。
- monitor ring buffer 与 SQLite 双写并存（M6），本页只呈现 SQLite；`/v1/monitor/*` 仍可被 CLI / 调试使用。
- 日志无正文内容（M6 不落正文）。
- 轮询间隔内多次请求 / 大结果集：分页查询走 `ts` 索引，上限 10 万行由 M6 prune 兜底。

## 4. 验收（实现前必填）

1. 重启 relay 后日志页仍能浏览跨重启的历史（来自 SQLite）。
2. 新请求 ≤ 3s 出现在列表顶部（近实时）；「暂停」后不再刷新。
3. 类型 / 模型 / 时间区间筛选各自正确且可组合；分页与 `total` 正确。
4. 类型 / 模型下拉数据来自 `/v1/logs/kinds|models`。
5. chat_done 行展示 token 总数与耗时；chat_error 行展示错误摘要。
6. 清空（含条件清空）有二次确认，生效后列表刷新。
7. `DELETE /v1/logs` 经壳可用（Rust 放行后手动 / 集成验证）。
8. 空态 / 加载态 / 错误态正确；前端类型检查（vue-tsc）通过；frontend build 通过。

## 5. 支撑脚本

- 可选 `scripts/m7_logs_mock.py`：无真实登录态下直接向 db 造一批 logs（可指定模型 / 时间跨度），供前端联调筛选 / 分页 / 近实时。
- 前端交互手测为主（打包态 / dev 态各跑一遍）。

## 6. 实现记录（2026-09-04 已实现）

落地文件与改动：

- `frontend/src/views/Logs.vue`（重写）：数据源切 `/v1/logs`（不再拉 `/v1/monitor/events`）。2.5s 轮询当前页（暂停停轮询）；类型（4 种已知 kind）/ 模型（`/v1/logs/models`）/ 时间区间（本地 datetime-local → UTC `+00:00` 口径提交）筛选可组合；分页 20/50/100 + `total` + 页码越界回退；清空二次确认（NModal）按当前条件（无条件 = 全清）；chat_done 行显示 `共 X tokens · 耗时 Y`，悬浮 title 显示完整 usage；chat_error 显示错误摘要；汇总改为「本页 N 条 · 共 M 条」；空态 / 加载态 / 错误横幅。
- `frontend/src/api.ts`：`relay()` method 扩为 `GET | POST | DELETE`；新增 `LogRow`/`LogsResult`/`LogsQuery` 类型与 `fetchLogs`/`fetchLogKinds`/`fetchLogModels`/`deleteLogs`（monitor 相关函数保留，后端 ring buffer 未动）。
- `src-tauri/src/lib.rs`：`relay` 代理 method match 增加 `"DELETE"` 分支（仍限 `/v1/*`），DELETE 供前端经壳调 `/v1/logs` 清空。
- `relay/m7_logs_mock.py`（新建，可选）：向指定 data 目录批量造演示 logs（模型 / 时间跨度可调），联调后可在 GUI 一键清空。

验证情况：

- `npm run build`（vue-tsc + vite）通过；`cargo check` 通过。
- 端到端（真实 relay + mock 数据）：`kind+limit` 分页、时间区间（`+00:00` 口径）、`DELETE ?kind=chat_error`（删除行数正确）、kinds/models 下拉数据源均验证通过。
- GUI 交互（近实时轮询、筛选组合、清空确认、空/加载/错误态）以打包态手测为准（§5）。
