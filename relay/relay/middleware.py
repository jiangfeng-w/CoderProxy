"""静态 RELAY_API_KEY 鉴权（Bearer + x-api-key 兼容）。

agent 在自定义供应商里填的 api_key = 中转自己的静态 token（RELAY_API_KEY），
真正的牛码 llm- key / oauth token 只在 relay 内部持有，永不下发。

鉴权头兼容（多协议入站：对标 sub2api `api_key_auth.go` 的三头统一）：
- OpenAI Chat Completions / Responses：`Authorization: Bearer <key>`
- Anthropic Messages：`x-api-key: <key>`（Claude Code CLI 等原生客户端）
- `x-goog-api-key: <key>`（Gemini 系客户端顺手兼容）
任一头匹配即通过；Bearer 优先级最高。
"""
from __future__ import annotations

from fastapi import HTTPException, Request

from relay.storage import relay_api_key


def _extract_key(request: Request) -> str | None:
    """按 Bearer → x-api-key → x-goog-api-key 顺序提取入站 key。"""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        token = auth[len("Bearer "):].strip()
        if token:
            return token
    for header in ("x-api-key", "x-goog-api-key"):
        token = (request.headers.get(header) or "").strip()
        if token:
            return token
    return None


def require_api_key(request: Request) -> None:
    """校验入站 API key（RELAY_API_KEY），任一头匹配即通过，否则 401。"""
    token = _extract_key(request)
    if token is None:
        raise HTTPException(
            status_code=401,
            detail="缺少 Authorization: Bearer <api_key> 或 x-api-key")
    if token != relay_api_key():
        raise HTTPException(status_code=401, detail="Invalid API key")
