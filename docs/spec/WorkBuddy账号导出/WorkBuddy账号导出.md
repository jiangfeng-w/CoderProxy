# WorkBuddy 账号导出

> **状态**：已实现（2026-10-12；真机只读重建与 9router 官方导出 10 段逐字节对拍一致；用户验收通过：导出功能正常）
> **优先级**：核心
> **来源**：用户需求「有了导入，是不是也应该有导出功能」；由 [WorkBuddy账号多方式导入](../WorkBuddy账号多方式导入/) 自然延伸（导入/导出一对）
> **隐私声明**：本文档不含任何真实账号、凭证、token。

## 背景 / 目标

账号池（`platform_workbuddy.json`）目前只能导入、不能导出。本期补导出，兼容两种目标格式（用户拍板范围）：

1. **cockpit-tools 兼容格式**（主格式）：`workbuddy_accounts_*.json` 形态的纯数组，一份文件两用——CoderProxy 自身备份/迁移 + 直接喂给 cockpit-tools 导入（其 `import_from_json` 第 1 级原生识别，导入是逐条 upsert 增量，无风险）。
2. **9router 完整备份**（读取合并流）：读取本机 9router 的 SQLite 库 → 取全部数据段（settings/apiKeys/proxyPools/…原样）→ 把选中账号合并进 `providerConnections` → 输出**完整可恢复的备份文件**。

**用户决策**：
- 格式范围 = 仅 cockpit + 9router（不做 CPA/sub2api——Codex 生态格式，CodeBuddy 号无对应平台位）
- 9router 形态 = **读取合并流**（用户提议「导出前读取 9router 配置」）：能读到库 → 导出完整备份 JSON；读不到 → 降级为**剪贴板条目片段**（用户手动合并）
- **合并策略 = ①保留原样**（用户拍板）：只增/只更新勾选导出的账号，9router 里其它连接（含未勾选的 codebuddy-cn 号）原样保留，绝不因一次导出丢号
- 自动备份（定时导出）暂不做（用户：「自动备份以后再说」）

## 关键机制考证（2026-10-12）

### 9router 导入是「全量覆盖」——这决定导出形态

`src/lib/db/index.js` `importDb`（:96-162）：导入前无条件 `DELETE` 七类数据，再按 payload 里有什么重建什么；缺段静默清空（settings 有 `mergeWithDefaults` 兜底不崩，但 apiKeys/proxyPools 等实打实丢）。**因此绝不能把「只含账号」的瘦文件喂给 9router 恢复**——会清掉 API key 与全部设置。完整备份才是唯一安全的恢复载体（这正是读取合并流的意义）。

### 9router 库可直接只读读取（实测）

- 路径：`%APPDATA%\9router\db\data.sqlite`（Windows；`DATA_DIR` 常量 `src/lib/dataDir.js`），WAL 模式
- 实测：`sqlite3.connect("file:...?mode=ro", uri=True)` 在 9router 运行中成功读出全部表（settings 1 行 / providerConnections 1 行 codebuddy-cn / apiKeys 1 行）；只读连接取一致快照，不写不锁
- 表列：`providerConnections(id, provider, authType, name, email, priority, isActive, data JSON, createdAt, updatedAt)`；`data` 字段实测清单：`accessToken/refreshToken/expiresAt(ISO 串)/expiresIn/testStatus/backoffLevel/errorCode/lastError/lastErrorAt/rateLimitedUntil/modelLock_*`
- 导出函数 `exportDb`（`db/index.js:65-90`）语义已读透：各表行 → `{...data, id, provider, ...}` 扁平化 + `kv` 四个 scope；**密码 hash 在 settings 里原样导出**（`exportSettings` 即 `readRaw`），故完整备份含登录凭据——与 9router 自身导出行为一致，属预期
- 本机状态：9router 正在运行（:20128），库存在

### cockpit-tools 导出格式（实物验证）

用户提供的 `workbuddy_accounts_2026-10-11.json` 实测字段（19 个，Option 缺失即不出现）：
`id, email, uid, nickname, access_token, refresh_token, token_type, expires_at(毫秒), domain, dosage_notify_code, payment_type, quota_raw, auth_raw, profile_raw, usage_raw, status, usage_updated_at(秒), created_at(秒), last_used(秒)`

- **id 规则实测确认**：`workbuddy_` + `md5(uid)`（32 hex）；用真实文件反推验证一致
- cockpit 侧导入（`import_from_json`）是 upsert：id 存在则覆盖、保留 created_at/tags——round-trip 最保真的关键就是复刻 id 规则
- 本项目账号池无 `auth_raw/profile_raw/usage_raw/quota_raw/dosage_*` 字段（导入时已忽略）→ 导出**不产出**这些段（cockpit 侧解析容忍缺失，走「新账号重建」分支而非「恢复」分支；uid/email 相同的账号在 cockpit 里仍是同一个，符合预期）

## 设计方案

### 核心决策

| 决策 | 结论 |
|---|---|
| 实现位置 | `relay/relay/platforms/workbuddy/exports.py`（与 imports.py 并列，解析层复用其 `_expiry_ms`/`decode_jwt_claims` 等） |
| 9router 读数 | 只读打开 SQLite（stdlib `sqlite3`，uri `mode=ro`）；**不依赖 9router 运行、不需密码**；读不到（无库/无表）→ 降级剪贴板片段 |
| 9router 写备份 | 输出完整备份 JSON（与 `exportDb` 同构），`providerConnections` 合并选中账号（拍板①保留原样）；**写文件只落 CoderProxy 侧（返回给前端下载），绝不写 9router 库** |
| 合并匹配 | 目标连接按 JWT `sub`（=uid）匹配：已有 codebuddy-cn 连接且其 token 的 sub 相同 → 更新 token 组（accessToken/refreshToken/expiresAt/expiresIn）+ updatedAt，保留其运行态字段（testStatus/backoffLevel/errorCode/lastError*/rateLimitedUntil/modelLock_*）；无匹配 → append 新连接（新 UUID、`provider:"codebuddy-cn"`、`authType:"oauth"`、name=昵称、priority=池内序、isActive=true、同构 data） |
| cockpit 导出 | 数组（多选）或单对象（cockpit 导入两者都吃）；字段按实测清单，缺失字段不输出；`created_at/last_used` 秒、`expires_at` 毫秒 |
| 端点 | `POST /v1/platforms/workbuddy/accounts/export`（body `{uids, format: "cockpit"|"9router"}`）→ 返回文本内容 + 建议文件名 + 实际形态（完整备份/片段）；方法约束 POST（壳只放行 GET/POST/DELETE） |
| 前端 | 账号卡片复选框 + 顶部「导出」按钮 → 弹窗选格式 → cockpit：下载文件；9router：读到库→下载完整备份，读不到→复制片段+提示 |
| 安全 | 导出内容含明文 token（本地文件，与 cockpit-tools/9router 自身导出同风险模型）；不进日志；响应不经 AccountView（本就含 token，属导出语义） |

### 新增/修改文件

```
relay/relay/platforms/workbuddy/exports.py     # 新：build_cockpit_export / load_9router_db /
                                               #   merge_9router_backup / build_nine_router_fragment / export_accounts
relay/relay/platforms/routes.py                # +1 端点 export
relay/tests/test_wb_exports.py                 # 新：cockpit 字段/round-trip、9router 合并（①保留原样）、
                                               #   降级片段、无库报错、路由 mock
frontend/src/views/providers/WorkBuddyTab.vue  # 复选框 + 导出弹窗（格式二选 + 下载/复制）
frontend/src/stores/workbuddy.ts               # selectedUids 状态 + exportAccounts action
frontend/src/api.ts                            # wbExport 类型与调用
docs/spec/WorkBuddy账号导出/探针-9router只读读取.py  # 真机只读探针（读库结构，不写）
```

### 导出契约（exports.py）

**`build_cockpit_export(accounts) -> str`**：每账号 →
```json
{"id": "workbuddy_<md5(uid)>", "email": "<nickname 兜底>", "uid", "nickname",
 "access_token", "refresh_token", "token_type": "Bearer", "expires_at": <毫秒>,
 "domain", "status": "normal", "created_at": <秒>, "last_used": <秒>}
```
（账号池无的字段——quota_raw/usage_raw 等——不输出）

**9router 合并流**：
1. `load_9router_db()`：候选路径 `%APPDATA%\9router\db\data.sqlite`（env `RELAY_9ROUTER_DB` 覆盖）→ 只读连接 → `exportDb` 同构重建（settings 单行 + 六表 + kv 四 scope）→ 返回 dict；无库/无表 → `NineRouterUnavailable`
2. `merge_9router_backup(backup, selected, all_accounts)`：遍历 `providerConnections`，对每条 codebuddy-cn 连接解其 accessToken 的 JWT sub；命中选中账号 → 替换 token 组 + updatedAt；未命中的一律原样；选中但无匹配 → append 新连接
3. 新旧账号的 uid 来源：池内优先 `uid` 字段（本机导入的来源），无则 JWT sub（token 导入的来源）

**降级片段**：`build_nine_router_fragment(accounts)` → 数组（每条同 providerConnections 新连接形态，含 `id` 新 UUID）——前端复制到剪贴板，用户手动贴进备份的数组。

### 端点契约

`POST /v1/platforms/workbuddy/accounts/export`
- body：`{uids: string[], format: "cockpit" | "9router"}`（uids 空 = 全部）
- 响应：`{format, mode: "full_backup" | "fragment" | "cockpit", filename, content, note}`
  - cockpit → `mode:"cockpit"`，content = 数组或单对象 JSON 文本
  - 9router 读到库 → `mode:"full_backup"`，content = 完整备份 JSON，note 说明「含 9router 全部配置」
  - 9router 读不到 → `mode:"fragment"`，content = 条目数组 JSON，note 提示手动合并
- 未知 uid → 404；format 非法 → 400；9router 库损坏 → 500（detail 中文）

### 单测口径（keyless）

- cockpit：字段清单与实物对齐、id=md5(uid)（用真实文件的 uid/id 对锁定）、缺失字段不输出、单/多账号形态、**round-trip**（导出文本喂回 `parse_import_text` 得同 uid/同 token）
- 9router 合并：①保留原样（未选中连接逐字节不变）/ 选中命中更新（运行态字段保留、token 组替换）/ 选中无匹配 append（新 UUID、data 同构）/ updatedAt 更新
- 降级：无库 → fragment 形态、note 文案
- 只读属性：monkeypatch 记录调用，断言未执行任何写 SQL（连接以 `mode=ro` 打开）
- 路由：两格式分支、404/400、401 门禁

## 实施步骤（commit 序列）

| # | commit | 验证 |
|---|---|---|
| 1 | `feat: WorkBuddy 账号导出构造层`（exports.py + test_wb_exports.py） | pytest；round-trip 绿 |
| 2 | `feat: 账号导出端点`（routes +1 + 路由测试 + 打包登记） | pytest 全量 + 源码起服冒烟 |
| 3 | `feat: 供应商页账号导出 UI`（复选框 + 导出弹窗） | format:check + build |
| 4 | `chore: 9router 只读探针 + spec 台账` | 探针真机 PASS |
| 5 | 手动验收：cockpit 文件导出→ cockpit-tools 导入（或本应用回导）；9router 完整备份导出→ 9router 恢复（号更新、apiKeys/设置留） | 用户手动；小号优先（规则 5） |

## 已知边界

- **绝不写 9router 库**：只读打开，产出文件由用户在 9router UI 手动恢复（避免两应用并发写 SQLite）
- 9router 恢复是整库覆盖：完整备份恢复会回到「导出那一刻的 9router 全量状态」——未选中的连接保留，但导出之后你在 9router 里新做的改动（如新增了别的连接）会被这份文件覆盖掉，属用户恢复时的自然语义
- cockpit 缺 raw 字段：cockpit 侧导入这些号走「新账号重建」而非「原账号恢复」（id 相同仍会 upsert 到同一 id，功能上等价）
- `expires_at` 缺失（token 非 JWT 且未刷新过）→ 输出 0，cockpit 侧不接受 0？——**实测待验证**：cockpit `json_object_i64_field` 对 0 的处理在导入测试覆盖（无 uid 场景已有降级主键），不阻塞
- 导出含明文 token：仅本机文件与剪贴板，UI 需提示；不进日志
- 9router 不在默认路径（自定义 `DATA_DIR`）→ env `RELAY_9ROUTER_DB` 覆盖；探测不到走降级，不阻塞

## 验收

- [x] `pytest tests/ -x -q` 全绿（542 passed；test_wb_exports 16 例含实物 id 向量/round-trip 回导/只读不改库断言/合并三分支；test_wb_routes +5 例）
- [x] ruff（新文件）+ `npm run format:check` + `npm run build` 通过
- [x] 源码起服冒烟：cockpit 导出内容正确（token 保留、id=md5(uid)）；9router 导出读到真实配置（10 段 + apiKeys + settings），合成号 append、原有连接原样保留（拍板①验证）、400/404 映射
- [x] 真机只读探针 PASS（`探针-9router只读读取.py`）：只读重建 10 段 → 与 9router 官方导出 API 逐字节一致；只读不改库
- [x] 打包登记：两个 spec hiddenimports 加 `relay.platforms.workbuddy.exports`；onefile 产物冒烟
- [x] 手动验收（2026-10-12 用户确认）：导出功能正常（弹窗勾选 + 系统另存为；cockpit 兼容格式与 9router 完整备份导出）
- [ ] 手动验收（可选，待用户）：备份文件在 cockpit-tools / 本应用回导、9router 恢复的成功落地（恢复为 9router 整库覆盖语义，用户按需自行执行）
