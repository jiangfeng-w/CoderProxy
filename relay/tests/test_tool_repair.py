"""工具配对安全网单测（BUG-4）：fill_missing_tool_results / repack_tool_results /
cleanup_orphan_tool_calls / repair_tool_pairing（一键三层）。

纯函数级（wire dict 形态），对照参考 tool_pairing.go / chatcoder ensure_tool_pairing 语义：
- fill：assistant.tool_calls 缺结果 → 补显式占位（不删调用、不动齐全批次）；
- repack：结果间夹插入物 → 后移；下一组组头不被吞；无改动返回原对象；
- cleanup：双侧对称裁剪（孤立结果删、无结果调用删/裁、批内部分保留）；
- repair：fill→repack→cleanup 串联，任何输入终态「无孤儿、无断裂」；
- 无工具流量 → 原对象（零改动）。
"""
from relay.tool_repair import (
    MISSING_RESULT_PLACEHOLDER,
    cleanup_orphan_tool_calls,
    fill_missing_tool_results,
    repack_tool_results,
    repair_tool_pairing,
)


def _asst(call_ids: list[str], content=None) -> dict:
    return {"role": "assistant", "content": content,
            "tool_calls": [{"id": cid, "type": "function",
                            "function": {"name": "Bash", "arguments": "{}"}}
                           for cid in call_ids]}


def _tool(cid: str, text: str = "ok") -> dict:
    return {"role": "tool", "tool_call_id": cid, "content": text}


def _user(text: str = "hi") -> dict:
    return {"role": "user", "content": text}


def _dev(text: str = "notice") -> dict:
    return {"role": "developer", "content": text}


# ─────────────────────────── repack_tool_results ───────────────────────────

def test_repack_moves_developer_between_results_to_after_group():
    """assistant[c1,c2] | tool c1 | developer X | tool c2 → 两条结果连续，X 后移。"""
    msgs = [_asst(["c1", "c2"]), _tool("c1"), _dev("image_resize_notice"), _tool("c2")]
    out = repack_tool_results(msgs)
    assert [m["role"] for m in out] == ["assistant", "tool", "tool", "developer"]
    assert [m.get("tool_call_id") for m in out[:3]] == [None, "c1", "c2"]
    assert out[3] is msgs[2]  # 只调顺序、不改内容（同对象引用）


def test_repack_no_insertion_returns_same_object():
    """结果本来连续 → 零改动（返回原对象，调用方可按 identity 判断）。"""
    msgs = [_asst(["c1"]), _tool("c1"), _user("next")]
    assert repack_tool_results(msgs) is msgs


def test_repack_does_not_swallow_next_group_head():
    """两组紧邻：asst[c1]|tool c1|asst[c2]|tool c2 —— 下一组组头绝不能被当插入物吞掉。"""
    msgs = [_asst(["c1"]), _tool("c1"), _asst(["c2"]), _tool("c2")]
    assert repack_tool_results(msgs) is msgs


def test_repack_trailing_insertion_is_kept_in_place():
    """插入物在整组结果之后（已是合法位置）→ 不改动。"""
    msgs = [_asst(["c1"]), _tool("c1"), _dev("after")]
    assert repack_tool_results(msgs) is msgs


def test_repack_batch_without_results_left_for_cleanup():
    """assistant 后没有结果（孤儿调用形态）→ repack 不动，交 cleanup 处理。"""
    msgs = [_asst(["c1"]), _dev("X"), _user("next")]
    assert repack_tool_results(msgs) is msgs


def test_repack_short_input_unchanged():
    assert repack_tool_results([]) == []
    two = [_asst(["c1"]), _tool("c1")]
    assert repack_tool_results(two) is two


# ─────────────────────────── cleanup_orphan_tool_calls ───────────────────────────

def test_cleanup_orphan_result_removed():
    """孤立 tool 结果（无对应调用）整条删除，其余消息原样。"""
    msgs = [_user("hi"), _tool("ghost")]
    out = cleanup_orphan_tool_calls(msgs)
    assert out == [_user("hi")]


def test_cleanup_call_without_result_drops_tool_calls_key():
    """无结果的 tool_call → 从 tool_calls 剔除；剔空则删键（消息本身保留）。"""
    msgs = [_asst(["c1"], content="边想边说"), _user("next")]
    out = cleanup_orphan_tool_calls(msgs)
    assert "tool_calls" not in out[0]
    assert out[0]["content"] == "边想边说"


def test_cleanup_partial_batch_keeps_paired_side_symmetrically():
    """批内部分保留：c1 有结果、c2 没有 → 只留 c1；结果侧同步只留 c1（无孤儿、无断裂）。"""
    msgs = [_asst(["c1", "c2"]), _tool("c1"), _user("next")]
    out = cleanup_orphan_tool_calls(msgs)
    assert [tc["id"] for tc in out[0]["tool_calls"]] == ["c1"]
    assert [m["role"] for m in out] == ["assistant", "tool", "user"]


def test_cleanup_full_batch_zero_change_same_object():
    """整批齐全 → 零改动（原对象）。"""
    msgs = [_asst(["c1", "c2"]), _tool("c1"), _tool("c2"), _user("next")]
    assert cleanup_orphan_tool_calls(msgs) is msgs


def test_cleanup_no_tool_traffic_same_object():
    """无任何工具流量 → 原对象（不分配）。"""
    msgs = [_user("hi"), {"role": "assistant", "content": "答"}]
    assert cleanup_orphan_tool_calls(msgs) is msgs
    assert cleanup_orphan_tool_calls([]) == []


def test_cleanup_both_sides_trimmed_together():
    """双侧孤儿同帧清理：孤立结果删 + 无结果调用裁，一次到位。"""
    msgs = [_asst(["c1"]), _user("mid"), _tool("ghost"), _tool("c1")]
    out = cleanup_orphan_tool_calls(msgs)
    assert out == [_asst(["c1"]), _user("mid"), _tool("c1")]


# ─────────────────────────── 组合（先 repack 再 cleanup） ───────────────────────────

def test_combined_malformed_history_no_orphans_no_breaks():
    """畸形历史（插入物 + 孤立结果 + 无结果调用）→ repack+cleanup 后无孤儿、无断裂。"""
    msgs = [
        _user("问"),
        _asst(["c1", "c2"]),          # 批 1
        _tool("c1"),
        _dev("notice"),               # 夹心：要后移
        _tool("c2"),
        _asst(["c3"]),                # 无结果调用：要剔除 tool_calls
        _tool("ghost"),               # 孤立结果：要删除
        _asst(["c4"]),                # 批 2
        _tool("c4"),
    ]
    out = cleanup_orphan_tool_calls(repack_tool_results(msgs))

    # 每条 assistant 的结果紧跟其后，中间不夹任何消息
    for i, m in enumerate(out):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            want = {tc["id"] for tc in m["tool_calls"]}
            following = [x for x in out[i + 1:] if x.get("role") == "tool"
                         and x.get("tool_call_id") in want]
            assert len(following) == len(want)  # 结果齐全
            nxt = out[i + 1]
            assert nxt.get("role") == "tool"    # 紧接着就是结果
            assert nxt.get("tool_call_id") in want
    # 无孤儿：每条 tool 消息都有前置调用；无无主 tool_calls
    call_ids = {tc["id"] for m in out if isinstance(m, dict)
                for tc in (m.get("tool_calls") or [])}
    for m in out:
        if m.get("role") == "tool":
            assert m["tool_call_id"] in call_ids
    assert "tool_calls" not in out[5]  # c3 无结果 → 键已删
    # 插入物后移到批 1 结果之后、c3 之前
    dev_pos = next(i for i, m in enumerate(out) if m.get("role") == "developer")
    c2_pos = next(i for i, m in enumerate(out) if m.get("tool_call_id") == "c2")
    assert dev_pos > c2_pos


# ─────────────────────────── fill_missing_tool_results ───────────────────────────

def test_fill_appends_placeholder_for_missing_result():
    """缺结果的调用 → 补显式占位（紧跟既有结果之后），调用记录不丢。"""
    msgs = [_asst(["c1", "c2"]), _tool("c1", "1")]
    out = fill_missing_tool_results(msgs)
    assert [m["role"] for m in out] == ["assistant", "tool", "tool"]
    assert out[1]["tool_call_id"] == "c1" and out[1]["content"] == "1"  # 既有结果在前
    assert out[2]["tool_call_id"] == "c2"
    assert out[2]["content"] == MISSING_RESULT_PLACEHOLDER


def test_fill_no_missing_returns_same_object():
    """批内结果齐全 → 零改动（返回原对象）。"""
    msgs = [_asst(["c1", "c2"]), _tool("c1"), _tool("c2")]
    assert fill_missing_tool_results(msgs) is msgs


def test_fill_batch_without_any_result_gets_all_placeholders():
    """整批无结果（如 agent 丢了全部结果）→ 每个调用都补占位。"""
    msgs = [_user("问"), _asst(["c1", "c2"]), _user("下一轮")]
    out = fill_missing_tool_results(msgs)
    assert [m["role"] for m in out] == ["user", "assistant", "tool", "tool", "user"]
    assert [m["tool_call_id"] for m in out[2:4]] == ["c1", "c2"]
    assert all(m["content"] == MISSING_RESULT_PLACEHOLDER for m in out[2:4])


def test_fill_no_tool_traffic_same_object():
    msgs = [_user("hi"), {"role": "assistant", "content": "答"}]
    assert fill_missing_tool_results(msgs) is msgs


# ─────────────────────────── repair_tool_pairing（一键三层） ───────────────────────────

def test_repair_fills_then_repacks_to_contiguous():
    """缺结果 + 夹心并存：占位补齐后仍归位到连续形态（fill→repack 同序验证）。"""
    msgs = [_asst(["c1", "c2"]), _tool("c1"), _dev("notice")]
    out = repair_tool_pairing(msgs)
    # c2 无结果 → 补占位；developer 是结果块外的插入物（无结果续接）→ 保持在组后
    roles = [m["role"] for m in out]
    assert roles == ["assistant", "tool", "tool", "developer"]
    assert out[2]["tool_call_id"] == "c2"


def test_repair_missing_result_between_results():
    """真实断裂形态：asst[c1,c2] + tool c1 + X + tool c2（X 夹心）→ 全部归位连续。"""
    msgs = [_asst(["c1", "c2"]), _tool("c1"), _dev("X"), _tool("c2")]
    out = repair_tool_pairing(msgs)
    assert [m["role"] for m in out] == ["assistant", "tool", "tool", "developer"]
    assert [m.get("tool_call_id") for m in out[1:3]] == ["c1", "c2"]


def test_repair_missing_result_batch_eats_between_first():
    """缺结果的批 + 结果夹心：fill 先吃既有结果、补占位，repack 再整体归位。"""
    msgs = [_asst(["c1", "c2"]), _tool("c1"), _dev("X")]
    out = repair_tool_pairing(msgs)
    assert [m["role"] for m in out] == ["assistant", "tool", "tool", "developer"]
    assert out[1]["tool_call_id"] == "c1"
    assert out[2]["tool_call_id"] == "c2"
    assert out[2]["content"] == MISSING_RESULT_PLACEHOLDER
    assert out[3].get("content") == "X"


def test_repair_malformed_full_pipeline():
    """混合畸形（缺结果 + 孤儿结果 + 无结果调用 + 夹心）→ 终态每批连续、无孤儿。"""
    msgs = [
        _user("问"),
        _asst(["c1", "c2"]),   # 批 1：c2 缺结果
        _tool("c1"),
        _dev("notice"),        # 夹心
        _asst(["c3"]),         # 无结果的调用
        _tool("ghost"),        # 孤儿结果
        _asst(["c4"]),         # 批 2：完整
        _tool("c4"),
    ]
    out = repair_tool_pairing(msgs)
    # c3 无结果：fill 已经给 c3 补过占位（不删调用）——三批都有结果、无孤儿
    for i, m in enumerate(out):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            want = {tc["id"] for tc in m["tool_calls"]}
            nxt = out[i + 1]
            assert nxt.get("role") == "tool" and nxt["tool_call_id"] in want
    # 孤儿结果 ghost 已删；c3 的占位在（fill 语义）
    ids = [m.get("tool_call_id") for m in out if m.get("role") == "tool"]
    assert "ghost" not in ids and "c3" in ids
