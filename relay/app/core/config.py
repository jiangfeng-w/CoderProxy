"""relay 适配层：把 relay.config.settings 暴露为 vendored ta3.py 期望的 app.core.config.settings。

vendored ta3.py 顶层 `from app.core.config import settings`，读取
ta3_user_agent / ta3_stream_idle_timeout / ta3_kimi_thinking_effort /
ta3_thinking_watchdog。本模块仅做转发，不改 vendored 代码。
"""
from relay.config import settings as settings
