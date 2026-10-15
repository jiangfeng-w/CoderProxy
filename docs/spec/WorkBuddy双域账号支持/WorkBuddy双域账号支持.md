# WorkBuddy 双域账号支持（双平台拆分）

> **状态**：**已实现（2026-10-14，keyless/打包态全绿；国际版设备流登录 + chat 200 待补测）**——commit 1–5 全部落地：realms.py profile 单点 + 全链路参数化（chat/billing/oauth/catalog/imports/store 分池）+ 双平台注册 + 前缀 `wbcn`/`wbg`（legacy 读兼容）+ 导入端点泛化 + 前端双 tab；pytest 567 绿 + 打包冒烟 15/15；详见「验收」。原始定稿注记（2026-10-13 立稿；**2026-10-14 改版并定稿**：由「单池每账号 realm 切换」改为「**双平台实例拆分**」——用户拍板拆 tab：两域模型体系不同，单池无法正确表达；tab 名定稿 `WorkBuddy CN` / `WorkBuddy`；**模型前缀定稿**：`wbcn/`（CN）/ `wbg/`（国际版；**2026-10-14 二次拍板**：国际版由 `wb` 改 `wbg`，降低短前缀撞名风险），停用 `WorkBuddy/` 作规范前缀、仅保留读兼容归一；**2026-10-14 定位拍板**：①国内版/国际版**按两个独立平台完整区分**（独立池/目录/前缀/tab；profile 仅为代码层实现手段，不改变「两个平台」语义）；②国际版反代实现**沿用 CN 版同源参考谱系**（workbuddy2api-panel 主线 + 9router/chatcoder/cockpit-tools 佐证），不另寻新参考；③**产品矩阵澄清（用户 2026-10-14）**：workbuddy 系列共 **4 产品**（CodeBuddy IDE 线 / WorkBuddy Agent 线 × 国内 / 国际）；**同 locale 两软件共用一个账号体系、国内/国际完全互不干扰**（四者可同机共存——用户本机仅装国内版两款）；**国内版签到只在 WorkBuddy**（国内 platform 选 `workbuddy` 即此历史因）、**国际版无签到**（定稿）；国际版域选 `workbuddy.ai`（国际版无签到故 `workbuddy.ai` / `codebuddy.ai` 功能等价，为统一取 WorkBuddy 域）；**2026-10-14 复核修订**：导入/导出口径统一（§1/§4）、归属校验落点细化（§6）、打包登记/模块级单例/测试清单补全（§3、验收、清单）、三需求同批协调（§9）））
> **优先级**：核心
> **来源**：《WorkBuddy 反代优化方案》P0-1 + 用户需求「分成两个 tab：WorkBuddy CN 和 WorkBuddy（国际版模型和国内版不一样）」+「前缀改短：`wbcn/`、`wbg/`，不再用 `WorkBuddy` 做前缀」
> **参考依据**：panel `headers.go`（realm 头族分级）/ `client.go`（`globalOn` / `chatBase` / `billingMeterPaths` / `refresh` 走 chatBase）/ `panel/login.go`（`loginEndpoints` 分域）/ `pool/realm.go`（realm 分池选号）——**panel 与本项目同为 WorkBuddy 产品线**，其 global 实现是国际版主档；9router `codebuddy-cn` / `codebuddy-intl` 为**并列产品线（CodeBuddy/IDE）**的双 provider——仅取「同产品线 CN/intl 拆双 provider」的形态佐证，域值不援用；chatcoder / cockpit-tools / workbuddy-switch 继续作链路口径佐证。**反代实现主线沿用 CN 版同源参考谱系**（2026-10-14 拍板），国际版不另寻新参考

## 背景 / 目标

### 为什么必须拆（单池混两域的硬伤）

1. **模型目录天然只能表达一边**：`catalog.py:79-85` 目录拉取取「首个 normal 号」——单池混号时 `/v1/models` 只会出现第一个号所属域的模型，另一域模型缺失。
2. **聊天选号会错配**：`chat.py:442-465` 换号只认 401/403；国际版独有模型轮到 CN 号 → 上游 11102 → 当前代码不换号、直接 404——同一请求时好时坏（取决于游标转到谁）。
3. **两域模型体系不同**（用户观测）：各有自己目录与档位；混在一个前缀/一个池里无法正确表达。

拆成两个平台实例后：`wbcn/...` 恒走 CN 池、`wbg/...` 恒走国际版池——模型→账号零错配，各 tab 模型列表天然正确。

### 目标

- 新增平台实例 `workbuddy-global`（WorkBuddy 国际版）：独立账号池、独立模型目录、独立模型前缀；**CN 现有行为逐字零回归**。
- realm 从「每账号」收敛到「每平台」：聊天热路径零 realm 分支（差异固定在平台层 profile）。

## 设计方案

### 1. 双平台实例总表

| | CN（现状） | 国际版（新增） |
|---|---|---|
| platform_id | `workbuddy`（不动） | `workbuddy-global` |
| 显示名（tab） | `WorkBuddy CN`（用户拍板 2026-10-14） | `WorkBuddy`（用户拍板 2026-10-14） |
| 模型前缀（agent 可见） | **`wbcn/`**（用户拍板 2026-10-14；旧 `WorkBuddy/` 停止作规范前缀、读兼容归一） | **`wbg/`**（用户拍板 2026-10-14；二次拍板由 `wb` 改 `wbg`——降低撞名风险） |
| 账号池文件 | `platform_workbuddy.json`（不动） | `platform_workbuddy-global.json`（store 按 platform_id 自动隔离） |
| 签到 | 有 | **无**（定稿：用户 2026-10-14 确认国际版无签到功能；panel D4 门控同证） |
| OAuth 登录 / 粘贴 Token·JSON 导入 / 配额 / 删除 | 有 | 有（粘贴导入走泛化端点） |
| 本机导入 / 导出 / IDE 切号 | 有 | **有**（2026-10-15 二期补齐：本机实测 WorkBuddyAI 客户端登录态 `workbuddy-desktop-ai.info` 与 CN 同构同族、exe 同名 binding 派生密钥可用；导出 id 前缀 `codebuddy_`、9router provider `codebuddy-intl`；端点泛化 `/{platform}/...`） |

### 2. realm profile（数据化单点）

新增 `relay/platforms/workbuddy/realms.py`：`WbRealm` 数据类 + `REALM_CN` / `REALM_GLOBAL` 两实例。所有双域差异收敛于此，其余模块只读 profile：

> **定位（2026-10-14 拍板）**：国内版/国际版**按两个独立平台对待**——各自账号体系、模型体系、前缀、tab，语义上就是两个平台；`WbRealm` profile 是**代码层实现手段**（差异数据化单点、保零回归、便单测），不改变双平台定位。国际版反代实现**沿用 CN 版同源参考谱系**（workbuddy2api-panel 为主线，9router / chatcoder / cockpit-tools 佐证），不另寻新参考——`WbRealm` 的字段取值全部来自该谱系对 global 的既有实证。

| profile 字段 | CN 取值（= 现状字面量） | 国际版取值（panel 证据） |
|---|---|---|
| chat_base | `wb_api_base`（copilot.tencent.com） | `wb_global_base`（新增配置，缺省 `https://www.workbuddy.ai`） |
| billing_base | `wb_api_base`（现状 `billing.py:65/389`） | `wb_global_base` |
| refresh_base | `wb_web_base`（codebuddy.cn，现状 `oauth.py:230`） | `wb_global_base`（panel refresh 走 chatBase→global 域） |
| login_base（auth/state、token、login/account） | `wb_api_base` | `wb_global_base`（panel `loginEndpoints`） |
| catalog_base | `wb_api_base` | `wb_global_base` |
| billing 路径候选 | `/v2/billing/meter/*` 单候选（现状） | **去 `/v2` 前缀优先** → `/v2` 回退（panel `billingMeterPaths` / `checkinMeterPaths`） |
| 目录企业端点候选 | `/console/enterprises/personal/models`（现状） | `/v2/enterprises/personal/models` → `/console/...` 回退（panel `globalModelsProbePaths`） |
| Origin / Referer | `https://www.codebuddy.cn` | `https://www.workbuddy.ai` |
| Accept-Language | `zh-CN` | `en-US`（panel `acceptLanguageFor`） |
| UA 平台段 | `WorkBuddy` | `WorkBuddy AI`（`WorkBuddy/{v} WorkBuddy AI/{v} CLI/{cli}`；panel `defaultWorkBuddyUAFor`） |
| chat X-Domain / 企业头 | 账号 domain；无则 `X-No-Department-Info: 1`；有企业发 `X-Enterprise-Id` | 恒 `X-Domain: www.workbuddy.ai` + `X-No-Enterprise-Id: 1`（panel `injectGlobalChatHeaders`） |
| billing UA（单段） | `WorkBuddy/{v}` | 同 CN（panel `billingUA` 不分域） |
| billing X-Enterprise-Id / X-Tenant-Id | 有企业则发（现状） | 不注入（global 无企业概念） |
| X-IDE-Name/Type/Product/Purpose 四头 | `WorkBuddy`（现状） | 同 CN（panel `injectAttribution` 不分域——**只换 UA 平台段**，勿自我发挥） |
| oauth platform 参数 | `workbuddy`（现状桌面档） | 先沿用 `workbuddy`；实测不通再校准（profile 单字段） |
| supports_checkin | True | False（定稿：国际版无签到——用户确认 + panel `scheduler.go` D4 门控同证） |
| 账号归属域 | **拒绝国际版两族**（`workbuddy.ai` / `codebuddy.ai`——国际账号体系）；其余（含空域）放行 | `workbuddy.ai` / `codebuddy.ai` 两族均候选（国际两软件共用账号体系），以补全成功为准（见 §6） |

- `{v}` / `{cli}` 沿用 `5.5.4` / `2.137.1`（panel intl 逆向为 `5.5.2`——先同款只换平台段，实测后再校准）
- `wb_user_agent` 语义收紧为**显式覆盖**（默认空 → 按 profile 拼；用户显式设置时两域都用该值，保留自愈路径）；新增 `wb_cli_version`（env `WB_CLI_VERSION`）

> **产品矩阵与账号体系（2026-10-14 用户澄清，定稿口径）**：workbuddy 系列共 **4 个产品**——两条产品线 × 国内/国际：
>
> | | 国内 | 国际 | 说明 |
> |---|---|---|---|
> | **CodeBuddy**（IDE 线） | codebuddy.cn | codebuddy.ai | ≈ VSCode 系 IDE |
> | **WorkBuddy**（Agent 线） | workbuddy.cn 族（`copilot.tencent.com` 等） | workbuddy.ai | ≈ Codex 系 Agent |
>
> **账号体系**：**同一 locale 的两个软件共用一套账号**（国内 CodeBuddy+WorkBuddy 一个号；国际 CodeBuddy+WorkBuddy 一个号）；**国内/国际两套完全互不干扰**（四个软件可同机共存——用户本机仅装国内版两款）。
>
> **参考仓库口径差异的根因**：9router 反代 codebuddy 域（其 intl = `codebuddy.ai`、品牌 `IDE`/`CodeBuddy`、platform=ide）；panel 反代 workbuddy 域（其 global = `workbuddy.ai`、品牌 `WorkBuddy AI`）——**两者都正确，只是各自产品线不同**。
>
> **本项目选型**：国内 WB 平台当初选 `workbuddy` 域正是因为**签到在 WorkBuddy**（历史因）；**国际版无签到**（定稿）→ 域选 `workbuddy.ai` 与 `codebuddy.ai` 功能等价，**为与国内版统一取 `workbuddy.ai`**（panel 口径）。codebuddy 域若未来要反代，属**第三、四个平台**（另立项，前缀 `cbcn/`+`cbai/` 可参考 9router 双 provider），本方案不含。9router `codebuddy-intl` 仅作「同产品线 CN/intl 拆双 provider」形态佐证，域值不援用。

### 3. 代码参数化落点

- **client.py**：头构造改读 profile（`user_agent_for(profile)` / `origin_for(profile)` / `accept_language_for(profile)`；`billing_headers` / `refresh_headers` 加 `profile` 参数，**默认 `REALM_CN` 保零回归**）
- **chat.py**：`WorkBuddyChatAdapter(model, *, profile=REALM_CN, ...)`；`_headers` / `_stream_once` 的 base 与头走 profile；`_normal_accounts` / `_pick_account` 用 `profile.platform_id`；`factory` 与 `register_provider` 双注册（`partial` 捕获 profile）；401 刷新钩子随 adapter 实例（profile 天然正确）
- **模块级单例改造（易漏点，两处）**：`chat.py:203` 与 `catalog.py:44` 各有一个 `_platform_adapter = WorkBuddyAdapter()` 写死 CN 的模块级单例——**必须改为按 profile 取实例**（chat：收进 adapter 实例字段，轮换链经 `self._account_adapter.ensure_token`；catalog：改 `dict[platform_id, WorkBuddyAdapter]` 惰性缓存——实例轻量无状态，刷新锁本就是模块级按 uid 共享）。不改造会导致**国际版账号被拿去 CN 池刷新/归档**（串池）
- **adapter.py**：`WorkBuddyAdapter(profile=REALM_CN)`；`PLATFORM_ID` 常量 → `self.profile.platform_id`；`info()` 返回对应 `PlatformInfo`（display_name / supports_checkin / supports_quota）
- **billing.py**：base / 路径候选走 profile；`perform_checkin` / `refresh_checkin_status` / `fetch_quota` 等签名加 `profile` 参数，**内部 `store.update_account(PLATFORM_ID, …)` 全部改按 `profile.platform_id` 落库**（账本分池，避免签到/配额写错池）；签到按 `supports_checkin` 门控（国际版 False 定稿 → 返回明确「国际版无签到」文案、不发上游；CN 走现状）；`prefetch_caches` 按 profile 分支（CN：签到状态 + 配额；国际版：仅配额）
- **oauth.py**：`start_login(profile)` / `refresh_token(account, profile)` 贯穿；登录槽记录 profile；`prefetch_caches` 按 profile 分支；**`imports.py` 的跨模块依赖一并处理**——现 `imports.py:44 from …oauth import PLATFORM_ID, prefetch_caches`，重构后 PLATFORM_ID 不再是单一常量：`import_text(text, profile)` / `import_local(profile=REALM_CN)` 签名显式传 profile，`imports.py` 内部的 `store.upsert_account(PLATFORM_ID, …)` 与预取调用全部改按 profile
- **catalog.py**：base / 企业端点候选走 profile；**缓存按 platform_id 分桶**（现 `_cache` 全局单例 → `dict[platform_id]`；`_negative_until` 同理）
- **imports.py**：补全调用走 profile base；归属校验（见 §6）
- **platforms/__init__.py**：`PLATFORMS = {"workbuddy": WorkBuddyAdapter(REALM_CN), "workbuddy-global": WorkBuddyAdapter(REALM_GLOBAL)}`
- **model_ref.py**：新增 `PREFIX_WBCN = "wbcn"`（CN）与 `PREFIX_WBG = "wbg"`（国际版）入 `base_prefix_table()`；**legacy 别名** `workbuddy → wbcn`（读兼容，见 §5）。自定义供应商名称冲突守卫 `providers_custom.name_conflicts` 自动生效（「WorkBuddy」保留字一并在 legacy 表内）
- **routes.py**：`_visible_by_default` / `_model_visible` 的无档位默认隐藏规则改成「前缀 ∈ {`wbcn`, `wbg`}」；自定义名称冲突文案改「（牛码/wbcn/wbg；WorkBuddy 为保留字）」；`_any_platform_available` 查两个池（任一非空即可用）

### 4. 注册与路由

- 模型前缀路由：`wbcn/model` → CN factory；`wbg/model` → 国际版 factory（`register_provider("wbcn", ...)` / `register_provider("wbg", ...)` 双注册；`adapter_registry` 已按前缀分发）；旧 `WorkBuddy/model` 读兼容归一 `wbcn/model`（见 §5）
- 平台端点：全走 `{platform}` 路径参数（现有通用端点零改动即可服务 `workbuddy-global`）
- **导入端点细分**（与 §1 能力表口径一致）：仅**粘贴导入**从现硬编码 `/workbuddy/accounts/import` 泛化为 `/v1/platforms/{platform}/accounts/import`（前端两组 tab 共用；handler 按 platform 的 profile 走补全 base + 归属校验）；`import-local` / `export` / `ide-switch` / `ide-state` **路径保持现 `/workbuddy/...` 硬编码不动**——国际版无对应端点（FastAPI 对未知路径天然 404），前端不显示入口，无需 handler 级兜底；`exports.py` / `ide_switch.py` 模块不动（CN 专用，`PLATFORM_ID` 常量保持）
- 可用性：chat 的 `_available()` 按 profile 查本池（`routes` 区分 503/404 用）；服务门控 `_any_platform_available` 查两池

### 5. 旧前缀退役（`WorkBuddy/` → `wbcn/` 读兼容）

- **背景**：`WorkBuddy/` 是 2026-10-08 起现网在用的前缀——存量 agent 配置的请求全名、落盘设置键（`model_whitelist` / `model_visibility` / `thinking_defaults`）都含此形态。本次**停用为规范前缀、保留读兼容归一**，不破坏存量：
  - `model_ref.base_prefix_table()` 保留 `workbuddy → wbcn` 映射（大小写不敏感）→ 存量 agent 请求 `WorkBuddy/deepseek-flash` 解析归一为 `wbcn/deepseek-flash`，照常路由 CN 池
  - 落盘键读归一：三处读路径经 `model_ref.canonical_key` 已天然归一——旧键自动等价命中（**不改落盘**、后续保存时自然写新键，与既有「存量裸名 → 牛码/ 仅读归一」先例一致）
  - `/v1/models` 只输出新前缀 id（`wbcn/…`、`wbg/…`）；旧前缀不再出现在任何输出
- **保留字**：`WorkBuddy` 不入规范输出但继续禁用作自定义供应商名（legacy 映射在表内，`name_conflicts` 天然拦截）；`wbcn` / `wbg` 同样保留
- **勿混改**：UA 里的 `WorkBuddy` 品牌段、`workbuddy` platform_id / Store 文件名是上游指纹与内部标识，**与模型前缀无关，一律不动**
- 拒绝项：不做「全局迁移脚本改落盘旧键」（读归一已等价；主动迁移徒增写风险与回滚面）

### 6. 账号归属校验（防混池）

- **两个入口都校验（登录归档 + 导入入库）**，统一收在 `imports.py` / `oauth.py` 归档前的归属判定函数（profile 可判定）：
  - **国际版平台**：候选域 = `workbuddy.ai` / `codebuddy.ai` 两族（**国际版两软件共用账号体系**「用户 2026-10-14」，账号的 domain 形态可能来自任一侧）；最终以「补全调用是否成功」为准（补全走国际版 base）；CN 域名账号自然失败 → 不入库
  - **CN 平台**：登录/导入时拒绝**国际版两族域**（`workbuddy.ai` / `codebuddy.ai`——国际账号体系）；其余（含空域，兼容存量）放行并归 CN；国际号误入提示「请到 WorkBuddy tab 添加」（现状这些号在 CN 池本来就不可用，拒绝是修正而非回归）
- **存量不动**：本校验只作用于**新登录 / 新导入**两个入口；已躺在 `platform_workbuddy.json` 里的账号（含用户手改 JSON 塞入的 global 号）**不追溯、不清理**——该号在 CN 池本就跑不通（404/11102 类表现），清理动作留给用户手动删除，避免自动删数据

### 7. 前端

- `Providers.vue`：现有 tab 文案改 `WorkBuddy CN`，新增第二个 tab `WorkBuddy`（复用 `WorkBuddyTab`，传 `platform` prop）；后端 `PlatformInfo.display_name` 同步（CN `WorkBuddy CN` / 国际版 `WorkBuddy`；现 `WorkBuddy (CodeBuddy CN)` 退役）
- `WorkBuddyTab.vue`：所有 API 调用带 platform；**模型区前缀过滤按 platform**（CN `wbcn/` / 国际版 `wbg/`）；能力位隐藏——国际版仅隐藏签到/连签（`supports_checkin=False`）；**二期（2026-10-15）**：IDE 切号 / 导出 / 本机导入对国际版放开（`cnOnly` 已移除，文案按 displayName 动态）
- `stores/workbuddy.ts`：**方案落死为「按 platform 工厂化」**（`useWorkbuddyStoreFor(platform)`：内部 `defineStore('wb-'+platform)` 惰性注册 + 模块级 Map 缓存实例保证幂等——Pinia 同 id 重复 `defineStore` 本身安全，缓存的目的是避免每次调用新建 Map 查找开销）；不采用「单 store 加 platform 维度」（两池状态天然独立，工厂化后组件各自持有，逻辑更直白）
- `api.ts`：`wb*` 函数加 `platform` 参数（默认 `workbuddy`——现有调用点零改动）；`import-local` / `export` / `ide-*` 函数**不加 platform 参数**（CN 专用路径）

### 8. 被否决的备选

- **单池 + 每账号 realm 切换**（2026-10-13 初稿）：否决——目录单手、选号错配、模型列表三处硬伤（§背景）；且热路径要按账号分支
- **国际版复制一套模块**：否决——双域差异全部集中在 profile（约一屏数据），复制代码违反单点原则
- **单 tab 内分节展示**：否决——用户拍板拆 tab；两域账号池/模型前缀本就该是独立命名空间
- **把 codebuddy 域（codebuddy.cn / codebuddy.ai）纳入本需求**：否决——codebuddy 是并列产品线（IDE），若反代属**第三、四个平台**、另立项（前缀 `cbcn/`+`cbai/` 可参考 9router 双 provider 形态）；本方案只做 WorkBuddy 产品线（`workbuddy` + `workbuddy-global`）
- ~~**国际版后台挂载 import-local/export 端点 + handler 404 兜底**：否决——路径保持 CN 专用（现硬编码形态），国际版天然无此端点~~ **（2026-10-15 二期推翻）**：国际版客户端（WorkBuddyAI 5.7.6）本机实测已安装，登录态文件/加密族/exe binding 与 CN 同构 → 客户端集成端点全部泛化 `/{platform}/...`（realm_for 判 404），CN 旧路径保留别名；「CN 生态专属」前提不再成立

### 9. 三需求同批协调（2026-10-14 追加）

《双域》（本篇）、《[反代账号池健康管理](../WorkBuddy反代账号池健康管理/)》、《[反代账号优先级](../WorkBuddy反代账号优先级/)》**三者改动全部落在同一条链上**——`chat.py` 选号/轮换链 + `store.py` 账号行字段：

| 需求 | 对同一条链的改动 |
|---|---|
| 双域 | `_headers`/base/选号 profile 化、`_normal_accounts` 按 `profile.platform_id` 查池 |
| 健康管理 | 轮换链喂入口（连败/冷却）、`_pick_account` 过滤降权/冷却、store 加三字段 + preserve |
| 优先级 | `_pick_account` 分组、store 加 priority + preserve |

分三轮做会把同一段代码改三遍、测试写三遍。**实施顺序建议：双域先行（打底：profile 参数化 + 池隔离）→ 健康管理 + 优先级同批紧随（一次改到位 `_pick_account` 与 preserve 名单）**；健康管理 spec 的「§5 与优先级协调」保持有效。三篇 spec 的实施步骤表以本注记为准排期。

## 已知边界

- **国际版签到（定稿）**：**无签到功能**（用户 2026-10-14 确认；panel 同证——`scheduler.go:436` D4 门控「realm=global 账号无签到体系，直接跳过（不发起任何上游调用，避免风控）」+ `pool/entry.go:94`「global 域账号无签到体系，恒为 false」）。本仓库实现：`supports_checkin=False` → 签到端点返回明确「国际版无签到」文案、不发上游；前端隐藏签到入口。备注：panel 对国际版另有**对话活跃上报**（`/v2/report`，点亮连登/每日积分）——属"任务/连登"类功能、非签到，超本期范围（本仓库不实现）
- ~~国际版 IDE 切号 / 导出 / 本机导入本期不做~~ **（2026-10-15 二期补齐）**：本机实测 WorkBuddyAI 5.7.6——① 登录态 `workbuddy-desktop-ai.info` 与 CN `workbuddy-desktop.info` 同目录并列、`$wbEncrypted` 结构同构（account/auth/accounts/allAccounts 四根 + 字段级 envelope）；② exe 含同名 `electron_browser_workbuddy_storage` binding，密钥派生脚本原样可用（打包产物冒烟实证：import-local 真读到国际账号 uid/domain）；③ exe 装 D 盘（`D:\Programs\WorkBuddyAI`），候选路径 + winreg 注册表兜底发现；④ 导出对齐 cockpit CodeBuddy 模块格式（id=`codebuddy_`+md5(小写 uid)，用户实物 `codebuddy_461e541f…` 逐字吻合）、9router provider `codebuddy-intl`
- **国际版 OAuth platform 参数（2026-10-14 实测校准）**：`workbuddy` 与 `CLI` 在国际版 base `/v2/plugin/auth/state` 均返回 code=0 + 有效 state → **沿用 `workbuddy`**（profile 单字段可改，留作日后校准点）
- **目录/配额路径候选序（2026-10-14 实测校准）**：`/v3/config` 200（27 模型）；`/v2/enterprises/personal/models` 200（18 模型）而 `/console/...` 500 → `/v2` 家族优先回归确认；billing `/billing/meter/get-user-resource`（无 /v2）200 且带回 `Accounts[]`（`/v2` 亦可作回退）；`get-user-resource-summary` 在国际版 base 404（不进候选）；`/v2/plugin/accounts` 存在（200）
- **chat 出站形状（2026-10-14 实测）**：国际版 `/v2/chat/completions` 首条消息非 system → **400/11128**「first message is not system prompt」→ `ensure_system_first` 前置中性 system（panel `ensureConsoleSystem` 同款）；请求 body 形状（类型化块）与三种头族（panel 全量 / OmniRoute SaaS / 9router intl）在该小号 token 下均被 **403/11140**「request illegal」（displayMsg=安全审核）拒绝——**与头族形状无关**（账号健康复验 200，未封）；判定为该 token 的授权会话类型（console 授权流，JWT `azp: console`）不被 chat 网关接受，chat 需 relay 设备授权流（platform=workbuddy）签发的 token；**待设备流登录后用 relay 链路补测 chat 200**（验收项「无 11140」的正式判定点）
- 国际版 `auth/token` 轮询行为（11217 pending 语义）沿用 CN 同构实现（OmniRoute 对 workbuddy.ai 实测同形状），未单独复测
- 轮询游标仍为模块级全局（两池共享一个计数器——仅影响轮起始位置，无正确性问题；如需独立再拆）
- 国际版模型默认可见性规则与 CN 同款（前缀 ∈ {`wbcn`, `wbg`} + 无档位 → 默认隐藏）
- 前缀大小写不敏感匹配（现口径继承）：`WBCN/…`、`WBG/…` 等输入均归一为小写规范形态输出
- 前缀撞名：`wbg` / `wbcn` 均为 3+ 字符、撞名概率极低；`name_conflicts` 只拦新建/改名、不追溯存量自定义供应商——若存量恰有同名自定义供应商会遮蔽其前缀路由，届时手动改名即可（知情接受，不做迁移脚本）
- **`workbuddy://` 协议回跳唤起 CN 客户端（2026-10-15 用户实测 + 注册表取证）**：添加**国际版**账号时，浏览器授权完成后可能自动打开一次 **WorkBuddy CN 客户端**。根因：上游 AI 页面 OAuth 回跳用 `workbuddy://` scheme，而 Windows 注册表里该 scheme 被 CN 客户端抢注（`HKCU\Software\Classes\workbuddy\shell\open\command` → `WorkBuddy.exe`；国际版注册的是 `workbuddy-ai://`、另一条 `buddy-app://` 也指向 CN）——**上游页面行为，relay/本应用未启动任何 CN 客户端**（添加账号链路零 start_client 调用），不写数据、不影响登录结果。缓解：国际版 OAuth 弹窗加知情提示文案（本地修订，非修复）；不改注册表（侵入用户系统）
- 规则 5：全部真机验证只用小号

## 验收

- [x] profile 单测：CN profile 所有取值与现状字面量逐字一致；国际版取值对齐 panel 证据表（`test_wb_dual_realm.py::test_realm_*`）
- [x] `_headers` 双 profile 快照用例（UA 三段平台段、Origin、Accept-Language、X-Domain、X-No-Enterprise-Id / X-Enterprise-Id 分支）；CN 断言改为对拼装产物断言（输出与变更前逐字一致）
- [x] 前缀路由：`wbg/x` 构造国际版 adapter、`wbcn/x` 构造 CN adapter；未知模型 404 语义不变
- [x] **旧前缀读兼容**：请求 `WorkBuddy/x` 归一 `wbcn/x` 正常路由；落盘旧键（白名单/可见性/思考档位）经读归一等价命中；`/v1/models` 不再输出 `WorkBuddy/…`；自定义供应商名 `WorkBuddy`/`wbcn`/`wbg` 被拒（`name_conflicts` 实测拦截）
- [x] 账号池隔离：两 platform_id 独立文件读写、互不可见（单测 + 冒烟「池隔离」项）
- [x] **全链路串池回归（单测级）**：国际版账号的 ensure_token / 签到拒答 / 配额写入 / 目录归档全部落 `platform_workbuddy-global.json`（断言 CN 文件零写入）；CN 反之（`test_billing_writes_own_pool` / `test_catalog_uses_own_pool` / `test_oauth_refresh_writes_own_pool`）
- [x] **模块级单例分流**：chat 轮换链与 catalog 取用的账号层实例随 profile（chat 单例收进 adapter 实例字段；catalog 按 platform_id 惰性缓存；`test_pool_isolation_two_files` + 冒烟双池目录项）
- [x] 归属校验：global 域账号进 CN 池被拒并引导；CN 号进国际版池补全失败不入库；**已存在于池内的历史账号不受校验影响（不追溯）**（`allows_domain` 仅作用新登录/新导入两入口）
- [x] 导入端点细分：粘贴 `/accounts/import` 双平台可用；`import-local` / `export` 对 `workbuddy-global` 未挂载（**实测为 405 而非 404**——FastAPI 路径命中原生 DELETE `/{platform}/accounts/{uid}` 模式、方法不匹配；语义一致：绝无 2xx，前端不显示入口。测试按实测断言，见 `test_import_local_export_not_mounted_for_global`）
- [x] 可用性/门控：仅国际版池有号时服务可用；`_available()` 按池判定（冒烟「服务门控任一上游可用」+「CN 池空 → wbcn 503 引导」）
- [x] 国际版签到（定稿为无）：`supports_checkin=False` → 明确「国际版无签到」文案 + transport 断言零上游调用（单测 + 冒烟项）
- [x] CN 全量 `test_wb_*` 绿 + **`test_model_prefix.py` 按 `wbcn` 口径同步改写后绿**（含 legacy `WorkBuddy/x` → `wbcn/x` 归一断言）；**pytest 567 绿**
- [x] （有国际版小号时）实测（2026-10-15 全部闭环）：目录 200 / 配额 200 / auth·plugin 200（探针）；**登录（本机客户端导入 + 设备流）与 chat 已实测**——用户真实对话 **46+ 轮成功**（含 35 工具 / thinking max / 三协议 / Claude Code·ZCode 等真 agent），零功能性 error；此前 403/11140 系 console 授权流 token 限制，非链路问题
  - ⚠ **2026-10-16 修正注记**：上述「探针」中的 `探针-wb国际版契约.py` / `探针-wb国际版chat形状.py` 系**绕过 relay 直连上游**（含 UA/头族 A/B 风控对照），**两个脚本已封存禁用**（违反 AGENTS.md 硬性规则 6）；同期另一国际版账号被封（疑与此相关），本 spec 结论保留、复测方法改经 relay/伪装层——见 [`docs/上游风控与客户端兼容.md`](../../上游风控与客户端兼容.md) §5
- [x] **二期验收（2026-10-15，用户实测）**：A 切号正常 / B cockpit-tools 导入成功 / C 9router 导入成功 / D 真 agent（ZCode）跨轮多工具对话正常 / E 官方客户端内对话正常；F 限额边界知情不测；脚本层四项全过（思考档位 none=0·low=37·max=43 / 工具跨轮 3 轮 / 双池并发隔离（附 CN 6004 限流透传实录）/ 协议三连）
- [x] 前端：两 tab（`WorkBuddy CN` / `WorkBuddy`）并存、各自账号与模型区正确、国际版能力位隐藏；format:check + build 绿（vue-tsc 零错 + prettier 全绿 + vite build 通过）
- [x] **打包冒烟**：`relay.spec` / `relay-onefile.spec` 登记 `relay.platforms.workbuddy.realms` 后，产物起服 + 双前缀 `/v1/models` + 池隔离/503 语义/旧前缀兼容/端点细分/签到拒答（`冒烟-双域注册.py` 源码态 15/15 + **打包产物态 15/15**；CN chat 真对话待解封按规则 5 补测）

## 实施步骤（commit 序列）

| # | commit | 验证 |
|---|---|---|
| 1 | `feat: WorkBuddy realm profile 与双平台注册`（realms.py + config + platforms 注册 + 模块级单例改造 + 测试） | pytest；CN profile 逐字回归；单例分流用例 |
| 2 | `feat: 聊天/账单/登录/目录按 profile 参数化`（chat/billing/oauth/catalog/imports + 双注册 + store 分池 + 测试） | pytest；双 profile 快照；串池回归 |
| 3 | `feat: 模型前缀改短（wbcn/wbg）+ 旧前缀读兼容`（model_ref + routes + 前端过滤 + test_model_prefix 同步 + 测试） | pytest；旧名等价用例 |
| 4 | `feat: 国际版平台端点泛化与归属校验`（platforms/routes import 细分 + imports 校验 + 测试） | pytest；404 未挂载用例 |
| 5 | `feat: 供应商页双 tab（WorkBuddy CN / WorkBuddy）`（Providers.vue + WorkBuddyTab prop + store/api + build） | format:check + build |
| 6 | `chore: 打包登记 + spec 台账回填`（两个 spec 文件 + 国际版小号实测结论 / platform 参数校准） | 打包冒烟 / 探针 / 手动 |

> 与健康管理/优先级同批时按 §9 排期：本表先行（commit 1–2 打底），健康+优先级紧随。

## 改动文件清单

```
relay/relay/platforms/workbuddy/realms.py   # 新：WbRealm + REALM_CN / REALM_GLOBAL + is_global_domain
relay/relay/config.py                       # + wb_global_base / wb_cli_version；wb_user_agent 语义收紧
relay/relay/platforms/workbuddy/client.py   # 头构造按 profile
relay/relay/platforms/workbuddy/chat.py     # adapter 持 profile；base/头/选号；模块单例收进实例；factory 双注册
relay/relay/platforms/workbuddy/adapter.py  # WorkBuddyAdapter(profile)；PLATFORM_ID → profile.platform_id；info() 能力位
relay/relay/platforms/workbuddy/billing.py  # base/路径候选按 profile；签到分支；store 落库按 platform_id
relay/relay/platforms/workbuddy/oauth.py    # realm 参数贯穿；预取分支；归档/刷新按 profile 落库
relay/relay/platforms/workbuddy/catalog.py  # base/候选；缓存分桶；模块单例按 platform_id 缓存
relay/relay/platforms/workbuddy/imports.py  # 补全 base + 归属校验（两入口）+ profile 贯穿；解除对 oauth.PLATFORM_ID 的常量依赖
relay/relay/platforms/__init__.py           # PLATFORMS 双实例
relay/relay/model_ref.py                    # + PREFIX_WBCN/PREFIX_WBG；workbuddy→wbcn legacy 映射
relay/relay/routes.py                       # 前缀规则双化（{wbcn,wbg}）；双池可用性；冲突文案（牛码/wbcn/wbg）
relay/relay/platforms/routes.py             # 仅粘贴 import 端点泛化；import-local/export/ide-* 保持 CN 专用路径
relay/relay-onefile.spec / relay.spec       # hiddenimports 登记 relay.platforms.workbuddy.realms（双保险惯例）
relay/tests/test_wb_*.py                    # profile/双快照/隔离/校验/路由/签到拒答
relay/tests/test_model_prefix.py            # 前缀断言按 wbcn 口径改写 + legacy 归一用例
frontend/src/views/Providers.vue            # tab 文案改 `WorkBuddy CN` + 新增 `WorkBuddy` tab
frontend/src/views/providers/WorkBuddyTab.vue  # platform prop + 能力位隐藏 + 前缀过滤参数化
frontend/src/stores/workbuddy.ts            # useWorkbuddyStoreFor(platform) 工厂化（方案落死）
frontend/src/api.ts                         # wb* platform 参数（CN 专用函数不加）
```
