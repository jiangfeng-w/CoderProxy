"""M11 工具指纹采集 keyless 单测：归一化 + 落库去重 + 查询。

- 两种工具声明形态（OpenAI function / Anthropic tool）归一化成同一指纹。
- 同一 (name, param_sig) 只留一行，重复声明累加 calls。
- 不触发牛码网络；db 落 tmp 目录（monkeypatch settings.data_dir）。
"""
import asyncio

from relay import tool_inventory
from relay.config import settings


def _run(coro):
    return asyncio.run(coro)


def _openai_tool(name, parameters=None, description=""):
    return {"type": "function",
            "function": {"name": name, "description": description,
                         "parameters": parameters or {"type": "object", "properties": {}}}}


def _anthropic_tool(name, properties=None, description=""):
    return {"type": "tool", "name": name, "description": description,
            "input_schema": {"type": "object", "properties": properties or {}}}


# ─────────────────────────── 归一化 ───────────────────────────

def test_normalize_openai_function():
    tools = [_openai_tool("bash", {"type": "object", "properties": {
        "command": {"type": "string"}}}, "run a shell command")]
    fp = tool_inventory.normalize_tools(tools)
    assert len(fp) == 1
    assert fp[0]["name"] == "bash"
    assert fp[0]["description"] == "run a shell command"
    assert fp[0]["param_sig"] == "(command:string)"
    assert "command" in fp[0]["schema_json"]


def test_normalize_anthropic_tool():
    tools = [_anthropic_tool("fs_read", {"path": {"type": "string"}}, "read a file")]
    fp = tool_inventory.normalize_tools(tools)
    assert len(fp) == 1
    assert fp[0]["name"] == "fs_read"
    assert fp[0]["param_sig"] == "(path:string)"


def test_param_sig_stable_ordering():
    """参数签名与属性声明顺序无关，只与键名+类型有关。"""
    a = _openai_tool("edit", {"type": "object", "properties": {
        "b": {"type": "string"}, "a": {"type": "integer"}}})
    b = _openai_tool("edit", {"type": "object", "properties": {
        "a": {"type": "integer"}, "b": {"type": "string"}}})
    fa, fb = tool_inventory.normalize_tools([a, b])
    assert fa["param_sig"] == fb["param_sig"] == "(a:integer, b:string)"
    # 参数形态不同 → 签名不同（同工具名视为不同变体）
    c = _openai_tool("edit", {"type": "object", "properties": {"a": {"type": "string"}}})
    fc = tool_inventory.normalize_tools([c])[0]
    assert fc["param_sig"] != fa["param_sig"]


def test_normalize_ignores_empty_or_non_dict():
    """空/None/非 dict/无名 工具一律跳过。"""
    fp = tool_inventory.normalize_tools(None)
    assert fp == []
    fp = tool_inventory.normalize_tools([{}, {"type": "function", "function": {"name": ""}}])
    assert fp == []


# ─────────────────────────── 落库去重 + 查询 ───────────────────────────

def test_record_and_list_dedup(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    tools = [
        _openai_tool("bash", {"type": "object", "properties": {
            "command": {"type": "string"}}}, "run a shell command"),
        _openai_tool("apply_patch", {"type": "object"}),  # 无 properties
    ]
    n1 = _run(tool_inventory.record_tools(tools, "glm-x"))
    assert n1 == 2
    # 重复声明同一工具 → 只累计 calls，不新增行
    n2 = _run(tool_inventory.record_tools(tools, "glm-x"))
    assert n2 == 2
    rows = _run(tool_inventory.list_tools())
    assert len(rows) == 2
    by_name = {r["name"]: r for r in rows}
    assert by_name["bash"]["calls"] == 2
    assert by_name["apply_patch"]["calls"] == 2
    assert by_name["bash"]["model"] == "glm-x"
    # list_tools 按 calls 倒序（都一样时按 name 升序）
    assert rows[0]["name"] in ("apply_patch", "bash")


def test_record_upserts_new_variant_for_same_name(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    _run(tool_inventory.record_tools(
        [_openai_tool("read", {"type": "object", "properties": {"path": {"type": "string"}}})],
        "glm-x"))
    # 同名但参数形态不同 → 新行
    _run(tool_inventory.record_tools(
        [_openai_tool("read", {"type": "object", "properties": {"file_path": {"type": "string"}}})],
        "glm-y"))
    rows = _run(tool_inventory.list_tools())
    assert len(rows) == 2
    assert {r["name"] for r in rows} == {"read"}


def test_record_empty_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    assert _run(tool_inventory.record_tools(None, "glm-x")) == 0
    assert _run(tool_inventory.record_tools([], "glm-x")) == 0
    assert _run(tool_inventory.list_tools()) == []


def test_record_mixed_openai_and_anthropic(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    tools = [
        _openai_tool("bash", {"type": "object", "properties": {"command": {"type": "string"}}}),
        _anthropic_tool("fs_list", {"dirPath": {"type": "string"}}),
    ]
    _run(tool_inventory.record_tools(tools, "glm-x"))
    rows = _run(tool_inventory.list_tools())
    assert len(rows) == 2
    assert {r["name"] for r in rows} == {"bash", "fs_list"}
