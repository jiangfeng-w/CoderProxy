"""工具名双向伪装（M3）三模式编排。

对外只暴露 OpenAI /v1，agent 的工具名五花八门（bash/read_file/glob/apply_patch），
牛码模型只认识官方桌面端的原生工具集（Read/Edit/Bash/SubAgent…，PascalCase）。
M3 在 M2 的 vendored disguise/restore 之上补两层：

1. 三模式（settings.tool_mode，见开发计划 §5.4）：
   - hybrid（默认）：有映射工具重命名为 ta3 原生名 + 参数适配；无映射长尾透传保留
     （M1 探针已确认牛码网关容忍任意工具名）；历史未映射调用降级文本占位。
   - strict：有映射同 hybrid；无映射丢弃（对齐 chatcoder，模型不感知该工具）。
   - passthrough：tools 原样透传，不做任何映射（放弃伪装层，兼容性最好）。

2. 请求级映射表：
   把「agent 本次实际用的工具名」记进 restore_map，入站按它精确还原——避免
   TO_TA3 多对一导致的 FROM_TA3 反查歧义（如 terminal_exec 与 bash 都映射 Bash）。

实现边界：本模块只做「编排出站 tools schema + 构建请求级映射表」；历史消息伪装
与入站还原由 vendored ta3.py 完成（接收本模块产出的映射表，见 ta3.py 构造参数）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.models.providers.ta3_tool_aliases import ARGS_FROM_TA3, ARGS_TO_TA3, TO_TA3
from app.models.providers.ta3_tool_schemas import TA3_NATIVE_SCHEMAS

MODE_HYBRID = "hybrid"
MODE_STRICT = "strict"
MODE_PASSTHROUGH = "passthrough"
VALID_MODES = (MODE_HYBRID, MODE_STRICT, MODE_PASSTHROUGH)

# agent 侧常见工具名 → ta3 原生名（扩展映射，覆盖开发计划 §3.2 表格）。
# 语义 1:1 才映射；语义模糊的长尾（glob/apply_patch/lsp/skill/webfetch/MCP/update_plan）
# 不映射——hybrid 透传保留、strict 丢弃，由模型/agent 自行兜底。
EXT_TO_TA3: dict[str, str] = {
    # 终端
    "bash": "Bash",
    "shell": "Bash",          # Codex
    "exec_command": "Bash",   # Codex
    # 读
    "read": "Read",
    "read_file": "Read",      # Codex
    # 写
    "write": "Write",
    "write_file": "Write",
    # 编辑
    "edit": "Edit",
    # 搜索 / 目录
    "grep": "Search",
    "grep_files": "Search",   # Codex
    "search": "Search",
    "list_dir": "List",       # Codex
    # 联网
    "web_search": "WebSearch",
    "websearch": "WebSearch",
    # 待办
    "todo_write": "TodoWrite",
    "todowrite": "TodoWrite",
    # 子代理 / 图片
    "subagent": "SubAgent",
    "view_image": "ViewImage",
}

# agent 参数键名 → ta3 键名（None=丢弃）；键名一致的省略（disguise 逻辑按原名保留）。
EXT_ARGS_TO_TA3: dict[str, dict[str, str | None]] = {
    "read": {"path": "filepath"},
    "read_file": {"file_path": "filepath"},
    "write": {"path": "filepath"},
    "write_file": {"file_path": "filepath"},
    "edit": {
        "path": "filepath",
        "old_text": "oldString",
        "new_text": "newString",
        "replace_all": "replaceAll",
    },
    "grep": {"pattern": "query"},
    "grep_files": {"pattern": "query"},
    "search": {"pattern": "query"},
    "list_dir": {"path": "dirPath"},
    "subagent": {"task_description": "prompt", "task_title": "description"},
}


@dataclass
class DisguiseContext:
    """一次 chat 请求的工具伪装上下文（relay 产出，vendored ta3.py 消费）。"""

    mode: str
    outbound_tools: list[dict]  # 发给牛码的最终 tools schema（已重命名/顶替/透传）
    disguise_map: dict[str, str] = field(default_factory=dict)   # agent 名 → ta3 名（历史重命名）
    restore_map: dict[str, str] = field(default_factory=dict)    # ta3 名 → agent 名（请求级精确还原）
    # 长尾透传工具名集合（agent 原名 == 模型暴露名，如 TRAE 直接用的 Read/Edit）。
    # 入站/历史还原时必须原样保留，否则会被 FROM_TA3 / 未映射降级误伤 → 工具调用断流。
    passthrough_names: set[str] = field(default_factory=set)
    args_to_ta3: dict[str, dict[str, str | None]] = field(default_factory=dict)
    args_from_ta3: dict[str, dict[str, str | None]] = field(default_factory=dict)
    tools_pre_disguised: bool = True
    # M4 监控：工具映射命中 / 长尾透传 / 丢弃 计数（GUI 日志面板用）
    tool_map_hits: int = 0
    tool_longtail_passthrough: int = 0
    tool_dropped: int = 0


def _normalize_mode(mode: str) -> str:
    m = (mode or MODE_HYBRID).strip().lower()
    return m if m in VALID_MODES else MODE_HYBRID


def build_disguise_context(tools, mode: str = MODE_HYBRID) -> DisguiseContext:
    """按模式把 agent 的 tools schema 编排出站终态，并构建请求级映射表。

    参数工具（OpenAI function 格式）：[{type, function:{name, parameters, description}}]。
    """
    mode = _normalize_mode(mode)
    tools = list(tools or [])
    if mode == MODE_PASSTHROUGH:
        # 原样透传：不重命名/不降级/不还原（provider 的 passthrough 分支接管）
        return DisguiseContext(mode=mode, outbound_tools=tools,
                               passthrough_names={s.get("function", {}).get("name") or ""
                                                  for s in tools
                                                  if s.get("function", {}).get("name")},
                               tool_longtail_passthrough=len(tools))

    disguise_map = {**TO_TA3, **EXT_TO_TA3}
    args_to = {**ARGS_TO_TA3, **EXT_ARGS_TO_TA3}
    args_from = {**ARGS_FROM_TA3}  # 基础：vendored ta3→agent；下面按请求覆盖
    outbound: list[dict] = []
    restore_map: dict[str, str] = {}
    passthrough_names: set[str] = set()
    map_hits = 0
    longtail = 0
    dropped = 0
    for schema in tools:
        function = schema.get("function") or {}
        agent_name = str(function.get("name") or "")
        if not agent_name:
            continue
        alias = disguise_map.get(agent_name)
        if alias is None:
            # 无映射长尾：strict 丢弃；hybrid 透传保留（M1 确认网关容忍）
            if mode == MODE_STRICT:
                dropped += 1
                continue
            longtail += 1
            outbound.append(schema)
            passthrough_names.add(agent_name)
            continue
        native = TA3_NATIVE_SCHEMAS.get(alias)
        if native is None:
            # 映射到 ta3 名但无原生 schema（理论不可达）：按长尾处理
            if mode == MODE_STRICT:
                dropped += 1
                continue
            longtail += 1
            outbound.append(schema)
            passthrough_names.add(agent_name)
            continue
        map_hits += 1
        restore_map[alias] = agent_name  # 请求级：入站按 agent 实际名精确还原
        outbound.append(native)
        # 该 agent 工具的参数键逆映射 → 入站参数还原表（请求内同一 agent 约定）
        mapping = args_to.get(agent_name)
        if mapping:
            rev = {v: k for k, v in mapping.items() if v is not None}
            if rev:
                args_from[alias] = {**args_from.get(alias, {}), **rev}

    return DisguiseContext(
        mode=mode,
        outbound_tools=outbound,
        disguise_map=disguise_map,
        restore_map=restore_map,
        passthrough_names=passthrough_names,
        args_to_ta3=args_to,
        args_from_ta3=args_from,
        tool_map_hits=map_hits,
        tool_longtail_passthrough=longtail,
        tool_dropped=dropped,
    )
