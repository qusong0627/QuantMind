"""标的级禁买判据的**唯一出处**（ST / 退市）。

为什么单独立一个模块
--------------------
同一句「这只票名字里有 ST，别买」在本仓曾经写了两遍，而且两遍**不一样**：
`services/live_trading/services/tdx_signal_push_service.py` 原写 ``"ST" in
name.upper()``（子串命中）且完全不看退市；决策层用的是前缀表 + 退市标记。
判定分裂的后果不是"多拦一只"，而是**两处对同一只票给出相反结论**——推送栏里
过滤掉了、决策层却又放行进去买。两处现已同源（推送侧改为调用本模块）。

故本模块只做一件事：把「名称 → 是否禁买」收敛成一处。名称取不到（空串）时
**一律放行**（fail-open），这条是隔壁实测后的口径，理由照抄：宁可漏拦一只，
也不能因为名称表拉不到就把当天所有买入停掉；代码黑名单（按 6 位号精确匹配）
不依赖名称，任何时候都生效，两者互补而不是互相备份。

**本模块不看代码、不查库、不做 IO**：只做名称字符串 → bool。ST 的
*逐日*口径（某只票今天是不是 ST）属于行情侧（见 ``stock_daily_latest.is_st``、
``scripts/daily_review.py``）；这里判的是「按名称就该禁买」的静态一类。
"""

from __future__ import annotations

#: ST 类前缀。**顺序无关**（逐个 startswith），列全是为了让人一眼看到全形态：
#: ``ST`` 特别处理、``*ST`` 退市风险警示、``SST``/``S*ST`` 未股改叠加特别处理。
ST_PREFIXES: tuple[str, ...] = ("ST", "*ST", "SST", "S*ST")

#: 退市整理期标记：``退市海润``（前缀式）与 ``海润退``（后缀式）两种都在用，
#: 故判**包含**而不是判前缀。A 股证券简称里「退」字基本只出现在这一类。
DELIST_MARK = "退"


def is_risky_name(name: str | None) -> bool:
    """证券简称是否命中 ST / 退市（**空名返回 False**）。

    先去掉空白再判：真实源里出现过 ``"ST 三圣"``（中间带一个空格），
    不归一就会漏拦——而漏拦的代价是拿真钱买一只退市风险股。
    """
    s = str(name or "").strip().replace(" ", "").replace("　", "").upper()
    if not s:
        return False
    if any(s.startswith(prefix) for prefix in ST_PREFIXES):
        return True
    return DELIST_MARK in s
