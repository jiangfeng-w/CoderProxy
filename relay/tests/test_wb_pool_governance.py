"""三需求同批（2026-10-17）chat 层验收：优先级分组 / 启用停用 / 健康喂入口。

覆盖《WorkBuddy 反代账号优先级》《WorkBuddy 账号启用停用》《WorkBuddy 反代账号池
健康管理》的选号链与错误分流验收（MockTransport，不触网；账号池落 tmp 目录）。
"""
import asyncio
import time
from datetime import datetime

import httpx
import pytest

from app.models.schemas import ChatMessage, ChatRequest
from relay.config import settings
from relay.platforms import store
from relay.platforms.base import PlatformUpstreamError
from relay.platforms.workbuddy import chat as wbchat

_SSE_OK = [
    'data: {"id":"cmb-1","model":"glm-5.3-flash","choices":[{"index":0,'
    '"delta":{"role":"assistant","content":"","reasoning_content":"想"},"finish_reason":""}]}',
    'data: {"id":"cmb-1","model":"glm-5.3-flash","choices":[{"index":0,'
    '"delta":{"content":"你好"},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":18,"completion_tokens":87,"total_tokens":105}}',
    "data: [DONE]",
]


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    wbchat.reset_cursor()
    return tmp_path


async def _seed_account(uid="u_wb1", **extra):
    await store.upsert_account("workbuddy", {
        "uid": uid, "access_token": "at", "refresh_token": "rt",
        "expires_at": 9_999_999_999_999,
        **extra})
    account = await store.find_account("workbuddy", uid)
    assert account is not None
    return account


def _sse_response(lines, status=200):
    body = "".join(f"{ln}\n\n" for ln in lines).encode()
    return httpx.Response(status, content=body,
                          headers={"Content-Type": "text/event-stream"})


def _adapter(handler, model="glm-5.3-flash"):
    return wbchat.WorkBuddyChatAdapter(model, transport=httpx.MockTransport(handler))


async def _collect(adapter, request):
    return [e async for e in adapter.stream_structured(request)]


REQ = ChatRequest(model="glm-5.3-flash",
                  messages=[ChatMessage(role="user", content="hi")])


def _uid_of(request) -> str:
    return request.headers.get("X-User-Id", "")


# ─────────────────────────── 优先级（分组选号） ───────────────────────────


def test_priority_tier_selection(data_dir):
    """priority：最小优先级组内轮询；低优组不被选中（缺省全 1 时与现状等价）。"""
    _run(_seed_account(uid="u_a", priority=1))
    _run(_seed_account(uid="u_b", priority=1))
    _run(_seed_account(uid="u_c", priority=2))
    seen: list[str] = []

    def handler(request):
        seen.append(_uid_of(request))
        return _sse_response(_SSE_OK)

    for _ in range(3):
        _run(_collect(_adapter(handler), REQ))
    assert seen == ["u_a", "u_b", "u_a"]  # C（priority 2）永不命中


def test_priority_fallback_on_credential_rejected(data_dir):
    """高优组全部凭证被拒（单请求内 tried 耗尽）→ 本请求降级到次优组。"""
    _run(_seed_account(uid="u_a", priority=1))
    _run(_seed_account(uid="u_b", priority=2))
    calls: list[str] = []

    def handler(request):
        uid = _uid_of(request)
        calls.append(uid)
        if uid == "u_a":
            return httpx.Response(401, json={"code": 401, "msg": "expired"})
        return _sse_response(_SSE_OK)

    events = _run(_collect(_adapter(handler), REQ))
    assert calls == ["u_a", "u_b"]
    assert events[-1]["content"] == "你好"


# ─────────────────────────── 启用停用（全链路排除） ───────────────────────────


def test_disabled_account_never_selected(data_dir):
    """停用号永不被选（上游只收启用号的请求）。"""
    _run(_seed_account(uid="u_a", enabled=False))
    _run(_seed_account(uid="u_b"))
    seen: list[str] = []

    def handler(request):
        seen.append(_uid_of(request))
        return _sse_response(_SSE_OK)

    _run(_collect(_adapter(handler), REQ))
    _run(_collect(_adapter(handler), REQ))
    assert seen == ["u_b", "u_b"]


def test_all_disabled_raises_no_available(data_dir):
    """全停用 ≈ 池空 → NoAvailableAccountError（503 语义）。"""
    _run(_seed_account(uid="u_a", enabled=False))
    _run(_seed_account(uid="u_b", enabled=False))
    a = _adapter(lambda r: _sse_response(_SSE_OK))
    with pytest.raises(wbchat.NoAvailableAccountError):
        _run(_collect(a, REQ))


# ─────────────────────────── 连败软降级 ───────────────────────────


def test_consecutive_5xx_suspends_account(data_dir):
    """连败软降级：3 次 5xx → 挂起；之后请求不再选该号。"""
    _run(_seed_account(uid="u_a"))
    _run(_seed_account(uid="u_b"))
    calls: list[str] = []

    def handler(request):
        uid = _uid_of(request)
        calls.append(uid)
        if uid == "u_a":
            return httpx.Response(500, text="boom")
        return _sse_response(_SSE_OK)

    for _ in range(5):
        _run(_collect(_adapter(handler), REQ))
    assert calls.count("u_a") == 3  # a 被选中 3 次（第 3 次失败触发挂起）
    idx = len(calls)
    _run(_collect(_adapter(handler), REQ))
    assert calls[idx:] == ["u_b"]  # 挂起后只选 b


def test_degraded_account_returns_after_window(data_dir, monkeypatch):
    """挂起到期后回池（时间推进模拟）。"""
    _run(_seed_account(uid="u_a"))
    _run(_seed_account(uid="u_b"))
    base = 1_000_000_000_000
    now_box = {"t": base}
    monkeypatch.setattr(store, "now_ms", lambda: now_box["t"])
    for _ in range(3):
        _run(store.note_failure("workbuddy", "u_a", now=base))
    seen: list[str] = []

    def handler(request):
        seen.append(_uid_of(request))
        return _sse_response(_SSE_OK)

    _run(_collect(_adapter(handler), REQ))
    _run(_collect(_adapter(handler), REQ))
    assert seen == ["u_b", "u_b"]  # 挂起期内 a 不被选
    now_box["t"] = base + store.DEGRADE_WINDOW_MS + 1
    _run(_collect(_adapter(handler), REQ))
    assert seen[-1] == "u_a"  # 到期回池


def test_all_degraded_raises_suspended(data_dir, monkeypatch):
    """全池降权 → AllAccountsSuspendedError（429 语义）；Retry-After = 最近到期。"""
    _run(_seed_account(uid="u_a"))
    _run(_seed_account(uid="u_b"))
    base = 1_000_000_000_000
    monkeypatch.setattr(store, "now_ms", lambda: base)
    for uid in ("u_a", "u_b"):
        for _ in range(3):
            _run(store.note_failure("workbuddy", uid, now=base))
    a = _adapter(lambda r: _sse_response(_SSE_OK))
    with pytest.raises(wbchat.AllAccountsSuspendedError) as ei:
        _run(_collect(a, REQ))
    assert ei.value.retry_after_s == store.DEGRADE_WINDOW_MS // 1000


def test_success_clears_fail_count(data_dir):
    """成功一次 → 连败计数清零。"""
    _run(_seed_account(uid="u_a"))
    _run(_seed_account(uid="u_b"))
    calls: list[str] = []

    def handler(request):
        uid = _uid_of(request)
        calls.append(uid)
        if uid == "u_a" and calls.count("u_a") < 2:
            return httpx.Response(500, text="boom")
        return _sse_response(_SSE_OK)

    for _ in range(3):
        _run(_collect(_adapter(handler), REQ))
    acc = _run(store.find_account("workbuddy", "u_a"))
    assert acc["fail_count"] == 0 and acc["degraded_until"] == 0


# ─────────────────────────── 5xx / 超时（喂入口纪律） ───────────────────────────


def test_generic_5xx_rotates_once_only(data_dir):
    """5xx（未产出帧）→ 喂连败 + 换号一次；再失败透传（防上游整体故障全池轮转）。"""
    _run(_seed_account(uid="u_a"))
    _run(_seed_account(uid="u_b"))
    _run(_seed_account(uid="u_c"))
    calls: list[str] = []

    def handler(request):
        calls.append(_uid_of(request))
        return httpx.Response(500, text="boom")

    with pytest.raises(PlatformUpstreamError):
        _run(_collect(_adapter(handler), REQ))
    assert len(calls) == 2  # 只换一次（第 3 个号不打；第 2 个号由游标决定）
    assert calls[0] == "u_a" and calls[1] != "u_a"
    for uid in calls:
        acc = _run(store.find_account("workbuddy", uid))
        assert acc["fail_count"] == 1  # 两个被喂连败的号各 +1


def test_upstream_error_after_frames_never_rotates(data_dir):
    """已产出帧的上游错误：绝不换号（进流纪律）+ 喂连败。"""
    _run(_seed_account(uid="u_a"))
    _run(_seed_account(uid="u_b"))
    calls: list[str] = []

    def handler(request):
        calls.append(_uid_of(request))

        async def broken():
            yield (_SSE_OK[0] + "\n\n").encode()
            raise httpx.ReadError("mid-stream reset")

        return httpx.Response(200, content=broken(),
                              headers={"Content-Type": "text/event-stream"})

    with pytest.raises(PlatformUpstreamError):
        _run(_collect(_adapter(handler), REQ))
    assert calls == ["u_a"]  # 不换号
    acc = _run(store.find_account("workbuddy", "u_a"))
    assert acc["fail_count"] == 1


def test_read_timeout_no_rotation_no_penalty(data_dir):
    """读超时（upstream_timeout）：不换号（只打一次）、不喂连败。"""
    _run(_seed_account(uid="u_a"))
    _run(_seed_account(uid="u_b"))
    calls: list[str] = []

    def handler(request):
        calls.append(_uid_of(request))
        raise httpx.ReadTimeout("timed out")

    with pytest.raises(PlatformUpstreamError) as ei:
        _run(_collect(_adapter(handler), REQ))
    assert calls == ["u_a"]
    assert ei.value.status == 504
    assert "upstream_timeout" in str(ei.value)
    acc = _run(store.find_account("workbuddy", "u_a"))
    assert not acc.get("fail_count")


# ─────────────────────────── 6004 模型级冷却 ───────────────────────────


def test_6004_parses_reset_time_and_rotates(data_dir):
    """6004 带恢复时间 → 锁满上游墙钟（尊重上游时间）；未产出帧换号。"""
    _run(_seed_account(uid="u_a"))
    _run(_seed_account(uid="u_b"))
    reset_ms = int(time.time() * 1000) + 8 * 3600_000  # 8h（实测 8h14m 形态）
    reset_str = datetime.fromtimestamp(reset_ms / 1000,
                                       store.CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    calls: list[str] = []

    def handler(request):
        uid = _uid_of(request)
        calls.append(uid)
        if uid == "u_a":
            return _sse_response(
                ['data: {"code": 6004, "msg": "请求频率限制，将在 '
                 + reset_str + ' 重置"}'])
        return _sse_response(_SSE_OK)

    events = _run(_collect(_adapter(handler), REQ))
    assert calls == ["u_a", "u_b"]  # 换号（其他号可能可用）
    assert events[-1]["content"] == "你好"
    acc = _run(store.find_account("workbuddy", "u_a"))
    until = acc["model_cooldowns"]["glm-5.3-flash"]["until"]
    assert abs(until - reset_ms) < 2000  # 锁满（秒级格式化精度内）


def test_6004_all_pool_raises_model_unavailable(data_dir):
    """6004 全池命中 → ModelUnavailableError（400 语义）。"""
    _run(_seed_account(uid="u_a"))
    _run(_seed_account(uid="u_b"))
    a = _adapter(lambda r: _sse_response(
        ['data: {"code": 6004, "msg": "请求频率限制"}']))
    with pytest.raises(wbchat.ModelUnavailableError) as ei:
        _run(_collect(a, REQ))
    assert ei.value.accounts == 2


def test_6004_after_frames_429_no_rotate(data_dir):
    """已产出帧的 6004：不换号（进流纪律）→ 429 透传。"""
    _run(_seed_account(uid="u_a"))
    _run(_seed_account(uid="u_b"))
    calls: list[str] = []

    def handler(request):
        calls.append(_uid_of(request))

        async def stream():
            yield (_SSE_OK[0] + "\n\n").encode()
            yield ('data: {"code": 6004, "msg": "限流了"}\n\n').encode()

        return httpx.Response(200, content=stream(),
                              headers={"Content-Type": "text/event-stream"})

    with pytest.raises(PlatformUpstreamError) as ei:
        _run(_collect(_adapter(handler), REQ))
    assert calls == ["u_a"]
    assert ei.value.status == 429


def test_model_cooldown_skips_same_model_only(data_dir, monkeypatch):
    """冷却期内：同模型跳过该号；同账号其他模型不受影响。"""
    _run(_seed_account(uid="u_a"))
    _run(_seed_account(uid="u_b"))
    base = 1_000_000_000_000
    monkeypatch.setattr(store, "now_ms", lambda: base)
    _run(store.note_rate_limit("workbuddy", "u_a", "glm-5.3-flash", now=base))
    seen: list[str] = []

    def handler(request):
        seen.append(_uid_of(request))
        return _sse_response(_SSE_OK)

    _run(_collect(_adapter(handler), REQ))
    assert seen == ["u_b"]  # a 对 glm-5.3-flash 冷却中
    wbchat.reset_cursor()
    _run(_collect(_adapter(handler, model="other-model"), REQ))
    assert seen[-1] == "u_a"  # 其他模型不受影响


def test_all_model_cooled_raises_model_unavailable(data_dir, monkeypatch):
    """全池该模型冷却 → 选号空 → ModelUnavailableError（400 语义；带解封时刻）。"""
    _run(_seed_account(uid="u_a"))
    _run(_seed_account(uid="u_b"))
    base = 1_000_000_000_000
    monkeypatch.setattr(store, "now_ms", lambda: base)
    for uid in ("u_a", "u_b"):
        _run(store.note_rate_limit("workbuddy", uid, "glm-5.3-flash", now=base))
    a = _adapter(lambda r: _sse_response(_SSE_OK))
    with pytest.raises(wbchat.ModelUnavailableError) as ei:
        _run(_collect(a, REQ))
    assert ei.value.accounts == 2
    assert ei.value.unlock_at_ms == base + 60_000


# ─────────────────────────── 11102 负缓存 ───────────────────────────


def test_11102_negative_cache_skips_account(data_dir):
    """11102 → 写负缓存（10min）+ 换号；命中负缓存的号被跳过。"""
    _run(_seed_account(uid="u_a"))
    _run(_seed_account(uid="u_b"))
    calls: list[str] = []

    def handler(request):
        uid = _uid_of(request)
        calls.append(uid)
        if uid == "u_a":
            return _sse_response(['data: {"code": 11102, "msg": "model not found"}'])
        return _sse_response(_SSE_OK)

    events = _run(_collect(_adapter(handler), REQ))
    assert calls == ["u_a", "u_b"]  # a 写负缓存后换号
    assert events[-1]["content"] == "你好"
    acc = _run(store.find_account("workbuddy", "u_a"))
    assert acc["model_cooldowns"]["glm-5.3-flash"]["reason"] == "11102"
    _run(_collect(_adapter(handler), REQ))
    assert calls == ["u_a", "u_b", "u_b"]  # a 命中负缓存不再被打


def test_11102_all_pool_raises_model_unavailable(data_dir):
    """全池 11102 → ModelUnavailableError（400 语义；hint 带账号数）。"""
    _run(_seed_account(uid="u_a"))
    _run(_seed_account(uid="u_b"))
    a = _adapter(lambda r: _sse_response(
        ['data: {"code": 11102, "msg": "gone"}']))
    with pytest.raises(wbchat.ModelUnavailableError) as ei:
        _run(_collect(a, REQ))
    assert ei.value.accounts == 2
    assert ei.value.unlock_at_ms > 0


# ─────────────────────────── 11140 双语义 / 12153 会话失效 ───────────────────────────


def test_11140_request_illegal_rotates_no_penalty(data_dir):
    """11140 request illegal（授权类）→ 换号、不喂连败。"""
    _run(_seed_account(uid="u_a"))
    _run(_seed_account(uid="u_b"))
    calls: list[str] = []

    def handler(request):
        uid = _uid_of(request)
        calls.append(uid)
        if uid == "u_a":
            return _sse_response(['data: {"code": 11140, "msg": "request illegal"}'])
        return _sse_response(_SSE_OK)

    events = _run(_collect(_adapter(handler), REQ))
    assert calls == ["u_a", "u_b"]
    assert events[-1]["content"] == "你好"
    acc = _run(store.find_account("workbuddy", "u_a"))
    assert not acc.get("fail_count")


def test_11140_rate_limit_message_goes_model_cooldown(data_dir):
    """11140 限流文案 → 模型级冷却（同 code 双语义分流，按 msg 关键词）。"""
    _run(_seed_account(uid="u_a"))
    _run(_seed_account(uid="u_b"))
    calls: list[str] = []

    def handler(request):
        uid = _uid_of(request)
        calls.append(uid)
        if uid == "u_a":
            return _sse_response(
                ['data: {"code": 11140, "msg": "rate-limiting requests, retry later"}'])
        return _sse_response(_SSE_OK)

    _run(_collect(_adapter(handler), REQ))
    assert calls == ["u_a", "u_b"]
    acc = _run(store.find_account("workbuddy", "u_a"))
    assert acc["model_cooldowns"]["glm-5.3-flash"]["reason"] == "6004"


def test_12153_all_pool_raises_no_available(data_dir):
    """12153 会话失效：换号、不喂；全池 → 503 引导重登（不杀号）。"""
    _run(_seed_account(uid="u_a"))
    _run(_seed_account(uid="u_b"))
    calls: list[str] = []

    def handler(request):
        calls.append(_uid_of(request))
        return _sse_response(
            ['data: {"code": 12153, "msg": "Offline user session not found"}'])

    with pytest.raises(wbchat.NoAvailableAccountError):
        _run(_collect(_adapter(handler), REQ))
    assert calls == ["u_a", "u_b"]
    acc = _run(store.find_account("workbuddy", "u_a"))
    assert not acc.get("fail_count")
    assert acc.get("status") != "disabled"  # 不杀号


# ─────────────────────────── 国际版池直接用例（双域参数化验证） ───────────────────────────


def test_global_realm_same_governance(data_dir):
    """国际版池同款治理：停用排除 + 优先级分组（profile 参数化的同一份实现）。"""
    from relay.platforms.workbuddy.realms import REALM_GLOBAL

    async def _seed_global(uid, **extra):
        await store.upsert_account("workbuddy-global", {
            "uid": uid, "access_token": "at", "refresh_token": "rt",
            "expires_at": 9_999_999_999_999, "domain": "www.workbuddy.ai", **extra})

    _run(_seed_global("u_ga", enabled=False))          # 停用号
    _run(_seed_global("u_gb", priority=1))
    _run(_seed_global("u_gc", priority=2))             # 低优组
    seen: list[str] = []

    def handler(request):
        seen.append(_uid_of(request))
        return _sse_response(_SSE_OK)

    a = wbchat.WorkBuddyChatAdapter("glm-5.3-flash",
                                    transport=httpx.MockTransport(handler),
                                    profile=REALM_GLOBAL)
    _run(_collect(a, REQ))
    _run(_collect(a, REQ))
    assert seen == ["u_gb", "u_gb"]  # 停用号 u_ga 与低优组 u_gc 均不被选


def test_global_realm_health_filter(data_dir, monkeypatch):
    """国际版池同款健康过滤：全池降权 → AllAccountsSuspendedError（双域同构）。"""
    from relay.platforms.workbuddy.realms import REALM_GLOBAL

    async def _seed_global(uid):
        await store.upsert_account("workbuddy-global", {
            "uid": uid, "access_token": "at", "refresh_token": "rt",
            "expires_at": 9_999_999_999_999, "domain": "www.workbuddy.ai"})

    _run(_seed_global("u_g1"))
    _run(_seed_global("u_g2"))
    base = 1_000_000_000_000
    monkeypatch.setattr(store, "now_ms", lambda: base)
    for uid in ("u_g1", "u_g2"):
        for _ in range(3):
            _run(store.note_failure("workbuddy-global", uid, now=base))
    a = wbchat.WorkBuddyChatAdapter("glm-5.3-flash",
                                    transport=httpx.MockTransport(
                                        lambda r: _sse_response(_SSE_OK)),
                                    profile=REALM_GLOBAL)
    with pytest.raises(wbchat.AllAccountsSuspendedError):
        _run(_collect(a, REQ))


# ─────────────── 选号日志与轮询可见性（验收反馈 2026-10-17） ───────────────


def test_pick_logged_with_account(data_dir, monkeypatch):
    """选号日志：每次选号写 db（kind=wb_pick，detail 含 uid/昵称/优先级）。"""
    from relay import db

    _run(_seed_account(uid="u_a", nickname="一号", priority=2))
    events: list[dict] = []

    async def _fake_log(**fields):
        events.append(fields)
        return 0

    monkeypatch.setattr(db, "log_event", _fake_log)
    _run(_collect(_adapter(lambda r: _sse_response(_SSE_OK)), REQ))
    picks = [e for e in events if e.get("kind") == "wb_pick"]
    assert len(picks) == 1
    assert picks[0]["detail"]["uid"] == "u_a"
    assert picks[0]["detail"]["nickname"] == "一号"
    assert picks[0]["detail"]["priority"] == 2


def test_priority_same_tier_round_robin(data_dir):
    """同优先级多号严格轮询（3 号 A/B/C 默认全 1：A→B→C→A）——验收确认项。"""
    _run(_seed_account(uid="u_a"))
    _run(_seed_account(uid="u_b"))
    _run(_seed_account(uid="u_c"))
    seen: list[str] = []

    def handler(request):
        seen.append(_uid_of(request))
        return _sse_response(_SSE_OK)

    for _ in range(4):
        _run(_collect(_adapter(handler), REQ))
    assert seen == ["u_a", "u_b", "u_c", "u_a"]  # 严格轮询（跨请求游标连续）
