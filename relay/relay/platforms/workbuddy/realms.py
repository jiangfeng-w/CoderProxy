"""WorkBuddy 域 profile：国内版（workbuddy）与国际版（workbuddy-global）差异单点。

「WorkBuddy双域账号支持」§2 定稿：双域按**两个平台实例**完整区分，差异全部收敛
在本模块的数据类里，其余模块只读 profile。国际版实现沿用 CN 版同源参考谱系
（workbuddy2api-panel 主线）。

- 静态差异（origin / Accept-Language / 路径候选 / 能力位）为字段；
- base 类取值经 property **读时取 settings**（env 覆盖与测试 monkeypatch 都生效）；
- CN 取值 = 现状字面量（逐字零回归）；国际版取值 = panel global 主档
  （`headers.go` defaultWorkBuddyUAFor / `client.go` billingMeterPaths /
  `global_models.go` globalModelsProbePaths / `login.go` loginEndpoints）+
  2026-10-14 国际版小号只读探针实测（`docs/spec/WorkBuddy双域账号支持/`）
  ——探针已证：/v3/config 200、/v2/enterprises 200（/console 500）、
  billing 无 /v2 前缀路径 200、chat 首条非 system → 400/11128。
"""
from __future__ import annotations

from dataclasses import dataclass

from relay.config import settings

# 国际版账号体系域根（CodeBuddy/WorkBuddy 国际版共用一套账号，两族都接受）
_GLOBAL_DOMAIN_ROOTS = ("workbuddy.ai", "codebuddy.ai")


def is_global_domain(domain: str) -> bool:
    """domain 是否属于国际版账号体系（workbuddy.ai / codebuddy.ai 裸域或任意子域）。

    空串 → False（无法判定，不作为国际版证据）。
    """
    text = str(domain or "").strip().lower()
    if not text:
        return False
    if text.startswith("."):
        text = text[1:]
    return any(text == root or text.endswith("." + root) for root in _GLOBAL_DOMAIN_ROOTS)


@dataclass(frozen=True)
class WbRealm:
    """WorkBuddy 域（平台）profile：所有双域差异的单点。"""

    platform_id: str
    display_name: str
    model_prefix: str
    is_global: bool
    ua_platform: str  # UA 第二段品牌（CN WorkBuddy / 国际版 WorkBuddy AI）
    supports_checkin: bool
    origin: str
    accept_language: str
    supports_quota: bool = True
    oauth_platform: str = "workbuddy"  # auth/state?platform=（探针实测两值均 200，沿用现状）
    chat_domain: str = ""  # 非空 → chat 恒发 X-Domain（国际版）；空 = 按账号 domain（CN）
    ensure_system_first: bool = False  # chat 首条强制 system（国际版实测 400/11128）
    quota_fallback_style: str = "saas"  # get-user-resource 回退头风格（CN saas / 国际版 desktop）
    # ── 官方客户端集成（ide_switch / imports 本机导入；2026-10-15 二期增补） ──
    auth_file_name: str = "workbuddy-desktop.info"  # %LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\
    client_image_name: str = "WorkBuddy.exe"  # tasklist/taskkill 映像名
    exe_env_override: str = "RELAY_WB_IDE_EXE"  # 客户端 exe 显式覆盖 env 名
    exe_candidates: tuple[str, ...] = (
        r"%LOCALAPPDATA%\Programs\WorkBuddy\WorkBuddy.exe",
        r"%PROGRAMFILES%\WorkBuddy\WorkBuddy.exe",
        r"%PROGRAMFILES(X86)%\WorkBuddy\WorkBuddy.exe",
    )
    # ── 导出集成（exports；2026-10-15 二期增补） ──
    cockpit_id_prefix: str = "workbuddy"  # cockpit id = <prefix>_md5(uid)
    nine_router_provider: str = "codebuddy-cn"  # 9router providerConnections.provider
    export_filename_base: str = "workbuddy_accounts"  # cockpit 导出文件名基

    # ─────────────── base 类：读时取 settings（env / monkeypatch 生效） ───────────────

    @property
    def _primary_base(self) -> str:
        return settings.wb_global_base if self.is_global else settings.wb_api_base

    @property
    def chat_base(self) -> str:
        return self._primary_base

    @property
    def billing_base(self) -> str:
        return self._primary_base

    @property
    def login_base(self) -> str:
        return self._primary_base

    @property
    def catalog_base(self) -> str:
        return self._primary_base

    @property
    def refresh_base(self) -> str:
        # CN 现状走 web 域（oauth.py 原 settings.wb_web_base）；国际版走全球 base
        # （panel RefreshToken → chatBase）。
        return settings.wb_global_base if self.is_global else settings.wb_web_base

    # ─────────────── 路径候选 / 头族差异 ───────────────

    @property
    def meter_paths(self) -> tuple[str, ...]:
        """billing/meter 域路径前缀候选序（panel billingMeterPaths / checkinMeterPaths）。

        CN：[带 /v2]（现状逐字）；国际版：[无 /v2 优先, 带 /v2 回退]。
        """
        if self.is_global:
            return ("/billing/meter", "/v2/billing/meter")
        return ("/v2/billing/meter",)

    @property
    def enterprise_model_paths(self) -> tuple[str, ...]:
        """企业模型目录端点候选序（panel globalModelsProbePaths）。

        CN：/console 单候选（现状）；国际版：/v2 家族优先（探针实测 200）→ /console 回退
        （探针实测 500）。
        """
        if self.is_global:
            return ("/v2/enterprises/personal/models",
                    "/console/enterprises/personal/models")
        return ("/console/enterprises/personal/models",)

    @property
    def quota_candidates(self) -> tuple[tuple[str, str], ...]:
        """配额端点候选序：(路径, 头风格)。

        CN（现状逐字）：summary + web 套餐页头 → get-user-resource + SaaS 回退头；
        国际版（panel billingMeterPaths + 探针实测）：无 /v2 get-user-resource +
        桌面头 → /v2 回退（summary 在国际版 base 404，不进候选）。
        """
        if self.is_global:
            return (("/billing/meter/get-user-resource", "desktop"),
                    ("/v2/billing/meter/get-user-resource", "desktop"))
        return (("/v2/billing/meter/get-user-resource-summary", "web_summary"),
                ("/v2/billing/meter/get-user-resource", "saas"))

    @property
    def inject_enterprise_headers(self) -> bool:
        """billing 域是否注入 X-Enterprise-Id / X-Tenant-Id（国际版无企业概念）。"""
        return not self.is_global

    def allows_domain(self, domain: str) -> bool:
        """账号归属域校验（§6 防混池）：空域放行；否则须与本 platform 的域族一致。"""
        text = str(domain or "").strip()
        if not text:
            return True
        return is_global_domain(text) == self.is_global


REALM_CN = WbRealm(
    platform_id="workbuddy",
    display_name="WorkBuddy CN",
    model_prefix="wbcn",  # 2026-10-14 拍板改短（旧 WorkBuddy/ 读兼容归一，见 model_ref）
    is_global=False,
    ua_platform="WorkBuddy",
    supports_checkin=True,
    origin="https://www.codebuddy.cn",
    accept_language="zh-CN",
)

REALM_GLOBAL = WbRealm(
    platform_id="workbuddy-global",
    display_name="WorkBuddy",
    model_prefix="wbg",
    is_global=True,
    ua_platform="WorkBuddy AI",
    supports_checkin=False,  # 定稿：国际版无签到（panel D4 门控同证）
    origin="https://www.workbuddy.ai",
    accept_language="en-US",
    chat_domain="www.workbuddy.ai",
    ensure_system_first=True,
    quota_fallback_style="desktop",
    # 国际版官方客户端（WorkBuddy AI，2026-10-15 本机实测：
    #   - 安装于 D:\Programs\WorkBuddyAI\WorkBuddyAI.exe（非默认盘，注册表兜底发现）
    #   - 登录态文件 workbuddy-desktop-ai.info 与 CN 同目录并列、结构同构
    #     （account/auth/accounts/allAccounts 四根 + 同字段级 $wbEncrypted）
    #   - exe 内含同名 binding electron_browser_workbuddy_storage（密钥派生同款）
    auth_file_name="workbuddy-desktop-ai.info",
    client_image_name="WorkBuddyAI.exe",
    exe_env_override="RELAY_WB_AI_IDE_EXE",
    exe_candidates=(
        r"%LOCALAPPDATA%\Programs\WorkBuddyAI\WorkBuddyAI.exe",
        r"%PROGRAMFILES%\WorkBuddyAI\WorkBuddyAI.exe",
        r"%PROGRAMFILES(X86)%\WorkBuddyAI\WorkBuddyAI.exe",
        r"D:\Programs\WorkBuddyAI\WorkBuddyAI.exe",
    ),
    # cockpit 国际版导出（CodebuddyAccount 形状；id=codebuddy_+md5(小写 uid)，
    # 与用户实物 codebuddy_461e541f… 逐字吻合）+ 9router provider codebuddy-intl
    cockpit_id_prefix="codebuddy",
    nine_router_provider="codebuddy-intl",
    export_filename_base="codebuddy_accounts",
)

ALL_REALMS: tuple[WbRealm, WbRealm] = (REALM_CN, REALM_GLOBAL)

_REALMS_BY_PLATFORM: dict[str, WbRealm] = {r.platform_id: r for r in ALL_REALMS}


def realm_for(platform_id: str) -> WbRealm | None:
    """platform_id → profile（未注册的 platform_id 返回 None）。"""
    return _REALMS_BY_PLATFORM.get(str(platform_id or ""))
