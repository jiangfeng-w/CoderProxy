"""模型名命名空间解析（供应商前缀）——「WorkBuddy聊天反代与多平台聚合」§4.1 定稿。

对外模型名统一为 `<供应商名>/<上游模型名>`（2026-10-08 用户拍板）：

- 牛码：`牛码/glm-5.3`；**裸名兼容**（无前缀的旧名按牛码处理，存量 agent 配置不改）；
- WorkBuddy：`WorkBuddy/glm-5.3-flash`；
- 自定义供应商：`<供应商名>/<模型>`（名称来自供应商页 F5 的 name 字段）。

解析规则：
- 按**第一个** `/` 切分；前缀大小写不敏感匹配（`workbuddy/` 也接受），
  但规范化输出固定为「牛码」/「WorkBuddy」/自定义名原文；
- 带 `/` 但前缀不在已知表内 → UnknownProviderError（调用方 404「未知供应商」）；
- 无 `/`（或 `/` 前/后为空）→ 裸名，prefix=""，视作牛码。

本模块不依赖 relay 其它模块（storage/routes/adapter 均可引用，避免环）。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

PREFIX_NIUCODE = "牛码"
PREFIX_WORKBUDDY = "WorkBuddy"


def base_prefix_table() -> dict[str, str]:
    """内置供应商前缀表：casefold 前缀 → 规范前缀。"""
    return {
        PREFIX_NIUCODE.casefold(): PREFIX_NIUCODE,
        PREFIX_WORKBUDDY.casefold(): PREFIX_WORKBUDDY,
    }


def canonical_prefix(prefix: str) -> str:
    """前缀规范形态：内置表命中取规范名；否则原样返回（自定义供应商名）。"""
    text = str(prefix or "").strip()
    return base_prefix_table().get(text.casefold(), text)


class UnknownProviderError(ValueError):
    """带 `/` 但前缀不在已知供应商表内。"""

    def __init__(self, head: str):
        self.head = head
        super().__init__(f"未知供应商: {head}")


@dataclass(frozen=True)
class ModelRef:
    """解析后的模型引用：prefix=""/牛码 → 牛码链路；其余为对应供应商。"""

    prefix: str  # 规范前缀；"" = 裸名（默认牛码）
    bare: str    # 上游真实模型名（无前缀）
    raw: str     # 请求原名（原样回显用）

    @property
    def provider(self) -> str:
        """规范供应商前缀（裸名归一到牛码）。"""
        return self.prefix or PREFIX_NIUCODE

    @property
    def is_niucode(self) -> bool:
        return self.prefix in ("", PREFIX_NIUCODE)

    @property
    def canonical(self) -> str:
        """全名（白名单/思考配置/日志键统一用该形态）。"""
        return f"{self.provider}/{self.bare}"


def parse_model_ref(raw: str, known: Mapping[str, str] | None = None) -> ModelRef:
    """解析模型名 → ModelRef。

    known：casefold 前缀 → 规范前缀；缺省 = 内置两前缀（牛码/WorkBuddy）。
    routes 可传入含自定义供应商名的全表。
    """
    text = str(raw or "").strip()
    head, sep, tail = text.partition("/")
    if not sep or not head.strip() or not tail.strip():
        return ModelRef(prefix="", bare=text, raw=text)
    table = base_prefix_table() if known is None else known
    canonical = table.get(head.strip().casefold())
    if canonical is None:
        raise UnknownProviderError(head.strip())
    return ModelRef(prefix=canonical, bare=tail.strip(), raw=text)


def parse_model_ref_lenient(raw: str) -> ModelRef:
    """宽容解析（出站构造路径专用）：未知前缀按原样作前缀，不抛错。

    请求路径的合法性已由 _ensure_model（严格解析 + 目录校验）把关；此处只做
    「全名 → （前缀, 裸名）」拆分，避免 build_provider 这类同步函数被迫等待
    自定义供应商配置的异步加载。裸名 → prefix=""（牛码）。
    """
    text = str(raw or "").strip()
    head, sep, tail = text.partition("/")
    if not sep or not head.strip() or not tail.strip():
        return ModelRef(prefix="", bare=text, raw=text)
    canonical = base_prefix_table().get(head.strip().casefold()) or head.strip()
    return ModelRef(prefix=canonical, bare=tail.strip(), raw=text)


def canonical_key(name: str) -> str:
    """任意形态模型名 → 规范全名（用于白名单/思考配置的比较与存储）。

    裸名 → `牛码/<name>`；已带已知前缀 → 规范前缀全名；未知前缀原样返回
    （自定义供应商目录未加载时的宽容回退，避免脏数据炸读路径）。
    """
    text = str(name or "").strip()
    if not text:
        return ""
    try:
        return parse_model_ref(text).canonical
    except UnknownProviderError:
        return text


def normalize_whitelist(names: list[str]) -> list[str]:
    """白名单归一（读展示/写入前）：裸名 → `牛码/<name>`；哨兵 __none__ 原样保留。"""
    out: list[str] = []
    for n in names:
        text = str(n or "").strip()
        if not text:
            continue
        out.append(text if text == "__none__" else canonical_key(text))
    return out


def full_name(prefix: str, bare: str) -> str:
    """(前缀, 裸名) → 规范全名。前缀取规范形态（牛码/WorkBuddy 大小写固定）。"""
    return f"{canonical_prefix(prefix)}/{str(bare or '').strip()}"
