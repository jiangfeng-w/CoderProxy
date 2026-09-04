"""relay 配置：环境变量优先，缺省用默认值。

- 服务：RELAY_HOST / RELAY_PORT / RELAY_API_KEY
- ta3：TA3_API_BASE / TA3_USER_AGENT / TA3_STREAM_IDLE_TIMEOUT /
       TA3_KIMI_THINKING_EFFORT / TA3_THINKING_WATCHDOG
- 工具：TOOL_MODE（hybrid|strict|passthrough，M3 生效，M2 仅占位）
- 数据：RELAY_DATA_DIR（token/模型/config 落盘目录）

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
    ta3_user_agent: str = ""
    ta3_stream_idle_timeout: float = 180.0
    ta3_kimi_thinking_effort: str = "low"
    ta3_thinking_watchdog: float = 240.0
    # 直连：M1 结论要求绕系统代理，trust_env=False 语义由 relay/__init__.py 设 NO_PROXY 实现；
    # TRUST_ENV_PROXY=true 时保留系统代理行为（调试用）
    trust_env_proxy: bool = False
    # 工具模式（M3 生效；M2 阶段沿用 vendored ta3.py 内置 disguise/restore）
    tool_mode: str = "hybrid"
    # 数据目录
    data_dir: str = field(default_factory=lambda: str(_RELAY_ROOT / "data"))

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
        s.ta3_user_agent = _env("TA3_USER_AGENT", s.ta3_user_agent)
        s.trust_env_proxy = _env("TRUST_ENV_PROXY", "").lower() in ("1", "true", "yes")
        s.tool_mode = _env("TOOL_MODE", s.tool_mode)
        s.data_dir = _env("RELAY_DATA_DIR", s.data_dir)
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
        return s


settings = Settings.from_env()
