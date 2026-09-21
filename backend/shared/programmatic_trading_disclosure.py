"""程序化交易投资者报告义务：待填报信息与高频阈值护栏。

**这个模块不替用户履行报告义务，也不做合规判定。** 它做两件事：

1. 把券商/交易所要求填报的「交易软件信息」（软件名称、版本号、开发者/供应商名称）
   从版本单一事实源拼成一段可直接复制的文本，免得用户手抄错版本号；
2. 把法规里的**高频交易认定阈值**写成常量，给运行期的下单频率配置当护栏——
   配置撞上阈值时**只告警、不拦截**（拦不拦是用户自己的交易决策，不是本工具的权限）。

法规依据（用户须自行与开户券商/交易所核对最新口径）：

- 中国证监会《证券市场程序化交易管理规定（试行）》（2024 年发布，2024-10-08 施行）：
  投资者从事程序化交易前，应当通过证券公司向证券交易所报告，报告内容含账户基本信息、
  资金信息、交易信息，以及**交易软件的名称、版本号、开发者（供应商）名称**等。
- 沪深北交易所程序化交易管理实施细则：**高频交易**指单账户每秒申报、撤单笔数最高达到
  300 笔以上，或单日申报、撤单笔数最高达到 20000 笔以上。被认定为高频交易的，还需
  额外报告系统服务器所在地、系统测试报告、系统故障应急方案等信息。

阈值随监管口径变动，**以券商/交易所最新要求为准**；本模块的常量只是护栏，不是法律意见。
"""

from __future__ import annotations

import logging
import math
import os
from typing import Any

from backend.shared.version import get_version_info

logger = logging.getLogger(__name__)

# ── 交易软件信息（券商填报用）────────────────────────────────────────
#
# 名称与开发者可由分发方通过环境变量覆盖：程序化交易报告里要填的是**用户实际拿到的
# 那个软件**的信息，二次分发的名字/主体与上游开源项目未必相同，写死会让人填错。
_DEV_ENV = "QM_SOFTWARE_DEVELOPER"
_DEFAULT_DEVELOPER = "QuantMind 开源项目"


def resolve_developer(raw: str | None) -> str:
    """分发方名称：空串/空白一律回落到默认值（填报栏留空等于没填）。"""
    return (raw or "").strip() or _DEFAULT_DEVELOPER


SOFTWARE_NAME = "QuantMind"
SOFTWARE_DEVELOPER = resolve_developer(os.getenv(_DEV_ENV))

# ── 高频交易认定阈值（护栏用，不是拦截阈值）──────────────────────────
#
# 单位陷阱：法规按**每秒**申报+撤单笔数，而风控规则 `l3.order_frequency` 配的是
# **每分钟**。两个单位混用会把 300 笔/秒 看成 300 笔/分，差 60 倍。这里两套都留常量。
HFT_ORDERS_PER_SECOND = 300
HFT_ORDERS_PER_DAY = 20_000
HFT_ORDERS_PER_MINUTE = HFT_ORDERS_PER_SECOND * 60  # = 18000


def software_version() -> str:
    """当前部署版本（复用 ``shared/version.py``，不另建版本来源）。"""
    return str(get_version_info().get("version") or "dev")


def is_high_frequency_order_rate(per_minute: float | None) -> bool:
    """每分钟下单上限是否已触及高频交易认定线（≥300 笔/秒 等价于 ≥18000 笔/分）。

    取不到值（None / 非数 / 负数）一律判 ``False`` —— 配置缺失是另一回事，
    不该在这里被报成「你可能被认定为高频」。
    """
    if per_minute is None or isinstance(per_minute, bool):
        return False
    try:
        value = float(per_minute)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(value):
        return False
    return value >= HFT_ORDERS_PER_MINUTE


def disclosure_lines() -> list[str]:
    """待填报信息（键值对形态，便于前端渲染成可复制块）。"""
    return [
        f"交易软件名称：{SOFTWARE_NAME}",
        f"交易软件版本号：{software_version()}",
        f"开发者（供应商）名称：{SOFTWARE_DEVELOPER}",
    ]


def disclosure_text() -> str:
    """可直接复制到券商程序化交易报告表里的文本块。"""
    return "\n".join(disclosure_lines())


def log_high_frequency_warning(
    per_minute: Any,
    *,
    source: str,
    rule_key: str = "l3.order_frequency",
) -> bool:
    """下单频率配置触及高频认定线时打 WARNING；返回是否已告警。

    **只告警不拦截**：被认定为高频交易本身不违法，只是要额外报告并接受更严的监管，
    要不要落在这一侧是用户自己的事。真正危险的是**用户不知道自己已经踩线**。
    """
    if not is_high_frequency_order_rate(per_minute):
        return False
    logger.warning(
        "下单频率配置 %s=%s 已达高频交易认定线（≥%d 笔/分钟 即 ≥%d 笔/秒，"
        "或单日 ≥%d 笔）：按交易所口径可能被认定为高频交易，须额外报告系统服务器所在地、"
        "系统测试报告与故障应急方案。来源：%s。本工具不代为报告，请与开户券商核对。",
        rule_key,
        per_minute,
        HFT_ORDERS_PER_MINUTE,
        HFT_ORDERS_PER_SECOND,
        HFT_ORDERS_PER_DAY,
        source,
    )
    return True
