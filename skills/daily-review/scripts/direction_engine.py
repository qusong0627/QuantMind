"""次日走势方向引擎：多维度信号加权 → 明确方向 + 数据完整度星级 + 每维依据。

纯函数、无 I/O，输入 daily_review 的 stats 字典，输出：
    {
      "date", "total_score", "direction", "data_completeness",
      "dimensions": [{key, name, score, weight, evidence}],
      "stars"  # = data_completeness：数据覆盖星级（缺 news/L2 时降级），非模型信心
    }

方向阈值（total_score 归一在 [-10, +10]）：
    >= +3.5  强烈看多    +1.5~+3.5  看多    -1.5~+1.5  震荡
    -3.5~-1.5  看空      <-3.5      强烈看空

设计原则：分数可解释——每个维度独立给分，依据写进 evidence。
分数只是“解读框架”，不伪装精确。
"""
from __future__ import annotations

from typing import Any


def _f(v: Any) -> float:
    """任意可空值 → float，None/NaN → 0。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if f == f else 0.0


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _dim(name: str, score: float, weight: float, evidence: str) -> dict[str, Any]:
    return {"key": name, "name": name, "score": round(_clamp(score, -1, 1) * weight, 2),
            "weight": weight, "raw": round(score, 2), "evidence": evidence}


def _index_row(idx: list[dict], name: str) -> dict | None:
    for r in idx or []:
        if r.get("name") == name and not r.get("missing"):
            return r
    return None


# ── 维度 1：大盘趋势 + 量能 ──
def score_trend(idx: list[dict], market: dict) -> dict:
    sh = _index_row(idx, "上证指数")
    ev: list[str] = []
    s = 0.0
    if sh:
        vma20 = _f(sh.get("vs_ma20"))
        if vma20 > 0:
            s += 1.0; ev.append(f"上证站上 MA20（+{vma20:.2f}%）")
        elif vma20 < -1:
            s -= 1.0; ev.append(f"上证跌破 MA20（{vma20:.2f}%）")
        else:
            ev.append(f"上证贴近 MA20（{vma20:+.2f}%）")
        pct = _f(sh.get("pct"))
        if pct >= 1:
            s += 1.0; ev.append(f"上证当日 {pct:+.2f}% 放量上行")
        elif pct <= -1:
            s -= 1.0; ev.append(f"上证当日 {pct:+.2f}% 走弱")
        else:
            ev.append(f"上证当日 {pct:+.2f}%")
    vr = _f(market.get("amount_ratio_ma5"))
    if market.get("amount_ma5_yi"):
        if vr > 1.15:
            s += 0.6; ev.append(f"两市成交为5日均 {vr:.2f}x 显著放量")
        elif vr < 0.85:
            s -= 0.6; ev.append(f"两市成交为5日均 {vr:.2f}x 缩量")
        else:
            ev.append(f"量能 {vr:.2f}x 5日均（平稳）")
    r = _f(market.get("up_down_ratio"))
    if r >= 1.5:
        s += 0.6; ev.append(f"涨跌家数比 {r:.2f} 普涨")
    elif r <= 0.67:
        s -= 0.6; ev.append(f"涨跌家数比 {r:.2f} 普跌")
    else:
        ev.append(f"涨跌家数比 {r:.2f}（分化）")
    if not ev:
        ev.append("指数/量能数据缺失，趋势维度中性")
    return _dim("趋势/量能", s, 3.0, "；".join(ev))


# ── 维度 2：涨跌结构 + 情绪 ──
def score_breadth(market: dict, sentiment: dict | None) -> dict:
    ev: list[str] = []
    s = 0.0
    up = _f(market.get("limit_up")); dn = _f(market.get("limit_down"))
    bro = _f(market.get("broke_up"))
    net = up - dn
    if net >= 30:
        s += 1.0; ev.append(f"涨停-跌停 = {up:.0f}-{dn:.0f} = {net:.0f}，赚钱效应强")
    elif net <= -10:
        s -= 1.0; ev.append(f"涨停-跌停 = {up:.0f}-{dn:.0f} = {net:.0f}，亏钱效应强")
    else:
        ev.append(f"涨停{up:.0f}/跌停{dn:.0f}/炸板{bro:.0f}")
    zhangban = up + bro
    bro_rate = bro / zhangban if zhangban > 0 else 0.5
    if zhangban > 0:
        if bro_rate >= 0.45:
            s -= 0.7; ev.append(f"炸板率 {bro_rate:.0%} 偏高（追高意愿崩）")
        elif bro_rate <= 0.25:
            s += 0.7; ev.append(f"炸板率 {bro_rate:.0%} 低，封板质量好")
    streak = _f(market.get("max_streak"))
    if streak >= 5:
        s += 0.5; ev.append(f"最高 {streak:.0f} 连板，情绪高涨")
    elif 0 < streak <= 2:
        s -= 0.5; ev.append(f"最高仅 {streak:.0f} 板，情绪低迷")
    if sentiment:
        bp = _f(sentiment.get("buy_pressure_mean")); sp = _f(sentiment.get("sell_pressure_mean"))
        if bp > sp:
            s += 0.5; ev.append(f"买压 {bp:.2f} > 卖压 {sp:.2f}")
        elif sp > bp:
            s -= 0.5; ev.append(f"卖压 {sp:.2f} > 买压 {bp:.2f}")
    if not ev:
        ev.append("涨跌结构数据缺失，情绪维度中性")
    return _dim("情绪/结构", s, 2.5, "；".join(ev))


# ── 维度 3：L2 微观结构 ──
def score_l2(factors: dict | None) -> dict:
    l2 = (factors or {}).get("l2") or {}
    if not l2:
        return _dim("L2 微观", 0.0, 2.0, "L2 因子数据缺失（news_review 后重跑或检查 l2_factors 分区），维度中性")
    ev: list[str] = []
    s = 0.0
    strong = _f(l2.get("strong_pct"))
    if strong >= 0.30:
        s += 1.0; ev.append(f"正向因子强信号股占比 {strong:.0%}（知情资金扩散）")
    elif strong <= 0.12:
        s -= 1.0; ev.append(f"正向因子强信号股占比仅 {strong:.0%}（知情资金收敛）")
    else:
        ev.append(f"正向因子强信号股占比 {strong:.0%}")
    div = _f(l2.get("divergence_mean"))
    if div >= 0.6:
        s -= 0.7; ev.append(f"量价背离均值 {div:.2f} 偏高（谨慎）")
    else:
        ev.append(f"量价背离均值 {div:.2f}")
    super_net = _f(l2.get("super_net_yi"))
    if super_net > 0:
        s += 0.7; ev.append(f"主力净流入 {super_net:+.2f} 亿")
    elif super_net < 0:
        s -= 0.7; ev.append(f"主力净流出 {super_net:+.2f} 亿")
    else:
        ev.append(f"主力净额 {super_net:+.2f} 亿")
    vpin = _f(l2.get("vpin_mean"))
    if vpin >= 0.6:
        s += 0.5; ev.append(f"VPIN 家族全市场中位 {vpin:.2f}（知情交易活跃）")
    if not ev:
        ev.append("L2 读数为空")
    return _dim("L2 微观", s, 2.0, "；".join(ev))


# ── 维度 4：新闻情绪 ──
def score_news(news: dict | None) -> dict:
    if not news:
        return _dim("新闻情绪", 0.0, 2.0, "新闻情绪数据缺失（先跑 news_review.py），维度中性")
    ev: list[str] = []
    s = 0.0
    total = _f(news.get("n"))
    if total > 0:
        bull = _f(news.get("bullish")); bear = _f(news.get("bearish"))
        net_ratio = (bull - bear) / total
        if net_ratio >= 0.20:
            s += 1.0; ev.append(f"当日新闻净情绪 {net_ratio:+.0%}（利好 {bull:.0f} / 利空 {bear:.0f}）偏多")
        elif net_ratio <= -0.20:
            s -= 1.0; ev.append(f"当日新闻净情绪 {net_ratio:+.0%}（利好 {bull:.0f} / 利空 {bear:.0f}）偏空")
        else:
            ev.append(f"新闻情绪中性（利好 {bull:.0f} / 利空 {bear:.0f}）")
    gold = _f(news.get("gold_news"))
    reverse = _f(news.get("reverse_news"))
    if gold > reverse:
        s += 0.6; ev.append(f"高质量源 {gold:.0f} 篇 > 反向源 {reverse:.0f} 篇")
    elif reverse > gold:
        s -= 0.6; ev.append(f"反向源 {reverse:.0f} 篇密集（情绪标签可信度下降）")
    gh = _f(news.get("golden_hour_bullish")); gh_t = _f(news.get("golden_hour_total"))
    if gh_t > 0 and gh / gh_t >= 0.5:
        s += 0.4; ev.append(f"黄金时段利好占比 {gh / gh_t:.0%}")
    if news.get("top_sectors"):
        ev.append("新闻聚焦板块：" + "、".join(str(x) for x in (news.get("top_sectors") or [])[:3]))
    if not ev:
        ev.append("新闻数据为空")
    return _dim("新闻情绪", s, 2.0, "；".join(ev))


# ── 维度 5：板块 + 资金流 ──
def score_sector(sectors: dict, sector_flow: list[dict] | None) -> dict:
    ev: list[str] = []
    s = 0.0
    top = (sectors or {}).get("行业板块(一级)") or []
    if top:
        avg = _f(sum(r.get("avg_pct") for r in top[:5]) / max(1, len(top[:5])))
        if avg >= 2:
            s += 0.8; ev.append(f"领涨行业 Top5 均涨 {avg:+.2f}%（赚钱主线明确）")
        elif avg <= -1:
            s -= 0.8; ev.append(f"领涨行业 Top5 均涨 {avg:+.2f}%（无主线）")
        else:
            ev.append(f"领涨行业 Top5 均涨 {avg:+.2f}%")
        ev.append("领涨：" + "、".join(str(r.get("SectorName")) for r in top[:3]))
    sf = sector_flow or []
    if sf:
        inflow = [r for r in sf if _f(r.get("net_yi")) > 0]
        outflow = [r for r in sf if _f(r.get("net_yi")) < 0]
        if len(inflow) >= len(outflow) and len(inflow) > 0:
            s += 0.6; ev.append(f"申万行业净流入 {len(inflow)} / 净流出 {len(outflow)}（资金回流）")
        elif len(outflow) > len(inflow):
            s -= 0.6; ev.append(f"申万行业净流出 {len(outflow)} > 净流入 {len(inflow)}（资金离场）")
    if not ev:
        ev.append("板块/资金流数据缺失，维度中性")
    return _dim("板块/资金流", s, 1.5, "；".join(ev))


def _direction(total: float) -> str:
    if total >= 3.5:
        return "强烈看多"
    if total >= 1.5:
        return "看多"
    if total > -1.5:
        return "震荡"
    if total > -3.5:
        return "看空"
    return "强烈看空"


def score_dimensions(stats: dict) -> dict[str, Any]:
    dims = [
        score_trend(stats.get("index") or [], stats.get("market") or {}),
        score_breadth(stats.get("market") or {}, stats.get("sentiment")),
        score_l2(stats.get("factors")),
        score_news(stats.get("news")),
        score_sector(stats.get("sectors"), (stats.get("factors") or {}).get("sector_flow")),
    ]
    total = sum(d["score"] for d in dims)
    direction = _direction(total)

    # 数据完整度星级（≠ 模型置信度）：只由数据覆盖决定 ——
    # 新闻 + L2 都在 → 5；缺其一 → 3；都缺 → 2。
    # T6-4 审计（2026-10-10）：字段名原为 `confidence`，与语义（数据覆盖）不符，
    # 改为 `data_completeness`；原先末尾还有一段「数据全且维度高度一致 → 5」的分支，
    # 经推演为恒等 no-op（stars ∈ {5,3,2}，`stars >= 4` 只可能是 5，赋值 5→5），
    # 已删除——一致性由报告正文的维度依据表达，不混入覆盖星级。
    has_news = bool(stats.get("news"))
    has_factors = bool((stats.get("factors") or {}).get("l2"))
    if has_news and has_factors:
        stars = 5
    elif has_factors:
        stars = 3
    elif has_news:
        stars = 3
    else:
        stars = 2

    return {
        "date": stats.get("meta", {}).get("trade_date"),
        "total_score": round(total, 2),
        "direction": direction,
        "data_completeness": stars,
        "max_score": round(sum(d["weight"] for d in dims), 2),
        "dimensions": dims,
    }


__all__ = ["score_dimensions"]