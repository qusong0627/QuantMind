"""市场状态日更持久化（P3 · 设计 §6.2）：指数历史 → ``qm_regime_daily``（生效日口径）。

- **计算单源**：``shared/market_regime.build_state_rows``（i→i+1 标注，与日频/日内同口径）；
- **写入冻结**：``shared/regime_daily_store.insert_frozen_rows``（冲突不覆盖，
  ``computed_at`` 留痕；重跑同一批生效日 = 0 新行）；
- **索引映射**：``market_regime.REGIME_INDEX_BY_MARKET``（CN=000300.SH / HK=HSI.HK /
  US=SPX.US）。表外的市场（CRYPTO/FUTURES）**诚实拒绝**——没有指数口径可依，
  绝不拿 000300 冒充；索引数据取不到就不写行；
- **尾部重算**：日更每次重算最近 ``TAIL_EFFECTIVE_DATES`` 个生效日，把迟到/修订
  数据补进尚未定型的尾部；更早的历史行保持冻结（回填走 CLI 一次性写入）。
- **尾行（§6.4 必要条件）**：数据末根 bar 额外标注到「其下一交易日」（
  ``trading_calendar.next_trading_day_xcal``，日历给定）——节假日/周末期间数据里
  没有下一根 bar，自然配对写不出「次日生效」行，次日盘前推理就拿不到当日生效值。
  日历答不了（越界/库缺失）就不写这一行并告警，**绝不按自然日猜**；数据补齐后
  自然配对会算出同一状态 → 冻结写入冲突、天然幂等（无前视：只用到末根 bar 为止的数据）。

数据源按市场：CN 走 QuantDB 中枢 ``qdb_index_daily``（与日内 regime 服务同源同表）；
HK/US 走各自市场根目录的 ``1_kline_data/index_daily`` 分区（hive dt=*/data.parquet）。

接线：beat ``regime-daily-persist``（交易日 16:30，``REGIME_PERSIST_ENABLED`` 默认 on）
→ ``engine.tasks.regime_daily_persist``（心跳 ``regime_persist``）→ ``persist_all()``；
存量回填 ``python backend/scripts/regime_backfill.py --market CN --from 2018-01-01``。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from backend.shared.market_regime import (
    DEFAULT_WINDOW,
    REGIME_INDEX_BY_MARKET,
    build_state_rows,
    thresholds_fingerprint,
)

logger = logging.getLogger(__name__)

#: 每日重算的尾部生效日数（吸收迟到数据；历史行冻结不重算）
TAIL_EFFECTIVE_DATES = 10
#: 日更覆盖的市场（表外市场传入时单市场诚实拒绝）
DEFAULT_MARKETS: tuple[str, ...] = ("CN", "HK", "US")
#: CN hub 取数下界：早于库里最早行不影响结果（取到啥算啥）
_CN_HISTORY_FLOOR = "20050101"


def _iso(value: Any) -> str:
    """归一为 ``YYYY-MM-DD``（date/datetime/str 都收）。"""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def _load_cn_history(symbol: str) -> dict[str, list]:
    """CN：QuantDB 中枢 ``qdb_index_daily``（与 realtime_regime 同源）。"""
    from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
    from backend.shared.stock_utils import StockCodeUtil

    hub = QuantDBDataHub.get_instance()
    today = date.today().strftime("%Y%m%d")
    df = hub.fetch_series(
        "qdb_index_daily",
        StockCodeUtil.to_suffix(symbol),
        _CN_HISTORY_FLOOR,
        today,
        columns=["close", "volume"],
    )
    if df is None or len(df) == 0:
        return {"dates": [], "closes": [], "volumes": []}
    if "dt" in df.columns:
        df = df.sort_values("dt")

        def _label(v: Any) -> str:
            text = str(int(v))
            return f"{text[:4]}-{text[4:6]}-{text[6:8]}"

        dates = [_label(v) for v in df["dt"].tolist()]
    else:  # pragma: no cover - hub 契约带 dt 列；无 dt 时按索引时间兜底
        dates = [_iso(d) for d in df.index]
    closes = [float(v) for v in df["close"].tolist()]
    volumes = [float(v) for v in df["volume"].tolist()] if "volume" in df.columns else []
    return {"dates": dates, "closes": closes, "volumes": volumes}


def _load_partitioned_history(market: str, symbol: str) -> dict[str, list]:
    """HK/US：各自市场根的 ``1_kline_data/index_daily`` hive 分区。"""
    import duckdb

    from backend.services.engine.data_platform.quantdb_factor_reader import (
        market_data_dir,
    )

    part_dir = market_data_dir(market) / "1_kline_data" / "index_daily"
    glob = str(part_dir / "dt=*" / "data.parquet")
    con = duckdb.connect()
    try:
        df = con.execute(
            "SELECT time, close, volume FROM read_parquet(?) "
            "WHERE symbol = ? ORDER BY time",
            [glob, symbol],
        ).fetchdf()
    finally:
        con.close()
    if df.empty:
        return {"dates": [], "closes": [], "volumes": []}
    dates = [_iso(v) for v in df["time"].tolist()]
    closes = [float(v) for v in df["close"].tolist()]
    volumes = [float(v) for v in df["volume"].tolist()]
    return {"dates": dates, "closes": closes, "volumes": volumes}


def load_index_history(market: str, symbol: str) -> dict[str, list]:
    """该市场 regime 指数的日线历史（dates 升序，与 closes/volumes 等长）。"""
    if market == "CN":
        return _load_cn_history(symbol)
    return _load_partitioned_history(market, symbol)


async def persist_market_regime(
    market: str,
    *,
    backfill_from: Any = None,
    backfill_to: Any = None,
    tail: int | None = TAIL_EFFECTIVE_DATES,
    dry_run: bool = False,
) -> dict[str, Any]:
    """单市场持久化；返回摘要 dict（supported/reason/inserted/existed/日期范围）。

    选择语义：给了 ``backfill_from``/``backfill_to`` 就按区间过滤（回填模式，
    ``tail`` 忽略）；否则重算尾部 ``tail`` 个生效日（日更模式）。
    """
    from backend.shared.regime_daily_store import insert_frozen_rows

    market_upper = str(market or "").upper().strip()
    symbol = REGIME_INDEX_BY_MARKET.get(market_upper)
    base: dict[str, Any] = {"market": market_upper or str(market), "index": symbol}
    if not symbol:
        return {
            **base,
            "supported": False,
            "reason": (
                "no_regime_index_vocabulary——表外市场没有指数口径，"
                f"诚实拒绝（可持久化市场：{sorted(REGIME_INDEX_BY_MARKET)}）"
            ),
        }
    base["supported"] = True
    history = load_index_history(market_upper, symbol)
    dates = history["dates"]
    base["bars"] = len(dates)
    base["last_bar"] = dates[-1] if dates else None
    if len(dates) < DEFAULT_WINDOW + 2:
        return {**base, "rows": 0, "inserted": 0, "existed": 0,
                "reason": "index_history_insufficient"}

    # 尾行生效日 = 数据末根 bar 的下一交易日（日历给定；答不了就不写，见模块 docstring）
    tail_effective_date: str | None = None
    try:
        from backend.shared.trading_calendar import next_trading_day_xcal

        next_session = next_trading_day_xcal(market_upper, date.fromisoformat(str(dates[-1])[:10]))
        if next_session is not None:
            tail_effective_date = next_session.isoformat()
    except Exception as exc:  # noqa: BLE001 — 日历答不了 = 少一行尾行，绝不猜
        logger.warning("regime 尾行日历计算异常 market=%s: %s", market_upper, exc)
    if tail_effective_date is None:
        logger.warning(
            "regime 尾行跳过 market=%s：日历答不了 %s 的下一交易日（本次少写一行生效值，"
            "次日盘前该市场 regime 可能缺行）",
            market_upper, dates[-1],
        )
    base["tail_effective_date"] = tail_effective_date

    rows = build_state_rows(
        history["closes"], history["volumes"], dates,
        tail_effective_date=tail_effective_date,
    )
    if backfill_from is not None or backfill_to is not None:
        lo = _iso(backfill_from) if backfill_from is not None else None
        hi = _iso(backfill_to) if backfill_to is not None else None
        rows = [
            row for row in rows
            if (lo is None or row["effective_date"] >= lo)
            and (hi is None or row["effective_date"] <= hi)
        ]
    elif tail:
        rows = rows[-int(tail):]
    if not rows:
        return {**base, "rows": 0, "inserted": 0, "existed": 0,
                "reason": "no_effective_rows"}

    fingerprint = thresholds_fingerprint()
    span = {"first_date": rows[0]["effective_date"], "last_date": rows[-1]["effective_date"],
            "thresholds_hash": fingerprint}
    if dry_run:
        return {**base, **span, "rows": len(rows), "dry_run": True, "reason": "ok"}
    result = await insert_frozen_rows(market_upper, rows, thresholds_hash=fingerprint)
    return {**base, **span, **result, "rows": result["total"], "reason": "ok"}


async def persist_all(
    markets: tuple[str, ...] = DEFAULT_MARKETS,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """逐市场持久化：单市场失败记入摘要并继续（不拖累其它市场）。"""
    summaries: list[dict[str, Any]] = []
    for market in markets:
        try:
            summaries.append(await persist_market_regime(market, **kwargs))
        except Exception as exc:  # noqa: BLE001 — 记入摘要，绝不静默
            logger.exception("regime 持久化失败 market=%s", market)
            summaries.append(
                {
                    "market": str(market).upper().strip(),
                    "supported": True,
                    "reason": f"error: {type(exc).__name__}: {exc}"[:300],
                }
            )
    return summaries
