"""黑名单股票 × `评分A` 模型推理 Top100 的半年体检（只读分析脚本）。

口径（写死在代码里，避免每次口头解释不同）：
- **评分 A 模型**：`eval_scores` 每模型最新快照 `grade='A'`（今天 2026-11-28 的视角），
  并剔除 `qm_user_models` 里**仅剩 archived** 的模型（历史推断仍算分时口径，但不计入本次活跃 A）；
- **推理批次**：`qm_model_inference_runs` `status='completed'`，
  `prediction_trade_date` 落在窗口内；同一模型同一天有多个 run 时取 `created_at` 最新；
- **Top100**：`engine_signal_scores`（`source='batch'`, `market='CN'`）按
  `fusion_score` 降序、同日同 run 排名取前 100；symbol 是 6 位裸码，统一转后缀式；
- **收益**：信号日（= `prediction_trade_date`）**开盘买**，持有 H 个交易日，
  T+H 开盘卖；数据来自 QuantDB `daily_forward`（open→open，与实盘力 TopN 同口径）；
- **黑名单**：`load_exclusion_list('CN').symbols()` 当前生效集合（blocking 且未过期）。
  **注意：名单只有当前快照，用它回溯半年带前视**，结论只用于「这些票值不值得放行」，
  不能当无偏回测。

输出：控制台打印 + `data/blacklist_top100_study_<yyyymmdd>.json` 摘要。
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import psycopg2

from backend.shared.exclusion_list import (
    BLOCKING_SOURCES,
    load_exclusion_list,
    source_label,
)
from backend.shared.quantdb_paths import resolve_quantdb_subdir
from backend.shared.stock_name_mapper import resolve_name
from backend.shared.stock_utils import StockCodeUtil

START = "2026-05-28"
END = "2026-11-27"
HORIZONS = (5, 10, 20)
TOP_N = 100
TOP_STRICT = 20

DB = dict(
    host=os.environ.get("DB_HOST", "db"),
    port=int(os.environ.get("DB_PORT", "5432")),
    dbname=os.environ.get("DB_NAME", "quantmind"),
    user=os.environ.get("DB_USER", "quantmind"),
    password=os.environ.get("DB_PASSWORD", ""),
)

TOP_SQL = """
WITH latest AS (
  SELECT DISTINCT ON (object_id)
         object_id, grade, score, snapshot_date
  FROM eval_scores
  WHERE object_type = 'model'
  ORDER BY object_id, snapshot_date DESC
), a_models AS (
  SELECT object_id AS model_id, score AS eval_score, snapshot_date
  FROM latest
  WHERE grade = 'A'
), archived_only AS (
  SELECT model_id FROM qm_user_models
  GROUP BY model_id
  HAVING bool_and(status = 'archived')
), canon AS (
  SELECT r.run_id, r.model_id, r.prediction_trade_date,
         ROW_NUMBER() OVER (
           PARTITION BY r.model_id, r.prediction_trade_date
           ORDER BY r.created_at DESC, r.run_id DESC
         ) AS rn
  FROM qm_model_inference_runs r
  JOIN a_models a ON a.model_id = r.model_id
  WHERE r.status = 'completed'
    AND r.prediction_trade_date BETWEEN %s AND %s
    AND r.model_id NOT IN (SELECT model_id FROM archived_only)
), scores AS (
  SELECT s.run_id, s.symbol, MAX(s.fusion_score) AS score
  FROM engine_signal_scores s
  JOIN canon c ON c.run_id = s.run_id AND c.rn = 1
  WHERE s.market = 'CN'
    AND s.source = 'batch'
    AND s.fusion_score IS NOT NULL
  GROUP BY 1, 2
), ranked AS (
  SELECT run_id, symbol, score,
         ROW_NUMBER() OVER (
           PARTITION BY run_id ORDER BY score DESC, symbol
         ) AS rk
  FROM scores
)
SELECT c.model_id, c.prediction_trade_date, a.eval_score, a.snapshot_date,
       r.symbol, r.score, r.rk
FROM ranked r
JOIN canon c ON c.run_id = r.run_id AND c.rn = 1
JOIN a_models a ON a.model_id = c.model_id
WHERE r.rk <= %s
"""


UNIVERSE_SQL = """
WITH latest AS (
  SELECT DISTINCT ON (object_id)
         object_id, grade
  FROM eval_scores
  WHERE object_type = 'model'
  ORDER BY object_id, snapshot_date DESC
), a_models AS (
  SELECT object_id AS model_id FROM latest WHERE grade = 'A'
), archived_only AS (
  SELECT model_id FROM qm_user_models
  GROUP BY model_id
  HAVING bool_and(status = 'archived')
), canon AS (
  SELECT r.run_id, r.model_id, r.prediction_trade_date,
         ROW_NUMBER() OVER (
           PARTITION BY r.model_id, r.prediction_trade_date
           ORDER BY r.created_at DESC, r.run_id DESC
         ) AS rn
  FROM qm_model_inference_runs r
  JOIN a_models a ON a.model_id = r.model_id
  WHERE r.status = 'completed'
    AND r.prediction_trade_date BETWEEN %s AND %s
    AND r.model_id NOT IN (SELECT model_id FROM archived_only)
)
SELECT c.model_id, c.prediction_trade_date,
       COUNT(*) AS n_total,
       COUNT(*) FILTER (WHERE s.symbol = ANY(%s)) AS n_black
FROM engine_signal_scores s
JOIN canon c ON c.run_id = s.run_id AND c.rn = 1
WHERE s.market = 'CN'
  AND s.source = 'batch'
  AND s.fusion_score IS NOT NULL
GROUP BY 1, 2
"""


def _load_universe_share(black_codes: list[str]) -> pd.DataFrame:
    with psycopg2.connect(**DB) as conn:
        return pd.read_sql(
            UNIVERSE_SQL, conn, params=(START, END, black_codes)
        )


def _stats(values: np.ndarray) -> dict:
    obs = values[np.isfinite(values)]
    n = int(obs.size)
    if n == 0:
        return {"n": 0}
    mean = float(obs.mean())
    std = float(obs.std(ddof=1)) if n > 1 else float("nan")
    t = float(mean / (std / np.sqrt(n))) if n > 1 and std > 0 else float("nan")
    return {
        "n": n,
        "mean": round(mean, 6),
        "median": round(float(np.median(obs)), 6),
        "win_rate": round(float((obs > 0).mean()), 4),
        "std": round(std, 6) if np.isfinite(std) else None,
        "t_naive": round(t, 2) if np.isfinite(t) else None,
    }


def _load_top100() -> pd.DataFrame:
    with psycopg2.connect(**DB) as conn:
        frame = pd.read_sql(
            TOP_SQL, conn, params=(START, END, TOP_N)
        )
    frame["prediction_trade_date"] = pd.to_datetime(
        frame["prediction_trade_date"]
    )
    frame["symbol_suffix"] = frame["symbol"].map(
        lambda s: StockCodeUtil.to_suffix(str(s))
    )
    return frame


def _load_model_names(model_ids: list[str]) -> dict[str, str]:
    if not model_ids:
        return {}
    with psycopg2.connect(**DB) as conn:
        frame = pd.read_sql(
            """
            SELECT DISTINCT model_id,
                   COALESCE(metadata_json->>'display_name',
                            metadata_json->>'model_name', '') AS name,
                   COALESCE(metadata_json->>'market', '') AS market
            FROM qm_user_models
            WHERE model_id = ANY(%s)
            """,
            conn,
            params=(model_ids,),
        )
    out: dict[str, str] = {}
    for row in frame.itertuples(index=False):
        if row.model_id not in out and row.name:
            out[row.model_id] = str(row.name)
    return out


def _load_prices(start: str, end: str) -> pd.DataFrame:
    root = Path(resolve_quantdb_subdir("1_kline_data", "daily_forward"))
    lo = int(pd.Timestamp(start).strftime("%Y%m%d")) - 10
    hi = int(
        (pd.Timestamp(end) + pd.Timedelta(days=45)).strftime("%Y%m%d")
    )
    parts = sorted(
        p.name.split("=", 1)[1]
        for p in root.glob("dt=*")
        if p.name.split("=", 1)[1].isdigit() and lo <= int(p.name.split("=", 1)[1]) <= hi
    )
    globs = [str(root / f"dt={d}" / "*.parquet") for d in parts]
    con = duckdb.connect()
    frame = con.execute(
        """
        SELECT symbol, dt, open
        FROM read_parquet($paths)
        WHERE open IS NOT NULL AND open > 0
        """,
        {"paths": globs},
    ).df()
    return frame


def _attach_forward_returns(
    signals: pd.DataFrame, prices: pd.DataFrame
) -> pd.DataFrame:
    """给每条信号补 T+1..T+H 开盘收益（信号日开盘买，T+H 开盘卖）。"""
    calendar = sorted(int(d) for d in prices["dt"].unique())
    idx = {d: i for i, d in enumerate(calendar)}
    pivot = prices.pivot(index="symbol", columns="dt", values="open")
    symbols = list(pivot.index)
    sym_pos = {s: i for i, s in enumerate(symbols)}
    values = pivot.to_numpy(dtype=float)
    trade_dt = signals["prediction_trade_date"].dt.strftime("%Y%m%d").astype(int)
    signals = signals.assign(trade_dt=trade_dt)
    for h in HORIZONS:
        result = np.full(len(signals), np.nan)
        for d, group in signals.groupby("trade_dt"):
            entry = idx.get(int(d))
            if entry is None or entry + h >= len(calendar):
                continue
            exit_pos = entry + h
            rows = group.index.to_numpy()
            sym_rows = np.array(
                [sym_pos.get(s, -1) for s in group["symbol_suffix"]], dtype=int
            )
            ok = sym_rows >= 0
            if not ok.any():
                continue
            result[rows[ok]] = (
                values[sym_rows[ok], exit_pos] / values[sym_rows[ok], entry] - 1.0
            )
        signals[f"ret_t{h}"] = result
    return signals


def _daily_matched(
    signals: pd.DataFrame, ret_col: str, top_n: int, flag: pd.Series
) -> dict:
    """同模型同日的「命中组均值 − 其余组均值」，按日聚类的差与 t。"""
    base = signals[signals["rk"] <= top_n].dropna(subset=[ret_col]).copy()
    if base.empty:
        return {"days": 0}
    base["_flag"] = flag.reindex(base.index).fillna(False).astype(bool)
    grouped = (
        base.groupby(["model_id", "trade_date", "_flag"])[ret_col]
        .mean()
        .unstack("_flag")
    )
    if True not in grouped.columns or False not in grouped.columns:
        return {"days": 0}
    diff = (grouped[True] - grouped[False]).dropna()
    if diff.empty:
        return {"days": 0}
    obs = diff.to_numpy(dtype=float)
    std = float(obs.std(ddof=1)) if obs.size > 1 else float("nan")
    t = float(obs.mean() / (std / np.sqrt(obs.size))) if std > 0 else float("nan")
    return {
        "days": int(obs.size),
        "mean_diff": round(float(obs.mean()), 6),
        "median_diff": round(float(np.median(obs)), 6),
        "win_rate": round(float((obs > 0).mean()), 4),
        "t_day_cluster": round(t, 2) if np.isfinite(t) else None,
    }


def main() -> None:
    blacklist = load_exclusion_list("CN")
    if blacklist is None:
        raise SystemExit("排除名单未导入，无法分析")
    black = blacklist.symbols()
    black_codes = sorted({str(s).split(".")[0] for s in black})
    print(f"[名单] 基准日 {blacklist.asof}，生效 {len(black)} 只 / 总条目 {len(blacklist.items)}")

    signals = _load_top100()
    signals["trade_date"] = signals["prediction_trade_date"].dt.strftime("%Y-%m-%d")
    signals["is_blacklisted"] = signals["symbol_suffix"].isin(black)
    prices = _load_prices(START, END)
    signals = _attach_forward_returns(signals, prices)

    model_ids = sorted(signals["model_id"].unique())
    names = _load_model_names(model_ids)

    days = signals[["model_id", "prediction_trade_date"]].drop_duplicates()
    per_day = (
        signals.groupby(["model_id", "prediction_trade_date"])["is_blacklisted"]
        .sum()
    )
    total_slots = len(signals)
    black_slots = int(signals["is_blacklisted"].sum())
    top20 = signals[signals["rk"] <= TOP_STRICT]
    black_top20 = int(top20["is_blacklisted"].sum())
    universe = _load_universe_share(black_codes)
    universe_total = int(universe["n_total"].sum())
    universe_black = int(universe["n_black"].sum())
    universe_share = universe_black / universe_total
    top100_share = black_slots / total_slots
    top20_share = black_top20 / len(top20)
    ever_black = sorted(
        signals.loc[signals["is_blacklisted"], "symbol_suffix"].unique()
    )
    top_black = Counter(
        signals.loc[signals["is_blacklisted"], "symbol_suffix"]
    ).most_common(20)

    print("\n== 覆盖 ==")
    print(
        f"A 级模型 {len(model_ids)} 个 · 模型-日 {len(days)} 个 · "
        f"Top100 槽位 {total_slots:,} · 黑名单槽位 {black_slots:,} "
        f"({black_slots / total_slots:.2%})"
    )
    print(
        f"出现过黑名单的模型-日 {int((per_day > 0).sum()):,} / {len(per_day):,} "
        f"({(per_day > 0).mean():.1%})；平均每日 {per_day.mean():.2f} 只，"
        f"中位 {per_day.median():.0f}，P90 {per_day.quantile(0.9):.0f}"
    )
    print(
        f"打分池（每 run 全部打分行）黑名单占比 {universe_share:.2%}；"
        f"Top100 占比 {top100_share:.2%}（选择比 {top100_share / universe_share:.2f}）；"
        f"Top20 占比 {top20_share:.2%}（选择比 {top20_share / universe_share:.2f}）"
    )
    print(
        f"半年内进过 Top100 的黑名单股票 {len(ever_black)} 只 / 名单 {len(black)} 只 "
        f"（{len(ever_black) / len(black):.1%}）"
    )
    print("Top10 上榜次数：")
    for sym, cnt in top_black[:10]:
        print(f"  {sym} {resolve_name(sym) or '':<8} {cnt:,}")

    # 分来源：同一只票可命中多个源，这里按「命中该源」分别统计，不做互斥归因
    symbol_sources = {
        sym: set(hit.sources) for sym, hit in blacklist.items.items()
    }
    source_report: dict = {}
    print("\n== 分来源有效性（Top100 信号级；同一票可多源命中）==")
    for source in sorted(BLOCKING_SOURCES):
        mask = signals["symbol_suffix"].map(
            lambda s, src=source: src in symbol_sources.get(s, ())
        )
        n = int(mask.sum())
        if n == 0:
            continue
        entry: dict = {"signals": n}
        for h in HORIZONS:
            entry[f"t{h}"] = _stats(
                signals.loc[mask, f"ret_t{h}"].to_numpy(dtype=float)
            )
        for h in (5, 10):
            entry[f"t{h}_matched"] = _daily_matched(
                signals, f"ret_t{h}", TOP_N, mask
            )
        source_report[source] = entry
        print(
            f"  {source_label(source):<16} 信号 {n:>6} · "
            f"T+5 {entry['t5'].get('mean')} · T+10 {entry['t10'].get('mean')} · "
            f"配对差 T+5 {entry['t5_matched'].get('mean_diff')} "
            f"(t={entry['t5_matched'].get('t_day_cluster')}) · "
            f"T+10 {entry['t10_matched'].get('mean_diff')} "
            f"(t={entry['t10_matched'].get('t_day_cluster')})"
        )

    monthly = (
        signals.assign(month=signals["prediction_trade_date"].dt.strftime("%Y-%m"))
        .groupby("month")["is_blacklisted"]
        .agg(["count", "sum"])
    )
    monthly["share"] = monthly["sum"] / monthly["count"]
    print("\n== 月度 Top100 黑名单占比 ==")
    for month, row in monthly.iterrows():
        print(
            f"  {month}: {int(row['sum']):,}/{int(row['count']):,} "
            f"= {row['share']:.2%}"
        )

    print("\n== 按模型推理买入的收益（T+1 开盘买，持有 H 日）==")
    report: dict = {
        "generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window": [START, END],
        "blacklist": {
            "asof": blacklist.asof,
            "blocking": len(black),
            "total_items": len(blacklist.items),
        },
        "coverage": {
            "a_models": len(model_ids),
            "model_days": int(len(days)),
            "top100_slots": total_slots,
            "blacklist_slots": black_slots,
            "blacklist_share": round(black_slots / total_slots, 6),
            "universe_slots": universe_total,
            "universe_black_slots": universe_black,
            "universe_black_share": round(universe_share, 6),
            "top20_black_share": round(top20_share, 6),
            "selection_ratio_top100": round(top100_share / universe_share, 4),
            "selection_ratio_top20": round(top20_share / universe_share, 4),
            "model_days_with_black": int((per_day > 0).sum()),
            "avg_black_per_model_day": round(float(per_day.mean()), 3),
            "distinct_black_in_top100": len(ever_black),
            "distinct_black_total": len(black),
        },
        "monthly": {
            month: {
                "slots": int(row["count"]),
                "black": int(row["sum"]),
                "share": round(float(row["share"]), 4),
            }
            for month, row in monthly.iterrows()
        },
        "top_symbols": [
            {"symbol": s, "name": resolve_name(s) or "", "count": int(c)}
            for s, c in top_black
        ],
        "source_effectiveness": source_report,
        "returns": {},
        "matched_vs_non_black": {},
    }

    for label, mask in (
        ("blacklist", signals["is_blacklisted"]),
        ("non_blacklist", ~signals["is_blacklisted"]),
    ):
        block = {}
        for h in HORIZONS:
            col = f"ret_t{h}"
            block[f"t{h}_all_top100"] = _stats(
                signals.loc[mask, col].to_numpy(dtype=float)
            )
            block[f"t{h}_top20"] = _stats(
                signals.loc[mask & (signals["rk"] <= TOP_STRICT), col].to_numpy(
                    dtype=float
                )
            )
        report["returns"][label] = block
        print(f"\n[{label}]")
        for key, value in block.items():
            print(f"  {key}: {value}")

    for h in HORIZONS:
        col = f"ret_t{h}"
        for top_n in (TOP_N, TOP_STRICT):
            key = f"t{h}_top{top_n}"
            report["matched_vs_non_black"][key] = _daily_matched(
                signals, col, top_n, signals["is_blacklisted"]
            )
    print("\n== 同日同模型 黑名单 − 非黑名单（Top100 / Top20）==")
    for key, value in report["matched_vs_non_black"].items():
        print(f"  {key}: {value}")

    per_model = []
    for model_id, group in signals[signals["is_blacklisted"]].groupby("model_id"):
        row = {"model_id": model_id, "name": names.get(model_id, "")}
        row["black_signals"] = int(len(group))
        row["avg_black_per_day"] = round(
            len(group) / group["prediction_trade_date"].nunique(), 2
        )
        for h in (5, 10):
            row[f"t{h}"] = _stats(group[f"ret_t{h}"].to_numpy(dtype=float))
        per_model.append(row)
    per_model.sort(key=lambda r: -(r["t5"].get("mean") or -9))
    report["per_model"] = per_model
    print("\n== 各模型黑名单信号（按 T+5 均值排序，前 10 / 后 5）==")
    for row in per_model[:10] + per_model[-5:]:
        print(
            f"  {row['model_id'][-24:]:<24} {row['name'][:30]:<30} "
            f"黑信号 {row['black_signals']:>5} · T+5 {row['t5'].get('mean')} "
            f"(n={row['t5'].get('n')}) · T+10 {row['t10'].get('mean')}"
        )

    out = Path("/data") / (
        "blacklist_top100_study_"
        + pd.Timestamp.now().strftime("%Y%m%d_%H%M")
        + ".json"
    )
    out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n[输出] {out}")


if __name__ == "__main__":
    main()
