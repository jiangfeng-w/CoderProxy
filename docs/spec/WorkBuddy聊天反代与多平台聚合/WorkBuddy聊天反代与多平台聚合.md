# WorkBuddy 聊天反代与多平台聚合

> **状态**：**已实现**（2026-10-09——实现步骤 1–8 全部落地；pytest 367 passed；e2e/打包产物冒烟 19 项全绿；三协议真客户端回填多协议需求 A 段）
> **优先级**：核心
> **来源**：用户最终愿景：对标 9router 的多平台账号聚合网关（牛码 + WorkBuddy 双上游聊天反代，对外统一 OpenAI 兼容 /v1），附带签到功能；2026-10-07 用户拍板转发架构路线——「入站做功夫，牛码单独用 Ta3Provider，其他借鉴 9router」；2026-10-08 用户拍板模型命名前缀体系（A1）与其余按建议推进
> **依赖**：WorkBuddy多账号登录与签到（账号池底座，已实现 2026-10-08）；与[多协议端点兼容](../多协议端点兼容-三协议入站统一/)共享入站归一层（正交）
> **定稿日期**：2026-10-07（架构）/ 2026-10-08（细化）；**实现完成**：2026-10-09
> **隐私声明**：本文档仅含公开开源项目信息与占位符示例，不含任何真实账号、凭证、token、uid。

## 1. 背景 / 目标

CoderProxy 最终形态 = 多平台 AI 账号聚合网关：agent 只见 OpenAI 兼容 `/v1`，relay 内部把请求路由到牛码或 WorkBuddy（按模型名），多 WorkBuddy 账号池轮转。对标 9router（`decolua/9router`，本地克隆 `d:\Code\ref-projects\9router`），差异点是 CoderProxy 自带伪装登录与签到，且不暴露任何上游内部协议。

三阶段总览：

| 阶段 | 内容 | 状态 |
|---|---|---|
| ① 牛码反代 | ta3 登录 + /v1/chat/completions 转发 | 已实现 |
| ② WorkBuddy 账号层 | 多账号登录 + 签到 + 配额 | 已实现（2026-10-08） |
| ③ WorkBuddy 聊天反代（本需求） | 聊天转发 + 多平台聚合路由 + 账号池轮转 + 自定义供应商转发生效 | **方案已定稿** |

## 2. 参考项目（本地克隆于 `d:\Code\ref-projects\`）

| 项目 | 借鉴什么 | 关键位置 |
|---|---|---|
| **workbuddy2api-panel**（Go, 2.1k★） | **聊天契约主参考**（自身即 WorkBuddy 反代，代码多轮同步最新）：chat 出站头族/UA 档位、payload 改写管线、SSE 帧白名单重建、模型目录双端点、错误分类 | `internal/upstream/headers.go`、`payload.go`、`client.go`、`internal/server/handler.go` |
| **workbuddy-cockpit**（Go, MIT） | 同源谱系（可交叉印证头族/会话头族/pool 冷却分级）；少量超前特性（会话头族 issue #35） | `internal/upstream/`、`internal/pool/` |
| **9router**（Node） | **聚合架构主参考**：executor/translator 正交分层、DefaultExecutor + 薄覆写、provider/connection 分离 | `open-sse/executors/codebuddy-cn.js`、`registry/codebuddy-cn.js` |
| **cockpit-tools**（Rust, 18.7k★） | **只借登录/签到/配额契约与会话存储结构**——已核实**无聊天反代功能**（其 `codex_local_access` 系列是 Codex 专用本地网关，上游 `chatgpt.com`/`api.openai.com`；对 `copilot.tencent.com` 只有 `workbuddy_oauth.rs` 登录） | `crates/cockpit-core/src/modules/` |

> B1 结论（2026-10-08 源码核实）：2api-panel 默认 chat 出站 = **桌面档**（`userAgent` 默认三段式 `WorkBuddy/5.5.4 WorkBuddy/5.5.4 CLI/2.137.1` + 归属四头 `X-Agent-Purpose: conversation`/`X-IDE-Name|Type: WorkBuddy`/`X-IDE-Version`/`X-Product: WorkBuddy`）；README 里「默认保持 CLI 档」是初版提交的过时陈述，以代码为准。9router 的 CLI 档（`X-Product: SaaS`、`X-IDE-*: CLI`、UA `CLI/2.108.1 CodeBuddy/2.108.1`）为备选档位。

## 3. 转发架构（2026-10-07 定稿）

### 3.1 为什么不把 Ta3Provider 塞进 9router 式 executor

9router 是 Node、Ta3Provider 是 Python，代码级复用不存在。架构层面也不该硬塞：9router 的 executor 只管传输（`buildUrl`/`buildHeaders`/重试），**协议翻译全部在 translator 层**；而 Ta3Provider 是「协议翻译 + 请求指纹 + 流解析」三合一（vendored 资产，拆它风险大收益零）。定稿路线（用户拍板）：**入站做功夫，牛码单独用 Ta3Provider，其他借鉴 9router**。

### 3.2 分层设计

```
入站协议层   /v1/chat/completions（现有）+ /v1/responses、/v1/messages
             （见[多协议端点兼容](../多协议端点兼容-三协议入站统一/)定稿——入站识别 + 转换器归一到 ChatRequest，
             鉴权头 Bearer / x-api-key 兼容提取）
      ↓
路由层       adapter_for(model)：**模型名前缀**（`<供应商名>/<模型>`,见 §4.1）判 provider——
             "牛码/…" → Ta3Provider；"WorkBuddy/…" → WorkBuddy 适配；
             其余前缀匹配自定义供应商（平台自定义名）→ OpenAICompatProvider + 纯配置
      ↓
出站 adapter 层（借鉴 9router 三层经验）
             - Ta3Provider：原样保留（接口 chat/stream_structured 不动）
             - OpenAICompatProvider：新建默认实现，只管传输（URL/头/auth/retry）
             - WorkBuddy 等：不写独立类——对齐 9router `codebuddy-cn.js` 模式，
               继承默认实现 + 钩子（transform_request 改请求怪癖、parse_error 认特殊错误码）
             - 自定义 baseurl：零代码纯配置（9router openai-compatible-* 节点即此做法）
      ↓
出站流转换   上游 SSE/事件 → 入站协议帧的翻译统一在出口管道（逐 chunk 纯函数 + 每流状态机），
            不散在 adapter 里（9router streamingHandler + initState 模式）
```

与「多协议端点兼容」的分工：该需求 = 本架构的**入站层定稿**（只到 ChatRequest 为止，出站当时仅挂 Ta3Provider）；本需求在其上做**出站层扩展**（adapter 注册表化）。两者正交，可分别实施；都会动 `routes.py` chat 管道，建议串行。

### 3.3 provider 与 connection 分离（9router 核心经验）

- **provider = 静态协议定义**：baseUrl 形态、协议、默认头、auth 描述符、怪癖——代码/配置常量。
- **connection = 动态凭据**：具体账号的 token、自定义 baseUrl 覆盖——来自各平台账号池（②的 `platform_workbuddy.json` / 自定义供应商配置）。
- `execute` 时合成 credentials 注入 adapter。这与「平台注册表 + 各平台账号池 JSON」的既有设计（②）天然对上。

### 3.4 Ta3Provider 的演进（优化不冻结）

保留 ≠ 冻结（AGENTS 规则 1 允许本地修订）。已知优化点与时机：

| 优化点 | 现状 | 时机 |
|---|---|---|
| BUG-001 空流假成功 | **已修复 2026-10-08** | 已闭环 |
| 上游错误非结构化 | vendored ta3.py 抛 `RuntimeError("模型请求失败 {status}：{body}")` 文本，`routes.py` 被迫正则 `_UPSTREAM_ERR_RE` 反解析 | **本期随 adapter 抽象一起做**（ta3.py 加结构化异常，加「本地修订」注释） |
| 401 刷新重试下沉 | `_sse_with_retry`/`_chat_with_retry` 耦合牛码语义（重跑 sync_models） | **本期下沉**到各 provider 的 `refresh_credentials` 钩子（Ta3Provider 实现 = sync_models；WorkBuddy = 账号池 refresh） |

原则：`chat`/`stream_structured` **签名保持稳定**（oai_adapter/routes/tests 都依赖）；接口定型后内部实现可安全重构——adapter 接口 + 回归测试是护栏。

### 3.5 账号池轮转与故障转移（节奏分层）

1. **本期第一版**：简单轮询选号 + 401 刷新重试（复用 `oauth.refresh_token` 与 per-uid Lock）；选号范围 = `status == "normal"` 的账号；全部不可用 → 503 + Retry-After。
2. **进阶**（远期）：9router 连接级故障转移——失败按模型锁定该账号（`markAccountUnavailable`）→ exclude 换下一个；combo 降级编排（订阅→便宜→免费）与 cockpit 冷却分级（429 软冷却/402 硬冷却）更远，不进本期。
3. **活跃联动（顺带收益）**：聊天请求天然点亮 WorkBuddy 成长任务的行为事件（对齐 cockpit `/v2/report` 活跃上报语义）；连登/抽奖接口（②已留挂载点）届时再评估接入。

## 4. 细化定稿（2026-10-08：A 类产品决策 + B 类实测落证）

### 4.1 模型命名与路由：前缀体系（用户拍板 2026-10-08）

**统一规则：对外模型名 = `<供应商名>/<上游模型名>`**，所有 provider 一律加前缀，`/v1/models` 与请求 `model` 字段同一命名空间。

| 供应商 | 前缀 | 示例 | 依据 |
|---|---|---|---|
| 牛码（ta3） | **`牛码/`** | `牛码/glm-5.3` | 用户拍板；与 WorkBuddy/自定义供应商同构 |
| WorkBuddy | **`WorkBuddy/`** | `WorkBuddy/glm-5.3-flash` | 用户拍板（原推荐 `cbcn/` 9router 式短前缀被否决，理由：名称可读性优先） |
| 自定义供应商 | 用户填写的名称 | `基元律动/glm-5.3-flash`、`deepseek/deepseek-flash` | 用户拍板；名称来自供应商页 F5 的 `name` 字段 |

规则细节：

1. **解析**：`model` 按**第一个 `/`** 切分（前缀名允许中文，不复用 9router 的 `~`/`.` 分隔语法）；前缀大小写不敏感匹配（`workbuddy/` 也接受），但 `/v1/models` 返回规范大小写。
2. **冲突天然消解**：牛码 `/v1/models`（现目录）与 WorkBuddy 目录有同名模型（`glm-5.3`/`glm-5.3-flash`/`kimi-k3` 等），前缀化后不再冲突，去重逻辑仅需处理「同名供应商」（新建自定义供应商时校验 name 不与内置前缀重名，重名 400）。
3. **兼容**：不带前缀的裸模型名 → 按牛码处理（向后兼容现有 agent 配置，M2–M10 用户不改配置即用）；带未知前缀 → 404 `未知供应商: <prefix>`。**若该名同时存在于 WorkBuddy 目录**（未来裸名不再唯一时）以牛码优先（现状：裸名 = 牛码，零歧义）。
4. **白名单**：`model_whitelist` 存**带前缀全名**（含 `牛码/…`）。迁移：白名单里的旧裸名（无 `/`）读取时归一为 `牛码/<name>`（读时归一，不改存储；写时存全名）。`DISABLE_ALL` 语义不变。
5. **思考强度配置**（`thinking_defaults`，按模型名字典）：同样迁移，读时裸名归一为 `牛码/<name>`。
6. **日志/统计**：`chat_request` 落 `model` 全名；`/v1/logs/models`、`/v1/stats` 无需改动（天然按全名区分）。GUI 日志页/统计页展示可读的短名（去前缀展示，`牛码/` 可省略？——**否**，一律展示全名，避免歧义；这是既有页面的显示口径，不做特化）。
7. **上游模型名**：adapter 出站前去掉前缀（`payload.model = 裸名`），WorkBuddy 目录里 `auto`/`fast-model` 等别名模型照常可路由（`WorkBuddy/auto`）。

> 备选方案（否决）：① 原名透传 + 冲突去重（模型名不唯一 → 白名单/思考配置/统计全都要按 `(provider, name)` 复合键改造，改动面远大于前缀）；② 9router 式短前缀（`cbcn/`）——用户否决，可读性优先。

### 4.2 WorkBuddy 聊天契约（B 类实测，2026-10-08 小号只读探针）

探针脚本 [探针-wb聊天契约.py](探针-wb聊天契约.py)（只读、不刷账号；原始响应落系统临时目录，不入库）。实测账号为小号（AGENTS 硬性规则 5）。**结论：全部 8 项落证**：

| # | 项 | 实测结论 |
|---|---|---|
| B1 | **chat 出站档位** | **桌面档可行**（2api-panel 口径：UA `WorkBuddy/5.5.4 WorkBuddy/5.5.4 CLI/2.137.1` + 归属四头）→ 200 流式正常（83 帧/3.2s，含 thinking 333 字符）；**CLI 档（9router 口径）也 200 可用**。**定稿选桌面档**——与登录档位（`platform=workbuddy` 桌面客户端）身份一致，且 2api-panel 为当前最活跃反代实现 |
| B2 | **强制 stream** | 非流式 → `HTTP 400 {"code":11101,"msg":"Non-stream chat request is currently not supported"}`。**relay 侧对非流式入站做本地聚合**：内部强制 `stream:true` 转发，收流后聚合成 JSON（复用 `chat_with_empty_retry` 同款判定口径）；客户端无感 |
| B3 | **内容风控** | agent 身份句 system（"You are Claude Code, Anthropic's official CLI…"）→ `HTTP 400 {"code":11128,"msg":"Illegal API invocation from an unapproved channel"}`；**中性提示词替换后可过**（"You are a helpful AI assistant…" → 200；长 system 纯文本也过）。**口径**：命中 agent 身份模式（正则见 9router `codebuddy-cn.js`，含 `<agent-identity>`/`cc_entrypoint` 等）或 system >2000 字符 → 替换为中性句并原样保留字符串/块形状；**不重试**，替换后直接发（9router 同款「一次换、不重试」；若仍被拦 = 用户内容触发审核，如实透传错误） |
| B4 | **思考暴露与控制** | 上游响应：不传 effort 时**默认思考**（同题 670 帧/2184 thinking 字符）；`reasoning_effort:"none"` → **真关**（thinking=0）；`reasoning_effort:"off"` → **无效**（当作未知档位，仍思考 449 字符）；`low` → 生效（thinking=0 但内容变短）；`xhigh` 超出档位但 200（上游按未知档处理）。**口径**：`none` 用 `reasoning_effort:"none"`（不要用 `off`）；档位合法性表来自目录 `reasoning.supportedEfforts` |
| B5 | **工具透传** | `tools` + `tool_choice:"auto"` → 200，`finish_reason:"tool_calls"`，delta.tool_calls 按 index 分片（首片带 `id`/`function.name`，后续只带 `arguments` 片段）→ **上游支持工具调用，纯透传即可，M3 工具伪装不应用于 WorkBuddy 链路**（牛码才需要伪装） |
| B6 | **错误码族** | 已实测：`11101`（参数错/非流式）、`11128`（渠道风控）、`11102`（模型不可用，如 `kimi-k2-instruct-taiji` "model service info not found"）。**映射口径**：`11101`→400、`11128`→400（提示内容被拦）、`11102`→404（模型不存在）、`6004`（限流，带恢复时间文案）→429 + Retry-After（远期，本期先如实透传原文）；未知 code → 502 |
| B7 | **会话头族最小集** | 实测**仅** `CommonHeaders` + 归属四头 + `X-Machine-ID`/`X-Session-ID` 即 200（本探针未带 `X-Conversation-*`/B3 链路族）。**本期不做**会话头族（属 cockpit 后台聚合特性，非聊天必需）；留扩展点 |
| B8 | **usage/长流** | 上游**每流末尾带完整 usage**（`prompt_tokens`/`completion_tokens`/`total_tokens`/`completion_tokens_details.reasoning_tokens`/`completion_thinking_tokens`/`cache_*`）→ 直接映射 `Usage`（`reasoning_tokens` 取 `completion_tokens_details.reasoning_tokens`）；长流稳定性：探针最长 16.2s 无断流（长连接需正式 e2e 观察）；多账号并发设备指纹关联：本期用「每 uid 稳定派生 `X-Machine-ID`/`X-Session-ID`」（②已实现，`wbcp:` 前缀 ≠ 参考项目 `wb2a:` 前缀，避免与参考实现同值——多号互异、同号恒定） |

**模型目录（A3 定稿依据，实测）**：
- `GET /v3/config`（桌面档 UA 即过）→ `data.models[]`，**54 个**（含 `auto`/`fast-model`/`balanced-model`/`deep-model` 别名与 `glm-5.3`/`kimi-k3-1`/`glm-5.3-flash` 等）；字段丰富：`maxInputTokens`/`maxOutputTokens`/`reasoning.{supportedEfforts,defaultEffort,canDisableThinking,summary}`/`supportsToolCall`/`supportsImages`/`credits`/`tags`。
- `GET /console/enterprises/personal/models` → `data.models[]`（31 个）+ `data.agents[]`（`name:"cli"` 的 `models[]` 白名单）。
- **定稿口径**：两路并集（v3/config 为主，企业端点补缺；去重按 id，v3 优先——2api-panel 同口径）；**过滤非对话模型**：`maxOutputTokens ≤ 256` 或 `tags` 含 `text-to-image` 或 id 前缀 `nes-`/`completion-`/`codewise-`（实测剔除 `hunyuan-3b`/`completion-gf`/`codewise-*`/`hunyuan-image-alpha` 等）；**direct 别名照常保留**（`auto` 等实测 200 可用，`WorkBuddy/auto`）。
- 多账号目录合并：取**首个 normal 账号**的目录（不同账号套餐不同，目录可能有差异；不做并集——目录随选号账号自然刷新，记为已知边界）。

### 4.3 账号池选号策略（A2 定稿）

| 决策点 | 结论 |
|---|---|
| 参与轮转范围 | `status == "normal"` 的账号；无启用开关（②的账号模型无 enabled 字段，本期不加） |
| 选号算法 | **轮询**（`itertools` 式游标，进程内），并发安全（asyncio.Lock 保护游标） |
| 401/凭证失效 | 该号 `refresh_token` 刷新一次 → 重试同号一次；仍失败 → 标记该号本次请求失败，**换下一个号**（第一版：最多尝试全部 normal 号） |
| 全部不可用 | **503 + `Retry-After: 60`**（对齐 9router 语义；错误体 OpenAI 兼容 `{"error": {"type": "no_available_account"}}`） |
| 刷新失败处置 | 单号刷新失败**不**从池中删除（下次仍可试）；连续失败计数留远期（冷却分级） |
| 429/402 冷却 | **不进本期**（远期：cockpit `CoolSoft`/`CoolHard` 分级） |
| 空池（无 normal 号） | 503 + 提示「请到供应商页添加 WorkBuddy 账号」 |

### 4.4 服务门控与自定义供应商（A4 定稿）

| 决策点 | 结论 |
|---|---|
| `_serving()` 语义 | **改为「任一上游可用」**：`not _service_disabled and (牛码已登录 or WorkBuddy 账号池非空)`。纯 WorkBuddy 用户（无牛码账号）可用 `/v1`；M9 手动停止语义不变（停 = 全停） |
| 顶栏 | 供应商页已拍板「完全去牛码态」，本需求不动前端顶栏（服务开关按钮仍在，语义已中立） |
| 自定义供应商转发 | **本期一并接入**（供应商页 F5 明确依赖本需求 adapter 层）：`enabled=true` 的条目进 `/v1/models`（`<name>/<model>`，模型清单手填）+ `adapter_for` 命中即转发（OpenAI 兼容 baseurl + Bearer key + 默认实现，无钩子）；`enabled=false` 不出现。密钥不出 relay（管理 API 不回显口径不变） |
| 自定义供应商流式/非流式 | 默认实现按 `stream` 原样转发；上游 SSE 帧清洗（复用 9router「白名单重建」思路：只留 `id`/`object`/`created`/`model`/`choices[].delta|finish_reason`/`usage`） |

### 4.5 出口管道与横切

1. **WorkBuddy SSE → OpenAI 帧**：上游帧已近 OpenAI 形态（`choices[].delta.{content,reasoning_content,tool_calls,role}`），出口管道做**白名单重建**：保留 `id`/`model`/`created`/`choices[].{index,delta,finish_reason}`/`usage`；`delta` 只留 `role`/`content`/`reasoning_content`/`tool_calls`（`function_call`/`refusal`/`extra_fields` 等腾讯字段剥离——**不透上游内部字段给 agent**，AGENTS 硬性规则 2）；usage 逐字段映射（见 B8）。
2. **非流式**：内部强制 stream 收流聚合（B2），产出 `ChatResponse` 后走现有 `chat_response_to_openai`。
3. **工具横切**（9router `concerns/toolCall.js` 经验）：tool call id 清洗/补齐、缺失 tool response 修复、工具名映射还原抽公共模块 `relay/relay/tool_repair.py`（adapter 与 translator 共用，不散在各 adapter）；WorkBuddy 链路**不做**工具名伪装（B5：上游原生支持），仅做 id 规范化与多轮配对修复。
4. **空流防御**：WorkBuddy adapter 复用 BUG-001 修复后的判定口径（`EmptyStreamError` + 独立预算 + 耗尽 502）；WorkBuddy 上游若 200 后空流同样不假成功。
5. **401 刷新重试下沉**：`_sse_with_retry`/`_chat_with_retry` 的牛码特化（重跑 sync_models）改为 adapter 的 `refresh_credentials()` 钩子分派（Ta3 = sync_models；WorkBuddy = 账号池刷新；自定义供应商 = 无操作）。

### 4.6 实施步骤（commit 序列）

| # | commit | 内容 | 验证 |
|---|---|---|---|
| 1 | `feat: 模型名前缀路由与 adapter 注册表骨架` | `adapter_for()` 注册表 + 前缀解析/白名单与思考默认读时归一 + Ta3Provider 适配为注册表成员（牛码路由零行为变化） | pytest 全量零回归 + 新前缀单测 |
| 2 | `feat: WorkBuddy 聊天 adapter 与账号池轮转` | `platforms/workbuddy/chat.py`（桌面档头族 + payload 钩子：强制 stream、system 中性化、effort 归一）+ 轮询选号 + 401 换号 + 503 语义 | pytest（MockTransport） |
| 3 | `feat: WorkBuddy 模型目录与 /v1/models 合并` | 目录双端点并集 + 非对话过滤 + 前缀合并进 `/v1/models`（含白名单语义） | pytest + 小号真机 `/v1/models` |
| 4 | `feat: 自定义供应商转发生效` | OpenAICompatProvider 默认实现 + `adapter_for` 命中转发 + `/v1/models` 合并 | pytest（假上游）+ 真 baseurl 冒烟 |
| 5 | `feat: 服务门控任一上游可用` + 401 下沉 | `_serving()` 语义 + adapter `refresh_credentials` 钩子 | pytest（test_service_gate 扩展） |
| 6 | `feat: 供应商页模型区（WorkBuddy/自定义）` | WorkBuddy tab 模型区 = 目录列表 + 白名单启停 + 手动测试；自定义供应商模型区接线 | npm run format:check + build |
| 7 | `chore: 打包登记` | 两个 spec 的 hiddenimports（+ 产物冒烟发现的 502/503/全名单模型端点三处修复） | `py -3.13 build_sidecar.py` + 产物冒烟 |
| 8 | `feat: 三 provider e2e 冒烟脚本落地` | WorkBuddy 三协议真客户端（兼回填多协议 A 段）+ 牛码回归（登录态/模型列表/单模型端点，规则 5：对话待解封用小号）+ 自定义真转发 + 网络层错误 502 映射修复 | 冒烟 19/19 + 见 §5 |

## 5. 验收

- [x] 转发架构定稿（2026-10-07：adapter 注册表分层 + Ta3Provider 保留 + 9router executor 模式借鉴，见 §3）
- [x] 细化定稿（2026-10-08）：A1 前缀体系（用户拍板）/ A2 选号策略 / A3 目录与模型区 / A4 服务门控与自定义供应商；B1–B8 聊天契约小号实测全部落证（[探针-wb聊天契约.py](探针-wb聊天契约.py)）
- [x] 实现后补全（2026-10-09）：双 provider 路由正确性（adapter 注册表 + 前缀解析）/ 账号池轮转与 401 刷新下沉（`refresh_credentials` 钩子）/ 牛码回归（登录态 + 前缀目录 + 单模型端点；主账号封禁，对话待解封用小号补测）/ WorkBuddy 小号聊天冒烟 / 自定义供应商转发冒烟——pytest 367 passed + e2e 冒烟 19 项全绿
- [x] 打包产物冒烟（2026-10-09，[冒烟-三provider-e2e.py](冒烟-三provider-e2e.py)）：三 provider 模型列表（牛码 5 + WorkBuddy 45 合并）+ WorkBuddy 流式/非流式对话 + 工具透传 + 自定义供应商真转发——19/19 通过（隔离数据目录、真 exe 产物）

### 与多协议端点兼容的验收衔接（2026-10-08 补，2026-10-09 执行完毕）

本需求落地后**兼任** [多协议端点兼容](../多协议端点兼容-三协议入站统一/) **验收 A 段（入站层三协议 e2e）的上游**——该段不依赖牛码账号，用 WorkBuddy（或自定义供应商）当上游即可执行：

- [x] WorkBuddy 转发上线后，三种真客户端（Codex CLI `/v1/responses`、Claude Code `/v1/messages`、通用 OpenAI 客户端 `/v1/chat/completions`）各跑通一次对话，回填多协议需求验收 A 段（2026-10-09：Codex CLI 0.153.4 与 Claude Code 2.1.263 真客户端各一次「你好」；OpenAI SDK 3.26.1 流式+非流式各一次；另有脚本级 `/v1/responses`+`/v1/messages` 回归）
- 用 WorkBuddy 作 A 段上游的收益：不掺牛码工具伪装/指纹净化特化逻辑，入站层问题暴露更干净
- 分层判读（勿混淆）：`chat_error` 里是上游报错 → 出站/WorkBuddy 问题；`400 请求体不符合 xx 协议` → 入站层（`protocol_adapter.py`）问题

## 6. 已知边界

- 聊天域风控未知点：流式长连接指纹稳定性（探针最长 16.2s 未验长时间）、多账号并发对话的设备指纹关联（每 uid 稳定派生已缓解，未实测多号并发对话）
- WorkBuddy 目录随选号账号套餐差异（多账号不做并集，取首号；不同套餐可见模型可能不同）
- 非流式聚合延迟：客户端非流式请求需等上游完整流（WorkBuddy 强 stream），首字延迟高于牛码直连
- 白名单/思考配置的裸名兼容仅**读时归一**：GUI 写入新值存全名；存量 `relay_state.json` 不改写
- 前端「牛码」二字作为前缀在 `/v1/models` 中暴露：agent 侧可见（可接受，纯本地服务）；不含任何上游内部字段（硬性规则 2 边界 = 不泄漏 ta3/WorkBuddy 内部协议与 token，供应商名允许）
- 本期不做：账号冷却分级/模型级限流锁定/combo 降级（远期）、会话头族（B7 留扩展点）、Trae/Qoder 平台
