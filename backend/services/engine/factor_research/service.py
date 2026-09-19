"""因子研究 —— 服务层：目录 / 排行榜 / 单因子 / 对比 / 合成 / 最优权重 / 快照管理。

数据分层：
- 月末名次面板（store.panel）→ 任意 N、任意区间、N 扫描、标签、综合分（scorecard.py）；
- 月末打分长表（store.scores_for）→ 合成回测在线现算（与旧版一致，周初到周末毫秒级）；
- 序列与元数据（ic.parquet / benchmarks.parquet / stock_snapshot.parquet）。

区间语义：区间内相邻月末的持有期收益逐月复利，首月末净值 1.0；首月换手 100% 全额计费。

快照管理：全部在本地 QuantDB 上计算（不上传任何数据）；未计算时前端提供
「一键计算」入口（snapshot_status / start_build），构建即
``python3 backend/scripts/build_factor_research.py`` 的后台子进程。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd

from backend.services.engine.factor_research import analysis, scorecard, store
from backend.services.engine.factor_research.catalog import BY_CODE, FACTORS, L1_ORDER


def _points(dates, values) -> list[dict]:
    return [
        {"date": str(pd.Timestamp(d).date()), "value": round(float(v), 6)}
        for d, v in zip(dates, values, strict=False)
        if np.isfinite(v)
    ]


def _instrument_names() -> dict[str, dict]:
    """symbol → {name, industry}（证券主表快照，缺失时优雅降级）。"""
    try:
        from backend.services.engine.factor_research import data as frdata

        instr = frdata.load_instrument()
        return {
            r["symbol"]: {"name": r["name"], "industry": r["industry"]}
            for _, r in instr.iterrows()
        }
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 目录
# ---------------------------------------------------------------------------
def catalog(dataset: str = "classic") -> dict:
    if dataset == "private":
        f = store.factors_meta()
        if f is None:
            raise FileNotFoundError(
                "私人因子库快照缺失（请先一键计算，产物 factor_research_private/factors.json）"
            )
        items = [{**e, "env_tag": "", "time_tag": ""} for e in f.get("factors", [])]
        return {
            "factors": items,
            "l1_order": f.get("l1_order", []),
            "l2_order": f.get("l2_order", {}),
            "benchmarks": [
                {"symbol": c, "name": scorecard.BENCH_NAMES[c]}
                for c in scorecard.BENCH_ORDER
            ],
            "meta": f.get("meta", {}),
        }
    m = store.metrics()
    meta = m.get("meta", {})
    computed = set(m.get("metrics", {}).keys())
    items = []
    l2_order: dict[str, list[str]] = {}
    for f in FACTORS:
        l2s = l2_order.setdefault(f["l1"], [])
        if f["l2"] not in l2s:
            l2s.append(f["l2"])
        items.append(
            {
                "code": f["code"],
                "name_cn": f["name_cn"],
                "l1": f["l1"],
                "l2": f["l2"],
                "direction": f["direction"],
                "description": f["description"],
                "formula": f["formula"],
                "wind_source": f["wind_source"],
                "env_tag": f["env_tag"],
                "time_tag": f["time_tag"],
                "available": bool(f["available"])
                and (f["code"] in computed or not computed),
                "unavailable_reason": f["unavailable_reason"],
            }
        )
    return {
        "factors": items,
        "l1_order": L1_ORDER,
        "l2_order": l2_order,
        "benchmarks": [
            {"symbol": c, "name": scorecard.BENCH_NAMES[c]}
            for c in scorecard.BENCH_ORDER
        ],
        "meta": meta,
    }


# ---------------------------------------------------------------------------
# 区间公共上下文（排行/单因子/对比共用）
# ---------------------------------------------------------------------------
# 上下文分两级，因为两级的代价差一个数量级：
# - 轻上下文（面板/掩码/基准/IC/区间元信息）：人人都要，构建 ~10s；
# - 派生量（全因子 top-30 序列 + 环境/时效标签）：只有排行榜与单因子详情用，
#   而它是逐因子的 Python 循环，2754 因子上要几十秒 —— 对比/合成/寻优端点本来
#   一秒能出，不该陪着等。故按需构建。
#
# 缓存键里带面板快照 mtime（store.panel_stamp），**不设 TTL**：快照只在重建时变，
# 而重建一次要付几十秒冷启动，按固定时间过期等于每 10 分钟让第一个请求白卡一次。
_ctx_cache: dict[tuple, dict] = {}
_derived_cache: dict[tuple, dict] = {}
_ctx_lock = threading.Lock()
_derived_lock = threading.Lock()
_MAX_CTX = 4  # 面板本体在 store 里另有共享缓存，这里每份只几十 MB，留几份够切数据集


def _range_key(start: str | None, end: str | None, dataset: str) -> tuple:
    return (start or "", end or "", dataset, store.panel_stamp(dataset))


def _build_ctx(key: tuple, start: str | None, end: str | None, dataset: str) -> dict:
    p = store.panel(dataset)
    if p is None:
        raise FileNotFoundError(
            f"factor_panel.parquet 缺失（数据集 {dataset}；请先跑构建或点「快照-一键计算」）"
        )
    bench = store.benchmark_table(dataset)
    ic = store.ic_table(dataset)
    mask = scorecard.month_mask(p.dates, start, end)
    benches = scorecard.bench_series(bench, mask, p.dates) if bench is not None else {}
    rdates = p.dates[mask]
    bench_ret = None
    prim = benches.get(scorecard.BENCH_PRIMARY)
    if prim is not None and len(prim["nav"]) > 1:
        nav = prim["nav"]
        bench_ret = nav[1:] / nav[:-1] - 1
    return {
        "_key": key,
        "panel": p,
        "mask": mask,
        "rdates": rdates,
        "benches": benches,
        "ic": ic,
        "ic_index": scorecard.ic_index(ic) if ic is not None else {},
        "bench_ret": bench_ret,
    }


def _range_ctx(start: str | None, end: str | None, dataset: str = "classic") -> dict:
    """区间轻上下文。冷构建持 _ctx_lock 单飞 —— 并发请求各建一份会把内存与 GIL
    一起打满：2026-09-19 实测因子研究页并发 6 个请求把引擎顶到健康检查 3/3 失败、
    被看门狗当作「无响应」重启。"""
    key = _range_key(start, end, dataset)
    hit = _ctx_cache.get(key)
    if hit is not None:
        return hit
    with _ctx_lock:
        hit = _ctx_cache.get(key)  # 等锁期间可能已被别的请求建好
        if hit is not None:
            return hit
        ctx = _build_ctx(key, start, end, dataset)
        while len(_ctx_cache) >= _MAX_CTX:
            _ctx_cache.pop(next(iter(_ctx_cache)))  # 插入序 → 最旧的一份
        _ctx_cache[key] = ctx
        return ctx


def _build_derived(ctx: dict) -> dict:
    """派生量：全因子 top-30 序列 + 环境标签 + 时效标签（逐因子循环，最慢的一段）。"""
    p, mask = ctx["panel"], ctx["mask"]
    series30 = {c: scorecard.topn_series(p, p.index(c), 30, mask) for c in p.codes}
    bench_ret = ctx["bench_ret"]
    env = (
        scorecard.env_tags({c: s["ret"] for c, s in series30.items()}, bench_ret)
        if bench_ret is not None and len(bench_ret) >= 6
        else {}
    )
    ic = ctx["ic"]
    ttag = scorecard.time_tags(ic, p.codes, ctx["rdates"]) if ic is not None else {}
    return {"series30": series30, "env_tags": env, "time_tags": ttag}


def _range_ctx_full(
    start: str | None, end: str | None, dataset: str = "classic"
) -> dict:
    """轻上下文 + 派生量（排行榜 / 单因子详情专用）。

    派生量单独一把锁：它与轻上下文互不阻塞 —— 否则一次排行榜冷启动会把
    对比/合成这类秒级端点也堵上几十秒。
    """
    ctx = _range_ctx(start, end, dataset)
    key = ctx["_key"]
    d = _derived_cache.get(key)
    if d is None:
        with _derived_lock:
            d = _derived_cache.get(key)
            if d is None:
                d = _build_derived(ctx)
                _derived_cache.clear()  # 只留当前快照的一份
                _derived_cache[key] = d
    return {**ctx, **d}  # ctx 里的 "_key" 一并带上，调用方不关心


def _range_meta(ctx: dict) -> dict:
    rdates = ctx["rdates"]
    return {
        "start": str(pd.Timestamp(rdates[0]).date()) if len(rdates) else None,
        "end": str(pd.Timestamp(rdates[-1]).date()) if len(rdates) else None,
        "n_months": int(len(rdates)),
    }


# ---------------------------------------------------------------------------
# 排行榜
# ---------------------------------------------------------------------------
def _snapshot_maps(snap: pd.DataFrame | None) -> tuple[dict, dict]:
    """个股快照 → (symbol → 总市值亿, symbol → 行业)。

    排行榜要给 2754 个因子各查 30 只持仓（82,620 次）；原先是把 DataFrame 传进去逐行
    `snap_idx.loc[sym]`，每次都要现造一个 Series，实测 7.7s —— 占排行榜剩下的全部时间。
    预先转成 dict 后是纯哈希查表。
    """
    if snap is None or "symbol" not in snap.columns:
        return {}, {}
    syms = snap["symbol"]
    mv = (
        dict(zip(syms, snap["total_mv_yi"], strict=True))
        if "total_mv_yi" in snap
        else {}
    )
    ind = dict(zip(syms, snap["industry"], strict=True)) if "industry" in snap else {}
    return mv, ind


def _holdings_profile(
    p, fi: int, n: int, snap_mv: dict, snap_ind: dict
) -> tuple[float | None, str | None, list[dict]]:
    """最新截面 Top-N 持仓画像：(中位市值亿, 市值风格, 前三行业)。

    市值风格（按中位总市值）：≥500 亿大盘 · 100~500 亿中盘 · <100 亿小盘。
    """
    last = len(p.dates) - 1
    mvs: list[float] = []
    inds: dict[str, int] = {}
    for sym in p.sym_at(fi, last, n):
        mv = snap_mv.get(sym)
        if mv is not None and np.isfinite(mv):
            mvs.append(float(mv))
        ind = snap_ind.get(sym)
        if isinstance(ind, str) and ind:
            inds[ind] = inds.get(ind, 0) + 1
    med = round(float(np.median(mvs)), 1) if mvs else None
    style = None
    if med is not None:
        style = "大盘" if med >= 500 else ("中盘" if med >= 100 else "小盘")
    top_ind = [
        {"name": k, "count": v}
        for k, v in sorted(inds.items(), key=lambda kv: -kv[1])[:3]
    ]
    return med, style, top_ind


def _meta_map(dataset: str) -> dict[str, dict]:
    """code → 元数据（经典：catalog.py；私人：factors.json）。"""
    if dataset == "private":
        f = store.factors_meta() or {}
        return {e["code"]: e for e in f.get("factors", [])}
    return BY_CODE


def leaderboard(
    start: str | None = None,
    end: str | None = None,
    n: int = 30,
    dataset: str = "classic",
) -> dict:
    """排行榜。n=业绩 KPI 的持仓数（默认 30；标签恒按 top-30 基准自动判定）。"""
    ctx = _range_ctx_full(start, end, dataset)  # 用到 series30/env_tags/time_tags
    p, ic = ctx["panel"], ctx["ic"]
    fmeta = _meta_map(dataset)
    rdates = ctx["rdates"]
    n = max(1, min(int(n or 30), scorecard.MAX_SCAN_N))
    series = (
        ctx["series30"]
        if n == 30
        else {c: scorecard.topn_series(p, p.index(c), n, ctx["mask"]) for c in p.codes}
    )
    snap_mv, snap_ind = _snapshot_maps(store.stock_snapshot())
    rows = []
    ic_index = ctx["ic_index"]  # 全表分好组再逐因子取，别在循环里全表扫（22s → 秒级）
    for code in p.codes:
        meta = fmeta.get(code, {})
        s = series[code]
        kpi = scorecard.gate_kpi(dict(s["kpi"]))
        if ic is not None:
            kpi.update(scorecard.ic_stats_from(ic_index.get(code), rdates))
        ex = scorecard.excess_vs(ctx["benches"], kpi)
        med_mv, mv_style, top_ind = _holdings_profile(
            p, p.index(code), n, snap_mv, snap_ind
        )
        rows.append(
            {
                "code": code,
                "name_cn": meta.get("name_cn", code),
                "l1": meta.get("l1", ""),
                "l2": meta.get("l2", ""),
                **kpi,
                "excess_300": ex.get("000300.SH"),
                "excess_800": ex.get("000906.SH"),
                "excess_500": ex.get("000905.SH"),
                "median_mv_yi": med_mv,
                "mv_style": mv_style,
                "top_industries": top_ind,
                "env_tag": ctx["env_tags"].get(code, meta.get("env_tag", "")),
                "time_tag": ctx["time_tags"].get(code, meta.get("time_tag", "")),
            }
        )
    df = scorecard.composite_scores(pd.DataFrame(rows))
    df["insufficient"] = df["n_months"].fillna(0) < scorecard.MIN_MONTHS
    # 疑似未来函数：|IC|>0.15 或 |ICIR|>5 超出真实因子的物理上限
    # （features_daily 的 return_1d/3d/5d/10d/20d/60d 家族实测与次月收益相关性 0.2~0.94，是标签泄漏）
    df["suspicious"] = (df["ic_mean"].abs() > 0.15) | (df["ic_ir"].abs() > 5)
    df = df.sort_values(
        ["insufficient", "suspicious", "composite", "code"],
        ascending=[True, True, False, True],
    ).reset_index(drop=True)
    df["rank"] = np.arange(1, len(df) + 1)
    out = store._sanitize(df.to_dict("records"))
    m = store.factors_meta() if dataset == "private" else store.metrics()
    meta_out = (m or {}).get("meta", {})
    return {
        "leaderboard": out,
        "meta": {**meta_out, "range": _range_meta(ctx), "top_n": n, "dataset": dataset},
    }


# ---------------------------------------------------------------------------
# 单因子
# ---------------------------------------------------------------------------
def factor_detail(
    code: str,
    ns: list[int] | None = None,
    start: str | None = None,
    end: str | None = None,
    stocks_n: int = 30,
    dataset: str = "classic",
) -> dict | None:
    meta = _meta_map(dataset).get(code)
    if meta is None:
        return None
    ctx = _range_ctx_full(start, end, dataset)  # 用到 env_tags/time_tags
    p, ic = ctx["panel"], ctx["ic"]
    if code not in p.ci:
        return None
    fi = p.index(code)
    mask = ctx["mask"]

    wanted = sorted(
        {int(n) for n in (ns or [30]) if 1 <= int(n) <= scorecard.MAX_SCAN_N}
    ) or [30]
    variants = []
    for n in wanted:
        s = scorecard.topn_series(p, fi, n, mask)
        variants.append(
            {
                "n": n,
                "kpi": scorecard.gate_kpi(dict(s["kpi"])),
                "excess": scorecard.excess_vs(ctx["benches"], s["kpi"]),
                "nav": _points(s["dates"], s["nav"]),
            }
        )

    benches = [
        {
            "code": c,
            "name": scorecard.BENCH_NAMES[c],
            "kpi": b["kpi"],
            "nav": _points(b["dates"], b["nav"]),
        }
        for c, b in ctx["benches"].items()
    ]

    nscan = scorecard.nscan(p, fi, mask)

    ic_points: list[dict] = []
    ic_kpi = {"ic_mean": None, "ic_std": None, "ic_ir": None, "ic_win_rate": None}
    if ic is not None:
        sub = ic[ic["factor_code"] == code].set_index("trade_date")["ic"]
        sub = sub.reindex(pd.DatetimeIndex(ctx["rdates"])).dropna()
        ic_points = _points(sub.index, sub.to_numpy(dtype=float))
        ic_kpi = scorecard.ic_stats_from(ctx["ic_index"].get(code), ctx["rdates"])

    # 最新月末截面的 Top-N 个股表（始终为全样本最新截面）
    last = len(p.dates) - 1
    snap = store.stock_snapshot()
    snap_idx = snap.set_index("symbol") if snap is not None else None
    kk = min(int(stocks_n), p.fwd.shape[2])
    sym_codes = p.sym_codes(fi, last)[:kk]
    scores = p.score[fi, last, :kk]
    raws = p.raw[fi, last, :kk]
    stocks = []
    for r_i in range(kk):
        c_i = int(sym_codes[r_i])
        if c_i < 0:
            continue
        sym = p.symbols[c_i]
        row = {
            "rank": r_i + 1,
            "symbol": sym,
            "score": round(float(scores[r_i]), 3) if np.isfinite(scores[r_i]) else None,
            "raw": round(float(raws[r_i]), 4) if np.isfinite(raws[r_i]) else None,
        }
        if snap_idx is not None and sym in snap_idx.index:
            srow = snap_idx.loc[sym]
            row.update(
                {
                    "name": srow.get("name"),
                    "industry": srow.get("industry"),
                    "total_mv_yi": None
                    if pd.isna(srow.get("total_mv_yi"))
                    else round(float(srow["total_mv_yi"]), 1),
                    "pe_ttm": None
                    if pd.isna(srow.get("pe_ttm"))
                    else round(float(srow["pe_ttm"]), 2),
                    "pb": None
                    if pd.isna(srow.get("pb"))
                    else round(float(srow["pb"]), 2),
                    "avg_amount_yi": None
                    if pd.isna(srow.get("avg_amount_yi"))
                    else round(float(srow["avg_amount_yi"]), 2),
                }
            )
        stocks.append(row)

    ind_count: dict[str, int] = {}
    cap_count: dict[str, int] = {}
    for row in stocks:
        ind = row.get("industry") or "未知"
        ind_count[ind] = ind_count.get(ind, 0) + 1
        bucket = scorecard.cap_bucket(row.get("total_mv_yi"))
        if bucket:
            cap_count[bucket] = cap_count.get(bucket, 0) + 1
    industry_dist = [
        {"name": k, "count": v}
        for k, v in sorted(ind_count.items(), key=lambda kv: -kv[1])
    ]
    cap_dist = [
        {"name": label, "count": cap_count.get(label, 0)}
        for label in scorecard.CAP_LABELS
    ]

    out = {
        "code": code,
        "name_cn": meta["name_cn"],
        "display_name": meta.get("display_name", ""),
        "l1": meta["l1"],
        "l2": meta["l2"],
        "direction": meta["direction"],
        "description": meta["description"],
        "formula": meta["formula"],
        "wind_source": meta["wind_source"],
        "available": bool(meta["available"]),
        "env_tag": ctx["env_tags"].get(code, meta.get("env_tag", "")),
        "time_tag": ctx["time_tags"].get(code, meta.get("time_tag", "")),
        "range": _range_meta(ctx),
        "variants": variants,
        "benchmarks": benches,
        "nscan": nscan,
        "ic": ic_points,
        "ic_kpi": ic_kpi,
        "stocks": stocks,
        "stocks_date": str(pd.Timestamp(p.dates[last]).date()),
        "industry_dist": industry_dist,
        "cap_dist": cap_dist,
    }
    return store._sanitize(out)


# ---------------------------------------------------------------------------
# 多因子对比
# ---------------------------------------------------------------------------
def compare(
    items: list[dict] | list[str],
    start: str | None = None,
    end: str | None = None,
    dataset: str = "classic",
) -> dict:
    ctx = _range_ctx(start, end, dataset)
    p = ctx["panel"]
    fmeta = _meta_map(dataset)
    norm: list[dict] = []
    for it in items[:12]:
        if isinstance(it, str):
            norm.append({"code": it, "n": 30})
        else:
            norm.append({"code": str(it.get("code")), "n": int(it.get("n") or 30)})
    out = []
    for it in norm:
        code, n = it["code"], max(1, min(int(it["n"]), scorecard.MAX_SCAN_N))
        meta = fmeta.get(code)
        if meta is None or code not in p.ci:
            continue
        s = scorecard.topn_series(p, p.index(code), n, ctx["mask"])
        ic_points = []
        if ctx["ic"] is not None:
            sub = ctx["ic"][ctx["ic"]["factor_code"] == code].set_index("trade_date")[
                "ic"
            ]
            sub = sub.reindex(pd.DatetimeIndex(ctx["rdates"])).dropna()
            ic_points = _points(sub.index, sub.to_numpy(dtype=float))
        out.append(
            {
                "code": code,
                "name_cn": meta["name_cn"],
                "l1": meta["l1"],
                "l2": meta["l2"],
                "n": n,
                "kpi": {
                    **s["kpi"],
                    **scorecard.ic_stats_from(ctx["ic_index"].get(code), ctx["rdates"]),
                }
                if ctx["ic"] is not None
                else s["kpi"],
                "excess": scorecard.excess_vs(ctx["benches"], s["kpi"]),
                "nav": _points(s["dates"], s["nav"]),
                "ic": ic_points,
            }
        )

    corr = (
        store.corr_table() if dataset != "private" else None
    )  # 私人库暂不构建全量相关矩阵
    corr_sub = None
    codes = [x["code"] for x in out]
    if corr is not None and not corr.empty and codes:
        s = set(codes)
        cs = corr[corr["factor_a"].isin(s) & corr["factor_b"].isin(s)]
        corr_sub = [
            {
                "factor_a": str(r["factor_a"]),
                "factor_b": str(r["factor_b"]),
                "corr": float(r["corr"]) if np.isfinite(r["corr"]) else None,
            }
            for r in cs.to_dict("records")
        ]
    benches = [
        {
            "code": c,
            "name": scorecard.BENCH_NAMES[c],
            "nav": _points(b["dates"], b["nav"]),
        }
        for c, b in ctx["benches"].items()
    ]
    return {
        "factors": out,
        "corr": corr_sub,
        "benchmarks": benches,
        "range": _range_meta(ctx),
    }


def correlation(codes: list[str] | None = None) -> dict:
    corr = store.corr_table()
    if corr is None or corr.empty:
        return {"pairs": []}
    if codes:
        s = set(codes)
        corr = corr[corr["factor_a"].isin(s) | corr["factor_b"].isin(s)]
    return {
        "pairs": [
            {
                "factor_a": str(r["factor_a"]),
                "factor_b": str(r["factor_b"]),
                "corr": float(r["corr"]) if np.isfinite(r["corr"]) else None,
            }
            for r in corr.to_dict("records")
        ]
    }


def screening() -> dict:
    """因子筛选清单（质量门槛 + 同源去重，含剔除原因）。"""
    return store.screening()


# ---------------------------------------------------------------------------
# 快照管理：状态探测 + 一键计算（全部本地计算，不上传任何数据）
# ---------------------------------------------------------------------------
_BUILD_LOG = "build.log"
_BUILD_PID = "build.pid"


def _build_running(d: Path) -> int | None:
    """返回正在运行的构建进程 PID（无则 None）。带 cmdline 校验防 PID 复用。"""
    pf = d / _BUILD_PID
    if not pf.exists():
        return None
    try:
        pid = int(pf.read_text().strip())
        os.kill(pid, 0)
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        return None
    try:  # PID 复用防护：必须是 build_factor_research 的进程
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="ignore")
        if "build_factor" not in cmdline:
            return None
    except OSError:
        pass
    return pid


def snapshot_status(dataset: str = "classic") -> dict:
    """快照状态：是否已生成 / 构建中 / 日志进度。供前端「一键计算」入口。"""
    d = store.artifact_dir(dataset)
    meta: dict = {}
    mf = d / "metrics.json"  # 构建元信息内嵌在 metrics.json 的 meta 字段
    if mf.exists():
        try:
            meta = json.loads(mf.read_text(encoding="utf-8")).get("meta", {})
        except Exception:  # noqa: BLE001 - 元信息损坏不阻塞状态查询
            meta = {}
    pid = _build_running(d)
    log_tail: list[str] = []
    step = ""
    lf = d / _BUILD_LOG
    if lf.exists():
        try:
            lines = lf.read_text(encoding="utf-8", errors="ignore").splitlines()
            log_tail = [ln for ln in lines[-40:] if ln.strip()][-12:]
            for ln in reversed(lines):
                if ln.startswith("["):
                    step = ln[:80]
                    break
        except OSError:
            pass
    return {
        "exists": (d / "factor_panel.parquet").exists(),
        "running": pid is not None,
        "built_at": meta.get("built_at"),
        "window": meta.get("window"),
        "n_factors": meta.get("n_factors") or meta.get("n_factors_computed"),
        "n_dates": meta.get("n_dates"),
        "step": step,
        "log_tail": log_tail,
        "dataset": dataset,
    }


_BUILD_SCRIPTS = {
    "classic": "build_factor_research.py",
    "private": "build_factor_panel_private.py",
}


def start_build(dataset: str = "classic") -> dict:
    """启动快照构建（后台子进程；已在构建则直接返回运行中）。全部本地计算。"""
    d = store.artifact_dir(dataset)
    if (pid := _build_running(d)) is not None:
        return {"started": False, "running": True, "pid": pid}
    d.mkdir(parents=True, exist_ok=True)
    root = (
        Path(__file__).resolve().parents[4]
    )  # backend/services/engine/factor_research → 仓库根
    script = (
        root
        / "backend"
        / "scripts"
        / _BUILD_SCRIPTS.get(dataset, "build_factor_research.py")
    )
    if not script.exists():
        return {"error": f"构建脚本缺失: {script}"}
    log = open(d / _BUILD_LOG, "a", encoding="utf-8")  # noqa: SIM115 - 交给子进程持有
    log.write(
        f"\n===== build started {pd.Timestamp.now().isoformat(timespec='seconds')} =====\n"
    )
    proc = subprocess.Popen(  # noqa: S603 - 固定脚本路径，无用户输入
        [sys.executable, str(script)],
        cwd=str(root),
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    (d / _BUILD_PID).write_text(str(proc.pid), encoding="utf-8")
    return {"started": True, "running": True, "pid": proc.pid, "dataset": dataset}


# ---------------------------------------------------------------------------
# 多因子合成（在线现算）
# ---------------------------------------------------------------------------
def _scores_in_range(
    scores: pd.DataFrame, start: str | None, end: str | None
) -> pd.DataFrame:
    if not start and not end:
        return scores
    dates = pd.DatetimeIndex(sorted(pd.to_datetime(scores["trade_date"]).unique()))
    mask = scorecard.month_mask(dates.to_numpy(), start, end)
    keep = set(dates[mask])
    return scores[pd.to_datetime(scores["trade_date"]).isin(keep)]


def compose(
    weights: dict[str, float],
    top_n: int = 30,
    threshold: float | None = None,
    thresholds: dict[str, float] | None = None,
    cost_rate: float | None = None,
    start: str | None = None,
    end: str | None = None,
    dataset: str = "classic",
) -> dict:
    """自定义权重合成 +（可选）阈值过滤 → 实时回测。

    weights: {factor_code: 权重}（内部按 Σ|w| 归一）
    threshold: 合成打分的过滤下限（全局，z 刻度；None=不过滤）
    thresholds: 每因子过滤下限 {code: z}（选股前先按各因子阈值筛股）
    """
    fmeta = _meta_map(dataset)
    weights = {c: float(w) for c, w in (weights or {}).items() if c in fmeta and w != 0}
    if not weights:
        return {"error": "weights 为空"}
    codes = list(weights)
    scores = store.scores_for(codes, dataset)
    if scores is None or scores.empty:
        return {"error": "快照缺失（请在「快照」中一键计算）"}
    scores = _scores_in_range(scores, start, end)
    if scores.empty:
        return {"error": "所选区间内没有月末截面数据"}

    # 每因子阈值：先筛股（保留同时满足全部已设阈值因子的股票）
    th = {
        c: float(v)
        for c, v in (thresholds or {}).items()
        if c in weights and v is not None
    }
    if th:
        sub = scores[scores["factor_code"].isin(th)]
        sub = sub.assign(_pass=sub["score"] >= sub["factor_code"].map(th))
        g = sub.groupby(["trade_date", "symbol"], as_index=False).agg(
            n=("_pass", "size"), ok=("_pass", "all")
        )
        keep = g[(g["n"] == len(th)) & g["ok"]][["trade_date", "symbol"]]
        scores = scores.merge(keep, on=["trade_date", "symbol"], how="inner")
        if scores.empty:
            return {"error": "阈值过滤后无剩余股票（放宽容忍度或减少阈值因子）"}

    wsum = sum(abs(w) for w in weights.values())
    scores = scores.assign(
        _contrib=scores["score"].astype("float64")
        * scores["factor_code"].map(dict(weights)).astype("float64")
    )
    comp = scores.groupby(["trade_date", "symbol"], as_index=False)["_contrib"].sum()
    comp["score"] = comp["_contrib"] / wsum
    wide = comp.pivot(index="trade_date", columns="symbol", values="score")
    wide.index = pd.to_datetime(wide.index)
    wide = wide.sort_index()

    fwd = store.fwd_returns(dataset)
    fwd_wide = fwd.pivot(index="trade_date", columns="symbol", values="fwd_ret")
    fwd_wide.index = pd.to_datetime(fwd_wide.index)
    common = wide.index.intersection(fwd_wide.index)
    wide, fwd_wide = wide.loc[common], fwd_wide.loc[common].sort_index()
    if threshold is not None:
        wide = wide.where(wide >= float(threshold))

    bt = analysis.backtest_topn(
        wide,
        fwd_wide,
        top_n=int(top_n),
        cost_rate=cost_rate or analysis.COST_RATE,
    )
    k = analysis.kpi(bt["ret"], bt["nav"])
    ctx = _range_ctx(start, end, dataset)
    k_ex = scorecard.excess_vs(ctx["benches"], k)

    names = _instrument_names()
    last_date = max(bt["holdings"]) if bt["holdings"] else None
    latest = (
        wide.loc[last_date].dropna().sort_values(ascending=False)
        if last_date is not None
        else pd.Series(dtype=float)
    )
    latest_rows = [
        {
            "symbol": s,
            "name": names.get(s, {}).get("name", s),
            "industry": names.get(s, {}).get("industry", ""),
            "score": round(float(v), 3),
        }
        for s, v in latest.head(int(top_n)).items()
    ]
    benches = [
        {
            "code": c,
            "name": scorecard.BENCH_NAMES[c],
            "nav": _points(b["dates"], b["nav"]),
        }
        for c, b in ctx["benches"].items()
    ]
    out = {
        "kpi": k,
        "excess": k_ex,
        "nav": _points(bt["nav"].index, bt["nav"].to_numpy()),
        "turnover": _points(bt["turnover"].index, bt["turnover"].to_numpy()),
        "benchmarks": benches,
        "holdings": latest_rows,
        "holdings_date": str(last_date.date()) if last_date is not None else None,
        "weights": weights,
        "thresholds": th or None,
        "top_n": int(top_n),
        "threshold": threshold,
        "range": _range_meta(ctx),
    }
    return store._sanitize(out)


# ---------------------------------------------------------------------------
# 最优权重（粗网格 + 逐目标）
# ---------------------------------------------------------------------------
def _weight_grid(k: int) -> list[list[float]]:
    """非负、和为 1 的粗网格（整数分份枚举，组合数 ≤ 900；k>10 时随机采样）。"""
    if k > 10:
        rng = np.random.default_rng(42)
        w = rng.dirichlet(np.ones(k), size=800)
        return [list(np.round(x, 3)) for x in w]
    d = 2
    for cand in range(2, 25):
        n = 1
        for i in range(k - 1):
            n = n * (cand + k - 1 - i) // (i + 1)
        if n <= 900:
            d = cand
        else:
            break
    combos: list[list[float]] = []

    def rec(rem: int, parts: list[int]) -> None:
        if len(parts) == k - 1:
            combos.append([*parts, rem])
            return
        for x in range(rem + 1):
            rec(rem - x, [*parts, x])

    rec(d, [])
    return [[c / d for c in combo] for combo in combos]


def optimal_weights(
    codes: list[str],
    top_n: int = 30,
    start: str | None = None,
    end: str | None = None,
    dataset: str = "classic",
) -> dict:
    """在所选因子上网格搜索（非负、和为 1），夏普/年化/超额各给一组最优权重。

    候选池 = 各因子月末 top-150 的并集（面板限制），组合内缺失因子记 0 分（与 compose 同口径）；
    展示口径以 compose 全样本精确回测为准。
    """
    t0 = time.time()
    ctx = _range_ctx(start, end, dataset)
    p = ctx["panel"]
    codes = [c for c in codes if c in p.ci][:10]
    if len(codes) < 1:
        return {"error": "至少选 1 个可用因子"}
    k = len(codes)
    fis = [p.index(c) for c in codes]
    idx = np.where(ctx["mask"])[0]
    months = idx[:-1]
    if len(months) < 6:
        return {"error": "区间过短（至少 6 个月）"}

    pools = []
    syms_all = p.symbols
    for a in months:
        sym_list: list[str] = []
        for fi in fis:
            sym_list.extend(p.sym_at(fi, a))
        syms = sorted(set(sym_list))
        si = {s: i for i, s in enumerate(syms)}
        S = np.zeros((len(syms), k), dtype=np.float32)
        M = np.zeros((len(syms), k), dtype=np.float32)
        F = np.full(len(syms), np.nan, dtype=np.float32)
        for j, fi in enumerate(fis):
            codes_row = p.sym_codes(fi, a)
            for r_i in range(len(codes_row)):
                c_i = int(codes_row[r_i])
                if c_i < 0:
                    continue
                i = si[syms_all[c_i]]
                S[i, j] = p.score[fi, a, r_i]
                M[i, j] = 1.0
                fv = p.fwd[fi, a, r_i]
                if np.isfinite(fv):
                    F[i] = fv
        pools.append((np.asarray(syms), S, M, F))

    grid = _weight_grid(k)
    W = np.asarray(grid, dtype=np.float64).T  # k × C
    C = W.shape[1]
    T = len(pools)
    rets = np.full((T, C), np.nan)
    prev_top: list[set] = [set() for _ in range(C)]
    cost = analysis.COST_RATE
    for t in range(T):
        syms, S, M, F = pools[t]
        P = (S @ W) / (M @ W)
        kk = min(int(top_n), P.shape[0])
        part = np.argpartition(-P, kk - 1, axis=0)[:kk]
        for c_i in range(C):
            sel = part[:, c_i]
            f = F[sel]
            r = float(np.nanmean(f)) if np.isfinite(f).any() else np.nan
            cur = set(syms[sel].tolist())
            to = 1.0 if not prev_top[c_i] else len(cur - prev_top[c_i]) / max(kk, 1)
            rets[t, c_i] = (r - to * cost) if np.isfinite(r) else np.nan
            prev_top[c_i] = cur

    mean = np.nanmean(rets, axis=0)
    sd = np.nanstd(rets, axis=0, ddof=1)
    sharpe = np.where(sd > 0, mean / np.maximum(sd, 1e-12) * np.sqrt(12), np.nan)
    nav_path = np.vstack([np.ones(C), np.nan_to_num(1 + rets, nan=1.0).cumprod(axis=0)])
    nav_final = nav_path[-1]
    years = max(T / 12.0, 1 / 12.0)
    annual = np.where(nav_final > 0, np.power(nav_final, 1 / years) - 1, -1.0)
    peak = np.maximum.accumulate(nav_path, axis=0)
    mdd = (1 - nav_path / peak).max(axis=0)
    win = np.nanmean(rets > 0, axis=0)
    prim = ctx["benches"].get(scorecard.BENCH_PRIMARY, {}).get("kpi", {})
    bench_ann = prim.get("annual_return")
    excess = annual - bench_ann if bench_ann is not None else np.full(C, np.nan)

    def _winner(values: np.ndarray) -> dict | None:
        if not np.isfinite(values).any():
            return None
        i = int(np.nanargmax(values))
        return {
            "weights": {
                c: round(float(w), 3) for c, w in zip(codes, W[:, i], strict=False)
            },
            "sharpe": round(float(sharpe[i]), 3) if np.isfinite(sharpe[i]) else None,
            "annual_return": round(float(annual[i]), 4),
            "max_drawdown": round(float(mdd[i]), 4),
            "win_rate": round(float(win[i]), 4),
            "excess_300": round(float(excess[i]), 4)
            if np.isfinite(excess[i])
            else None,
        }

    out = {
        "objectives": {
            "sharpe": _winner(sharpe),
            "annual_return": _winner(annual),
            "excess_300": _winner(excess),
        },
        "n_combos": int(C),
        "n_months": int(T),
        "codes": codes,
        "elapsed_ms": int((time.time() - t0) * 1000),
    }
    return store._sanitize(out)
