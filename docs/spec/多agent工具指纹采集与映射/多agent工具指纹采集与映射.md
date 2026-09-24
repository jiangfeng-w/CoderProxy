# 多 agent 工具指纹采集与映射

> 状态：开发中（采集器已实现 2026-09-05；TRAE 映射已实现 2026-09-06；其它 agent 映射待采数据）
> 优先级：核心
> 来源：用户实战反馈（2026-09-05）——在 TRAE 对话中切换 strict 模式后「工具 0 个、agent 停在执行工具步、总览仅已忽略增加」；顺藤摸瓜确认 relay 映射表对这些 agent 的工具**一个都没命中**，hybrid 实际退化成「原样转发」。用户要跨多个 agent（TRAE / Qoder / CodeBuddy / WorkBuddy / Cursor / Codex / Claude Code / opencode……）都能用，需要先拿到各 agent 的**真实工具名 + 每个工具的作用 + 参数**，才能跟牛码原生工具语义对应、做双向伪装。
> 依赖：M3 工具名双向伪装（映射表 TO_TA3/FROM_TA3/ARGS_* 在本需求里被扩展）；M6 数据底座（SQLite 落库沿用同库）
> 定稿日期：—（采集器与 TRAE 映射设计已定稿；其它 agent 映射待采数据后回填）

## 1. 背景与目标

CoderProxy 对任意 agent 暴露 OpenAI `/v1`。agent 的工具名五花八门（`bash`/`read_file`/`editor_apply_diff`/`glob`/`apply_patch`……），牛码模型只认官方桌面端原生工具集（PascalCase：`Read`/`Edit`/`Bash`/`SubAgent`）。relay 靠 [tool_disguise.py](../../../relay/relay/tool_disguise.py) 的映射表做边界翻译，但**映射表是写死的、且只覆盖了有限的常见名**——遇到不认识的工具名，hybrid 下退化为长尾透传、strict 下直接丢弃。

用户实测已确认：TRAE 等 agent 的工具名与现有映射表一个都不匹配，导致 strict 模式下 `tools=0`、agent 停在工具执行步。要根治，必须先**系统性地发现各 agent 的真实工具集**，再补映射。

### 目标

| # | 功能 | 说明 |
|---|---|---|
| F1 | 采集真实工具声明 | 在伪装**前**抓取每个 `/v1` 请求里 agent 声明的工具集（名字 + 描述 + 参数 schema） |
| F2 | 归一化多形态 | OpenAI（`type=function, function.name/parameters`）与 Anthropic（`type=tool, name/input_schema`）统一成同一指纹 |
| F3 | 落库去重 | 存 SQLite，按 `(name, param_sig)` 去重，重复声明累加 `calls` 频次 |
| F4 | 可查询/导出 | 提供只读查询供后续映射与人工核对 |
| F5 | 语义映射 | 依据采集到的名 + 描述 + 参数，把 agent 工具 → 牛码原生工具，双向写入映射表 |

### 非目标

- 不做自动语义匹配（需要人/模型判定 1:1 对应），采集器只负责拿数据。
- 不改 vendored `relay/app/**`（硬性规则 1）；扩展映射仍只改 `tool_disguise.py` 与 `ta3_tool_aliases.py`。
- 不保证覆盖所有 agent 的所有自定义长尾——只覆盖真实流过 relay 的请求。

## 2. 设计方案

### 2.1 核心思路：被动采集，而非反查文档

因为 CoderProxy 是所有 agent 的统一出口（OpenAI `/v1`），**每个要接入的 agent 都必须把工具定义发给 relay**。所以「真实工具名 + 作用 + 参数」的权威来源就是入站请求体，而不是各家源码/文档。做一个采集器让它自己长出来，对未知 agent 自动生效，比人工逐一查证可靠得多。

### 2.2 采集器（已实现）

- 位置：[tool_inventory.py](../../../relay/relay/tool_inventory.py)
- 归一化两种形态 → 统一 `{name, description, param_sig, schema_json}`：
  - OpenAI：`item["function"]["name"/"parameters"/"description"]`
  - Anthropic：`item["name"/"input_schema"/"description"]`
- `param_sig`：由参数 schema 的属性键 + 类型生成稳定签名（与声明顺序无关），用于区分同名不同参数变体。
- 落表 `tool_inventory`（与 `db.py` 共库 `coderproxy.db`，WAL + `to_thread`）：
  - 字段：`id / name / param_sig / description / schema_json / model / first_ts / last_ts / calls`
  - `UNIQUE(name, param_sig)`，冲突时 `calls+1`、刷新 `last_ts`、取最新可读描述/schema。
- 对外：`record_tools(tools, model)` / `list_tools()`（异步）。
- 开关：`settings.tool_inventory_enabled`（默认开，`RELAY_TOOL_INVENTORY=false` 关闭）。

### 2.3 挂接点（已实现）

[routes.py](../../../relay/relay/routes.py) 在 `build_disguise_context` **之前**、连通性探针（`probe`）请求豁免：

```python
if settings.tool_inventory_enabled and not probe:
    source_tools = body.get("tools")
    if source_tools:
        await tool_inventory.record_tools(source_tools, chat_request.model)
```

必须在伪装前采，否则拿到的是已重命名的牛码 schema，而非 agent 真实声明。

### 2.4 采集 → 映射管线（TRAE 已落地，其它 agent 待采数据）

```
入站 tools ──采集器──> tool_inventory 表（name+param_sig+描述+schema+频次）
                              │
                              ▼
              (人/模型) 语义 1:1 判定：某 agent 工具 = 牛码哪个原生工具
                              │
                              ▼
        生成/扩展 TO_TA3 / FROM_TA3 / ARGS_TO_TA3 / ARGS_FROM_TA3（双向，规则 4）
                              │
                              ▼
        补进 tool_disguise.py + ta3_tool_aliases.py（出站 schema + 历史 tool_calls + 入站 restore 三处同步）
```

关键：采集器只回答「这个 agent 有哪些工具、参数是什么、干啥用的」；**「对应牛码哪个工具」仍需人判定**（描述 + 参数已提供足够依据）。命名不冲突的前提下可能还要为同名不同变体做区分。

### 2.5 映射判定规则（TRAE 已验证 2026-09-06）

- **同名 + 参数键一致 → 透传**：`TodoWrite`（`todos`）、`WebSearch`（`query`）。无需翻译，透传保留 TRAE 原生字段（含 `summary` 等），避免 TRAE 端字段缺失校验报"工具 id 有问题"。
- **同名 + 参数键不同 → 映射**：`Read`/`Write`（`file_path↔filepath`）必须翻译，否则牛码执行器读 `args.filepath` 拿不到参数。
- **不同名 → 映射**：`SearchReplace→Edit`、`RunCommand→Bash`、`Grep→Search`、`LS→List`、`Skill→ReadSkill`。
- **多对一去重 / 防重复**：同一牛码工具被多个 agent 工具映射（如 `Grep`/`SearchCodebase→Search`）时，若都保留会向牛码发**两个同名 `Search`** → 牛码模型 0 输出。既要保证工具名唯一、又不想压缩工具数，最终采用：**其一映射、其余透传**（如 `Grep→Search`，`SearchCodebase` 透传），出站仍 27 工具、无重复 `Search`。
- **vendored ta3.py 一处修复**：`_restore_tool_calls` 原用 `if real is not name`（只还原"名字变了"的参数），导致同名工具（`Read filepath→file_path`）参数不还原 → 改为 `if real != name or self._args_from_ta3.get(name)`（该文件为 vendored，改动处需回上游 参考实现 同步；参考实现 仅供参考，不强制）。

**TRAE 映射表（2026-09-06 端到端验证，27 工具 = 7 映射 + 20 透传）**：

| TRAE 工具 | → 牛码 | 参数适配 | 类别 |
|---|---|---|---|
| Read | Read | `file_path→filepath` | 映射 |
| Write | Write | `file_path→filepath` | 映射 |
| SearchReplace | Edit | `file_path→filepath, old_str→oldString, new_str→newString` | 映射 |
| RunCommand | Bash | `command→command` | 映射 |
| Grep | Search | `pattern→query, path→path` | 映射 |
| LS | List | `path→dirPath` | 映射 |
| Skill | ReadSkill | `name→skillName` | 映射 |
| TodoWrite | — | — | 透传 |
| WebSearch | — | — | 透传 |
| SearchCodebase | — | — | 透传 |
| （其余 17 个 TRAE 私有工具：Task/Glob/WebFetch/DeleteFile/StopCommand/CheckCommandStatus/AskUserQuestion/NotifyUser/OpenPreview/Schedule/PureShowWidget/create_goal/get_goal/update_goal/run_mcp/RequestAuthorization/browser_waiting_for_user_interaction） | — | — | 透传 |

> 验证结论：全量 keyless 单测 122 通过；TRAE 端读到 `chat_request.tools=27`（未压缩）、「已转换」正常上涨、无 0 输出空白，读/写/改/运行/搜索/列表/技能均正常返回。

### 2.6 被否决的备选

- **反查各家源码/文档**：不可靠（agent 常改工具名）、工作量爆炸、且覆盖不了未列举 & 未知 agent。否决，改为被动采集。
- **采集器主动脚本**（遍历各 agent 模拟调用）：需为每个 agent 单独写适配，违背「任意 agent 自动生效」宗旨。否决。

## 3. 已知边界

- 采集的是「真实流过 relay」的声明；某个 agent 若从不走 relay，则采不到（须先把它指向 `/v1`）。
- 同名工具不同参数会拆成多行，映射阶段需按实际语义/参数收敛。
- `description` 是 agent 自称的「作用」，语义判定仍以人为准，存在不可 1:1 对应的长尾（保持 hybrid 透传 / strict 丢弃，归 M3 行为）。
- 采集写库为轻量、异步、随机访问一条 upsert，对吞吐影响可忽略；过度频繁可在关闭开关时移除（`RELAY_TOOL_INVENTORY=false`）。
- **同名工具必须区分"参数键是否一致"**：一致（TodoWrite/WebSearch）走透传；不一致（Read/Write）必须映射，否则牛码执行器读不到参数。
- **同一牛码工具被多源映射会重复**：向牛码发两个同名 `Search` 会导致模型 0 输出；已用"其一映射、其余透传 + 出站去重兜底"规避（见 §2.5）。

## 4. 验收

采集器（已实现）：
1. OpenAI / Anthropic 两种工具声明均被归一化成同一指纹落库。
2. 同一 `(name, param_sig)` 重复声明只累加 `calls`，不新增行；同名不同参数生成新行。
3. 空 / 无 / 非 dict 工具被忽略，不报错。
4. `list_tools` 按 `calls` 倒序返回，含描述 / schema / first/last_ts。
5. 挂接点在伪装前生效、跳过 `probe`；`tool_inventory_enabled=false` 时不写库。
6. keyless 单测 `tests/test_tool_inventory.py` 全绿。

映射阶段（TRAE 已验收 2026-09-06；其它 agent 待采数据）：
7. [x] TRAE：27 工具 = 7 映射 + 20 透传；读/写/改/运行/搜索/列表/技能端到端正常，`chat_request.tools=27` 不压缩、无 0 输出、总览「已转换」上涨。
8. [x] 双向映射字符串/参数键正确，出站 schema + 历史 tool_calls + 入站 restore 三处一致（keyless 单测覆盖：`test_tool_disguise`）。
9. [ ] 其它 agent（Qoder/Cursor/Codex/Claude Code/opencode 等）待采集工具集后，按 §2.5 规则补映射。

## 5. 支撑脚本

- 采集器：`relay/relay/tool_inventory.py` + 单测 `relay/tests/test_tool_inventory.py`（已落地）。
- TRAE 映射：`relay/relay/tool_disguise.py` 的 `EXT_TO_TA3`/`EXT_ARGS_TO_TA3`（表见 §2.5）+ `relay/app/models/providers/ta3.py` 的 `_restore_tool_calls` 一处修复（vendored，需回上游同步）；单测 `relay/tests/test_tool_disguise.py`。
- 牛码工具 schema 核对：解包 `D:\Programs\ta3-new-coder-desktop\resources\app.asar`（`dist/main/plugins/tools/*.js`），以真实 `function.parameters` 为准。
- 导出/核对：当前可直查 `coderproxy.db` 的 `tool_inventory` 表；GUI 页待评估。
