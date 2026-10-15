# WorkBuddy 反代账号池健康管理

> **状态**：方案已定稿（2026-10-13 立稿、2026-10-14 定稿；设计已按参考实现逐条核对并修正原方案 4 处事实错误）；**2026-10-15 复核增补**：逐条对照 panel/9router 两参考后补 3 处缺口（11140 双语义、12153 会话失效、内容类零惩罚完整性）+ 1 处数值取向（6004 封顶改「尊重上游时间」——用户拍板）+ 1 处可选增补（前端限流倒计时）；见 §7
> **优先级**：核心
> **来源**：《WorkBuddy 反代优化方案》P0-2（软降级）+ P0-3（软冷却）+ P2-6（负缓存）合并立项——三者共享同一批改动点（store 字段/preserve、chat 轮换链、选号过滤链、routes 映射），拆开会导致同一段代码反复改两次
> **依据**：workbuddy2api-panel `internal/pool/degrade.go`（连败降权）、`state.go`（`NoteSuccess` / 13 号误杀教训）、`cooldown.go`（`CooldownSoftForModel` 模型级 / `BlockModelBackoff` 负缓存 6h→24h）、`transition.go`（签到只解冻硬冷却）、`internal/server/handler.go`（喂入口纪律）；9router `executors/codebuddy-cn.js`（6004 恢复时间解析）、`services/accountFallback.js`（退避数值 base 2s / max 5min）

## 背景 / 目标

现状（2026-10-13 核对）：

| 问题 | 坐标 |
|---|---|
| 选号只按 `status == "normal"` 过滤；上游 5xx / 网络失败 / WAF 后该号仍被持续选中，每请求浪费一次尝试 | `chat.py:231-235` `_normal_accounts` |
| 6004 只映射 429 透传给 agent，账号/模型不进冷却——下一请求还打同号，agent 侧连续限流 | `chat.py:353-354` `parse_error` |
| 11102 只映射 404——同号同模型组合反复白试 | `chat.py:355` |

另一面（panel 血泪教训，勿踩）：

- 「一次失败就永久杀号会误杀健康账号」（P0-1 侦察：13 个 disabled 号全部 refresh 成功，是历史误判受害者）→ **只挂起、不杀号**
- 「降权期内再次达阈不延长——防用户重试把降权越堆越厚」
- 「带权威分类的错误（429/5xx/12153/11140/11102）各有知道原因的惩罚，不喂连败——再喂是重复计罚」
- **6004 是模型级限流**（该后端该模型被限流，切模型即可能可用）——panel 写模型级冷却表（`CooldownSoftForModel`，切模型豁免），**不**停账号
- 签到/余额恢复只解冻「余额耗尽」硬冷却，**不清软限流**（panel `transition.go` 实测备注：若签到清软冷却，「6004 台账被抹后撞限号被误判健康、重新选中再撞 429，全池冷却保护形同虚设——两号池实测复现」）

目标：账号池「坏号自动挂起、好号快速恢复、冷却按模型裁决」，全部字段级最小改动，默认行为可回归。

## 设计方案

### 1. 账号行新字段（store）

```
fail_count: int = 0            # 连续失败计数（瞬时；chat 成功清零）
degraded_until: int = 0        # 连败挂起截止（ms 时间戳；0 = 正常）
model_cooldowns: dict = {}     # {model: {"until": ms, "reason": "6004"|"11102", "hits": n}}
```

- `upsert_account` 的 preserve 名单（现 `checkin`/`quota`）**追加**这三项（重登录/导入不清惩罚与冷却）
- 与 panel 差异注明：panel 的 `modelCooldowns` 是运行态（重启清零）；本仓库统一落盘（store 是唯一状态载体，重启保留更保守）
- **一次性合并**《WorkBuddy 反代账号优先级》的 `priority` 字段——同一次改 preserve 名单，避免两次改（见 §5 协调）

### 2. 连败软降级（P0-2）

```
note_failure(uid)：fail_count++；达阈值 3 → degraded_until = now + 5min、fail_count=0
note_success(uid)：fail_count=0、degraded_until=0
```

- 降权期内再次达阈**不延长**（对齐 panel `degrade.go` 语义；计数清零，到期放行后需重新连败满阈值才再降）
- 参数为自有取值（3 连败 / 5min；panel 默认 5 连败 / 10min、上限 2h）——桌面场景快速挂起/快速恢复，不署 panel 口径
- `_normal_accounts` 追加过滤：`degraded_until <= now`

**喂入口纪律**（只喂「不知道原因的失败」）：

| 错误类型 | 动作 |
|---|---|
| 401/403（凭证被拒） | 换号（现状）+ **不喂**（凭证问题由换号/重登处理） |
| 上游 5xx / 网络错误（**未 yield 过帧**） | 换号一次 + `note_failure`（面板：5xx 走熔断；本仓库无熔断器，简化为连败，注明差异） |
| 上游错误（**已 yield 过帧**） | **绝不换号**（本项目进流纪律：防重复产出/重复计费）+ `note_failure` |
| 业务码错误（11148/11128/11101 等） | 不喂（请求问题不是账号问题） |
| 6004 / 429 | 不喂连败（另走 §3 模型冷却） |
| 11102 | 不喂连败（另走 §3 负缓存） |
| **11140（`request illegal`，账号级授权）** | 换号重试、**不喂**（凭证/授权问题，由换号/重登处理）；全池皆如此 → 503 引导重登（同 401 全池语义） |
| **11140（`rate-limiting requests` 等限流文案）** | 按 §3 模型级冷却处理（**11140 不可按 code 一刀切**——panel 警示：同 code 承载授权与限流双语义，按 msg 关键词分流） |
| **12153（`Offline user session not found`，会话失效）** | 换号重试、不喂；全池 12153 → 503 引导重登；**不做连续计数禁用**（不杀号沿用 401 哲学） |

- 实现落点：`chat.py` `_frames_with_rotation` 加 `yielded` 标志；`_stream_once` 抛出的错误按上表分流
- `note_success` 时机：`_stream_once` 正常收流结束（含 0 帧的干净结束；空流由 routes 层独立重试预算处理）
- **refresh 成功不清降权**（勘误：panel `ClearSessionDead` 只清 12153 连续计数，不能外推为「清降权」；且本项目 refresh 只发生在账号被选中时，「选中→刷新成功→清降权」会让坏号循环重生）

### 3. 模型级冷却 + 负缓存（P0-3 + P2-6 合并）

**6004（模型级限流）**：

- 识别：账号 + 模型已知时（`_frames_with_rotation` 持有 `account` 与 `self._model`）
- 恢复时间解析（对齐 panel `ParseRateReset` + 9router 正则）：CN `将在 YYYY-MM-DD HH:MM:SS 重置` / EN `will reset at ... UTC+8`，两种形态同一时区（UTC+8）解析
  - 解析成功 → `model_cooldowns[model].until = 恢复墙钟`（**尊重上游时间**，宽松封顶 12h 仅防脏数据——2026-10-15 用户拍板修订：原「封顶 2h」为 panel 多连接兜底口径，桌面单池号少、撞墙成本高（直接 429 给 agent），且上游给的是权威时间；今天实测撞出 8h14m 重置时间，截 2h 会每 2h 重复撞墙）
  - 解析失败 → 有界退避：`60s × 2^(hits-1)`，封顶 15min（**自有取值**——9router 为 2s 起 / 5min，panel 为 600s 起 / 2h；原方案「9router 口径」表述有误）
  - 冷却期内再次触发不翻倍/不延长（对齐 panel「兜底探测不再把冷却越堆越厚」）
- 注：6004 只在「模型已知」时写模型级冷却；模型未知的兜底按账号级处理（本期聊天链路模型恒已知，兜底可不实现）

**11102（模型不可用负缓存）**：

- 写 `model_cooldowns[model]`（reason=`11102`），TTL **10min**（自有取值；panel 为 6h 起指数至 24h——桌面场景模型可用性变化快，取短 TTL，注明差异；原方案「TTL 10min（panel 语义）」表述有误）
- 命中时对 `(uid, model)` 组合跳过该号；全池该模型都命中 → 直接 404（不消耗请求）
- 成功一次即清该模型的 `11102` 条目（6004 条目不清——各有恢复语义）

**选号链**（`_pick_account`）：`normal ∩ 非 tried ∩ 非降权 ∩ 非（uid, model）冷却` → 取 `min(priority)` 组 → 组内游标

### 4. 全池挂起 → 429 + Retry-After

- 新增 `AllAccountsSuspendedError(retry_after_s)`：池中有 normal 号但**全部**处于降权/冷却（且非 tried 耗尽、非凭证被拒场景）→ routes 映射 **429 + Retry-After**（取全池最近到期时刻，秒）
- 现有 `NoAvailableAccountError`（无号 / 全部凭证被拒）维持 503 + Retry-After 不变
- 管理端点（签到/配额/IDE 切号）**不受**降权/冷却影响（用户显式操作单号，照常执行）

### 5. 与《WorkBuddy 反代账号优先级》的协调（同批实施）

- priority spec 已定稿（2026-10-11），不重复设计；本批实施顺序建议：**本需求先行，priority 紧随**（或同一 PR）
- `store.py` preserve 名单一次性合并 `priority + fail_count + degraded_until + model_cooldowns`（避免两次改）
- `_pick_account` 合并后语义：先按 priority 分组 → 组内过滤冷却/降权 → 游标轮询；**高优组全冷却/全降权时落到下一组**（可用性优先于优先级——在 priority spec 上补一句此决策）
- `AccountView`（`base.py`）加 `priority` 时顺带透出 `degraded_until` / `model_cooldowns`（只读；**前端倒计时展示已拍板必做**，见 §7-E）
  - `model_cooldowns` 透出时**已过期条目先惰性清理**（对齐 9router `clearAccountError` 的过期清理与 `pruneExpiredModelCooldowns`），保证前端拿到的"最早到期"语义干净

### 6. 被否决的备选

- **6004 写账号级冷却**（原方案 P0-3 口径）：否决——6004 是模型级语义（panel 切模型豁免），账号级冷误会停掉该号其他可用模型；与 11102 共用 (uid, model) 键设计更内聚
- **熔断器**（panel `NoteError` 指数退避 + breaker）：不做——桌面单机场景连败降权已覆盖；引入熔断增加状态面
- **12153 连续计数 / 禁用**：不做——本项目对 401 仅换号不杀号，无 13 号误杀风险，`NoteSessionDead` 机制无对应物
- **签到解冻清冷却**（原方案口径）：否决——见背景 panel 实测教训；本项目签到与冷却无联动

### 7. 复核增补（2026-10-15，对照 panel/9router 逐条复核后）

**A. 11140 双语义分流**（§2 喂入口表已并入）：`msg` 关键词判分流——
`request illegal`（含）→ 账号级授权类（换号、不喂、全池 503 引导重登）；限流文案（`rate-limiting requests` 等）→ §3 模型级冷却。
依据：panel `handler.go:1148-1153`（request illegal → 账号授权故障）+ `client.go:129-134`（警示：11140 不能按 code 判定，同 code 承载模型级限流文案）。

**B. 12153 会话失效处置**（§2 喂入口表已并入）：换号重试、不喂连败、全池 503；不做连续计数禁用（panel 的 `NoteSessionDead` 阈值机制可省——本项目无「一次失败杀号」路径，无需防误杀计数）。
依据：panel `state.go` NoteSessionDead/ClearSessionDead 设计；9router 无特判（其错误表把相关文案归瞬态冷却）。

**C. 内容/授权类「零惩罚」的完整性**：不喂连败的判定按 **msg 关键词匹配（中英双形态）**，不按 code 死判——对齐 panel `contentBlockedMarkers`（`blocked by security policy` / `unapproved channel` / `illegal api invocation`）。上游换文案时最差退化为「未知错误进连败」，不会漏罚到异常路径。

**D. 6004 封顶改「尊重上游时间」**（§3 已修订）：解析成功锁满上游墙钟（宽松封顶 12h 防脏数据）；解析失败才走 15min 有界退避。用户 2026-10-15 拍板。

**E. 前端限流倒计时展示（2026-10-15 用户拍板必做，对齐 9router）**：

用户要求「和 9router 一样显示出来，可以获取到限流解除时间」。9router 实际做法（已读源码）：

- **数据源**：连接记录上的扁平字段 `modelLock_${model}`（ISO 串）；前端 `ConnectionRow.js:91-112` 取**所有 `modelLock_*` 中最早且未到期者**作为 `modelLockUntil`，`useEffect` 每秒重算 `isCooldown`（到期自动消失、状态回 active）
- **展示**：`CooldownTimer.js`（42 行）——`⏱ {remaining}` 橙色等宽小字，每秒 tick；格式化 `Xs`（<60s）/ `Xm Ys`（<1h）/ `Xh Ym`（≥1h）；到期返回 null 不渲染
- **触达**：只在 `isCooldown && isActive !== false` 时显示（禁用号不显示倒计时）

本仓库落点（双域通用，CN/国际同款）：
- 后端：`AccountView` 透出 `degraded_until`（ms）+ `model_cooldowns`（{model: {until, reason}}）——前端直接算最早到期
- 前端：`WorkBuddyTab.vue` 账号卡片 meta 行加倒计时组件（`<CooldownCountdown :until>` 每秒 tick，格式化同 9router 口径：`Xs`/`Xm Ys`/`Xh Ym`，`⏱` 前缀）；**取 `degraded_until` 与 `model_cooldowns` 全部未到期条目的最早者**——语义即「该号最早何时可能恢复出场」
- 展示文案区分两类：连败挂起（降权）显示 `⏱ 挂起 Xm`；模型冷却显示 `⏱ 限流 Xh Ym`（含 reason 6004/11102 tooltip「最晚恢复：YYYY-MM-DD HH:MM:SS」）
- 到期自动消失（每秒重算，与 9router 一致）；账号卡片其余操作（删除/刷新配额等）不受影响

**复核已对齐项（无需改动）**：连败降级语义（不延长/成功清零/refresh 不清）、模型级 6004 豁免、11102 负缓存与成功只清 11102、请求级 4xx 不罚（9router 同哲学）、落盘/preserve/priority 协调。

**明确不抄项（维持原判）**：熔断器（5xx 并连败）、积分保底/成本分层/探索权重/在途限流（panel 多租户优化，桌面不适用）、12153 连续禁用计数。

## 已知边界

- 降权/冷却字段落盘在账号行（`platform_workbuddy.json`），用户可在文件里手改——不做防护（桌面单用户场景，与现口径一致）
- `catalog.py` 目录选号（首个 normal 号）与 `_available` 探测**不做**冷却过滤（目录有 5min 负缓存兜底；为最小改动）
- 6004 恢复时间正则覆盖 CN/EN 两形态；未知形态走有界退避（绝不臆造时间）
- 全池冷却的 Retry-After 取「最近到期」；若全池降权（无冷却）则 Retry-After = 最近降权到期

## 验收

- [ ] 连败：3 连败出池 → 第 4 个请求不再选该号（MockTransport 注入 5xx）；`note_success` 后回池；降权期内不延长
- [ ] 已 yield 帧的上游错误 → 不换号直接抛（进流纪律回归）+ 喂连败
- [ ] 6004 两种文案（带恢复时间 / 不带）→ 冷却时长断言；冷却期内选号跳过该 (uid, model)；**同账号其他模型不受影响**
- [ ] 11102 → 负缓存 10min；命中即跳过；成功清除
- [ ] 全池冷却 → 429 + Retry-After 响应断言（含取值正确性）
- [ ] preserve：重登录不重置降权/冷却/priority
- [ ] 现有 `test_wb_chat.py` / `test_platform_store.py` 零回归
- [ ] （与 priority 同批时）默认全 1 分组时选号序列与现状逐请求等价
- [ ] **11140 双语义**：`request illegal` → 换号+全池 503；限流文案 → 模型冷却（两形态各一用例）
- [ ] **12153**：换号重试 + 全池 503（不喂连败断言）
- [ ] **6004 尊重上游时间**：解析出远未来重置时间（如 8h）→ 冷却锁满（宽松封顶 12h 断言）
- [ ] **前端倒计时（对齐 9router）**：AccountView 透出 `degraded_until`/`model_cooldowns`；前端每秒 tick、取最早未到期条、格式化 `Xs`/`Xm Ys`/`Xh Ym`、到期自动消失；挂起/限流两类文案可区分；format:check + build 绿

## 实施步骤（commit 序列）

| # | commit | 验证 |
|---|---|---|
| 1 | `feat: 账号池健康字段与连败软降级`（store + chat 喂入口 + 测试） | pytest；3 连败/回池/不换号三用例 |
| 2 | `feat: 模型级冷却与 11102 负缓存`（chat parse_error/轮换链 + 测试） | pytest；冷却时长/跳过/清除用例 |
| 3 | `feat: 全池挂起 429 映射`（routes + 测试） | pytest + 冒烟 |
| 4 | `chore: priority 合并落地或交接注记`（视批次安排） | 全量回归 |
| 5 | `feat: 前端限流倒计时（对齐 9router CooldownTimer）`（AccountView 透出 + WorkBuddyTab 倒计时组件） | format:check + build；手动验收倒计时真实倒走 |

## 改动文件清单

```
relay/relay/platforms/store.py               # 字段 + preserve + note_outcome 帮助函数
relay/relay/platforms/workbuddy/chat.py      # 喂入口 + 选号过滤 + 冷却写入/读取 + 新错误类
relay/relay/platforms/base.py                # AccountView 透出（可选）
relay/relay/routes.py                        # AllAccountsSuspendedError → 429 + Retry-After
relay/tests/test_wb_chat.py / test_platform_store.py / test_wb_routes.py
docs/spec/WorkBuddy反代账号优先级/           # 补「同批协调」注记（不改原设计）
```
