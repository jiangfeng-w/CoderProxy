"""工具配对安全网（BUG-4「出站安全网」，spec §4.5.3 规划落地）。

对齐参考实现 `workbuddy2api-panel/internal/upstream/tool_pairing.go`（逐条移植）并
吸收 `chatcoder`/`9router` 的「补缺失结果」路线，共四层（`repair_tool_pairing` 一键串联）：

- `merge_adjacent_tool_calls(messages)`：把**相邻**的两条 assistant.tool_calls 声明
  合并成一条（上游 deepseek 系对「背靠背拆分」形态判 11148 顶死会话；参考实现
  `mergeAdjacentToolCalls` 2026-09-20 线上对照实测：拆两条 → 503/11148，合成一条
  → 200；`content: []` 也算空——部分客户端用它表达「无正文」）；
- `fill_missing_tool_results(messages)`：给 assistant.tool_calls 中**缺结果**的调用补一条
  显式占位 tool 消息（对齐 chatcoder `ensure_tool_pairing` 的「补显式丢失占位而非假数据」；
  9router `fixMissingToolResponses` 只补空串，占位文案对模型更友好——调用记录不丢、
  模型知道该结果不可依赖）；
- `repack_tool_results(messages)`：把夹在 assistant.tool_calls 与其 tool 结果之间的
  **非 tool 消息**后移，保证同一批 tool_call 的结果在 wire 上连续（Codex 的
  `<image_resize_notice>` developer 消息会插在两份结果中间）；并归正结果批——
  **错位收拢**（紧邻位置无本组结果时向后探测，找到则前移；对齐 magpie
  `pairToolMessages` / LiteLLM Case A「不要求紧邻」口径）、**声明序归位**
  （批内结果按 tool_calls 声明顺序重排，对齐 magpie/Reasonix）、**同 id 去重**
  （批内同一 tool_call_id 只留一条：**空内容让位有内容者**——对齐 OmniRoute
  `preferResult`「空占位携带零信息，丢弃无损失」，防空占位顶掉真实结果；双空
  或双非空保持先到者——同 magpie/qwen 先到口径）；
- `cleanup_orphan_tool_calls(messages)`：按 id 集合**双侧对称**裁剪孤儿——
  assistant.tool_calls 只留有结果配对的项（空了删 `tool_calls` 键），`role:tool`
  只留调用被保留的项（孤儿结果整条删）；顺手清理**空 `tool_calls: []`** 键
  （hermes `sanitize_api_messages` 口径，部分严格实现拒绝空数组）。

背景：OpenAI 兼容协议要求带 tool_calls 的 assistant 之后**紧跟**其每条结果，
中间夹任何消息、或任一侧缺配对，上游都会 400（WorkBuddy code=11148，
tool_call_sequence_broken）并顶死整条会话。网关是最后一道防线：能补齐的补齐
（显式占位），补不齐的（双侧都无主的）裁掉——宁可丢一轮工具上下文，也好过整条
会话报废。

调用序（`repair_tool_pairing`）：**先 merge 再 fill 再 repack 再 cleanup**——
- merge 最先行：两条相邻 assistant.tool_calls 先合成一批，fill/repack 才能看到完整
  「一批调用 + 一批结果」的形态（若 fill 先行，第一条 assistant 的缺失结果占位会插进
  两条 assistant 之间、相邻关系被破坏，合并永远不会发生——正是要挡的 11148 形态）；
- fill 次行：缺失的占位插在 assistant 之后，随后 repack 可把它与既有结果一并归位
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


def _content_is_empty(msg: object) -> bool:
    """结果消息的 content 是否为空占位（对齐 OmniRoute `preferResult` 的 contentIsEmpty）：
    缺键 / None / 空串 / 仅空白串 / 空数组 / 空对象 → True；其余（含数字等）→ False。"""
    if not isinstance(msg, dict):
        return True
    content = msg.get("content")
    if content is None:
        return True
    if isinstance(content, str):
        return content.strip() == ""
    if isinstance(content, (list, dict)):
        return len(content) == 0
    return False


def _empty_content_for_merge(v) -> bool:
    """content 是否为空（缺失 / None / 空串 / 空数组）——合并判定专用。

    空数组也必须算空：有客户端把「没有正文」发成 `content: []` 而不是 null，
    此前按「非字符串一律非空」处理 → 背靠背的两条 assistant(tool_calls) 不合并
    → 上游 deepseek 系判 11148（正是 merge 要挡的形态，条件漏了 []）。
    非空数组仍视为有内容：宁可漏合并，也不丢内容。字符串按参考实现口径**不做
    strip**（纯空白串视为有内容——条件从严，不引入新语义）。"""
    if v is None:
        return True
    if isinstance(v, str):
        return v == ""
    if isinstance(v, list):
        return len(v) == 0
    return False


def _merge_reasoning_content(dst: dict, src: dict) -> None:
    """把 src 的 reasoning_content 并入 dst（两边都有则换行拼接）。

    deepseek 多轮要求 assistant 带思维链回填（本仓库 `_backfill_reasoning_content`
    同源逻辑），合并时丢弃会换一个错误。"""
    rc = src.get("reasoning_content")
    if not isinstance(rc, str) or rc == "":
        return
    prev = dst.get("reasoning_content")
    if isinstance(prev, str) and prev != "":
        dst["reasoning_content"] = prev + "\n" + rc
        return
    dst["reasoning_content"] = rc


def merge_adjacent_tool_calls(messages: list) -> list:
    """相邻 assistant.tool_calls 合并成一条（对齐参考实现 `mergeAdjacentToolCalls`）。

    上游 deepseek 系对「背靠背拆分」形态（两条相邻的、各自带 tool_calls 的 assistant）
    判 400 code=11148（tool_call_sequence_broken），整条会话报废：客户端每次重试重放
    同一条历史，池侧换号也无效（不是账号问题）。网关作为最后一道防线按最严口径归一。

    合并条件从严（避免引入新语义），两种形态：
    1. 本条是带 tool_calls 且**无正文**的 assistant → 并入上一条同为
       assistant+tool_calls 的消息（背靠背的并行调用声明）；
    2. 反向：本条是纯正文 assistant（无 tool_calls 键），上一条是带 tool_calls
       且无正文的 assistant → 把正文折进上一条（部分 OpenAI 兼容客户端把正文与
       调用声明拆成两条独立 assistant 回放）。
    两条消息必须**相邻**（中间隔着任何消息都不合并——隔着消息说明不是同一批声明，
    凭猜测合并会改变语义）；形态 2 只认字符串正文（数组正文可能含多模态块，拼接
    会丢结构）。

    两形态都会搬迁/拼接 `reasoning_content`（不丢思维链回填）。无改动时返回原对象。
    """
    if len(messages) < 2:
        return messages
    out: list = []
    changed = False
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "assistant" and out:
            prev = out[-1]
            prev_is_call_head = (
                isinstance(prev, dict) and prev.get("role") == "assistant"
                and isinstance(prev.get("tool_calls"), list) and bool(prev["tool_calls"]))
            tcs = m.get("tool_calls")
            # 形态一：本条 = tool_calls 非空 + 无正文 → 并入上一条调用声明
            if (prev_is_call_head and isinstance(tcs, list) and tcs
                    and _empty_content_for_merge(m.get("content"))):
                prev["tool_calls"] = list(prev["tool_calls"]) + list(tcs)
                _merge_reasoning_content(prev, m)
                changed = True
                continue  # 本条已并入上一条，不再单独出站
            # 形态二：本条 = 纯正文字符串（无 tool_calls 键）→ 折进上一条无正文的调用声明
            if "tool_calls" not in m:
                txt = m.get("content")
                if (prev_is_call_head and isinstance(txt, str) and txt != ""
                        and _empty_content_for_merge(prev.get("content"))):
                    prev["content"] = txt
                    _merge_reasoning_content(prev, m)
                    changed = True
                    continue  # 正文已折进上一条
        out.append(m)
    if not changed:
        return messages
    return out


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
    """工具配对一键修复：merge（相邻合并）→ fill（补缺失显式占位）→ repack（归位）
    → cleanup（裁孤儿/空 name）。

    参考管线同序（2api-panel 出站：`mergeAdjacentToolCalls → repack → cleanup`；
    chatcoder：`repair_tool_call_ids → ensure_tool_pairing → normalize_tool_sequence`）。
    任何输入经此函数后保证：带 tool_calls 的 assistant 之后**连续**跟着本批全部结果，
    且无孤儿（无主 tool 消息 / 无结果调用 / 空 name 调用）。
    """
    return cleanup_orphan_tool_calls(
        repack_tool_results(fill_missing_tool_results(merge_adjacent_tool_calls(messages))))


def repack_tool_results(messages: list) -> list:
    """归正每批 assistant.tool_calls 的「结果段」：错位收拢 / 声明序 / 去重 / 夹心后移。

    ```text
    asst tool_calls=[c00 c01] | tool c00 | X | tool c01
        → asst tool_calls=[c00 c01] | tool c00 | tool c01 | X     （夹心后移）
    asst tool_calls=[c00 c01] | user X | tool c00 | tool c01
        → asst tool_calls=[c00 c01] | tool c00 | tool c01 | user X（错位收拢）
    asst tool_calls=[c00 c01] | tool c01 | tool c00
        → asst tool_calls=[c00 c01] | tool c00 | tool c01        （声明序归位）
    ```

    窗口探测（对齐 magpie `pairToolMessages` / LiteLLM Case A「不要求紧邻」口径）：
    组头后向后扫到**下一个组头 / 非 dict / 列表尾**为止；窗口内有本组结果 → 收拢到
    组头之后（夹心统一后移，彼此相对顺序保持）；没有 → 本组原地不动，交
    `cleanup_orphan_tool_calls`。**非本组 tool**（孤儿/他组结果）不挡探测——随夹心
    后移，孤儿由 cleanup 随后裁掉（否则孤儿夹在组头与结果之间会让乱序结果得不到
    归位）。结果**按 tool_calls 声明顺序**稳定归位（并行工具结果常乱序；声明序即
    上游位置式校验的对齐序）；同 id 重复去留：**空内容让位有内容者**（对齐
    OmniRoute `preferResult`——空占位零信息，防其顶掉真实结果；形态如
    `tool c1(空) | tool c1(真实)`），双空或双非空保持先到者（magpie/qwen 先到
    口径；LiteLLM Case D「留最后」为少数派）。

    只调顺序、不改内容；无改动时返回原对象（identity 零分配）。下一组
    assistant.tool_calls 是新的组头，绝不会被当作插入物吞掉（否则它自己那批结果
    永远得不到重排）。
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
        order = {cid: k for k, cid in enumerate(_tool_call_ids(m))}
        j = i + 1
        results: list = []
        others: list = []
        while j < n:
            mm = messages[j]
            if not isinstance(mm, dict) or _is_group_head(mm):
                break  # 下一组组头 / 非 dict：窗口边界
            if mm.get("role") == "tool":
                tid = mm.get("tool_call_id")
                if isinstance(tid, str) and tid in order:
                    results.append(mm)
                    j += 1
                    continue
                # 非本组 tool（孤儿/他组）：不挡探测，随夹心后移，cleanup 随后裁
            others.append(mm)  # 夹心（user/developer/孤儿 tool 等）：声明序归位后后移
            j += 1
        if not results:
            out.append(m)
            i += 1
            continue  # 窗口内无本组结果：本组原地不动
        deduped: dict = {}
        for r in results:
            # 同 id 重复：非空内容让位于有内容者（对齐 OmniRoute preferResult——
            # 「空占位携带零信息，丢弃无损失」）；双空/双非空保持先到者。
            prev = deduped.get(r["tool_call_id"])
            if prev is None or (_content_is_empty(prev) and not _content_is_empty(r)):
                deduped[r["tool_call_id"]] = r
        ordered = sorted(deduped.values(), key=lambda r: order[r["tool_call_id"]])
        candidate = ordered + others
        if (len(candidate) == j - i - 1
                and all(a is b for a, b in zip(candidate, messages[i + 1:j]))):
            out.append(m)
            out.extend(messages[i + 1:j])  # 已是合法形态：零改动路径
        else:
            changed = True
            out.append(m)
            out.extend(candidate)
        i = j
    if not changed:
        return messages
    return out


def _tool_call_name_empty(tc: object) -> bool:
    """assistant.tool_calls[] 单项的 function.name 是否为空（参考实现同口径）。

    仅当 `function` 对象存在、`name` 字段存在且为**空白字符串**时判真；
    function / name 字段缺失时判假——历史报文里存在不带 function 的占位条目，
    上游不会因此拒参，不扩大打击面。"""
    if not isinstance(tc, dict):
        return False
    fn = tc.get("function")
    if not isinstance(fn, dict):
        return False
    name = fn.get("name")
    if not isinstance(name, str):
        return False
    return name.strip() == ""


def cleanup_orphan_tool_calls(messages: list) -> list:
    """剔除无法配对的 tool_call 与 tool 结果（对齐参考 resolveToolPairing 语义）。

    - 收集全线 `role:tool` 的 tool_call_id（结果集）与 `assistant.tool_calls[].id`
      （调用集）；两侧共用 keepCalls（调用存在**且**结果存在）按 id 对称裁剪：
      assistant 只留有结果的调用（过滤后为空则删 `tool_calls` 键，不删消息本身；
      内容为空字符串同样的空壳消息是合法 wire 形态）；`role:tool` 只在对应调用
      被保留时保留，否则删除整条消息；
    - 追加否决条件：`function.name` 为空白串（含纯空白）的调用一律不保留——上游
      对这类条目直接判 11133 model_param_invalid（tool_calls[i].function.name must
      be a non-empty string），带不带配对结果都会拒；带结果的脏条目原本会命中
      keepCalls 原样出站（线上「整条会话每轮 503、只能清历史」的成因，参考实现
      535e19e 修复）。删除动作复用同一份对称裁剪；
    - 空 `tool_calls: []` 键顺手清理（hermes `sanitize_api_messages` 口径：部分
      严格实现拒绝空数组；此类消息本身保留）；
    - 无任何工具流量且无空 `tool_calls` → 原对象原样返回（零分配零改动）。

    部分保留不会留下无结果的 tool_call（两侧同口径），这是「让请求通过」的安全网。
    """
    if not messages:
        return messages
    call_ids: set[str] = set()
    result_ids: set[str] = set()
    name_bad_ids: set[str] = set()
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
            tcs = m.get("tool_calls")
            if isinstance(tcs, list) and not tcs:
                has_traffic = True  # 空 tool_calls 也是待清理对象
            if isinstance(tcs, list):
                for tc in tcs:
                    if not isinstance(tc, dict):
                        continue
                    tid = tc.get("id")
                    if isinstance(tid, str) and tid:
                        call_ids.add(tid)
                        if _tool_call_name_empty(tc):
                            name_bad_ids.add(tid)
                        has_traffic = True
    if not has_traffic:
        return messages

    # keepCalls：调用 id 双侧齐全（调用存在且结果存在），且 function.name 非空白串。
    keep_calls = {tid for tid in call_ids
                  if tid in result_ids and tid not in name_bad_ids}

    # 1) assistant.tool_calls：按 keep_calls 对称裁剪——只留有结果的调用，
    #    过滤后为空则删键（就地改写，同参考实现；无改动消息保持原样）；
    #    空数组键一并清理。
    changed = False
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        tcs = m.get("tool_calls")
        if not isinstance(tcs, list):
            continue
        if not tcs:
            m.pop("tool_calls", None)  # 空数组：部分严格实现拒绝，直接清键
            changed = True
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
           "fill_missing_tool_results", "merge_adjacent_tool_calls",
           "repack_tool_results", "repair_tool_pairing"]
