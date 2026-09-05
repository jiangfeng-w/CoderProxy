"""目录同步：拉取 ta3 模型目录并落本地存储。

复用 vendored app/auth/ta3/catalog.py 的网络与解析逻辑：
- fetch_catalog_raw：组织 + 各组织 assistants（端点顺序已按 M1 三坑修正）
- _parse_config_models：assistant 配置 → 模型条目（name/apiKey/apiBase/anthropic/...）
- Ta3Unauthorized：401 时 refresh 一次后重试

组织选择对齐上游 sync_ta3_models：取第一个有 assistants 的组织
（多组织配置可能不一致，不做跨组织混合合并）。

只写 relay.storage（本地 JSON），不碰 DB（上游 Model 表逻辑不适用）。
"""
from __future__ import annotations

import logging

from app.auth.ta3 import catalog as ta3_catalog
from app.auth.ta3 import session as ta3_session

from relay import storage
from relay.config import settings

logger = logging.getLogger(__name__)

PROVIDER_ID = 1


async def sync_models() -> list[dict]:
    """同步目录 → storage.models，返回模型条目列表（空 = 未登录或目录无模型）。"""
    api_base = settings.ta3_api_base
    token = await ta3_session.ensure_token(provider_id=PROVIDER_ID, api_base=api_base)
    try:
        raw = await ta3_catalog.fetch_catalog_raw(api_base, token)
    except ta3_catalog.Ta3Unauthorized:
        token = await ta3_session.ensure_token(provider_id=PROVIDER_ID, api_base=api_base)
        raw = await ta3_catalog.fetch_catalog_raw(api_base, token)

    orgs = raw["organizations"]
    assistants_by_org = raw["assistants_by_org"]

    # 组织选择：第一个有 assistants 的组织（对齐上游默认行为）
    selected_org_id = ""
    selected_assistants: list[dict] = []
    for org in orgs:
        org_id = str(org.get("id") or org.get("organizationId") or "").strip()
        assistants = assistants_by_org.get(org_id) or []
        if assistants:
            selected_org_id = org_id
            selected_assistants = assistants
            break
    if not selected_assistants:
        logger.warning("[relay] 目录未解析出任何模型（组织=%d，assistants=0）", len(orgs))
        await storage.save_models([])
        return []

    org_name = str(next(
        (o.get("name") or "") for o in orgs
        if str(o.get("id") or o.get("organizationId") or "").strip() == selected_org_id
    ) or selected_org_id)

    entries: dict[str, dict] = {}
    for assistant in selected_assistants:
        profile_id = (
            assistant.get("id")
            or str(assistant.get("name") or "")
        )
        for entry in ta3_catalog._parse_config_models(assistant):  # noqa: SLF001（vendored 私有函数，复用）
            key = entry["name"]
            if key not in entries:
                entry["org_id"] = selected_org_id
                entry["org_name"] = org_name
                entry["profile_id"] = profile_id
                entries[key] = entry

    if not entries:
        logger.warning("[relay] 组织 %s 的配置未解析出模型", org_name)
        await storage.save_models([])
        return []

    models = list(entries.values())
    await storage.save_models(models)
    logger.info("[relay] 组织 %s 同步出 %d 个模型", org_name, len(models))
    return models
