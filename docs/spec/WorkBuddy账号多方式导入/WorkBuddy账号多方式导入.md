# WorkBuddy 账号多方式导入

> **状态**：已实现（2026-10-11；端点契约/JWT 兜底已本机只读实测（探针 3 项 PASS）；用户验收通过：本地导入正常）
> **优先级**：核心
> **来源**：用户需求「像 cockpit tools 一样，添加账号有三种方式：OAuth 授权、json 粘贴导入、读取本地 workbuddy 配置导入」（附 cockpit-tools 弹窗三 tab 截图）
> **隐私声明**：本文档不含任何真实账号、凭证、token；响应契约仅字段名级脱敏记录。

## 背景 / 目标

「添加 WorkBuddy 账号」目前只有设备授权（OAuth）一条路。本期对齐 cockpit-tools 形态，弹窗改三 tab：

1. **OAuth 授权**（已有，不动）
2. **Token / JSON 粘贴导入**：textarea 粘贴 access token 或 JSON（单对象/数组），解析入库
3. **本地导入**：「从本机 WorkBuddy 导入」（读官方客户端登录态文件解密，复用「WorkBuddy-IDE账号切换」的密钥派生/解密能力）+「选择 JSON 文件导入」（file input 读文本走同一 JSON 解析）

**用户决策**：
- UI 形态照抄 cockpit-tools 三 tab（截图为证）
- JSON 导入格式**兼容 cockpit-tools 导出格式**（键名 snake/camel 双写、数组/`accounts`/`items` 包裹、最简条目只需 `access_token`）
- 不做导出（后续需求可加，格式已对齐）

## 参考项目（本地克隆于 `d:\Code\ref-projects\`）

| 能力 | 关键实现（cockpit-tools，MIT） |
|---|---|
| 粘贴分发 | `useProviderAccountsPage.ts:1825-1880`：`{`/`[` 开头 → importFromJson；否则整串当纯 token |
| 纯 token 补全 | `workbuddy_oauth.rs:1140-1300 build_payload_from_token`：`GET /v2/plugin/accounts`（Bearer）→ `data.accounts[]` 取 `lastLogin` 项或首项（uid/nickname/email/enterpriseId）；失败**报错不入库**；配额三件套 best-effort |
| JSON 三级解析 | `workbuddy_account.rs:778-844 import_from_json`：①单账号对象 → ②数组 → ③任意 Value（Array 逐条 / Object 取 `accounts`/`items` 键数组）；`payload_from_import_value`（:898-979）键名双写、缺字段容忍、未知键忽略 |
| 本机导入 | `workbuddy_account.rs:1222-1273` + commands 编排：读登录态 → 解密 → 联网补全（失败容忍保留本地结果）→ upsert → 清占位账号 |
| 导出格式 | `Vec<WorkbuddyAccount>` 纯数组；`id/email/access_token/created_at/last_used` 必有，其余可缺省（`expires_at` 毫秒、`created_at` 秒） |

## 本机实测证据（2026-10-11，只读）

| 项 | 结果 |
|---|---|
| `GET /v2/plugin/accounts`（Bearer + plugin 域头族） | `{code:0, msg, requestId, data:{accounts:[…]}}`；每项含 `uid/nickname/uin/type/lastLogin/pluginEnabled/deployStatus/sso/phoneNumber(明文)/mpOpenId/loginChannels`，与 auth 文件 account 对象同构；**不含 email/refreshToken/expiresAt/domain** |
| JWT 声明（access token 为 JWT） | `sub`（=account.uid，36 位）、`nickname`、`preferred_username`、`enterprise_id`、`exp/iat`、`scope` 等——**uid/expires_at 可从 JWT 兜底** |
| token 形态 | 非 `uid+token` 复合串（本机 JWT 形态）；复合串解析仅对粘贴输入兼容 |

## 设计方案

### 核心决策

| 决策 | 结论 |
|---|---|
| 解析层位置 | 新模块 `relay/relay/platforms/workbuddy/imports.py`（oauth/billing/ide_switch 并列；编排+解析不入既有模块防膨胀） |
| 粘贴分流 | `{`/`[` 开头 → JSON 解析（不联网）；否则纯 token → JWT 解码预填 + `GET /v2/plugin/accounts` 联网补全 |
| 联网补全口径 | accounts 端点失败（HTTP 非 2xx / code≠0）→ **报错不入库**（对齐 cockpit 严格口径，防坏 token 污染账号池）；JWT（sub/exp/nickname）作响应缺字段兜底 |
| 本机导入口径 | 登录态读取/解密失败 → 报错；解密成功即入库（凭证来自官方客户端，无需 accounts 校验），配额预取 best-effort（对齐 cockpit 宽松口径） |
| JSON 导入口径 | **全程不联网**（对齐 cockpit）；缺 uid 的条目照收入库（uid 空则用 `wb_{md5(token)[:12]}` 降级主键，与 oauth 归档同口径） |
| 去重 | `store.upsert_account(preserve=True)` 复用：同 uid 覆盖 token/资料、保留 checkin/quota 缓存 |
| 导入后配额 | token/本机源 best-effort 预拉（复用 oauth 归档后的预取编排）；JSON 源不拉 |
| 方法约束 | 只用 POST（Tauri 壳转发 GET/POST/DELETE） |
| 安全 | 响应一律 AccountView 序列化（不含 token，安全断言入测）；日志不落 token |

### 新增/修改文件

```
relay/relay/platforms/workbuddy/imports.py      # 新：parse_import_text / parse_import_json /
                                                #   payload_from_import_value / decode_jwt_claims /
                                                #   complete_from_accounts_api / import_from_local / import_text
relay/relay/platforms/routes.py                 # +2 端点（import / import-local）
relay/tests/test_wb_imports.py                  # 新：解析/JWT/补全/本机导入单测
relay/tests/test_wb_routes.py                   # +路由用例
relay/relay-onefile.spec、relay/relay.spec      # hiddenimports 双保险登记 relay.platforms.workbuddy.imports
frontend/src/views/providers/WorkBuddyTab.vue   # 添加弹窗改三 tab（NTabs type="segment"）
frontend/src/stores/workbuddy.ts                # importText/importLocal actions
frontend/src/api.ts                             # wbImportText/wbImportLocal
docs/spec/WorkBuddy账号多方式导入/探针-账号端点契约.py   # /v2/plugin/accounts 只读探针（可复现）
```

### 解析契约（imports.py）

**`parse_import_text(text) -> list[dict]`**（payload 统一形：`{access_token, refresh_token?, uid?, nickname?, email?, enterprise_id?, domain?, expires_at?, source}`）：

1. `text.strip()` 以 `{`/`[` 开头 → `parse_import_json`：
   - 整体 `json.loads` 失败 → 报错「不是有效 JSON」
   - ① dict 含 access_token → 单条；② list → 逐条（报错定位「第 N 条记录解析失败」）；③ dict 含 `accounts`/`items` 数组 → 逐条
   - 每条 `payload_from_import_value`：`access_token` 必填（键名 `access_token`/`accessToken`/`token`，缺失报「缺少 access_token」）；选填键名双写：`uid`、`nickname`、`email`、`enterprise_id|enterpriseId`、`enterprise_name|enterpriseName`（不入库仅忽略）、`refresh_token|refreshToken`、`domain`、`expires_at|expiresAt`（int 或数字字符串，秒/毫秒双口径归一，<1e11 视为秒 ×1000）；未知键忽略
2. 否则纯 token：`normalize`（`uid+token` 复合串 `split_once('+')` 兼容）→ 单条 `{access_token, source:"token"}` → 联网补全（见端点契约节）

**`decode_jwt_claims(token)`**：JWT 三段 base64url 解 payload（不验签，仅身份预填）；失败返回 `{}`。

**`complete_from_accounts_api(access_token)`**：`GET {WB_API_BASE}/v2/plugin/accounts`（`plugin_headers(access_token)`）→ code≠0/HTTP 错 → `PlatformUpstreamError`；取 `lastLogin` 项或首项 → uid/nickname/enterprise_id；JWT claims 兜底（accounts 缺 uid 时用 sub）；`expires_at` = JWT `exp`×1000；`domain` 空。

**`import_from_local()`**：auth 文件存在且无 logged-out 标记 → `ide_switch.get_official_key()` + 读文件 + `decode_session` → payload（access/refresh token、expiresAt、domain、account.uid/nickname/enterpriseId）→ upsert → best-effort 预拉配额；文件不存在/登出态 → 报错「未在本机 WorkBuddy 客户端中找到登录信息」。

**入库**：`store.upsert_account("workbuddy", entry, preserve=True)`；uid 缺失降级 `wb_{sha256(token)[:12]}`（同 oauth 归档口径）。

### 端点清单（platforms/routes.py 追加）

| 方法/路径 | 职责 |
|---|---|
| POST `/v1/platforms/workbuddy/accounts/import` | body `{text}`；粘贴/JSON 文件共用入口（前端 file input 读文本直传）→ `{imported: N, accounts: [AccountView…]}` |
| POST `/v1/platforms/workbuddy/accounts/import-local` | 无 body；读本机官方客户端登录态 → `{imported: 1, accounts: […]}`；本机无登录态 → 409 |

错误映射：JSON/解析错误与 accounts 补全失败 → 400/502（中文 detail 透传前端展示）；本机无登录态/客户端缺失 → 409。

### 前端弹窗（WorkBuddyTab.vue 改造）

- `NModal` 内容改 `NTabs type="segment"` 三 pane：**OAuth 授权**（现有 pending/failed/starting 内容原样迁入）/ **Token / JSON**（textarea + 「导入」按钮，placeholder 照抄 cockpit：`示例：ghu_xxx / sk-ws-xxx / {"access_token":"eyJ...","refresh_token":"rt_..."} / [{...}]`）/ **本地导入**（说明行 + 「从本机 WorkBuddy 导入」+ 分隔「或从 JSON 文件导入」+ 「选择 JSON 文件导入」）
- JSON 文件：原生 `<input type="file" accept="application/json">` 读文本 → `importText(content)`（cockpit 同款，不走 Tauri 文件对话框）
- store：`importText(text)` / `importLocal()`（成功后 `load()` + `loadIdeState()` 刷新——本机导入的账号可能正是 IDE 当前号）
- 成功 `message.success('成功导入 N 个账号')` 并关弹窗；失败 message.error 透传 relay 中文 detail

### 单测口径（keyless）

- `parse_import_text` 全矩阵：纯 token / `uid+token` 复合串 / 单对象 / 数组 / `accounts` 包裹 / `items` 包裹 / camel+snake 双写 / `expires_at` 秒毫秒归一 / 缺 access_token 报错 / 坏 JSON 报错 / 空输入报错
- `decode_jwt_claims`：sub/exp 提取、坏 JWT 返回空
- accounts 补全：MockTransport 成功（lastLogin 项选取、JWT exp 回填）/ code≠0 报错不入库
- `import_from_local`：tmp auth 文件 + 合成密钥（monkeypatch `get_official_key`/`auth_file_path`，复用 test_wb_ide_switch fixture 模式）/ 无文件报错
- 路由：import 端点 JSON/token 分流、import-local mock、401 门禁、**响应序列化不含 token 子串**（安全断言）

## 实施步骤（commit 序列）

| # | commit | 验证 |
|---|---|---|
| 1 | `feat: WorkBuddy 账号导入解析层`（imports.py + test_wb_imports.py） | pytest；ruff |
| 2 | `feat: WorkBuddy 账号导入管理端点`（routes +2 + 路由测试 + 打包 spec 登记） | pytest 全量 + 源码起服冒烟 |
| 3 | `feat: 添加账号弹窗三 tab——粘贴/本地导入`（api/store/tab） | format:check + build |
| 4 | `chore: 探针脚本 + spec 台账`（探针-账号端点契约.py + 文档收尾） | 探针只读 PASS |
| 5 | 手动验收：粘贴真实 token 导入 / JSON 文件导入 / 本机导入（账号池出现同 uid 账号且配额刷新） | 小号优先（规则 5） |

## 实物格式兼容增补（2026-10-12，用户提供两份真实文件实测驱动）

用户指出 9router 亦有 codebuddy-cn 账号（此前仅查其 oauth 目录误判不支持；实际在**设置导出**里），并提供两份实物文件驱动增补：

| 来源 | 实物格式 | 兼容结论 |
|---|---|---|
| cockpit-tools `export_accounts`（`workbuddy_accounts_*.json`） | 纯数组 + snake 键（id/email/uid/nickname/access_token/refresh_token/token_type/expires_at 毫秒/domain/dosage_notify_code/payment_type/quota_raw） | **设计时已对齐，实物直接吃进**（未知键 quota_raw 等忽略）；email 字段实为昵称兜底、照存无害 |
| 9router 设置导出（`9router-backup-*.json`） | 顶层 `{settings, providerConnections, apiKeys, …}`；账号在 `providerConnections[]`：camelCase（accessToken/refreshToken/expiresAt **ISO 串**/expiresIn 秒偏移/**无 uid**/name 为显示名/provider/authType） | **增补后兼容**：识别 `providerConnections` 包裹，**只吃 `provider=="codebuddy-cn"`**（codex/kiro 等其它平台跳过并提示）；`expiresAt` ISO→毫秒（缺省回退 `expiresIn` 秒偏移）；uid 缺失 JWT `sub` 兜底；`name` → nickname 兜底 |

- 9router 的 codebuddy-cn 设备授权流与本项目同源（同一套 `/v2/plugin/auth` 端点，`X-Product: SaaS` 档），token 体系互通；但其 accessToken issuer 为 `www.codebuddy.cn`（console 档），对 workbuddy 域 plugin API 的适用性**导入后用「刷新配额」验证**（401 换号机制兜底）
- 测试：`test_wb_imports.py` 按两份实物结构脱敏构造 3 例（cockpit 实物形态 / 9router 实物形态 / 其它平台混入跳过）；pytest 521 绿
- 两份实物文件均为同一账号（uid 相同）——导入按 uid 去重覆盖（preserve 保留缓存），互不冲突

## 已知边界

- `/v2/plugin/accounts` 不返回 email/refreshToken/expiresAt/domain：email 留空（store 可空）；expires_at 从 JWT exp 兜底；refresh_token 缺失 → 该号 chat 401 后无法刷新（`refresh_credentials` 会跳过无 refresh_token 的号换下一号，现有机制天然兜底）；domain 空 → X-Domain 头省略（现有可选逻辑）
- JSON 导入坏 token 不联网校验（对齐 cockpit）——入库后用「刷新配额」验证有效性
- 本机导入仅当前登录账号有完整凭证（accounts 数组其它账号只有 profile 无 token，无法导入）
- 粘贴多行多 token（每行一个）不做：cockpit 同样一次一条，JSON 数组已覆盖批量场景
- 登出标记存在（`.logged-out`）时本机导入报错（登录态已被官方客户端清空）

## 验收

- [x] `pytest tests/ -x -q` 全绿（517 passed；test_wb_imports 17 例解析/补全/本机导入；test_wb_routes +6 例路由含安全断言：导入响应不含 token）
- [x] ruff（新文件）+ `npm run format:check` + `npm run build` 通过
- [x] 源码起服冒烟：JSON 批量导入入库正确（无 uid 降级主键）、坏 JSON 400、**import-local 真机读出真实登录账号**（隔离数据目录）、token 零泄漏
- [x] 只读探针 3 项 PASS（`探针-账号端点契约.py`）：登录态解密 → /v2/plugin/accounts 契约 → 字段形态（确认不含 email/refreshToken/expiresAt，JWT 兜底设计成立）
- [x] 打包冒烟：onefile 产物 import 端点 200（hiddenimports 双保险登记生效）
- [x] 手动验收（2026-10-11 用户确认）：本地导入（从本机 WorkBuddy 读取登录账号）正常
