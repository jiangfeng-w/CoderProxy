# WorkBuddy 反代账号优先级

> **状态**：方案已定稿（2026-10-11；评估结论：好做——改动集中三处，无新依赖/无加密/无进程管理，比 IDE 切号量级小一半以上）
> **优先级**：交付
> **来源**：用户需求「反代 workbuddy 可以给每个账号设置优先级，从 1 开始，数字越小优先级越高，先把优先级高的账号用完；同一优先级内保持现有轮询；默认所有账号优先级都是 1」
> **前置**：「WorkBuddy聊天反代与多平台聚合」的账号池轮询选号 + 「WorkBuddy多账号登录与签到」的多账号存储

## 背景 / 目标

反代 WorkBuddy 聊天目前对所有 normal 账号**纯轮询**（`chat.py:_pick_account` 全局游标），积分消耗雨露均沾，无法指定「先用哪个号」。本期给账号加**优先级**：

- 每账号可设 `priority`（整数，**从 1 开始，越小越优先**，默认全部 1）
- 反代选号：**先选最小 priority 组**（组内保持现有全局游标轮询）；高优组全部凭证被拒（单请求内 tried 耗尽）才降级到下一优先级组
- 默认全 1 ⇒ 行为与现状完全等价（全池一个组 = 现有轮询），零回归

**用户决策**：
- 优先级从 1 开始、数字越小越高、默认 1
- 同优先级内轮询（现状口径不变）
- 「先用完优先级高的」= 只要高优组有可用号就一直用它（触发降级的条件是**凭证级失败 401/403**，见「已知边界」对「积分耗尽」的说明）

## 评估（用户要求先行评估）

| 维度 | 结论 |
|---|---|
| 存储改动 | `platform_workbuddy.json` 账号行加 `priority` 字段（int，缺省 1）；`upsert_account(preserve=True)` 的 preserve 名单加 `priority`（重登录/导入不带该字段时保留用户设置）；`update_account` 局部 patch 已有，直接复用 |
| 选号改动 | `chat.py:_pick_account` 现有逻辑前加一步「取最小 priority 组」（~5 行）；`_normal_accounts` 的 uid 排序保证组内顺序稳定，游标轮询不均匀问题不引入 |
| 端点 | 新增 1 个（POST，壳转发约束内）；`AccountView` 透出 `priority` |
| 前端 | 账号卡片 meta 行加「优先级 N ›」可点击（对齐配额明细的交互形态），小弹窗 NInputNumber 设置 |
| 测试 | 全 keyless：store 默认值/preserve、chat 分组选号（等价回归 + 跨组降级）、路由、AccountView 不含 token 断言照旧 |
| 风险 | 低：默认值让现网行为零变化；无新外部契约 |

**结论：好做。** 预计 3 个功能 commit（store+选号 / 端点+前端 / 收尾）。

## 设计方案

### 核心决策

| 决策 | 结论 |
|---|---|
| 字段位置 | 通用 `store.py` 层（多平台账号存储通用化——trae/qoder 未来接入天然继承），`priority: int` 缺省 1 |
| 选号算法 | `_pick_account(tried)`：normal + 排除 tried 后，取 `min(priority)` 组 → 组内 `_next_cursor() % len(tier)` 轮询；组间顺序天然实现「先用高优」 |
| 降级语义 | 单请求内：高优组号逐个 401/403 进 `tried`，组空自动落到次优组（现有 `_frames_with_rotation` 循环零改动，`_pick_account` 内部分组即可） |
| 跨请求语义 | 高优组凭证恢复（refresh 成功/重新可用）后，下一请求自动回到高优组——无需持久化「降级状态」 |
| 设置端点 | 通用 `POST /v1/platforms/{platform}/accounts/{uid}/priority`（body `{priority: int>=1}`）——优先级是账号池概念不做 WB 专用；`store.update_account` 落盘 |
| AccountView | 加 `priority` 字段透出（不含 token 的安全边界不变） |
| 前端 | 账号卡片 acct-meta 行「优先级 N ›」可点击 → 小 NModal（NInputNumber min=1 max=99 + 确定）；默认 1 不显示高亮 |

### 修改文件

```
relay/relay/platforms/store.py                  # priority 缺省 1 + preserve 名单
relay/relay/platforms/base.py                   # AccountView + priority 字段
relay/relay/platforms/routes.py                 # POST /{p}/accounts/{uid}/priority
relay/relay/platforms/workbuddy/chat.py         # _pick_account 分组选号
relay/tests/test_platform_store.py              # priority 默认/preserve/patch
relay/tests/test_wb_chat_account*.py（现有 chat 选号测试文件）  # 分组选号用例
relay/tests/test_wb_routes.py                   # priority 端点用例
frontend/src/api.ts                             # WbAccount.priority + wbSetPriority
frontend/src/stores/workbuddy.ts                # setPriority action
frontend/src/views/providers/WorkBuddyTab.vue   # 卡片「优先级 N ›」+ 设置弹窗
relay/relay-onefile.spec、relay/relay.spec      # 无新模块则不动（routes/store 都已登记）
```

### 端点

| 方法/路径 | 职责 |
|---|---|
| POST `/v1/platforms/{platform}/accounts/{uid}/priority` | body `{priority}`（int，1–99）；返回更新后 AccountView；账号 404 / 非法值 400 |

### 选号示例（3 账号：A=priority 1，B=1，C=2）

- 正常流：请求在 A/B 间轮询（与现状两号轮询一致），C 永不命中
- A、B 均 401（单请求内 tried={A,B}）→ 本请求降级用 C
- 下一请求 tried 清空 → 回到 A/B（若 C 在用中发现高优恢复，同理）
- A 设为 priority 3、B 保持 1、C 保持 2 → 用 B（1 组），B 拒 → C（2 组）→ A（3 组）

## 同批协调（2026-10-13 追加）

- 本需求的 `priority` 与《[WorkBuddy 反代账号池健康管理](../WorkBuddy反代账号池健康管理/)》（2026-10-13 立项）的 `fail_count` / `degraded_until` / `model_cooldowns` **共享 `store.py` preserve 名单与 `_pick_account` 选号链**——实施时两者同批合并：preserve 名单一次性加全四个字段，`_pick_account` 一次改成「priority 分组 → 组内过滤降权/冷却 → 游标轮询」。
- 选号语义补充决策（健康管理 spec 追加）：**高优组全部冷却/降权时落到下一组**（可用性优先于优先级）。
- `AccountView` 加 `priority` 时顺带透出 `degraded_until` / `model_cooldowns`（只读展示）。

## 已知边界

- **「用完」= 凭证级失败才降级**：积分余额不足但 token 有效的号仍会被高优组选中，上游的「配额耗尽」错误（具体码未实测——不能拿真实账号压测耗尽）会作为普通 400 透传给 agent 而**不触发换号**。后续若实测拿到耗尽错误码，可把该 code 纳入 `_CredentialRejected` 同类处理（留作增强，不在本期）。
- 无 refresh_token 且 expires_at 缺失的号（粘贴导入无 refreshToken 的场景）轮到即 401 → 组内降级，与现状行为一致。
- 优先级只影响**反代聊天选号**；签到/配额/IDE 切号均不感知 priority（不改动）。
- 上限 99：够用且防手滑输入离谱值；不做拖拽排序 UI（整数即全序）。

## 实施步骤（commit 序列）

| # | commit | 验证 |
|---|---|---|
| 1 | `feat: 账号池优先级字段与反代分组选号`（store + base + chat + store/chat 测试） | pytest 全量 + ruff（默认全 1 等价性用例必须绿） |
| 2 | `feat: 账号优先级设置端点与卡片入口`（routes + api/store/tab + 路由测试） | pytest + format/build |
| 3 | `chore: spec 台账收尾` | 打包冒烟（若无模块级新增则仅产物验证）+ 台账 |

## 验收

- [ ] `pytest tests/ -x -q` 全绿（含：默认全 1 时选号序列与现状**逐请求等价**的回归用例；跨优先级降级用例）
- [ ] ruff + format:check + build 通过
- [ ] 手动验收（待用户）：设 A=1/B=2，聊天两次均落 A；A 删除或凭证失效后自动用 B；恢复默认 1 后轮询如常
