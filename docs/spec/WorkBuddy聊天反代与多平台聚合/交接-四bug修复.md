# 交接：WorkBuddy 实测四 bug 修复（2026-10-09 深夜会话交接）

> **给接手 agent 的读法**：本文件是唯一权威交接依据，自包含。先读 §1 当前状态与 §2 工作纪律，
> 再按 §4 的修复顺序动手；§3 四个 bug 的根因/证据/方案都是**已实证**的（复现脚本在仓库里可直接跑）。
> 上一轮会话上下文里有过长讨论，**不要再去找那段对话**，所有结论已固化到本文件。
>
> 来源需求：[WorkBuddy聊天反代与多平台聚合](WorkBuddy聊天反代与多平台聚合.md)（已实现，验收见 §5）
> 用户身份：仓库作者本人（单人项目）；沟通用中文。

---

## 1. 当前状态（接手时的真实现场）

| 项 | 状态 |
|---|---|
| 需求主线 | **已实现并验收**（2026-10-09：pytest 367 绿、e2e/产物冒烟 19/19、三协议真客户端回填多协议 A 段）——见 §5 |
| 本次四 bug | **全部已定位，均未修复**（bug1/2 前端、bug3/4 出站链）——见 §3 |
| 代码 HEAD | `c3676bc`（main 分支，工作区另有本次交接新增文档/脚本待提交，见 §6） |
| dev 运行态 | `npm --prefix frontend run tauri dev` 形式（实际用 `frontend/node_modules/.bin/tauri dev`）已起在后台；sidecar 端口 3601；**用户会自己重启**，接手时先用 `curl 127.0.0.1:3601/v1/auth/status` 探活，不通就按 §2 重新拉起 |
| 数据目录 | `%APPDATA%\com.coderproxy.desktop\data`（真实用户数据：小号 WorkBuddy 账号池 + 牛码主号登录态） |
| 小号 | uid `522666c8…`（nickname 卡卡西），额度充足；**牛码主账号已封禁**，只能用登录态+目录验证，禁止真实对话 |

**已提交的相关 commit**（从旧到新，均在本需求分支/主线）：
`c78ad38` 定稿 → `c538947` 前缀路由+注册表 → `097fcbf` WB 聊天 adapter → `eb5f721` 目录 →
`1318eb2` 自定义供应商 → `c59982a` 服务门控 → `b90a7ff` WB tab 模型区 → `5e422ea` 打包登记+三处语义修复 →
`5db4768` WB 网络错误 502 → `0363b6f` e2e 冒烟脚本 + 自定义 404 消息修正 → `c3676bc` 文档回填。

---

## 2. 工作纪律（硬性，来自 AGENTS.md + 本会话踩坑）

1. **中文回答**；提交信息 = 单行 `type: 概述` + 空行 + 逐条 `- ` bullet（写清改了什么/为什么）。
2. **本地起服/测试环境**：
   - 单测：`cd relay && py -3.13 run_tests.py`（自举入口，不要用裸 `pytest`——本机 python 是 shim 且不读 PYTHONPATH）。
   - 起 relay：`cd relay && py -3.13 run.py run --port <port>`（源码态）；打包产物 `relay/dist/relay-sidecar-onefile.exe run --port <port>`。
   - GUI dev：`frontend/node_modules/.bin/tauri dev`（会编译壳 + 起 Vite:1420 + 拉起 sidecar）。
   - 依赖在 `relay/_deps`（httpx 等），脚本里 `sys.path.insert(0, relay / "_deps")`。
3. **进程清理纪律（本会话踩过重坑）**：
   - PyInstaller onefile 是**两层进程**，`proc.terminate()` 只杀 bootloader，子进程会残留并锁住 exe；
     必须 `taskkill /PID <pid> /T /F`（进程树），或直接抄现有脚本的 `RelayProc.kill()`。
   - 残留的 sidecar 会占住 `src-tauri/binaries/relay-sidecar-x86_64-pc-windows-msvc.exe` 的**文件锁**，
     让 PyInstaller 重建报 `PermissionError`。清理前**先核对进程的 exe 路径/创建时间**，
     确认是自己冒烟残留再杀；**用户真实应用不要动**（判别方法：看父进程是否存活、数据目录是不是 `%APPDATA%`）。
   - 别把 `%TEMP%` 的 `_MEI*`、`proto-smoke-*`、`cp-e2e8-*` 当垃圾乱清——先看时间与归属。
4. **小号限额纪律**（AGENTS 硬性规则 5）：只准用小号；复现类脚本请求数固定（2~7 次），跑完别重跑；
   不要在真实牛码账号上反复触发对话（主号封禁，仅登录态可用）。
5. **参考实现**（`D:\Code\ref-projects\`）：WorkBuddy 聊天契约主参考 **workbuddy2api-panel**（Go；
   `internal/upstream/payload.go`、`tool_pairing.go`、`thinking.go`）；聚合架构参考 **9router**
   （`open-sse/translator/request/openai-responses.js`）；另有 **chatcoder**（`server/app/orchestration/compaction.py`）。
   **本会话已读完这三处关键文件，结论见 §3，不用重读全仓库。**
6. 前端规则：优先 Naive UI 现成组件；格式 `npm --prefix frontend run format` / `format:check`。

---

## 3. 四个 bug：根因 / 证据 / 修复方案

### BUG-4（最严重）工具配对断裂 → 上游 400 code=11148【已实证】

**用户现象**：ZCode 报 `tool calls and tool results do not match, please start a new conversation and retry（上游 code=11148）`。

**昨晚真实日志证据**（`%APPDATA%\com.coderproxy.desktop\data\coderproxy.db` 表 `logs`，id 4819–4822）：
```
17:40:24 chat_request  openai-responses   ← ZCode 自动重试 1
17:40:27 chat_done
17:40:27 chat_request  openai-responses   ← 重试 2
17:40:29 chat_error    "…（上游 code=11148）"
```
即同一条会话内 ZCode 连续重试 3 次、每次 body 都带坏历史，最后一次落 `chat_error`。

**根因（真实上游 A/B 钉死）**：`relay/relay/protocol_adapter.py::responses_to_chat_request`
在**并行工具调用**时产出顺序断裂。ZCode 的 input 是 `fc,fc → fco,fco → fc → fco`
（两批调用各自带结果），转换器对 `function_call_output` 分支**没有先把待发的 assistant 落盘**
（`current_assistant` 攒着不 flush），于是产出：

```
[11] assistant tool_calls=[C1,C2]   ← 批 1
[12] assistant tool_calls=[C3]      ← 批 2 插在批 1 结果之前 ✗
[13] tool → C1
[14] tool → C2
[15] tool → C3
```

上游要求「带 tool_calls 的 assistant 后**紧跟**它每条结果」，中间不得夹任何消息 → 判 11148。

**A/B 实证（可复跑，2 次小号请求）**：
```
py -3.13 "docs/spec/WorkBuddy聊天反代与多平台聚合/复现-wb工具配对断裂.py"
  [A] 断裂形态 → status=400  {"code":11148,"msg":"tool calls and tool results do not match…"}
  [B] 修复形态 → status=200  content='两个任务目录都是空的…'
```

**修复方案（两步，参考实现已验证的口径）**：

1. **转换器修根**（`protocol_adapter.py::responses_to_chat_request` 的 `function_call_output` /
   `custom_tool_call_output` 分支）：处理该分支时**先 `flush_assistant()`**，再入 `pending`；
   并顺手对齐 9router `openai-responses.js`:130 的行为——遇 fco 时先推 assistant、再**立即推送**
   待发结果（9router 是「flush assistant → 先到先排」，我们的 `pending` 累积到下一个
   message/结尾才冲刷，也可保持，只要保证同一批的 flush 时序正确）。
2. **出站安全网 `relay/relay/tool_repair.py`（新建，spec §4.5.3 早已规划但未实现）**：
   两个纯函数，WorkBuddy adapter 在 `transform_request` 之后对 `body["messages"]` 跑一遍：
   - `repack_tool_results(messages)`——把夹在 `assistant.tool_calls` 与其结果之间的**非 tool 消息**
     后移（对齐 `workbuddy2api-panel/internal/upstream/tool_pairing.go::repackToolResultBlocks`）；
   - `cleanup_orphan_tool_calls(messages)`——按 id 集合**双侧对称**裁剪：assistant.tool_calls 只留
     有结果配对的项（空了就删 `tool_calls` 键），`role:tool` 只留调用被保留的项
     （对齐同文件 `cleanupOrphanToolCalls`；注意参考注释里说的历史坑：**不要**「批内不全就删整批」，
     那会留下孤儿 tool 结果，反倒制造 11148）。
   顺序：先 repack 再 cleanup（参考 `payload.go:60-62` 同序）。
3. 补测试：`relay/tests/` 新增（或并入 test_protocol_adapter.py）——
   ① `fc,fc→fco,fco→fc→fco` 转换后每条 assistant 的结果紧跟；
   ② 畸形历史（孤立 tool 结果 / 无结果 tool_call / 结果中间夹 developer 消息）经 tool_repair 后
      「无孤儿、无断裂」；③ 无工具流量时零改动（返回原对象）。

**注意**：`_clamp_responses_call_id` 的 64 截断**不是**本 bug 原因（昨晚 3 个真实 id 均 32 字符），
别顺手改它。

---

### BUG-3 agent 思考强度只在 `/v1/responses` 协议生效【已定位，修复未做】

**用户现象**：WorkBuddy 模型下，只有 agent 用 responses 格式（Codex 系）时思考强度设置生效；
chat / claude 两种格式无效。

**日志证据**（同一 db，id 4807–4815）：

| 协议 | 请求里 | reasoning_tokens | 结论 |
|---|---|---|---|
| claude | `thinking:{type:enabled}`（无 budget） | 0 | 无思考 |
| openai | `reasoning_effort=max` | **0**（耗时 2.7s） | 无思考 |
| openai-responses | `reasoning:{effort:max}` | 41 / 335 | **有思考** |

**探针基线（修复前，已跑）**：
```
py -3.13 "docs/spec/WorkBuddy聊天反代与多平台聚合/探针-wb思考形状.py" --via-relay
  chat: effort=max+thinking.enabled  → 思考✓ (863 chars)   ← ZCode chat 形状居然通
  chat: 仅 thinking.enabled          → 思考✗ (0)
  messages: thinking.enabled(无budget)→ 思考✗ (0)
  responses: reasoning.effort=max    → 思考✓ (1266 chars)
  chat: thinking.disabled            → "思考✓" ⚠ 关思考意图仍出思考，同条链要一起修
```

**根因（与参考 `thinking.go` 一致）**：WorkBuddy 上游对 deepseek 系要求**两个条件同时满足**——
`thinking:{type:"enabled"}` **且** 带 `reasoning_effort` 档位（官方客户端 `isThinkingEnabled =
!!(reasoning_summary || reasoning_effort || reasoning?.effort)`）。我们当前 adapter
（`relay/relay/platforms/workbuddy/chat.py::transform_request`）只把 `reasoning_effort` 透传，
**从不注入 `thinking` 开关**：
- claude 入站（ZCode 形状 `thinking:{type:enabled}` 无 budget）→ 归一后 `reasoning_effort=None` →
  adapter 什么都不发 → 上游默认不思考；
- openai 入站 effort=max → 只发 effort、没有 thinking 开关 → 上游不思考（探针 C 组证实：
  直连单发 effort=max 有思考，但经 relay 的单发 effort 因缺另一条件而无效）；
- responses 入站 effort 被 `apply_thinking_default`/上游侧补齐（且 responses 路径下我们发的
  `reasoning_summary` 恰好满足 `reasoning_summary||effort` 判定）→ 有思考。

**修复方案（对齐 `workbuddy2api-panel/internal/upstream/thinking.go`）**：
在 `WorkBuddyChatAdapter.transform_request`（或出站前统一钩子）对 **deepseek 系模型**（模型名
`startswith("deepseek")`，大小写无关）做：
1. 显式 `thinking.type == "disabled"` → 删 `reasoning_effort`（snake+camel）并**不注入**，
   确保真关（修上面探针最后一行）；
2. 其余情况（无 thinking / enabled / 只有 effort）→ **注入 `thinking:{type:"enabled"}`**，
   且保证有 `reasoning_effort`：agent 显式给了就用它，没给就用**目录默认档**
   （`model_entry.completion_options.defaultEffort`，取自 `/v3/config` 的 `reasoning.defaultEffort`；
   参考实现兜底 `"high"`）；
3. 档位超出模型 `supportedEfforts` 时**降级**到 ≤ 请求档的最高支持档（参考 `normalizeReasoningEffort`；
   我们的目录条目已有 `completion_options.thinkingLevels` 或 `reasoning_efforts`，可用）；
4. `reasoning_summary:"auto"` 保留现有行为（仅 effort≠none 时配）。
   注意探针 B 组直连显示 `effort=none` 也会出思考——我们对 "关" 的语义要按 1）走（删字段），
   不要再发 effort=none 给 deepseek 系。

**回归证据**：修复后重跑 `探针-wb思考形状.py --via-relay`，五个形状应变为：
chat(带effort)✓ / chat(仅thinking)✓（补默认档） / messages(仅thinking)✓（补档） /
responses✓ / disabled✗（真关）。再补单测（MockTransport）覆盖「注入 thinking / 缺档补默认 /
disabled 删档 / 非 deepseek 零改动」。

---

### BUG-1 牛码 tab 模型区混入 WorkBuddy 模型【已定位，修复未做】

`frontend/src/views/providers/NiucodeModels.vue::load()` 用 `getModels(true)`（全量目录，含
`牛码/`、`WorkBuddy/`、`<自定义>/`），渲染前**没有按前缀过滤**。GUI 里牛码 tab 应只显示
`牛码/` 前缀条目（或裸名归一到牛码）。

**修复**：`load()` 里过滤 `m.id.startsWith('牛码/')`（与 `WorkBuddyTab.vue` 的
`WB_PREFIX` 过滤对称）；「全部启用/禁用」等聚合操作也因此天然只作用于牛码条目。
检查同文件 `onAllEnable/onAllDisable`、`count` 文案（`已启用 x/y`）是否受影响，一并对齐。
**注意**：`store.syncTick` 监听与失败模型可见性语义（all=1）不要破坏。

---

### BUG-2 WorkBuddy tab 模型卡片行为与牛码 tab 不一致【已定位，修复未做】

`frontend/src/views/providers/WorkBuddyTab.vue` 的模型表是本期新写的精简版，缺三处（对照
`NiucodeModels.vue` 模板 + 样式）：

1. **启用控件是裸 checkbox**，牛码 tab 是 `label.switch > input + span.slider` 滑杆样式
   （样式在 `NiucodeModels.vue:520-556`，WorkBuddyTab 里没有——补同名样式或抽公共）；
2. **模型名不可点击复制**：牛码 tab 是 `<td class="mono copyable" title="点击复制模型名"
   @click="onCopy(m.id)">`（带 `navigator.clipboard` + message 反馈）；
3. **缺「默认思考强度」列**：牛码 tab 有 `NSelect`（chosen-effort，写 `thinking_defaults`，
   `STANDARD_EFFORTS`/`STANDARD_EFFORT_LABELS` 在 `frontend/src/store`）；WorkBuddy 条目
   `completion_options` 已带 `thinkingLevels`/`defaultEffort`（catalog.py `_to_entry` 产出），
   接上即可（**与 BUG-3 的「缺档补默认」是同一份数据**，先做 BUG-3 后端语义再做前端列更顺）。

前端改完跑 `npm --prefix frontend run format:check` 与 `npm --prefix frontend run build`（含 vue-tsc）。

---

## 4. 建议修复顺序（含验证与提交节奏）

1. **先 BUG-4**（最严重、会顶死整条会话）：转换器修根 + `tool_repair.py` 安全网 + 单测；
   验证 = `复现-wb工具配对断裂.py` 的 A 组变为「转换器产出已是修复形态」、`py -3.13 run_tests.py` 全绿；
   再用真客户端（ZCode 或 Codex CLI，经本地 relay）跑一次带工具的多轮确认不再 11148。
   提交信息建议：`fix: responses 并行工具调用顺序断裂致上游 11148（转换器修根 + tool_repair 安全网）`
2. **再 BUG-3**：adapter 注入 thinking/补默认档/禁用分支 + 单测；验证 = `探针-wb思考形状.py --via-relay`
   五形状符合预期（§3 BUG-3 末尾表）。
   提交：`fix: WorkBuddy deepseek 系思考开关注入（chat/messages 协议思考强度生效）`
3. **再 BUG-1 / BUG-2**（前端两个，可一次提交）：`fix: 供应商页模型区——牛码 tab 前缀过滤 + WorkBuddy 卡片对齐（开关/复制/思考列）`
4. 全部完成后：更新本需求 §5 验收里对应条目、必要时在 `docs/已知问题.md` 记一条 11148 根因小结（可选），
   并重建 sidecar：`py -3.13 relay/build_sidecar.py`（会覆盖 `src-tauri/binaries/…exe`；
   **先确认没有残留 sidecar 进程占锁**，见 §2.3）。注意 dev 模式还会从 `src-tauri/target/debug/relay-sidecar.exe`
   拉起（本会话已同步过一次；重建后如 GUI 用 dev 起，记得同步这个拷贝，命令：
   `cp src-tauri/binaries/relay-sidecar-x86_64-pc-windows-msvc.exe src-tauri/target/debug/relay-sidecar.exe`）。

---

## 5. 主线已完成部分的验收（别重复做）

- 需求文档：[WorkBuddy聊天反代与多平台聚合.md](WorkBuddy聊天反代与多平台聚合.md)（状态：已实现 2026-10-09，§5 验收已勾）。
- pytest：`cd relay && py -3.13 run_tests.py` → 367 passed（**本文档新增脚本后应仍 367；修复 bug 时再加用例**）。
- e2e 冒烟：`py -3.13 "docs/spec/WorkBuddy聊天反代与多平台聚合/冒烟-三provider-e2e.py"`
  → 19/19（隔离数据目录 + 真 exe；含三协议经 WorkBuddy、自定义供应商真转发、无账号 503）。
  该脚本自带 `taskkill /T` 进程树清理——新写脚本请抄它的 `RelayProc`。
- 三协议真客户端已回填多协议需求 A 段（Codex CLI 0.153.4 / Claude Code 2.1.263 / OpenAI SDK 3.26.1）。
- **遗留未验**：牛码 chat 真实对话（主号封禁、规则 5；不许在真实账号补测，等解封用小号）。

---

## 6. 本会话新增、待提交的文件

- `docs/spec/WorkBuddy聊天反代与多平台聚合/交接-四bug修复.md`（本文件）
- `docs/spec/WorkBuddy聊天反代与多平台聚合/探针-wb思考形状.py`（BUG-3 探针，`--direct` / `--via-relay`）
- `docs/spec/WorkBuddy聊天反代与多平台聚合/复现-wb工具配对断裂.py`（BUG-4 A/B 复现，2 请求）
- 另有临时脚本（不在仓库）：`%TEMP%\cp-echo-capture.py`、`%TEMP%\cp-think-matrix.py`（可删）。

建议提交：`docs: 交接文档 + 四 bug 复现/探针脚本（BUG-4 11148 已实证）`

## 7. 环境速查

| 事项 | 值 / 命令 |
|---|---|
| 数据目录 | `%APPDATA%\com.coderproxy.desktop\data`（`relay_state.json` / `platform_workbuddy.json` / `coderproxy.db`） |
| 日志查询 | `sqlite3 coderproxy.db "select id,ts,kind,model,detail from logs order by id desc limit 20"`（或 python sqlite3） |
| relay 单测入口 | `cd relay && py -3.13 run_tests.py [-k 过滤]` |
| 起 relay（源码） | `cd relay && py -3.13 run.py run --port 6101`（隔离起服要 `RELAY_DATA_DIR` 指到临时目录+复制真实文件） |
| GUI dev | `frontend/node_modules/.bin/tauri dev`（Vite 1420 + sidecar 3601） |
| 前端格式/构建 | `npm --prefix frontend run format:check` / `npm --prefix frontend run build` |
| 打包 sidecar | `cd relay && py -3.13 build_sidecar.py`（产物 `relay/dist/relay-sidecar-onefile.exe` + 拷贝 `src-tauri/binaries/`） |
| ZCode 会话回放 | `C:\Users\JF\.zcode\cli\rollout\model-io-sess_*.jsonl`（大文件！逐行 grep，别整文件 json.load） |
| ZCode 客户端代码 | `D:\Programs\ZCode\resources\glm\zcode.cjs`（14MB 单文件，用 grep -o 抠上下文） |
