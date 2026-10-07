# WorkBuddy 聊天反代与多平台聚合

> **状态**：规划中（2026-10-07 立项；方案细节待「WorkBuddy多账号登录与签到」落地验证后单独定稿）
> **优先级**：核心
> **来源**：用户最终愿景：对标 9router 的多平台账号聚合网关（牛码 + WorkBuddy 双上游聊天反代，对外统一 OpenAI 兼容 /v1），附带签到功能
> **依赖**：WorkBuddy多账号登录与签到（账号池是该期前置底座）
> **隐私声明**：本文档仅含公开开源项目信息，不含任何真实账号、凭证、token。

## 背景 / 目标

CoderProxy 最终形态 = 多平台 AI 账号聚合网关：agent 只见 OpenAI 兼容 `/v1`，relay 内部把请求路由到牛码或 WorkBuddy（按模型名），多 WorkBuddy 账号池轮转。对标 9router（`decolua/9router`，本地克隆 `d:\Code\ref-projects\9router`），差异点是 CoderProxy 自带伪装登录与签到，且不暴露任何上游内部协议。

三阶段总览：

| 阶段 | 内容 | 状态 |
|---|---|---|
| ① 牛码反代 | ta3 登录 + /v1/chat/completions 转发 | 已实现 |
| ② WorkBuddy 账号层 | 多账号登录 + 签到 + 配额 | 见「WorkBuddy多账号登录与签到」 |
| ③ WorkBuddy 聊天反代（本需求） | 聊天转发 + 多账号聚合路由 + 账号池轮转 | 规划中 |

## 参考项目（本地克隆于 `d:\Code\ref-projects\`）

| 项目 | 借鉴什么 | 关键位置 |
|---|---|---|
| **workbuddy-cockpit**（Go, MIT） | **聊天契约主参考**：WorkBuddy 插件聊天协议转换、chat 域指纹头族、账号池选号与冷却分级 | `internal/upstream/`（client.go/headers.go）、`internal/pool/` |
| **workbuddy2api-panel**（Go, MIT） | OpenAI → WorkBuddy 请求转换的同源印证（两者本身即 WorkBuddy 聊天反代，可行性已验证） | `internal/` |
| **9router**（Node） | **聚合架构主参考**：模型命名空间路由（`kr/claude-*` 前缀分 provider）、combo 三层降级编排（订阅→便宜→免费）、多账号轮转 | `open-sse/executors/`、`open-sse/combo/`、`open-sse/config/providers.js` |
| **cockpit-tools**（Rust, MIT） | 配额接口（选号依据）、活跃上报语义 | `crates/cockpit-core/src/modules/` |

## 设计方向（概要，待②落地后定稿）

1. **聊天 provider 新增**：`relay/relay/platforms/workbuddy/chat.py`（与 ta3 provider 隔离的**自研模块**；**不写进 `relay/app/models/providers/`**——该树是 ta3 vendored 拷贝，平台代码不混入，对齐「WorkBuddy多账号登录与签到」核心决策表；OpenAI 兼容 ↔ WorkBuddy 插件聊天协议转换，SSE 流式）。凭证从②的 `platforms/workbuddy` 账号池获取（复用 ensure_token/refresh，不另存）——②与③最大衔接点，②存储按多账号数组设计即为此
2. **路由层改造**：`relay/relay/routes.py` 的 `build_provider()`（L82-108，现硬编码 Ta3Provider、`provider` 仅为显示字段）改为**按模型名前缀路由**：`wb/*` → WorkBuddyProvider，其余 → Ta3Provider（对齐 9router 模型命名空间）；`/v1/models` 合并两 provider 模型目录（模型名冲突去重）
3. **账号池轮转**：初期简单轮询选号 + 401 时刷新重试；进阶对齐 9router combo 降级编排（配额耗尽自动切下一账号/平台）+ cockpit 冷却分级（429 软冷却/402 硬冷却）——远期，不进③第一期
4. **活跃联动（顺带收益）**：聊天请求天然点亮 WorkBuddy 成长任务的行为事件（对齐 cockpit `/v2/report` 活跃上报语义）；连登/抽奖接口（②已留挂载点）届时再评估接入

## 已知边界（定稿前风险预告）

- 入口仍只暴露 OpenAI 兼容端点（AGENTS 硬性规则 2 不破），WorkBuddy 内部协议不泄漏给 agent
- ③将首次改动 `/v1/chat/completions` 主链路（build_provider 路由化），牛码回归风险升级——验收必须含完整 ta3 e2e（登录/模型列表/一次完整对话）
- 聊天域风控未知点：流式长连接的指纹稳定性、多账号并发对话的设备指纹关联（X-Machine-ID 派生策略②已定，聊天域是否需按会话变化待小号验证）
- 模型目录合并策略（前缀改写 vs 原名透传）待定稿时决策

## 验收（待方案定稿后补全）

- [ ] 方案定稿（含 build_provider 改造设计、聊天契约确认、账号池选号策略）
- [ ] 实现后补全：双 provider 路由正确性 / 账号池轮转与 401 刷新 / ta3 完整回归 / 小号聊天冒烟
