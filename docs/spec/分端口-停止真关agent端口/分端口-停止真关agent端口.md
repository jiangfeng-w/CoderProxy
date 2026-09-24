# 停止服务=真关 agent 端口（分端口：base 内部口 + agent 对外口）

> 状态：规划中
> 优先级：交付（待拍板是否立项）
> 来源：M9「服务开关」的非目标——「停 base-url 是逻辑 503，端口仍监听；要真正关端口需把底座与 base-url 拆开」。用户询问能否「真正关闭端口」，评估后立为独立需求（**路线 B：单进程双端口**，非拆两个进程）
> 依赖：M9 服务开关（`/v1/service` 门控与 `_serving()` 已具备）；M4 壳 / M5 打包；M6 数据底座（进程内 storage 单例，单进程共享 = 本方案省事的根基）
> 定稿日期：—

## 1. 背景与目标

M9 实现了「对 agent 的 /v1 服务开关（软停，不动底座）」：未登录 / 手动停止时，`/v1/chat/completions`、`/v1/models` 返回 503，但进程常驻、**端口仍监听**。本需求把「停止服务」改为**真正释放 agent 端口**（外部连接直接 refused），同时底座接口在**内部端口**照常响应。

### 目标

| #  | 功能              | 说明                                                                                         |
| -- | --------------- | ------------------------------------------------------------------------------------------ |
| F1 | 单进程双端口          | 同一 relay 进程同时监听 `agent 对外口`（默认 3601，即 agent 的 base\_url）与 `base 内部口`（默认 3602，GUI/底座用）      |
| F2 | 停止 = 真关 agent 口 | `/v1/service/disable` → 关闭并释放 agent uvicorn server（socket 真释放）；`/v1/service/enable` → 重新拉起 |
| F3 | 底座不受影响          | 停止期间，base 内部口仍响应 `/v1/auth\|logs\|stats\|monitor\|service` 与 `/v1/models?all=1`            |
| F4 | 路由分流            | 按 path 把请求分流到对应端口（Rust `relay` 命令）                                                         |
| F5 | 状态一致            | 顶栏「运行中 / 已停止」反映 agent 口是否在监听                                                               |

### 非目标

- ❌ 不拆成两个进程（避免跨进程状态同步，见 §5 路线 A 对比）。

- ❌ 不改 agent 端口绑定 / OpenAI 协议之外的任何行为。

## 2. 设计方案（路线 B：单进程双端口）

### 2.1 relay：拆 `base_app` / `agent_app`

把 [routes.py](../../../relay/relay/routes.py) 的单一 `app` 拆成两个 FastAPI 实例（或两个 `APIRouter` 挂到一个共享依赖上）：

- **`base_app`（内部口）**：`/v1/auth/*`、`/v1/logs`、`/v1/stats`、`/v1/monitor/*`、`/v1/service`（开关）、`/v1/models?all=1`（GUI 管理）。

- **`agent_app`（对外口）**：`/v1/chat/completions`、`/v1/models`（默认，agent 发现）、`/v1/models/{id}`。

`_serving()` / `_service_disabled` 为模块级单例，单进程内天然共享（这是本方案比「拆两进程」省事的关键）。

### 2.2 双端口启动 + 动态启停 agent 端口（核心难点）

- [cli.py](../../../relay/relay/cli.py)：asyncio 同时 `serve()` 两个 uvicorn Server：base 常驻；agent 由开关停/启。

- `/v1/service/disable`：除置 `_service_disabled` 外，**shutdown agent server**（释放 3601 socket）。

- `/v1/service/enable`：重新 `serve()` agent server。

- 需处理：优雅关闭（含 in-flight SSE）、端口释放后的重绑竞态（TIME\_WAIT / `SO_REUSEADDR`）、事件循环内 server 生命周期。

### 2.3 配置与就绪上报

- [config.py](../../../relay/relay/config.py) / storage 新增 `base_port`（建议默认 `relay_port + 1`，或独立持久化字段）；两端口分别做占用预检。

- `[relay-ready]` 行带 `port`（agent 口）与 `base_port`（内部口），供壳解析。

### 2.4 Rust（[lib.rs](../../../src-tauri/src/lib.rs)）

- `ReadyInfo` 增加 `base_port`；`relay_status` 反馈 agent/base 两个口的监听状态。

- `relay` 命令按 path 分流：

  - `/v1/chat/completions`、`/v1/models`(默认)、`/v1/models/{id}` → **agent 口**；

  - `/v1/models?all=1`、`/v1/auth|logs|stats|monitor|service` → **base 口**。

### 2.5 前端

- 基本不用改：前端仍通过 Rust `relay` 调 `/v1/*`，由 Rust 分流；`/v1/service` 走 base 口。仅确认配置页「Agent 连接地址」（base\_url）仍指向 agent 口（默认 3601）。

## 3. 已知边界 / 风险

- **动态启停 agent server** 是本需求最大风险（端口生命周期、SSE 中断、重绑竞态）。

- 单测较难覆盖优雅关闭与端口释放，需补**集成冒烟脚本**（起服 → disable → 探 3601 失败/探 3602 成功 → enable → 恢复）。

- 涉及 sidecar 重新打包（PyInstaller 会包含新路由），须重建 sidecar + 重新 `tauri build`。

## 4. 验收

1. 启动后：agent 口（3601）与 base 口（3602）均在监听。
2. `POST /v1/service/disable`：agent 口**真关闭**（外部连接 refused / 端口释放），base 口仍响应全部底座接口（200）。
3. `POST /v1/service/enable`：agent 口恢复监听。
4. Rust `relay` 命令按 path 正确分流到对应端口。
5. 前端 build / vue-tsc 通过；relay 单测 + 分端口冒烟脚本通过。

## 5. 备选方案对比（路线 A / C）

| 路线         | 思路               | 优点            | 代价                                            |
| ---------- | ---------------- | ------------- | --------------------------------------------- |
| **B（本需求）** | 单进程双端口           | 状态无跨进程同步；改动集中 | 动态启停 agent server 较难；双 uvicorn 生命周期           |
| A          | 拆两个进程各一端口        | 端口/职责物理隔离     | 跨进程状态同步（storage 锁是进程内）；Rust 托管两个 sidecar；测试大改 |
| C          | relay 不变 + 薄代理进程 | 改动最小          | 多一个常驻进程；转发开销；架构更绕                             |

> 评估结论：B 在「真关端口 + 保底座 + 免跨进程同步」三者间性价比最高，但仍需专门排期。

## 6. 支撑

- 本需求评估稿（当前文档）；实现时补充：`scripts/` 分端口冒烟脚本（起服 → disable 验证端口释放 → enable 恢复）。

