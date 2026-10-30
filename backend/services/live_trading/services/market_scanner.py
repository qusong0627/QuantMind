"""全市场扫描器（P1 影子模式，2026-10-30）。

数据：桥的全市场当日快照 ``quote:snap:{YYYYMMDD}``（5,577 只，5 分钟一轮）。
每轮全量进内存（~2MB）做横截面打分，产出候选池；**只写信号台账，不下任何单**。

打分（全部可复算，四项截面 z-score，±3 截断）：
    chg            = last / pre_close - 1                  日内动量
    pos_in_range   = (last - low) / (high - low)           日内位置（0=最低 1=最高）
    book_imbalance = (bid1_v - ask1_v) / (bid1_v + ask1_v) 买一/卖一压差
    amount                                                 成交额（流动性）
权重：0.35 / 0.20 / 0.25 / 0.20。

准入：price>0、pre_close>0、bid1/ask1 都在、成交额 ≥ min_amount（默认 500 万）、
涨幅 < max_chg（默认 9.5%，接近涨停买不到）。

持仓高位提示：``pos_in_range >= 0.9`` 且 ``book_imbalance < 0``（冲到日内高位但
买盘转弱）→ 记一条 advisory，交给 SLTP/人工判断，不自动卖。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DAY_KEY = "quote:snap:%s"
POSITIONS_KEY = "bigqmt:positions:%s"
LATEST_KEY = "qm:scanner:latest"
STREAM_KEY = "qm:scanner:signals:%s"

DEFAULT_INTERVAL_S = 60.0
DEFAULT_TOP_N = 50
DEFAULT_MIN_AMOUNT = 5_000_000.0
DEFAULT_MAX_CHG = 0.095
DEFAULT_HIGH_POS = 0.9
WEIGHTS = {"chg": 0.35, "pos": 0.20, "imb": 0.25, "amt": 0.20}


@dataclass(frozen=True)
class Candidate:
    code: str
    score: float
    chg: float
    pos_in_range: float
    book_imbalance: float
    amount: float
    last: float
    pre_close: float


def _f(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _z(values: list[float]) -> list[float]:
    """截面 z-score（±3 截断）；全相等时返回全 0。"""
    n = len(values)
    if n == 0:
        return []
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    std = var**0.5
    if std < 1e-12:
        return [0.0] * n
    return [max(-3.0, min(3.0, (v - mean) / std)) for v in values]


def _row_features(row: dict[str, Any]) -> dict[str, float] | None:
    """单只快照 → 因子向量；不满足准入返回 None。"""
    last = _f(row.get("last")) or _f(row.get("price"))
    pre = _f(row.get("pre_close"))
    high = _f(row.get("high"))
    low = _f(row.get("low"))
    amount = _f(row.get("amount")) or 0.0
    bids = [_f(row.get(f"bid{i}_p")) for i in range(1, 6)]
    asks = [_f(row.get(f"ask{i}_p")) for i in range(1, 6)]
    bid1_v = _f(row.get("bid1_v")) or 0.0
    ask1_v = _f(row.get("ask1_v")) or 0.0
    if not last or not pre or pre <= 0:
        return None
    if not bids[0] or not asks[0]:
        return None
    chg = last / pre - 1.0
    if high is not None and low is not None and high > low:
        pos = (last - low) / (high - low)
    else:
        pos = 0.5
    pos = max(0.0, min(1.0, pos))
    denom = bid1_v + ask1_v
    imb = (bid1_v - ask1_v) / denom if denom > 0 else 0.0
    return {
        "chg": chg,
        "pos": pos,
        "imb": imb,
        "amt": amount,
        "last": last,
        "pre_close": pre,
    }


def score_universe(
    snapshots: dict[str, dict[str, Any]],
    *,
    top_n: int = DEFAULT_TOP_N,
    min_amount: float = DEFAULT_MIN_AMOUNT,
    max_chg: float = DEFAULT_MAX_CHG,
    holdings: set[str] | None = None,
    high_pos: float = DEFAULT_HIGH_POS,
    names: dict[str, str] | None = None,
) -> dict[str, Any]:
    """全市场快照 → 候选池 + 持仓高位提示（纯函数）。

    ``names``（代码→简称）用于排 ST/退市：买不进也不该给买入候选。
    """
    holdings = holdings or set()
    names = names or {}
    codes: list[str] = []
    feats: list[dict[str, float]] = []
    for code, row in snapshots.items():
        name = str(names.get(code) or "")
        if "ST" in name or "退" in name:
            continue
        f = _row_features(row)
        if f is None:
            continue
        if f["amt"] < min_amount:
            continue
        if f["chg"] >= max_chg:
            continue
        codes.append(code)
        feats.append(f)
    if not codes:
        return {"universe": len(snapshots), "eligible": 0, "top": [], "holdings": []}

    z_chg = _z([f["chg"] for f in feats])
    z_pos = _z([f["pos"] for f in feats])
    z_imb = _z([f["imb"] for f in feats])
    z_amt = _z([f["amt"] for f in feats])

    scored: list[Candidate] = []
    for i, code in enumerate(codes):
        score = (
            WEIGHTS["chg"] * z_chg[i]
            + WEIGHTS["pos"] * z_pos[i]
            + WEIGHTS["imb"] * z_imb[i]
            + WEIGHTS["amt"] * z_amt[i]
        )
        f = feats[i]
        scored.append(
            Candidate(
                code=code,
                score=round(score, 6),
                chg=round(f["chg"], 6),
                pos_in_range=round(f["pos"], 4),
                book_imbalance=round(f["imb"], 4),
                amount=f["amt"],
                last=f["last"],
                pre_close=f["pre_close"],
            )
        )
    scored.sort(key=lambda c: c.score, reverse=True)

    by_code = {c.code: c for c in scored}
    holding_flags = [
        {
            "code": code,
            "pos_in_range": by_code[code].pos_in_range,
            "book_imbalance": by_code[code].book_imbalance,
            "chg": by_code[code].chg,
            "reason": "日内高位且买盘转弱（advisory，不自动卖）",
        }
        for code in sorted(holdings)
        if code in by_code
        and by_code[code].pos_in_range >= high_pos
        and by_code[code].book_imbalance < 0
    ]

    return {
        "universe": len(snapshots),
        "eligible": len(codes),
        "top": [c.__dict__ for c in scored[: max(0, int(top_n))]],
        "holdings": holding_flags,
    }


_NAMES_CACHE: dict[str, Any] = {"date": "", "names": {}}


def load_stock_names(trade_date: str) -> dict[str, str]:
    """PG ``stock_daily_latest`` 的代码→简称（进程内按日缓存）。

    只用于扫描器的 ST/退市过滤；取不到返回空字典（退化为不过滤，由下单侧
    的桥风控兜底）。同步驱动（psycopg2/psycopg）在 to_thread 里调用。
    """
    if _NAMES_CACHE.get("date") == trade_date and _NAMES_CACHE.get("names"):
        return _NAMES_CACHE["names"]
    dsn = (
        f"host={os.getenv('DB_HOST', 'db')} port={os.getenv('DB_PORT', '5432')} "
        f"dbname={os.getenv('DB_NAME', 'quantmind')} user={os.getenv('DB_USER', 'quantmind')} "
        f"password={os.getenv('DB_PASSWORD', '')}"
    )
    try:
        try:
            import psycopg

            conn = psycopg.connect(dsn)
        except ImportError:
            import psycopg2

            conn = psycopg2.connect(dsn)
        ymd = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
        with conn, conn.cursor() as cur:
            # 当日数据通常盘后才入库：取 **≤扫描日的最近交易日**，别按等号查空集
            # （2026-10-30 实测：查 10-30 得 0 行 → ST 过滤整体失效）。
            cur.execute(
                "SELECT symbol, stock_name FROM stock_daily_latest "
                "WHERE trade_date = (SELECT max(trade_date) FROM stock_daily_latest "
                "WHERE trade_date <= %s)",
                (ymd,),
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 - 名称缺失只影响 ST 过滤
        logger.warning("[MarketScanner] 读取股票简称失败: %s", exc)
        return {}
    from backend.shared.stock_utils import StockCodeUtil

    names = {
        str(sym).upper()
        if "." in str(sym)
        else StockCodeUtil.to_suffix(str(sym)).upper(): str(name or "")
        for sym, name in rows
    }
    _NAMES_CACHE.update({"date": trade_date, "names": names})
    logger.info("[MarketScanner] 简称加载 %d 只（基准日 ≤%s）", len(names), trade_date)
    return names


async def _bridge_redis():
    from backend.services.live_trading.services.quote_cache_reader import _bridge_redis

    return await _bridge_redis()


async def _load_snapshots(redis, date_str: str) -> dict[str, dict[str, Any]]:
    raw = await redis.hgetall(DAY_KEY % date_str)
    out: dict[str, dict[str, Any]] = {}
    for code, payload in (raw or {}).items():
        try:
            row = json.loads(payload) if isinstance(payload, str) else payload
        except (TypeError, ValueError):
            continue
        if isinstance(row, dict):
            out[str(code).upper()] = row
    return out


async def _load_holdings(redis, account: str) -> set[str]:
    if not account:
        return set()
    try:
        raw = await redis.get(POSITIONS_KEY % account)
        payload = json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        return set()
    positions = (payload or {}).get("positions") or {}
    return {
        str(code).upper()
        for code, row in positions.items()
        if (row or {}).get("volume")
    }


async def run_scan_once(
    *, date_str: str | None = None, persist: bool = True
) -> dict[str, Any]:
    """跑一轮扫描；``persist=True`` 时写 Redis 台账（影子，不下单）。"""
    date_str = date_str or time.strftime("%Y%m%d")
    account = str(os.getenv("QMT_EXEC_ACCOUNT_ID", "") or "").strip()
    redis = await _bridge_redis()
    try:
        if not account:
            try:
                from backend.services.live_trading.services.qmt_exec_client import (
                    QmtExecClient,
                )

                client = QmtExecClient.from_env()
                await client.refresh_settings()
                account = str(client.effective_config().get("account_id") or "")
            except Exception:  # noqa: BLE001
                account = ""
        snapshots = await _load_snapshots(redis, date_str)
        holdings = await _load_holdings(redis, account)
        names = await asyncio.to_thread(load_stock_names, date_str)
        started = time.perf_counter()
        result = score_universe(snapshots, holdings=holdings, names=names)
        result["date"] = date_str
        result["account"] = account
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
        result["ts"] = round(time.time(), 3)
        if persist:
            key = LATEST_KEY
            await redis.set(key, json.dumps(result, ensure_ascii=False), ex=86400)
            stream = STREAM_KEY % date_str
            for item in result["top"][:20]:
                await redis.xadd(
                    stream,
                    {
                        "payload": json.dumps(
                            {"kind": "candidate", **item}, ensure_ascii=False
                        )
                    },
                    maxlen=5000,
                    approximate=True,
                )
            for item in result["holdings"]:
                await redis.xadd(
                    stream,
                    {
                        "payload": json.dumps(
                            {"kind": "holding_high", **item}, ensure_ascii=False
                        )
                    },
                    maxlen=5000,
                    approximate=True,
                )
        return result
    finally:
        try:
            await redis.aclose()
        except Exception:  # noqa: BLE001
            pass


def session_active(now: float | None = None) -> bool:
    lt = time.localtime(now if now is not None else time.time())
    if lt.tm_wday >= 5:
        return False
    hhmm = lt.tm_hour * 100 + lt.tm_min
    return (930 <= hhmm <= 1130) or (1300 <= hhmm <= 1500)


async def run_market_scanner_task(interval_seconds: float | None = None) -> None:
    """常驻循环：交易时段内每 ``interval_seconds`` 扫一轮（默认 60s）。"""
    interval = float(
        interval_seconds or os.getenv("QM_MARKET_SCANNER_INTERVAL", DEFAULT_INTERVAL_S)
    )
    logger.info("[MarketScanner] started interval=%.0fs (shadow: 只写台账)", interval)
    while True:
        try:
            if session_active():
                result = await run_scan_once()
                logger.info(
                    "[MarketScanner] universe=%s eligible=%s top=%s holdings_flags=%s %.0fms",
                    result.get("universe"),
                    result.get("eligible"),
                    len(result.get("top") or []),
                    len(result.get("holdings") or []),
                    result.get("elapsed_ms") or 0,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 扫描循环必须活着
            logger.warning("[MarketScanner] loop error: %s", exc)
        await asyncio.sleep(max(10.0, interval))


if __name__ == "__main__":  # pragma: no cover - 手工跑一轮
    logging.basicConfig(level=logging.INFO)
    out = asyncio.run(run_scan_once())
    print(json.dumps(out, ensure_ascii=False, indent=1)[:4000])
