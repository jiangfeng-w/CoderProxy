"""WorkBuddy 聊天出站 adapter（「WorkBuddy聊天反代与多平台聚合」§3.2 / §4.2 落地）。

职责（对齐 9router `codebuddy-cn.js` 的「默认实现 + 薄钩子」模式）：

- **传输**：POST `{wb_api_base}/v2/chat/completions`（强 stream，B2 实测 400/11101）；
  桌面档头族（B1 实测 2026-10-08：UA 三段式 + 归属四头，与登录档位身份一致）；
- **请求改写钩子** `transform_request`：强制 `stream:true`；system 中性化（B3：agent
  身份句/超长 system 命中 11128 渠道风控 → 替换中性句，不重试）；思考形状
  （透传档位 §2.2：**形状归一**——deepseek 系开意图 `thinking:{type:"enabled"}` +
  agent 原档（不降级不改写、缺档不兜底）、关意图 `thinking:{type:"disabled"}` 真关、
  零表态零注入；非 deepseek 维持 B4 口径；强制/透传语义由 relay 层决定）；
  多轮一致性（deepseek/kimi 回填 `reasoning_content`；出站工具配对安全网 BUG-4：
  fill→repack→cleanup，对齐 2api-panel/9router/chatcoder）；
- **错误识别钩子** `parse_error`：错误码族映射（B6：11101→400 / 11128→400 /
  11102→404 / 6004→429；未知→透传上游状态）；
- **响应翻译**：上游 SSE 帧白名单重建（剥离 `function_call`/`refusal`/`extra_fields`
  等腾讯内部字段——硬性规则 2）；usage 映射（B8）。

账号池选号（§4.3）：轮询 `status == "normal"` 账号；401/403 换号重试（每号最多一次，
全部试完抛最后错误）；无可用账号 → NoAvailableAccountError（routes 映射 503）。

非流式：上游仅支持流式（B2），本 adapter 内部收流聚合为 ChatResponse。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from datetime import datetime

import httpx

from app.models.schemas import ChatMessage, ChatRequest, ChatResponse, Usage

from relay.config import settings
from relay.platforms import store
from relay.platforms.base import PlatformUpstreamError
from relay.platforms.workbuddy import oauth
from relay.platforms.workbuddy.adapter import WorkBuddyAdapter
from relay.platforms.workbuddy.client import derive_stable_id, user_agent_for
from relay.platforms.workbuddy.realms import REALM_CN, WbRealm
from relay.tool_repair import repair_tool_pairing

logger = logging.getLogger(__name__)


# ── 连接阶段瞬时错误重试 ─────────────────────────────────────────────
# 2026-10-11 实测：DNS 抖动数分钟 → getaddrinfo failed 直透 agent（当日 3 连败、
# 5 分钟后自愈）。仅重试「连接建立/请求发出前」的失败（ConnectError/ConnectTimeout，
# 此时上游未收到任何字节、请求未生效，重试绝对幂等安全）；一旦进入流绝不重试
# （防重复产出/重复计费）；HTTP 错误响应语义不变。1 次重试 + 1s 退避——分钟级
# 断网仍快速报错，由 agent 决定重试时机（秒级抖动则完全无感）。
_CONNECT_RETRY_ATTEMPTS = 2
_CONNECT_RETRY_BACKOFF_S = 1.0


async def _enter_stream_with_connect_retry(stack: AsyncExitStack, client: httpx.AsyncClient,
                                           method: str, url: str,
                                           **kwargs) -> httpx.Response:
    """经 stack 进入 ``client.stream()``，连接阶段失败时退避重试（仅进流前）。

    返回已进入的 Response；调用方经同一 stack 统一退出。重试只包裹「进入」动作：
    进入成功后的任何异常都不经过这里，结构性保证不在流中重试。
    """
    for attempt in range(1, _CONNECT_RETRY_ATTEMPTS + 1):
        try:
            return await stack.enter_async_context(client.stream(method, url, **kwargs))
        except (httpx.ConnectError, httpx.ConnectTimeout):
            if attempt >= _CONNECT_RETRY_ATTEMPTS:
                raise
            await asyncio.sleep(_CONNECT_RETRY_BACKOFF_S)
    raise AssertionError("unreachable")  # pragma: no cover

PLATFORM_ID = REALM_CN.platform_id

CHAT_PATH = "/v2/chat/completions"

# 国际版 chat 首条 system 兜底句（panel ensureConsoleSystem 同款文本）
CONSOLE_SYSTEM_PROMPT = "You are a helpful assistant."

# ── B3：system 中性化（9router codebuddy-cn.js 同款口径） ──
NEUTRAL_SYSTEM_PROMPT = ("You are a helpful AI assistant that helps with "
                         "software engineering tasks.")
_SYSTEM_MAX_LEN = 2000
_AGENT_PATTERN = re.compile(
    r"you are claude code|claude.?code.+official.+cli|anthropic.+official.+cli"
    r"|you are (?:cursor|windsurf|cline|aider|continue|copilot|cody)"
    r"|you are an? (?:ai )?(?:coding |code )?agent"
    r"|cc_entrypoint\s*=\s*(?:cli|vscode|jetbrains|gui)"
    r"|claude.?code.+issues|give feedback.+claude.?code"
    r"|you are .{0,30}(?:powerful )?ai agent|orchestration capabilities"
    r"|OhMyOpenCode|<agent-identity>|<Role>|<Behavior_Instructions>",
    re.IGNORECASE)

# ── B6：错误码族（实测 2026-10-08）──
_CODE_11101_BAD_PARAMS = 11101  # 参数错 / 非流式
_CODE_11102_MODEL_MISSING = 11102  # 模型不可用
_CODE_11128_CHANNEL_BLOCKED = 11128  # 渠道风控（agent 身份句等）
_CODE_6004_RATE_LIMIT = 6004  # 限流（msg 常附恢复时间文案）

# ── 健康管理分流（2026-10-17，spec §2/§7）──
_CODE_11140_REQUEST_ILLEGAL = 11140  # 双语义：授权类（request illegal）/ 限流文案
_CODE_12153_SESSION_DEAD = 12153  # 会话失效（Offline user session not found）
# 11140 不可按 code 一刀切——同 code 承载授权与限流双语义，按 msg 关键词分流（panel 警示）
_RATE_LIMIT_MSG_RE = re.compile(r"rate.?limit", re.IGNORECASE)
# 内容/授权类零惩罚标记（§7-C：msg 关键词中英双形态，不按 code 死判）
_CONTENT_BLOCK_MARKERS = ("blocked by security policy", "unapproved channel",
                          "illegal api invocation")
_SESSION_DEAD_MARKERS = ("offline user session not found",)

# 6004 恢复时间解析（对齐 panel ParseRateReset + 9router 正则；两形态同一时区 UTC+8）
_RESET_CN_RE = re.compile(r"将在\s*(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})\s*重置")
_RESET_EN_RE = re.compile(r"will reset at\s*(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")

# ── BUG-3：deepseek 系思考形状（对齐 workbuddy2api-panel/internal/upstream/thinking.go）──
# 官方客户端对 deepseek 系「开思考」= `thinking.type=enabled` **且**带某档
# `reasoning_effort`（isThinkingEnabled = !!(reasoning_summary || reasoning_effort ||
# reasoning?.effort)）；裸 `reasoning_effort:"none"` 上游不认（BUG-3 探针实证仍出思考），
# 关意图必须翻译成 `thinking:{type:"disabled"}`。本 adapter 只做**形状归一翻译**
# （不选档、不降级、不兜底），强制/透传语义由 relay 层（routes）决定。


def _is_deepseek_model(model: str) -> bool:
    """模型名以 deepseek 为前缀（大小写无关；对齐官方 thinkingFormat:"deepseek" 判定口径）。"""
    return str(model or "").strip().lower().startswith("deepseek")


def _is_kimi_model(model: str) -> bool:
    """kimi 系（对齐 9router 模型级规则 `/^kimi-/i`；WB 目录如 kimi-k3/kimi-k3-1）。"""
    return str(model or "").strip().lower().startswith("kimi-")


# GPT 系 max_tokens 下限（2026-10-16 对齐参考实现 payload.go::clampGPTMinMaxTokens）：
# 上游 GPT 系对 max_tokens < 16 一律 400 code=11133（model_param_invalid；实测 15 拒、
# 16 过），而 Claude Code `/model` 切换会发极小 max_tokens 探针——全号轮转同样被拒、
# 客户端切不过去（换号无效，不是账号问题）。非 GPT 系模型不抬（上游接受小值）。
_GPT_MIN_MAX_TOKENS = 16


def _clamp_gpt_max_tokens(model: str, value):
    """GPT 系模型 max_tokens 抬到下限 16；其余模型原样返回。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return value
    if "gpt-" not in str(model or "").lower():
        return value
    return max(value, _GPT_MIN_MAX_TOKENS)


def _has_reasoning_trace(messages: list) -> bool:
    """会话是否已有 reasoning 痕迹（对齐 2api-panel hasTrace：非空 reasoning string 或 rc 键存在）。"""
    for m in messages:
        if not isinstance(m, dict):
            continue
        r = m.get("reasoning")
        if isinstance(r, str) and r:
            return True
        if "reasoning_content" in m:
            return True
    return False


def _backfill_reasoning_content(messages: list[dict], model: str,
                                thinking_enabled: bool) -> None:
    """多轮一致性：历史 assistant 回填 ``reasoning_content``（就地改写，零返回值）。

    根因（上游实测口径）：thinking 模式下多轮会话里 assistant 消息须带
    ``reasoning_content``（string），缺失时上游 400「The reasoning_content in the
    thinking mode must be passed back to the API.」——第三方 agent 丢推理回传即命中。

    参考口径（三家交叉验证取并集）：

    - 2api-panel `backfillReasoningContent`（主档，同款 WorkBuddy 上游实证）：
      deepseek 系**全量** assistant 回填，门控 = ``thinkingEnabled || hasTrace``
      （注入 enabled 后零痕迹也补，issue #165）；值分支：rc 已是 string 保留 →
      复制非空 ``reasoning`` → 否则补 ``""``；并镜像 ``reasoning``（非空保留 →
      rc 非空写 rc → 皆无补空格 ``" "``——部分租户校验 ``len(reasoning)>0``，
      空白串过闸、空串不过）。
    - 9router `reasoningContentInjector`（模型级补充）：``kimi-`` 系**仅工具回合**
      （带 tool_calls 的 assistant）补 ``" "`` 占位——kimi 只在工具调用链路校验。
    - chatcoder（交叉印证）：仅工具回合输出 rc——同「工具回合是关键点」结论。

    调用时机：``transform_request`` 中 ``_apply_thinking`` 与工具配对修复**之后**、
    最终 messages 上执行（参考管线同序）；``thinking_enabled`` 取注入后
    ``thinking.type == "enabled"``。

    ⚠ 实测警示（2026-10-10 静默丢弃实验，见 spec §3-G / 探针-wb-reasoning回放.py）：
    本上游对历史 ``reasoning_content`` **白名单反序列化、静默丢弃**（同长标记放 rc 里
    prompt_tokens +0、放 content 里 +481；暗号复述实验模型答「无」）。即回填的**值**
    不会被模型读到——本函数的唯一价值是**过「必须回传」校验**（缺失即 400）。
    因此：① 占位与真值同效，**勿投入「真值缓存回填」类升级**（零质量收益）；
    ② 若未来要让模型真正记得历史思考，唯一机制是正文注入（`<thought>…</thought>`
    进 content），属独立议题。

    ⚠ 口径校准（2026-10-11 11155 探针，见 spec §5 / 探针-wb-11155-trailing-assistant.py）：
    「缺失即 400」在 deepseek-v4.1-flash + thinking enabled 上**未复现**——历史 assistant
    不带 rc 键直连上游 200；「tools + trailing assistant 即 11155」的 OmniRoute 口径
    （对 www.workbuddy.ai 实证）同样不适用我方上游（同形状 200）；我方日志亦从未出现
    该 400/11155/11128/11101。本函数**维持不动、定位降级为纯保险**（2api-panel 口径 +
    零成本：rc 本就被上游静默丢弃、不计费；防其它模型/租户/上游收紧校验），勿据此
    删除，也勿据「400 校验」表述再投入升级。
    """
    if not messages:
        return
    if _is_deepseek_model(model):
        if not thinking_enabled and not _has_reasoning_trace(messages):
            return  # disabled + 零痕迹 → 零改动（官方门控两半边都不亮）
        for m in messages:
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            rc = m.get("reasoning_content")
            if isinstance(rc, str):
                pass  # 已有 string（含空串）→ 保留，不覆盖
            else:
                # 非 string（null/数字）按「无」处理（官方 "string" != typeof 语义）
                r = m.get("reasoning")
                rc = r if isinstance(r, str) else ""
                m["reasoning_content"] = rc
            r = m.get("reasoning")
            if isinstance(r, str) and r:
                continue  # reasoning 已非空 → 不覆盖
            m["reasoning"] = rc if rc else " "  # 镜像（rc 优先；皆无补空白占位）
        return
    if _is_kimi_model(model):
        for m in messages:
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            tcs = m.get("tool_calls")
            if not (isinstance(tcs, list) and tcs):
                continue  # 仅工具回合（9router scope="toolCalls"）
            rc = m.get("reasoning_content")
            if isinstance(rc, str) and rc:
                continue  # 已有非空 rc → 保留
            m["reasoning_content"] = " "  # 占位（9router PLACEHOLDER）


class NoAvailableAccountError(RuntimeError):
    """账号池无可用账号（无 normal 账号 / 全部停用 / 全部凭证或会话被拒）。routes 映射 503。"""


class AllAccountsSuspendedError(RuntimeError):
    """池中有启用账号但全部处于降权/冷却（账号级成因）→ routes 映射 429 + Retry-After。

    retry_after_s：全池最近到期时刻（秒，至少 1）——agent 按它决定重试时机。
    """

    def __init__(self, message: str, *, retry_after_s: int = 60) -> None:
        super().__init__(message)
        self.retry_after_s = int(retry_after_s)


class ModelUnavailableError(RuntimeError):
    """模型级全锁：全池该模型命中 6004 冷却 / 11102 负缓存，或上游直接返回 11102。

    routes 映射 400 + code=model_unavailable（spec §8-A：客户端把 503 当瞬时故障
    自动重试、把 404 当「模型不存在」下架；400 + 解锁时间才是「现在别用、几点后再试」
    的正确语义）。accounts：池中启用账号数；unlock_at_ms：最早解封时刻（ms）。
    """

    def __init__(self, message: str, *, accounts: int = 0, unlock_at_ms: int = 0) -> None:
        super().__init__(message)
        self.accounts = int(accounts)
        self.unlock_at_ms = int(unlock_at_ms)


class _CredentialRejected(RuntimeError):
    """本轮账号凭证被上游拒绝（401/403）：换号重试信号（不喂连败）。"""


class _SessionRejected(RuntimeError):
    """会话/授权失效（11140 request illegal / 12153）：换号重试、不喂连败。"""


class _UpstreamTimeout(RuntimeError):
    """读超时（上游停滞）：不换号、不喂连败（§8-C 超时止损）。"""


class _UpstreamCodeError(PlatformUpstreamError):
    """带上游 code/msg 的结构化错误（分流判定用；对外仍是 PlatformUpstreamError）。

    http_status：上游原始 HTTP 状态（分流时区分「5xx 喂连败」与「4xx 业务错误不喂」）。
    """

    def __init__(self, message: str, *, status: int, http_status: int = 0,
                 code: int | None = None, raw_msg: str = "") -> None:
        super().__init__(message, status=status)
        self.http_status = int(http_status or 0)
        self.code = code
        self.raw_msg = raw_msg


def _extract_code_msg(body_text: str) -> tuple[int | None, str]:
    """上游错误体 → (code, msg)；非 JSON / 非数字 code → (None, ...)。"""
    code: int | None = None
    msg = ""
    try:
        data = json.loads(body_text)
        if isinstance(data, dict):
            raw_code = data.get("code")
            if isinstance(raw_code, int) or (isinstance(raw_code, str)
                                             and raw_code.isdigit()):
                code = int(raw_code)
            msg = str(data.get("msg") or data.get("message") or "")
    except ValueError:
        pass
    return code, msg


def _classify_upstream_error(exc: Exception) -> str:
    """喂入口分流（健康管理 spec §2 表 + §7/§8 增补）。

    返回：model_cooldown / model_missing / session / business / generic。
    - model_cooldown：6004 或 11140 限流文案 → 模型级冷却（不喂连败、不换号）；
    - model_missing：11102 → 负缓存（不喂连败、不换号）；
    - session：11140 request illegal / 12153 → 换号、不喂；全池 → 503 引导重登；
    - business：4xx 业务/内容类错误（11148/11128/11101 等，含内容拦截标记）→ 不喂不换；
    - generic：5xx / 网络（不知道原因的失败）→ 喂连败 + 换号（未产出帧时）。
    """
    code = getattr(exc, "code", None)
    msg = str(getattr(exc, "raw_msg", "") or "")
    low = msg.lower()
    status = int(getattr(exc, "status", 0) or 0)
    if code == _CODE_6004_RATE_LIMIT or "频率限制" in msg or "限额" in msg:
        return "model_cooldown"
    if code == _CODE_11102_MODEL_MISSING:
        return "model_missing"
    if code == _CODE_11140_REQUEST_ILLEGAL or "request illegal" in low:
        return "model_cooldown" if _RATE_LIMIT_MSG_RE.search(msg) else "session"
    if code == _CODE_12153_SESSION_DEAD or any(m in low for m in _SESSION_DEAD_MARKERS):
        return "session"
    if any(m in low for m in _CONTENT_BLOCK_MARKERS):
        return "business"
    if status >= 500:
        return "generic"
    return "business"


def _parse_reset_at_ms(msg: str) -> int | None:
    """6004 恢复时间解析（CN/EN 两形态，均 UTC+8 墙钟 → ms）；未知形态 None。

    「绝不臆造时间」：解析失败返回 None，由 store.note_rate_limit 走有界退避。
    """
    for pat in (_RESET_CN_RE, _RESET_EN_RE):
        m = pat.search(str(msg or ""))
        if not m:
            continue
        try:
            dt = datetime.strptime(m.group(1).replace("T", " "), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        return int(dt.replace(tzinfo=store.CN_TZ).timestamp() * 1000)
    return None


def _fmt_ms(ms: int) -> str:
    """ms 时间戳 → UTC+8 可读时间（错误文案用）。"""
    if not ms:
        return "未知"
    return datetime.fromtimestamp(ms / 1000, store.CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _priority(account: dict) -> int:
    """账号优先级（1 起、越小越优先；缺省/非法 → 1）。"""
    try:
        value = int(account.get("priority") or 1)
    except (TypeError, ValueError):
        return 1
    return max(value, 1)


def _earliest_recovery_s(accounts: list[dict], model: str, now: int) -> int:
    """全池挂起时取「最近到期」时刻（降权 ∪ 模型冷却），返回秒（至少 1）。"""
    earliest: int | None = None
    for a in accounts:
        du = int(a.get("degraded_until") or 0)
        if du > now and (earliest is None or du < earliest):
            earliest = du
        cd = store.model_cooldown_until(a, model, now=now)
        if cd and (earliest is None or cd < earliest):
            earliest = cd
    if earliest is None:
        return 60
    return max(1, (earliest - now + 999) // 1000)


def _earliest_model_unlock_ms(accounts: list[dict], model: str, now: int) -> int:
    """全池模型锁：该模型冷却的最早解封时刻（ms；无命中 → 0）。"""
    earliest = 0
    for a in accounts:
        cd = store.model_cooldown_until(a, model, now=now)
        if cd and (not earliest or cd < earliest):
            earliest = cd
    return earliest


async def _log_pick(platform_id: str, model: str, account: dict) -> None:
    """选号日志（kind=`wb_pick`，GUI 日志页可筛；写库失败不影响主链路）。

    验收反馈（2026-10-17）：优先级/轮询效果需要「哪个请求走了哪个号」的可见性
    ——每次选号（含换号重试）落一条，detail 带 uid/昵称/优先级。
    """
    try:
        from relay import db  # 延迟导入：旁路依赖不进模块级
        await db.log_event(
            kind="wb_pick", model=model,
            detail={"uid": str(account.get("uid") or ""),
                    "nickname": str(account.get("nickname") or ""),
                    "priority": _priority(account)})
    except Exception as exc:  # noqa: BLE001（旁路：日志失败不阻断聊天）
        logger.warning("[wb-chat] 选号日志写入失败: %s", exc)


class WorkBuddyChatAdapter:
    """WorkBuddy 聊天出站 adapter（出站事件契约见 relay/adapter_registry.py）。

    bare_model：上游真实模型名（无前缀）；model_entry：目录条目（可空）。
    transport：测试注入点（httpx.MockTransport）。
    profile：域 profile（默认 CN——现网形态零回归）；选号/头族/base 全按它走。
    """

    def __init__(self, bare_model: str, *, ctx=None, model_entry: dict | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 profile: WbRealm = REALM_CN) -> None:
        self._model = str(bare_model or "")
        self._ctx = ctx          # 工具伪装上下文（B5：WorkBuddy 原生支持 tools，不做伪装）
        self._entry = model_entry or {}  # 目录条目（工厂契约保留；形状归一后暂无消费点）
        self._transport = transport
        self._profile = profile
        self._active_uid = ""
        # 账号层实例随 profile（原模块级单例写死 CN——国际版会被拿去 CN 池刷新，串池）
        self._account_adapter = WorkBuddyAdapter(profile)

    # ─────────────────────── 账号池选号（§4.3） ───────────────────────

    async def _normal_accounts(self) -> list[dict]:
        """启用号列表（系统态 normal ∩ 用户未停用；启用停用 spec 统一读口径）。"""
        accounts = [a for a in await store.load_accounts(self._profile.platform_id)
                    if store.is_selectable(a)]
        # uid 排序 + 全局游标 → 跨请求轮转（同号不连续命中）
        return sorted(accounts, key=lambda a: str(a.get("uid") or ""))

    async def _pick_account(self, tried: set[str],
                            rejected: set[str] | None = None) -> dict:
        """选号（三需求同批一次写全）：启用 ∩ 非 tried → 健康过滤 → priority 分组 → 组内游标。

        - 无启用号 / 全 tried / 全停用 → NoAvailableAccountError（503）；
        - 有候选但全部降权/冷却：本轮已有凭证/会话被拒（rejected 非空）→ 503 引导重登
          （spec §4：429 仅在「非凭证被拒场景」）；账号级降权 → 429（最近到期秒数）；
          全为模型冷却 → 400 model_unavailable（最早解封时刻）；
        - priority：取最小优先级组（组内游标轮询）；高优组全冷却/降权/停用时
          自然落到下一组（可用性优先于优先级）。
        """
        now = store.now_ms()
        candidates = [a for a in await self._normal_accounts()
                      if str(a.get("uid") or "") not in tried]
        if not candidates:
            raise NoAvailableAccountError(
                f"无可用 {self._profile.display_name} 账号（请到供应商页添加/启用或重登）")
        healthy = [a for a in candidates
                   if store.is_healthy_for_model(a, self._model, now=now)]
        if not healthy:
            self._raise_all_suspended(candidates, rejected or set(), now=now)
        min_pri = min(_priority(a) for a in healthy)
        tier = [a for a in healthy if _priority(a) == min_pri]
        idx = _next_cursor() % len(tier)
        return tier[idx]

    def _raise_all_suspended(self, candidates: list[dict], rejected: set[str],
                             *, now: int) -> None:
        """全候选不可用（降权/冷却）→ 按成因抛错（本函数不返回）。"""
        display = self._profile.display_name
        if rejected:
            # 凭证/会话被拒场景优先（spec §4）：用户动作 = 重登 → 503 语义
            raise NoAvailableAccountError(
                f"全部 {display} 账号凭证/会话失效，请到供应商页重新登录")
        degraded = [a for a in candidates
                    if int(a.get("degraded_until") or 0) > now]
        if degraded:
            retry_after = _earliest_recovery_s(candidates, self._model, now)
            raise AllAccountsSuspendedError(
                f"全部 {display} 账号暂时挂起（连败/限流冷却），"
                f"约 {retry_after}s 后自动恢复",
                retry_after_s=retry_after)
        unlock = _earliest_model_unlock_ms(candidates, self._model, now)
        raise ModelUnavailableError(
            f"模型 {self._model} 全池冷却中（{len(candidates)} 个账号均限流/负缓存），"
            f"最早 {_fmt_ms(unlock)} 解封",
            accounts=len(candidates), unlock_at_ms=unlock)

    async def refresh_credentials(self) -> bool:
        """401 刷新钩子（routes `_refresh_provider_credentials` 调用）：

        有 active 号 → 刷新该号；否则刷新全部 normal 号。返回是否至少成功一次。
        """
        if self._active_uid:
            accounts = [await store.find_account(self._profile.platform_id, self._active_uid)]
        else:
            accounts = await self._normal_accounts()
        ok = False
        for account in accounts:
            if not account:
                continue
            try:
                await oauth.refresh_token(account, profile=self._profile)
                ok = True
            except Exception as exc:  # noqa: BLE001（单号失败不阻断其它号）
                logger.warning("[wb-chat] 刷新账号 %s 失败: %s",
                               str(account.get("uid") or "")[:8], exc)
        return ok

    # ─────────────────────── 请求改写钩子 ───────────────────────

    def transform_request(self, request: ChatRequest) -> dict:
        """ChatRequest → 上游 chat/completions body。"""
        body: dict = {
            "model": self._model,
            "stream": True,  # B2：上游拒绝非流式（400/11101），relay 层做非流式聚合
            "messages": [_message_payload(m) for m in request.messages],
        }
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.max_tokens:
            body["max_tokens"] = _clamp_gpt_max_tokens(self._model, request.max_tokens)
        if request.tools:
            body["tools"] = request.tools  # B5：原生透传（不做工具名伪装）
        # B4/BUG-3：思考形状（deepseek 系对齐官方客户端；其余模型维持 B4 透传口径）。
        self._apply_thinking(body, request)
        # BUG-4 出站安全网（一键四层：merge 相邻合并 → fill 补缺失占位 → repack 归位
        # → cleanup 裁孤儿/空 name/清空数组，参考管线同序）：修根后转换器正常产出不会
        # 断裂，这里兜住 agent 端历史里的畸形配对（相邻双 tool_calls 声明 / 孤立结果 /
        # 无结果调用 / 无 name 调用 / 结果间夹 developer / 缺结果 / 错位 / 乱序 / 重复
        # 结果 / 空 tool_calls），否则上游判 11148 或 11133 顶死会话。缺结果补显式
        # 占位而非假数据（对齐 chatcoder）。
        body["messages"] = repair_tool_pairing(body["messages"])
        # 多轮一致性：deepseek/kimi 回填 reasoning_content（2api-panel 主档 + 9router
        # kimi 工具回合口径；见 `_backfill_reasoning_content`）。在配对修复之后执行——
        # 此时 assistant 的 tool_calls 终态已定（kimi 分支按它判定「工具回合」）。
        thinking = body.get("thinking")
        thinking_enabled = (isinstance(thinking, dict)
                            and str(thinking.get("type") or "").lower() == "enabled")
        _backfill_reasoning_content(body["messages"], self._model, thinking_enabled)
        _neutralize_system_messages(body["messages"])
        if self._profile.ensure_system_first:
            _ensure_system_first(body["messages"])
        return body

    def _apply_thinking(self, body: dict, request: ChatRequest) -> None:
        """思考字段出站形状（透传档位 §2.2：**形状归一翻译**，不选档不改档）。

        透传/强制语义由 relay 层决定（强制档在 routes 统一覆写 ChatRequest），本函数
        只把 ChatRequest 的思考信号翻译成官方客户端形状——不补档、不降级、不兜底：

        - 关意图（effort ∈ none/off/disabled，或 thinking=False）→ deepseek 系
          `thinking:{type:"disabled"}` 真关（裸 effort=none 上游不认，BUG-3 探针实证）；
          非 deepseek 系 none 直发 / off 删字段（B4 实证有效）；
        - 开意图（thinking=True 或带非关档位）→ deepseek 系 enabled + agent 原档
          （不降级不改写，缺档不兜底——§5-6 探针观察上游行为）+ summary:auto；
          非 deepseek 档位直发 + summary（B4）；
        - 零表态（无 thinking 无档位）→ 零注入零兜底，落上游默认思考行为。
        """
        raw_effort = request.reasoning_effort
        effort = raw_effort if raw_effort not in ("off", "disabled") else None
        if not _is_deepseek_model(self._model):
            if effort:
                body["reasoning_effort"] = effort
                if effort != "none":
                    body["reasoning_summary"] = "auto"  # B4：上游暴露思考的配对参数
            return

        # 关意图判定用**原始**档位：off/disabled 曾是「上游不识别→删字段」的 B4 口径，
        # 真关语义下与 none 同判（agent 明确表达不思考）。
        thinking_off = (raw_effort in ("none", "off", "disabled")
                        or request.thinking is False)
        if thinking_off:
            # 真关（用户拍板 2026-10-09：强制传关闭参数，探针直连实证 reasoning_tokens=0）
            body["thinking"] = {"type": "disabled"}
            return
        if request.thinking or effort:
            body["thinking"] = {"type": "enabled"}
            if effort:
                body["reasoning_effort"] = effort
            body["reasoning_summary"] = "auto"  # 上游暴露思考的配对参数
        # 零表态：什么都不发（默认档兜底已随「每模型默认思考强度」语义一并退役）

    # ─────────────────────── 错误识别钩子 ───────────────────────

    def parse_error(self, status: int, body_text: str) -> PlatformUpstreamError:
        """上游错误 → PlatformUpstreamError（status 由 routes 透传为 HTTP 状态码）。

        返回 `_UpstreamCodeError`（携带 code/raw_msg/原始 HTTP 状态）供轮换链
        分流（健康管理 spec §2 喂入口表）；对外行为不变（仍是 PlatformUpstreamError）。
        """
        code, msg = _extract_code_msg(body_text)
        if code == _CODE_6004_RATE_LIMIT or "频率限制" in msg or "限额" in msg:
            mapped = 429
        elif code == _CODE_11102_MODEL_MISSING:
            mapped = 404
        elif code == _CODE_11128_CHANNEL_BLOCKED:
            mapped = 400
        elif code == _CODE_11101_BAD_PARAMS and status < 400:
            mapped = 400
        else:
            mapped = status if 400 <= status <= 599 else 502
        text = msg or body_text.strip()[:300] or f"HTTP {status}"
        if code is not None:
            text = f"{text}（上游 code={code}）"
        return _UpstreamCodeError(text, status=mapped, http_status=status,
                                  code=code, raw_msg=msg)

    # ─────────────────────── 传输 ───────────────────────

    def _headers(self, account: dict) -> dict[str, str]:
        """B1 实测（2026-10-08）桌面档头族（2api-panel 口径，与登录档位身份一致）。

        双域差异按 profile（§2 表）：UA 平台段 / Origin / Accept-Language；
        域与企业头分派——国际版恒 `X-Domain: www.workbuddy.ai` + `X-No-Enterprise-Id`
        （panel injectGlobalChatHeaders）；CN 走账号域现状（零回归）。

        会话头族（BUG-014，2026-10-17）：上游 10-15/16 起对国际版 chat 网关加渠道校验，
        请求缺 `X-Conversation-*` 三件套即 400 code=11128（msg `conversation id missing`，
        displayTips「这次调用没通过渠道校验，可能不是官方客户端发出的」）。本函数每次请求
        调用一次（`_stream_once` L460），故在此现生成即「每请求新 id」——上游不拿会话 id
        存历史（9router 抓包：官方客户端亦每请求新 id）。两域同发：CN 未强制但实测容忍
        （panel 生产两域同发）；两个同值别名（`X-Root-Request-ID`/`X-Request-ID`）对齐
        官方客户端抓包形态（9router PR #4949）。依据与探针见 `docs/已知问题.md` BUG-014。
        """
        uid = str(account.get("uid") or "")
        headers = {
            "User-Agent": user_agent_for(self._profile),
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": self._profile.origin,
            "Referer": f"{self._profile.origin}/",
            "X-CodeBuddy-Request": "1",
            "Accept-Language": self._profile.accept_language,
            "X-Agent-Purpose": "conversation",
            "X-IDE-Name": "WorkBuddy",
            "X-IDE-Type": "WorkBuddy",
            "X-IDE-Version": settings.wb_client_version,
            "X-Product": "WorkBuddy",
            "Authorization": f"Bearer {account.get('access_token') or ''}",
        }
        if uid:
            headers["X-User-Id"] = uid
            # 每号稳定派生设备头（与账号层同式；防多号设备指纹关联，B8）
            headers["X-Machine-ID"] = derive_stable_id(uid, "machine")
            headers["X-Session-ID"] = derive_stable_id(uid, "session")
        # 会话头族：必须在 profile 早退分支之前（本函数有两条 return 路径）
        headers["X-Conversation-ID"] = secrets.token_hex(16)
        headers["X-Conversation-Request-ID"] = secrets.token_hex(16)
        headers["X-Conversation-Message-ID"] = secrets.token_hex(16)
        headers["X-Root-Request-ID"] = headers["X-Conversation-Request-ID"]
        headers["X-Request-ID"] = headers["X-Conversation-Message-ID"]
        if self._profile.chat_domain:
            headers["X-Domain"] = self._profile.chat_domain
            headers["X-No-Enterprise-Id"] = "1"
            return headers
        domain = str(account.get("domain") or "")
        if domain:
            headers["X-Domain"] = domain
        else:
            headers["X-No-Department-Info"] = "1"
        if account.get("enterprise_id"):
            headers["X-Enterprise-Id"] = str(account["enterprise_id"])
        else:
            headers["X-No-Enterprise-Id"] = "1"
        return headers

    def _new_client(self) -> httpx.AsyncClient:
        # trust_env=False：系统代理劫持 copilot.tencent.com 会 403（自举环境纪律）
        return httpx.AsyncClient(trust_env=False,
                                 timeout=httpx.Timeout(300.0, connect=15.0),
                                 transport=self._transport)

    async def _stream_once(self, account: dict, body: dict) -> AsyncIterator[dict]:
        """单号单次调用：HTTP 错误分类抛错（401/403 → 换号信号）。

        网络层错误（断连/超时/DNS）包成 PlatformUpstreamError(502)——与自定义供应商
        链路同口径，不把 httpx 异常裸露成 500（步骤 8 小号 e2e 断网实测暴露）。
        连接建立/请求发出前的瞬时失败（DNS 抖动等）先经一次退避重试（见
        `_enter_stream_with_connect_retry`），仍失败才按上述口径 502。
        """
        url = f"{self._profile.chat_base}{CHAT_PATH}"
        try:
            async with AsyncExitStack() as stack:
                client = await stack.enter_async_context(self._new_client())
                resp = await _enter_stream_with_connect_retry(
                    stack, client, "POST", url, headers=self._headers(account), json=body)
                if resp.status_code != 200:
                    text = (await resp.aread()).decode("utf-8", "replace")
                    err = self.parse_error(resp.status_code, text)
                    if resp.status_code in (401, 403):
                        raise _CredentialRejected(str(err)) from None
                    raise err
                async for line in resp.aiter_lines():
                    frame = _parse_frame(line)
                    if frame is None:
                        continue
                    if "__error__" in frame:
                        status, text = frame["__error__"]
                        raise self.parse_error(status, text)
                    yield frame
        except httpx.ReadTimeout as exc:
            # §8-C 超时止损：读超时 = 上游停滞，不是账号问题——不换号、不喂连败
            raise _UpstreamTimeout(f"上游读超时（upstream_timeout）：{exc}") from exc
        except httpx.HTTPError as exc:
            raise PlatformUpstreamError(f"上游网络错误: {exc}", status=502) from exc

    async def _frames_with_rotation(self, request: ChatRequest) -> AsyncIterator[dict]:
        """账号池换号编排 + 健康喂入口（健康管理 spec §2/§8，2026-10-17 同批）。

        分流表（`_classify_upstream_error`）：

        - 401/403 → 换号（每号一次）、不喂（凭证问题由换号/重登处理）；
        - session（11140 request illegal / 12153）→ 换号、不喂；全池 → 503 引导重登；
        - 6004 / 11140 限流文案 → 写 (账号, 模型) 冷却 + 换号（其他号可能可用）；
          已产出帧不换号 → 429（含恢复时间）；全池命中 → 400 model_unavailable；
        - 11102 → 写 (账号, 模型) 负缓存 + 换号（跳过该号）；已产出帧不换号；
          全池命中 → 400 model_unavailable；
        - generic（5xx/网络）：未产出帧 → 喂连败 + 换号一次（预算 1，防上游整体
          故障时全池轮转）；已产出帧 → 喂连败 + **绝不换号**（进流纪律）；
        - business（4xx 业务码）→ 不喂不换，透传；
        - 读超时 → 不换号、不喂（§8-C）。

        成功判定（§8-B）：仅流正常收尾（含 0 帧干净结束）记成功——清 11102
        负缓存 + 清零连败/挂起。
        """
        body = self.transform_request(request)
        pid = self._profile.platform_id
        display = self._profile.display_name
        tried: set[str] = set()
        rejected: set[str] = set()  # 凭证/会话被拒的号（全池挂起成因判定用）
        generic_switched = False  # 5xx/网络换号预算：一次
        total = len(await self._normal_accounts())
        while True:
            account = await self._pick_account(tried, rejected)
            uid = str(account.get("uid") or "")
            self._active_uid = uid
            await _log_pick(self._profile.platform_id, self._model, account)
            account = await self._account_adapter.ensure_token(account)  # 账号层刷新（profile 正确）
            yielded = False
            try:
                async for frame in self._stream_once(account, body):
                    yielded = True
                    yield frame
            except _CredentialRejected:
                tried.add(uid)
                rejected.add(uid)
                logger.warning("[wb-chat] 账号 %s 凭证被拒，换号重试", uid[:8])
                if len(tried) >= total:
                    raise NoAvailableAccountError(
                        f"全部 {display} 账号凭证失效，请到供应商页重新登录") from None
                continue
            except _UpstreamTimeout as exc:
                # 不换号、不喂连败（轮转只会让每个健康号各吃一次读超时）
                raise PlatformUpstreamError(str(exc), status=504) from exc
            except _UpstreamCodeError as exc:
                kind = _classify_upstream_error(exc)
                if kind == "model_cooldown":
                    until = await store.note_rate_limit(
                        pid, uid, self._model, reset_at_ms=_parse_reset_at_ms(exc.raw_msg))
                    logger.warning("[wb-chat] 模型 %s 限流（账号 %s），冷却至 %s",
                                   self._model, uid[:8], _fmt_ms(until))
                    if yielded:
                        # 已产出帧：不换号（进流纪律）→ 429 透传（含恢复时间）
                        raise PlatformUpstreamError(
                            f"{exc}；该模型冷却至 {_fmt_ms(until)}", status=429) from exc
                    tried.add(uid)
                    if len(tried) >= total:
                        pool = await self._normal_accounts()
                        earliest = _earliest_model_unlock_ms(
                            pool, self._model, store.now_ms()) or until
                        raise ModelUnavailableError(
                            f"模型 {self._model} 全池冷却中（{len(pool)} 个账号均限流），"
                            f"最早 {_fmt_ms(earliest)} 解封",
                            accounts=len(pool), unlock_at_ms=earliest) from exc
                    continue
                if kind == "model_missing":
                    unlock = await store.note_model_missing(pid, uid, self._model)
                    if yielded:
                        raise ModelUnavailableError(
                            f"模型 {self._model} 当前不可用：{exc}；"
                            f"最早 {_fmt_ms(unlock)} 解封",
                            accounts=total, unlock_at_ms=unlock) from exc
                    tried.add(uid)
                    logger.warning("[wb-chat] 账号 %s 模型 %s 不可用（11102），换号重试",
                                   uid[:8], self._model)
                    if len(tried) >= total:
                        pool = await self._normal_accounts()
                        earliest = _earliest_model_unlock_ms(
                            pool, self._model, store.now_ms()) or unlock
                        raise ModelUnavailableError(
                            f"模型 {self._model} 全池不可用（上游 11102），"
                            f"最早 {_fmt_ms(earliest)} 解封",
                            accounts=len(pool), unlock_at_ms=earliest) from exc
                    continue
                if kind == "session":
                    if yielded:
                        raise  # 已产出帧：绝不换号（防重复产出/重复计费）
                    tried.add(uid)
                    rejected.add(uid)
                    logger.warning("[wb-chat] 账号 %s 会话失效，换号重试: %s", uid[:8], exc)
                    if len(tried) >= total:
                        raise NoAvailableAccountError(
                            f"全部 {display} 账号会话失效，请到供应商页重新登录") from None
                    continue
                if kind == "business":
                    raise  # 请求问题不是账号问题：不喂不换
                # generic：5xx / 网络（不知道原因的失败）——喂连败
                await store.note_failure(pid, uid)
                if yielded:
                    raise
                if generic_switched or len(tried) + 1 >= total:
                    raise  # 换号预算用尽 / 已无更多号：透传最后错误
                generic_switched = True
                tried.add(uid)
                logger.warning("[wb-chat] 账号 %s 上游错误，换号重试: %s", uid[:8], exc)
                continue
            except PlatformUpstreamError as exc:
                # 网络层错误（httpx 异常包装 502）等：generic 语义
                await store.note_failure(pid, uid)
                if yielded:
                    raise
                if generic_switched or len(tried) + 1 >= total:
                    raise
                generic_switched = True
                tried.add(uid)
                logger.warning("[wb-chat] 账号 %s 上游错误，换号重试: %s", uid[:8], exc)
                continue
            else:
                # 流正常收尾：记成功——清 11102 负缓存 + 清零连败/挂起（§8-B）
                await store.clear_model_missing(pid, uid, self._model)
                await store.note_success(pid, uid)
                return

    # ─────────────────────── 出站事件契约 ───────────────────────

    async def stream_structured(self, request: ChatRequest) -> AsyncIterator[dict]:
        """上游 SSE → relay 事件（thinking/content/tool_call/done；同 Ta3Provider 契约）。"""
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: dict[int, dict] = {}
        finish_reason: str | None = None
        usage: Usage | None = None
        raw_tail: list[str] = []

        async for frame in self._frames_with_rotation(request):
            # raw_tail 仅本地诊断（EmptyStreamError 语义）；Usage 对象经 default=str 兜底
            raw_tail = [*raw_tail[-7:],
                        json.dumps(frame, ensure_ascii=False, default=str)[:300]]
            if frame.get("__usage__") is not None:
                usage = frame["__usage__"]
            if frame.get("__finish__"):
                finish_reason = str(frame["__finish__"])
            for ev in _frame_events(frame):
                if ev["type"] == "content":
                    content_parts.append(ev["delta"])
                elif ev["type"] == "thinking":
                    thinking_parts.append(ev["delta"])
                else:
                    _merge_tool_call(tool_calls, ev)
                yield ev

        yield {
            "type": "done",
            "content": "".join(content_parts) or None,
            "thinking": "".join(thinking_parts) or None,
            "tool_calls": _assemble_tool_calls(tool_calls),
            "finish_reason": finish_reason or "stop",
            "usage": usage or Usage(),
            "raw_tail": raw_tail,
        }

    async def chat(self, request: ChatRequest) -> ChatResponse:
        """非流式：内部收流聚合（B2：上游仅流式）。"""
        response = ChatResponse(content=None, tool_calls=[], finish_reason="stop",
                                usage=Usage())
        async for event in self.stream_structured(request):
            if event["type"] == "done":
                response = ChatResponse(
                    content=event["content"],
                    thinking=event["thinking"],
                    tool_calls=event["tool_calls"],
                    finish_reason=event["finish_reason"],
                    usage=event["usage"],
                    model=self._model,
                )
        return response


# ─────────────────────── 模块级工具 ───────────────────────

_cursor = 0


def _next_cursor() -> int:
    """进程内轮询游标（事件循环单线程语义下无锁安全）。"""
    global _cursor
    cur = _cursor
    _cursor = (cur + 1) % 1_000_000
    return cur


def reset_cursor() -> None:
    """测试复位。"""
    global _cursor
    _cursor = 0


def _message_payload(m: ChatMessage) -> dict:
    """ChatMessage → OpenAI 线格式（含图片块与 tool 往返字段）。"""
    payload: dict = {"role": "system" if m.role == "developer" else m.role}
    if m.content_blocks:
        blocks: list[dict] = []
        if m.content:
            blocks.append({"type": "text", "text": m.content})
        blocks.extend(m.content_blocks)
        payload["content"] = blocks
    else:
        payload["content"] = m.content
    if m.role == "tool" and m.tool_call_id:
        payload["tool_call_id"] = m.tool_call_id
    if m.role == "assistant" and m.tool_calls:
        payload["tool_calls"] = [
            {"id": tc.get("id") or "",
             "type": "function",
             "function": {"name": tc.get("name") or "",
                          "arguments": _arguments_str(tc.get("arguments"))}}
            for tc in m.tool_calls]
    if m.reasoning_content:
        payload["reasoning_content"] = m.reasoning_content
    return payload


def _arguments_str(args) -> str:
    if isinstance(args, str):
        return args
    if args is None:
        return "{}"
    return json.dumps(args, ensure_ascii=False)


def _flatten_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text") or "") for b in content
                         if isinstance(b, dict))
    return ""


def _neutralize_system_messages(messages: list[dict]) -> None:
    """B3：system 命中 agent 身份模式或超长 → 替换中性句（保持 str/块形状）。"""
    for msg in messages:
        if msg.get("role") != "system":
            continue
        text = _flatten_content(msg.get("content"))
        if not text:
            continue
        if len(text) > _SYSTEM_MAX_LEN or _AGENT_PATTERN.search(text):
            if isinstance(msg.get("content"), str):
                msg["content"] = NEUTRAL_SYSTEM_PROMPT
            else:
                msg["content"] = [{"type": "text", "text": NEUTRAL_SYSTEM_PROMPT}]


def _ensure_system_first(messages: list[dict]) -> None:
    """首条 system 兜底（panel ensureConsoleSystem；国际版专属）。

    2026-10-14 国际版小号探针实测：首条非 system → 400/11128
    "first message is not system prompt"。首条已是 system 不注入；其余前置
    `CONSOLE_SYSTEM_PROMPT`（面板同款文本）。就地改写，零返回值。
    """
    if not messages:
        return
    first = messages[0]
    if isinstance(first, dict) and \
            str(first.get("role") or "").strip().lower() == "system":
        return
    messages.insert(0, {"role": "system", "content": CONSOLE_SYSTEM_PROMPT})


def _parse_frame(line: str) -> dict | None:
    """上游 SSE 行 → 精简帧；心跳/空帧/[DONE]/不可解析行 → None。

    精简帧形状：{"choices": [{index, content?, reasoning_content?, tool_calls?}],
    "__usage__": Usage|None, "__finish__": str|None, "__error__": (status, body)?}
    白名单重建：`function_call`/`refusal`/`extra_fields`/`logprobs` 等腾讯字段剥离。
    """
    text = str(line or "").strip()
    if not text or text.startswith(":") or not text.startswith("data:"):
        return None
    payload = text[5:].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("error"):
        return {"__error__": (500, json.dumps(obj["error"], ensure_ascii=False)[:300])}
    code = obj.get("code")
    if code not in (None, 0):
        return {"__error__": (400, payload[:600])}  # 业务错误信封
    out: dict = {"choices": []}
    for ch in obj.get("choices") or []:
        if not isinstance(ch, dict):
            continue
        delta = ch.get("delta") or {}
        slim: dict = {"index": ch.get("index", 0)}
        if isinstance(delta.get("content"), str) and delta["content"]:
            slim["content"] = delta["content"]
        if isinstance(delta.get("reasoning_content"), str) and delta["reasoning_content"]:
            slim["reasoning_content"] = delta["reasoning_content"]
        tcs = delta.get("tool_calls")
        if isinstance(tcs, list) and tcs:
            slim["tool_calls"] = [_slim_tool_call(tc) for tc in tcs if isinstance(tc, dict)]
        if ch.get("finish_reason"):
            out["__finish__"] = str(ch["finish_reason"])
        out["choices"].append(slim)
    if isinstance(obj.get("usage"), dict):
        out["__usage__"] = _usage_from_upstream(obj["usage"])
    return out


def _slim_tool_call(tc: dict) -> dict:
    """工具调用分片白名单重建：只留 index/id/type/name/arguments。"""
    fn = tc.get("function") or {}
    slim: dict = {"index": tc.get("index", 0), "type": tc.get("type") or "function"}
    if tc.get("id"):
        slim["id"] = str(tc["id"])
    if fn.get("name"):
        slim["name"] = str(fn["name"])
    if fn.get("arguments") is not None:
        slim["arguments"] = str(fn["arguments"])
    return slim


def _usage_from_upstream(usage: dict) -> Usage:
    """上游 usage → relay Usage（B8：reasoning_tokens 取 completion_tokens_details）。"""
    details = usage.get("completion_tokens_details") or {}
    return Usage(
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        total_tokens=int(usage.get("total_tokens") or 0),
        reasoning_tokens=int(details.get("reasoning_tokens") or 0),
    )


def _frame_events(frame: dict) -> list[dict]:
    """精简帧 → relay 事件列表（content/thinking/tool_call）。"""
    events: list[dict] = []
    for ch in frame.get("choices") or []:
        if ch.get("content"):
            events.append({"type": "content", "delta": ch["content"]})
        if ch.get("reasoning_content"):
            events.append({"type": "thinking", "delta": ch["reasoning_content"]})
        for tc in ch.get("tool_calls") or []:
            events.append({"type": "tool_call", "delta": tc})
    return events


def _merge_tool_call(acc: dict[int, dict], ev: dict) -> None:
    """按 index 合并工具分片（首片带 id/name；后续分片只带 arguments 增量）。"""
    tc = ev["delta"]
    idx = int(tc.get("index") or 0)
    cur = acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
    if tc.get("id"):
        cur["id"] = str(tc["id"])
    if tc.get("name"):
        cur["name"] = str(tc["name"])
    if tc.get("arguments"):
        cur["arguments"] += str(tc["arguments"])


def _assemble_tool_calls(acc: dict[int, dict]) -> list[dict]:
    """合并表 → ChatResponse 工具调用列表（arguments 解析为对象；坏 JSON 原样）。"""
    out: list[dict] = []
    for idx in sorted(acc):
        cur = acc[idx]
        try:
            args: object = json.loads(cur["arguments"]) if cur["arguments"] else {}
        except ValueError:
            args = cur["arguments"]
        out.append({"id": cur["id"] or f"call_{idx:02d}", "name": cur["name"],
                    "arguments": args})
    return out


__all__ = ["AllAccountsSuspendedError", "ModelUnavailableError",
           "NoAvailableAccountError", "WorkBuddyChatAdapter",
           "factory", "reset_cursor"]


def factory(bare_model: str, *, ctx=None, model_entry: dict | None = None,
            **_) -> WorkBuddyChatAdapter:
    """adapter 注册表工厂（CN 形态——自定义供应商/测试沿用；零回归）。"""
    return WorkBuddyChatAdapter(bare_model, ctx=ctx, model_entry=model_entry)


def factory_for(profile: WbRealm):
    """按 profile 构造 factory（`register_provider` 双注册用）。"""
    def _factory(bare_model: str, *, ctx=None, model_entry: dict | None = None,
                 **_) -> WorkBuddyChatAdapter:
        return WorkBuddyChatAdapter(bare_model, ctx=ctx, model_entry=model_entry,
                                    profile=profile)
    return _factory


# 注册到出站 adapter 注册表（导入即生效；双域双前缀——CN `wbcn` / 国际版 `wbg`）
from relay import adapter_registry  # noqa: E402
from relay.platforms.workbuddy import catalog as _catalog  # noqa: E402
from relay.platforms.workbuddy.realms import ALL_REALMS  # noqa: E402


def _availability_for(profile: WbRealm):
    async def _available() -> bool:
        """可用性探测：本池是否有启用号（routes 区分 503/404 用）。"""
        accounts = await store.load_accounts(profile.platform_id)
        return any(store.is_selectable(a) for a in accounts)
    return _available


def _directory_for(profile: WbRealm):
    async def _load() -> list[dict]:
        return await _catalog.load_models(profile=profile)
    return _load


for _realm in ALL_REALMS:
    adapter_registry.register_provider(
        _realm.model_prefix,
        directory=_directory_for(_realm),
        factory=factory_for(_realm),
        availability=_availability_for(_realm),
    )

# 兼容符号（存量测试/脚本引用；CN 池探测——与 register_provider 处同一实现）
_available = _availability_for(REALM_CN)
