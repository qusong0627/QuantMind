"""买入前 K 线过滤（T-P4-04）——**纯函数唯一实现**（移植 KHunter 4 规则）。

设计口径（统一交易栈 §事前·形态；策略 spec ``risk.buy_filters``）：
- ``rise_from_low<=50%``：距 20 日低点涨幅 ≤50%（防追已大涨）；
- ``open_gap<=4%``：买入判断日开盘相对昨收跳空 ≤4%（防高开接盘）；
- ``bias5<=7``：5 日乖离率 ≤7%（防短线过热）；
- ``vol_ratio>=0.7``：当日量 / 前 5 日均量 ≥0.7（量能确认，防极度缩量假信号）。

纪律：过滤在**买入前**生效、只否决不下单（扫描/选股链仍只发现）；
数据不足的标的 **fail-closed 拒买**（拿不准就不买）；基础设施不可用由调用方
整步跳过并如实标注（本模块不静默吞）。

输入 bars：按日期升序的日线 dict 列表（open/high/low/close/volume），
最后一根 = 买入判断日；建议 ≥21 根（20 日回看），不足时按可得数据评估并如实拒/放。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

MIN_BARS_FOR_RULES = 6  # bias5 需 5 根 + 昨收；rise_from_low 尽可得窗口


@dataclass(frozen=True)
class BuyFilterConfig:
    """买入前过滤配置（默认 = 策略手册推荐值）。"""

    enabled: bool = True
    max_rise_from_low: float = 0.50  # 距 20 日低点涨幅上限
    rise_lookback: int = 20
    max_open_gap: float = 0.04  # 开盘跳空上限（相对昨收）
    max_bias5: float = 7.0  # BIAS5 上限（百分点）
    min_vol_ratio: float = 0.7  # 当日量/前 5 日均量 下限


DEFAULT_BUY_FILTERS = BuyFilterConfig()

_EXPR_RE = re.compile(
    r"^\s*(rise_from_low|open_gap|bias5|vol_ratio)\s*(<=|>=|<|>)\s*([0-9.]+)\s*%?\s*$"
)


def parse_buy_filters(expressions: list[str] | None) -> BuyFilterConfig:
    """解析策略 spec 的 ``risk.buy_filters`` 表达式列表 → 配置。

    未知键/不可解析表达式 → ``ValueError``（配置错误响亮失败，不静默忽略）。
    仅接受与默认规则同向的约束（rise/gap/bias 用 ``<=``；vol_ratio 用 ``>=``）。
    """
    if not expressions:
        return DEFAULT_BUY_FILTERS
    cfg = DEFAULT_BUY_FILTERS
    for raw in expressions:
        text = str(raw or "").strip()
        if not text:
            continue
        m = _EXPR_RE.match(text)
        if not m:
            raise ValueError(f"无法解析 buy_filters 表达式: {raw!r}")
        key, op, value_text = m.group(1), m.group(2), m.group(3)
        # 百分比记法归一：rise_from_low<=50% → 0.50；bias5<=7 本就是百分点
        value = float(value_text)
        if key in {"rise_from_low", "open_gap"}:
            if text.rstrip().endswith("%"):
                value = value / 100.0
            if op not in {"<=", "<"}:
                raise ValueError(f"buy_filters.{key} 只接受上界（<=/<）: {raw!r}")
            cfg = replace(
                cfg,
                max_rise_from_low=value
                if key == "rise_from_low"
                else cfg.max_rise_from_low,
                max_open_gap=value if key == "open_gap" else cfg.max_open_gap,
            )
        elif key == "bias5":
            if op not in {"<=", "<"}:
                raise ValueError(f"buy_filters.bias5 只接受上界（<=/<）: {raw!r}")
            cfg = replace(cfg, max_bias5=value)
        else:  # vol_ratio
            if op not in {">=", ">"}:
                raise ValueError(f"buy_filters.vol_ratio 只接受下界（>=/>）: {raw!r}")
            cfg = replace(cfg, min_vol_ratio=value)
    return cfg


def _f(row: dict[str, Any], key: str) -> float | None:
    value = row.get(key)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def evaluate_bar_filters(
    bars: list[dict[str, Any]],
    config: BuyFilterConfig | None = None,
) -> tuple[bool, list[str], dict[str, Any]]:
    """对单标的日线序列执行四规则 → (是否通过, 拒绝原因列表, 证据快照)。

    数据不足（<2 根或关键字段缺失）→ 拒买（fail-closed）；各规则独立证据留痕。
    """
    cfg = config or DEFAULT_BUY_FILTERS
    if not cfg.enabled:
        return True, [], {"enabled": False}
    series = [b for b in (bars or []) if isinstance(b, dict)]
    if len(series) < 2:
        return False, ["数据不足(日线<2根)"], {"bars": len(series)}

    last = series[-1]
    close = _f(last, "close")
    open_ = _f(last, "open")
    volume = _f(last, "volume")
    prev_close = _f(series[-2], "close")
    if close is None or open_ is None or prev_close is None or prev_close <= 0:
        return False, ["数据不足(关键字段缺失)"], {"bars": len(series)}

    reasons: list[str] = []
    evidence: dict[str, Any] = {"bars": len(series)}

    # ① 距低点涨幅（20 日窗口）
    window = series[-cfg.rise_lookback :]
    lows = [v for v in (_f(b, "low") for b in window) if v is not None and v > 0]
    if lows:
        low = min(lows)
        rise = (close - low) / low
        evidence["rise_from_low"] = round(rise, 6)
        if rise > cfg.max_rise_from_low:
            reasons.append(
                f"距{cfg.rise_lookback}日低点涨幅{rise:.1%}>{cfg.max_rise_from_low:.0%}"
            )
    else:
        reasons.append("数据不足(无有效 low)")

    # ② 开盘跳空（相对昨收）
    gap = (open_ - prev_close) / prev_close
    evidence["open_gap"] = round(gap, 6)
    if gap > cfg.max_open_gap:
        reasons.append(f"开盘跳空{gap:.1%}>{cfg.max_open_gap:.0%}")

    # ③ BIAS5（5 日乖离，含当日）
    window5 = series[-5:]
    closes5 = [v for v in (_f(b, "close") for b in window5) if v is not None]
    if len(closes5) >= 5:
        ma5 = sum(closes5) / len(closes5)
        if ma5 > 0:
            bias5 = (close - ma5) / ma5 * 100.0
            evidence["bias5"] = round(bias5, 4)
            if bias5 > cfg.max_bias5:
                reasons.append(f"BIAS5={bias5:.2f}>{cfg.max_bias5}")
    else:
        reasons.append("数据不足(BIAS5 需5根)")

    # ④ 量能确认（当日量 / 前 5 日均量）
    prev_vols = [v for v in (_f(b, "volume") for b in series[-6:-1]) if v is not None]
    if prev_vols and volume is not None:
        avg = sum(prev_vols) / len(prev_vols)
        if avg > 0:
            ratio = volume / avg
            evidence["vol_ratio"] = round(ratio, 4)
            if ratio < cfg.min_vol_ratio:
                reasons.append(f"量能{ratio:.2f}<{cfg.min_vol_ratio}")
    else:
        reasons.append("数据不足(量能)")

    return (not reasons), reasons, evidence


def apply_buy_filters(
    opportunities: list[Any],
    bars_by_symbol: dict[str, list[dict[str, Any]]],
    config: BuyFilterConfig | None = None,
) -> tuple[list[Any], list[dict[str, Any]]]:
    """对机会列表执行买入前过滤 → (保留, 拒绝明细[{symbol, reasons, evidence}])。

    ``bars_by_symbol`` 缺失某标的 → 该标的 fail-closed 拒买并标注（不静默放行）。
    """
    cfg = config or DEFAULT_BUY_FILTERS
    if not cfg.enabled:
        return list(opportunities), []
    kept: list[Any] = []
    rejected: list[dict[str, Any]] = []
    for opp in opportunities:
        symbol = str(getattr(opp, "symbol", "") or "")
        bars = bars_by_symbol.get(symbol)
        if bars is None:
            rejected.append(
                {
                    "symbol": symbol,
                    "reasons": ["无日线数据（fail-closed）"],
                    "evidence": {},
                }
            )
            continue
        ok, reasons, evidence = evaluate_bar_filters(bars, cfg)
        if ok:
            kept.append(opp)
        else:
            rejected.append(
                {"symbol": symbol, "reasons": reasons, "evidence": evidence}
            )
    return kept, rejected
