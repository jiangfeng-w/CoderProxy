# WorkBuddy 聊天反代与多平台聚合

> **状态**：转发架构已定稿（2026-10-07，基于 9router 转发架构深入调研）；WorkBuddy 聊天契约细节与账号池选号策略待②落地后细化定稿
> **优先级**：核心
> **来源**：用户最终愿景：对标 9router 的多平台账号聚合网关（牛码 + WorkBuddy 双上游聊天反代，对外统一 OpenAI 兼容 /v1），附带签到功能；2026-10-07 用户拍板转发架构路线——「入站做功夫，牛码单独用 Ta3Provider，其他借鉴 9router」
> **依赖**：WorkBuddy多账号登录与签到（账号池是该期前置底座）；与[多协议端点兼容](../多协议端点兼容-三协议入站统一/)共享入站归一层（正交，可分别实施）
> **隐私声明**：本文档仅含公开开源项目信息，不含任何真实账号、凭证、token。

## 1. 背景 / 目标

CoderProxy 最终形态 = 多平台 AI 账号聚合网关：agent 只见 OpenAI 兼容 `/v1`，relay 内部把请求路由到牛码或 WorkBuddy（按模型名），多 WorkBuddy 账号池轮转。对标 9router（`decolua/9router`，本地克隆 `d:\Code\ref-projects\9router`），差异点是 CoderProxy 自带伪装登录与签到，且不暴露任何上游内部协议。

三阶段总览：

| 阶段 | 内容 | 状态 |
|---|---|---|
| ① 牛码反代 | ta3 登录 + /v1/chat/completions 转发 | 已实现 |
| ② WorkBuddy 账号层 | 多账号登录 + 签到 + 配额 | 见「WorkBuddy多账号登录与签到」 |
| ③ WorkBuddy 聊天反代（本需求） | 聊天转发 + 多账号聚合路由 + 账号池轮转 | 规划中 |

## 2. 参考项目（本地克隆于 `d:\Code\ref-projects\`）

| 项目 | 借鉴什么 | 关键位置 |
|---|---|---|
| **workbuddy-cockpit**（Go, MIT） | **聊天契约主参考**：WorkBuddy 插件聊天协议转换、chat 域指纹头族、账号池选号与冷却分级 | `internal/upstream/`（client.go/headers.go）、`internal/pool/` |
| **workbuddy2api-panel**（Go, MIT） | OpenAI → WorkBuddy 请求转换的同源印证（两者本身即 WorkBuddy 聊天反代，可行性已验证） | `internal/` |
| **9router**（Node） | **聚合架构主参考**（2026-10-07 深入调研，见 §3）：executor/translator 正交分层、DefaultExecutor + 薄覆写、provider/connection 分离、连接级故障转移 | `open-sse/executors/`（`base.js`、`codebuddy-cn.js`、`default.js`）、`open-sse/translator/`、`src/sse/handlers/chat.js`、`src/sse/services/auth.js` |
| **cockpit-tools**（Rust, MIT） | 配额接口（选号依据）、活跃上报语义 | `crates/cockpit-core/src/modules/` |

## 3. 转发架构（2026-10-07 定稿）

### 3.1 为什么不把 Ta3Provider 塞进 9router 式 executor

9router 是 Node、Ta3Provider 是 Python，代码级复用不存在。架构层面也不该硬塞：9router 的 executor 只管传输（`buildUrl`/`buildHeaders`/重试），**协议翻译全部在 translator 层**；而 Ta3Provider 是「协议翻译 + 请求指纹 + 流解析」三合一（vendored 资产，拆它风险大收益零）。定稿路线（用户拍板）：**入站做功夫，牛码单独用 Ta3Provider，其他借鉴 9router**。

### 3.2 分层设计

```
入站协议层   /v1/chat/completions（现有）+ /v1/responses、/v1/messages
             （见[多协议端点兼容](../多协议端点兼容-三协议入站统一/)定稿——入站识别 + 转换器归一到 ChatRequest，
             鉴权头 Bearer / x-api-key 兼容提取）
      ↓
路由层       adapter_for(model)：模型目录条目已带 provider 字段（build_provider 在用），
             按它查注册表选 adapter——"ta3" → Ta3Provider；"workbuddy" → WorkBuddy 适配；
             查表 miss（自定义供应商）→ 默认 OpenAICompatProvider + 纯配置
      ↓
出站 adapter 层（借鉴 9router 三层经验）
             - Ta3Provider：原样保留（接口 chat/stream_structured 不动）
             - OpenAICompatProvider：新建默认实现，只管传输（URL/头/auth/retry）
             - WorkBuddy 等：不写独立类——对齐 9router `codebuddy-cn.js` 仅 96 行的模式，
               继承默认实现 + 两个小钩子（transform_request 改请求怪癖、parse_error 认特殊错误码）
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
| BUG-001 空流假成功 | [已知问题](../../已知问题.md)已定位根因与修复方向 | 独立缺陷，随时可修，不依赖本架构 |
| 上游错误非结构化 | vendored ta3.py 抛 `RuntimeError("模型请求失败 {status}：{body}")` 文本，`routes.py` 被迫正则 `_UPSTREAM_ERR_RE` 反解析 | 随 adapter 抽象一起做（ta3.py 加结构化异常，加「本地修订」注释） |
| 401 刷新重试下沉 | `_sse_with_retry`/`_chat_with_retry` 耦合牛码语义（重跑 sync_models） | 随 adapter 抽象下沉到各 provider 实现 |

原则：`chat`/`stream_structured` **签名保持稳定**（oai_adapter/routes/tests 都依赖）；接口定型后内部实现（`_parse_anthropic_frame` 等）可安全重构——adapter 接口 + 回归测试是护栏。

### 3.5 账号池轮转与故障转移（③期实现，节奏分层）

1. **第一版**：简单轮询选号 + 401 刷新重试（workbuddy-cockpit `RefreshToken` 锁内改写 + 调用方 SaveAtomic 语义）。
2. **进阶**（远期）：9router 连接级故障转移——失败按模型锁定该账号（`markAccountUnavailable`）→ exclude 换下一个 → 全部不可用 503 + Retry-After；combo 降级编排（订阅→便宜→免费）与 cockpit 冷却分级（429 软冷却/402 硬冷却）更远，不进③第一期。
3. **活跃联动（顺带收益）**：聊天请求天然点亮 WorkBuddy 成长任务的行为事件（对齐 cockpit `/v2/report` 活跃上报语义）；连登/抽奖接口（②已留挂载点）届时再评估接入。

## 4. 落地要点（③实施时的既有决策）

1. **聊天 provider 位置**：`relay/relay/platforms/workbuddy/chat.py`（自研模块；**不写进 `relay/app/models/providers/`**——该树是 ta3 vendored 拷贝，平台代码不混入）；出站形态 = OpenAICompatProvider + WorkBuddy 配置/钩子（WorkBuddy 出站为 OpenAI 兼容端点 `copilot.tencent.com/v2/chat/completions`，9router 实测强制 stream + 腾讯内容风控怪癖——全部表达为配置与 transform_request/parse_error 钩子）
2. **路由改造**：`build_provider()`（现硬编码 Ta3Provider）改为 `adapter_for(model)` 注册表查询；`/v1/models` 合并多 provider 模型目录（模型名冲突去重；前缀改写 vs 原名透传待定稿时决策）
3. **凭证衔接**：WorkBuddy 聊天凭证从②的账号池获取（复用 ensure_token/refresh，不另存）——②与③最大衔接点，②存储按多账号数组设计即为此
4. **工具调用横切**（9router `concerns/toolCall.js` 经验）：tool call id 清洗/补齐、缺失 tool response 修复、工具名映射还原抽公共模块，adapter 与 translator 共用，**不散在各 adapter 里**
5. **已知边界**：入口仍只暴露 OpenAI 兼容端点（AGENTS 硬性规则 2 不破），WorkBuddy 内部协议不泄漏给 agent；③首次改动 `/v1/chat/completions` 主链路，牛码回归风险升级——验收必须含完整 ta3 e2e；聊天域风控未知点（流式长连接指纹稳定性、多账号并发对话的设备指纹关联）待小号验证

## 5. 验收

- [x] 转发架构定稿（2026-10-07：adapter 注册表分层 + Ta3Provider 保留 + 9router executor 模式借鉴，见 §3）
- [ ] 待②落地后细化定稿：WorkBuddy 聊天契约细节（指纹头族/风控怪癖实测）、账号池选号策略、模型目录合并策略（前缀改写 vs 原名透传）
- [ ] 实现后补全：双 provider 路由正确性（adapter_for 注册表）/ 账号池轮转与 401 刷新下沉 / ta3 完整回归（登录/模型列表/一次完整对话）/ 小号聊天冒烟
