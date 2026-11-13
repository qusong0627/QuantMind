"""港股聪明钱 · 事件层（CCASS 席位 Δ持仓事件 + 南向直读流 + 逐日编排）。

口径（细案 §3.1/§3.2；全部常量在包 ``__init__``）：

- **差分只在两日皆在 top50 的 (席位, 标的) 对上做**——first_seen / exit_boundary
  不产生事件也不进统计，仅计数披露（top50 截断噪音）；
- **三条件入事件闸**：|Δvalue|≥1M HKD ∧ |Δqty|/qty_prev≥5% ∧ close≥0.5 HKD；
  事件仓保留全部合格事件；逐日类型资金流用**全部非嫌疑**事件求和；
- **过户嫌疑**：单席单日 |Δvalue| > 1.2×当日该股成交额（场内不可能）→
  ``transfer_suspect`` 打标：出流与统计、不触碰 episode 状态、计数披露；
- **episode 冷却**：同席同股同向 5 交易日内归并为一条 episode
  （``episode_lead``=1 标记 episode 首日、续存事件 0；连续事件刷新冷却；反向
  事件独立成立并重置；断档 >5 日独立成新 episode）。命中率/席位统计只用
  lead==1（细案 §3.1/§3.4）；
- **南向**：hsgt_south 是完整持仓表（缺席=0 合法，与 ccass 的 first_seen 不同），
  逐日 Δqty×close 直读成 ``category=southbound`` 流；CCASS 侧 A 席
  （A00002/03/04/06）与 hkscc 全局排除，防双重计数；A 席合计与南向总量对账；
- **事后收益**：fwd/exc 封存列，只作展示与统计，绝不进任何信号构造。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backend.shared.hk_smart_money import (
    EPISODE_COOLDOWN_DAYS,
    EPISODE_SEED_DAYS,
    EVENT_MIN_PCT,
    EVENT_MIN_VALUE_HKD,
    FWD_HORIZONS,
    MIN_PRICE_HKD,
    SOUTHBOUND_SEAT_IDS,
    TRANSFER_AMOUNT_RATIO,
)
from backend.shared.hk_smart_money import datasets as ds

log = logging.getLogger(__name__)

#: parquet schema 固定 dtype（分区并读 union 的前提：空分区也须同 schema）
_EVENT_DTYPES: dict[str, str] = {
    "dt": "object",
    "symbol": "object",
    "stock_name": "object",
    "participant_id": "object",
    "participant_name": "object",
    "category": "object",
    "kind": "object",
    "direction": "int64",
    "delta_qty": "int64",
    "delta_value": "float64",
    "delta_pct": "float64",
    "price": "float64",
    "day_amount": "float64",
    "transfer_suspect": "bool",
    "episode_lead": "int64",
    **{f"fwd_ret_{h}": "float64" for h in FWD_HORIZONS},
    **{f"fwd_exc_{h}": "float64" for h in FWD_HORIZONS},
}
_FLOW_DTYPES: dict[str, str] = {
    "dt": "object",
    "symbol": "object",
    "category": "object",
    "net_value": "float64",
    "net_qty": "int64",
    "n_up": "int64",
    "n_dn": "int64",
    "n_active": "int64",
}
_HOLDINGS_DTYPES: dict[str, str] = {
    "dt": "object",
    "symbol": "object",
    "category": "object",
    "qty_sum": "int64",
    "n_seats": "int64",
}


def _empty(dtypes: dict[str, str]) -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=t) for c, t in dtypes.items()})


class EventBuilder:
    """逐日事件提取器（跨日持有 episode 状态；分类器惰性加载一次）。"""

    def __init__(
        self,
        close: pd.DataFrame,
        amount: pd.DataFrame,
        fwd: dict[int, tuple[pd.DataFrame, pd.DataFrame]],
        data_dir: Path | str | None = None,
        min_value_hkd: float | None = None,
        min_pct: float | None = None,
        min_price: float | None = None,
    ) -> None:
        """阈值默认取包常量；显式传入仅供标定网格（validate --calibrate）。"""
        self._close = close
        self._amount = amount
        self._fwd = fwd
        self._min_value = (
            EVENT_MIN_VALUE_HKD if min_value_hkd is None else float(min_value_hkd)
        )
        self._min_pct = EVENT_MIN_PCT if min_pct is None else float(min_pct)
        self._min_price = MIN_PRICE_HKD if min_price is None else float(min_price)
        from backend.services.api.market_analysis_hk import (
            institutional_classifier as ic,
        )

        self._ic = ic
        self._overrides = ic.load_overrides(data_dir or ds.data_dir())
        self._cls_cache: dict[tuple[str, str], tuple[str, str]] = {}
        #: (participant_id, symbol) → (最后命中 idx, direction)：episode 冷却
        self._episodes: dict[tuple[str, str], tuple[int, int]] = {}

    # ------------------------------------------------------------ 分类

    def _cls(self, pid: str, name: str) -> tuple[str, str]:
        key = (pid, name)
        cls = self._cls_cache.get(key)
        if cls is None:
            cls = self._ic.classify(pid, name, self._overrides)
            self._cls_cache[key] = cls
        return cls

    # ------------------------------------------------------------ 单日事件

    def process_day(
        self,
        dt: str,
        prev: pd.DataFrame | None,
        cur: pd.DataFrame,
        day_idx: int,
    ) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any], pd.DataFrame, pd.DataFrame]:
        """单日提取。返回 (events, flows, counts, universe, holdings)。

        prev=None（序列首日）→ 无差分，仅 universe/holdings/计数。行情缺该日 →
        counts ``priced=False``，调用方跳过该日产物（下轮重试）。
        """
        ts = pd.Timestamp(dt)
        priced = ts in self._close.index
        universe = pd.DataFrame({"symbol": np.sort(cur["stock_code"].unique())})
        a_qty = int(
            cur.loc[
                cur["participant_id"].isin(SOUTHBOUND_SEAT_IDS), "holding_quantity"
            ].sum()
        )
        counts: dict[str, Any] = {
            "rows_prev": 0 if prev is None else int(len(prev)),
            "rows_cur": int(len(cur)),
            "in_both_n": 0,
            "first_seen_n": 0,
            "exit_n": 0,
            "raw_moves_n": 0,
            "events_n": 0,
            "suspect_n": 0,
            "flows_rows": 0,
            "a_seat_qty_sum": a_qty,
            "priced": bool(priced),
            "day_idx": day_idx,
        }
        if not priced:
            return (
                _empty(_EVENT_DTYPES),
                _empty(_FLOW_DTYPES),
                counts,
                universe,
                _empty(_HOLDINGS_DTYPES),
            )

        # 分类（唯一席位一次；事件差分与持仓聚合共用映射）
        uniq = cur[["participant_id", "participant_name"]].drop_duplicates(
            "participant_id"
        )
        cls_by_pid = {
            pid: self._cls(pid, name)
            for pid, name in zip(
                uniq["participant_id"], uniq["participant_name"], strict=True
            )
        }
        cat_map = {pid: c for pid, (c, _k) in cls_by_pid.items()}
        kind_map = {pid: k for pid, (_c, k) in cls_by_pid.items()}

        # 类型持仓（CCASS 5 类；信号定义域「当日或前日 qty>0」与 overview 持仓市值）
        hold = cur.assign(category=cur["participant_id"].map(cat_map))
        hold = hold[~hold["category"].isin(("southbound", "hkscc"))]
        holdings = (
            hold.groupby(["stock_code", "category"], as_index=False)
            .agg(
                qty_sum=("holding_quantity", "sum"),
                n_seats=("holding_quantity", "size"),
            )
            .rename(columns={"stock_code": "symbol"})
        )
        holdings.insert(0, "dt", dt)
        holdings = holdings[list(_HOLDINGS_DTYPES)].astype(_HOLDINGS_DTYPES)

        if prev is None:
            return (
                _empty(_EVENT_DTYPES),
                _empty(_FLOW_DTYPES),
                counts,
                universe,
                holdings,
            )

        merged = prev[["participant_id", "stock_code", "holding_quantity"]].merge(
            cur[
                [
                    "participant_id",
                    "stock_code",
                    "stock_name",
                    "participant_name",
                    "holding_quantity",
                ]
            ],
            on=["participant_id", "stock_code"],
            how="inner",
            suffixes=("_prev", "_cur"),
        )
        counts["in_both_n"] = int(len(merged))
        counts["first_seen_n"] = int(len(cur) - len(merged))
        counts["exit_n"] = int(len(prev) - len(merged))

        merged = merged[
            merged["holding_quantity_cur"] != merged["holding_quantity_prev"]
        ].copy()
        counts["raw_moves_n"] = int(len(merged))
        if merged.empty:
            return (
                _empty(_EVENT_DTYPES),
                _empty(_FLOW_DTYPES),
                counts,
                universe,
                holdings,
            )

        # 排除南向 A 席与 hkscc 托管池（防与 hsgt_south 双重计数）
        merged["category"] = merged["participant_id"].map(cat_map)
        merged["kind"] = merged["participant_id"].map(kind_map)
        merged = merged[~merged["category"].isin(("southbound", "hkscc"))]

        day_close = self._close.loc[ts]
        day_amount = (
            self._amount.loc[ts]
            if ts in self._amount.index
            else pd.Series(dtype="float64")
        )
        price = merged["stock_code"].map(day_close)
        amount = merged["stock_code"].map(day_amount)
        dq = (merged["holding_quantity_cur"] - merged["holding_quantity_prev"]).astype(
            "int64"
        )
        dv = dq * price
        qty_prev = merged["holding_quantity_prev"]
        pct = dq / qty_prev.where(qty_prev > 0)

        ok = (
            (dv.abs() >= self._min_value)
            & (pct.abs() >= self._min_pct)
            & (price >= self._min_price)
        )
        kept = merged[ok].copy()
        if kept.empty:
            return (
                _empty(_EVENT_DTYPES),
                _empty(_FLOW_DTYPES),
                counts,
                universe,
                holdings,
            )
        kept["price"] = price[ok]
        kept["day_amount"] = amount[ok]
        kept["delta_qty"] = dq[ok]
        kept["delta_value"] = dv[ok].astype("float64")
        kept["delta_pct"] = pct[ok].astype("float64")
        kept["direction"] = np.where(kept["delta_qty"] > 0, 1, -1).astype("int64")
        kept["suspect"] = (kept["day_amount"] > 0) & (
            kept["delta_value"].abs() > TRANSFER_AMOUNT_RATIO * kept["day_amount"]
        )
        counts["events_n"] = int(len(kept))
        counts["suspect_n"] = int(kept["suspect"].sum())

        # episode：非嫌疑事件更新状态（首日 lead=1，续存 0）；嫌疑不触碰
        leads = pd.Series(0, index=kept.index, dtype="int64")
        for idx, row in kept[~kept["suspect"]].iterrows():
            key = (row["participant_id"], row["stock_code"])
            direction = int(row["direction"])
            state = self._episodes.get(key)
            if (
                state is not None
                and state[1] == direction
                and day_idx - state[0] <= EPISODE_COOLDOWN_DAYS
            ):
                self._episodes[key] = (day_idx, direction)
            else:
                leads.at[idx] = 1
                self._episodes[key] = (day_idx, direction)
        kept["episode_lead"] = leads

        # 类型资金流：全部非嫌疑事件求和（嫌疑出流，计数披露）
        valid = kept[~kept["suspect"]]
        flows = _empty(_FLOW_DTYPES)
        if not valid.empty:
            agg = valid.assign(
                is_up=(valid["direction"] > 0).astype("int64"),
                is_dn=(valid["direction"] < 0).astype("int64"),
            ).groupby(["stock_code", "category"], as_index=False)
            flows = agg.agg(
                net_value=("delta_value", "sum"),
                net_qty=("delta_qty", "sum"),
                n_up=("is_up", "sum"),
                n_dn=("is_dn", "sum"),
            )
            flows["n_active"] = flows["n_up"] + flows["n_dn"]
            flows = flows.rename(columns={"stock_code": "symbol"})
            flows.insert(0, "dt", dt)
            flows = flows[list(_FLOW_DTYPES)]
            flows = flows.astype(_FLOW_DTYPES)
        counts["flows_rows"] = int(len(flows))

        # 事件仓列装配 + 事后收益封存列
        events = pd.DataFrame(
            {
                "dt": dt,
                "symbol": kept["stock_code"],
                "stock_name": kept["stock_name"],
                "participant_id": kept["participant_id"],
                "participant_name": kept["participant_name"],
                "category": kept["category"],
                "kind": kept["kind"],
                "direction": kept["direction"],
                "delta_qty": kept["delta_qty"],
                "delta_value": kept["delta_value"],
                "delta_pct": kept["delta_pct"],
                "price": kept["price"],
                "day_amount": kept["day_amount"],
                "transfer_suspect": kept["suspect"],
                "episode_lead": kept["episode_lead"],
            }
        )
        for h in FWD_HORIZONS:
            ret, exc = self._fwd[h]
            events[f"fwd_ret_{h}"] = (
                events["symbol"].map(ret.loc[ts]) if ts in ret.index else np.nan
            )
            events[f"fwd_exc_{h}"] = (
                events["symbol"].map(exc.loc[ts]) if ts in exc.index else np.nan
            )
        events = events[list(_EVENT_DTYPES)].astype(_EVENT_DTYPES)
        return events, flows, counts, universe, holdings

    # ------------------------------------------------------------ 南向直读

    def south_flows(
        self, dt: str, prev_south: pd.DataFrame | None, cur_south: pd.DataFrame
    ) -> pd.DataFrame:
        """南向逐日净流（完整持仓表口径：缺席=0，双向 fillna(0) 合法）。

        prev_south=None（南向序列首日，或无更早分区）→ 无差分，返回空。
        """
        if prev_south is None:
            return _empty(_FLOW_DTYPES)
        q = pd.DataFrame(
            {
                "prev": prev_south.set_index("symbol")["holding_quantity"],
                "cur": cur_south.set_index("symbol")["holding_quantity"],
            }
        ).fillna(0.0)
        dq = (q["cur"] - q["prev"]).round().astype("int64")
        dq = dq[dq != 0]
        if dq.empty:
            return _empty(_FLOW_DTYPES)
        ts = pd.Timestamp(dt)
        price = (
            self._close.loc[ts].reindex(dq.index)
            if ts in self._close.index
            else pd.Series(np.nan, index=dq.index)
        )
        flows = pd.DataFrame(
            {
                "symbol": dq.index,
                "category": "southbound",
                "net_value": dq.to_numpy() * price.to_numpy(),
                "net_qty": dq.to_numpy(),
                "n_up": (dq > 0).astype("int64").to_numpy(),
                "n_dn": (dq < 0).astype("int64").to_numpy(),
                "n_active": 1,
            }
        )
        flows.insert(0, "dt", dt)
        return flows[list(_FLOW_DTYPES)].astype(_FLOW_DTYPES)


def write_day_artifacts(
    dt: str,
    events: pd.DataFrame,
    flows: pd.DataFrame,
    universe: pd.DataFrame,
    holdings: pd.DataFrame,
) -> None:
    """单日四件套原子落盘。

    布局唯一出处：``events/dt=*/{data,flows,universe,holdings}.parquet``。
    读侧（``datasets.load_events/load_flows/load_universe/load_holdings``）必须
    与此一致——历史上 flows 曾按顶层 ``flows/`` 目录读而永远落空（写读缝，
    2026-10-16 修复），改动此处须同步 ``datasets._artifact_paths`` 的调用。
    """
    base = ds.product_dir() / "events" / f"dt={dt}"
    ds.write_parquet_atomic(events, base / "data.parquet")
    ds.write_parquet_atomic(flows, base / "flows.parquet")
    ds.write_parquet_atomic(universe, base / "universe.parquet")
    ds.write_parquet_atomic(holdings, base / "holdings.parquet")


def build_events(rebuild_days: int = 40) -> dict[str, Any]:
    """事件仓编排（增量或全量），逐日原子写 events/flows/counts/universe。

    - ``rebuild_days=0``：全量重建（474 分区逐日差分，实测全量首建 15-20 分钟）；
    - ``rebuild_days=N``：重算最近 N 个交易日 ∪ 产物缺失日；episode 状态从窗口
      前 ``EPISODE_SEED_DAYS`` 个交易日回放补齐（种子日只推状态、不写产物）；
    - 行情缺该交易日 → 跳过不写（下轮重试），计入 ``days_skipped_unpriced``。
    """
    t0 = time.monotonic()
    dates = list(ds.ccass_dates())
    if not dates:
        raise RuntimeError("ccass_top50 无分区，无法构建事件仓")
    existing = set(ds.event_dates())
    if rebuild_days <= 0:
        todo = list(dates)
    else:
        todo = sorted(
            set(dates[-rebuild_days:]) | {d for d in dates if d not in existing}
        )
    write_from = dates.index(todo[0])
    seed_lo = max(0, write_from - EPISODE_SEED_DAYS) if rebuild_days > 0 else 0

    kd = [d for d in ds.kline_dates() if d >= dates[0]]
    close, amount = ds.load_prices(kd)
    fwd = ds.forward_matrices(close)
    builder = EventBuilder(close, amount, fwd)

    south_set = set(ds.south_dates())
    stats: dict[str, Any] = {
        "days_total": len(dates),
        "days_written": 0,
        "days_seeded": 0,
        "days_skipped_unpriced": 0,
        "events_total": 0,
        "suspect_total": 0,
        "south_flow_rows": 0,
        "reconciliation": None,
    }
    prev: pd.DataFrame | None = None
    prev_south: pd.DataFrame | None = None
    last_cur_ts: str | None = None
    for i in range(seed_lo, len(dates)):
        dt = dates[i]
        cur = ds.load_ccass(dt)
        cur_south = ds.load_south(dt) if dt in south_set else None
        events, flows, counts, universe, holdings = builder.process_day(
            dt, prev, cur, i
        )
        if i < write_from:
            stats["days_seeded"] += 1
        elif not counts["priced"]:
            stats["days_skipped_unpriced"] += 1
            log.warning("行情缺 %s，跳过该日产物（下轮重试）", dt)
        else:
            if cur_south is not None:
                sf = builder.south_flows(dt, prev_south, cur_south)
                counts["south_flow_rows"] = int(len(sf))
                counts["south_qty_sum"] = int(cur_south["holding_quantity"].sum())
                counts["south_available"] = True
                # 对账必须在**同一股票集合**上做：CCASS 只存 top50，部分股票的
                # 中国结算席位排在 50 名之外（2026-11-12 有 86 只），拿全量南向
                # 对比会凭空少 3%。这里取两边都可见的交集，才是真对账。
                a_series = (
                    cur.loc[cur["participant_id"].isin(SOUTHBOUND_SEAT_IDS)]
                    .groupby("stock_code")["holding_quantity"]
                    .sum()
                )
                s_series = cur_south.set_index("symbol")["holding_quantity"]
                common = a_series.index.intersection(s_series.index)
                counts["a_seat_qty_common"] = int(a_series.loc[common].sum())
                counts["south_qty_common"] = int(s_series.loc[common].sum())
                counts["south_common_n"] = int(len(common))
                counts["south_total_n"] = int(len(s_series))
                if len(sf):
                    flows = pd.concat([flows, sf], ignore_index=True)
            else:
                counts["south_flow_rows"] = 0
                counts["south_available"] = False
            write_day_artifacts(dt, events, flows, universe, holdings)
            _write_counts(dt, counts)
            stats["days_written"] += 1
            stats["events_total"] += counts["events_n"]
            stats["suspect_total"] += counts["suspect_n"]
            stats["south_flow_rows"] += counts.get("south_flow_rows", 0)
            last_cur_ts = dt
        prev = cur
        if cur_south is not None:
            prev_south = cur_south
    # 南向 vs A 席对账（数据质量门：逐股 ±2%，聚合应更紧）
    if last_cur_ts is not None:
        counts_last = _read_counts(last_cur_ts)
        # 优先同一股票集合的对账（见上方 common 计算）；旧 counts 无该字段时
        # 退回全量口径，仅用于兼容历史产物。
        a_qty = counts_last.get("a_seat_qty_common") or counts_last.get("a_seat_qty_sum")
        s_qty = counts_last.get("south_qty_common") or counts_last.get("south_qty_sum")
        if a_qty and s_qty:
            stats["reconciliation"] = {
                "as_of": last_cur_ts,
                "a_seat_qty_sum": a_qty,
                "south_qty_sum": s_qty,
                "ratio": round(a_qty / s_qty, 4),
                "universe": "common_top50" if "a_seat_qty_common" in counts_last else "all_south",
                "common_n": counts_last.get("south_common_n"),
                "south_n": counts_last.get("south_total_n"),
            }
            if abs(a_qty / s_qty - 1.0) > 0.02:
                log.warning(
                    "南向对账偏差 >2%%（A 席 %s vs 南向 %s，as_of %s）",
                    a_qty,
                    s_qty,
                    last_cur_ts,
                )
    stats["duration_s"] = round(time.monotonic() - t0, 1)
    log.info("事件仓构建完成：%s", stats)
    return stats


def _write_counts(dt: str, counts: dict[str, Any]) -> None:
    path = ds.product_dir() / "events" / f"dt={dt}" / "counts.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(counts, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _read_counts(dt: str) -> dict[str, Any]:
    path = ds.product_dir() / "events" / f"dt={dt}" / "counts.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def read_counts(dt: str) -> dict[str, Any]:
    """读逐日计数（events/dt=/counts.json）；缺失/损坏返回空 dict。"""
    return _read_counts(dt)
