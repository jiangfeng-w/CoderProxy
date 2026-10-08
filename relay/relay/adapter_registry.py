"""出站 adapter 注册表骨架（「WorkBuddy聊天反代与多平台聚合」§3.2 / §3.5 落地层）。

职责：「模型名前缀 → 出站 adapter / 模型目录」两件事的注册与查询。

- 牛码（`牛码/…` 与裸名）：**不走本注册表**——routes 直接构造 Ta3Provider（vendored
  资产零变化），保持既有测试的 monkeypatch 点（routes.build_provider）有效；
- WorkBuddy / 自定义供应商：实现方（`platforms/workbuddy/chat.py`、`providers_custom.py`）
  在自身模块导入时 `register_provider(...)`；routes 通过本注册表查询与路由。

注册项（每个前缀可各配其一/全配）：
- directory：async () -> list[dict]，该供应商的模型目录条目（`name` 为**裸名**，
  附带字段与牛码目录条目同构：context_window / supports_reasoning 等 G​UI 展示字段
  由 oai_adapter.model_to_openai 统一序列化）；
- factory：callable(model_name_bare, ctx) -> adapter，构造聊天出站 adapter。

adapter 契约（与 Ta3Provider 对齐的最小集，见 §3.4「签名保持稳定」）：
- `async def chat(request: ChatRequest) -> ChatResponse`（非流式聚合）
- `def stream_structured(request: ChatRequest) -> AsyncIterator[dict]`（thinking/content/done 事件）
- 可选 `async def refresh_credentials() -> bool`：凭证刷新钩子（§4.5.5 401 下沉；
  返回 True = 已刷新、调用方可重试；无此钩子 = 不支持刷新，401 直接上抛）。

本模块不 import 任何 relay 业务模块（避免环）；实现方导入本模块注册即可。
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from relay import model_ref

logger = logging.getLogger(__name__)

# prefix（规范名）→ 注册项
_providers: dict[str, dict] = {}


def register_provider(prefix: str, *,
                      directory: Callable[[], Awaitable[list[dict]]] | None = None,
                      factory: Callable[..., object] | None = None,
                      availability: Callable[[], Awaitable[bool]] | None = None) -> None:
    """注册（或覆盖）某供应商前缀的目录加载器 / adapter 工厂 / 可用性探测。

    实现模块在**导入时**调用（重复注册以最后一次为准；测试可借此替换替身）。

    availability（可选）：async () -> bool，账号池/凭证是否就绪。routes 在目录
    查不到模型时用它区分「池不可用（→ 503 引导配置）」与「模型不存在（→ 404）」。
    """
    key = model_ref.canonical_prefix(prefix)
    _providers[key] = {"directory": directory, "factory": factory,
                       "availability": availability}
    logger.debug("[registry] 注册供应商 %s（directory=%s, factory=%s, availability=%s）",
                 key, bool(directory), bool(factory), bool(availability))


def unregister_provider(prefix: str) -> None:
    """移除注册（测试清理用）。"""
    _providers.pop(model_ref.canonical_prefix(prefix), None)


def has_provider(prefix: str) -> bool:
    return model_ref.canonical_prefix(prefix) in _providers


def known_prefixes() -> list[str]:
    """已注册供应商前缀（/v1/models 合并用）。"""
    return list(_providers)


async def provider_available(prefix: str) -> bool | None:
    """供应商可用性：True/False；未注册探测钩子 → None（调用方按 404 处理）。"""
    hook = (_providers.get(model_ref.canonical_prefix(prefix)) or {}).get("availability")
    if hook is None:
        return None
    try:
        return bool(await hook())
    except Exception as exc:  # noqa: BLE001（探测异常不炸请求路径）
        logger.warning("[registry] 可用性探测失败 %s: %s", prefix, exc)
        return None


def adapter_factory(prefix: str) -> Callable[..., object] | None:
    """该前缀的 adapter 工厂；未注册返回 None。"""
    return (_providers.get(model_ref.canonical_prefix(prefix)) or {}).get("factory")

async def provider_models(prefix: str) -> list[dict]:
    """该供应商的模型目录（裸名条目）；未注册目录加载器返回 []。"""
    loader = (_providers.get(model_ref.canonical_prefix(prefix)) or {}).get("directory")
    if loader is None:
        return []
    models = await loader()
    return models if isinstance(models, list) else []


async def find_provider_model(prefix: str, bare: str) -> dict | None:
    """在供应商目录中按裸名查找条目（命中返回条目；未注册/未命中 None）。"""
    name = str(bare or "").strip()
    if not name:
        return None
    for m in await provider_models(prefix):
        if isinstance(m, dict) and str(m.get("name") or "").strip() == name:
            return m
    return None


def build_adapter(model: dict, model_name: str,
                  ctx=None):
    """按前缀构造出站 adapter；未注册前缀返回 None（调用方转 404）。"""
    ref = model_ref.parse_model_ref_lenient(model_name)
    if ref.is_niucode:
        return None  # 牛码由 routes.build_provider 直接构造（见模块 docstring）
    factory = adapter_factory(ref.prefix)
    if factory is None:
        return None
    return factory(ref.bare, ctx=ctx, model_entry=model)
