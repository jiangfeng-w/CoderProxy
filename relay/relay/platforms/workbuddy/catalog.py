"""WorkBuddy 模型目录：`/v3/config` ∪ `/console/enterprises/personal/models`（§4.2 实测）。

实测（2026-10-08，小号）：
- `GET /v3/config`（桌面档 UA 即过）→ `data.models[]` 54 个（含 `auto`/`fast-model`
  /`balanced-model`/`deep-model` 别名与 `glm-5.3`/`kimi-k3-1` 等）；字段
  `id`/`name`/`maxInputTokens`/`maxOutputTokens`/`reasoning.{supportedEfforts,
  defaultEffort,canDisableThinking,summary}`/`supportsToolCall`/`supportsImages`。
- `GET /console/enterprises/personal/models` → `data.models[]` 31 个 + `data.agents[]`
  （`name:"cli"` 的 `models[]` 白名单）。
- **口径**：两路并集（v3 为主，企业端点补缺；去重按 id，v3 优先——2api-panel 同口径）；
  过滤非对话模型（`maxOutputTokens ≤ 256` / `tags` 含图/视频生成族
  `text-to-image`/`image-to-image`/`text-to-video`/`image-to-video` /
  id 前缀 `nes-`/`completion-`/`codewise-`）。
- **国际版 `/v3/config` 按 UA 三路并集**（2026-10-16 对齐参考实现 7b24e22；**同日本机小号实测**：
  桌面档 27 条 / IDE 档 7 条 / CLI 档 20 条（过滤非对话后），并集 **31** 条——桌面档独有 8
  （`gpt-6-sol`/`gpt-6-luna`/`gpt-6.1-sol`/`gpt-5.6-sol`/`gpt-6-astra`/`grok-4.7`/`gemini-3.8-flash`
  /`step-5-preview`）、IDE 补 3（`o4-mini`/`enhance-1.0`/`auto-chat`）、CLI 补 1（`deep-model`）。
  **各路集合随上游漂移**（参考实现 10-02 的「CLI 独有 deepseek 系列」在本次实测里已出现在桌面档），
  故按「桌面档主路 + 其余路补缺 id」的结构对齐、不锁具体模型名）。桌面档为主路（chat 路径同源 UA，
  字段权威），IDE/CLI 只补桌面档缺失的 id；CN 维持单路（参考实现 CN 侧亦单路）。显式
  `WB_USER_AGENT` 覆盖时退化为单路（用户自愈优先）。
- 多账号：取**首个 normal 号**的目录（不同套餐可见模型有差异；不做并集——目录随选号
  账号自然刷新，已知边界）。

输出条目与牛码目录条目同构（`name` 为裸名；`context_window`/`completion_options`
等 GUI 展示字段），由 `oai_adapter.model_to_openai` 统一序列化。
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from relay.config import settings
from relay.platforms import store
from relay.platforms.workbuddy.adapter import WorkBuddyAdapter
from relay.platforms.workbuddy.client import derive_stable_id, user_agent_for
from relay.platforms.workbuddy.realms import REALM_CN, WbRealm

logger = logging.getLogger(__name__)

PLATFORM_ID = REALM_CN.platform_id

V3_CONFIG_PATH = "/v3/config"
# CN 现状单候选（兼容常量；双域候选序见 realms.WbRealm.enterprise_model_paths——
# 国际版 /v2 家族优先 → /console 回退）
ENTERPRISE_MODELS_PATH = "/console/enterprises/personal/models"

# 国际版 `/v3/config` 三路探测的 IDE / CLI UA（参考实现 internal/upstream/client.go
# 常量逐字：`codeBuddyIDEUA` / `codeBuddyCLIUA`）。桌面档 UA 为 profile 默认（主路）。
_IDE_UA = "CodeBuddyIDE/4.12.0 CodeBuddy/4.12.0"
_CLI_UA = "CLI/2.63.2 CodeBuddy/2.63.2"

CACHE_TTL_S = 3600  # 目录缓存 1h（对齐 2api 谱系；失败负缓存 5min）
NEGATIVE_TTL_S = 300

# 缓存按 platform_id 分桶（双域 spec §3：现全局单例 → dict[platform_id]；
# 负缓存同理——两域模型目录天然独立，一域的失败不该拖累另一域）。
_caches: dict[str, dict] = {}
_negative_until: dict[str, float] = {}

# single-flight 锁按 platform_id 分桶（BUG-008 修复，2026-10-17）：冷缓存/过期
# 瞬间 N 并发各拉一轮上游（2N 次 GET）→ 锁内双检后只拉一轮（对齐
# adapter.py::_refresh_locks / ensure_token 既有模式；双域互不阻塞）。
_load_locks: dict[str, asyncio.Lock] = {}

# 账号层 adapter 按 profile 惰性缓存（单例改造：原模块级单例写死 CN 会在
# 国际版目录路径上拿错池实例；实例轻量无状态，按 platform_id 缓存即可）。
_platform_adapters: dict[str, WorkBuddyAdapter] = {}


def _lock_for(platform_id: str) -> asyncio.Lock:
    lock = _load_locks.get(platform_id)
    if lock is None:
        lock = asyncio.Lock()
        _load_locks[platform_id] = lock
    return lock


def _adapter_for(profile: WbRealm) -> WorkBuddyAdapter:
    adapter = _platform_adapters.get(profile.platform_id)
    if adapter is None:
        adapter = WorkBuddyAdapter(profile)
        _platform_adapters[profile.platform_id] = adapter
    return adapter


def _cache_for(platform_id: str) -> dict:
    cache = _caches.get(platform_id)
    if cache is None:
        cache = {"at": 0.0, "models": [], "uid": ""}
        _caches[platform_id] = cache
    return cache


def reset_cache() -> None:
    """测试复位（全桶清零 + 清 single-flight 锁——锁实例不得跨事件循环复用）。"""
    _caches.clear()
    _negative_until.clear()
    _load_locks.clear()


def _clear_cache(platform_id: str) -> None:
    """池空 → 清本桶缓存与负缓存（BUG-007：目录随账号态即时反映）。

    不清的话：删光/全停用后 1h TTL 内普通读仍返回 stale 目录；且残留负缓存会
    让「重新加号 → 普通读」在 TTL 内继续返回空。
    """
    _cache_for(platform_id).update({"at": 0.0, "models": [], "uid": ""})
    _negative_until.pop(platform_id, None)


async def load_models(*, force: bool = False,
                      transport: httpx.AsyncBaseTransport | None = None,
                      profile: WbRealm = REALM_CN) -> list[dict]:
    """取 WorkBuddy 模型目录（裸名条目列表；缓存 1h，失败负缓存 5min）。

    BUG-007（2026-10-17 修复）：账号检查先于缓存判断——池空（删光/全停用）→
    清本桶缓存 + 返回 `[]`（不触网、不设负缓存），「全停用 ≈ 池空」的目录语义
    即时成立（否则 stale 目录会造出「模型看得见、一用就 503」）。
    BUG-008（2026-10-17 修复）：single-flight——缓存 miss 进 per-platform 锁 +
    锁内双检，冷缓存 N 并发只拉一轮上游；`force=True` 绕锁强拉（语义即「立即强拉」）。
    """
    pid = profile.platform_id
    account = await _pick_account(profile)
    if account is None:
        _clear_cache(pid)
        return []
    cache = _cache_for(pid)
    if not force:
        now = time.time()
        if cache["models"] and now - cache["at"] < CACHE_TTL_S:
            return list(cache["models"])
        if now < _negative_until.get(pid, 0.0):
            return []
        async with _lock_for(pid):
            # 锁内双检（等锁期间并发可能已写入缓存；池也可能已空）
            now = time.time()
            if cache["models"] and now - cache["at"] < CACHE_TTL_S:
                return list(cache["models"])
            if now < _negative_until.get(pid, 0.0):
                return []
            account = await _pick_account(profile)
            if account is None:
                _clear_cache(pid)
                return []
            return await _load_and_cache(account, cache, pid,
                                         transport=transport, profile=profile)
    # force：绕锁强拉
    return await _load_and_cache(account, cache, pid,
                                 transport=transport, profile=profile)


async def _load_and_cache(account: dict, cache: dict, pid: str, *,
                          transport: httpx.AsyncBaseTransport | None = None,
                          profile: WbRealm = REALM_CN) -> list[dict]:
    """拉取并写缓存（成功写正缓存；空结果写负缓存 5min）。"""
    now = time.time()
    account = await _adapter_for(profile).ensure_token(account)
    models = await _fetch_union(account, transport=transport, profile=profile)
    if models:
        cache.update({"at": now, "models": models,
                      "uid": str(account.get("uid") or "")})
    else:
        _negative_until[pid] = now + NEGATIVE_TTL_S
        logger.warning("[wb-catalog] 目录拉取为空（%s，账号 %s）", pid,
                       str(account.get("uid") or "")[:8])
    return models


async def _pick_account(profile: WbRealm = REALM_CN) -> dict | None:
    """目录拉取选号：本池首个启用号（uid 排序稳定；全停用 ≈ 池空）。"""
    accounts = [a for a in await store.load_accounts(profile.platform_id)
                if store.is_selectable(a)]
    if not accounts:
        return None
    return sorted(accounts, key=lambda a: str(a.get("uid") or ""))[0]


async def _fetch_union(account: dict,
                       transport: httpx.AsyncBaseTransport | None = None,
                       profile: WbRealm = REALM_CN) -> list[dict]:
    """两路并发探测 → 并集（v3 优先；企业端点补缺；两路全败抛最后一个错误）。

    v3 路为 `_fetch_v3_routes`：CN 单路 / 国际版三路 UA 并集（见模块头注释）。"""
    results = await asyncio.gather(
        _fetch_v3_routes(account, transport=transport, profile=profile),
        _fetch_enterprise(account, transport=transport, profile=profile),
        return_exceptions=True)
    v3_models = results[0] if isinstance(results[0], list) else []
    ent_models = results[1] if isinstance(results[1], list) else []
    for r in results:
        if isinstance(r, Exception):
            logger.warning("[wb-catalog] 目录单路失败: %s", r)
    if not v3_models and not ent_models:
        for r in results:
            if isinstance(r, Exception):
                raise r
        return []
    merged: dict[str, dict] = {}
    for m in v3_models:
        merged[m["name"]] = m
    for m in ent_models:
        merged.setdefault(m["name"], m)
    return list(merged.values())


def _v3_uas(profile: WbRealm = REALM_CN) -> tuple[str | None, ...]:
    """`/v3/config` 探测 UA 路（None = profile 默认桌面档 UA）。

    global 三路并集（桌面档主路 + IDE + CLI，三路各有独有模型；见模块头注释）；
    CN 维持单路（参考实现 CN 侧亦单路）。显式 `wb_user_agent` 覆盖时退化为单路
    ——用户自愈路径优先，且不额外打上游。
    """
    if settings.wb_user_agent or not profile.is_global:
        return (None,)
    return (None, _IDE_UA, _CLI_UA)


async def _fetch_v3_routes(account: dict,
                           transport: httpx.AsyncBaseTransport | None = None,
                           profile: WbRealm = REALM_CN) -> list[dict]:
    """`/v3/config` 探测：单路（CN / 显式覆盖）或 global 三路 UA 并集。

    合并口径对齐参考实现 `mergeV3Routes`：桌面档条目字段权威（主路，chat 路径
    同源 UA），IDE/CLI 只补桌面档缺失的模型 id（字段取自各自条目）；单路失败
    只 WARN 不拖累其余路，全失败抛最后一个错误（与 `_fetch_union`「全败才抛」一致）。
    """
    uas = _v3_uas(profile)
    if len(uas) == 1:
        return await _fetch_v3(account, transport=transport, profile=profile)
    results = await asyncio.gather(
        *[_fetch_v3(account, transport=transport, profile=profile, ua=ua)
          for ua in uas],
        return_exceptions=True)
    merged: dict[str, dict] = {}
    any_ok = False
    for ua, result in zip(uas, results):
        if isinstance(result, Exception):
            logger.warning("[wb-catalog] v3/config 单路失败（UA=%s）: %s",
                           ua or "default", result)
            continue
        any_ok = True
        for m in result:
            merged.setdefault(m["name"], m)  # 主路优先：先到者（桌面档）字段权威
    if not any_ok:
        for result in results:
            if isinstance(result, Exception):
                raise result
        return []
    return list(merged.values())


async def _fetch_v3(account: dict,
                    transport: httpx.AsyncBaseTransport | None = None,
                    profile: WbRealm = REALM_CN,
                    ua: str | None = None) -> list[dict]:
    """`GET /v3/config` → data.models[]（v3 主路径；桌面档 UA 门禁；ua 覆盖可选）。"""
    url = f"{profile.catalog_base}{V3_CONFIG_PATH}"
    body = await _get_json(url, account, transport=transport, profile=profile, ua=ua)
    data = body.get("data") or {}
    raw = data.get("models") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    out = []
    for m in raw:
        entry = _to_entry(m)
        if entry:
            out.append(entry)
    return out


async def _fetch_enterprise(account: dict,
                            transport: httpx.AsyncBaseTransport | None = None,
                            profile: WbRealm = REALM_CN) -> list[dict]:
    """企业模型目录（按 agents[cli].models 过滤）：端点候选按 profile（双域 spec §2）。

    CN：/console 单候选（现状）；国际版：/v2 家族优先 → /console 回退（探针实测）。
    候选逐路尝试：全败抛最后一个错误（与 _fetch_union 的「全败才抛」口径一致）。
    """
    last_err: Exception | None = None
    body: dict | None = None
    for path in profile.enterprise_model_paths:
        try:
            body = await _get_json(f"{profile.catalog_base}{path}", account,
                                   transport=transport, profile=profile)
            break
        except (RuntimeError, ValueError) as exc:
            last_err = exc
            continue
    if body is None:
        if last_err is not None:
            raise last_err
        return []
    data = body.get("data") or {}
    if not isinstance(data, dict):
        return []
    cli_ids: list[str] = []
    for ag in data.get("agents") or []:
        if isinstance(ag, dict) and ag.get("name") == "cli":
            cli_ids = [str(x) for x in (ag.get("models") or [])]
            break
    out = []
    for m in data.get("models") or []:
        entry = _to_entry(m)
        if not entry:
            continue
        if cli_ids and entry["name"] not in cli_ids:
            continue
        out.append(entry)
    return out


async def _get_json(url: str, account: dict,
                    transport: httpx.AsyncBaseTransport | None,
                    profile: WbRealm = REALM_CN,
                    ua: str | None = None) -> dict:
    """带桌面档头族的 GET（缓存/注入点经 transport 参数；ua 覆盖仅三路探测用）。"""
    headers = _catalog_headers(account, profile=profile, ua=ua)
    async with httpx.AsyncClient(trust_env=False, timeout=30.0,
                                 transport=transport) as client:
        resp = await client.get(url, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"目录端点 HTTP {resp.status_code}: {resp.text[:120]}")
    body = resp.json()
    if not isinstance(body, dict) or body.get("code") not in (None, 0):
        raise RuntimeError(f"目录端点业务失败: code={body.get('code')}")
    return body


def _catalog_headers(account: dict, profile: WbRealm = REALM_CN,
                     ua: str | None = None) -> dict[str, str]:
    """目录端点头族（v3/config 桌面档；2api 实测 UA 门禁）。

    双域差异按 profile：Origin/Referer、Accept-Language、UA 平台段；域/企业头
    分派同 chat（§2 表）：国际版恒 X-Domain: www.workbuddy.ai + X-No-Enterprise-Id。
    `ua` 覆盖仅全球三路探测用（IDE/CLI 档；None = profile 默认桌面档/显式覆盖）。
    """
    uid = str(account.get("uid") or "")
    headers = {
        "User-Agent": ua or user_agent_for(profile),
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": profile.origin,
        "Referer": f"{profile.origin}/",
        "X-CodeBuddy-Request": "1",
        "Accept-Language": profile.accept_language,
        "Authorization": f"Bearer {account.get('access_token') or ''}",
    }
    if uid:
        headers["X-User-Id"] = uid
        headers["X-Machine-ID"] = derive_stable_id(uid, "machine")
        headers["X-Session-ID"] = derive_stable_id(uid, "session")
    if profile.chat_domain:
        # 国际版：恒声明域 + 无企业（panel injectGlobalChatHeaders 口径延伸至目录）
        headers["X-Domain"] = profile.chat_domain
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


def _to_entry(m) -> dict | None:
    """上游模型条目 → 目录条目（裸名 name + 牛码同构字段）；非对话/脏数据过滤。"""
    if not isinstance(m, dict):
        return None
    mid = str(m.get("id") or "").strip()
    if not mid or _non_chat_model(m):
        return None
    reasoning = m.get("reasoning") if isinstance(m.get("reasoning"), dict) else {}
    efforts = [str(x) for x in (reasoning.get("supportedEfforts") or []) if str(x).strip()]
    levels = [{"level": lv, "label": ""} for lv in efforts]
    max_out = m.get("maxOutputTokens")
    ctx = m.get("maxInputTokens") or m.get("contextWindow")
    return {
        "name": mid,  # 裸名（前缀由 routes 合并层添加）
        "title": m.get("name") or mid,
        "context_window": int(ctx) if isinstance(ctx, (int, float)) else None,
        "max_output_tokens": int(max_out) if isinstance(max_out, (int, float)) else None,
        "provider": "workbuddy",  # 目录标记（内部用，不下发 agent）
        "completion_options": {
            "thinkingEnabled": bool(reasoning.get("supportedEfforts")
                                    or reasoning.get("effort")
                                    or reasoning.get("defaultEffort")),
            "thinkingLevels": levels,
            "canDisableThinking": bool(reasoning.get("canDisableThinking")),
            "defaultEffort": str(reasoning.get("defaultEffort") or
                                 reasoning.get("effort") or ""),
        },
        "is_multimodal": bool(m.get("supportsImages")),
        "supports_tool_call": bool(m.get("supportsToolCall", True)),
    }


def _non_chat_model(m: dict) -> bool:
    """非对话模型过滤（B 实测口径）：tiny 输出 / 图·视频生成 tag / 补全族前缀。

    tag 族扩展（2026-10-16 对齐参考实现 `client.go::nonChatModel`）：
    `image-to-image`/`text-to-video`/`image-to-video` 与前缀族并列——桌面档目录会
    带入 `seedance` 视频、`gpt-image` 系列（两域共用口径），不拦会混进 /v1/models
    且选中报 11102。
    """
    mid = str(m.get("id") or "").lower()
    if mid.startswith(("nes-", "completion-", "codewise-")):
        return True
    max_out = m.get("maxOutputTokens")
    if isinstance(max_out, (int, float)) and 0 < max_out <= 256:
        return True
    tags = m.get("tags") or []
    if isinstance(tags, list) and any(t in tags for t in (
            "text-to-image", "image-to-image", "text-to-video", "image-to-video")):
        return True
    return False


__all__ = ["load_models", "reset_cache"]
