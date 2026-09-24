# docs 开发文档索引

CoderProxy 的开发文档**全部在 `docs/`**。新会话 / 新接手者的入口就是本文件。先读「自举环境」，再按「读什么」进具体文档。

## 读什么

| 文件 | 内容 |
|---|---|
| `CoderProxy-开发计划-2026-09-03.md` | **总方案**（必读）：架构 / 调研结论 / M1–M5 里程碑 / 风险合规 / 待拍板开放项 |
| `spec/` | 每里程碑一个子目录：需求文档 + 该期支撑脚本。`spec/README.md` 是总览表 |
| `开发规则.md` | 非硬性开发规则 / 常用命令 / 对接指针（硬性规则见根 AGENTS.md） |
| `上游风控与客户端兼容.md` | 牛码风控指纹封禁实测（竞品身份句黑名单）+ 客户端协议兼容 + 错误透传 + 排查复测方法 |
| `已知问题.md` | 缺陷/隐患台账：已定位但未修复的问题（现象/根因/证据/修复方向/验收） |
| 本文件 | `docs/` 索引 + 自举环境 |

## 自举环境（新会话必读，本机已实测）

- **Python 依赖**：本机 `python` 是 shim，**缺 venv 模块**，全局**没有 httpx/sqlalchemy**。装依赖用
  `python -m pip install --target <本地目录> 依赖名`（或自建 venv），跑脚本时把该目录加进 `sys.path`。
- **直连**：本机 httpx 默认会读系统代理，把 `lc.yinhaiyun.com` 走隧道 → **连不上**。所有请求必须 **`trust_env=False`**（详见总计划 §3.4 #3）。
- **打包 sidecar**：本机 `py` 是 Python 3.13（带 pip、会读 PYTHONPATH），`python` 是 3.12 Codex shim（无 pip / 无 PyInstaller / 不读 PYTHONPATH）。**打 sidecar 必须 `py -3.13` + `PYTHONPATH=relay/_deps` 跑 `build_sidecar.py`**，不要用 `python` 那个 shim（详见 `开发规则.md`「构建与打包」）。
- **登录**：本机有银海通 IM 时走 `:13631/getuid` 静默秒登；否则浏览器 PKCE-SM3。聊天用**模型自带 `llm-` key**（非 oauth token）；目录端点为 `POST /ai/continue/ide/list-assistants?appId=personal`（细节见总计划 §3.4，可跑脚本见 `spec/M1-登录与网关探针/探针-登录与伪装冒烟.md`）。
- **验证用小号**：避免触发牛码风控封号（总计划 §10 / AGENTS 硬性规则 5）。
- **写盘限制**：目标 `D:\Code\home\own-project\CoderProxy` 常不在 IDE 工作区，改文件经工作区暂存再 copy（见 `开发规则.md`）。

## 下一步

按总计划 §9 里程碑顺序推进。M1–M9 已完成（M8 于 2026-09-04 落地 Token 统计页：`/v1/stats` 按 模型/天/小时/类型 聚合 token 与请求数，前端新增「统计」导航页，用 ECharts 做折线/柱状图；M9 于 2026-09-04 落地「对 agent 的 /v1 服务软停开关」）。当前进入 **M10 设置页与侧边栏登录（数据目录查看/修改）**，需求见 `docs/spec/M10-设置页与侧边栏登录/`。