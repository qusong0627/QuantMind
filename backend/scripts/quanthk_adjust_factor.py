#!/usr/bin/env python3
"""QuantHK 复权因子链 + daily_backward（Yahoo Adj Close 版，2026-11-14 重写）。

背景
----
旧实现依赖 akshare ``stock_hk_hist``（东方财富）取 ``preclose``：
1. 该端点 2026-11 起对容器 IP 返回 RemoteDisconnected（实测），每晚增量全部
   失败，`daily_backward` 只写到 2026-05-08（8-28 之后的分区只有 ~130 只）；
2. 东财 preclose 是「昨收」，不含现金分红，复权链会漏掉股息。

新口径
------
- 复权比例来自 Yahoo ``Adj Close / Close``：按分红/拆股阶跃，实测在非行动日
  完全稳定（00700：2025-05-16/2026-05-15 两次分红各一次阶跃）；
- 原始 OHLCV 复用本地 ``daily_forward``（Sina），不重复抓行情；
- 以既有因子文件在重叠日的值做比例锚（scale），历史段保持不变、只续新日期；
- 只重写「最后一个完整覆盖日之后」的 daily_backward 分区（增量），默认不碰
  历史；``--full`` 才全量重写。

用法
----
    python backend/scripts/quanthk_adjust_factor.py            # 增量
    python backend/scripts/quanthk_adjust_factor.py --refetch  # 强制重抓全部标的因子
    python backend/scripts/quanthk_adjust_factor.py --full     # 全量重写 daily_backward
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("quanthk_adj")

QUANTHK_DATA_DIR = Path(
    os.getenv("QM_QUANTHK_DATA_DIR", str(PROJECT_ROOT / "data" / "quanthk"))
)
PAID_DIR = QUANTHK_DATA_DIR / "_paid_raw"
FORWARD_DIR = QUANTHK_DATA_DIR / "1_kline_data" / "daily_forward"
BACKWARD_DIR = QUANTHK_DATA_DIR / "1_kline_data" / "daily_backward"
FACTOR_DIR = QUANTHK_DATA_DIR / "2_base_sector" / "adjust_factors"

KLINE_COLS = [
    "symbol", "time", "open", "high", "low", "close",
    "volume", "amount", "release_id", "published_at",
]
YAHOO_CHUNK = int(os.getenv("HK_YAHOO_CHUNK", "80"))
_now_iso = pd.Timestamp.now().isoformat()


# ---------------------------------------------------------------- 目录/IO


def _max_partition_dt(root: Path) -> str | None:
    if not root.is_dir():
        return None
    ds = [p.name[3:] for p in root.glob("dt=*") if (p / "data.parquet").is_file()]
    return max(ds) if ds else None


def _partition_dts(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(
        p.name[3:] for p in root.glob("dt=*") if (p / "data.parquet").is_file()
    )


def _atomic_write(df: pd.DataFrame, path: Path) -> None:
    tmp = path.with_name(f"{path.name}.tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(path)


def _read_paid(code5: str) -> pd.DataFrame | None:
    f = PAID_DIR / f"{code5}.csv"
    if not f.exists():
        return None
    try:
        df = pd.read_csv(f, encoding="gbk", low_memory=False)
    except Exception:  # noqa: BLE001
        df = pd.read_csv(f, encoding="utf-8", low_memory=False)
    df = df.rename(
        columns={
            "交易日期": "time", "开盘价": "open", "最高价": "high", "最低价": "low",
            "收盘价": "close", "昨收盘": "preclose", "成交量": "volume", "成交额": "amount",
        }
    )
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    for c in ("open", "high", "low", "close", "preclose", "volume", "amount"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["time", "close"]).sort_values("time")


def _paid_factor(code5: str) -> pd.DataFrame | None:
    """付费 CSV → 因子表（旧口径，仅用于没有既有因子文件的标的）。"""
    paid = _read_paid(code5)
    if paid is None or paid.empty:
        return None
    prev = paid["close"].shift(1)
    step = paid["preclose"] / prev
    step = step.where(prev.notna() & paid["preclose"].notna() & (step > 0), 1.0)
    step = np.where(np.abs(step - 1.0) < 1e-4, 1.0, step)
    factor = step.cumprod()
    out = pd.DataFrame(
        {
            "symbol": code5,
            "time": paid["time"].dt.date,
            "close_raw": paid["close"],
            "preclose": paid["preclose"],
            "adj_step": step,
            "adj_factor": factor,
        }
    )
    return out


def _load_factor(path: Path) -> pd.DataFrame | None:
    if not path.is_file():
        return None
    try:
        df = pd.read_parquet(path)
    except Exception:  # noqa: BLE001
        return None
    if not {"time", "close_raw", "adj_factor"}.issubset(df.columns):
        return None
    df = df.copy()
    df["time"] = pd.to_datetime(df["time"], errors="coerce").dt.date
    df = df.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
    return df


# ---------------------------------------------------------------- Yahoo


def _fetch_yahoo_ratios(
    tickers: list[str], start: str, end: str
) -> dict[str, pd.DataFrame]:
    """批量抓 Yahoo 日线，返回 ticker → DataFrame(date, close, ratio)。"""
    import yfinance as yf

    out: dict[str, pd.DataFrame] = {}
    for i in range(0, len(tickers), YAHOO_CHUNK):
        chunk = tickers[i : i + YAHOO_CHUNK]
        data = None
        for attempt in range(3):
            try:
                data = yf.download(
                    chunk,
                    start=start,
                    end=end,
                    auto_adjust=False,
                    actions=False,
                    progress=False,
                    group_by="ticker",
                    threads=True,
                )
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("Yahoo 批次 %d 失败（第 %d 次）: %s", i, attempt + 1, str(exc)[:120])
        if data is None or data.empty:
            log.warning("Yahoo 批次 %d/%d 无数据", i, len(tickers))
            continue
        for tk in chunk:
            try:
                if isinstance(data.columns, pd.MultiIndex):
                    if tk not in set(data.columns.get_level_values(0)):
                        continue
                    df = data[tk]
                else:
                    df = data
                if "Close" not in df.columns or "Adj Close" not in df.columns:
                    continue
                d = pd.DataFrame(
                    {
                        "date": pd.to_datetime(df.index).tz_localize(None).normalize(),
                        "close": pd.to_numeric(df["Close"], errors="coerce"),
                        "adj_close": pd.to_numeric(df["Adj Close"], errors="coerce"),
                    }
                ).dropna()
                if d.empty:
                    continue
                d["ratio"] = d["adj_close"] / d["close"]
                d = d.loc[np.isfinite(d["ratio"]) & (d["ratio"] > 0)]
                if d.empty:
                    continue
                out[tk] = d[["date", "close", "ratio"]].sort_values("date").reset_index(drop=True)
            except Exception as exc:  # noqa: BLE001
                log.debug("Yahoo 解析 %s 失败: %s", tk, exc)
        log.info("Yahoo 进度 %d/%d", min(i + YAHOO_CHUNK, len(tickers)), len(tickers))
    return out


def _update_factor(
    code5: str, base: pd.DataFrame | None, yahoo: pd.DataFrame | None
) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    """把 Yahoo 比例接到既有因子序列后面；返回 (完整因子表, 新行)。"""
    if base is None or base.empty or yahoo is None or yahoo.empty:
        return base, None
    base_last = base["time"].max()
    base_last_factor = float(base["adj_factor"].iloc[-1])
    yahoo = yahoo.copy()
    yahoo["date"] = pd.to_datetime(yahoo["date"]).dt.date
    yahoo = yahoo.sort_values("date").reset_index(drop=True)

    overlap = yahoo.loc[yahoo["date"] <= base_last]
    if len(overlap):
        scale = base_last_factor / float(overlap["ratio"].iloc[-1])
        first_prev_close = float(overlap["close"].iloc[-1])
    else:
        # 无重叠：用 Yahoo 首个比例归一（只保证自身连续，不回写历史）
        scale = base_last_factor / float(yahoo["ratio"].iloc[0])
        first_prev_close = float(base["close_raw"].iloc[-1])

    new = yahoo.loc[yahoo["date"] > base_last].copy()
    if new.empty:
        return base, None
    new["adj_factor"] = new["ratio"] * scale
    prev_close = new["close"].shift(1)
    prev_close.iloc[0] = first_prev_close
    prev_factor = new["adj_factor"].shift(1)
    prev_factor.iloc[0] = base_last_factor
    new["preclose"] = prev_close
    new["adj_step"] = new["adj_factor"] / prev_factor.replace(0, np.nan)
    new = new.rename(columns={"date": "time", "close": "close_raw"})
    new.insert(0, "symbol", code5)
    new = new[["symbol", "time", "close_raw", "preclose", "adj_step", "adj_factor"]]
    if "symbol" not in base.columns:
        base = base.copy()
        base.insert(0, "symbol", code5)
    full = pd.concat([base, new], ignore_index=True).sort_values("time")
    full["symbol"] = full["symbol"].fillna(code5)
    full = full.drop_duplicates("time", keep="last").reset_index(drop=True)
    return full, new


# ---------------------------------------------------------------- daily_backward


def _factor_from_yahoo(code5: str, yahoo: pd.DataFrame | None) -> pd.DataFrame | None:
    """没有付费历史的新标的：直接用 Yahoo Adj/Close 比例建因子（最早=1）。

    只保证同一标的内部连续；绝对价格水平无跨标的可比性，但收益率正确。
    """
    if yahoo is None or yahoo.empty:
        return None
    df = yahoo.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = (
        df.sort_values("date")
        .drop_duplicates("date", keep="last")
        .reset_index(drop=True)
    )
    ratio = pd.to_numeric(df["ratio"], errors="coerce")
    valid = ratio.notna() & (ratio > 0)
    df = df.loc[valid].reset_index(drop=True)
    ratio = ratio.loc[valid].reset_index(drop=True)
    if df.empty:
        return None
    factor = ratio / float(ratio.iloc[0])
    prev_close = df["close"].shift(1)
    prev_factor = factor.shift(1)
    step = factor / prev_factor
    step.iloc[0] = 1.0
    return pd.DataFrame(
        {
            "symbol": code5,
            "time": df["date"],
            "close_raw": df["close"],
            "preclose": prev_close,
            "adj_step": step,
            "adj_factor": factor,
        }
    )


def _last_full_backward_date() -> str | None:
    """从后往前找最后一个「覆盖接近历史峰值」的 daily_backward 分区。

    不能用固定 90%×daily_forward：forward 含 ~500 个无付费因子的 ETF/窝轮，
    backward 永远覆盖不到；用最近 160 个分区的**最大覆盖数**做基准（自身
    历史峰值），≥95% 峰值即视为完整。
    """
    bwd = _partition_dts(BACKWARD_DIR)
    fwd = set(_partition_dts(FORWARD_DIR))
    window = [d for d in bwd[-160:] if d in fwd]
    counts: dict[str, int] = {}
    for dt in window:
        try:
            b = pd.read_parquet(
                BACKWARD_DIR / f"dt={dt}" / "data.parquet", columns=["symbol"]
            )
        except Exception:  # noqa: BLE001
            continue
        counts[dt] = int(b["symbol"].nunique())
    if not counts:
        return None
    peak = max(counts.values())
    if peak <= 0:
        return None
    for dt in reversed(window):
        if counts.get(dt, 0) >= 0.95 * peak:
            return dt
    return None


def _write_backward(
    rebuild_dates: list[str],
    factor_panel: dict[str, dict[str, float]],
) -> int:
    """按日重写 daily_backward（价格×因子；量/额保留不复权）。

    因子按日从 factor_panel 取；某标的当日无因子行（停牌/未上市）则跳过该行。
    """
    written = 0
    for dt in rebuild_dates:
        fwd_path = FORWARD_DIR / f"dt={dt}" / "data.parquet"
        try:
            raw = pd.read_parquet(fwd_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] daily_forward 读取失败: %s", dt, exc)
            continue
        if raw.empty:
            continue
        raw = raw.drop_duplicates("symbol", keep="last").copy()
        factors = raw["symbol"].map(factor_panel.get(dt, {}))
        raw["factor"] = pd.to_numeric(factors, errors="coerce")
        missing = int(raw["factor"].isna().sum())
        raw = raw.loc[raw["factor"].notna() & raw["factor"].gt(0)]
        if raw.empty:
            log.warning("[%s] 无可对齐的复权因子，跳过（missing=%d）", dt, missing)
            continue
        out = pd.DataFrame(
            {
                "symbol": raw["symbol"],
                "time": pd.to_datetime(raw["time"]).dt.date,
                "open": raw["open"] * raw["factor"],
                "high": raw["high"] * raw["factor"],
                "low": raw["low"] * raw["factor"],
                "close": raw["close"] * raw["factor"],
                "volume": raw["volume"],
                "amount": raw["amount"],
                "release_id": "backward_adjusted",
                "published_at": _now_iso,
            }
        )[KLINE_COLS]
        pdir = BACKWARD_DIR / f"dt={dt}"
        pdir.mkdir(parents=True, exist_ok=True)
        _atomic_write(out, pdir / "data.parquet")
        written += 1
        if written % 20 == 0:
            log.info("daily_backward 进度 %d/%d（%s）", written, len(rebuild_dates), dt)
    return written


# ---------------------------------------------------------------- 主流程


def main() -> int:
    ap = argparse.ArgumentParser(description="QuantHK 复权因子 + daily_backward（Yahoo）")
    ap.add_argument("--refetch", action="store_true", help="强制重抓全部标的因子")
    ap.add_argument("--full", action="store_true", help="全量重写 daily_backward")
    ap.add_argument(
        "--since",
        default=None,
        help="从该日期之后重建 daily_backward（YYYY-MM-DD/YYYYMMDD）；默认自动探测",
    )
    args = ap.parse_args()

    from backend.shared.stock_utils import StockCodeUtil

    latest_forward = _max_partition_dt(FORWARD_DIR)
    if latest_forward is None:
        log.error("daily_forward 无分区，无法复权")
        return 1
    codes = sorted(p.stem for p in PAID_DIR.glob("*.csv"))
    if not codes:
        log.error("付费 CSV 为空: %s", PAID_DIR)
        return 1
    # 付费清单不含新上市/ETF/部分小票：把最新 daily_forward 的标的并入，
    # 用 Yahoo Adj/Close 直接建因子（无付费历史时），否则回测会少 20%+ 标的。
    latest_fwd_part = FORWARD_DIR / f"dt={latest_forward}" / "data.parquet"
    if latest_fwd_part.is_file():
        try:
            fwd_syms = (
                pd.read_parquet(latest_fwd_part, columns=["symbol"])["symbol"]
                .dropna()
                .astype(str)
            )
            fwd_codes = {
                s[:-3].zfill(5) for s in fwd_syms if s.endswith(".HK")
            }
            codes = sorted(set(codes) | fwd_codes)
        except Exception as exc:  # noqa: BLE001
            log.warning("读取 daily_forward 标的池失败，仅用付费清单: %s", exc)

    factor_path = {
        c: FACTOR_DIR / f"{StockCodeUtil.to_hk_suffix(c)}.parquet" for c in codes
    }
    ends: dict[str, str] = {}
    starts_override: dict[str, str] = {}
    todo: list[str] = []
    since_s = pd.Timestamp(args.since).strftime("%Y%m%d") if args.since else None

    for c in codes:
        fp = factor_path[c]
        f = _load_factor(fp)
        if f is None or f.empty:
            todo.append(c)
            continue
        end_s = pd.Timestamp(f["time"].max()).strftime("%Y%m%d")
        min_s = pd.Timestamp(f["time"].min()).strftime("%Y%m%d")
        ends[c] = end_s
        # 无付费历史的新标的（上一轮只用 Yahoo 建了近期因子）：若因子起点晚于
        # 目标研究起点，用 Yahoo 全段重建，否则回测窗口前段没有复权价。
        need_early = bool(
            since_s
            and min_s > since_s
            and not (PAID_DIR / f"{c}.csv").exists()
        )
        if args.refetch or end_s < latest_forward or need_early:
            todo.append(c)
            if need_early:
                starts_override[c] = since_s

    log.info(
        "复权因子：%d 只待更新（forward=%s，已最新 %d）",
        len(todo),
        latest_forward,
        len(codes) - len(todo),
    )

    if args.since:
        rebuild_from = pd.Timestamp(args.since).strftime("%Y%m%d")
    else:
        rebuild_from = None if args.full else _last_full_backward_date()
    if rebuild_from:
        log.info("daily_backward 增量起点：%s 之后", rebuild_from)
    else:
        log.info("daily_backward 全量重写（未找到完整分区）")

    yahoo_map: dict[str, pd.DataFrame] = {}
    if todo:
        tickers = [StockCodeUtil.to_hk_suffix(c) for c in todo]
        # 起点不得早于「需要重写的 backward 起点」：否则一次会拉 20 年数据，
        # 大批量下载极慢且容易触发 Yahoo 限流。早于该起点的历史段已在
        # daily_backward 现存分区里，不需要重抓。
        floor = rebuild_from or "20260101"
        starts = [
            max(starts_override.get(c, ends.get(c, floor)), floor) for c in todo
        ]
        start_date = min(starts)
        start_dt = (
            datetime.strptime(start_date, "%Y%m%d") - timedelta(days=10)
        ).date()
        end_dt = date.today() + timedelta(days=1)
        yahoo_map = _fetch_yahoo_ratios(
            tickers, start_dt.isoformat(), end_dt.isoformat()
        )
        log.info("Yahoo 覆盖 %d/%d 只待更新标的", len(yahoo_map), len(todo))

    updated = 0
    no_yahoo = 0
    for c in todo:
        fp = factor_path[c]
        base = _load_factor(fp)
        if base is None:
            base = _paid_factor(c)
        tk = StockCodeUtil.to_hk_suffix(c)
        yahoo = yahoo_map.get(tk)
        if yahoo is None:
            no_yahoo += 1
        if base is None:
            yahoo_base = _factor_from_yahoo(c, yahoo)
            if yahoo_base is not None and not yahoo_base.empty:
                FACTOR_DIR.mkdir(parents=True, exist_ok=True)
                yahoo_base.assign(source="yahoo_adj").to_parquet(fp, index=False)
                updated += 1
            continue
        if c in starts_override:
            # 无付费历史、因子起点晚于研究起点：用 Yahoo 全段**重建**（替换）
            yahoo_base = _factor_from_yahoo(c, yahoo)
            if yahoo_base is not None and not yahoo_base.empty:
                FACTOR_DIR.mkdir(parents=True, exist_ok=True)
                yahoo_base.assign(source="yahoo_adj").to_parquet(fp, index=False)
                updated += 1
            continue
        full, new = _update_factor(c, base, yahoo)
        if full is None or full.empty:
            continue
        if new is not None and not new.empty:
            FACTOR_DIR.mkdir(parents=True, exist_ok=True)
            full.assign(source="paid+yahoo_adj").to_parquet(fp, index=False)
            updated += 1
        else:
            # 无新数据（Yahoo 缺该标的）：至少把付费段因子落盘，供 backward 续写
            if not fp.exists():
                FACTOR_DIR.mkdir(parents=True, exist_ok=True)
                full.assign(source="paid").to_parquet(fp, index=False)

    log.info("因子文件更新 %d 只（Yahoo 缺 %d）", updated, no_yahoo)

    # ---- 为保证 backward 连续，重新扫描全部因子文件，按日 reindex + ffill ----
    fwd_dts = _partition_dts(FORWARD_DIR)
    rebuild_dates = (
        fwd_dts
        if (args.full or rebuild_from is None)
        else [d for d in fwd_dts if d > rebuild_from]
    )
    date_index = pd.DatetimeIndex([pd.Timestamp(d) for d in rebuild_dates])
    factor_panel: dict[str, dict[str, float]] = {d: {} for d in rebuild_dates}
    for c in codes:
        fp = factor_path[c]
        if not fp.is_file():
            continue
        try:
            f = pd.read_parquet(fp, columns=["time", "adj_factor"])
        except Exception:  # noqa: BLE001
            continue
        f["time"] = pd.to_datetime(f["time"], errors="coerce")
        f = (
            f.dropna(subset=["time"])
            .drop_duplicates("time", keep="last")
            .sort_values("time")
        )
        if f.empty:
            continue
        series = f.set_index("time")["adj_factor"].astype("float64")
        vals = series.reindex(date_index, method="ffill")
        tk = StockCodeUtil.to_hk_suffix(c)
        for d, v in zip(rebuild_dates, vals, strict=False):
            if pd.notna(v):
                factor_panel[d][tk] = float(v)

    written = _write_backward(rebuild_dates, factor_panel)
    print(
        {
            "updated_factors": updated,
            "no_yahoo": no_yahoo,
            "rebuild_from": rebuild_from,
            "backward_partitions": written,
            "latest_forward": latest_forward,
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
