# WorkBuddy 多账号登录与签到

> **状态**：方案已定稿（2026-10-07，由 2026-10-04 三方可行性分析 + 2026-10-06 实施计划定稿转化）
> **优先级**：核心
> **来源**：多平台扩展调研（WorkBuddy/Trae/Qoder 可行性分析 → 用户决策：本期只做 WorkBuddy，Trae/Qoder 留扩展位）
> **隐私声明**：本文档仅含公开开源项目信息与占位符示例，不含任何真实账号、凭证、token。

## 背景 / 目标

CoderProxy 目前只支持牛码（ta3）单 provider 的登录与聊天转发。本期扩展第一个「账号型平台」**WorkBuddy/CodeBuddy CN**（腾讯，`copilot.tencent.com`）：多账号登录、手动签到、配额查询。最终愿景是 9router 形态的多平台账号聚合网关（聊天反代见后续需求「WorkBuddy聊天反代与多平台聚合」），本期交付的账号池是那期的前置底座。

**用户决策**：
- 多账号池（任意数量账号，每号独立凭证/签到/配额）
- 仅手动签到（前端按钮触发，无后台调度）
- 不做连登/抽奖（代码留扩展点）
- **与牛码零冲突**：`/v1/auth/*`、`/v1/chat/completions`、`relay_state.json` 的 `ta3_auth` 全不动

## 参考项目（本地克隆于 `d:\Code\ref-projects\`，新会话可直接读取）

| 项目 | 许可 | 借鉴什么 | 关键文件 |
|---|---|---|---|
| **cockpit-tools**（jlcodes99，Rust） | MIT | **主参考**：登录/签到/配额 API 契约（三方交叉验证最权威） | `crates/cockpit-core/src/modules/codebuddy_cn_oauth.rs`（登录 L173-437、refresh L527-571、签到宽松解析 L1401-1706） |
| **workbuddy-cockpit**（Arimayuki03，Go） | MIT | 签到/指纹/连登管家（后续扩展）；`X-CodeBuddy-Request` 等头族 | `internal/upstream/headers.go`、`internal/upstream/tasks.go` |
| **workbuddy-switch**（changexbc，Rust） | MIT | 契约交叉验证（OAuth 三端点、签到幂等码） | `src-tauri/src/oauth.rs`、`checkin.rs` |
| **workbuddy2api-panel**（linguo2625469，Go） | MIT | 后端能力同源参考（签到/连登/抽奖/保活） | `internal/` |
| **9router**（decolua，Node） | — | 阶段③主参考；本期仅 provider 插件式组织印证 | `open-sse/executors/` |
| **WorkDaddy**（babygoton，Node） | **AGPL-3.0** | **只借鉴思路，禁止抄任何代码**（CDP 注入路线，与本项目纯 HTTP 路线不同） | — |

## 设计方案

### 核心决策

| 决策 | 结论 |
|---|---|
| 新代码位置 | `relay/relay/platforms/`（relay 原生服务包子包；`relay/app/**` 是 ta3 vendored 拷贝不混入） |
| 路由命名空间 | `/v1/platforms/{platform}/*`（平台名是路径参数，trae/qoder 平行加入零改主干） |
| 存储文件 | 独立 `platform_workbuddy.json`（accounts[]，与 relay_state.json 完全隔离） |
| 抽象 | `PlatformAdapter` Protocol（typing.Protocol 非 ABC，只约束账号型平台，ta3 不纳入） |
| 登录模式 | 设备授权（无 PKCE、无本地回调 server）：start → 前端开浏览器 → relay 后台 asyncio task 轮询 → status 查询（对齐 ta3 in-flight task 模式，升级为 login_id 多槽并存） |
| 鉴权 | 新端点全部 `Depends(require_api_key)`（对齐 M6+ 管理端点惯例） |
| 测试 mock | httpx.MockTransport + monkeypatch（不引 respx，对齐现有零依赖测试模式） |

### 新增文件

```
relay/relay/platforms/
├── __init__.py      # 注册表 PLATFORMS = {"workbuddy": WorkBuddyAdapter()}；get_platform()
├── base.py          # PlatformAdapter Protocol + PlatformInfo/AccountView/CheckinResult/QuotaSnapshot + PlatformAuthError
├── store.py         # 通用多账号 JSON 存储（load/save/find/upsert/delete_account，按 platform_id 分文件）
├── routes.py        # APIRouter /v1/platforms/*
└── workbuddy/
    ├── __init__.py
    ├── client.py    # 指纹头组装 + {code,msg,data} 信封解析 + trust_env=False 直连 + _new_client 测试注入点
    ├── oauth.py     # 设备授权流：start_login / _poll_token(600s 超时) / login_status / cancel_login / refresh_token
    ├── billing.py   # get_checkin_status(宽松解析) / perform_checkin(幂等) / fetch_quota(多形态解析)
    └── adapter.py   # WorkBuddyAdapter：编排 oauth/billing/store；ensure_token(per-uid Lock 双检)

frontend/src/views/WorkBuddy.vue       # 账号卡片列表 + 添加账号弹窗（授权 URL + 轮询等待）；挂接「供应商页」WorkBuddy 分节（见「供应商页-登录与多平台账号入口」；若该页未先落地，以独立页+nav 项过渡，落成后迁入）
frontend/src/stores/workbuddy.ts       # Pinia store（对齐 stores/config.ts 拆分先例）
```

### WorkBuddy API 契约（三方逆向交叉验证，client.py 内常量化）

- base `https://copilot.tencent.com`（`WB_API_BASE`），web 域 `https://www.codebuddy.cn`（`WB_WEB_BASE`，refresh 用）；均入 config.py 新增 `wb_*` 配置项（env 覆盖，独立于 ta3_user_agent）
- **登录**：`POST {base}/v2/plugin/auth/state?platform=CLI` → state+authUrl；轮询 `GET {base}/v2/plugin/auth/token?state=`；`GET {base}/v2/plugin/login/account?state=` 取 uid/nickname/domain
- **刷新**：`POST {WB_WEB_BASE}/v2/plugin/auth/token/refresh`（`X-Refresh-Token` 头；该头**只允许出现在 refresh 请求**）
- **签到**：状态 `POST {base}/v2/billing/meter/checkin-activity-status`（失败回退 `checkin-status`）；执行 `POST {base}/v2/billing/meter/daily-checkin` body `{}`；已签到判定 = code 10001/14001 **或** msg 含「已签到」/「already」→ 幂等成功
- **配额**：`POST {base}/v2/billing/meter/get-user-resource`（body 含 PageNumber/PageSize/ProductCode:"p_tcaca"/Status:[0,3]/PackageEndTimeRange...），响应按 `data/resources` / `data/data/resources` / `Response/Data/Accounts` 多形态找数组，聚合 remaining/used/total/expire_at
- **指纹**（billing 域必带）：UA `WorkBuddy/<ver>`、Origin/Referer `https://www.codebuddy.cn`、`X-CodeBuddy-Request: 1`、`Accept-Language: zh-CN`、`Authorization`、`X-User-Id`、`X-Domain`、`X-Machine-ID`/`X-Session-ID`（`sha256("wbcp:{purpose}:{uid}")` 截 36 hex 稳定派生，防多号设备指纹关联）
- **指纹**（plugin 域）：额外 `X-No-Authorization/X-No-User-Id/X-No-Enterprise-Id/X-No-Department-Info: true` 无值声明头族
- 签到状态宽松解析照抄 cockpit-tools `parse_checkin_status_data`（snake/camel 双写、bool 兼容 0/1/"true"）

### 端点清单（platforms/routes.py）

| 方法/路径 | 职责 |
|---|---|
| GET `/v1/platforms` | 平台注册表（id/display_name/supports_checkin/supports_quota） |
| GET `/v1/platforms/{p}/accounts` | 账号列表（AccountView，**响应不含任何 token**） |
| POST `/v1/platforms/{p}/login/start` | `{login_id, authorize_url, expires_in:600}` |
| GET `/v1/platforms/{p}/login/status?login_id=` | pending/success/failed |
| POST `/v1/platforms/{p}/login/cancel` | 取消轮询 task |
| DELETE `/v1/platforms/{p}/accounts/{uid}` | 删除账号 |
| POST `/v1/platforms/{p}/accounts/{uid}/checkin` | 手动签到（返回 CheckinResult + 更新后 AccountView） |
| GET `/v1/platforms/{p}/accounts/{uid}/checkin-status` | 拉上游刷新缓存 |
| POST `/v1/platforms/{p}/accounts/{uid}/quota` | 刷新配额 |

错误映射：`PlatformAuthError(login_required)` → 401 `{code:"login_required"}`（前端引导重登）；`PlatformUpstreamError` → 502；`httpx.HTTPError` → 504。未知 platform → 404。

### 存储结构（platform_workbuddy.json，示例均为占位符）

```json
{"version": 1, "accounts": [{"uid": "u_xxxx", "nickname": "占位昵称", "email": "", "enterprise_id": "", "domain": "",
  "access_token": "<敏感，仅本地存储>", "refresh_token": "<敏感>", "expires_at": 0,
  "status": "normal", "checkin": {"last_checkin_date": "YYYY-MM-DD", "streak_days": 0, "today_checked_in": false},
  "quota": {"total": 0, "remaining": 0, "used": 0, "expire_at": "", "fetched_at": ""}}]}
```

- upsert 按 uid 去重；重登录覆盖 token 但**保留旧 checkin/quota 缓存**
- `today_checked_in` 读取时按 Asia/Shanghai 本地日期失效（`last_checkin_date != 今天` → 未签到）
- 落盘纪律照抄 storage.py（双锁 + `asyncio.to_thread` + tmp.replace 原子写）但**独立锁实例**
- `expires_at` 秒/毫秒双口径归一（<1e11 视为秒 ×1000）

### 关键复用（现有代码）

- 落盘纪律：`relay/relay/storage.py`（照抄模式不复用函数体）
- in-flight 登录 task 模式：`relay/app/auth/ta3/oauth.py` L114-208
- per-uid 刷新锁（防 stampede）：`relay/app/auth/ta3/session.py` `_refresh_locks`
- 前端 `relay()` 封装已支持 GET/POST/DELETE（`frontend/src/api.ts`）；`openAuthorizeUrl` 复用

### 修改现有文件（最小侵入）

- `relay/relay/routes.py`：+2 行挂载平台 router
- `relay/relay/config.py`：+`wb_api_base/wb_web_base/wb_user_agent/wb_client_version` 配置项
- `relay/relay-onefile.spec` **和** `relay/relay.spec`：hiddenimports 加 `relay.platforms`、`relay.platforms.workbuddy`（build_sidecar.py 默认构建 onefile；有漏登记踩坑史，双保险）
- `frontend/src/api.ts`：尾部追加 /v1/platforms 分节（listWbAccounts/wbLoginStart/wbLoginStatus/wbLoginCancel/wbDeleteAccount/wbCheckin/wbCheckinStatus/wbRefreshQuota + WbAccount 等类型）
- `frontend/src/Shell.vue`：views 注册表 + nav 图标
- **不动**：`/v1/auth/*` 路由、storage.py、ta3 全部、Rust 壳（lib.rs 只按 `/v1/` 前缀放行，新端点天然通过）

### 实施步骤（commit 序列）

| # | commit | 验证 |
|---|---|---|
| 1 | `feat: 平台插件底座与多账号存储`（platforms/{__init__,base,store}.py + test_platform_store.py） | pytest test_platform_store |
| 2 | `feat: WorkBuddy HTTP 客户端与请求指纹`（client.py + wb_* 配置 + test_wb_client.py） | pytest；ruff check |
| 3 | `feat: WorkBuddy 设备授权登录流`（oauth.py + adapter.ensure_token/refresh + test_wb_oauth.py） | pytest（MockTransport 全序列） |
| 4 | `feat: WorkBuddy 签到与配额查询`（billing.py + adapter 编排 + test_wb_billing.py） | pytest |
| 5 | `feat: /v1/platforms 管理端点`（routes.py + 挂载 + test_wb_routes.py） | pytest 全量 + 源码跑 relay 后 curl |
| 6 | `feat: 前端 WorkBuddy 账号页`（WorkBuddy.vue + stores/workbuddy.ts + api.ts + Shell.vue；**挂接「供应商页」分节**——见 [供应商页](../供应商页-登录与多平台账号入口/)，后端步骤 1–5 不受影响可先行） | npm run format:check、npm run build、dev 预览 |
| 7 | `chore: 打包登记 platforms 模块`（两个 spec） | `py -3.13 build_sidecar.py` + 产物冒烟 |
| 8 | 手动小号 e2e：登录→签到→配额→删除；**ta3 回归**（/v1/auth/login/start、/v1/models、一次 /v1/chat/completions） | 硬性规则 5：只用小号 |

## 已知边界

- 指纹遗漏 = 403：三域（plugin/billing/refresh）头族见契约节；所有 httpx 客户端 `trust_env=False`（系统代理劫持 copilot.tencent.com 会 403）
- refresh 端点双口径：契约说 `www.codebuddy.cn`，Go 参考用 `copilot.tencent.com`——两个配置项兜底，小号冒烟确认后定默认值
- 轮询 task 泄漏：600s 强制超时 + 完成即弹出 + status 查询顺带清理
- 已签到误报：code + msg 双判定（见契约节）
- 与 ta3 冲突面检查（每步回归）：ta3_auth 键、/v1/auth/*、settings.ta3_user_agent 独立、storage 锁互不干扰
- 本期不做：连登/抽奖（billing.py 按职责拆分留挂载点）、后台定时签到、Trae/Qoder 平台、聊天转发（见「WorkBuddy聊天反代与多平台聚合」需求）

## 验收

- [ ] `pytest tests/ -x -q` 全绿（含 5 个新测试文件；test_wb_routes 含安全断言：响应序列化后不含 access_token/refresh_token 子串；含 ta3 路由回归断言）
- [ ] `npm run format:check` + `npm run build` 通过
- [ ] `py -3.13 build_sidecar.py` 产物冒烟：`curl -H "Authorization: Bearer <key>" http://127.0.0.1:3601/v1/platforms` 返回 200
- [ ] 小号 e2e：登录（设备授权全流程）→ 签到（含重复签到幂等提示）→ 配额刷新 → 删除账号
- [ ] ta3 回归三项：/v1/auth/login/start、/v1/models、一次 /v1/chat/completions 完整对话
- [ ] 前端：账号卡片（昵称/配额/签到状态/连签天数）、添加账号弹窗、签到按钮 loading/已签到态、删除二次确认
