"""M3 工具名双向伪装 keyless 单测：三模式编排 + 请求级映射（不触发牛码网络请求）。

覆盖 M3 spec 验收：
- hybrid：fs_read/editor_apply_diff/terminal_exec → Read/Edit/Bash，回包还原原名+参数
- hybrid：长尾（apply_patch/glob）透传不丢弃，模型中返回时原名返回 agent
- strict：长尾丢弃，模型不感知
- 历史消息未映射调用转文本占位，多轮不协议断裂
"""
import json

from app.models.providers.ta3 import Ta3Provider
from app.models.schemas import ChatMessage, ChatRequest

from relay import tool_disguise


def _tool(name: str, parameters: dict | None = None) -> dict:
    return {"type": "function",
            "function": {"name": name, "parameters": parameters or {"type": "object"}}}


def _provider(ctx: tool_disguise.DisguiseContext, *, model="glm-5.3-flash", meta=None) -> Ta3Provider:
    return Ta3Provider(
        api_key="llm-abc", base_url="https://lc.yinhaiyun.com/newcoder", model=model,
        meta=meta,
        tool_mode=ctx.mode,
        disguise_map=ctx.disguise_map,
        restore_map=ctx.restore_map,
        args_to_ta3=ctx.args_to_ta3,
        args_from_ta3=ctx.args_from_ta3,
        tools_pre_disguised=ctx.tools_pre_disguised,
        passthrough_names=ctx.passthrough_names,
    )


# ─────────────────────────── 出站 tools schema ───────────────────────────

def test_hybrid_outbound_maps_to_native_schema():
    """hybrid：fs_read/editor_apply_diff/terminal_exec → Read/Edit/Bash，schema 整体顶替。"""
    tools = [_tool("fs_read"), _tool("editor_apply_diff"), _tool("terminal_exec")]
    ctx = tool_disguise.build_disguise_context(tools, "hybrid")
    names = [t["function"]["name"] for t in ctx.outbound_tools]
    assert names == ["Read", "Edit", "Bash"]
    # schema 用 TA3_NATIVE_SCHEMAS 顶替（非只改 name）：Read 带 filepath 属性
    assert "filepath" in ctx.outbound_tools[0]["function"]["parameters"]["properties"]


def test_hybrid_longtail_passthrough():
    """hybrid：长尾 apply_patch/glob 透传保留，schema 原样。"""
    tools = [_tool("fs_read"), _tool("apply_patch"), _tool("glob")]
    ctx = tool_disguise.build_disguise_context(tools, "hybrid")
    names = [t["function"]["name"] for t in ctx.outbound_tools]
    assert names == ["Read", "apply_patch", "glob"]
    patched = next(t for t in ctx.outbound_tools if t["function"]["name"] == "apply_patch")
    assert patched["function"]["parameters"] == {"type": "object"}


def test_strict_drops_longtail():
    """strict：长尾丢弃，模型不感知该工具。"""
    tools = [_tool("fs_read"), _tool("apply_patch"), _tool("glob")]
    ctx = tool_disguise.build_disguise_context(tools, "strict")
    names = [t["function"]["name"] for t in ctx.outbound_tools]
    assert names == ["Read"]


def test_passthrough_keeps_all_unchanged():
    """passthrough：tools 原样透传，不重命名/不丢弃。"""
    tools = [_tool("fs_read"), _tool("apply_patch")]
    ctx = tool_disguise.build_disguise_context(tools, "passthrough")
    assert [t["function"]["name"] for t in ctx.outbound_tools] == ["fs_read", "apply_patch"]
    assert ctx.restore_map == {}  # 不构建还原映射


def test_agent_extension_mapping():
    """agent 常见工具名（bash/read_file/edit）→ ta3 原生 schema。"""
    tools = [_tool("bash"), _tool("read_file"), _tool("edit"), _tool("grep")]
    ctx = tool_disguise.build_disguise_context(tools, "hybrid")
    names = [t["function"]["name"] for t in ctx.outbound_tools]
    assert names == ["Bash", "Read", "Edit", "Search"]


# ─────────────────────────── 入站还原 ───────────────────────────

def test_restore_request_scoped_agent_name():
    """入站：ta3 名按请求级 restore_map 还原为 agent 实际名+参数字段。"""
    tools = [_tool("bash"), _tool("read_file")]
    ctx = tool_disguise.build_disguise_context(tools, "hybrid")
    p = _provider(ctx)
    out = p._restore_tool_calls([
        {"id": "c1", "name": "Bash", "arguments": '{"command": "echo hi"}'},
        {"id": "c2", "name": "Read", "arguments": '{"filepath": "a.py", "offset": 1}'},
    ])
    assert [t["name"] for t in out] == ["bash", "read_file"]
    # read_file 的参数还原到 agent 的 file_path 键
    assert out[1]["arguments"] == {"file_path": "a.py", "offset": 1}


def test_restore_vendored_name_exact():
    """入站：chatcoder 内部名 terminal_exec → Bash → 还原回 terminal_exec（还原原名）。"""
    tools = [_tool("terminal_exec")]
    ctx = tool_disguise.build_disguise_context(tools, "hybrid")
    p = _provider(ctx)
    out = p._restore_tool_calls([{"id": "c1", "name": "Bash", "arguments": '{"command": "echo hi"}'}])
    assert out[0]["name"] == "terminal_exec"
    assert out[0]["arguments"] == {"command": "echo hi"}


def test_restore_longtail_preserved():
    """入站：未知名长尾（透传保留的 apply_patch）原样返回 agent。"""
    tools = [_tool("fs_read"), _tool("apply_patch")]
    ctx = tool_disguise.build_disguise_context(tools, "hybrid")
    p = _provider(ctx)
    out = p._restore_tool_calls([{"id": "c1", "name": "apply_patch", "arguments": '{"patch": "x"}'}])
    assert out[0]["name"] == "apply_patch"
    assert out[0]["arguments"] == {"patch": "x"}


def test_passthrough_restore_keeps_name():
    """passthrough：入站不还原（模型返回原名即原名）。"""
    tools = [_tool("bash")]
    ctx = tool_disguise.build_disguise_context(tools, "passthrough")
    p = _provider(ctx)
    out = p._restore_tool_calls([{"id": "c1", "name": "bash", "arguments": '{"command": "x"}'}])
    assert out[0]["name"] == "bash"


def test_restore_hybrid_native_name_passthrough_not_mismapped():
    """回归：hybrid 下 agent 直接用 ta3 原生英文名（Read/Edit）长尾透传，模型返回
    同名工具调用时不可被 FROM_TA3 误还原成 fs_read（否则 agent 收到不认识的工具，
    「调用工具直接停」）。"""
    tools = [_tool("Read"), _tool("Edit")]
    ctx = tool_disguise.build_disguise_context(tools, "hybrid")
    # 长尾透传：出站保留原名 schema
    assert [t["function"]["name"] for t in ctx.outbound_tools] == ["Read", "Edit"]
    assert "Read" in ctx.passthrough_names
    p = _provider(ctx)
    out = p._restore_tool_calls([
        {"id": "c1", "name": "Read", "arguments": '{"file_path": "a.py"}'},
        {"id": "c2", "name": "Edit", "arguments": '{"file_path": "a.py"}'},
    ])
    # 名字不被误映射成 fs_read / editor_apply_diff，参数也原样保留
    assert [t["name"] for t in out] == ["Read", "Edit"]
    assert out[0]["arguments"] == {"file_path": "a.py"}
    assert out[1]["arguments"] == {"file_path": "a.py"}


def test_history_hybrid_native_name_passthrough_kept():
    """回归：hybrid 透传工具的历史 tool_calls 多轮回传时原名保留（不降级成文本）。"""
    tools = [_tool("Read")]
    ctx = tool_disguise.build_disguise_context(tools, "hybrid")
    p = _provider(ctx)
    out = p._disguise_message(ChatMessage(role="assistant", content="",
        tool_calls=[{"id": "c1", "name": "Read", "arguments": {"file_path": "a.py"}}]))
    assert "tool_calls" in out
    assert out["tool_calls"][0]["function"]["name"] == "Read"
    assert json.loads(out["tool_calls"][0]["function"]["arguments"]) == {"file_path": "a.py"}


# ─────────────────────────── 历史消息伪装 ───────────────────────────

def test_history_disguise_hybrid_mapped():
    """hybrid：历史 tool_calls 重命名 + 参数适配（bash→Bash、edit→Edit）。"""
    tools = [_tool("bash"), _tool("edit")]
    ctx = tool_disguise.build_disguise_context(tools, "hybrid")
    p = _provider(ctx)
    out = p._disguise_message(ChatMessage(role="assistant", content="",
        tool_calls=[
            {"id": "c1", "name": "bash", "arguments": {"command": "echo hi"}},
            {"id": "c2", "name": "edit",
             "arguments": {"path": "a.py", "old_text": "x", "new_text": "y"}},
        ]))
    tcs = out["tool_calls"]
    assert tcs[0]["function"]["name"] == "Bash"
    assert json.loads(tcs[0]["function"]["arguments"]) == {"command": "echo hi"}
    assert tcs[1]["function"]["name"] == "Edit"
    assert json.loads(tcs[1]["function"]["arguments"]) == {
        "filepath": "a.py", "oldString": "x", "newString": "y"}


def test_history_unmapped_placeholder():
    """hybrid/strict：历史未映射调用降级文本占位，多轮不协议断裂。"""
    tools = [_tool("bash")]
    ctx = tool_disguise.build_disguise_context(tools, "hybrid")
    p = _provider(ctx)
    out = p._disguise_message(ChatMessage(role="assistant", content="",
        tool_calls=[{"id": "c1", "name": "some_unknown_tool", "arguments": {}}]))
    assert "tool_calls" not in out
    assert "当前环境不可用" in (out["content"] or "")


def test_history_passthrough_verbatim():
    """passthrough：历史调用原样透传（不重命名、不降级）。"""
    tools = [_tool("bash"), _tool("apply_patch")]
    ctx = tool_disguise.build_disguise_context(tools, "passthrough")
    p = _provider(ctx)
    out = p._disguise_message(ChatMessage(role="assistant", content="",
        tool_calls=[
            {"id": "c1", "name": "bash", "arguments": {"command": "echo hi"}},
            {"id": "c2", "name": "apply_patch", "arguments": {}},
        ]))
    names = [t["function"]["name"] for t in out["tool_calls"]]
    assert names == ["bash", "apply_patch"]


def test_history_disguise_anthropic():
    """Anthropic 协议（kimi）：历史 tool_use 重命名为 ta3 原生名。"""
    tools = [_tool("bash")]
    ctx = tool_disguise.build_disguise_context(tools, "hybrid")
    p = _provider(ctx, model="kimi-k3", meta={"anthropic": True})
    _, converted = p._convert_anthropic_messages([ChatMessage(
        role="assistant", content=None,
        tool_calls=[{"id": "c1", "name": "bash", "arguments": {"command": "echo hi"}}],
    )])
    blocks = converted[0]["content"]
    tool_use = next(b for b in blocks if b["type"] == "tool_use")
    assert tool_use["name"] == "Bash"
    assert tool_use["input"] == {"command": "echo hi"}


# ─────────────────────────── provider 预伪装开关 ───────────────────────────

def test_prepare_tools_default_strict():
    """直接使用 provider（未预伪装）：默认走 disguise_tools（strict，丢长尾）。"""
    p = Ta3Provider(api_key="x", base_url="b", model="m")
    req = ChatRequest(model="m", messages=[ChatMessage(role="user", content="x")],
                      tools=[_tool("terminal_exec"), _tool("glob_file_search")])
    names = [t["function"]["name"] for t in p._prepare_tools(req)]
    assert names == ["Bash"]


def test_prepare_tools_pre_disguised_passthrough():
    """relay 预伪装后：出站 schema 原样透传（含透传长尾）。"""
    tools = [_tool("Bash"), _tool("glob_file_search")]
    ctx = tool_disguise.build_disguise_context(tools, "passthrough")
    p = _provider(ctx)
    req = ChatRequest(model="m", messages=[ChatMessage(role="user", content="x")],
                      tools=tools)
    names = [t["function"]["name"] for t in p._prepare_tools(req)]
    assert names == ["Bash", "glob_file_search"]
