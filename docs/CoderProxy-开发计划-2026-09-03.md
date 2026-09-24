# CoderProxy 开发计划（完整方案 + 路线图）

| 项 | 内容 |
|----|---|
| 版本 | v0.1（方案评审版，未定稿实现） |
| 日期 | 2026-09-03 |
| 定位 | 本地桌面中转服务：把 Ta+3「牛码」（银海）官方模型能力，以 OpenAI 兼容 API 暴露给任意第三方 agent（Trae / Cursor / Continue / opencode / cline 等） |
| 复用来源 | 参考实现仓库（已逆向出的「伪装牛码登录 + 请求指纹伪装」），只做 Ta+3 牛码 |
| 技术栈 | Tauri(Rust) 壳 + Vue3 前端 + Python FastAPI relay（vendored 的 ta3 伪装/登录库）|

---

## 0. 一句话目标

> 在中转工具里登录一次牛码（伪装成官方桌面端拿 token），本地开一个 OpenAI 兼容服务（`http://127.0.0.1:<port>/v1` + 一个静态 `api_key`），你就能在任意 agent 里把它当「自定义供应商」用，agent 的请求会被伪装成牛码官方客户端转发给牛码网关，流式返回。

## 1. 要解决什么问题

- 牛码账号/额度（银海 Ta+3 的 kimi / deepseek 等模型）通常绑定官方桌面客户端，第三方 agent 接不进去。
- 参考实现 项目已经做完「**伪装登录**（PKCE-SM3 / 银海通 IM 静默）+ **请求指纹伪装**（Electron UA、X-Call-Source、工具名双向映射）」这套脏活，且 **模块化、与 参考实现 主流程解耦**，可直接抽取复用。
- 本项目把这套能力二次封装成一个**本地中转网关 + 图形界面**，对外只暴露最标准的 OpenAI Chat Completions 协议，让接入成本降到「填一个 base_url + api_key」。

## 2. 非目标（第一版不做）

- ❌ 不做转发侧的多轮/审批/上下文压缩/编排（那些是每个 agent 自己 lane 的事，中转只做单请求伪装转发）。
- ❌ 不接入 workbuddy / trae 等其他供应商（本期只做 ta3）。
- ❌ 不提供 web 鉴权、多用户、云部署（纯本机单用户工具）。
- ❌ 不保证第三方 agent 的长尾工具（apply_patch/glob/lsp/skill/MCP）都能伪装成功。

## 3. 调研：常见 agent 的工具集与「伪装到 ta3」可行性

### 3.1 ta3（牛码）原生工具集（PascalCase，来自 参考实现 `ta3_tool_schemas.py`）

`Read, List, Search, Diff, Write, Edit, Bash, WebSearch, TodoWrite, SubAgent, Plan, ReadSkill, get_project_memory, generate_project_memory, read_file_range, get_file_outline, RevertFile, single_find_and_replace, TaskQuery, TaskCancel`（项目另补 `ReadAttachment, ViewImage, BashStatus, BashKill`）。

### 3.2 各 agent 工具 vs ta3 对应

| Agent | 工具（摘主干） | 能 1:1 伪装成 ta3 | 无对应（会丢） |
|---|---|---|---|
| **CodeBuddy**（腾讯） | `Read, Write, Bash, Glob, Grep, Edit, WebSearch, TodoWrite, SubAgent, Plan` + `mcp__*` | ✅ 与 ta3 **同风格**：Read/Write/Bash/Edit/WebSearch/TodoWrite/SubAgent 直通；Glob≈List、Grep≈Search | MCP 工具 |
| **Trae** | 内置 `阅读/文件系统(增删改查)/终端/联网搜索/预览`；Plus：`workspace search`(语义检索)、todo、sequential thinking | ✅ head 好映射：Read/Edit(+Write)/Bash/WebSearch | workspace search、preview、sequential thinking |
| **Codex**（OpenAI，Responses API 风格） | `shell / read_file / list_dir / grep_files / glob_file_search / apply_patch / view_image / update_plan / web_search / exec_command / write_stdin` | ⚠️ 部分：read_file→Read、list_dir→List、grep_files→Search、web_search→WebSearch、exec_command→Bash、update_plan≈TodoWrite | `apply_patch`（保真 diff 写入，ta3 无等价）、glob_file_search、write_stdin |
| **OpenCode** | 小写 `bash / write / edit / read / grep / glob / lsp / patch / skill / todowrite / webfetch / task / websearch` | ⚠️ 部分：bash/write/read/edit→Bash/Write/Read/Edit、grep→Search、todowrite→TodoWrite、websearch→WebSearch、glob≈List | lsp、skill、patch、webfetch、task |
| **ZCode** | 套壳，集成 Claude Code / Gemini / Codex / OpenCode / Z.AI；ZCode Agent 专属 ≈ Anthropic 风格（Read/Write/Bash/Edit/Grep/Glob/TodoWrite/SubAgent/Plan/MCP） | 取决于底层框架（同上逐项） | MCP、框架长尾 |

### 3.3 可行性结论（关键）

1. **公约核心可伪装**：四家的"读 / 写 / 编辑 / 终端 / 搜索 / 待办 / 联网搜索"概念都在，能 1:1 重命名成 ta3 原生 PascalCase，参数做护栏式适配。
2. **长尾工具无法伪装**：Codex 的 `apply_patch`、OpenCode 的 `glob/lsp/skill/patch/webfetch/task`、各家的 MCP 工具（`mcp__*`）、Trae 的语义检索，**在 ta3 原生工具里没有等价物**。当前 参考实现 的 `disguise_tools` 做法（无映射即丢弃）对这些就是"直接砍掉该能力"。
3. **最大未知（必须先探针）**：牛码网关对"非 ta3 原生工具名"的**容忍度**。参考项目只发 ta3 原生名是因为契约假定如此；但标准 OpenAI function calling 通常只校验 schema 形状、不校验名字——**若牛码容忍任意工具，则长尾可透传保留；若拒绝，则只能丢弃**。→ 见 M1 探针实验。
4. **推荐策略 hybrid（可配置三档）**，探针后收敛默认值：
   - `hybrid`（默认候选）：head 工具 → 伪装成 ta3 原生名；长尾/MCP → 按探针结果「透传保留」或「丢弃单工具、不炸会话」。
   - `strict`：完全对齐参考项目，只发有映射的工具（其余丢弃）。最像官方客户端，但砍掉 agent 长尾能力。
   - `passthrough`：工具原样透传，只伪装头/UA/登录。兼容性最好；放弃工具名伪装这一层。

> **结论措辞**：工具伪装「部分可行」——能可靠伪装公约核心头部，但**永远覆盖不了各家长尾工具**；必须配合透传/丢弃的降级策略，并以一次真实探针实验定夺牛码的容忍边界。

### 3.4 M1 探针实测结论（✅ 已完成）

> 实测环境 2026-09-03，模型 `glm-5.3-flash`，登录走银海通 IM 静默(PKCE 兜底)，chat 用模型自带 `llm-` key。

| 探针 | HTTP | 实测 | 结论 |
|---|---|---|---|
| A. 原生工具 `Bash` | 200 | 模型真返回 `tool_calls: Bash({"command":"echo hi"})` | 登录+模型key+工具调用链路通 |
| B. 未知工具 `glob_file_search` | 200 | 模型照常应答，self-describe「有文件搜索工具」 | **牛码网关容忍任意工具名，不拒** |
| C. 无工具 | 200 | 正常回 "ok"（含 `reasoning_content`） | 基础调用可用 |

**确定性结论（直接影响 relay 设计，已写入 §5.4）：牛码网关对未知/长尾工具名高度容忍——标准 OpenAI function calling，校验 schema 形状而非名字。因此 hybrid 的长尾默认 = `passthrough（透传保留）`，不必复用 参考实现 的「舍弃长尾」逻辑。**

**M2 落地必须先搞定的三个现实坑（探针踩到）：**
1. **目录端点**：真正的可用端点是 `POST /ai/continue/ide/list-assistants?appId=personal`（返回**裸数组**）；`/ide/list-organizations`、`/ide/list-assistants`、`/ai/continue/ide/list-assistants(GET)` 实测 404/418。catalog.py 的候选顺序需上调 POST appId 端点。
2. **chat 鉴权**：走**模型自带 `apiKey`（`llm-…`）**，不是 oauth 会话 token；`Authorization: Bearer <llm-…>` + `api-key: <llm-…>`。
3. **直连**：本机 httpx 默认读系统代理会把 `lc.yinhaiyun.com` 走隧道失败 → **必须 `trust_env=False`**。

## 4. 总体架构

```
┌─────────────── Tauri(Rust + Vue3) 桌面壳 ───────────────┐
│  Vue 前端(GUI)                                           │
│  ├ 登录(拨浏览器/IM 静默) → 状态/账号/模型卡片            │
│  ├ 中转配置: 端口 / API_KEY(静态，agent里填这个) / 模式   │
│  ├ 工具模式: hybrid|strict|passthrough                   │
│  └ 日志面板(流式+错误)                                    │
│  Rust 后台                                               │
│  ├ 打包并 spawn Python sidecar(PyInstaller单文件)          │
│  ├ sidecar 生命周期管理 / 健康检查 / 端口探活 / 日志接管    │
│  └ 配置持久化(conf 文件: port / api_key / UA / tool_mode)│
└───────────────┬──────────────────────────────────────────┘
                │ HTTP(localhost) / sidecar stdout
┌───────────────▼────── Python FastAPI Relay (sidecar) ───┐
│  vendored 复用 参考实现:                                │
│   app/auth/ta3/*      登录(PKCE-SM3 + IM静默)/session/catalog│
│   models/providers/ta3*.py + aliases + schemas           │
│                        请求伪装 + 双向工具名 + SSE解析     │
│  Relay 新增:                                             │
│   oai_adapter.py    OpenAI↔ChatRequest/ChatResponse 映射  │
│   routes.py:      POST /v1/chat/completions (SSE+非流)   │
│                   GET  /v1/models                         │
│                   POST /v1/auth/login/{start,cancel}     │
│                   GET  /v1/auth/status  POST /v1/auth/logout│
│                   POST /v1/auth/sync                     │
│   middleware: 静态 api_key Bearer 鉴权                    │
│   storage:     本地 sqlite(ta3_auth 单行 + 配置)          │
└───────────────┬──────────────────────────────────────────┘
                │ OpenAI /v1, `Authorization: Bearer <静态key>`
                ▼
     任意第三方 agent (Trae/Cursor/Continue/opencode/cline)
```

**职责边界**：
- Relay = 登录、token 生命周期、请求伪装转发、SSE 收发。**只做单次请求的伪装转发**，不做 agent 循环/审批/压缩。
- Tauri = 界面 + sidecar 托管 + 配置。
- 每个 agent 自己负责自己的 agent-loop（含工具执行），中转只负责把它的 OpenAI 请求"伪装"成牛码客户端并发给牛码。

## 5. Relay（Python FastAPI sidecar）详细设计

### 5.1 复用什么、新增什么

- **复用（vendored 最小集）**：
  - `app/auth/ta3/`：`oauth.py`（PKCE-SM3 + 银海通 IM 静默登录）、`callback.py`（本地回调 server）、`pkce.py`（SM3 国密实现）、`session.py`（token 存取 / 401 刷新）、`catalog.py`（同步模型目录）。
  - `app/models/providers/ta3.py`（请求伪装头 + OpenAI/Anthropic 双 SSE 解析 + 工具名反伪装）+ `ta3_tool_aliases.py` + `ta3_tool_schemas.py`。
  - `app/models/schemas.py`（`ChatMessage/ChatRequest/ChatResponse/Usage`，或抽取最小别名）。
- **新增**：
  - `relay/oai_adapter.py`：OpenAI 线协议范式 → 本项目 `ChatRequest`，及 `stream_structured` 产出 → OpenAI SSE 分帧。
  - `relay/routes.py`：`/v1/*` 路由。
  - `relay/middleware.py`：静态 `api_key` Bearer 鉴权。
  - `relay/storage.py`：本地 sqlite（`ta3_auth` 单行 + 中转配置）。
  - `relay/tool_disguise.py`：三模式工具伪装编排（见 §5.4）。
  - `relay/config.py`：环境变量/`.env`/conf 文件统一配置加载。

### 5.2 对外 OpenAI 协议（第三方 agent 看到的世界）

- **`POST /v1/chat/completions`**
  - 入站解析：`model / messages / stream / tools / tool_choice / temperature / max_tokens / reasoning_effort`。
  - 走 `Ta3Provider.stream_structured(request)`（复用整套伪装/流式解析）。
  - 出站 SSE 分帧（兼容各 agent 的流式协议）：
    - 思考 → `delta.reasoning_content`（部分 agent 只认这个；兜底可同时写 `delta.content`，可配）
    - 正文 → `delta.content`
    - 工具调用 → `delta.tool_calls[].function.name/arguments`（透传则原样；伪装模式则反伪装回 agent 真名）
    - 结尾 `usage`（prompt/completion/total + reasoning_tokens）+ `finish_reason` + `data: [DONE]`
  - 非流式：内部收集整段，返回标准 JSON `choices[].message`.
  - 401 处理：牛码侧 401 → `session.refresh_token_once` → 重试一次（复用 ta3 语义）。
- **`GET /v1/models`**：返回 `catalog.py` 同步的模型目录（`id/name/context_window/supports_reasoning/is_multimodal`）。
- **`POST /v1/auth/login/start` / `GET /v1/auth/status` / `POST /v1/auth/login/cancel` / `POST /v1/auth/logout` / `POST /v1/auth/sync`**：GUI 调用。

### 5.3 登录与 token 生命周期（复用 ta3，不改语义）

- `oauth.start_login`：**先试银海通 IM 静默登录**（本机 `:13631` 有银海通则秒登），失败降级浏览器 PKCE-SM3（本地回调 server 收 code → 换 token）。
- GUI 点登录 → 打开系统浏览器授权 → 回调 `code` → 换 `access_token` → 落 sqlite `ta3_auth`.
- 每次发请求前 `session.ensure_token`；牛码 401 → `refresh` 一次 → 重试一次。
- 退出：清 `ta3_auth` + 复位。

### 5.4 工具伪装（核心，三模式编排）

以 `ta3_tool_aliases.py` 的 `TO_TA3 / FROM_TA3 / ARGS_TO_TA3 / ARGS_FROM_TA3` 为核心，新增「agent 侧常见工具 → ta3」**扩展映射**（如 `bash→Bash, glob→List, grep→Search` 等，覆盖 §3.2 表格）：

- **出站工具 schema**：
  - `hybrid`（默认，M1 已定）：有映射 → 重命名 + 参数适配；无映射 → **默认「透传保留」**（M1 确认牛码容忍任意工具名），可配置退化为丢弃。
  - `strict`：同 `disguise_tools` 现状——无映射即丢弃。
  - `passthrough`：tools 原样透传。
- **出站历史 tool_calls**：若伪装模式，历史调用名/参数也要伪装（复用 `_disguise_message`）。
- **入站 tool_calls**：模型返回的 ta3 名 → 反伪装回 agent 真实名（复用 `_restore_tool_calls`）。
- **M1 探针决定（已完成）**：牛码**容忍**未知工具名 → hybrid 长尾默认 **透传保留**（见 §3.4）。

## 6. Tauri（Rust）+ Vue3 前端设计

### 6.1 技术选型
- Tauri 2（Rust 后台，`tauri-plugin-shell` 管理 sidecar、`tauri-plugin-store` 持久化）。
- 前端 Vue3 + Vite + Element Plus（或其他，实现时定）。
- Python sidecar 用 **PyInstaller 打成单文件 exe**（避免用户装 Python；注意 参考实现 文档提到的 PyInstaller hidden-imports 坑，SM3 是纯 Py 无此问题）。

### 6.2 GUI 页面
1. **登录页 / 状态**：牛码登录按钮、账号 label、退出；同步模型按钮 + 模型表格（名/上下文/是否多模态/是否思考）。
2. **中转配置**：监听 host/port、`API_KEY`（默认启动随机生成，可一键复制；可改静态）、工具模式（hybrid/strict/passthrough）、UA.
3. **快速接入引导**：给出「在 Trae/Cursor/Continue/opencode 里填 `base_url + api_key`」的示例 JSON/文案例。
4. **日志面板**：请求转发、SSE 增量、错误；便于排查。

### 6.3 Rust↔sidecar 交互
- 启动：spawn sidecar 单文件 exe，传 `--port --api-key --config`；侧读 stdout 拿就绪信息与日志。
- 健康：`GET /v1/models` 探活。
- GUI → relay：`invoke_handler` → Rust → HTTP(localhost:port) 调 `/v1/auth/*`、`/v1/models`。

## 7. 配置与本地鉴权

- **你在 agent 里填的 `api_key` = 中转自己的静态 token**（`RELAY_API_KEY`）。真正的牛码 `access_token` 由 relay 持有，二者分离。
- 默认行为：relay 启动随机生成 `RELAY_API_KEY` 并在 GUI 展示；用户改配置则固定。
- `.env`/conf 项：
  - `RELAY_HOST=127.0.0.1`，`RELAY_PORT=8786`
  - `RELAY_API_KEY=<你填进 agent 的静态 key>`
  - `TA3_API_BASE=https://lc.yinhaiyun.com/newcoder`（缺省，可覆盖）
  - `TA3_USER_AGENT`（覆盖默认 Electron 同族 UA，伪装用）
  - `TOOL_MODE=hybrid|strict|passthrough`
  - `TRUST_ENV_PROXY=true`（httpx trust_env，与 CLI 一致）

## 8. 目录结构（未来实现铺排）

```
CoderProxy/
├─ src-tauri/                # Rust 壳 (Tauri2)
│   ├─ src/                  #   spawn sidecar / 配置 / invoke handlers
│   └─ tauri.conf.json
├─ frontend/                 # Vue3 + Vite 前端
│   └─ src/views/            #   登录/配置/模型/日志 页面
├─ relay/                    # Python FastAPI relay（vendored ta3）
│   ├─ app/  auth/ta3/*  models/providers/ta3*.py  models/schemas.py   # vendored
│   ├─ relay/  oai_adapter.py routes.py middleware.py storage.py tool_disguise.py config.py
│   ├─ vendor/  sm3.py 等     # 纯 Py 依赖
│   └─ pyproject.toml
├─ docs/  README.md(索引+自举) 开发规则.md 总计划  spec/(README + 每里程碑一子目录)
├─ AGENTS.md
└─ README.md
```

## 9. 里程碑与验收

| 阶段 | 内容 | 验收 |
|---|---|---|
| **M1 探针（✅ 已完成）** | 登录拿 token → 裸发 3 个最小 chat.completions（原生工具/未知工具/无工具）实测容忍度 | ✅ 结论：**牛码容忍任意工具名** → hybrid 长尾默认透传保留；同时定位目录端点为 POST `/ai/continue/ide/list-assistants?appId=`、chat 走模型 `llm-` key、需 `trust_env=False`（详见 §3.4） |
| **M2 Relay CLI** | vendor ta3 最小集 + `/v1/chat/completions`(SSE+非流) + `/v1/models` + 静态鉴权，纯命令行起服 | `curl -N http://127.0.0.1:8786/v1/chat/completions` 打通；思考/正文/usage/`[DONE]` 齐全 |
| **M3 工具伪装三模式** | `tool_disguise.py` 三档 + 反向 restore + 扩展映射表 | 同一份多工具请求在 strict/hybrid/passthrough 下输出正确；agent 端工具调用回传正常 |
| **M4 Tauri 壳** | Rust spawn sidecar + Vue 登录/配置/模型/日志 + 快速接入引导 | 一键启动；GUI 登录成功并同步出模型；agent 配好 base_url+key 后能对话 |
| **M5 打包/文档** | PyInstaller 单文件 + tauri-bundle；README + 各 agent 接入样例 | 双击即可用；干净机器跑通 |

> 每阶段先跑真实牛码账号做冒烟（用小号），再补单元测试（参考 参考实现 的 `test_ta3_*.py`，keyless 断言请求头/SSE 解析/401 刷新）。

## 10. 风险与合规（务必读完）

| # | 风险 | 缓解 |
|---|---|---|
| 1 | **合规/风控**：逆向+伪装登录属非官方客户端行为，牛码可风控**封号** | 只用小号验证；文档明示责任自负；头/UA/指纹做到与参考客户端尽量一致 |
| 2 | **牛码网关对未知工具名的容忍度未知** | M1 探针先行；默认 hybrid：不杂会话 |
| 3 | **牛码协议变更**（端点/UA/请求体/工具契约） | 端点与 UA 配置化；`ta3_*` 单一修改点；跟进参考项目 |
| 4 | **长尾工具能力丢失**（apply_patch/glob/lsp/skill/MCP） | 文档明确各 agent 不可伪装项；建议用 `passthrough` 优先保能力 |
| 5 | **Python 假运行时体积** | PyInstaller 单文件；必要时只打包 relay，Tauri 壳极薄 |
| 6 | 端口占用 / sidecar 崩溃 | Tauri 层探活自拉起；日志可查 |
| 7 | 不同 agent 流的 thinking 字段差异 | 出站统一 `reasoning_content`，兜底 content，可配 |

## 11. 待用户拍板的开放项

1. **项目命名**：暂定 `CoderProxy`（牛码中转），目录 `D:\Code\home\own-project\CoderProxy`，可改。
2. **工具模式默认值**：✅ 已定（M1 探针）——默认 `hybrid`，长尾=透传保留（见 §3.4）。
3. **API Key 默认策略**：建议启动随机生成 + GUI 展示/复制（比固定静态更省心），你也能改成固定。
4. **M1 探针（已做完毕）**：✅ 详见 §3.4，整方案假设已坐实，可进入 M2。