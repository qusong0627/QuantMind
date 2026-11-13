#!/usr/bin/env python3
"""港股聪明钱 · 证据裁定（pass1 类型效力；pass2 复合分回填）。

三脚本节奏（细案 docs/港股市场分析_聪明钱深度分析_细案_20261016.md §4）：
``--no-composite``（pass1：确保事件仓 → 24 候选信号双过闸 → evidence.json）→
``market_analysis/build_hk_smart_money.py``（逐日复合分）→
本脚本（pass2：读 scores 分区回填 evidence.composite）。

口径要点（细案 §3.3–§3.5；全部实现收在 ``backend.shared.hk_smart_money``）：
- 事件仓：pass1 需要 flows/holdings/events——仓库为空时本脚本直接触发全量首建
  （``build_events`` 的缺日并集语义），已有仓库默认增量刷新最近 40 个交易日；
- 双过闸/命中/不对称/ICIR 全部来自 ``stats``（与 hk_opportunity_ic_check 判据
  一字不改）；相关塌缩 |ρ|>0.8 保留 |IC| 强者；
- 席位统计（seats/月度/标的/持续性审计）自 2026-10-16 起由
  ``market_analysis/build_hk_smart_money.py`` 每日生成（原在此处，暗坑已修）；
- ``--calibrate``：事件闸阈值网格 {1M,2M,5M}×{3%,5%,10%} 抽样标定（只读原始
  分区、不写事件仓），网格与选定档写入 evidence.calibration；
- 证据台 = evidence.json（allow_nan=False 原子写）+ logs/hk_smart_money_validate.csv。

用法（项目根）：
  .venv/bin/python backend/scripts/hk_smart_money_validate.py --no-composite --calibrate
  .venv/bin/python backend/scripts/hk_smart_money_validate.py --no-composite --skip-events
  .venv/bin/python backend/scripts/hk_smart_money_validate.py        # pass2（先跑 build）
退码：0 成功；1 失败（不落盘半成品 evidence）。
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend.services.api.market_analysis_hk import (  # noqa: E402
    institutional_classifier as ic,
)
from backend.shared.hk_smart_money import (  # noqa: E402
    ADMIT_HORIZON,
    EVENT_MIN_PCT,
    EVENT_MIN_VALUE_HKD,
    HORIZONS,
    MIN_ABS_IC,
    MIN_DAYS_TAIL,
    MIN_Q51_BP,
    SIGNAL_KEY_SEP,
    SIGNAL_LABELS,
    SOUTHBOUND_SEAT_IDS,
    TRANSFER_AMOUNT_RATIO,
    WARMUP_DAYS,
)
from backend.shared.hk_smart_money import datasets as ds  # noqa: E402
from backend.shared.hk_smart_money import events as ev_mod  # noqa: E402
from backend.shared.hk_smart_money import stats  # noqa: E402
from backend.shared.utc_datetime import utc_now  # noqa: E402

log = logging.getLogger("hk_smart_money_validate")

OUT_CSV = _PROJECT_ROOT / "logs" / "hk_smart_money_validate.csv"
#: 标定网格（细案 §3.1）与目标日均事件区间
CAL_GRID_VALUES: tuple[float, ...] = (1_000_000.0, 2_000_000.0, 5_000_000.0)
CAL_GRID_PCTS: tuple[float, ...] = (0.03, 0.05, 0.10)
CAL_TARGET_DAILY: tuple[int, int] = (3_000, 10_000)
DEFAULT_REBUILD_EVENTS = 40


# ---------------------------------------------------------------- 小工具


def _iso(dt: str | None) -> str | None:
    if not dt:
        return None
    try:
        return pd.Timestamp(dt).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def _r(v: Any, nd: int = 1) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else round(f, nd)


def _i(v: Any) -> int | None:
    f = _r(v, 0)
    return None if f is None else int(f)


def _coverage(mat: pd.DataFrame) -> dict[str, Any]:
    n = mat.notna().sum(axis=1)
    alive = n[n > 0]
    return {
        "avg_names_per_day": _r(alive.mean(), 0) if len(alive) else None,
        "days": int(len(alive)),
    }


def _decision_text(r10: dict | None) -> str:
    if r10 is None:
        return "样本不足：有效交易日数不足（不可裁定）"
    if r10["tail_ok"]:
        return "h10 双过闸通过"
    parts: list[str] = []
    ic, q51, n = r10["rank_ic"], r10["q5_minus_q1_bp"], r10["n_days"]
    if abs(ic) < MIN_ABS_IC:
        parts.append(f"|IC|={abs(ic):.4f} < {MIN_ABS_IC}")
    if abs(q51) <= MIN_Q51_BP:
        parts.append(f"|q5−q1|={abs(q51):.0f}bp ≤ {MIN_Q51_BP:.0f}bp")
    if n < MIN_DAYS_TAIL:
        parts.append(f"n_days={n} < {MIN_DAYS_TAIL}")
    if np.sign(ic) != np.sign(q51):
        parts.append("sign(IC) ≠ sign(q5−q1)")
    return "未过闸：" + "；".join(parts) if parts else "未过闸"


# ---------------------------------------------------------------- 事件仓 / 计数


def _exclusions() -> dict[str, Any]:
    """逐日 counts.json 汇总（first_seen / exit_boundary / 嫌疑计数披露）。"""
    dates = ds.event_dates()
    agg = {
        "events": 0,
        "transfer_suspect": 0,
        "first_seen": 0,
        "exit_boundary": 0,
        "raw_moves": 0,
    }
    south_days = 0
    for d in dates:
        c = ev_mod.read_counts(d)
        agg["events"] += int(c.get("events_n") or 0)
        agg["transfer_suspect"] += int(c.get("suspect_n") or 0)
        agg["first_seen"] += int(c.get("first_seen_n") or 0)
        agg["exit_boundary"] += int(c.get("exit_n") or 0)
        agg["raw_moves"] += int(c.get("raw_moves_n") or 0)
        if c.get("south_available"):
            south_days += 1
    out: dict[str, Any] = {"days": len(dates), "south_days": south_days, **agg}
    if agg["events"]:
        out["transfer_suspect_share"] = _r(agg["transfer_suspect"] / agg["events"], 4)
    return out


def _reconciliation() -> dict[str, Any] | None:
    """A 席（南向结算席）与 hsgt_south 的**同一股票集合**对账（数据质量门）。

    CCASS 只披露 top50，部分股票的中国结算席位排在 50 名之外（2026-11-12
    有 86 只）；拿全量南向对比会凭空少 ~3%。按两边都可见的股票交集重算。
    """
    dates = ds.event_dates()
    south_dates = set(ds.south_dates())
    cc_dates = set(ds.ccass_dates())
    for dt in reversed(dates):
        if dt not in south_dates or dt not in cc_dates:
            continue
        try:
            cur = ds.load_ccass(dt)
            cur_south = ds.load_south(dt)
        except Exception:  # noqa: BLE001 - 单日读取失败换下一日
            continue
        a = (
            cur.loc[cur["participant_id"].isin(SOUTHBOUND_SEAT_IDS)]
            .groupby("stock_code")["holding_quantity"]
            .sum()
        )
        s = cur_south.set_index("symbol")["holding_quantity"]
        common = a.index.intersection(s.index)
        if not len(common):
            continue
        a_sum = int(a.loc[common].sum())
        s_sum = int(s.loc[common].sum())
        return {
            "as_of": _iso(dt),
            "a_seat_qty_sum": a_sum,
            "south_qty_sum": s_sum,
            "ratio": round(a_sum / s_sum, 4),
            "universe": "common_top50",
            "common_n": int(len(common)),
            "south_n": int(len(s)),
        }
    return None


def _south_gap_days() -> int:
    cc = set(ds.ccass_dates())
    so = set(ds.south_dates())
    return len(cc - so)


# ---------------------------------------------------------------- 标定网格


def _calibrate(
    close: pd.DataFrame,
    amount: pd.DataFrame,
    fwd: dict[int, tuple[pd.DataFrame, pd.DataFrame]],
    sample_days: int,
) -> dict[str, Any]:
    """事件闸阈值网格抽样标定（只读：不写事件仓、不触碰在线常量）。"""
    dates = list(ds.ccass_dates())[-max(sample_days, 5) :]
    log.info(
        "标定：抽样 %d 个交易日（%s ~ %s）× 网格 %d 档",
        len(dates),
        dates[0],
        dates[-1],
        len(CAL_GRID_VALUES) * len(CAL_GRID_PCTS),
    )
    frames = {d: ds.load_ccass(d) for d in dates}  # 一次 IO，九配置共享
    grid: list[dict[str, Any]] = []
    for v in CAL_GRID_VALUES:
        for p in CAL_GRID_PCTS:
            builder = ev_mod.EventBuilder(
                close, amount, fwd, min_value_hkd=v, min_pct=p
            )
            daily: list[int] = []
            syms: list[int] = []
            vals: list[np.ndarray] = []
            suspects = 0
            prev: pd.DataFrame | None = None
            for i, d in enumerate(dates):
                cur = frames[d]
                evs, _flows, counts, _u, _h = builder.process_day(d, prev, cur, i)
                prev = cur
                if not counts["priced"]:
                    continue
                daily.append(int(counts["events_n"]))
                suspects += int(counts["suspect_n"])
                if len(evs):
                    syms.append(int(evs["symbol"].nunique()))
                    vals.append(evs["delta_value"].abs().to_numpy(dtype="float64"))
            arr = np.concatenate(vals) if vals else np.zeros(1)
            mean_daily = float(np.mean(daily)) if daily else None
            grid.append(
                {
                    "min_value_hkd": v,
                    "min_pct": p,
                    "mean_daily": _r(mean_daily, 1),
                    "median_daily": _r(np.median(daily), 0) if daily else None,
                    "covered_symbols_mean": _r(np.mean(syms), 0) if syms else None,
                    "value_median": _r(np.median(arr), 0),
                    "value_p90": _r(np.percentile(arr, 90), 0),
                    "suspect_total": suspects,
                    "in_target": bool(
                        mean_daily is not None
                        and CAL_TARGET_DAILY[0] <= mean_daily <= CAL_TARGET_DAILY[1]
                    ),
                }
            )
            log.info(
                "标定：≥%.0fM且≥%.0f%% → 日均 %.0f 事件 / 覆盖 %.0f 只",
                v / 1e6,
                p * 100,
                mean_daily or 0.0,
                np.mean(syms) if syms else 0.0,
            )
    selected = next(
        r
        for r in grid
        if r["min_value_hkd"] == EVENT_MIN_VALUE_HKD and r["min_pct"] == EVENT_MIN_PCT
    )
    return {
        "sample_days": len(dates),
        "sample_start": _iso(dates[0]),
        "sample_end": _iso(dates[-1]),
        "target_daily_events": list(CAL_TARGET_DAILY),
        "grid": grid,
        "selected": selected,
        "note": (
            "选定档 = 包常量 EVENT_MIN_VALUE_HKD/EVENT_MIN_PCT（标定只读、不影响"
            "事件仓）；若选定档未落在目标区间，须改常量后重跑 validate+build。"
        ),
    }


# ---------------------------------------------------------------- 复合分（pass2）


def _composite_eval(
    fwd_map: dict[int, tuple[pd.DataFrame, pd.DataFrame]], eval_start: pd.Timestamp
) -> dict[str, Any] | None:
    """把逐日复合分当信号跑 §3.3 同一套协议（读 build 产出的 scores 分区）。"""
    sdates = ds.score_dates()
    if not sdates:
        return None
    cols: dict[pd.Timestamp, pd.Series] = {}
    covered_means: list[float] = []
    for d in sdates:
        sc = ds.load_scores(d)
        if sc is None or sc.empty:
            continue
        cols[pd.Timestamp(d)] = sc.set_index("symbol")["score"]
        cm = sc["covered"].dropna()
        if len(cm):
            covered_means.append(float(cm.mean()))
    if not cols:
        return None
    mat = pd.DataFrame(cols).T.sort_index()
    mat = mat.loc[mat.index >= eval_start]
    ev = stats.evaluate(mat, fwd_map)
    pool = mat.notna().sum(axis=1)
    return {
        "results": ev["results"],
        "by_year": ev["by_year"],
        "n_dates": int(len(mat)),
        "avg_pool": _r(pool.mean(), 0) if len(pool) else None,
        "avg_covered": _r(np.mean(covered_means), 2) if covered_means else None,
    }


# ---------------------------------------------------------------- 主流程


def run(
    eval_start_arg: str | None,
    rebuild_events: int,
    with_composite: bool,
    calibrate: bool,
    calibrate_days: int,
    skip_events: bool,
) -> dict[str, Any]:
    t0 = time.monotonic()

    # 1) 事件仓（pass1 的输入；空仓自动全量首建）
    events_stats: dict[str, Any] | None = None
    if skip_events:
        if not ds.event_dates():
            raise RuntimeError(
                "事件仓为空，--skip-events 不可用（先跑一次不带该 flag）"
            )
        log.info("事件仓：--skip-events，使用既有 %d 个分区", len(ds.event_dates()))
    else:
        events_stats = ev_mod.build_events(rebuild_days=rebuild_events)
        log.info("事件仓：%s", events_stats)

    # 2) 行情矩阵（日历 = ccass 覆盖段的交易日）
    cc = ds.ccass_dates()
    if not cc:
        raise RuntimeError("ccass_top50 无分区——先同步港股数据")
    kd = [d for d in ds.kline_dates() if d >= cc[0]]
    close, amount = ds.load_prices(kd)
    if close.empty:
        raise RuntimeError("daily_forward 无可用分区（ccass 覆盖段内）")
    calendar = close.index
    fwd_map = ds.forward_matrices(close)
    # 评估窗终点必须由**事件数据**决定，不能取价格日历末日：港股 K 线常在
    # CCASS 数据之前落盘（T 日 K 线当晚有、HKEX 持股要等到次日），取价格末日
    # 会让 evidence.eval_end 比产物 as_of 多一天，三面自洽检查误报。
    event_dates = ds.event_dates()
    eval_end = pd.Timestamp(event_dates[-1]) if event_dates else calendar[-1]

    eval_start = (
        pd.Timestamp(eval_start_arg)
        if eval_start_arg
        else calendar[min(WARMUP_DAYS, max(len(calendar) - 1, 0))]
    )
    log.info(
        "评估窗：%s ~ %s（暖机 %d 交易日；日历 %d 日；事件 %d 日）",
        eval_start.strftime("%Y-%m-%d"),
        eval_end.strftime("%Y-%m-%d"),
        WARMUP_DAYS,
        len(calendar),
        len(event_dates),
    )

    # 3) 标定（可选，先于评估；只读）
    calibration = (
        _calibrate(close, amount, fwd_map, calibrate_days) if calibrate else None
    )
    if calibration is None:
        # pass2/日常重跑不带 --calibrate：沿用既有 evidence 的标定网格（网格自带
        # sample_start/end 标注归属），不因重跑把方法论证据抹成空
        prev = ds.load_product_json("evidence")
        if isinstance(prev, dict):
            calibration = prev.get("calibration")

    # 4) 信号面板 → 双过闸（h10 裁定，h5/h20 披露）
    panels = stats.compute_all_signals(close, amount)
    records: list[dict[str, Any]] = []
    for key in sorted(panels):
        mat_full = panels[key]
        mat = mat_full.loc[mat_full.index >= eval_start]
        ev = stats.evaluate(mat, fwd_map)
        r10 = ev["results"][str(ADMIT_HORIZON)]
        category, variant = key.split(SIGNAL_KEY_SEP, 1)
        records.append(
            {
                "name": key,
                "category": category,
                "category_label": _category_label(category),
                "variant": variant,
                "variant_label": SIGNAL_LABELS.get(variant, variant),
                "coverage": _coverage(mat),
                "direction": stats.direction_from(r10),
                "admitted": False,
                "decision": _decision_text(r10),
                "results": ev["results"],
                "by_year": ev["by_year"],
            }
        )

    passed = [
        rec["name"]
        for rec in records
        if rec["results"][str(ADMIT_HORIZON)] is not None
        and rec["results"][str(ADMIT_HORIZON)]["tail_ok"]
    ]
    by_name = {rec["name"]: rec for rec in records}
    ic10 = {k: by_name[k]["results"][str(ADMIT_HORIZON)]["rank_ic"] for k in passed}
    corr = stats.corr_matrix(
        {k: panels[k].loc[panels[k].index >= eval_start] for k in passed}
    )
    admitted_keys, pairs, collapsed = stats.collapse_admitted(passed, ic10, corr)
    collapsed_by = {c["signal"]: c for c in collapsed}
    admitted_set = set(admitted_keys)
    for rec in records:
        rec["admitted"] = rec["name"] in admitted_set
        if rec["name"] in collapsed_by:
            rec["decision"] = f"相关塌缩：{collapsed_by[rec['name']]['reason']}"
    log.info(
        "入柱裁定：%d 候选 → h10 过闸 %d → 塌缩后入柱 %d：%s",
        len(records),
        len(passed),
        len(admitted_keys),
        admitted_keys,
    )

    # 5) 证据组装（席位统计自 2026-10-16 起迁至 build_hk_smart_money.py 每日
    #    生成——席位榜是每日消费的盘点数据，不挂在校准节奏上）
    exclusions = _exclusions()
    warnings = _warnings(admitted_keys, calibration, exclusions)
    composite = None
    if with_composite:
        composite = _composite_eval(fwd_map, eval_start)
        if composite is None:
            if admitted_keys:
                # 有入柱却无评分分区 = build（或其跳过评分）没跑成，须告警
                warnings.append(
                    "复合分回填失败：scores 分区为空——先跑 build_hk_smart_money.py"
                )
            # 零入柱：build 按设计跳过评分，scores/ 结构上不存在——非失败，
            # 不上「先跑 build」假告警（evidence_only 告警已说明）
        else:
            r10c = composite["results"][str(ADMIT_HORIZON)]
            log.info(
                "复合分（pass2）：n_dates=%d IC=%s q5−q1=%s tail_ok=%s",
                composite["n_dates"],
                (r10c or {}).get("rank_ic"),
                (r10c or {}).get("q5_minus_q1_bp"),
                (r10c or {}).get("tail_ok"),
            )
    else:
        warnings.append(
            "pass1 运行（--no-composite）：evidence.composite 为空，需 build 后跑 pass2 回填"
        )

    evidence: dict[str, Any] = {
        "validated_at": utc_now().isoformat(),
        "version": 1,
        "window": {
            "eval_start": eval_start.strftime("%Y-%m-%d"),
            "eval_end": eval_end.strftime("%Y-%m-%d"),
            "warmup_days": WARMUP_DAYS,
            "event_days": int(len(ds.event_dates())),
            "event_rows": exclusions["events"],
        },
        "method": {
            "event_gate": (
                f"|Δvalue| ≥ {EVENT_MIN_VALUE_HKD / 1e6:.0f}M HKD ∧ "
                f"|Δqty|/qty(t−1) ≥ {EVENT_MIN_PCT:.0%} ∧ close ≥ 0.5 HKD"
            ),
            "transfer_suspect": (
                f"单席单日 |Δvalue| > {TRANSFER_AMOUNT_RATIO}×当日成交额 → 剔除出"
                "类型流与席位统计（事件仓保留并打标）"
            ),
            "episode": "同席同股同向 5 交易日内归并，仅首日入流与统计；反向重置",
            "forward": (
                "fwd(t,h) = close[t+h]/close[t+1] − 1，clip(−0.5, +1.0)；"
                "exc = fwd − 当日全市场有效中位（bp 展示）"
            ),
            "gate": (
                f"sign(IC)==sign(q5−q1) ∧ |IC|≥{MIN_ABS_IC} ∧ "
                f"|q5−q1|>{MIN_Q51_BP:.0f}bp ∧ n_days≥{MIN_DAYS_TAIL}"
                f"（h{ADMIT_HORIZON} 裁定，h{HORIZONS[0]}/h{HORIZONS[2]} 披露）"
            ),
            "seat_shrink": (
                "shrunk = w×原始 + (1−w)×中性（exc=0bp、hit=50%），w=n/(n+20)；"
                "事件集 = episode 首日 ∧ ¬嫌疑"
            ),
            "composite": (
                "admitted 信号截面分位（负向取 100−pct）等权平均；"
                "covered<1 不发布分数（缺失绝不填 50）"
            ),
        },
        "admit_horizon": ADMIT_HORIZON,
        "signals": records,
        "correlation": {"pairs": pairs, "collapsed": collapsed},
        "calibration": calibration,
        "exclusions": exclusions,
        "reconciliation": _reconciliation(),
        "composite": composite,
        "admitted": admitted_keys,
        "evidence_only": not admitted_keys,
        "warnings": warnings,
    }

    ds.write_json_atomic("evidence", evidence)
    _write_csv(records)
    log.info(
        "evidence.json → %s（%.1fs；事件 %d 行 / %d 日）",
        ds.product_dir() / "evidence.json",
        time.monotonic() - t0,
        exclusions["events"],
        exclusions["days"],
    )
    return evidence


def _category_label(category: str) -> str:
    return ic.category_label(category)


def _warnings(
    admitted: list[str],
    calibration: dict[str, Any] | None,
    exclusions: dict[str, Any],
) -> list[str]:
    out: list[str] = []
    overrides = ds.data_dir() / "2_base_sector" / "institutional_overrides.parquet"
    if not overrides.is_file():
        out.append(
            "人工覆盖表 institutional_overrides.parquet 不存在——席位分类为关键词"
            "默认档（新增覆盖表后须重跑 validate+build 生效）"
        )
    gaps = _south_gap_days()
    if gaps:
        out.append(
            f"南向分区缺 {gaps} 日（相对 CCASS 日历）——差分归并至下一可用分区，"
            "缺口日的南向净流在产品中为缺失（绝不伪造 0）"
        )
    if not admitted:
        out.append(
            "无类型信号通过 h10 双过闸——evidence-only 模式：共识榜不发布，"
            "类型效力表与席位榜照常交付"
        )
    if calibration is not None and not calibration["selected"].get("in_target"):
        out.append(
            f"标定：选定档日均事件 {calibration['selected'].get('mean_daily')} "
            f"未落目标区间 {CAL_TARGET_DAILY[0]}–{CAL_TARGET_DAILY[1]}（披露不阻断）"
        )
    if exclusions["days"] and exclusions["events"] == 0:
        out.append("事件仓全空——阈值或数据面异常，先核对 ccass_top50 分区")
    return out


def _write_csv(records: list[dict[str, Any]]) -> None:
    rows: list[dict[str, Any]] = []
    for rec in records:
        for h in HORIZONS:
            r = rec["results"][str(h)] or {}
            rows.append(
                {
                    "signal": rec["name"],
                    "category": rec["category"],
                    "variant": rec["variant"],
                    "horizon": h,
                    "admitted": rec["admitted"],
                    "direction": rec["direction"],
                    **{
                        k: r.get(k)
                        for k in (
                            "n_days",
                            "rank_ic",
                            "icir",
                            "ic_pos_rate",
                            "q1_bp",
                            "q2_bp",
                            "q3_bp",
                            "q4_bp",
                            "q5_bp",
                            "q5_minus_q1_bp",
                            "buy_hit",
                            "sell_hit",
                            "asym_bp",
                            "tail_ok",
                        )
                    },
                }
            )
    if not rows:
        return
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(OUT_CSV, index=False)
    log.info("CSV → %s（%d 行）", OUT_CSV, len(rows))


# ---------------------------------------------------------------- CLI


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="港股聪明钱证据裁定")
    parser.add_argument(
        "--start", default=None, help="评估窗起点 YYYY-MM-DD（默认暖机后首日）"
    )
    parser.add_argument(
        "--rebuild-events",
        type=int,
        default=DEFAULT_REBUILD_EVENTS,
        help="事件仓增量窗（交易日；默认 40，0=全量；空仓自动全量）",
    )
    parser.add_argument(
        "--skip-events", action="store_true", help="跳过事件仓刷新（pass2 提速）"
    )
    parser.add_argument(
        "--no-composite", action="store_true", help="pass1：跳过复合分回填"
    )
    parser.add_argument(
        "--calibrate", action="store_true", help="事件闸阈值网格标定（抽样只读）"
    )
    parser.add_argument(
        "--calibrate-days", type=int, default=30, help="标定抽样交易日数（默认 30）"
    )
    args = parser.parse_args()
    if args.rebuild_events < 0 or args.calibrate_days < 5:
        log.error("参数非法：--rebuild-events ≥ 0、--calibrate-days ≥ 5")
        return 1
    try:
        ev = run(
            eval_start_arg=args.start,
            rebuild_events=args.rebuild_events,
            with_composite=not args.no_composite,
            calibrate=args.calibrate,
            calibrate_days=args.calibrate_days,
            skip_events=args.skip_events,
        )
    except Exception as exc:  # noqa: BLE001 —— 失败不落盘半成品
        log.exception("裁定失败：%s", exc)
        return 1
    print("ADMITTED_SIGNALS =", ev["admitted"])
    for name in ev["admitted"]:
        rec = next(r for r in ev["signals"] if r["name"] == name)
        print(f"  - {name} direction={rec['direction']}")
    if ev["evidence_only"]:
        print("  （零入柱：evidence-only 模式，共识榜不发布）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
