#!/usr/bin/env python3
"""港股候选池（恒指权重股动量评分 → ``data/hk_picks.json``）。

端口自旧栈 quant-Trader ``data/HK_stock/get_daily_price_hk.py``（腾讯抓取器）+
``scripts/hk_picks.py``（评分），2026-10-08 随港股富途通道一并复活：

- 抓取：腾讯 ``web.ifzq.gtimg.cn/appstock/app/fqkline/get`` 后复权日 K，免费无鉴权；
  每只 **≥65 根**才入池（60 日动量 + 20 日窗口的最短样本）。
  **不用** quanthk parquet——那份本地日线最后分区停在 2026-08-28（断更）。
- 除新：最后一个交易日落后**全池最新**交易日 >3 个工作日的标的剔出（停牌/退市；
  2026-10-08 实盘发现恒生银行 00011.HK 退市后仍以 90 根旧 K 冲进池子第 3 名），
  剔除台账写 ``excluded_stale``。根数不足是另一条线（``MIN_BARS``）。
- 评分：mom20 45% / mom60 30% / 趋势（现价 vs MA20）15% / 低波动 10%，
  分位排名合成（并列取平均名次），低波动反向计分。
- 产物：``date``（北京今日）/ ``generated_at`` / ``data_date``（全池最新交易日）/
  ``stale_days``（data_date 之后的工作日数，港股假期未建模）/ ``market_direction`` /
  ``universe_size`` / ``picks``（默认 top 20，``name`` 取 QM 名称表、缺失留空）。
- 消费：``scripts/live_hourly_analysis_hk.py`` 注入盘中分析提示词；它按
  ``stale_days`` 判定池是否可用（>5 个交易日降级为「仅持仓复盘」）。

用法：
    python3 scripts/hk_picks.py                     # 全池抓取 + 评分 → top 20
    python3 scripts/hk_picks.py --top 10
    python3 scripts/hk_picks.py --symbols 00700.HK,09988.HK   # 只跑指定标的（调试）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_FILE = ROOT / "data" / "hk_picks.json"
CN_TZ = timezone(timedelta(hours=8))

API = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

#: 恒生指数权重股（旧栈同名清单，30 只）
DEFAULT_SYMBOLS = [
    "00700.HK", "09988.HK", "03690.HK", "01810.HK", "00941.HK",  # 腾讯/阿里/美团/小米/中移动
    "00005.HK", "01299.HK", "00939.HK", "03988.HK", "00011.HK",  # 汇丰/友邦/建行/中行/恒生
    "02318.HK", "02628.HK", "01398.HK", "00998.HK", "00388.HK",  # 平安/人寿/工行/中信/港交所
    "01093.HK", "09618.HK", "09999.HK", "02020.HK", "01024.HK",  # 石药/京东/网易/安踏/快手
    "02331.HK", "02688.HK", "00288.HK", "00016.HK", "00027.HK",  # 李宁/新奥/万洲/新鸿基/银河
    "01928.HK", "00267.HK", "00175.HK", "02382.HK", "06862.HK",  # 金沙/中信/吉利/舜宇/海底捞
]

#: 评分权重（各分量先转 0~1 分位，再合成 0~100）
W_MOM20, W_MOM60, W_TREND, W_LOWVOL = 0.45, 0.30, 0.15, 0.10

#: 入池最短样本：60 日动量需要 closes[-61]，再留 5 根缓冲对齐除权缺口
MIN_BARS = 65

#: 单只 K 线允许落后全池最新交易日的**工作日**上限（>此值 = 停牌/退市，出池）。
#: 3 容忍偶发漏抓与单日临时停牌，又不给停了一个月以上的标的机会。
MAX_BAR_LAG_WEEKDAYS = 3
LOT_HINT = 100  # 仅用于读数提示，不影响评分


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def _f(value: object, default: float = 0.0) -> float:
    """腾讯接口的 OHLCV 是字符串（偶有 '' / 'N/A'）→ float，坏值走默认。"""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _parse_day(value: str) -> date | None:
    """``YYYY-MM-DD`` → date；坏值/空值 None（调用方各自决定退化行为）。"""
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _last_valid_bar_date(bars: list[dict]) -> str:
    """最后一根**收盘价有效**的 K 线日期（对齐 score_universe 实际用的 closes[-1]）。"""
    for bar in reversed(bars):
        if _f(bar.get("close")) > 0:
            return str(bar.get("date") or "")
    return ""


# ---------------------------------------------------------------- 抓取（纯解析 + HTTP 两层）


def parse_kline_payload(payload: object, symbol: str) -> list[dict]:
    """腾讯 fqkline 响应 → ``[{date, open, close, high, low, volume}]``（纯函数）。

    后复权数据在 ``qfqday``，普通在 ``day``（腾讯两处都可能给）；``code != 0``
    视为无数据（旧栈同口径，不抛异常——单只失败不该打断全池）。
    """
    if not isinstance(payload, dict) or payload.get("code") != 0:
        return []
    code = symbol.replace(".HK", "").replace(".", "")
    node = (payload.get("data") or {}).get(f"hk{code}") or {}
    if not isinstance(node, dict):
        return []
    rows = node.get("qfqday") or node.get("day") or []
    bars: list[dict] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        bars.append({
            "date": str(row[0]),
            "open": row[1],
            "close": row[2],
            "high": row[3],
            "low": row[4],
            "volume": row[5],
        })
    return bars


def fetch_kline(symbol: str, start: str, end: str, *, timeout: int = 15) -> list[dict]:
    """拉单只港股日 K（后复权）。网络/解析失败抛异常，由调用方记一笔并继续。"""
    import requests

    code = symbol.replace(".HK", "").replace(".", "")
    resp = requests.get(
        API,
        params={"param": f"hk{code},day,{start},{end},320,qfq"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return parse_kline_payload(resp.json(), symbol)


# ---------------------------------------------------------------- 评分（纯函数）


def _ranks(values: list[float]) -> list[float]:
    """值 → 分位排名 0~1（并列取平均名次；单元素给 0.5）。"""
    n = len(values)
    if n <= 1:
        return [0.5] * n
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 / (n - 1)  # 并列平均
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def score_universe(bars_map: dict[str, list[dict]]) -> list[dict]:
    """全池动量评分 → ``[{code, last_close, mom20, mom60, trend, vol20, score}]``（降序）。

    两条剔除线，都在进 stats 之前（不参与分位，避免污染全池排序）：

    - 样本不足 ``MIN_BARS``（只 20 根 K 的票会在 mom60 上拿随机名次）；
    - 末根 K 线陈旧（``stale_symbols``：停牌/退市股拿半年前的动量照样能冲高名次）。
    """
    stale = {e["code"] for e in stale_symbols(bars_map)}
    stats: list[dict] = []
    for code, bars in bars_map.items():
        if code in stale:
            continue
        closes = [c for c in (_f(b.get("close")) for b in bars) if c > 0]
        if len(closes) < MIN_BARS:
            continue
        last = closes[-1]
        mom20 = last / closes[-21] - 1 if closes[-21] else 0.0
        mom60 = last / closes[-61] - 1 if closes[-61] else 0.0
        ma20 = sum(closes[-20:]) / 20
        trend = last / ma20 - 1 if ma20 else 0.0
        rets = [
            closes[i] / closes[i - 1] - 1
            for i in range(len(closes) - 20, len(closes))
            if closes[i - 1]
        ]
        mean = sum(rets) / len(rets) if rets else 0.0
        vol20 = (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5 if rets else 0.0
        stats.append({
            "code": code,
            "last_close": round(last, 3),
            "mom20": round(mom20 * 100, 2),
            "mom60": round(mom60 * 100, 2),
            "trend": round(trend * 100, 2),
            "vol20": round(vol20 * 100, 2),
        })
    if not stats:
        return []
    r20 = _ranks([s["mom20"] for s in stats])
    r60 = _ranks([s["mom60"] for s in stats])
    rt = _ranks([s["trend"] for s in stats])
    rv = _ranks([-s["vol20"] for s in stats])  # 低波动反向：波动越小分越高
    for i, s in enumerate(stats):
        s["score"] = round(
            (W_MOM20 * r20[i] + W_MOM60 * r60[i] + W_TREND * rt[i] + W_LOWVOL * rv[i]) * 100,
            1,
        )
    return sorted(stats, key=lambda s: -s["score"])


def market_direction(stats: list[dict]) -> str:
    """全池 mom20 中位数 → 大盘方向标签（注入分析提示词）。"""
    if not stats:
        return "震荡（样本不足）"
    mids = sorted(s["mom20"] for s in stats)
    mid = mids[len(mids) // 2]
    if mid > 1.0:
        return "bullish（权重股动量偏多）"
    if mid < -1.0:
        return "bearish（权重股动量偏空）"
    return "震荡（权重股动量中性）"


def data_date(bars_map: dict[str, list[dict]]) -> str:
    """全池最新一根日 K 的日期（YYYY-MM-DD）；无数据返回空串。"""
    dates = [str(b.get("date") or "") for bars in bars_map.values() for b in bars]
    return max((d for d in dates if d), default="")


def stale_days(data_day: str, today: date) -> int:
    """``data_day`` 之后到 ``today`` 的工作日数（不含周末，**不建模港股假期**）。

    用作「池子还新不新」的刻度：0=今日/最近交易日数据，节假日会高估 1~2 天
    （消费侧阈值 5 个交易日，留足余量）。
    """
    start = _parse_day(data_day)
    if start is None or start >= today:
        return 0
    days = 0
    cur = start + timedelta(days=1)
    while cur <= today:
        if cur.weekday() < 5:
            days += 1
        cur += timedelta(days=1)
    return days


def stale_symbols(
    bars_map: dict[str, list[dict]], *, max_lag: int = MAX_BAR_LAG_WEEKDAYS
) -> list[dict]:
    """末根 K 线落后**全池最新**交易日 > ``max_lag`` 个工作日的标的 → ``[{code,last_bar,lag_days}]``（纯函数）。

    基准是自比的（不读墙钟）：整池同一天停（长假后首抓）时谁都不算落后——
    那种「池子整体不新」由 ``stale_days`` 管，别混为一谈。
    只盘点根数已够 ``MIN_BARS`` 的标的：根数不足是另一条剔除线（不参与分位，
    也不该出现在这份台账里）。
    """
    dated: dict[str, str] = {}
    for code, bars in bars_map.items():
        valid = [c for c in (_f(b.get("close")) for b in bars) if c > 0]
        if len(valid) < MIN_BARS:
            continue
        last = _last_valid_bar_date(bars)
        if last:
            dated[code] = last
    if not dated:
        return []
    pool_latest = _parse_day(max(dated.values()))
    if pool_latest is None:
        return []
    stale = [
        {
            "code": code,
            "last_bar": last,
            "lag_days": stale_days(last, pool_latest),
        }
        for code, last in dated.items()
    ]
    return sorted(
        (e for e in stale if e["lag_days"] > max_lag),
        key=lambda e: (-e["lag_days"], e["code"]),
    )


def load_names() -> dict[str, str]:
    """QM 港股名称表（backend 侧唯一出处）；导入失败退化为空表（picks 的 name 留空）。"""
    sys.path.insert(0, str(ROOT))
    try:
        from backend.services.agent_arena.stock_names import HK_STOCK_NAMES

        return dict(HK_STOCK_NAMES)
    except Exception as exc:  # noqa: BLE001 名称只是展示面，缺了不该打断出池
        print(f"⚠️ 名称表不可用（{type(exc).__name__}: {exc}），picks.name 留空")
        return {}


# ---------------------------------------------------------------- 组装


def build_doc(
    stats: list[dict],
    *,
    top: int,
    names: dict[str, str],
    latest_bar: str,
    today: date,
    generated_at: str,
    excluded_stale: list[dict] | None = None,
) -> dict:
    """评分结果 → 落盘文档（纯函数：不碰网络、不读时间）。

    ``excluded_stale`` 是 ``stale_symbols`` 的剔除台账：生产上要能回答
    「今天为什么少了某只」。
    """
    picks = [
        {**s, "name": names.get(s["code"], "")} for s in stats[: max(int(top), 0)]
    ]
    return {
        "date": today.strftime("%Y-%m-%d"),
        "generated_at": generated_at,
        "data_date": latest_bar,
        "stale_days": stale_days(latest_bar, today),
        "market_direction": market_direction(stats),
        "universe_size": len(stats),
        "excluded_stale": list(excluded_stale or []),
        "picks": picks,
    }


def fetch_universe(symbols: list[str], *, days: int = 400, sleep_s: float = 0.3) -> dict:
    """逐只抓取（限速 sleep_s）；单只失败打印并继续，返回 ``{code: bars}``。"""
    end = now_cn().strftime("%Y-%m-%d")
    start = (now_cn() - timedelta(days=days)).strftime("%Y-%m-%d")
    bars_map: dict[str, list[dict]] = {}
    for symbol in symbols:
        try:
            bars = fetch_kline(symbol, start, end)
        except Exception as exc:  # noqa: BLE001 单只失败不打断全池
            print(f"❌ {symbol}: {type(exc).__name__}: {exc}")
            time.sleep(sleep_s)
            continue
        if not bars:
            print(f"⚠️  {symbol}: 无数据")
            time.sleep(sleep_s)
            continue
        bars_map[symbol] = bars
        print(f"✅ {symbol}: {len(bars)} 根 ({bars[0]['date']} ~ {bars[-1]['date']})")
        time.sleep(sleep_s)
    return bars_map


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="港股候选池（恒指权重股动量评分）")
    ap.add_argument("--top", type=int, default=20, help="候选池数量，默认 20")
    ap.add_argument("--symbols", default="", help="逗号分隔覆盖股票池（调试用）")
    ap.add_argument("--sleep", type=float, default=0.3, help="每只之间的限速秒数")
    ap.add_argument("--out", default="", help="输出路径覆盖（默认 data/hk_picks.json）")
    args = ap.parse_args(argv)

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] or DEFAULT_SYMBOLS
    bars_map = fetch_universe(symbols, sleep_s=args.sleep)
    if not bars_map:
        print("❌ 全部标的抓取失败（腾讯接口不可达？）——保留上一份 hk_picks.json 不动")
        return 1

    excluded = stale_symbols(bars_map)
    stats = score_universe(bars_map)
    if not stats:
        print(f"❌ 可评分样本不足（每只需 ≥{MIN_BARS} 根日 K）——保留上一份 hk_picks.json 不动")
        return 1
    if excluded:
        detail = "、".join(f"{e['code']}({e['last_bar']}, 落后 {e['lag_days']}d)" for e in excluded)
        print(
            f"⚠️  剔除 {len(excluded)} 只陈旧标的"
            f"（末根 K 落后全池最新交易日 >{MAX_BAR_LAG_WEEKDAYS} 个工作日）：{detail}"
        )

    today = now_cn().date()
    doc = build_doc(
        stats,
        top=args.top,
        names=load_names(),
        latest_bar=data_date(bars_map),
        today=today,
        generated_at=now_cn().isoformat(),
        excluded_stale=excluded,
    )
    out_file = Path(args.out) if args.out else OUT_FILE
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(
        json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(
        f"\n候选池 top {len(doc['picks'])}（大盘 {doc['market_direction']}；"
        f"数据日 {doc['data_date']}，陈旧 {doc['stale_days']} 个交易日）→ "
        f"{out_file.relative_to(ROOT) if out_file.is_relative_to(ROOT) else out_file}"
    )
    for p in doc["picks"][:10]:
        print(
            f"  {p['score']:5.1f}  {p['code']} {p['name'] or '—':<8} "
            f"mom20 {p['mom20']:+.1f}%  mom60 {p['mom60']:+.1f}%"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
