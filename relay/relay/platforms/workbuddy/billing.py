"""WorkBuddy 签到与配额查询（billing 域，指纹头见 client.billing_headers）。

契约（三方参考交叉验证，spec「API 契约」节）：
- 签到状态：POST /v2/billing/meter/checkin-activity-status（官方 CLI 用），失败回退
  /v2/billing/meter/checkin-status；仅 code===0 视成功；data 宽松解析
  （snake/camel 双写、bool 兼容 true/1/"true"，照抄 cockpit-tools parse_checkin_status_data）
- 手动签到：POST /v2/billing/meter/daily-checkin body {}；已签到幂等判定 =
  code 10001/14001 或 msg 含「已签到」/「already」→ 按成功返回
- 配额：POST /v2/billing/meter/get-user-resource（PageNumber/PageSize/ProductCode
  "p_tcaca"/Status [0,3]/PackageEndTimeRange）；响应多形态找数组
  （data/resources、data/data/resources、…/Response/Data/Accounts），聚合聚合值

token 保活由 adapter.ensure_token 前置保证；上游 401/403 → login_required。
"""
from __future__ import annotations

from datetime import datetime

from relay.config import settings
from relay.platforms import store
from relay.platforms.base import (
    CheckinResult,
    PlatformAuthError,
    PlatformUpstreamError,
)
from relay.platforms.workbuddy.client import (
    CN_ORIGIN,
    DEFAULT_OK_CODES,
    WbClient,
    billing_headers,
    parse_envelope,
)

PLATFORM_ID = "workbuddy"
_client = WbClient()


# 已签到幂等码：官方 CLI 语义（workbuddy-switch 交叉验证）
CHECKED_IN_CODES = (10001, 14001)

# get-user-resource 响应的数组形态（顺序即优先级，对齐 cockpit-tools user_resource_items）
_QUOTA_ITEMS_PATHS = (
    "/data/resources",
    "/data/data/resources",
    "/data/Response/Data/Accounts",
    "/data/data/Response/Data/Accounts",
    "/Response/Data/Accounts",
)


async def _billing_post(account: dict, path: str, json_body: dict,
                        ok_codes: tuple[int, ...] = DEFAULT_OK_CODES) -> dict:
    """billing 域 POST：指纹头组装 + 信封解析；401/403 → login_required。"""
    headers = billing_headers(
        access_token=account.get("access_token") or "",
        uid=account.get("uid") or "",
        domain=account.get("domain") or "",
        enterprise_id=account.get("enterprise_id") or "",
    )
    url = f"{settings.wb_api_base}{path}"
    try:
        body = await _client.request_json("POST", url, headers=headers,
                                               json_body=json_body)
    except PlatformUpstreamError as exc:
        if exc.status in (401, 403):
            raise PlatformAuthError(f"登录态失效: {exc}") from exc
        raise
    return parse_envelope(body, ok_codes=ok_codes, context=path)


def _loose_bool(data: dict, snake: str, camel: str, default=None):
    """宽松 bool：snake/camel 双写，true/1/"true" 兼容（对齐官方 JS 语义）。"""
    raw = data.get(snake, data.get(camel))
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    text = str(raw).strip().lower()
    if text in ("true", "1"):
        return True
    if text in ("false", "0"):
        return False
    return default


def _loose_num(data: dict, snake: str, camel: str, default=0) -> float:
    raw = data.get(snake, data.get(camel))
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _parse_checkin_status(data: dict) -> dict:
    """签到状态宽松解析（照抄 cockpit-tools parse_checkin_status_data 语义）。"""
    return {
        "today_checked_in": bool(_loose_bool(data, "today_checked_in", "todayCheckedIn", False)),
        "active": bool(_loose_bool(data, "active", "Active", True)),
        "streak_days": int(_loose_num(data, "streak_days", "streakDays", 0)),
        "daily_credit": int(_loose_num(data, "daily_credit", "dailyCredit", 0)),
    }


async def checkin_status_payload(account: dict) -> dict:
    """查上游签到状态：activity 优先，失败回退 legacy checkin-status。"""
    last_err: Exception | None = None
    for path in ("/v2/billing/meter/checkin-activity-status",
                 "/v2/billing/meter/checkin-status"):
        try:
            data = await _billing_post(account, path, {}, ok_codes=(0,))
            return _parse_checkin_status(data if isinstance(data, dict) else {})
        except (PlatformAuthError, PlatformUpstreamError) as exc:
            if isinstance(exc, PlatformAuthError) and exc.kind == "login_required":
                raise  # 登录态失效无需回退重试
            last_err = exc
    raise last_err if last_err else PlatformUpstreamError("签到状态查询失败")


async def refresh_checkin_status(account: dict) -> dict:
    """拉上游签到状态并落库（today_checked_in=True 时 last_checkin_date 记今天）。"""
    status = await checkin_status_payload(account)
    old_checkin = account.get("checkin") or {}
    checkin = {
        "last_checkin_date": store.today_cn() if status["today_checked_in"]
        else old_checkin.get("last_checkin_date", ""),
        "streak_days": status["streak_days"],
        "today_checked_in": status["today_checked_in"],
    }
    return await store.update_account(PLATFORM_ID, account["uid"], {"checkin": checkin})


async def perform_checkin(account: dict) -> tuple[CheckinResult, dict]:
    """手动签到（幂等）：已签到按成功返回；成功后更新 checkin 缓存并回传最新账号。"""
    raw = await _billing_raw_post(account, "/v2/billing/meter/daily-checkin", {})
    code = raw.get("code")
    msg = str(raw.get("message") or raw.get("msg") or "")

    if code in CHECKED_IN_CODES or "已签到" in msg or "already" in msg.lower():
        # 幂等成功；best-effort 补拉权威状态（streak/active）
        try:
            updated = await refresh_checkin_status(account)
        except (PlatformAuthError, PlatformUpstreamError):
            updated = dict(account)
            updated.setdefault("checkin", {})["today_checked_in"] = True
        return CheckinResult(success=True, message="今日已签到", today_checked_in=True), updated

    if code not in (0, 200):
        return (CheckinResult(success=False, message=msg or f"签到失败 (code={code})",
                              today_checked_in=False), account)

    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    streak = int(_loose_num(data, "streak_days", "streakDays",
                            _loose_num(account.get("checkin") or {}, "streak_days", "streak_days", 0)))
    credit = int(_loose_num(data, "credit", "todayCredit",
                            _loose_num(data, "today_credit", "todayCredit", -1)))
    patch = {"checkin": {"last_checkin_date": store.today_cn(), "streak_days": streak,
                         "today_checked_in": True}}
    updated = await store.update_account(PLATFORM_ID, account["uid"], patch)
    return (CheckinResult(success=True, message="签到成功", streak_days=streak,
                          credit=credit if credit >= 0 else None,
                          today_checked_in=True), updated)


def _quota_body() -> dict:
    """配额查询请求体：**空 {}**（免费号实测唯一能拿到资源包的形态）。

    cockpit-tools / workbuddy2api-panel 的带筛选 body（ProductCode p_tcaca /
    Status [0,3] / PackageEndTimeRange）对免费号返回空资源包——e2e 实测
    （2026-10-07）：同一免费号，空 body（9router CLI 组合）能拿到 Accounts[]，
    大 body 只回全 0。
    """
    return {}


def _web_summary_headers(account: dict) -> dict[str, str]:
    """① 主路径头：官网套餐页形态（对齐 workbuddy-switch credits.rs resource_auth_headers）。

    与登录 platform=workbuddy（桌面端）身份配对一致：桌面端 billing 指纹 +
    X-Client-Platform: web（WorkBuddy 用户中心 Axios 拦截器恒带，桌面端调同一组
    billing 接口时同样保持，避免网关当未知客户端）+ Origin/Referer 官网套餐页。
    summary 端点是官方套餐页设计用途，天然带回免费/付费资源包。
    """
    headers = billing_headers(
        access_token=account.get("access_token") or "",
        uid=account.get("uid") or "",
        domain=account.get("domain") or "",
        enterprise_id=account.get("enterprise_id") or "",
    )
    headers["X-Client-Platform"] = "web"
    headers["Referer"] = f"{CN_ORIGIN}/profile/plans-usage"
    return headers


def _saas_headers(account: dict) -> dict[str, str]:
    """② 回退路径头：SaaS/CLI 伪装（对齐 9router providers/registry/codebuddy-cn.js）。

    与登录身份不一致（CLI 皮肤），仅作 summary 失败时的兼容回退——9router
    实测该组合在旧 get-user-resource 上对免费号有效。
    """
    return {
        "User-Agent": "CLI/2.108.1 CodeBuddy/2.108.1",
        "X-Product": "SaaS",
        "X-IDE-Type": "CLI",
        "X-IDE-Name": "CLI",
        "x-requested-with": "XMLHttpRequest",
        "x-codebuddy-request": "1",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {account.get('access_token') or ''}",
    }


def _dig(body: dict, pointer: str):
    """极简 JSON Pointer（/a/b/c），找到即返回。"""
    node = body
    for part in pointer.strip("/").split("/"):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _quota_items(body: dict) -> list:
    for path in _QUOTA_ITEMS_PATHS:
        arr = _dig(body, path)
        if isinstance(arr, list) and arr:
            return arr
    return []


async def fetch_quota(account: dict) -> dict:
    """拉取配额并聚合落库（多形态响应 → QuotaSnapshot 口径）。

    两级编排（身份一致性优先）：
    ① 主路径 POST /v2/billing/meter/get-user-resource-summary，空 body + 桌面端
       billing 指纹 + X-Client-Platform: web（workbuddy-switch 配对形态：
       platform=workbuddy 登录的官方套餐页端点，免费/付费包都回）；
    ② 回退 POST /v2/billing/meter/get-user-resource，空 body + SaaS/CLI 头
       （9router 实测组合，免费号可用；与登录身份不一致仅作兜底）。

    两路径响应共用同一套解析（summary 的数据在 data.resources 等形态，
    嵌套腾讯信封 data.Response.Data.Accounts 亦兼容）。
    """
    last_err: Exception | None = None
    for path, headers in (
        ("/v2/billing/meter/get-user-resource-summary", _web_summary_headers(account)),
        ("/v2/billing/meter/get-user-resource", _saas_headers(account)),
    ):
        try:
            raw = await _billing_raw_post(account, path, {}, headers=headers)
        except PlatformAuthError:
            raise  # 登录态失效：两路径都不用试了
        except PlatformUpstreamError as exc:
            last_err = exc
            continue
        items = _quota_items(raw)
        if items:
            return await _save_quota(account, items)
        # 空结果也继续试下一路径（summary 空 → 旧接口可能有）
    if last_err is not None:
        raise last_err
    return await _save_quota(account, [])


async def _save_quota(account: dict, items: list) -> dict:
    """逐包归一 + 聚合落库。

    每包归一为 {name, total, used, remaining, expire_at, cycle}（e2e 用户拍板
    2026-10-08：弹窗逐包展示——各包额度/到期不同，聚合一刀切会丢信息）；
    quota.packages 落逐包列表，total/remaining/used/expire_at 为聚合摘要。

    到期时间口径（对齐 workbuddy-switch/9router 的占位语义）：
    - 多形态归一为 "YYYY-MM-DD HH:MM:SS"（epoch 秒/毫秒时间戳也接受）
    - 周期包（CycleEndTime < DeductionEndTime）取 CycleEndTime（本轮刷新点，
      官方套餐页显示的「下次权益周期更新时间」即此）；
    - 一次性包取 DeductionEndTime/PackageEndTime（真实到期）
    - 超 730 天占位（9999/2035/2049）视为长期有效，被真实到期覆盖
    """
    packages: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        size = _item_num(item, "CycleCapacitySizePrecise", "CycleCapacitySize",
                         "CapacitySizePrecise", "CapacitySize")
        used = _item_num(item, "CycleCapacityUsedPrecise", "CycleCapacityUsed",
                         "CapacityUsedPrecise", "CapacityUsed")
        # 到期：周期包优先 CycleEndTime（下轮刷新点），一次性包用 DeductionEndTime；
        # 占位值（>730 天）不作为有效到期，但保留真实值的提取机会
        cycle_expire = _norm_expire(item.get("CycleEndTime"))
        final_expire = _norm_expire(item.get("DeductionEndTime")
                                    or item.get("PackageEndTime")
                                    or item.get("expire_at"))
        # 周期语义：CycleEndTime 严格早于 DeductionEndTime 即周期包
        # （DeductionEndTime 是长期占位不影响该判定——占位值恰恰是周期包的常态）
        is_cycle = bool(cycle_expire and final_expire
                        and _parse_dt(cycle_expire) < _parse_dt(final_expire))
        expire = cycle_expire if (is_cycle or (cycle_expire and not final_expire)) \
            else final_expire
        if _is_far_future(expire):
            expire = ""  # 纯长期占位：不显示到期
        packages.append({
            "name": str(item.get("PackageName") or item.get("SubProductName")
                        or item.get("name") or ""),
            "total": size,
            "used": used,
            "remaining": max(0.0, size - used),
            "expire_at": expire,
            "cycle": is_cycle,
        })
    total = sum(p["total"] for p in packages)
    used_sum = sum(p["used"] for p in packages)
    # 聚合到期 = 最近的未来真实到期（最早的过期才是用户关心的下限）
    future = [p["expire_at"] for p in packages if p["expire_at"]]
    expire_at = min(future) if future else ""
    quota = {
        "total": total,
        "remaining": max(0.0, total - used_sum),
        "used": used_sum,
        "expire_at": expire_at,
        "packages": packages,
        "fetched_at": datetime.now(store.CN_TZ).isoformat(timespec="seconds"),
    }
    return await store.update_account(PLATFORM_ID, account["uid"], {"quota": quota})


def _norm_expire(raw) -> str:
    """到期多形态归一 "YYYY-MM-DD HH:MM:SS"：epoch 秒/毫秒、既有日期串直通。"""
    if raw is None or raw == "":
        return ""
    # 数字或数字串 → epoch（<1e11 秒 ×1000；ms 直接用）
    try:
        n = float(raw)
        if n > 0:
            ts = n / 1000 if n >= 1e11 else n
            return datetime.fromtimestamp(ts, store.CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        pass
    text = str(raw).strip()
    # RFC3339 / ISO → 统一格式
    if "T" in text:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))\
                .astimezone(store.CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return text


def _parse_dt(text: str):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=store.CN_TZ)
        except ValueError:
            continue
    return None


def _is_far_future(date_text: str) -> bool:
    """到期占位值判定：距今超 730 天视为长期有效（9999/2049 等不显示）。"""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(date_text, fmt).replace(tzinfo=store.CN_TZ)
            return (dt - datetime.now(store.CN_TZ)).days > 730
        except ValueError:
            continue
    return False


async def _billing_raw_post(account: dict, path: str, json_body: dict,
                            headers: dict[str, str] | None = None) -> dict:
    """返回原始信封（不 parse_envelope）：调用方自行判 code 或挖数组。

    headers 缺省走桌面端 billing 指纹；配额回退路径传 _saas_headers 覆盖。
    """
    if headers is None:
        headers = billing_headers(
            access_token=account.get("access_token") or "",
            uid=account.get("uid") or "",
            domain=account.get("domain") or "",
            enterprise_id=account.get("enterprise_id") or "",
        )
    url = f"{settings.wb_api_base}{path}"
    try:
        return await _client.request_json("POST", url, headers=headers,
                                               json_body=json_body)
    except PlatformUpstreamError as exc:
        if exc.status in (401, 403):
            raise PlatformAuthError(f"登录态失效: {exc}") from exc
        raise


def _item_num(item: dict, *names: str) -> float:
    """多名字段宽松取数（Precise 字符串字段优先，plain 数值兜底，9router num() 语义）。"""
    for name in names:
        raw = item.get(name)
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return 0.0
