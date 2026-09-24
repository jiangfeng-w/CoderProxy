# Tauri 桌面壳与 GUI（M4）

- **状态**：实现中（relay 侧适配已落地 2026-09-03；Tauri 壳待 Rust 工具链）

- **优先级**：交付

- **来源**：开发计划 §4（架构：Tauri(Rust) 壳 + Vue3 前端 + spawn Python relay sidecar）

- **定稿日期**：2026-09-03

## 需求背景

M2/M3 完成后是纯后端 relay，需要一层桌面壳让非命令行用户"点一下就登录、配置、开聊"：伪装牛码登录、持有静态 API Key 展示、管理 relay 进程。

## 设计目标

Tauri 2(Rust) 壳 spawn Python FastAPI relay 作 sidecar，Vue3 前端承载登录/配置/模型/日志四屏。

## 设计方案

### 1. 架构

```
Tauri 2 (Rust) ──spawn──► Python relay sidecar (FastAPI, vendored ta3)
Vue3 前端 (webview) ◄──IPC── 壳（登录/配置/日志/状态）
```

- Rust 壳负责：spawn/kill sidecar、端口分配、持 token 与 config、向前端暴露 IPC。

- Python sidecar 是独立的 `relay` 包，M2 已实现核心；M4 只加"命令行参数驱动 + 被 spawn"的适配。

### 2. GUI 四屏

- **登录**：触发 IM 静默（优先）/ PKCE-SM3 浏览器授权；显示登录状态与当前账号。

- **配置**：端口、模型白名单、RELAY 静态 API Key（生成/复制）、工具模式（hybrid/strict/passthrough 下拉）。

- **模型**：列出目录可用模型，勾选启用。

- **日志**：relay 转发日志、工具映射命中/长尾透传计数、401 刷新记录。

### 3. relay 进程管理

- 随壳启动/退出；端口冲突自动换；崩溃自动拉起并提示。

- sidecar 配置经参数/JSON 传入，不写进仓库。

## 否决的备选

- **纯 CLI（不要 GUI）**：能用但违背"给 agent 配置供应商"的交付定位；CLI 保留作 sidecar，GUI 才是一等入口。

- **Electron**：体积更大、指纹更易被风控识别；Tauri 壳更轻、UA 无关。

## 已知边界

- Sidecar 是 Python，需随包携带运行时（见 M5），体积/启动体积权衡。

- Webview 登录联动浏览器授权，需处理跨浏览器回跳 focus。

## 已落地（relay 侧适配，2026-09-03）

Rust 工具链未装，按「relay 侧优先」先做 sidecar 被 spawn 所需的一切适配，Tauri 壳后续补齐：

- **模型白名单**：`relay/storage.py` 新增 `get/set_model_whitelist`、`is_model_enabled`（空=全部启用，兼容 M2/M3）；`/v1/models` 只暴露勾选模型；chat 对白名单外模型 404。

- **静态 API Key 管理**：`storage.save_api_key` 持久化；CLI `--api-key` 设置并落盘；`/v1/auth/config` GET 展示 + POST 部分更新（api\_key / tool\_mode / model\_whitelist）。

- **监控（GUI 日志面板数据源）**：新建 `relay/monitor.py`（环形事件缓冲 + 按 kind 统计）；`/v1/monitor/stats`、`/v1/monitor/events?after_id=` 增量拉取；chat 请求/完成/错误、401 刷新、工具映射命中/长尾透传/丢弃（`tool_disguise.DisguiseContext` 计数）全部上报。

- **sidecar 就绪协议**：startup 向 stdout 打 `[relay-ready] port=... api_key=... tool_mode=...`（flush=True，管道缓冲不吞行）；CLI banner 同 flush。

- **端口分配**：`--port 0` 随机空闲端口；目标端口被占自动顺延（最多 100 个）；`_pick_port` 单元覆盖。

## 验收

- [x] relay 侧：模型白名单只暴露勾选模型到 `/v1/models`；chat 白名单外 404

- [x] relay 侧：静态 API Key 生成/持久化/展示/覆盖（CLI `--api-key` + `/v1/auth/config`）

- [x] relay 侧：`/v1/monitor/*` 可见工具映射命中/长尾透传/401 刷新计数

- [x] relay 侧：端口冲突自动切换、`--port 0` 随机端口、`[relay-ready]` 就绪行

- [ ] 点登录 → IM/PKCE 登录成功，状态可见（Tauri 壳）

- [ ] 配置页生成/复现静态 API Key，agent 用 `http://127.0.0.1:8786/v1` + key 可聊（Tauri 壳）

- [ ] 模型页可勾选，只暴露勾选模型到 `/v1/models`（Tauri 壳）

- [ ] 日志可见工具映射与长尾透传计数（Tauri 壳）

- [ ] sidecar 随壳启停（Tauri 壳）

