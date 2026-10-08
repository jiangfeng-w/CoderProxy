"""relay 配置：环境变量优先，缺省用默认值。

- 服务：RELAY_HOST / RELAY_PORT / RELAY_API_KEY
- ta3：TA3_API_BASE / TA3_USER_AGENT / TA3_STREAM_IDLE_TIMEOUT /
       TA3_KIMI_THINKING_EFFORT / TA3_THINKING_WATCHDOG
- 空响应：RELAY_EMPTY_STREAM_RETRIES（上游 200+空流自动重试次数，0 关闭）
- 工具：TOOL_MODE（hybrid|strict|passthrough，M3 生效，M2 仅占位）
- 数据：RELAY_DATA_DIR（token/模型/config 落盘目录）
- 日志：RELAY_LOG_MAX_ROWS（logs 保留行数上限，默认 100000）

RELAY_API_KEY 未显式设置时由 relay.storage 首次启动生成并持久化
（settings 仅暴露环境变量或空串，真正的 key 经 storage.ensure_api_key 回填）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_RELAY_ROOT = Path(__file__).resolve().parent.parent  # relay/ 项目根（含 app/ 与 relay/）


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    # 服务
    relay_host: str = "127.0.0.1"
    relay_port: int = 3601
    relay_api_key: str = ""
    # ta3
    ta3_api_base: str = "https://lc.yinhaiyun.com/newcoder"
    # 默认伪装 Electron 同族 UA（对齐聊天请求 ta3.py 的 _DEFAULT_TA3_UA），
    # 使目录同步 / OAuth token / 登录等全部出站请求不裸露 python-httpx 指纹；
    # 可经 TA3_USER_AGENT 覆盖
    ta3_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "ta3-new-coder-desktop/1.0.0 Chrome/126.0.0.0 Electron/31.0.0 Safari/537.36"
    )
    ta3_stream_idle_timeout: float = 300.0
    ta3_kimi_thinking_effort: str = "low"
    ta3_thinking_watchdog: float = 240.0
    # 上游空响应自动重试（BUG-001：网关间歇性 200 后立即干净关流，0 token 空流）。
    # relay 内对空响应重发上游请求（独立预算，不与 401 刷新共用）；耗尽仍空则抛错，
    # 由 routes 记 chat_error——不再向 agent 发假成功的空流/空 JSON。
    empty_stream_retries: int = 3
    # WorkBuddy（CodeBuddy CN）平台插件（/v1/platforms/workbuddy/*）。
    # API 域双口径：契约参考（workbuddy-switch）用 copilot.tencent.com，
    # cockpit-tools 实测全走 www.codebuddy.cn——两个配置项兜底，小号冒烟后定默认。
    wb_api_base: str = "https://copilot.tencent.com"
    wb_web_base: str = "https://www.codebuddy.cn"
    # 出站 UA：对齐官方桌面端 RestOperations 层三段式（workbuddy-cockpit headers.go：
    # WorkBuddy/<clientVer> WorkBuddy/<clientVer> CLI/<cliVer>）；billing 白名单类
    # 接口官方用单段 WorkBuddy/<clientVer>（client.py 按 domain 选择）
    wb_user_agent: str = "WorkBuddy/5.5.4 WorkBuddy/5.5.4 CLI/2.137.1"
    wb_client_version: str = "5.5.4"
    # 直连：M1 结论要求绕系统代理，trust_env=False 语义由 relay/__init__.py 设 NO_PROXY 实现；
    # TRUST_ENV_PROXY=true 时保留系统代理行为（调试用）
    trust_env_proxy: bool = False
    # 工具模式（M3 生效；M2 阶段沿用 vendored ta3.py 内置 disguise/restore）。
    # D1 后：env TOOL_MODE 仅作磁盘未保存时的兜底，启动装载以磁盘持久化值为准。
    tool_mode: str = "hybrid"
    # 数据目录
    data_dir: str = field(default_factory=lambda: str(_RELAY_ROOT / "data"))
    # 日志保留行数上限（超出滚动删除最旧，防磁盘膨胀）
    relay_log_max_rows: int = 100000
    # 工具指纹采集（M11：多 agent 工具名发现）。入站请求的真实工具声明
    # 自动归一化后落库 tool_inventory，供后续语义映射。RELAY_TOOL_INVENTORY=false 关闭。
    tool_inventory_enabled: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        s.relay_host = _env("RELAY_HOST", s.relay_host)
        try:
            s.relay_port = int(_env("RELAY_PORT", str(s.relay_port)))
        except ValueError:
            pass
        s.relay_api_key = _env("RELAY_API_KEY", s.relay_api_key)
        s.ta3_api_base = _env("TA3_API_BASE", s.ta3_api_base)
        s.wb_api_base = _env("WB_API_BASE", s.wb_api_base)
        s.wb_web_base = _env("WB_WEB_BASE", s.wb_web_base)
        s.wb_user_agent = _env("WB_USER_AGENT", s.wb_user_agent)
        s.wb_client_version = _env("WB_CLIENT_VERSION", s.wb_client_version)
        s.ta3_user_agent = _env("TA3_USER_AGENT", s.ta3_user_agent)
        s.trust_env_proxy = _env("TRUST_ENV_PROXY", "").lower() in ("1", "true", "yes")
        s.tool_mode = _env("TOOL_MODE", s.tool_mode)
        s.data_dir = _env("RELAY_DATA_DIR", s.data_dir)
        s.tool_inventory_enabled = _env(
            "RELAY_TOOL_INVENTORY", "").lower() not in ("0", "false", "no")
        try:
            s.ta3_stream_idle_timeout = float(_env("TA3_STREAM_IDLE_TIMEOUT", str(s.ta3_stream_idle_timeout)))
        except ValueError:
            pass
        try:
            s.ta3_kimi_thinking_effort = _env("TA3_KIMI_THINKING_EFFORT", s.ta3_kimi_thinking_effort)
        except ValueError:
            pass
        try:
            s.ta3_thinking_watchdog = float(_env("TA3_THINKING_WATCHDOG", str(s.ta3_thinking_watchdog)))
        except ValueError:
            pass
        try:
            s.empty_stream_retries = max(0, int(_env(
                "RELAY_EMPTY_STREAM_RETRIES", str(s.empty_stream_retries))))
        except ValueError:
            pass
        return s


settings = Settings.from_env()
