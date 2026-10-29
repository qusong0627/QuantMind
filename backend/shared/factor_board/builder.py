"""因子概率板：编排（候选池 → 自算 IC → 入选 → 去重 → regime → 合成 → 共振）。

模块全部纯函数/装载器；本文件只做流程编排与 meta/质检组包。P2 接线约定：

- **选择管线**（run_selection）为产板与滚动重选（rolling.py）共用同一实现；
- **二次去重/regime** 是入选名单的稳定化变换：异常一律降级 + warning 显形
  （去重回退未去重名单；regime 整块 null 权重全不动），**绝不拦板**；
- **可交易掩码**：名单层过滤（stocks 计数显形，掩码缺失不过滤）；
- **滚动 OOS 证据**：meta.validation.rolling_oos 读取（月频台账）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path

import numpy as np
import pandas as pd

from backend.shared.factor_board import board_io as IO
from backend.shared.factor_board import composite as CP
from backend.shared.factor_board import config as C
from backend.shared.factor_board import dedup as DEDUP
from backend.shared.factor_board import mask as MASK
from backend.shared.factor_board import regime as REGIME
from backend.shared.factor_board import sectors as S
from backend.shared.factor_board import selection as SEL
from backend.shared.factor_board import signals as SIG
from backend.shared.utc_datetime import to_utc_iso, utc_now

log = logging.getLogger("factor_board.builder")


class BuildError(RuntimeError):
    """硬门不过 → 不落盘（CLI 退码 1）。"""


@dataclass
class SelectionRun:
    """一次选择管线的全量中间物（产板与滚动重选共用；勿跨日复用）。"""

    cand: pd.DataFrame
    cand_fp: dict
    dates: list[str]
    trade_date: str
    close: pd.DataFrame
    close_nonpos: int
    dropped: dict[str, list[str]]
    fwd5: dict[str, pd.Series]
    ic_table: dict
    net_acc: dict | None
    days_by_lib: dict[str, list[str]]
    stats: pd.DataFrame
    sel: pd.DataFrame
    gate_info: dict
    cost_rate: dict
    warnings: list[str] = field(default_factory=list)


def run_selection(
    quantdb_root: Path,
    *,
    as_of: str | None = None,
    today: _date | None = None,
) -> SelectionRun:
    """候选池 → 数据窗 → 自算 IC（毛+净轨）→ 闸门 → 入选。

    ``as_of``：窗尾分区（'YYYYMMDD' 或 ISO，缺省=最新分区）——滚动重选以历史
    as_of 复用同一条管线（同窗 85/65、同 h=5、同闸）。硬门不过抛 BuildError。
    """
    today = today or _date.today()
    warnings: list[str] = []

    # ── 1. 候选池与数据窗 ──────────────────────────────────────────
    cand, cand_fp = SEL.load_candidates(quantdb_root)
    # 全库共同分区：各库同步有先后，按 l1 最新日会整库剔除尚未更新的库。
    dates_all, latest_by_lib = CP.common_factor_dates(
        quantdb_root, sorted(cand["lib"].unique())
    )
    if not dates_all:
        dates_all = CP.list_factor_dates(quantdb_root)
        warnings.append("候选库无共同分区，退回 l1 分区（可能整库剔除）")
    else:
        l1_latest = latest_by_lib.get("l1_factors")
        if l1_latest and l1_latest > dates_all[-1]:
            warnings.append(
                f"全库共同最新分区 {dates_all[-1]}；l1 已到 {l1_latest}，"
                "等待其余库同步（不用残缺截面选因子）"
            )
    if as_of:
        as_of_dt = str(as_of).replace("-", "")
        dates_all = [d for d in dates_all if d <= as_of_dt]
        if not dates_all:
            raise BuildError(f"as_of {as_of} 早于全部因子分区")
    dates = dates_all[-C.WINDOW_DATES :]
    if len(dates) < C.EVAL_DAYS + 6:
        raise BuildError(f"因子分区不足：仅 {len(dates)} 日")
    trade_date = dates[-1]
    close = CP.load_close_matrix(quantdb_root, dates)
    if close.empty:
        raise BuildError("qfq 收盘矩阵为空（daily_forward 缺失）")

    nonpos = int((close <= 0).to_numpy().sum())
    total_cells = int(close.notna().to_numpy().sum())
    if total_cells and nonpos / max(total_cells, 1) > C.CLOSE_NONPOS_MAX_FRAC:
        warnings.append(f"close 非正值 {nonpos}/{total_cells} 超阈值")
    stale_days = (today - pd.Timestamp(trade_date).date()).days
    if stale_days > C.FACTOR_STALE_DAYS:
        warnings.append(
            f"因子最新分区 {trade_date} 距今 {stale_days} 天（>{C.FACTOR_STALE_DAYS}）"
        )

    cand, dropped = SEL.drop_missing_columns(cand, quantdb_root, trade_date)
    if dropped:
        warnings.append(
            f"候选剔除 {sum(len(v) for v in dropped.values())} 个：{sorted(dropped)[:4]}"
        )

    # ── 2. 自算近效 IC（毛 + 净成本轨）→ 闸门 → 入选 ──────────────
    eval_idx = range(len(dates) - C.EVAL_DAYS - 1, len(dates) - 5)
    fwd5 = CP.fwd_ranks(close, dates, eval_idx, 5)
    fwd5_raw = CP.fwd_rets(close, dates, eval_idx, 5)
    cost = SEL.resolve_cost_rate()
    if cost.get("warning"):
        warnings.append(cost["warning"])
    ic_table, net_acc, days_by_lib = SEL.compute_ic_table(
        quantdb_root,
        cand,
        list(fwd5.keys()),
        fwd5,
        fwd_ret=fwd5_raw,
        cost_rate=float(cost["rate"]),
    )
    stats = SEL.summarize_ic(ic_table, cand, net_table=net_acc)
    if stats.empty:
        raise BuildError("无候选因子可评估（IC 全空）")
    sel, gate_info = SEL.select_factors(stats)
    if len(sel) < C.MIN_SELECTED:
        raise BuildError(f"入选因子 {len(sel)} < 硬门 {C.MIN_SELECTED}")
    if gate_info["degraded"]:
        warnings.append(f"胜率门槛降级至 {C.DEGRADE_WIN_RATE}（过闸不足）")

    return SelectionRun(
        cand=cand,
        cand_fp=cand_fp,
        dates=dates,
        trade_date=trade_date,
        close=close,
        close_nonpos=nonpos,
        dropped=dropped,
        fwd5=fwd5,
        ic_table=ic_table,
        net_acc=net_acc,
        days_by_lib=days_by_lib,
        stats=stats,
        sel=sel,
        gate_info=gate_info,
        cost_rate=cost,
        warnings=warnings,
    )


def build_board(
    quantdb_root: Path,
    *,
    model_date: str | None = None,
    signals_csv: str | Path | None = None,
    board_dir: Path | None = None,
    today: _date | None = None,
    members_out: dict | None = None,
) -> dict:
    """构建决策板；``members_out`` 传入 dict 时填充板块成分明细（钻取伴生件）。

    明细**不进板 JSON**（概念展开 6 万+ 行，内嵌会让产物膨胀数倍），由 CLI 另存
    ``members/{decision_date}.json``。明细生成异常只记 warning，绝不拦板（板 JSON
    契约与旧调用方零扰动——缺省 None 时行为不变）。
    """
    today = today or _date.today()
    run = run_selection(quantdb_root, today=today)
    warnings = list(run.warnings)
    dates, trade_date = run.dates, run.trade_date
    sel = run.sel

    # ── 2.5 入选内二次去重（P2-e；异常/跌破硬门 → 回退未去重名单） ──
    dedup_block: dict | None = None
    try:
        outcome = DEDUP.dedup_selection(
            quantdb_root, run.gate_info["gated_table"], sel, dates[-C.EVAL_DAYS :]
        )
        if len(outcome.kept_keys) < C.MIN_SELECTED:
            warnings.append(
                f"二次去重后入选 {len(outcome.kept_keys)} < 硬门 {C.MIN_SELECTED}，"
                "回退未去重名单"
            )
        else:
            dedup_block = DEDUP.dedup_block(outcome)
            if outcome.dropped or outcome.pruned_keys:
                warnings.append(
                    f"二次去重：席位置换 {len(outcome.dropped)} 个、"
                    f"族修剪 {len(outcome.pruned_keys)} 个"
                )
            if dedup_block["warnings"]:
                warnings.append(
                    f"二次去重：{len(dedup_block['warnings'])} 对保留候选人 "
                    f"|ρ|≥{C.DEDUP_WARN}"
                )
            sel = _sel_from_keys(run.stats, outcome.pool, outcome.kept_keys)
    except Exception as exc:  # noqa: BLE001 — 去重是稳定化变换，坏掉不拦板
        warnings.append(f"二次去重异常，跳过（用未去重名单）：{exc}")
        log.warning("二次去重异常", exc_info=True)

    # ── 2.6 regime 分桶权重（P2-c；只降不翻，失败整块 null 权重全不动） ──
    regime_block: dict | None = None
    try:
        sel, regime_block, regime_warnings = REGIME.build_regime_weights(
            quantdb_root, sel
        )
        warnings += regime_warnings
    except Exception as exc:  # noqa: BLE001 — 板永不因 regime 失败
        warnings.append(f"regime 计算异常，权重全不动：{exc}")
        log.warning("regime 计算异常", exc_info=True)

    # ── 3. 合成截面 + 持有期画像 ──────────────────────────────────
    comps = CP.build_composite_history(quantdb_root, sel, dates, len(dates) - 70)
    comp_today = comps.get(trade_date, pd.Series(dtype=float)).dropna()
    if len(comp_today) < C.MIN_COMPOSITE_TODAY:
        raise BuildError(
            f"当日合成覆盖 {len(comp_today)} < 硬门 {C.MIN_COMPOSITE_TODAY}"
        )
    close = run.close
    hz_idx = range(len(dates) - C.HORIZON_WINDOW, len(dates) - 20)
    fwd_by_h = {h: CP.fwd_ranks(close, dates, hz_idx, h) for h in C.HORIZON_DAYS}
    horizon = CP.horizon_table(
        quantdb_root, sel, dates, len(dates) - C.HORIZON_WINDOW, fwd_by_h
    )

    # ── 4. 模型信号 + 可交易掩码 + 个股/板块共振 ──────────────────
    mdl, mdl_meta = SIG.load_model_signals(csv_path=signals_csv, model_date=model_date)
    if mdl_meta["stale"]:
        warnings.append(f"模型信号非当日（{mdl_meta['trade_date']}，stale=true）")
    if mdl_meta["n"] < C.MODEL_MIN_ROWS:
        warnings.append(
            f"模型信号仅 {mdl_meta['n']} 行（<{C.MODEL_MIN_ROWS}，疑似残缺 run）"
        )

    name_of, st_of = SIG.load_instruments(quantdb_root)
    members = SIG.load_sector_members(quantdb_root)
    ind2 = (
        members[members["SectorType"] == "行业板块(二级)"]
        .drop_duplicates("Symbol")
        .set_index("Symbol")["SectorName"]
    )

    stock = pd.DataFrame({"composite": comp_today})
    stock["name"] = name_of.reindex(stock.index)
    stock["st"] = st_of.reindex(stock.index).fillna(False)
    stock["mdl_rank"] = mdl["rank_pct"].reindex(stock.index)
    stock["sector_l2"] = ind2.reindex(stock.index)
    blk_long, blk_short, mask_info, mask_warn = MASK.mask_columns(
        quantdb_root, trade_date, stock.index
    )
    if mask_warn:
        warnings.append(mask_warn)
    stock["blocked_long"] = blk_long
    stock["blocked_short"] = blk_short
    cov_n = int(stock["mdl_rank"].notna().sum())
    if cov_n < C.MIN_MODEL_INTERSECTION:
        warnings.append(f"因子×模型交集仅 {cov_n} 只（<{C.MIN_MODEL_INTERSECTION}）")

    q = S.quadrant_frame(stock)
    rho = S.spearman(q["composite"], q["mdl_rank"])
    # 全量注入象限列（缺双轴之一即 NaN）——供成分明细钻取行携带 quadrant
    stock["quadrant"] = q["quadrant"].reindex(stock.index)

    sectors_payload: dict = {}
    for stype in C.SECTOR_TYPES:
        payload = IO.sector_payload(stock, members, stype)
        key = C.SECTOR_KEY[stype]
        if payload is None:
            warnings.append(f"板块表缺失：{stype}")
            payload = {"q67": None, "q33": None, "items": []}
        sectors_payload[key] = payload

    # ── 5. 验证（近窗口径自洽；真 OOS 走滚动台账） ────────────────
    val = _validate(comps, run.fwd5, members, dates)
    val["rolling_oos"] = IO.latest_rolling_oos(
        board_dir if board_dir is not None else C.resolve_board_dir()
    )
    if val["rolling_oos"] is None:
        val["note"] += "；rolling_oos=null（滚动重选台账暂无已回填 OOS）"

    # ── 6. 组包 ───────────────────────────────────────────────────
    decision_date = str(pd.Timestamp(mdl_meta["trade_date"]).date())
    trade_date_iso = str(pd.Timestamp(trade_date).date())  # 20261009 → 2026-10-09
    board = {
        "schema_version": C.SCHEMA_VERSION,
        "meta": {
            "decision_date": decision_date,
            "trade_date": trade_date_iso,
            "generated_at": to_utc_iso(utc_now()),
            "data_window": [dates[0], dates[-1]],
            "eval_days": int(len(run.fwd5)),
            "model": mdl_meta,
            "inputs": {
                **run.cand_fp,
                "dropped": {k: len(v) for k, v in run.dropped.items()},
            },
            "thresholds": C.thresholds_snapshot(cost_rate=run.cost_rate),
            "coverage": {
                "pool": int(len(run.cand)),
                "evaluated": run.gate_info["evaluated"],
                "gated": run.gate_info["gated"],
                "gated_long": run.gate_info["gated_long"],
                "gated_short": run.gate_info["gated_short"],
                "selected": int(len(sel)),
                "families": int(sel["group"].nunique()),
                "composite_n": int(len(comp_today)),
                "model_cov_n": cov_n,
                "close_nonpos": run.close_nonpos,
                "mask": mask_info,
            },
            "warnings": warnings,
        },
        "factors": IO.factor_payload(sel, horizon),
        "horizon": IO.horizon_payload(horizon, sel),
        "validation": val,
        "resonance": {
            "spearman": IO._clean(rho),
            "n": int(len(q)),
            "quadrants": S.quadrant_counts(q),
        },
        "sectors": sectors_payload,
        "stocks": S.stock_lists(stock),
        "regime": IO.regime_payload(regime_block),
        "dedup": dedup_block,
    }

    # ── 7. 板块成分明细（钻取伴生件；异常只告警，绝不拦板） ────────
    if members_out is not None:
        try:
            members_out.update(
                {
                    "decision_date": decision_date,
                    "trade_date": trade_date_iso,
                    "sectors": {
                        C.SECTOR_KEY[t]: S.members_index(stock, members, t)
                        for t in C.SECTOR_TYPES
                    },
                }
            )
        except Exception as exc:  # noqa: BLE001 — 伴生件不拦主产物
            warnings.append(f"板块成分明细生成异常，未落盘：{exc}")
            log.warning("板块成分明细生成异常", exc_info=True)
    return board


def _sel_from_keys(
    stats: pd.DataFrame, pool: pd.DataFrame, keys: tuple[str, ...]
) -> pd.DataFrame:
    """去重后的入选表：全列取自 stats，sgn/w 取池值（替补行也齐备）。

    行序 = greedy 步行序（= 最终席位序；池序保证恒等退化时与未去重名单同集）。
    """
    st = stats.set_index(["lib", "name"])
    po = pool.set_index("key")
    rows = []
    for k in keys:
        lib, n = DEDUP.split_key(k)
        r = st.loc[(lib, n)].to_dict()
        r["sgn"] = int(po.loc[k, "sgn"])
        r["w"] = float(po.loc[k, "w"])
        rows.append({"lib": lib, "name": n, **r})
    return pd.DataFrame(rows)


def _validate(
    comps: dict[str, pd.Series],
    fwd5: dict[str, pd.Series],
    members: pd.DataFrame,
    dates: list[str],
) -> dict:
    """个股层 + 板块（行业二级）层 RankIC（近 VALIDATION_WINDOW 评估日，fwd5）。"""
    sm2 = members[members["SectorType"] == "行业板块(二级)"].rename(
        columns={"Symbol": "symbol"}
    )[["symbol", "SectorName"]]
    stock_ics, sector_ics = [], []
    for dt in dates[len(dates) - C.EVAL_DAYS : len(dates) - 5]:
        comp = comps.get(dt)
        fr = fwd5.get(dt)
        if comp is None or fr is None:
            continue
        m = comp.notna() & fr.notna()
        if int(m.sum()) < C.MIN_COMPOSITE_IC_DAY:
            continue
        stock_ics.append(float(comp[m].rank().corr(fr[m])))
        d = pd.DataFrame({"symbol": comp.index, "comp": comp.values})
        d["fwd"] = fr.reindex(d["symbol"]).values
        d = d.dropna().merge(sm2, on="symbol")
        g = d.groupby("SectorName").agg(
            comp=("comp", "mean"), fwd=("fwd", "mean"), n=("fwd", "size")
        )
        g = g[g["n"] >= C.SECTOR_MIN_N]
        if len(g) >= C.SECTOR_IC_MIN_SECTORS:
            sector_ics.append(float(g["comp"].rank().corr(g["fwd"].rank())))
    return {
        "stock": _agg_ic(stock_ics),
        "sector": _agg_ic(sector_ics),
        "in_sample": True,
        "note": (
            "入选窗=评估窗（近60交易日），口径自洽验证；真 OOS（前窗选、后窗验）"
            "见 rolling_oos"
        ),
    }


def _agg_ic(ics: list[float]) -> dict:
    s = pd.Series(ics, dtype=float).dropna().tail(C.VALIDATION_WINDOW)
    if s.empty or float(s.std()) == 0:
        return {
            "rank_ic": None,
            "icir": None,
            "t": None,
            "win": None,
            "days": int(len(s)),
        }
    mu, sd = float(s.mean()), float(s.std())
    return {
        "rank_ic": IO._clean(mu),
        "icir": IO._clean(mu / sd),
        "t": IO._clean(mu / sd * float(np.sqrt(len(s)))),
        "win": IO._clean(float((s > 0).mean())),
        "days": int(len(s)),
    }
