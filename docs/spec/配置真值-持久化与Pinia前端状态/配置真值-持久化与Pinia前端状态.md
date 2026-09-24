# 配置真值：持久化 + Pinia 前端状态 + 壳去配置镜像

> 状态：已实现（2026-09-05 三层实现完成；自动化测试绿；打包后手工回归 R1–R4 通过）
> 优先级：交付
> 来源：用户报 bug「工具伪装切任何模式，顶栏恒显智能适配」→ 根因链排查 + 架构评估结论
> 依赖：M4 Tauri 壳 / M6 数据底座（`relay_state.json` 落盘）/ M9 服务开关 / 现有 `frontend/src/store.ts`
> 定稿日期：2026-09-05（三项分歧已拍板：① 持久化优先装载 ② 端口「保存即自动重启」保留现状 ③ 壳不再持有配置镜像、api_key 改由前端随请求携带）

## 1. 背景与根因

### 1.1 症状（一个根因的三个表现）

| # | 症状 | 性质 |
| -- | ---- | ---- |
| S1 | 工具伪装切到 strict / passthrough，**顶栏恒显「智能适配」**（= hybrid） | 纯显示滞后，功能已生效 |
| S2 | 配置页「重置密钥」后，GUI 经壳转发任何**需鉴权请求**（服务开关/日志/统计/再改配置）持续 401，直到重启 relay | 潜在真故障 |
| S3 | **tool_mode 未持久化**：GUI 切 strict 只改内存，重启 relay/应用后悄悄回到 hybrid | 配置丢失（比显示更严重的隐藏 bug） |

### 1.2 根因链（三层，环环相扣）

1. **壳（Rust）只在 sidecar 启动时解析一次 `[relay-ready]`**，把 `port / api_key / tool_mode` 存进 `ReadyInfo`，此后永不更新（[lib.rs spawn_with_port](../../../src-tauri/src/lib.rs) 读线程命中首行即返回；[relay_status](../../../src-tauri/src/lib.rs) 直接读该快照）。
   - 对 `running / port` 这种**进程状态**，启动快照天然合理（`port` 要重启才变，重启会重新上报）；
   - 对 `tool_mode / api_key` 这种**运行期可变配置**，快照必然过期——而 relay 端这两者恰恰都是**热改不重启**：
     - 切模式 = `POST /v1/auth/config` 直接改全局 `settings.tool_mode`（[routes.py auth_config_update](../../../relay/relay/routes.py)）；
     - 重置密钥 = `POST /v1/auth/config` 落盘新 key（[routes.py](../../../relay/relay/routes.py)），壳里还是旧 key → 后续转发带旧 Bearer → 401。
   - **结构性病灶**：`ReadyInfo` 把「进程状态」与「业务配置镜像」混在同一个启动时点快照里，前者合理、后者必过期。
2. **tool_mode 是唯一没落盘的 GUI 配置**：storage 已持久化 `api_key / port / model_whitelist`（[storage.py](../../../relay/relay/storage.py)），唯独 tool_mode 只进内存不进盘。
3. **前端 config 展示分散、无单一状态**：Config.vue 与 Overview.vue 各自 `getConfig()` 存局部变量（Overview 还 5s 轮询，[Overview.vue](../../../frontend/src/views/Overview.vue)）；顶栏却读另一个来源 `store.relay.tool_mode`（壳快照）。同一份配置三个展示位、三条数据流，天然会漂。

### 1.3 结论

功能层「模式已生效」没有任何问题；坏的是**配置的持久化、装载、前端状态管理没有统一真值**。修复 = 让「**后端磁盘 = 唯一权威源，前端 Pinia = 唯一镜像，壳 = 纯进程管理与透明转发（不再持有配置）**」。

## 2. 目标 / 非目标

### 目标

| # | 功能 | 说明 |
| -- | ---- | ---- |
| F1 | 配置全部落盘，启动装载，持久化优先 | `tool_mode` 补落盘；`api_key / port / model_whitelist` 维持；开机读盘生效（决策 D1） |
| F2 | `/v1/auth/config` = 唯一写入口，写路径顺序固定 | 校验 → **逐字段落盘** → 同步内存运行值 → 返回**完整 config** 作为成功回执 |
| F3 | 前端引入 Pinia，config 单一 store | 页面/顶栏全部读 store；写成功后用响应整体替换；删除各页重复 getConfig / 局部轮询 |
| F4 | 壳不再持有配置镜像 | `relay_status` 只报 running/port；转发鉴权 key 由前端随每次 `relay` 调用携带（决策 D3） |
| F5 | 端口保存仍自动重启 | 保留现状（决策 D2）：保存 → 落盘 → 自动 `relayRestart` → 就绪后 store 刷新 |

### 非目标

- ❌ 拆分「底座 / agent 对外口」两个进程或端口（独立需求「分端口-停止真关agent端口」，另文）。
- ❌ 配置改迁 SQLite：沿用 `relay_state.json`（日志已独立走 `coderproxy.db`，互不干扰）。
- ❌ 改动 `[relay-ready]` 行协议格式：行内仍打印三个字段，兼容 CLI 与 m5 打包脚本；壳解析侧改为只需 `port`。
- ❌ 处理「agent 直连」的鉴权：Agent 自己填 base_url + api_key，语义不变（key = 磁盘当前值）。

## 3. 已拍板决策（用户 2026-09-05 确认）

### D1 装载优先级：持久化优先

> 用户原话：「持久化优先，优先从磁盘里读取数据」。

**生效值 = 磁盘持久化值（有）→ 环境变量（磁盘无，兜底默认）→ 代码默认值。**

统一语义下，**所有「显式变更」都走落盘**（GUI POST 或 CLI flag），环境变量只充当「从未保存过」时的初始默认：

- GUI `POST /v1/auth/config` 改 tool_mode → 落盘 + 改内存；
- CLI `run --tool-mode` 改为**同样落盘**（对齐现有 `--port` 语义），不再只是内存覆盖；
- `TOOL_MODE` / `RELAY_API_KEY` 环境变量由「启动覆盖」降级为「磁盘无值时的兜底默认」（影响 headless 使用习惯，见「已知边界」）。

### D2 端口：保存即自动重启（保留现状）

> 用户原话：「按你说的来，保留现状『保存即自动重启』」。

端口保存 → `POST` 落盘成功 → 前端收到成功回执 → 自动 `relayRestart` → sidecar 就绪后 `configStore.load()` 刷新 port/base_url 展示。

### D3 壳去镜像：api_key 由前端携带（方案 A）

> 用户原话：「按你的推荐来」。

壳**不再持有任何业务配置镜像**：

- `ReadyInfo` 收敛为进程状态 `{port}`；`relay_status` 只返回 `{running, port}`；
- `relay`（GUI→壳→relay 转发）命令新增入参 `api_key`，转发时 `Authorization: Bearer <前端传入的 key>`，不再从启动快照取；
- 前端在 config store 装载/回写成功后把当前 key 同步给 api 层，每次 `invoke` 携带。

否决的备选：
- **B（壳同步镜像）**：壳拦截 `POST /v1/auth/config` 成功响应回写快照。改动小，但壳继续持有配置镜像的架构债仍在——今后每加一个配置字段都要求壳配合，同类 bug 易复发。
- **C（纯前端顶栏轮询 getConfig）**：能修 S1，但修不了 S2/S3，且不落在统一 store，属于打补丁。

## 4. 设计方案

### 4.1 relay：tool_mode 落盘与统一装载（F1）

[storage.py](../../../relay/relay/storage.py) 新增（对齐 `get_port/save_port` 模式）：

```python
DEFAULT_TOOL_MODE = "hybrid"

async def get_tool_mode() -> str:
    state = await _io(_load_state)
    m = (state.get("config") or {}).get("tool_mode")
    return m if m in ("hybrid", "strict", "passthrough") else DEFAULT_TOOL_MODE

async def save_tool_mode(mode: str) -> str:
    async with _asyncio_lock:
        state = await _io(_load_state)
        state.setdefault("config", {})["tool_mode"] = mode
        await _io(_save_state, state)
    return mode
```

统一装载函数（实现于 [storage.py](../../../relay/relay/storage.py)，`cli._cmd_run` 启动时调用；GUI 与 CLI 同入口）：

```python
async def apply_persisted_config() -> None:
    """D1 启动装载：磁盘持久化的 config 字段优先于 env/默认兜底。

    磁盘未保存的字段保持 env/默认，不覆写（实现按「存在才装载」判读）。
    """
    state = await _io(_load_state)
    m = (state.get("config") or {}).get("tool_mode")
    if m in _VALID_TOOL_MODES:
        settings.tool_mode = m
```

装载路径汇总：
- `tool_mode`：`apply_persisted_config()` 磁盘优先；env `TOOL_MODE` 仅磁盘无值兜底。
- `api_key`：[ensure_initialized](../../../relay/relay/storage.py) 改为**磁盘有 key 即覆盖 env**；磁盘无且设了 env `RELAY_API_KEY` 时以 env 为本次有效值（不落盘）。
- `port`：`cli._cmd_run` 沿用 `get_port()`（磁盘优先，对齐 D1）。
- `--tool-mode` 显式给出 = 落盘持久化（对齐 `--port`），并覆盖磁盘装载结果。
- **调用点仅在 [cli.py `_cmd_run`](../../../relay/relay/cli.py)**：GUI（壳 spawn sidecar = `cli run`）与命令行都走该入口；routes startup 不再重复调用（db 初始化不受影响）。

### 4.2 relay：写路径统一「校验 → 落盘 → 改内存 → 返回完整 config」（F2）

重排 [auth_config_update](../../../relay/relay/routes.py)（行为不变、顺序规范化）：

1. 解析 body、逐字段校验（非法值 400，先全部校验再动任何状态）；
2. 逐字段**落盘**：`save_tool_mode / save_api_key / regenerate_api_key / set_model_whitelist / save_port`；
3. 落盘成功后**同步内存运行值**（`settings.tool_mode` 等；`save_*` 内部本就回填 settings，顺序即保证）；
4. 返回 `await auth_config()` 的完整 config —— **响应即唯一成功回执**，前端直接整体替换 store。

磁盘写失败时运行值不被污染：因为内存同步发生在落盘成功之后（save_* 落盘抛错则不执行后续）。

### 4.3 Rust 壳：ReadyInfo 收敛为进程状态、relay 接收前端 key（F4）

[lib.rs](../../../src-tauri/src/lib.rs)：

- `ReadyInfo` 收敛为 `{ port: u16 }`；`parse_ready` 只需 `port`，对行内多余的 `api_key=` / `tool_mode=` **忽略**（`[relay-ready]` 行格式不动，向后兼容）；
- `relay_status` 返回 `{"running": true, "port": r.port}`；无 running 时同现状返回 `{"running": false}`；
- `relay` 命令签名增加 `api_key: String` 入参，转发头改为 `Authorization: Bearer {api_key}`；删除从 `ReadyInfo.api_key` 取值逻辑；
- `AppState.ready` 只承载进程状态，命途与 sidecar 生命周期一致（spawn 写 / stop 清 / 重启重建），不再承载任何配置。

### 4.4 前端：Pinia config store + 顶栏 / 页面收拢（F3）

- `frontend/package.json` 新增 `pinia`（^3，兼容 vue ^3.5），`main.ts` 挂载。
- **新 store：`src/stores/config.ts`（`useConfigStore`）**
  ```ts
  state: { loaded: boolean, base_url, host, port, api_key, tool_mode, model_whitelist }
  actions:
    load()                       // GET /v1/auth/config → apply；异常置 loaded=false
    update(patch)                // POST → 响应 apply（整体替换）→ 返回响应
  apply(cfg) 内部：整对象替换 + 同步 api 层当前 key（setAuthKey）
  getters:   baseUrl 等展示派生
  ```
- **现有 [store.ts](../../../frontend/src/store.ts) 迁移为 Pinia runtime store（`useRuntimeStore`）**：保留 `running / auth / serviceEnabled / modelStatus / batchTested / view`，**移除 `relay.tool_mode`**（顶栏不再依赖壳快照）；数据源仍是 relay_status / auth_status / service_status 轮询。
- **api 层 [api.ts](../../../frontend/src/api.ts)**：
  - `relay(method, path, body)` 内部携带模块级当前 key：`invoke("relay", { method, path, body, api_key: currentAuthKey })`；提供 `setAuthKey(k)` 供 config store 同步；启动默认空串（受保护端点等 store.load 后再调）；
  - `RelayStatus` 类型改为 `{ running: boolean; port?: number }`（去掉 api_key / tool_mode）。
- **Shell.vue**：顶栏模式标签改为 `configStore.tool_mode`（`toolModeLabel` 保留复用）；就绪轮询里若 `!configStore.loaded` 触发一次 `configStore.load()`，保证 relay 就绪后 config 先于其他受保护操作装载。
- **Config.vue / Overview.vue**：删除各自局部 `config` 与重复 `getConfig`（Overview 5s 轮询一并删）；读写全部走 `useConfigStore`；配置页选项/输入框直接绑定 store。
- **端口保存编排（F5）**：`configStore.update({ port })` 成功 → `relayRestart()` → 轮询 `running=true` → `configStore.load()` 刷新（port/base_url 已可即时显示，刷新兜底收敛）。

启动时序（单例窗口）：

```
壳 spawn sidecar
  → [relay-ready]（壳只记 port）
  → 前端轮询 relay_status：running=true
  → configStore.load()（GET /v1/auth/config，真值入 store + 同步 auth key）
  → 页面/顶栏渲染 store
修改配置
  → configStore.update(patch)
      ├─ 热改字段（tool_mode/白名单/key）：POST 返回即 apply，界面即时收敛
      └─ 端口：POST 返回 → relayRestart → 就绪 → load()
```

### 4.5 接口 / 影响清单

| 层 | 文件 | 改动 |
| -- | ---- | ---- |
| relay | `storage.py` | +`get_tool_mode/save_tool_mode`、+`apply_persisted_config` |
| relay | `config.py` | tool_mode env 语义注释更新（兜底） |
| relay | `cli.py` | `--tool-mode` 改落盘；`run` 调装载 |
| relay | `routes.py` | startup 调装载；`auth_config_update` 顺序规范化（校验→落盘→内存） |
| Rust | `lib.rs` | ReadyInfo→{port}；`relay_status` 收敛；`relay` +api_key 入参 |
| 前端 | `package.json` / `main.ts` | +pinia |
| 前端 | `src/stores/config.ts`（新）/ `store.ts` | config store 新建；runtime store 迁移去 tool_mode |
| 前端 | `api.ts` | relay 带 key；RelayStatus 类型收敛 |
| 前端 | `Shell.vue` / `Config.vue` / `Overview.vue` | 读 store、删重复轮询、端口重启编排 |

## 5. 已知边界

- **环境变量语义变化（D1）**：`TOOL_MODE` / `RELAY_API_KEY` 由「启动覆盖」降级为「磁盘无值兜底」。GUI（壳 spawn 不带 env）无感；**headless CLI 用户若同时设 env 且磁盘有历史值，磁盘值生效**——与旧行为相反，需在发布说明标注。
- **装载竞态**：relay 就绪后、config 未 load 完成前，任何需要鉴权的 GUI 请求会 401 → 前端以 `loaded` 门控（Config/Overview 在 `loaded=false` 时禁用写操作或自动补 load），不依赖壳兜底。
- **`[relay-ready]` 协议**：行内三字段保留（兼容 m5 打包脚本与日志输出）；壳解析只认 `port`，新增字段不再需要壳配合。
- **单例窗口**：Tauri 单实例，无多窗口并发写配置问题；写操作统一经 store action 串行语义足够。
- **agent 直连语义不变**：base_url + api_key 由用户填给 agent，key 的当前值始终 = 磁盘值（store 装载后一致）。

## 6. 验收

### 自动化

1. relay（`cd relay && pytest tests/`）：
   - `storage.get_tool_mode / save_tool_mode` 往返 + 非法值回退 hybrid；
   - `auth_config_update` 写 tool_mode：响应含新值、`settings.tool_mode` 已变、state 落盘（读回一致）；
   - `apply_persisted_config`：磁盘有值 → settings 用磁盘值；磁盘无 → 用 env/默认（monkeypatch）；
   - `cli run --tool-mode` 落盘：模拟调用后 `get_tool_mode()` 返回新值；
   - 既有用例全绿（含 `test_m4_adaptations.py` 中 config 写路径 401/400 断言不变）。
2. Rust（`cargo test --manifest-path src-tauri/Cargo.toml`）：`parse_ready` 只需 port（缺 api_key 也能成功）；含既有 data_dir 用例。
3. 前端 `npm --prefix frontend run build`（vue-tsc + vite）通过；prettier `format:check` 通过。

### 手工回归（小号，GUI 打包后）

| # | 操作 | 预期 |
| -- | ---- | ---- |
| R1 | 配置页切 strict/passthrough | 顶栏 ≤1 轮询周期内显示对应中文标签；再次重启应用后仍为该模式（S1/S3 回归） |
| R2 | 重置密钥 → 立即点「日志」「统计」或再改配置 | 不再 401（S2 回归） |
| R3 | 改端口保存 | 自动重启，配置页 base_url / 顶栏端口展示随新值刷新 |
| R4 | 配置页选项 / Overview 工具映射区 | 展示即 store 值，切页不丢、无重复轮询请求（Network 面板无 Overview 5s config 请求） |

## 7. 支撑脚本 / 文档

- 本需求以既有单测文件扩展为主，新增 `relay/tests/test_config_persistence.py`（F1/F2/D1 语义）；Rust 侧 `parse_ready` 用例并入 `lib.rs` 测试模块。
- 前端无新增脚本；手工回归 R1–R4 建议随 M5 打包产物执行。
