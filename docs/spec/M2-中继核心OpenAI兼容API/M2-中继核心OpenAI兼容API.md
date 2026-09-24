# 中继核心（OpenAI 兼容 API）（M2）

- **状态**：已实现（2026-09-03）
- **优先级**：核心（第一步要能跑通的后端）
- **来源**：开发计划 §4-§5；M1 三坑见 §3.4
- **定稿日期**：2026-09-03
- **实现说明**：`relay/` 目录 = vendored ta3 最小集（`app/`，尽量不改）+ relay 新增模块（`relay/`）。启动：`python run.py run`（本机 shim 环境）或标准环境 `python -m relay run`。vendored 仅有去 sqlalchemy 依赖的必要适配（import 位置挪动，逻辑不变）；`app/auth/ta3/session.py` 由 relay 重写为本地 JSON 文件存储（M2 口径）。

## 需求背景

为目标：把牛码模型以 OpenAI `Base URL` + `API Key` 暴露给任意 agent。M2 只做纯后端 relay 最小可运行版本——能登录、能列模型、能把 `chat/completions` 转发到牛码（SSE + 非流），伪装登录与工具伪装分层接入（工具伪装归 M3）。

## 设计目标

纯 Python FastAPI relay，最小集：登录 + 目录 + OpenAI 协议转发 + 本地静态鉴权。不带 GUI/MCP/多模型路由（后续 M4/M5 补）。

## 设计方案

### 1. 伪装登录（复用 vendored token）

把 参考实现 的 ta3 登录最小集 vendored 进 `relay/`：PKCE-SM3 浏览器登录 + 银海通 IM 静默秒登 + token 落本地文件（非 DB）。登录在 CLI/后续 GUI 触发，非每次请求。

### 2. 目录与 chat 参数（M1 三坑落地）

- 目录：`POST /ai/continue/ide/list-assistants?appId=personal`（裸数组）→ 解析出模型（name / apiBase / **apiKey=`llm-…`** / anthropic 标志 / completionOptions）。
- chat 请求鉴权：模型自带 `llm-` key，非会话 token。`Authorization: Bearer <llm-…>` + `api-key: <llm-…>`。
- 所有对外请求 **`trust_env=False`** 直连（绕系统代理）。

### 3. HTTP 接口（对 agent）

| 接口 | 说明 |
|---|---|
| `GET /v1/models` | 列出牛码可用模型 |
| `POST /v1/chat/completions` | OpenAI 兼容；`stream=true`(SSE) 与 `stream=false` 都支持 |
| `GET /v1/models/{id}` | OAI 兼容单模型（可选） |

转发要点：
- **流式透传**：把牛码 SSE 逐块转发，并做**字段翻译**——牛码的 `reasoning_content` 映射到 OpenAI 的 reasoning/`delta.reasoning`，`usage`/`stream_options.include_usage` 归一。
- **工具调用**：先把 `finish_reason=tool_calls` 直接透传（双向翻译归 M3），保证"能调用工具"先立起来。
- **401 处理**：token 失效时刷新并重试一次。

### 4. 本地鉴权（静态 Relay API Key）

- 启动固定/随机 `RELAY_API_KEY`（§评估项），agent 填它鉴权；真正的牛码 token 只在 relay 内部。
- 校验 `Authorization: Bearer <RELAY_API_KEY>`，未匹配 401。

### 5. 配置与运行

- 启动：CLI `python -m relay run [--port 8786] [--api-base …]`。
- 配置：api-base / model 白名单 / 端口 / key 放本地 config（CLI/GUI 边缘，不进 git）。

## 否决的备选

- **直接起 参考实现 整套服务**：拖 Postgres/Redis/Qdrant，太重；relay 只需 ta3 伪装子集。
- **用 参考实现 的 catalog.py**：其候选端点顺序在实测中失效（GET 404/418），需按 M1 结论重排，故 vendored 后本地改。

## 已知边界

- 单模型路由（取目录第一个可用 openai 协议模型）；多模型选择/白名单留到后续。
- 非流式由 relay 聚合 SSE 实现，注意并发与超时。
- 不处理 rate limit / 并发队列（后续）。

## 验收

- [x] `python -m relay run` 起服；`/v1/models` 返回非空模型列表（实测同步出 5 个模型：glm-5.3-flash / glm-5.3 / kimi-k3 / deepseek-v4-flash / deepseek-v4-flash-vision-exp）
- [x] `curl /v1/chat/completions`（非流+流）均能返回模型回复，`reasoning_content` 正确落位（实测 deepseek-v4-flash，非流含 reasoning_content+usage，流式逐块 SSE + usage + `[DONE]`）
- [x] 错误鉴权 → 401；模型能发起 `tool_calls`（透传阶段不要求双向翻译）—— 401 ✅；tool_calls 透传沿用 vendored ta3.py 的 strict 式 disguise（只认 参考实现 内部工具名），第三方 agent 常用工具名的扩展映射归 M3，**真实 tool_calls 触发待用小号复核**
- [x] 登录 token 持久化，重启 relay 免重登（实测重启后 `/v1/auth/status` 仍 logged_in）

> 冒烟账号：本机银海通 IM 静默登录（开发机账号）。按 AGENTS 硬性规则 5，端到端（尤其工具调用）建议另行用小号复核。