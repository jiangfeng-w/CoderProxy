"""WorkBuddy 签到与配额单测：MockTransport 覆盖签到幂等/状态回退/配额多形态/401 映射。

不触网；billing._client 注入 MockTransport，存储落 tmp。
"""
import httpx
import pytest

from relay.config import settings
from relay.platforms import store
from relay.platforms.base import PlatformAuthError
from relay.platforms.workbuddy import billing

API = "https://copilot.tencent.com"
METER = f"{API}/v2/billing/meter"


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))


def _mock_client(routes: dict[str, list[httpx.Response] | httpx.Response]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        for prefix, resp in routes.items():
            if str(request.url).startswith(prefix):
                if isinstance(resp, list):
                    item, *rest = resp
                    if rest:
                        resp[:] = rest  # type: ignore[arg-type]
                    return item
                return resp
        raise AssertionError(f"unexpected url: {request.url}")

    billing._client = billing.WbClient(transport=httpx.MockTransport(handler))


async def _seed_account(uid="u_b1", **extra):
    await store.upsert_account("workbuddy", {"uid": uid, "access_token": "at",
                                             "refresh_token": "rt", **extra})
    account = await store.find_account("workbuddy", uid)
    assert account is not None
    return account


async def test_perform_checkin_success():
    """code 0 签到成功：checkin 缓存更新（今天/已签/streak），credit 透出。"""
    _mock_client({
        f"{METER}/daily-checkin": httpx.Response(200, json={
            "code": 0, "data": {"streak_days": 3, "credit": 20}}),
    })
    account = await _seed_account(checkin={"last_checkin_date": "2026-10-01",
                                           "streak_days": 1, "today_checked_in": False})
    result, updated = await billing.perform_checkin(account)
    assert result.success is True
    assert result.streak_days == 3
    assert result.credit == 20
    assert updated["checkin"]["today_checked_in"] is True
    assert updated["checkin"]["last_checkin_date"] == store.today_cn()
    assert updated["checkin"]["streak_days"] == 3


@pytest.mark.parametrize("payload", [
    {"code": 10001, "msg": "今日已签到"},
    {"code": 14001, "msg": "重复签到"},
    {"code": 1, "msg": "already checked in today"},
])
async def test_perform_checkin_idempotent(payload):
    """已签到幂等判定：code 10001/14001 或 msg 含已签到/already → 按成功返回。"""
    routes: dict = {f"{METER}/daily-checkin": httpx.Response(200, json=payload)}
    if payload["code"] in billing.CHECKED_IN_CODES or "already" in payload["msg"]:
        # 补拉状态走 activity-status
        routes[f"{METER}/checkin-activity-status"] = httpx.Response(200, json={
            "code": 0, "data": {"todayCheckedIn": 1, "streakDays": 7, "active": "true"}})
    _mock_client(routes)
    account = await _seed_account()
    result, updated = await billing.perform_checkin(account)
    assert result.success is True
    assert result.message == "今日已签到"
    if payload["code"] in billing.CHECKED_IN_CODES or "already" in payload["msg"]:
        # 宽松解析链路：0/"true" → True，camel → snake
        assert updated["checkin"]["today_checked_in"] is True
        assert updated["checkin"]["streak_days"] == 7


async def test_perform_checkin_business_failure_keeps_account():
    """业务失败（非幂等码）：success=False 且账号/缓存不被改写。"""
    _mock_client({
        f"{METER}/daily-checkin": httpx.Response(200, json={
            "code": 500, "msg": "活动未开始"}),
    })
    account = await _seed_account(checkin={"last_checkin_date": "2026-10-01",
                                           "streak_days": 2, "today_checked_in": False})
    result, updated = await billing.perform_checkin(account)
    assert result.success is False
    assert "活动未开始" in result.message
    assert updated == account


async def test_checkin_status_fallback_and_loose_parse():
    """activity 接口失败回退 legacy；data 宽松解析（camel/0/"true"）。"""
    _mock_client({
        f"{METER}/checkin-activity-status": httpx.Response(500, json={"msg": "gone"}),
        f"{METER}/checkin-status": httpx.Response(200, json={
            "code": 0, "data": {"todayCheckedIn": 1, "active": "true",
                                "streakDays": 5, "dailyCredit": 10}}),
    })
    account = await _seed_account()
    status = await billing.checkin_status_payload(account)
    assert status == {"today_checked_in": True, "active": True,
                      "streak_days": 5, "daily_credit": 10}

    updated = await billing.refresh_checkin_status(account)
    assert updated["checkin"]["last_checkin_date"] == store.today_cn()
    assert updated["checkin"]["streak_days"] == 5


async def test_fetch_quota_summary_primary_then_legacy_fallback():
    """两级编排：summary（空 body + web 头）为主路径；summary 空结果 → 回退旧接口。

    身份一致性优先（workbuddy-switch 配对：platform=workbuddy 登录 → 官网套餐页
    端点 summary）；9router SaaS 组合仅作兜底。
    """
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if str(request.url).endswith("get-user-resource-summary"):
            # summary 空（免费号 summary 偶发空形态）
            return httpx.Response(200, json={"code": 0, "data": {}})
        # 旧接口回包（腾讯嵌套）
        return httpx.Response(200, json={
            "code": 0,
            "data": {"Response": {"Data": {"Accounts": [
                {"CycleCapacitySizePrecise": "500", "CycleCapacityUsedPrecise": "6.54",
                 "DeductionEndTime": "9999-12-31"},
            ]}}},
        })

    billing._client = billing.WbClient(transport=httpx.MockTransport(handler))
    account = await _seed_account()
    updated = await billing.fetch_quota(account)
    assert len(calls) == 2
    assert str(calls[0].url).endswith("get-user-resource-summary")
    assert str(calls[1].url).endswith("/get-user-resource")
    # 主路径头是桌面端 web 形态（身份一致），非 SaaS
    assert calls[0].headers["X-Client-Platform"] == "web"
    assert calls[0].headers["User-Agent"] == f"WorkBuddy/{settings.wb_client_version}"
    assert "X-Product" not in calls[0].headers
    # 回退路径才是 SaaS
    assert calls[1].headers["X-Product"] == "SaaS"
    assert updated["quota"]["total"] == 500


async def test_fetch_quota_summary_primary_success():
    """summary 主路径有数据：不触发回退，Cycle/Capacity 双口径聚合。"""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={
            "code": 0,
            "data": {"resources": [
                {"CycleCapacitySizePrecise": "500", "CycleCapacityUsedPrecise": "120",
                 "DeductionEndTime": "9999-12-31"},
                {"CapacitySizePrecise": "30", "CapacityUsedPrecise": "10",
                 "DeductionEndTime": "2026-12-31"},
            ]},
        })

    billing._client = billing.WbClient(transport=httpx.MockTransport(handler))
    account = await _seed_account()
    updated = await billing.fetch_quota(account)
    assert len(calls) == 1  # summary 成功即止
    assert updated["quota"]["total"] == 530
    assert updated["quota"]["used"] == 130
    assert updated["quota"]["expire_at"] == "2026-12-31"


async def test_fetch_quota_packages_detail_e2e_shape():
    """逐包明细（e2e 实测形态 2026-10-08）：套餐包 Cycle 500 + 两个奖励包
    （1500/100，epoch 毫秒到期）→ packages 逐包归一，聚合 expire 取最近未来。"""
    summary_body = {
        "code": 0,
        "data": {"resources": [
            # 套餐基础积分：Cycle 包，到期 = 下次周期更新（epoch 毫秒 → 2026-10-31）
            {"PackageName": "体验版", "CycleCapacitySizePrecise": "500",
             "CycleCapacityUsedPrecise": "0",
             "CycleEndTime": 1793414399000, "DeductionEndTime": 2697974400000},
            # 平台奖励：注册奖励 1500，到期 2026-11-07（epoch 毫秒）
            {"PackageName": "注册积分奖励", "CapacitySizePrecise": "1500",
             "CapacityUsedPrecise": "0", "DeductionEndTime": 1794093063000},
            # Buddy 加油站签到 100
            {"PackageName": "Buddy 加油站签到", "CapacitySizePrecise": "100",
             "CapacityUsedPrecise": "0", "DeductionEndTime": 1794093172000},
        ]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=summary_body)

    billing._client = billing.WbClient(transport=httpx.MockTransport(handler))
    account = await _seed_account()
    updated = await billing.fetch_quota(account)
    # 聚合：500 + 1500 + 100
    assert updated["quota"]["total"] == 2100
    assert updated["quota"]["used"] == 0
    pkgs = updated["quota"]["packages"]
    assert len(pkgs) == 3
    assert pkgs[0]["name"] == "体验版" and pkgs[0]["total"] == 500 and pkgs[0]["cycle"] is True
    assert pkgs[1]["name"] == "注册积分奖励" and pkgs[1]["total"] == 1500
    # epoch 毫秒到期归一为 CN 时间字符串（聚合取最近未来：套餐周期 10-31 早于奖励 11-07）
    assert updated["quota"]["expire_at"].startswith("2026-10-31")
    for p in pkgs:
        assert "T" not in p["expire_at"] and not p["expire_at"].isdigit()


async def test_fetch_quota_tencent_nested_form():
    """腾讯嵌套信封（9router 实测形态）：data.Response.Data.Accounts[]，
    基础包 Cycle* 字段 + 赠送包 Capacity 字段分别取数求和；Precise 字符串优先。"""
    quota_body = {
        "code": 0,
        "data": {
            "Response": {
                "Data": {
                    "Accounts": [
                        # 基础体验包（月续）：Cycle 字段，Precise 字符串优先
                        {"CycleCapacitySizePrecise": "500", "CycleCapacityUsedPrecise": "6.54",
                         "CycleEndTime": "2026-11-01 00:00:00", "DeductionEndTime": "9999-12-31"},
                        # 活动赠送包（一次性）：裸 Capacity 字段
                        {"CapacitySize": 50, "CapacityUsed": 20,
                         "CycleEndTime": "2026-10-31", "DeductionEndTime": "2026-10-31"},
                    ]
                }
            }
        },
    }
    _mock_client({
        f"{METER}/get-user-resource": httpx.Response(200, json=quota_body),
        # summary 404/空 → 走回退（本测试聚焦旧接口解析）
        f"{METER}/get-user-resource-summary": httpx.Response(200, json={"code": 0, "data": {}}),
    })
    account = await _seed_account()
    updated = await billing.fetch_quota(account)
    assert updated["quota"]["total"] == 550
    assert abs(updated["quota"]["used"] - 26.54) < 1e-9
    assert abs(updated["quota"]["remaining"] - (550 - 26.54)) < 1e-9
    # expire_at 取最大且过滤长期占位值（基础包 9999-12-31 被滤，
    # 剩赠送包真实到期 2026-10-31——对齐 workbuddy-switch FAR_FUTURE 语义）
    assert updated["quota"]["expire_at"] == "2026-10-31"


async def test_fetch_quota_empty_accounts_free_plan():
    """免费号：两路径都 code 0 但空 → 全 0 落库（fetched_at 有值=已刷新，不抛错）。"""
    _mock_client({
        f"{METER}/get-user-resource-summary": httpx.Response(
            200, json={"code": 0, "data": {}}),
        f"{METER}/get-user-resource": httpx.Response(
            200, json={"code": 0, "data": {"Response": {"Data": {"Accounts": []}}}}),
    })
    account = await _seed_account()
    updated = await billing.fetch_quota(account)
    assert updated["quota"]["total"] == 0
    assert updated["quota"]["fetched_at"]


async def test_fetch_quota_saas_headers_sent():
    """回退路径带 SaaS/CLI 伪装头 + 空 body（9router 实测口径）。"""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["ua"] = request.headers.get("User-Agent")
        captured["product"] = request.headers.get("X-Product")
        captured["ide"] = request.headers.get("X-IDE-Type")
        captured["body"] = request.read().decode("utf-8")
        return httpx.Response(200, json={"code": 0, "data": {}})

    billing._client = billing.WbClient(transport=httpx.MockTransport(handler))
    account = await _seed_account()
    await billing.fetch_quota(account)
    assert captured["ua"] == "CLI/2.108.1 CodeBuddy/2.108.1"
    assert captured["product"] == "SaaS"
    assert captured["ide"] == "CLI"
    assert captured["body"] == "{}"


async def test_fetch_quota_accounts_form_and_empty():
    """配额多形态：Response/Data/Accounts 兜底形态；无数组 → 全 0。"""
    _mock_client({
        f"{METER}/get-user-resource": [
            httpx.Response(200, json={"code": 0, "Response": {"Data": {"Accounts": [
                {"CapacitySizePrecise": "30.5", "CapacityUsedPrecise": "20.5"}]}}}),
            httpx.Response(200, json={"code": 0, "data": {}}),
        ]})
    account = await _seed_account()
    updated = await billing.fetch_quota(account)
    assert updated["quota"]["total"] == 30.5
    assert updated["quota"]["used"] == 20.5

    # 第二次：无数组 → 全 0
    updated2 = await billing.fetch_quota(await store.find_account("workbuddy", "u_b1"))
    assert updated2["quota"]["total"] == 0


async def test_billing_401_maps_login_required():
    """上游 401 → PlatformAuthError(login_required)（routes 映射 401 引导重登）。"""
    _mock_client({
        f"{METER}/daily-checkin": httpx.Response(401, json={"code": 401, "msg": "expired"}),
    })
    account = await _seed_account()
    with pytest.raises(PlatformAuthError) as ei:
        await billing.perform_checkin(account)
    assert ei.value.kind == "login_required"
