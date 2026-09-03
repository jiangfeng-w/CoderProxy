"""静态 RELAY_API_KEY Bearer 鉴权。

agent 在自定义供应商里填的 api_key = 中转自己的静态 token（RELAY_API_KEY），
真正的牛码 llm- key / oauth token 只在 relay 内部持有，永不下发。
"""
from __future__ import annotations

from fastapi import HTTPException, Request

from relay.storage import relay_api_key


def require_api_key(request: Request) -> None:
    """校验 Authorization: Bearer <RELAY_API_KEY>，未匹配 401。"""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少 Authorization: Bearer <api_key>")
    token = auth[len("Bearer "):].strip()
    if token != relay_api_key():
        raise HTTPException(status_code=401, detail="Invalid API key")
