"""登录 / 会话 / 目录同步流程封装（供 routes.py 调用）。

vendored oauth.py 的签名带 `db: AsyncSession`（SQLAlchemy 会话），relay 无 DB：
- 传入 no-op db 对象兜底 oauth.py 内部的 `db.commit()` 调用，不改 vendored 代码；
- 真实持久化走 relay.storage（本地 JSON 文件）。
"""
from __future__ import annotations

from app.auth.ta3 import oauth as ta3_oauth
from app.auth.ta3 import session as ta3_session

from relay import catalog_sync, storage
from relay.config import settings

PROVIDER_ID = 1


class _NoopDB:
    """兼容 vendored oauth.py 内 db.commit()/flush() 的 no-op 对象。"""

    async def commit(self):  # noqa: D102
        pass

    async def flush(self):  # noqa: D102
        pass

    async def execute(self, *args, **kwargs):  # noqa: D102
        return None

    async def add(self, *args, **kwargs):  # noqa: D102
        pass

    async def delete(self, *args, **kwargs):  # noqa: D102
        pass


noop_db = _NoopDB()


async def start_login() -> dict:
    """先试银海通 IM 静默登录，失败降级浏览器 PKCE。返回 {status, ...}。"""
    return await ta3_oauth.start_login(noop_db, PROVIDER_ID, settings.ta3_api_base)


async def login_status() -> dict:
    """查询登录状态：优先 in-flight 任务结果，其次本地登录态。

    上游（vendored get_login_status）在「无会话」时也返回 status=pending(错误=未登录)，
    与「浏览器授权中」(pending) 混淆。前端需区分「未登录」「登录中」，这里归一化：
    pending 且错误为「未登录」→ not_logged_in；其余原样返回。
    """
    result = await ta3_oauth.get_login_status(noop_db, PROVIDER_ID)
    if result.get("status") == "pending" and result.get("error") == "未登录":
        return {"status": "not_logged_in"}
    return result


async def cancel_login() -> None:
    await ta3_oauth.cancel_login(noop_db, PROVIDER_ID)


async def logout() -> None:
    """退出：清登录态 + 清本地模型目录。"""
    await ta3_session.clear_auth(PROVIDER_ID)
    await storage.save_models([])


async def sync_models() -> list[dict]:
    return await catalog_sync.sync_models()
