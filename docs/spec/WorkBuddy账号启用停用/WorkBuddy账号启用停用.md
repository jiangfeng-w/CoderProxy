# WorkBuddy 账号启用停用

> **状态**：方案已定稿（2026-10-15；用户拍板三决策：**单独立项** / **全链路排除** / **新增 `enabled` 字段**）
> **优先级**：交付
> **来源**：用户需求「可以启用和停用某个账号，就像 9router，停用的时候请求就不会走这个账号」
> **前置**：账号池存储（《WorkBuddy多账号登录与签到》）+ 反代选号（《WorkBuddy聊天反代与多平台聚合》）+ 双域拆分（《WorkBuddy双域账号支持》）
> **同批**：与《[WorkBuddy 反代账号池健康管理](../WorkBuddy反代账号池健康管理/)》《[WorkBuddy 反代账号优先级](../WorkBuddy反代账号优先级/)》共享 `store.py` preserve 名单与选号链——**三需求一次改完**（见「同批协调」）

## 背景 / 目标

现状：账号池所有 `status == "normal"` 的号全部参与请求（反代聊天轮询 + 模型目录选号 + 服务门控）。用户无法把某个号「从轮转中摘出去」——临时不想用的号只能删除，重新添加要重走登录/导入。

本期给每个账号加**用户级启用开关**（对齐 9router `isActive`）：

- 账号行 `enabled: bool`（缺省 true；与 `status` 系统态**分离**）
- 停用 = **全链路排除**：反代选号 / 模型目录选号 / 服务门控三处都不再算它（全停用 ≈ 池空）
- **管理操作不受影响**：签到 / 配额刷新 / 刷新状态 / IDE 切号 / 导出 / 删除照常可用（用户显式操作单号）
- 只有用户能改 `enabled`——系统任何自动逻辑（连败挂起/冷却/401 换号）**永不写**该字段（与健康管理「只挂起不杀号」哲学一致）

### 参考实现（2026-10-15 核对）

| 参考 | 落点 | 形态 |
|---|---|---|
| 9router | `src/lib/db/schema.js:42`（`isActive: "INTEGER DEFAULT 1"`）+ `db/repos/connectionsRepo.js:46,61,97` | 连接表布尔列（读归 bool / 写 `=== false ? 0 : 1` / 可按 isActive 过滤） |
| 9router | `providers/components/ConnectionsCard.js:167,371-374` | 卡片 Toggle（size sm，title Disable/Enable）→ `PUT /api/providers/{id}` body `{isActive}` |
| 9router | `ConnectionsCard.js:103,118,121` | 停用卡片 `opacity-60` 淡化 + 状态徽标显示 `disabled` + **不显示冷却倒计时**（`isActive !== false` 门控） |
| 9router | `api/v1/models/route.js:327`、`BasicChatPageClient.js:224` | 请求路径 `filter(c => c.isActive !== false)` |
| panel | `internal/pool/pick.go:307`、`internal/pool/entry.go:87`、`internal/panel/panel.go:245` | 选号直接跳过 `disabled`；面板统计单列 disabled 数 |

差异注明：panel 的 `disabled` 含**系统自动置位**（会话失效禁用）；本仓库按用户决策只做**用户意图**语义——系统态的挂起/冷却由健康管理需求承载（`degraded_until` / `model_cooldowns`），与 `enabled` 正交。

## 设计方案

### 核心决策

| 决策 | 结论 |
|---|---|
| 字段 | `enabled: bool`，缺省 true；**不复用 `status`**（status 保留系统态语义——登录/导入写 normal，后续可扩系统态值不被用户意图污染） |
| 语义 | 用户意图；只有 `POST .../enabled` 端点写它；`upsert_account`（重登录/导入）preserve 保留 |
| 排除面 | **全链路**（用户拍板）：反代选号 + 目录选号 + 服务门控；管理端点与导出不受影响 |
| 读取口径 | 统一助手 `store.is_selectable(a)`：`status == "normal" and a.get("enabled") is not False`（缺失/null/非 false 一律视为启用——存量文件零迁移） |
| 全停用语义 | ≈「池空」：目录空（1h TTL 内仍走缓存，之后为空）、聊天请求 503（`NoAvailableAccountError`）、服务门控该池视为不可用（另一池/牛码/自定义供应商可用则服务照常） |
| 端点 | `POST /v1/platforms/{platform}/accounts/{uid}/enabled`，body `{enabled: bool}` → 更新后 AccountView；404 账号不存在 / 400 非法 body（单一端点幂等，对齐壳转发仅 GET/POST/DELETE 约束） |
| 前端 | 账号卡片头部：名称前 NSwitch（size small）；停用态卡片淡化 +「已停用」NTag；停用时不显示健康倒计时（与 9router 一致） |
| 测试 | 全 keyless：store 默认/preserve（**含 falsy 陷阱**）/选号排除/全停用 503/门控/端点 |

### 选号链落点（4 处，全部改读 `store.is_selectable`）

| 位置 | 现状 | 改后 |
|---|---|---|
| `chat.py:_normal_accounts`（反代选号基座；`_pick_account` 与 `refresh_credentials` 复用） | `status == "normal"` | `is_selectable(a)` |
| `chat.py:_availability_for._available`（503/404 区分） | 同上 | `is_selectable(a)` |
| `catalog.py:_pick_account`（目录首个 normal 号） | 同上 | `is_selectable(a)` |
| `routes.py:_any_platform_available`（服务门控「任一上游可用」） | 同上 | `is_selectable(a)` |

- 健康管理需求在 `chat.py` 选号链内再叠「非降权/非冷却」过滤（**不改** catalog/门控——其 spec 已明确），与本需求的启用过滤正交；本批实施时一次写全。
- `refresh_credentials` 经 `_normal_accounts` 天然排除停用号（停用号不参与任何请求路径，含被动刷新）；重新启用后下次选中时经 `ensure_token` 按需刷新——无手动刷新需求，口径闭环。

### 关键实现陷阱（必须覆盖）

`store.upsert_account` 现 preserve 循环为 `if key not in merged and old.get(key):`——**按真值判断**。`enabled=False` 是 falsy，直接加进名单会导致「停用号重登录后被静默启用」。改法（示意）：

```python
for key in ("checkin", "quota"):
    if key not in merged and old.get(key):
        merged[key] = old[key]
# enabled 显式判在场而非真值（False 必须被保留）
if "enabled" not in merged and "enabled" in old:
    merged["enabled"] = old["enabled"]
```

回归用例必须含：停用 → 重登录 upsert（entry 无 enabled）→ 仍为 False。

### 端点与视图

| 方法/路径 | 职责 |
|---|---|
| POST `/v1/platforms/{platform}/accounts/{uid}/enabled` | body `{enabled: bool}`（`isinstance(v, bool)` 严格——`1/0` 拒绝）；`store.update_account` 落盘；返回更新后 AccountView；账号 404（`find_account` 预检或捕 `KeyError`，与 delete 口径一致）/ 非法 body 400 |

- `base.py AccountView` 加 `enabled: bool = True`（`from_dict`：`account.get("enabled") is not False`；`to_dict` 透出）
- 文案微调：`routes.py` 的 `PlatformUnavailableError(f"{prefix} 账号不可用，请到供应商页添加账号")` 与 `chat.py` 的 `NoAvailableAccountError(f"无可用 {display_name} 账号（请到供应商页添加/重登）")` 补「或启用停用账号」指向（全停用时用户需要知道能启用）

### 前端（WorkBuddyTab.vue，双 tab 同款）

- `api.ts`：`WbAccount.enabled?: boolean` + `wbSetEnabled(uid, enabled, platform)`（POST）
- `stores/workbuddy.ts`：`setEnabled(uid, enabled)` action（busyUid 期间锁定；成功后 `applyAccount` 局部收敛）
- 卡片：`acct-head` 名称前 `<NSwitch size="small" :value="a.enabled !== false" @update:value="...">`（title「停用后请求不再走该账号」）；停用 → 卡片加条件 class 淡化（`opacity: .6`，对齐 9router `opacity-60`）+ 名称旁「已停用」NTag
- 其余操作按钮（切号/签到/配额/刷新状态/删除）**不因停用而禁用**——管理面照常
- 健康倒计时（健康 spec commit 5 实现）渲染条件必须含 `a.enabled !== false`（与 9router 门控一致）

### 修改文件

```
relay/relay/platforms/store.py                  # is_selectable 助手 + preserve falsy 修复
relay/relay/platforms/base.py                   # AccountView.enabled
relay/relay/platforms/routes.py                 # POST .../enabled + 门控 + 文案
relay/relay/platforms/workbuddy/chat.py         # _normal_accounts / _available
relay/relay/platforms/workbuddy/catalog.py      # _pick_account
relay/tests/test_platform_store.py              # 默认/preserve falsy/patch
relay/tests/test_wb_chat.py                     # 停用不选/全停用 NoAvailableAccountError
relay/tests/test_wb_catalog.py                  # 停用跳过/全停用 None
relay/tests/test_wb_routes.py                   # 端点 200/404/400 + AccountView 含 enabled 不含 token
relay/tests/test_wb_dual_realm.py（或就近）      # 门控双池用例
frontend/src/api.ts                             # WbAccount.enabled + wbSetEnabled
frontend/src/stores/workbuddy.ts                # setEnabled action
frontend/src/views/providers/WorkBuddyTab.vue   # 开关 + 淡化 + 已停用标签
relay/relay-onefile.spec、relay/relay.spec      # 无新模块则不动（store/base/routes/catalog 均已登记）
```

## 同批协调（三需求一次实施，2026-10-15 拍板）

《健康管理》《优先级》《启用停用》共享同一批改动点，**一次改完**（拆开会把同一段代码改三轮）：

| 共享点 | 一次性写法 |
|---|---|
| `store.py` preserve 名单 | 一次加全 `priority + fail_count + degraded_until + model_cooldowns + enabled`（enabled 注意 falsy 判在场） |
| `chat.py` 选号链 | `priority 分组 → 组内过滤（启用 ∩ 非降权 ∩ 非冷却）→ 游标轮询` |
| `AccountView` | 一次透出 `priority / enabled / degraded_until / model_cooldowns` |
| 账号卡片 | 一次加：优先级入口 + 启用开关 + 倒计时（停用时倒计时隐藏） |

建议实施顺序（同批内）：**健康管理（选号链主体）→ 优先级（分组叠入）→ 启用停用（最薄：过滤器铺开 + UI 开关）**；或视批次合并为同一批提交。三份 spec 的验收清单分别勾。

## 被否决的备选

- **复用 `status` 字段**（`normal`/`disabled`）：否决——用户意图与系统态混用；健康管理/未来自动逻辑需要独立承载系统语义；读点分散无统一助手。
- **仅反代选号排除**：否决（用户拍板全链路）——目录与门控漏出会造出「模型看得见、请求报账号不可用」或「全停用后服务仍显示可用」的不一致态。
- **删除账号代替停用**：否决——丢凭证与签到/配额缓存，恢复需重登；停用是可逆轻操作。
- **写盘时写死 `enabled: true`**：不做——读取侧缺省语义足够（存量文件零迁移），避免无意义写放大。

## 已知边界

- 停用**不清理**该号的健康惩罚与冷却状态（`fail_count`/`degraded_until`/`model_cooldowns` 保留）；重新启用后按原状态继续参与——与 9router 同口径（停用不清状态）；清零属健康管理范畴，不在本期。
- 导出/导入**不携带**停用态：cockpit 格式无该概念、9router `isActive` 属其自身连接治理——回导按默认启用。**已知且接受**（如日后要携带，需改两格式 + 两侧解析，另议）。
- 停用号仍出现在账号列表与统计里（要能再启用）；不产请求，计费口径无变化。
- 全停用时目录走缓存（1h TTL 内仍列出模型），TTL 后为空；请求则即时 503（门控不依赖目录缓存）。
- 优先级（priority）**不因停用而重排**：停用号仍占其优先级组，只是不参选；启用后自动回原组。
- 管理端点（签到/配额/IDE 切号/导出）忽略 `enabled`（照常执行）——本就是用户显式对单号操作。
- `enabled` 落盘在账号行（`platform_workbuddy.json`），用户可手改文件——不做防护（桌面单用户口径，同现状）。

## 实施步骤（commit 序列）

| # | commit | 验证 |
|---|---|---|
| 1 | `feat: 账号池启用字段与全链路选号排除`（store + base + chat + catalog + routes 门控 + 测试） | pytest 全量 + ruff；默认（无字段）行为逐请求等价回归 |
| 2 | `feat: 账号启用停用端点与卡片开关`（routes + api/store/tab + 路由测试） | pytest + format:check + build |
| 3 | `chore: spec 台账收尾` | 打包冒烟（无模块级新增则仅产物验证）+ 台账回填 |

> 与健康管理/优先级同批时的合并注记：三项一次改 preserve 名单与选号链；本 spec 的 commit 1 与健康 commit 1/2、优先级 commit 1 合并为同一批次提交亦可（按实施会话安排）。

## 验收

- [ ] `pytest tests/ -x -q` 全绿，含：
  - [ ] store：缺省（无 `enabled` 字段）视为启用；`enabled=False` upsert 重登录后保留（**falsy 陷阱回归**）；`update_account` patch
  - [ ] chat：单号停用 → 永不被选（MockTransport 断言上游只收启用号的请求）；全停用 → `NoAvailableAccountError`（503 语义）
  - [ ] catalog：停用号被跳过；全停用 → `_pick_account` None（目录空）
  - [ ] routes：端点 200/404/400（含 `enabled: 1` 拒绝）；AccountView 含 `enabled` 且不含 token（照旧断言）
  - [ ] 门控：全池停用 → `_available` False / `_any_platform_available` 跳过该池（双池另一池正常时服务仍可用）
- [ ] ruff + `npm run format:check` + `npm run build` 通过
- [ ] 手动验收（待用户）：停用 A 后聊天请求只走 B（日志可见选号）；全停用 → 聊天 503 / 模型列表按目录口径收敛；重新启用恢复；签到/配额/切号/导出不受停用影响；停用后重登录仍保持停用
