"""WorkBuddy 模型目录：`/v3/config` ∪ `/console/enterprises/personal/models`（§4.2 实测）。

实测（2026-10-08，小号）：
- `GET /v3/config`（桌面档 UA 即过）→ `data.models[]` 54 个（含 `auto`/`fast-model`
  /`balanced-model`/`deep-model` 别名与 `glm-5.3`/`kimi-k3-1` 等）；字段
  `id`/`name`/`maxInputTokens`/`maxOutputTokens`/`reasoning.{supportedEfforts,
  defaultEffort,canDisableThinking,summary}`/`supportsToolCall`/`supportsImages`。
- `GET /console/enterprises/personal/models` → `data.models[]` 31 个 + `data.agents[]`
  （`name:"cli"` 的 `models[]` 白名单）。
- **口径**：两路并集（v3 为主，企业端点补缺；去重按 id，v3 优先——2api-panel 同口径）；
  过滤非对话模型（`maxOutputTokens ≤ 256` / `tags` 含 `text-to-image` /
  id 前缀 `nes-`/`completion-`/`codewise-`）。
- 多账号：取**首个 normal 号**的目录（不同套餐可见模型有差异；不做并集——目录随选号
  账号自然刷新，已知边界）。

输出条目与牛码目录条目同构（`name` 为裸名；`context_window`/`completion_options`
等 GUI 展示字段），由 `oai_adapter.model_to_openai` 统一序列化。
"""
from __future__ import annotations

import logging
import time

import httpx

from relay.config import settings
from relay.platforms import store
from relay.platforms.workbuddy.adapter import WorkBuddyAdapter
from relay.platforms.workbuddy.client import CN_ORIGIN, derive_stable_id

logger = logging.getLogger(__name__)

PLATFORM_ID = "workbuddy"

V3_CONFIG_PATH = "/v3/config"
ENTERPRISE_MODELS_PATH = "/console/enterprises/personal/models"

CACHE_TTL_S = 3600  # 目录缓存 1h（对齐 2api 谱系；失败负缓存 5min）
NEGATIVE_TTL_S = 300

_cache: dict = {"at": 0.0, "models": [], "uid": ""}
_negative_until = 0.0

_platform_adapter = WorkBuddyAdapter()


def reset_cache() -> None:
    """测试复位。"""
    global _negative_until
    _cache.update({"at": 0.0, "models": [], "uid": ""})
    _negative_until = 0.0


async def load_models(*, force: bool = False,
                      transport: httpx.AsyncBaseTransport | None = None) -> list[dict]:
    """取 WorkBuddy 模型目录（裸名条目列表；缓存 1h，失败负缓存 5min）。"""
    global _negative_until
    now = time.time()
    if not force:
        if _cache["models"] and now - _cache["at"] < CACHE_TTL_S:
            return list(_cache["models"])
        if now < _negative_until:
            return []
    account = await _pick_account()
    if account is None:
        return []
    account = await _platform_adapter.ensure_token(account)
    models = await _fetch_union(account, transport=transport)
    if models:
        _cache.update({"at": now, "models": models,
                       "uid": str(account.get("uid") or "")})
    else:
        _negative_until = now + NEGATIVE_TTL_S
        logger.warning("[wb-catalog] 目录拉取为空（账号 %s）",
                       str(account.get("uid") or "")[:8])
    return models


async def _pick_account() -> dict | None:
    """目录拉取选号：首个 normal 号（uid 排序稳定）。"""
    accounts = [a for a in await store.load_accounts(PLATFORM_ID)
                if str(a.get("status") or "normal") == "normal"]
    if not accounts:
        return None
    return sorted(accounts, key=lambda a: str(a.get("uid") or ""))[0]


async def _fetch_union(account: dict,
                       transport: httpx.AsyncBaseTransport | None = None) -> list[dict]:
    """两路并发探测 → 并集（v3 优先；企业端点补缺；两路全败抛最后一个错误）。"""
    import asyncio
    results = await asyncio.gather(
        _fetch_v3(account, transport=transport),
        _fetch_enterprise(account, transport=transport),
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


async def _fetch_v3(account: dict,
                    transport: httpx.AsyncBaseTransport | None = None) -> list[dict]:
    """`GET /v3/config` → data.models[]（v3 主路径；桌面档 UA 门禁）。"""
    url = f"{settings.wb_api_base}{V3_CONFIG_PATH}"
    body = await _get_json(url, account, transport=transport)
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
                            transport: httpx.AsyncBaseTransport | None = None) -> list[dict]:
    """`GET /console/enterprises/personal/models` → 按 agents[cli].models 过滤。"""
    url = f"{settings.wb_api_base}{ENTERPRISE_MODELS_PATH}"
    body = await _get_json(url, account, transport=transport)
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
                    transport: httpx.AsyncBaseTransport | None) -> dict:
    """带桌面档头族的 GET（缓存/注入点经 transport 参数）。"""
    headers = _catalog_headers(account)
    async with httpx.AsyncClient(trust_env=False, timeout=30.0,
                                 transport=transport) as client:
        resp = await client.get(url, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"目录端点 HTTP {resp.status_code}: {resp.text[:120]}")
    body = resp.json()
    if not isinstance(body, dict) or body.get("code") not in (None, 0):
        raise RuntimeError(f"目录端点业务失败: code={body.get('code')}")
    return body


def _catalog_headers(account: dict) -> dict[str, str]:
    """目录端点头族（v3/config 桌面档；2api 实测 UA 门禁）。"""
    uid = str(account.get("uid") or "")
    headers = {
        "User-Agent": settings.wb_user_agent,
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": CN_ORIGIN,
        "Referer": f"{CN_ORIGIN}/",
        "X-CodeBuddy-Request": "1",
        "Accept-Language": "zh-CN",
        "Authorization": f"Bearer {account.get('access_token') or ''}",
    }
    if uid:
        headers["X-User-Id"] = uid
        headers["X-Machine-ID"] = derive_stable_id(uid, "machine")
        headers["X-Session-ID"] = derive_stable_id(uid, "session")
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
    """非对话模型过滤（B 实测口径）：tiny 输出 / 图生成 / 补全族前缀。"""
    mid = str(m.get("id") or "").lower()
    if mid.startswith(("nes-", "completion-", "codewise-")):
        return True
    max_out = m.get("maxOutputTokens")
    if isinstance(max_out, (int, float)) and 0 < max_out <= 256:
        return True
    tags = m.get("tags") or []
    if isinstance(tags, list) and "text-to-image" in tags:
        return True
    return False


__all__ = ["load_models", "reset_cache"]
