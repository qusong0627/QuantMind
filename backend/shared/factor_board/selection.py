"""因子池装载 → 近效自算 IC → 闸门 → 族封顶（因子概率板 P0）。

IC 全部自算（dt 分区截面 × 前向收益），**不依赖各库 factor_series**——alpha_library
的报告已陈旧至 2026-08-27。前向收益由调用方以 qfq 收盘矩阵计算（见 composite.fwd_ranks）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from backend.shared.factor_board import config as C

log = logging.getLogger("factor_board.selection")


def resolve_cost_rate() -> dict:
    """净 IC 成本率（双边）解析：env ``QM_MINING_COST_RATE`` > mining_plugins 单源 > 兜底。

    返回 ``{"rate": float, "source": str, "warning": str | None}``。单源
    ``mining_plugins.config.get_cost_rate``（env > yaml > factor_research 常量）
    不可用（宿主无依赖/配置损坏）时取 ``COST_RATE_FALLBACK`` 并显形——
    成本源缺失绝不允许拖垮产板。
    """
    env = os.environ.get("QM_MINING_COST_RATE")
    if env:
        try:
            return {"rate": float(env), "source": "env", "warning": None}
        except ValueError:
            log.warning("QM_MINING_COST_RATE=%r 非法，落单源解析", env)
    try:
        from backend.services.engine.mining_plugins.config import get_cost_rate

        return {
            "rate": float(get_cost_rate()),
            "source": "mining_plugins",
            "warning": None,
        }
    except Exception as exc:  # noqa: BLE001 — 成本源坏不拦产板，兜底 + 显形
        return {
            "rate": float(C.COST_RATE_FALLBACK),
            "source": "fallback",
            "warning": f"成本率单源不可用（{exc}），兜底 {C.COST_RATE_FALLBACK}",
        }


def load_candidates(quantdb_root: Path) -> tuple[pd.DataFrame, dict]:
    """组装候选池：kept-362（日频库） + rd_mined 报告。

    返回 (cand, fingerprints)；cand 列：name/lib/sublibrary/display_name/kept_ic/
    kept_icir/turnover/src/group。
    """
    sel_path = quantdb_root / C.SELECTION_JSON_REL
    if not sel_path.exists():
        raise FileNotFoundError(f"联合筛选清单缺失: {sel_path}")
    raw = json.loads(sel_path.read_text(encoding="utf-8"))
    kept = pd.DataFrame(raw["kept"])
    kept = kept[kept["library"].isin(C.DAILY_LIBS)].copy()
    kept = kept.rename(
        columns={"library": "lib", "ic_mean": "kept_ic", "icir": "kept_icir"}
    )
    kept["src"] = "kept"
    cols = [
        "name",
        "lib",
        "sublibrary",
        "display_name",
        "kept_ic",
        "kept_icir",
        "turnover",
        "src",
    ]
    kept = kept.reindex(columns=cols)

    rd_rows: list[dict] = []
    rd_path = quantdb_root / C.RD_REPORT_REL
    rd_fp: dict = {"rd_report": None}
    if rd_path.exists():
        rd_fp["rd_report"] = _file_fingerprint(rd_path)
        for f in json.loads(rd_path.read_text(encoding="utf-8")).get("factors", []):
            icm = f.get("ic_neutral_mean")
            if icm is None:
                icm = f.get("ic_mean")
            if icm is None or abs(icm) < C.MIN_RD_REPORT_IC:
                continue
            rd_rows.append(
                {
                    "name": f["name"],
                    "lib": "rd_mined",
                    "sublibrary": C.RD_SUBLIBRARY,
                    "display_name": f.get("display_name") or f["name"],
                    "kept_ic": icm,
                    "kept_icir": f.get("icir"),
                    "turnover": None,
                    "src": "rd",
                }
            )
    else:
        log.warning("rd_mined 报告缺失，跳过 rd 源: %s", rd_path)

    cand = pd.concat([kept, pd.DataFrame(rd_rows, columns=cols)], ignore_index=True)
    # 同一 (lib,name) 可能在 kept 与 rd 报告里各出现一次；不去重会让
    # compute_ic_table 的 d[name] 返回 DataFrame（重复列），
    # v.notna() & fr.notna() 直接抛 duplicate labels。
    if cand.duplicated(["lib", "name"]).any():
        log.warning(
            "候选 (lib,name) 重复 %d 行，保留首行（优先联合筛选清单）",
            int(cand.duplicated(["lib", "name"]).sum()),
        )
        cand = cand.drop_duplicates(["lib", "name"], keep="first")
    cand["sublibrary"] = cand["sublibrary"].fillna("")
    cand["group"] = cand["lib"] + "/" + cand["sublibrary"].astype(str)
    fp = {
        "selection_json": _file_fingerprint(sel_path),
        **rd_fp,
    }
    return cand.reset_index(drop=True), fp


def _file_fingerprint(path: Path) -> dict:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return {"sha256_16": digest, "mtime": int(path.stat().st_mtime)}


def drop_missing_columns(
    cand: pd.DataFrame, quantdb_root: Path, ref_date: str
) -> tuple[pd.DataFrame, dict[str, list[str]]]:
    """按 ref_date 分区剔除某库不存在的因子列（分区缺失的整库剔除并告警）。"""
    dropped: dict[str, list[str]] = {}
    dropped_keys: set[tuple[str, str]] = set()
    for lib, g in cand.groupby("lib"):
        p = quantdb_root / "6_ml_datasets" / lib / f"dt={ref_date}" / "data.parquet"
        if not p.exists():
            dropped[lib] = g["name"].tolist()
            dropped_keys.update((lib, n) for n in g["name"])
            log.warning("库 %s 于 %s 无分区，整库剔除 %d 个候选", lib, ref_date, len(g))
            continue
        have = set(pq.ParquetFile(p).schema_arrow.names)
        miss = [n for n in g["name"] if n not in have]
        if miss:
            dropped[f"{lib}/缺列"] = miss
            dropped_keys.update((lib, n) for n in miss)
            log.warning("库 %s 缺列剔除 %d 个: %s", lib, len(miss), miss[:3])
    keep = ~cand.apply(lambda r: (r["lib"], r["name"]) in dropped_keys, axis=1)
    return cand[keep].reset_index(drop=True), dropped


def compute_ic_table(
    quantdb_root: Path,
    cand: pd.DataFrame,
    eval_dates: list[str],
    fwd_ranks: dict[str, pd.Series],
    *,
    horizon: int = 5,
    fwd_ret: dict[str, pd.Series] | None = None,
    cost_rate: float | None = None,
) -> tuple[
    dict[tuple[str, str], list[float]],
    dict[tuple[str, str], list[float]] | None,
    dict[str, list[str]],
]:
    """逐评估日算每个候选因子对前向 ``horizon`` 日收益的截面 RankIC。

    ``fwd_ranks[dt]`` 为该日前向收益的 rank 序列（index=symbol）。

    净 IC 轨（P2-a，``fwd_ret`` + ``cost_rate`` 同时给出时启用）：语义逐条对齐
    ``statistical/costs.net_rank_ic``——多头名单 = 当日因子 ``rank_pct ≥ 0.7``
    且样本 ≥ 10（``_long_membership``）；**当日新进**（对比上一评估日名单）从
    原始前向收益扣 ``cost_rate`` 后再算 RankIC；**首日不扣费但照常计 IC**；
    样本不足（<10，或整日跳过）的名单日不前进 prev（prev 跨过该日）。

    返回 ``(gross_acc, net_acc | None, days_by_lib)``；两条轨同日同掩码、逐日
    对齐；``days_by_lib[lib][i]`` 为 acc 第 i 个附加值的评估日（dt 分区名），
    regime/滚动等按日配对消费方必须据此配对，**不得自建日期列表**（库分区
    缺失会跳日，自建列表会错位）。
    """
    acc: dict[tuple[str, str], list[float]] = {
        (lib, n): [] for lib, n in zip(cand["lib"], cand["name"], strict=True)
    }
    net_acc: dict[tuple[str, str], list[float]] | None = (
        None
        if fwd_ret is None or cost_rate is None
        else {(lib, n): [] for lib, n in zip(cand["lib"], cand["name"], strict=True)}
    )
    days_by_lib: dict[str, list[str]] = {lib: [] for lib in cand.groupby("lib").groups}
    prev_long: dict[tuple[str, str], frozenset] = {}
    by_lib = {lib: sorted(set(g["name"].tolist())) for lib, g in cand.groupby("lib")}
    for dt in eval_dates:
        fr = fwd_ranks.get(dt)
        if fr is None or fr.notna().sum() < C.MIN_STOCK_SAMPLE:
            continue
        raw = fwd_ret.get(dt) if net_acc is not None else None
        for lib, names in by_lib.items():
            p = quantdb_root / "6_ml_datasets" / lib / f"dt={dt}" / "data.parquet"
            if not p.exists():
                continue
            days_by_lib[lib].append(dt)
            d = pd.read_parquet(p, columns=["symbol"] + names)
            d = d.drop_duplicates("symbol", keep="last").set_index("symbol")
            for n in names:
                key = (lib, n)
                v = d[n]
                m = v.notna() & fr.notna()
                val = (
                    v[m].rank().corr(fr[m])
                    if int(m.sum()) >= C.MIN_STOCK_SAMPLE
                    else np.nan
                )
                acc[key].append(val)
                if net_acc is None:
                    continue
                if raw is None:  # fwd_ret 与 fwd_ranks 同源同闸，正常不出现
                    net_acc[key].append(np.nan)
                    continue
                long_now = _long_now(v)
                nv = np.nan
                if int(m.sum()) >= C.MIN_STOCK_SAMPLE:
                    adj = raw
                    prev = prev_long.get(key)
                    if prev is not None and cost_rate and long_now is not None:
                        fresh = long_now - prev
                        if fresh:
                            adj = raw.copy()
                            sel = raw.index.isin(fresh)
                            adj[sel] = adj[sel] - float(cost_rate)
                    nv = float(v[m].rank().corr(adj[m].rank()))
                    if not math.isfinite(nv):
                        nv = np.nan
                net_acc[key].append(nv)
                if long_now is not None:  # 名单日（样本≥10）才前进 prev
                    prev_long[key] = long_now
    return acc, net_acc, days_by_lib


def _long_now(v: pd.Series) -> frozenset | None:
    """当日多头名单（``rank_pct ≥ NET_IC_LONG_PCT``）；样本 < NET_IC_MIN_NAMES → None。

    与 ``costs._long_membership`` 同口径：rank 只在非 NaN 值间进行。
    """
    if int(v.notna().sum()) < C.NET_IC_MIN_NAMES:
        return None
    pct = v.rank(pct=True)
    return frozenset(v.index[pct >= C.NET_IC_LONG_PCT])


def summarize_ic(
    ic_table: dict[tuple[str, str], list[float]],
    cand: pd.DataFrame,
    *,
    net_table: dict[tuple[str, str], list[float]] | None = None,
) -> pd.DataFrame:
    """汇总每个候选的 IC60/胜率/ICIR60（不够 IC_WINDOW-… 天有效 IC 的不可评估）。

    P2 增量（新列全部随 stats 流向后段，旧列零变化）：
    - ``ic20/win20/decay``：尾 ``DECAY_WINDOW`` 个有效日，方向对齐（``sgn×IC``，
      与持有期画像同口径）后的衰减状态机——``flipped``（对齐均值<0）/
      ``weak``（|均值|<``DECAY_MIN_ABS_IC``）/``insufficient``（有效日<
      ``DECAY_MIN_DAYS``）/``ok``；``ic20``=对齐均值（可为负=翻转显形），
      ``win20``=原始符号与 ``ic60`` 同号的占比（与 win60 同式仅窗不同）；
    - ``net_ic60/net_win60/net_icir60/eroded``：净 IC 轨（``net_table`` 给出时）；
      ``eroded`` = 成本把符号吃反（``sign(net) ≠ sign(gross)``），无净轨→None。
    """
    meta = cand.drop_duplicates(["lib", "name"]).set_index(["lib", "name"])
    rows = []
    for (lib, n), ics in ic_table.items():
        s = pd.Series(ics, dtype=float).dropna().iloc[-C.IC_WINDOW :]
        if len(s) < C.MIN_IC_DAYS:
            continue
        ic60 = float(s.mean())
        if not math.isfinite(ic60):
            continue
        sd = float(s.std())
        rows.append(
            {
                "lib": lib,
                "name": n,
                "group": meta.loc[(lib, n), "group"],
                "display": meta.loc[(lib, n), "display_name"],
                "kept_ic": float(meta.loc[(lib, n), "kept_ic"]),
                "kept_icir": meta.loc[(lib, n), "kept_icir"],
                "turnover": meta.loc[(lib, n), "turnover"],
                "src": meta.loc[(lib, n), "src"],
                "ic60": ic60,
                "win60": float((np.sign(s) == np.sign(ic60)).mean()),
                "n_days": int(len(s)),
                # ICIR = mean(IC)/std(IC)；t = ICIR×√n（显著性）。
                # 二者不可混用：旧实现把 t 写进 icir60，前端 ICIR 列被放大 √n 倍。
                "icir60": ic60 / sd if sd > 0 else None,
                "t60": ic60 / sd * math.sqrt(len(s)) if sd > 0 else None,
                **_decay_stat(s, ic60),
                **_net_stat(net_table, (lib, n), ic60),
            }
        )
    return pd.DataFrame(rows)


def _decay_stat(s: pd.Series, ic60: float) -> dict:
    """尾窗衰减状态机（细案 §三）：四态 ok/weak/flipped/insufficient。"""
    tail = s.iloc[-C.DECAY_WINDOW :]
    if len(tail) < C.DECAY_MIN_DAYS:
        return {"ic20": None, "win20": None, "decay": "insufficient"}
    aligned = float(np.sign(ic60)) * tail
    ic20 = float(aligned.mean())
    win20 = float((np.sign(tail) == np.sign(ic60)).mean())
    if ic20 < 0:
        decay = "flipped"
    elif abs(ic20) < C.DECAY_MIN_ABS_IC:
        decay = "weak"
    else:
        decay = "ok"
    return {"ic20": ic20, "win20": win20, "decay": decay}


def _net_stat(
    net_table: dict[tuple[str, str], list[float]] | None,
    key: tuple[str, str],
    ic60: float,
) -> dict:
    """净 IC 统计（成本只扣当日新进多头腿；见 compute_ic_table 口径注记）。"""
    empty = {
        "net_ic60": None,
        "net_win60": None,
        "net_icir60": None,
        "eroded": None,
    }
    if net_table is None or key not in net_table:
        return empty
    ns = pd.Series(net_table[key], dtype=float).dropna().iloc[-C.IC_WINDOW :]
    if len(ns) < C.MIN_IC_DAYS:
        return empty
    net_ic60 = float(ns.mean())
    if not math.isfinite(net_ic60):
        return empty
    nsd = float(ns.std())
    return {
        "net_ic60": net_ic60,
        "net_win60": float((np.sign(ns) == np.sign(net_ic60)).mean()),
        "net_icir60": net_ic60 / nsd if nsd > 0 else None,
        "net_t60": net_ic60 / nsd * math.sqrt(len(ns)) if nsd > 0 else None,
        "eroded": bool(np.sign(net_ic60) != np.sign(ic60)),
    }


def gate(stats: pd.DataFrame, min_win: float) -> pd.DataFrame:
    """有效性闸：|IC60|≥门槛 ∧ 方向与联合闸一致 ∧ 胜率≥min_win。"""
    return stats[
        (stats["ic60"].abs() >= C.MIN_ABS_IC60)
        & (np.sign(stats["ic60"]) == np.sign(stats["kept_ic"]))
        & (stats["win60"] >= min_win)
    ]


def select_factors(stats: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """闸门 + 族封顶。返回 (sel, gate_info)；sel 带 ``sgn``（±1）与 ``w``。

    过闸数 <MIN_GATED 时降级胜率至 DEGRADE_WIN_RATE（同 v2 原型出口）。
    """
    gated = gate(stats, C.MIN_WIN_RATE)
    degraded = False
    if len(gated) < C.MIN_GATED:
        gated = gate(stats, C.DEGRADE_WIN_RATE)
        degraded = True
        log.warning(
            "过闸数不足，胜率门槛降级至 %.2f → %d 个", C.DEGRADE_WIN_RATE, len(gated)
        )
    parts = [
        g.sort_values("ic60", key=lambda s: s.abs(), ascending=False).head(
            C.RD_FAMILY_CAP if lib == "rd_mined" else C.FAMILY_CAP
        )
        for (lib, _grp), g in gated.groupby(["lib", "group"])
    ]
    if parts:
        sel = pd.concat(parts).drop_duplicates(["lib", "name"]).reset_index(drop=True)
    else:
        # 极端行情下连降级胜率也无因子过闸：返回空表，由调用方按 MIN_SELECTED
        # 硬门报 BuildError；不能对空列表 pd.concat（会抛 ValueError 掩盖真因）。
        sel = gated.iloc[0:0].copy().reset_index(drop=True)
    sel["sgn"] = np.sign(sel["ic60"]).astype(int)
    sel["w"] = sel["ic60"].abs().clip(C.W_CLIP_LO, C.W_CLIP_HI)
    info = {
        "evaluated": int(len(stats)),
        "gated": int(len(gated)),
        "gated_long": int((gated["ic60"] > 0).sum()),
        "gated_short": int((gated["ic60"] < 0).sum()),
        "degraded": degraded,
        "families": int(sel["group"].nunique()),
        # P2-e：完整过闸表（含降级分支镜像）——builder 由此组「族头池」做二次去重；
        # 不落 JSON（meta.coverage 逐字段取数）。
        "gated_table": gated,
    }
    return sel, info
