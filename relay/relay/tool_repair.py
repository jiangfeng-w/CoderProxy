"""工具配对安全网（BUG-4「出站安全网」，spec §4.5.3 规划落地）。

对齐参考实现 `workbuddy2api-panel/internal/upstream/tool_pairing.go`（逐条移植）并
吸收 `chatcoder`/`9router` 的「补缺失结果」路线，共三层（`repair_tool_pairing` 一键串联）：

- `fill_missing_tool_results(messages)`：给 assistant.tool_calls 中**缺结果**的调用补一条
  显式占位 tool 消息（对齐 chatcoder `ensure_tool_pairing` 的「补显式丢失占位而非假数据」；
  9router `fixMissingToolResponses` 只补空串，占位文案对模型更友好——调用记录不丢、
  模型知道该结果不可依赖）；
- `repack_tool_results(messages)`：把夹在 assistant.tool_calls 与其 tool 结果之间的
  **非 tool 消息**后移，保证同一批 tool_call 的结果在 wire 上连续（Codex 的
  `<image_resize_notice>` developer 消息会插在两份结果中间）；
- `cleanup_orphan_tool_calls(messages)`：按 id 集合**双侧对称**裁剪孤儿——
  assistant.tool_calls 只留有结果配对的项（空了删 `tool_calls` 键），`role:tool`
  只留调用被保留的项（孤儿结果整条删）。

背景：OpenAI 兼容协议要求带 tool_calls 的 assistant 之后**紧跟**其每条结果，
中间夹任何消息、或任一侧缺配对，上游都会 400（WorkBuddy code=11148，
tool_call_sequence_broken）并顶死整条会话。网关是最后一道防线：能补齐的补齐
（显式占位），补不齐的（双侧都无主的）裁掉——宁可丢一轮工具上下文，也好过整条
会话报废。

调用序（`repair_tool_pairing`）：**先 fill 再 repack 再 cleanup**——
- fill 先行：缺失的占位插在 assistant 之后，随后 repack 可把它与既有结果一并归位
  （若先 repack 再 fill，`asst[c1,c2] + tool c1 + X` 这类输入会把新占位插在 X 前、
  与 c1 结果错位）；
- cleanup 收尾：仍是「让请求通过」的安全网，双侧对称裁剪，任何输入都不会再产生半截配对。

参考注释里的历史坑（勿复现）：孤儿清理**不能**用「批内不全就删整批」的旧实现——
那会留下无主结果（批 [c1,c2] 只回了 c1 时，调用侧整批删而结果侧仍按 id 命中），
出站变成「无 tool_calls 的 assistant + 孤儿 tool」，同样判 11148。两侧必须共用
同一份 keepCalls 按 id 对称裁剪。

本模块不 import relay 其它模块（纯函数，adapter 与 translator 共用）。
"""
from __future__ import annotations

# 缺结果占位文案（对齐 chatcoder 的显式丢失标注：不注入假数据、提示模型勿依赖、
# 必要时重跑工具）。以 [relay placeholder] 前缀自证来源，便于上游/日志排查。
MISSING_RESULT_PLACEHOLDER = (
    "[relay placeholder] The result of this tool call is missing from the conversation "
    "history. It is not a real result — do not rely on it, and re-run the tool if needed."
)


def _tool_call_ids(msg: dict) -> list[str]:
    """assistant 消息的 tool_calls id 列表（非 dict / 非非空字符串 id 一律跳过）。"""
    tcs = msg.get("tool_calls")
    if not isinstance(tcs, list):
        return []
    ids: list[str] = []
    for tc in tcs:
        if isinstance(tc, dict):
            tid = tc.get("id")
            if isinstance(tid, str) and tid:
                ids.append(tid)
    return ids


def _is_group_head(msg: object) -> bool:
    """assistant 且带非空 tool_calls = 一组工具结果的组头。"""
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return False
    tcs = msg.get("tool_calls")
    return isinstance(tcs, list) and len(tcs) > 0


def fill_missing_tool_results(messages: list) -> list:
    """给 assistant.tool_calls 中缺结果的调用补一条显式占位 tool 消息。

    对齐 chatcoder `ensure_tool_pairing` 的「补显式丢失占位而非假数据」；9router
    `fixMissingToolResponses` 只就近检查并补空串，本实现按全局 id 集合判定（不依赖
    紧邻假设，与 cleanup 同口径），占位文案提示模型「该结果不可依赖」。

    - 占位消息紧跟产出它的 assistant 消息之后的**该批既有结果之后**插入
      （保持「assistant → 本批结果」的连续形态；repack 会随后保证严格连续）；
    - 该 assistant 的 tool_calls 为空 / 无 tool_calls / 已有结果 → 不动；
    - 无任何工具流量 → 原对象原样返回（零分配零改动）。
    """
    if not messages:
        return messages
    # 全局结果集（用于判定哪些调用缺结果）
    result_ids: set[str] = set()
    has_traffic = False
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "tool":
            tid = m.get("tool_call_id")
            if isinstance(tid, str) and tid:
                result_ids.add(tid)
            has_traffic = has_traffic or bool(isinstance(tid, str) and tid)
        elif isinstance(m, dict) and m.get("role") == "assistant":
            has_traffic = has_traffic or bool(_tool_call_ids(m))
    if not has_traffic:
        return messages

    out: list = []
    changed = False
    i = 0
    n = len(messages)
    while i < n:
        m = messages[i]
        out.append(m)
        i += 1
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        batch_ids = _tool_call_ids(m)
        if not batch_ids:
            continue
        missing = [tid for tid in batch_ids if tid not in result_ids]
        if not missing:
            continue
        # 先吃掉本批紧随的既有结果（保持原有相对顺序），占位再补在其后；
        # 中间夹插入物的断续形态交由 repack 统一归位。
        want = set(batch_ids)
        while i < n:
            nxt = messages[i]
            if (isinstance(nxt, dict) and nxt.get("role") == "tool"
                    and isinstance(nxt.get("tool_call_id"), str)
                    and nxt["tool_call_id"] in want):
                out.append(nxt)
                i += 1
                continue
            break
        for tid in missing:
            out.append({"role": "tool", "tool_call_id": tid,
                        "content": MISSING_RESULT_PLACEHOLDER})
            changed = True
    if not changed:
        return messages
    return out


def repair_tool_pairing(messages: list) -> list:
    """工具配对一键修复：fill（补缺失显式占位）→ repack（归位）→ cleanup（裁孤儿）。

    参考管线同序（chatcoder：`repair_tool_call_ids → ensure_tool_pairing →
    normalize_tool_sequence`；2api-panel 出站：`repack → cleanup`）。任何输入
    经此函数后保证：带 tool_calls 的 assistant 之后**连续**跟着本批全部结果，
    且无孤儿（无主 tool 消息 / 无结果调用）。
    """
    return cleanup_orphan_tool_calls(repack_tool_results(fill_missing_tool_results(messages)))


def repack_tool_results(messages: list) -> list:
    """把插在 assistant.tool_calls 与其结果之间的非 tool 消息挪到整组之后。

    ```text
    asst tool_calls=[c00 c01] | tool c00 | X | tool c01
        → asst tool_calls=[c00 c01] | tool c00 | tool c01 | X
    ```

    只调顺序、不改内容；结果顺序保持不变（同批调用的原相对顺序 = 结果顺序）。
    无插入消息时零改动（返回原对象）。下一组 assistant.tool_calls 是新的组头，
    绝不会被当作插入物吞掉（否则它自己那批结果永远得不到重排）。
    """
    if len(messages) < 3:
        return messages
    out: list = []
    changed = False
    i = 0
    n = len(messages)
    while i < n:
        m = messages[i]
        if not _is_group_head(m):
            out.append(m)
            i += 1
            continue
        want = set(_tool_call_ids(m))
        out.append(m)
        i += 1
        results: list = []
        between: list = []
        saw_non_tool = False
        while i < n:
            mm = messages[i]
            if not isinstance(mm, dict):
                break
            role = mm.get("role")
            if role == "tool":
                tid = mm.get("tool_call_id")
                if not (isinstance(tid, str) and tid in want):
                    break  # 不属于本组的结果（下一组/孤儿）：交外层处理
                results.append(mm)
                if saw_non_tool:
                    changed = True
                i += 1
                continue
            if not results:
                break  # assistant 后还没有结果：交 cleanup_orphan_tool_calls 处理
            if _is_group_head(mm):
                break  # 下一组组头，绝不能当插入物吞掉
            # 同批结果尚未收齐时，中间消息视为插入物，暂存待后移
            between.append(mm)
            saw_non_tool = True
            i += 1
        out.extend(results)
        out.extend(between)
    if not changed:
        return messages
    return out


def cleanup_orphan_tool_calls(messages: list) -> list:
    """剔除无法配对的 tool_call 与 tool 结果（对齐参考 resolveToolPairing 语义）。

    - 收集全线 `role:tool` 的 tool_call_id（结果集）与 `assistant.tool_calls[].id`
      （调用集）；两侧共用 keepCalls（调用存在**且**结果存在）按 id 对称裁剪：
      assistant 只留有结果的调用（过滤后为空则删 `tool_calls` 键，不删消息本身；
      内容为空字符串同样的空壳消息是合法 wire 形态）；`role:tool` 只在对应调用
      被保留时保留，否则删除整条消息；
    - 无任何工具流量 → 原对象原样返回（零分配零改动）。

    部分保留不会留下无结果的 tool_call（两侧同口径），这是「让请求通过」的安全网。
    """
    if not messages:
        return messages
    call_ids: set[str] = set()
    result_ids: set[str] = set()
    has_traffic = False
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "tool":
            tid = m.get("tool_call_id")
            if isinstance(tid, str) and tid:
                result_ids.add(tid)
                has_traffic = True
        elif role == "assistant":
            for tid in _tool_call_ids(m):
                call_ids.add(tid)
                has_traffic = True
    if not has_traffic:
        return messages

    keep_calls = {tid for tid in call_ids if tid in result_ids}

    # 1) assistant.tool_calls：按 keep_calls 对称裁剪——只留有结果的调用，
    #    过滤后为空则删键（就地改写，同参考实现；无改动消息保持原样）。
    changed = False
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        tcs = m.get("tool_calls")
        if not isinstance(tcs, list) or not tcs:
            continue
        kept: list = []
        for tc in tcs:
            if not isinstance(tc, dict):
                continue
            tid = tc.get("id")
            if isinstance(tid, str) and tid in keep_calls:
                kept.append(tc)
        if len(kept) == len(tcs):
            continue  # 整批齐全：零改动
        changed = True
        if not kept:
            m.pop("tool_calls", None)
            continue
        m["tool_calls"] = kept

    # 2) role:tool 结果：只有对应调用被保留才保留；孤儿结果整条删除。
    kept_msgs: list = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "tool":
            tid = m.get("tool_call_id")
            if not (isinstance(tid, str) and tid in keep_calls):
                changed = True
                continue
        kept_msgs.append(m)
    if not changed:
        return messages
    return kept_msgs


__all__ = ["MISSING_RESULT_PLACEHOLDER", "cleanup_orphan_tool_calls",
           "fill_missing_tool_results", "repack_tool_results", "repair_tool_pairing"]
