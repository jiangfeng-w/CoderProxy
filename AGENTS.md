# Agents 规则

CoderProxy = 牛码中转：复用 chatcoder 逆向出的「伪装牛码登录 + 请求指纹」，以 **OpenAI 兼容 /v1** 暴露给任意 agent 的本地中转服务。

**开发文档全在** **`docs/`。开始时先读** **`docs/README.md`（索引 + 自举环境）。**

## 硬性规则（必守，违反即视为返工）

1. `relay/app/**` 是 vendored 自 chatcoder 的代码，尽量不改；要改回**上游 chatcoder 仓库**改再同步（上游路径等机器相关细节见 `docs/开发规则.md`）。
2. 对外只暴露 OpenAI `/v1/chat/completions` 与 `/v1/models`，禁止把 ta3/Anthropic 内部泄漏给 agent。
3. 本地鉴权用静态 `RELAY_API_KEY`（Bearer）；**真实牛码 token 永不下发 agent**，只在 relay 内部持有。
4. 工具映射改动必须双向（出站 schema/历史 + 入站 restore），否则破坏工具调用。
5. 端到端冒烟**只用小号**，严禁在真实牛码账号反复触发。
6. 前端写页面**优先用 Naive UI 的现成组件**（现有惯例：从 `naive-ui` 具名导入，如 `NButton`、`NSelect`、`NModal`），不重复造轮子、不自造样式。

## 技术栈

- **frontend/**（GUI）：Vue 3 + TypeScript + Vite；UI 组件库 **Naive UI**，图表 ECharts；桌面能力走 `@tauri-apps/api`；代码格式由 Prettier 统一（配置 `frontend/.prettierrc`，用 `npm run format` / `format:check`）。

- **src-tauri/**（桌面壳）：Rust + Tauri 2（tray-icon / single-instance / shell / opener 插件）。

- **relay/**（中转服务）：Python ≥3.10 + FastAPI + uvicorn + httpx + Pydantic v2；测试 pytest + pytest-asyncio；代码规范 ruff（line-length 100）。

## 提交信息规范（commit message）

- **主标题（subject）单行概括**：`type: 概述`（如 `feat:` / `fix:` / `chore:`），中文可适度长，但**不要把明细全塞进标题**。

- **副信息（body）：主标题下空一行，随后逐条 `- ` bullet 明细（条目之间不空行）**，写清「改了什么 / 为什么」。

- **结构示例**（自行构造，不引用仓库真实 commit）：主标题 `feat: <一句话概述>` → 空一行 → 逐条 `- <改了什么/为什么>` bullet 明细。

## 入口

- 总方案：`docs/CoderProxy-开发计划-2026-09-03.md`

- 里程碑需求：`docs/spec/`（README 为总览表；每期一个子目录，需求文档 + 该期支撑脚本）

- 非硬性开发规则 / 常用命令 / 对接指针：`docs/开发规则.md`

- 环境自举：`docs/README.md`「自举环境」

