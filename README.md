# CoderProxy · 牛码中转

把 Ta+3「牛码」（银海）官方模型能力，以 **OpenAI 兼容 API** 暴露给任意第三方 agent 的本地桌面中转服务。

```
你的 agent (Trae/Cursor/Continue/opencode/cline)
        │  OpenAI /v1 + Bearer <中转静态key>
        ▼
  ┌────────── CoderProxy ──────────┐
  │ Tauri(Rust)+Vue3  GUI ── spawn ──► │
  │ Python FastAPI relay (vendored ta3)│
  │   · 伪装牛码登录(PKCE-SM3 / IM)     │
  │   · 请求指纹伪装(Electron UA等)      │
  │   · 工具名双向伪装(hybrid/strict)    │
  └──────────┬─────────────────────────┘
             │ 伪装成牛码官方客户端
             ▼
   https://lc.yinhaiyun.com/newcoder
```

## 用它做什么

1. 启动应用，在 GUI 里「登录牛码」一次（伪装成官方桌面端拿 token）。
2. 在任意 agent 的自定义供应商里填：
   - `Base URL`: `http://127.0.0.1:8786/v1`
   - `API Key`: GUI 里显示/复制的静态 key
3. 开聊。你的请求会被伪装成牛码官方客户端转发，流式返回（支持思考/工具调用）。

## 现状

| 项 | 状态 |
|---|---|
| 方案/路线 | ✅ `docs/CoderProxy-开发计划-2026-09-03.md`（总计划）＋ `docs/spec/`（每里程碑一篇，均本地备忘不入仓库）
| 实现 | ⛔ 未开始，按 M1 探针 → M2 relay → M3 工具伪装 → M4 Tauri 壳 → M5 打包 推进 |

> 开发/实现约定见 `AGENTS.md`（本地备忘，不入仓库）。

## 风险提示

逆向 + 伪装登录属于**非官方客户端行为**，牛码可能风控**封号**。请使用小号验证，责任自负。详见 `docs/CoderProxy-开发计划-2026-09-03.md` §10（本地备忘）。

## 开发

- Relay（Python）：`cd relay && pip install -e . && python -m relay run`
- GUI（Tauri+Vue3）：`src-tauri/` + `frontend/`

## 目录

```
src-tauri/        Rust 壳（spawn sidecar / 配置）
frontend/         Vue3 前端（登录 / 配置 / 模型 / 日志）
relay/            Python FastAPI 中转（vendored ta3 伪装/登录库）
docs/             CoderProxy-开发计划 + spec 需求文档（本地备忘，不入仓库）
AGENTS.md         开发规则（本地备忘）
```