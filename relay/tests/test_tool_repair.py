"""工具配对安全网单测（BUG-4）：merge_adjacent_tool_calls / fill_missing_tool_results /
repack_tool_results / cleanup_orphan_tool_calls / repair_tool_pairing（一键四层）。

纯函数级（wire dict 形态），对照参考 tool_pairing.go / chatcoder ensure_tool_pairing 语义：
- merge：相邻双 assistant.tool_calls → 合成一条（11148 形态；`content: []` 也算空）；
- fill：assistant.tool_calls 缺结果 → 补显式占位（不删调用、不动齐全批次）；
- repack：结果间夹插入物 → 后移；错位结果 → 窗口探测收拢；批内结果按声明序归位；
  同 id 重复留先到者；下一组组头不被吞；无改动返回原对象；
- cleanup：双侧对称裁剪（孤立结果删、无结果调用删/裁、**空 name 调用剔**、批内部分
  保留）；空 `tool_calls: []` 键清理；
- repair：merge→fill→repack→cleanup 串联，任何输入终态「无孤儿、无断裂」；
- 无工具流量 → 原对象（零改动）。
"""
from relay.tool_repair import (
    MISSING_RESULT_PLACEHOLDER,
    cleanup_orphan_tool_calls,
    fill_missing_tool_results,
    merge_adjacent_tool_calls,
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


def test_repack_relocates_misplaced_result_after_interstice():
    """错位形态：asst[c1] | user X | tool c1 → 结果前移收拢、X 后移（不要求紧邻）。"""
    msgs = [_asst(["c1"]), _user("X"), _tool("c1")]
    out = repack_tool_results(msgs)
    assert [m["role"] for m in out] == ["assistant", "tool", "user"]
    assert out[1]["tool_call_id"] == "c1"
    assert out[2] is msgs[1]  # 插入物后移、同对象


def test_repack_window_without_result_left_in_place():
    """窗口内无本组结果（纯孤儿调用）→ 原位不动、返回原对象（交 cleanup）。"""
    msgs = [_asst(["c1"]), _user("X"), {"role": "assistant", "content": "next"}]
    assert repack_tool_results(msgs) is msgs


def test_repack_reorders_results_to_declaration_order():
    """乱序结果：asst[c1,c2] | tool c2 | tool c1 → 按 tool_calls 声明序归位。"""
    msgs = [_asst(["c1", "c2"]), _tool("c2", "2"), _tool("c1", "1")]
    out = repack_tool_results(msgs)
    assert [m.get("tool_call_id") for m in out[1:]] == ["c1", "c2"]
    assert out[1]["content"] == "1" and out[2]["content"] == "2"  # 内容随消息走


def test_repack_reorder_with_interstice():
    """乱序 + 夹心并存：asst[c1,c2] | tool c2 | X | tool c1 → 结果声明序连续、X 后移。"""
    msgs = [_asst(["c1", "c2"]), _tool("c2"), _dev("X"), _tool("c1")]
    out = repack_tool_results(msgs)
    assert [m["role"] for m in out] == ["assistant", "tool", "tool", "developer"]
    assert [m.get("tool_call_id") for m in out[1:3]] == ["c1", "c2"]


def test_repack_dedupes_same_id_keeps_first():
    """同 id 重复结果：只留最先一条（后到者按重放/回声丢弃）。"""
    msgs = [_asst(["c1"]), _tool("c1", "first"), _tool("c1", "second")]
    out = repack_tool_results(msgs)
    assert [m["role"] for m in out] == ["assistant", "tool"]
    assert out[1]["content"] == "first"


def test_repack_declaration_order_no_change_same_object():
    """结果已按声明序连续 → 零改动（identity 保持）。"""
    msgs = [_asst(["c1", "c2"]), _tool("c1"), _tool("c2"), _user("next")]
    assert repack_tool_results(msgs) is msgs


def test_repack_duplicate_noncontiguous_dedupes_keeps_first():
    """重复 + 夹心：asst[c1] | tool c1(first) | X | tool c1(second) → 留先到、X 后移。"""
    msgs = [_asst(["c1"]), _tool("c1", "first"), _dev("X"), _tool("c1", "second")]
    out = repack_tool_results(msgs)
    assert [m["role"] for m in out] == ["assistant", "tool", "developer"]
    assert out[1]["content"] == "first"
    assert out[2] is msgs[2]


def test_repack_duplicate_empty_placeholder_yields_to_real_result():
    """空占位 + 真结果并存：真结果胜出（对齐 OmniRoute preferResult：有内容者优先）。"""
    msgs = [_asst(["c1"]), _tool("c1", ""), _tool("c1", "real")]
    out = repack_tool_results(msgs)
    assert [m["role"] for m in out] == ["assistant", "tool"]
    assert out[1]["content"] == "real"


def test_repack_duplicate_real_result_beats_later_empty():
    """真结果在前 + 空占位在后：真结果保留（非空者优先，先到非空不回退）。"""
    msgs = [_asst(["c1"]), _tool("c1", "real"), _tool("c1", "")]
    out = repack_tool_results(msgs)
    assert [m["role"] for m in out] == ["assistant", "tool"]
    assert out[1]["content"] == "real"


def test_repack_duplicate_missing_content_yields_to_real_result():
    """缺 content 键（None）的占位 + 真结果：真结果胜出（null 视为空）。"""
    msgs = [_asst(["c1"]), {"role": "tool", "tool_call_id": "c1"}, _tool("c1", "real")]
    out = repack_tool_results(msgs)
    assert len([m for m in out if m.get("role") == "tool"]) == 1
    assert out[-1]["content"] == "real"


def test_repack_duplicate_whitespace_only_yields_to_real_result():
    """仅空白的 content（" "）视为空占位：真结果胜出。"""
    msgs = [_asst(["c1"]), _tool("c1", " "), _tool("c1", "real")]
    out = repack_tool_results(msgs)
    assert out[-1]["content"] == "real"


def test_repack_duplicate_both_empty_keeps_first():
    """双空占位并存：保持先到者（空与空等价，不做无谓替换）。"""
    msgs = [_asst(["c1"]), _tool("c1", ""), _tool("c1", " ")]
    out = repack_tool_results(msgs)
    assert len([m for m in out if m.get("role") == "tool"]) == 1
    assert out[1]["content"] == ""


def test_repack_duplicate_empty_noncontiguous_yields_to_real():
    """非连续重复：空占位 | X | 真结果 → 真结果胜出、X 后移（窗口内同口径）。"""
    msgs = [_asst(["c1"]), _tool("c1", ""), _dev("X"), _tool("c1", "real")]
    out = repack_tool_results(msgs)
    assert [m["role"] for m in out] == ["assistant", "tool", "developer"]
    assert out[1]["content"] == "real"
    assert out[2] is msgs[2]


def test_repack_orphan_tool_does_not_block_relocation():
    """孤儿 tool 挡在组头与乱序结果之间：不阻碍探测与归位（孤儿后移、交 cleanup 裁）。"""
    msgs = [_asst(["c1", "c2"]), _tool("ghost"), _tool("c2"), _tool("c1")]
    out = repack_tool_results(msgs)
    assert [m.get("tool_call_id") for m in out[1:]] == ["c1", "c2", "ghost"]


def test_repair_orphan_between_head_and_results_full_pipeline():
    """全管线：asst[c1,c2] | tool ghost | tool c2 | tool c1 → 归位 + 孤儿删除。"""
    msgs = [_asst(["c1", "c2"]), _tool("ghost"), _tool("c2", "2"), _tool("c1", "1")]
    out = repair_tool_pairing(msgs)
    assert [m["role"] for m in out] == ["assistant", "tool", "tool"]
    assert [m.get("tool_call_id") for m in out[1:]] == ["c1", "c2"]
    assert [m["content"] for m in out[1:]] == ["1", "2"]


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


def test_cleanup_empty_tool_calls_key_removed():
    """空 tool_calls: [] 键清理（消息本身保留、其余不动）——部分严格实现拒绝空数组。"""
    empty = {"role": "assistant", "content": "答", "tool_calls": []}
    msgs = [_user("hi"), empty]
    out = cleanup_orphan_tool_calls(msgs)
    assert "tool_calls" not in out[1]
    assert out[1]["content"] == "答"


def test_cleanup_empty_tool_calls_alongside_real_traffic():
    """空 tool_calls 与真实工具流量并存：两条路径同帧清理（空键删 + 孤儿删）。"""
    empty = {"role": "assistant", "content": "答", "tool_calls": []}
    msgs = [_asst(["c1"]), _tool("c1"), empty, _tool("ghost")]
    out = cleanup_orphan_tool_calls(msgs)
    assert "tool_calls" not in out[2]
    assert [m.get("tool_call_id") for m in out if m.get("role") == "tool"] == ["c1"]


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


def test_repair_misplaced_result_full_pipeline():
    """错位形态全管线：asst[c1] | user X | tool c1 → 结果收拢紧跟、X 后移。"""
    msgs = [_asst(["c1"]), _user("X"), _tool("c1", "res")]
    out = repair_tool_pairing(msgs)
    assert [m["role"] for m in out] == ["assistant", "tool", "user"]
    assert out[1]["tool_call_id"] == "c1"
    assert out[2]["content"] == "X"


def test_repair_partial_missing_with_misplaced_real_result():
    """部分缺 + 真实结果错位：asst[c1,c2] | user X | tool c1 →
    c2 补占位、c1 归位，终态按声明序连续（占位排在真实结果之后）。"""
    msgs = [_asst(["c1", "c2"]), _user("X"), _tool("c1", "real")]
    out = repair_tool_pairing(msgs)
    assert [m["role"] for m in out] == ["assistant", "tool", "tool", "user"]
    assert [m["tool_call_id"] for m in out[1:3]] == ["c1", "c2"]
    assert out[1]["content"] == "real"
    assert out[2]["content"] == MISSING_RESULT_PLACEHOLDER


def test_repair_duplicate_results_full_pipeline():
    """重复结果全管线：同 id 两条 → 只留先到一条、批次连续。"""
    msgs = [_asst(["c1"]), _tool("c1", "first"), _tool("c1", "second")]
    out = repair_tool_pairing(msgs)
    assert [m["role"] for m in out] == ["assistant", "tool"]
    assert out[1]["content"] == "first"


def test_repair_empty_tool_calls_full_pipeline():
    """空 tool_calls 键全管线：键清除、消息保留、其余零改动。"""
    msgs = [_user("hi"), {"role": "assistant", "content": "答", "tool_calls": []}]
    out = repair_tool_pairing(msgs)
    assert "tool_calls" not in out[1]
    assert out[0] is msgs[0]


# ──────────────── merge：相邻 assistant.tool_calls 合并（11148 形态） ────────────────

def _call(cid: str) -> dict:
    return {"id": cid, "type": "function", "function": {"name": "Bash", "arguments": "{}"}}


def test_merge_back_to_back_tool_calls():
    """背靠背两条 assistant.tool_calls（后条无正文）→ 合并成一条，调用按序拼接。"""
    msgs = [_user(), _asst(["c1"]), _asst(["c2"])]
    out = merge_adjacent_tool_calls(msgs)
    assert [m["role"] for m in out] == ["user", "assistant"]
    assert [tc["id"] for tc in out[1]["tool_calls"]] == ["c1", "c2"]


def test_merge_empty_array_content_also_merges():
    """`content: []` 也算空——部分客户端用它表达「无正文」（条件漏 [] 即漏合并）。"""
    msgs = [{"role": "assistant", "content": [], "tool_calls": [_call("c1")]},
            {"role": "assistant", "content": [], "tool_calls": [_call("c2")]}]
    out = merge_adjacent_tool_calls(msgs)
    assert len(out) == 1
    assert [tc["id"] for tc in out[0]["tool_calls"]] == ["c1", "c2"]


def test_merge_moves_and_joins_reasoning_content():
    """reasoning_content 不丢：后条拆迁、两边都有换行拼接（deepseek 多轮回填）。"""
    msgs = [{"role": "assistant", "content": None, "reasoning_content": "前",
             "tool_calls": [_call("c1")]},
            {"role": "assistant", "content": "", "reasoning_content": "后",
             "tool_calls": [_call("c2")]}]
    out = merge_adjacent_tool_calls(msgs)
    assert out[0]["reasoning_content"] == "前\n后"


def test_merge_folds_text_into_prev_call_head():
    """反向形态：纯正文 assistant 折进上一条无正文的调用声明。"""
    msgs = [_asst(["c1"]), {"role": "assistant", "content": "这是正文"}]
    out = merge_adjacent_tool_calls(msgs)
    assert len(out) == 1
    assert out[0]["content"] == "这是正文"
    assert [tc["id"] for tc in out[0]["tool_calls"]] == ["c1"]


def test_merge_skips_when_not_adjacent_or_non_empty():
    """从严不合并：中间隔消息 / 后条带正文且带 tool_calls → 原对象返回（零改动）。"""
    not_adjacent = [_asst(["c1"]), _user("x"), _asst(["c2"])]
    assert merge_adjacent_tool_calls(not_adjacent) is not_adjacent
    with_text = [_asst(["c1"]), _asst(["c2"], content="正文")]
    assert merge_adjacent_tool_calls(with_text) is with_text


def test_repair_merge_before_fill_full_pipeline():
    """全管线顺序：merge 先行 → 合并批缺结果照常补占位（终态一批调用接两结果）。"""
    msgs = [_asst(["c1"]), _asst(["c2"]), _tool("c1", "real")]
    out = repair_tool_pairing(msgs)
    assert [m["role"] for m in out] == ["assistant", "tool", "tool"]
    assert [tc["id"] for tc in out[0]["tool_calls"]] == ["c1", "c2"]
    assert out[1]["content"] == "real"
    assert out[2]["content"] == MISSING_RESULT_PLACEHOLDER


# ──────────────── cleanup：function.name 为空的调用剔除（11133 形态） ────────────────

def _asst_named(entries: list) -> dict:
    """entries = [(id, name)]；name=None 表示不带 function 键。"""
    tcs = []
    for cid, name in entries:
        tc = {"id": cid, "type": "function"}
        if name is not None:
            tc["function"] = {"name": name, "arguments": "{}"}
        tcs.append(tc)
    return {"role": "assistant", "content": None, "tool_calls": tcs}


def test_cleanup_drops_empty_name_call_with_result():
    """空 name 调用带配对结果 → 两侧对称剔除（否则上游判 11133 顶死会话）。"""
    msgs = [_asst_named([("c1", "")]), _tool("c1")]
    out = cleanup_orphan_tool_calls(msgs)
    assert len(out) == 1
    assert "tool_calls" not in out[0]


def test_cleanup_whitespace_name_is_dirty():
    msgs = [_asst_named([("c1", "   ")]), _tool("c1")]
    out = cleanup_orphan_tool_calls(msgs)
    assert len(out) == 1 and "tool_calls" not in out[0]


def test_cleanup_missing_function_key_not_dirty():
    """缺 function 键的历史占位条目不判脏（参考实现口径：不扩大打击面）。"""
    msgs = [_asst_named([("c1", None)]), _tool("c1")]
    out = cleanup_orphan_tool_calls(msgs)
    assert len(out) == 2
    assert out[0]["tool_calls"][0]["id"] == "c1"


def test_cleanup_mixed_batch_only_dirty_trimmed():
    """批内混合：只剔空 name 项，其余照常保留；两侧对称。"""
    msgs = [_asst_named([("c1", ""), ("c2", "Bash")]), _tool("c1"), _tool("c2")]
    out = cleanup_orphan_tool_calls(msgs)
    assert [tc["id"] for tc in out[0]["tool_calls"]] == ["c2"]
    assert [m["tool_call_id"] for m in out[1:]] == ["c2"]


def test_repair_empty_name_full_pipeline():
    """全管线：空 name 调用剔除后批次仍连续（结果侧同去）。"""
    msgs = [_asst_named([("c1", ""), ("c2", "Bash")]), _tool("c1"), _tool("c2")]
    out = repair_tool_pairing(msgs)
    assert [m["role"] for m in out] == ["assistant", "tool"]
    assert out[1]["tool_call_id"] == "c2"
