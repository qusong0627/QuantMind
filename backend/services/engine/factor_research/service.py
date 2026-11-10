"""因子研究 —— 服务层：目录 / 排行榜 / 单因子 / 对比 / 合成 / 最优权重 / 快照管理。

数据分层：
- 月末名次面板（store.panel）→ 任意 N、任意区间、N 扫描、标签、综合分（scorecard.py）；
- 月末打分长表（store.scores_for）→ 合成回测在线现算（与旧版一致，周初到周末毫秒级）；
- 序列与元数据（ic.parquet / benchmarks.parquet / stock_snapshot.parquet）。

市场分层（2026-10-16）：全部对外函数带 ``market``（缺省 ``"CN"``，零行为变化），
透传到 store.*（按市场根解析产物目录）与 scorecard 基准表。非 CN 的三处口径差异
在各取数点收口：基准表按市场（``scorecard.benches_for_market``，FUTURES 空表 →
无超额）、个股名称/行业走 ``_instrument_names`` 的市场分支（CN=instrument_detail，
非 CN=私人快照截面）、毕业管道（CUSTOM→CN）为 CN 专属（非 CN 面置 None）。

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

from backend.services.engine.factor_research import (
    analysis,
    dedup as dedup_lib,
    discovery,
    scorecard,
    store,
    taxonomy,
    windows as frwindows,
)
from backend.services.engine.factor_research.catalog import BY_CODE, FACTORS, L1_ORDER


def _points(dates, values) -> list[dict]:
    return [
        {"date": str(pd.Timestamp(d).date()), "value": round(float(v), 6)}
        for d, v in zip(dates, values, strict=False)
        if np.isfinite(v)
    ]


def _market_key(market: str) -> str:
    """市场键规范化（"hk" → "HK"）；未知市场抛 ValueError，绝不落 CN。

    深层的 store/scorecard 也各自校验（fail-closed 双保险），此处只负责让
    对外响应里带的是**规范键**（前端按它比对市场，别让 "hk"/"HK" 两副面孔）。
    """
    from backend.shared.feature_source import validate_market

    return validate_market(market)


def _instrument_names(market: str = "CN") -> dict[str, dict]:
    """symbol → {name, industry}（缺失时优雅降级为空表，前端回退 symbol）。

    CN：instrument_detail（含申万行业）；非 CN：私人快照截面（stock_snapshot）——
    构建器已按 security_master/sector 组装名称与行业（HK 行业实测全空、FUTURES
    两者皆无 → 相应字段为空串），正是排行榜/单因子看的同一份数据，不另拼一套。
    """
    from backend.shared.feature_source import validate_market

    if validate_market(market) != "CN":
        snap = store.stock_snapshot(market)
        if snap is None or "symbol" not in snap.columns:
            return {}
        out: dict[str, dict] = {}
        for _, r in snap.iterrows():
            name = r.get("name")
            ind = r.get("industry")
            out[str(r["symbol"])] = {
                "name": name if isinstance(name, str) and name else str(r["symbol"]),
                "industry": ind if isinstance(ind, str) else "",
            }
        return out
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
def catalog(dataset: str = "classic", market: str = "CN") -> dict:
    """因子目录 + 快照元信息 + **本市场**基准清单（前端基准控件的数据源）。"""
    mk = _market_key(market)
    benches = [{"symbol": c, "name": n} for c, n in scorecard.benches_for_market(mk)]
    if dataset == "private":
        f = store.factors_meta("private", mk)
        if f is None:
            raise store.SnapshotMissing(
                "私人因子库快照缺失（请先一键计算，产物 factor_research_private/factors.json）"
            )
        # 语义类别/中文名**读取时富化**（taxonomy 单一实现；不动 factors.json 产物、可回滚）。
        # 私人库 l2 实测零信息量（每库恒等于库名），前端第三级树改按 category 分组。
        items = [
            {**taxonomy.enrich(e), "env_tag": "", "time_tag": ""}
            for e in f.get("factors", [])
        ]
        return {
            "factors": items,
            "l1_order": f.get("l1_order", []),
            "l2_order": f.get("l2_order", {}),
            "benchmarks": benches,
            "market": mk,
            "meta": f.get("meta", {}),
        }
    m = store.metrics(dataset, mk)
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
        "benchmarks": benches,
        "market": mk,
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
_MAX_CTX = 2  # 窗口化后每份 ctx 持有一个 Panel（10 年 private ≈1.4G）；只留最近 2 个窗口


def _range_key(start: str | None, end: str | None, dataset: str, market: str) -> tuple:
    # market 必须进键：否则先构建的市场会被另一市场按 (区间, 数据集) 命中缓存，
    # 港股页面戴 A 股面板 —— 全程不报错（store 侧面板缓存已按市场分键，
    # 这里漏了等于把上面一层防住了、这一层又漏出去）。
    return (
        start or "",
        end or "",
        dataset,
        market,
        store.panel_stamp(dataset, market, start, end),
    )


def _build_ctx(
    key: tuple, start: str | None, end: str | None, dataset: str, market: str
) -> dict:
    p = store.panel(dataset, market, start, end)
    if p is None:
        raise store.SnapshotMissing(
            f"factor_panel.parquet 缺失（数据集 {dataset}；请先跑构建或点「快照-一键计算」）"
        )
    bench = store.benchmark_table(dataset, market)
    ic = store.ic_table(dataset, market)
    mask = scorecard.month_mask(p.dates, start, end)
    order = scorecard.bench_order_for_market(market)
    benches = (
        scorecard.bench_series(bench, mask, p.dates, order) if bench is not None else {}
    )
    rdates = p.dates[mask]
    bench_ret = None
    primary = order[0] if order else None
    prim = benches.get(primary) if primary is not None else None
    if prim is not None and len(prim["nav"]) > 1:
        nav = prim["nav"]
        bench_ret = nav[1:] / nav[:-1] - 1
    cov = store.coverage(dataset, market) or {}
    return {
        "_key": key,
        "dataset": dataset,
        "market": market,
        "bench_order": order,
        "bench_names": scorecard.bench_names_for_market(market),
        "panel": p,
        "mask": mask,
        "rdates": rdates,
        "benches": benches,
        "ic": ic,
        "ic_index": scorecard.ic_index(ic) if ic is not None else {},
        "bench_ret": bench_ret,
        "snapshot_meta": store.window_meta(dataset, market),
        # 覆盖率全文（每因子）很大，ctx 只留摘要；需要逐因子时走 coverage 端点/文件。
        "coverage_summary": {
            k: cov.get(k)
            for k in (
                "window",
                "survivorship_bias",
                "survivorship_threshold",
                "direction_calibration",
                "lookback_start",
            )
        },
        # 逐月覆盖（131 行，很小）：窗口级 survivorship 必须按**当前切片**判，
        # 不能拿全库一个 flag 盖到所有年份上（2020+ 覆盖好、2016 缺口大）。
        "coverage_months": cov.get("months", []),
    }


def _range_ctx(
    start: str | None,
    end: str | None,
    dataset: str = "classic",
    market: str = "CN",
) -> dict:
    """区间轻上下文。冷构建持 _ctx_lock 单飞 —— 并发请求各建一份会把内存与 GIL
    一起打满：2026-09-19 实测因子研究页并发 6 个请求把引擎顶到健康检查 3/3 失败、
    被看门狗当作「无响应」重启。"""
    mk = _market_key(market)
    key = _range_key(start, end, dataset, mk)
    hit = _ctx_cache.get(key)
    if hit is not None:
        return hit
    with _ctx_lock:
        hit = _ctx_cache.get(key)  # 等锁期间可能已被别的请求建好
        if hit is not None:
            return hit
        ctx = _build_ctx(key, start, end, dataset, mk)
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
    start: str | None,
    end: str | None,
    dataset: str = "classic",
    market: str = "CN",
) -> dict:
    """轻上下文 + 派生量（排行榜 / 单因子详情专用）。

    派生量单独一把锁：它与轻上下文互不阻塞 —— 否则一次排行榜冷启动会把
    对比/合成这类秒级端点也堵上几十秒。
    """
    ctx = _range_ctx(start, end, dataset, market)
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
    snap_meta = ctx.get("snapshot_meta") or {}
    cov = ctx.get("coverage_summary") or {}
    # 窗口级退市缺口：只统计当前切片覆盖到的月份（全库 flag 会误标覆盖良好的年份）
    sel = {str(pd.Timestamp(d).date()) for d in rdates}
    thr = float(cov.get("survivorship_threshold") or 0.05)
    months = ctx.get("coverage_months") or []
    if months:
        def _gap(m: dict) -> float:
            # 优先用逐库审计（行存在性下界）；旧快照/未审计时退回并集口径
            v = m.get("worst_library_missing")
            return float(v if v is not None else (m.get("missing_ratio") or 0))
        surv = any(
            m.get("date") in sel
            and _gap(m) > thr
            and int(m.get("kline_symbols") or 0) > 0
            for m in months
        )
    else:
        surv = bool(cov.get("survivorship_bias", snap_meta.get("survivorship_bias", False)))
    return {
        "start": str(pd.Timestamp(rdates[0]).date()) if len(rdates) else None,
        "end": str(pd.Timestamp(rdates[-1]).date()) if len(rdates) else None,
        "n_months": int(len(rdates)),
        "market": ctx["market"],
        # 主库全窗口（与用户选中的 start/end 不同：前者是快照边界，后者是切片）
        "window": cov.get("window") or snap_meta.get("window"),
        "lookback_start": cov.get("lookback_start") or snap_meta.get("lookback_start"),
        "direction_calibration": cov.get("direction_calibration")
        or snap_meta.get("direction_calibration"),
        # 历史窗口的退市股覆盖缺口：true 表示该窗口只能做研究、不得作为验收依据
        "survivorship_bias": surv,
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

    截面取该因子**自身**最近有数据的月份：断更因子（面板末月无行）取全局末月
    会得到空持仓 → 中位市值/风格/行业全空。
    """
    last = scorecard.latest_data_month(p, fi)
    if last is None:
        return None, None, []
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


def _meta_map(dataset: str, market: str = "CN") -> dict[str, dict]:
    """code → 元数据（经典：catalog.py；私人：factors.json，读取时经 taxonomy 富化）。"""
    if dataset == "private":
        f = store.factors_meta("private", market) or {}
        # 富化后 → leaderboard / factor_detail / compare / compose 全链带
        # category/category_name（enrich 返回新 dict，不改 store 缓存对象）
        return {e["code"]: taxonomy.enrich(e) for e in f.get("factors", [])}
    return BY_CODE


def leaderboard(
    start: str | None = None,
    end: str | None = None,
    n: int = 30,
    dataset: str = "classic",
    dedup: float = 0.0,
    market: str = "CN",
) -> dict:
    """排行榜。n=业绩 KPI 的持仓数（默认 30；标签恒按 top-30 基准自动判定）。

    dedup > 0：同源去重（|ρ| ≥ dedup 的因子只保留榜单更靠前者，口径同筛选页），
    剔除后重新编号 rank 1..N（composite 原值不动）；相关矩阵缺失抛
    ``store.DedupMatrixMissing``（路由译 409）。
    """
    mk = _market_key(market)
    ctx = _range_ctx_full(start, end, dataset, mk)  # 用到 series30/env_tags/time_tags
    p, ic = ctx["panel"], ctx["ic"]
    fmeta = _meta_map(dataset, mk)
    rdates = ctx["rdates"]
    n = max(1, min(int(n or 30), scorecard.MAX_SCAN_N))
    series = (
        ctx["series30"]
        if n == 30
        else {c: scorecard.topn_series(p, p.index(c), n, ctx["mask"]) for c in p.codes}
    )
    snap_mv, snap_ind = _snapshot_maps(
        store.stock_snapshot(mk, as_of=rdates[-1] if len(rdates) else None)
    )
    rows = []
    ic_index = ctx["ic_index"]  # 全表分好组再逐因子取，别在循环里全表扫（22s → 秒级）
    for code in p.codes:
        meta = fmeta.get(code, {})
        s = series[code]
        kpi = scorecard.gate_kpi(dict(s["kpi"]))
        if ic is not None:
            kpi.update(scorecard.ic_stats_from(ic_index.get(code), rdates))
        ex = scorecard.excess_vs(ctx["benches"], kpi, ctx["bench_order"])
        med_mv, mv_style, top_ind = _holdings_profile(
            p, p.index(code), n, snap_mv, snap_ind
        )
        row = {
            "code": code,
            "name_cn": meta.get("name_cn", code),
            "l1": meta.get("l1", ""),
            "l2": meta.get("l2", ""),
            # 经典库无 category（前端回退 l2 分组）；private 行带语义类别供点选过滤
            "category": meta.get("category", ""),
            "category_name": meta.get("category_name", ""),
            **kpi,
            # 按基准代码映射的超额（前端统一读它；CN 三码 = 300/800/500）
            "excess_by_bench": ex,
        }
        if mk == "CN":
            # 旧键一期兼容（CN 榜单列已发布）；非 CN 不发这三个键 ——
            # "excess_300" 挂在恒生基准上是一句假话，宁可整列不存在。
            row.update(
                {
                    "excess_300": ex.get("000300.SH"),
                    "excess_800": ex.get("000906.SH"),
                    "excess_500": ex.get("000905.SH"),
                }
            )
        rows.append(
            {
                **row,
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
    dedup_info: dict[str, object] = {"applied": False}
    if dedup and float(dedup) > 0:
        threshold = float(dedup)
        bundle = store.dedup_corr(dataset, mk)
        if bundle is None:
            raise store.DedupMatrixMissing(
                f"去重相关矩阵缺失（dataset={dataset}）：private 请先运行 "
                "backend/scripts/build_factor_panel_corr.py；"
                "classic 请重建经典快照（build_factor_research.py）"
            )
        names, matrix = bundle
        codes_all = df["code"].tolist()
        # 榜单序贪心：先见者（榜单更靠前）优先保留——与筛选页同一种「留代表」，
        # 判据各自随页（筛选页按强度序，这里按 composite 榜单序）
        kept, dropped, _dup_of = dedup_lib.greedy_dedup(
            codes_all, names, matrix, threshold
        )
        keep_set = set(kept)
        df = df[df["code"].isin(keep_set)].reset_index(drop=True)
        dedup_info = {
            "applied": True,
            "threshold": threshold,
            "n_before": len(codes_all),
            "dropped": len(dropped),
            "covered": len(set(codes_all) & set(names)),
        }
    df["rank"] = np.arange(1, len(df) + 1)
    out = store._sanitize(df.to_dict("records"))
    m = (
        store.factors_meta("private", mk)
        if dataset == "private"
        else store.metrics(dataset, mk)
    )
    meta_out = (m or {}).get("meta", {})
    return {
        "leaderboard": out,
        "meta": {
            **meta_out,
            "range": _range_meta(ctx),
            "top_n": n,
            "dataset": dataset,
            "market": mk,
            # 本市场基准清单（代码+名）——前端榜单超额列按它渲染，与结果里的
            # excess_by_bench 键一一对应（无基准市场为空表）。
            "benchmarks": [
                {"code": c, "name": ctx["bench_names"].get(c, c)}
                for c in ctx["bench_order"]
            ],
            "dedup": dedup_info,
        },
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
    market: str = "CN",
) -> dict | None:
    mk = _market_key(market)
    meta = _meta_map(dataset, mk).get(code)
    if meta is None:
        return None
    ctx = _range_ctx_full(start, end, dataset, mk)  # 用到 env_tags/time_tags
    p, ic = ctx["panel"], ctx["ic"]
    if code not in p.ci:
        # 因子存在、但所选窗口内没有有效截面（如 l2/gap/rd/minute 在 2016–2019）。
        # 不能 404：目录里能点到它，404 会让人以为整个因子坏了。给一个明确
        # 「该窗口无数据 + 数据自何时起」的空详情，前端渲染空表而不是报错。
        cov = store.coverage(dataset, mk) or {}
        fcov = (cov.get("factors") or {}).get(code) or {}
        reason = "该因子在所选窗口内无有效截面"
        if fcov.get("first_valid"):
            reason += f"（该因子数据自 {fcov['first_valid']} 起）"
        return store._sanitize(
            {
                "code": code,
                "name_cn": meta.get("name_cn", code),
                "display_name": meta.get("display_name", ""),
                "l1": meta.get("l1", ""),
                "l2": meta.get("l2", ""),
                "direction": int(meta.get("direction", 1)),
                "description": meta.get("description", ""),
                "formula": meta.get("formula", ""),
                "wind_source": meta.get("wind_source", ""),
                "source_library": discovery.source_of(meta.get("wind_source", "")),
                "available": bool(meta.get("available", True)),
                "unavailable_reason": reason,
                "env_tag": ctx["env_tags"].get(code, meta.get("env_tag", "")),
                "time_tag": ctx["time_tags"].get(code, meta.get("time_tag", "")),
                "range": _range_meta(ctx),
                "variants": [],
                "benchmarks": [],
                "nscan": [],
                "ic": [],
                "ic_kpi": {
                    "ic_mean": None,
                    "ic_std": None,
                    "ic_ir": None,
                    "ic_win_rate": None,
                },
                "stocks": [],
                "stocks_date": None,
                "stocks_stale": False,
                "industry_dist": [],
                "cap_dist": [
                    {"name": label, "count": 0} for label in scorecard.CAP_LABELS
                ],
            }
        )
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
                # bench_order 必须传：缺省是 CN 三码，非 CN 市场下 excess 全体
                # 按键错位 → 全 None，前端按本市场基准码取数取空（看着像没数据）。
                "excess": scorecard.excess_vs(
                    ctx["benches"], s["kpi"], ctx["bench_order"]
                ),
                "nav": _points(s["dates"], s["nav"]),
            }
        )

    benches = [
        {
            "code": c,
            "name": ctx["bench_names"].get(c, c),
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

    # 最新月末截面的 Top-N 个股表（始终为该因子自身的最新截面）。
    # 末月可能整月缺该因子的行（源库断更 → 面板不落行，2026-10-09 rd_mined/
    # gap_mined 实测）：取全局末月会得到空表 + 假日期 —— 回退到该因子自身最近
    # 有数据的截面；stocks_stale 供前端提示「数据未更新到最新截面」。
    last = scorecard.latest_data_month(p, fi)
    stocks_stale = last is not None and last < len(p.dates) - 1
    snap = store.stock_snapshot(
        mk, as_of=p.dates[last] if last is not None else None
    )
    snap_idx = snap.set_index("symbol") if snap is not None else None
    kk = min(int(stocks_n), p.fwd.shape[2])
    stocks: list[dict] = []
    stocks_date: str | None = None
    if last is not None:
        stocks_date = str(pd.Timestamp(p.dates[last]).date())
        sym_codes = p.sym_codes(fi, last)[:kk]
        scores = p.score[fi, last, :kk]
        raws = p.raw[fi, last, :kk]
        for r_i in range(kk):
            c_i = int(sym_codes[r_i])
            if c_i < 0:
                continue
            sym = p.symbols[c_i]
            row = {
                "rank": r_i + 1,
                "symbol": sym,
                "score": round(float(scores[r_i]), 3)
                if np.isfinite(scores[r_i])
                else None,
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
        # 来源库目录名（= 因子报告数据集名）：前端「因子报告」跳转入口据此定位。
        # 用 discovery.source_of 现算 —— 与扫描侧同一条反查（唯一实现）；经典
        # demo 的 wind_source 是 Wind 字段名，反查得空串 → 前端不渲染入口。
        "source_library": discovery.source_of(meta.get("wind_source", "")),
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
        "stocks_date": stocks_date,
        "stocks_stale": stocks_stale,
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
    market: str = "CN",
) -> dict:
    mk = _market_key(market)
    ctx = _range_ctx(start, end, dataset, mk)
    p = ctx["panel"]
    fmeta = _meta_map(dataset, mk)
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
        kpi = dict(s["kpi"])
        if ctx["ic"] is not None:
            kpi.update(scorecard.ic_stats_from(ctx["ic_index"].get(code), ctx["rdates"]))
        out.append(
            {
                "code": code,
                "name_cn": meta["name_cn"],
                "l1": meta["l1"],
                "l2": meta["l2"],
                "n": n,
                # 对比页既有口径不动；只对新增的 Fitness 族做样本不足门控
                "kpi": scorecard.gate_brain(kpi),
                "excess": scorecard.excess_vs(
                    ctx["benches"], s["kpi"], ctx["bench_order"]
                ),
                "nav": _points(s["dates"], s["nav"]),
                "ic": ic_points,
            }
        )

    corr = (
        store.corr_table("classic", mk) if dataset != "private" else None
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
            "name": ctx["bench_names"].get(c, c),
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


def correlation(codes: list[str] | None = None, market: str = "CN") -> dict:
    """全量因子相关对（classic 快照产物）。非 CN 无 classic 相关系数表 → 422
    （``MarketUnsupported``），不拿 A 股相关矩阵冒充 —— 私人库的相关矩阵是
    ``cross_corr.npz``（去重专用，不走本端点）。"""
    mk = _market_key(market)
    corr = store.corr_table("classic", mk)
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


def screening(market: str = "CN") -> dict:
    """因子筛选清单（质量门槛 + 同源去重，含剔除原因）。

    非 CN 诚实缺席：store 返回同构空结构 + ``available:false`` + reason（该产品
    是 A 股口径的七库筛选），前端据 reason 渲染阻断面而不是空表。
    """
    return store.screening(_market_key(market))


# ---------------------------------------------------------------------------
# 快照管理：状态探测 + 一键计算（全部本地计算，不上传任何数据）
# ---------------------------------------------------------------------------
_BUILD_LOG = "build.log"
_BUILD_PID = "build.pid"


def scan_sources(dataset: str = "private", market: str = "CN") -> dict:
    """扫描本市场数据目录 → 与快照目录的差异（新增/消失）。**只读**，绝不触发重算。

    为什么需要：目录（``factors.json``）是**快照产物**，不是每次读盘现算的。
    新挖到的因子写进 ``6_ml_datasets`` 后，不重算就看不见；而重算一次私人库要
    5~15 分钟并重写 5.5GB 宽表。这个接口让人先看清「有什么新的」再决定付不付。

    **口径**：**必须**与重算走同一份扫描（``discovery``），否则这里说「没有新
    因子」而重算算出别的结果 —— 那就成了一句骗人的前置提示。重算按钮
    （``start_build``）不带 ``--source``，即脚本默认的 ``auto``；这里同样按
    ``auto`` 比。故快照若当初是用 ``--source l1l2/kept`` 建的，会老实地报出大量
    「新增」——这是对的：点一次重算它们确实会进来（``snapshot_source`` 会把这件事
    告诉前端）。
    """
    if dataset != "private":
        raise ValueError(
            f"扫描仅支持 private 数据集（classic 的目录来自静态 catalog.py，"
            f"没有「盘上有什么」可对），收到 {dataset!r}"
        )
    mk = _market_key(market)
    f = store.factors_meta("private", mk)
    if f is None:
        raise store.SnapshotMissing(
            "私人因子库快照缺失（请先一键计算，产物 factor_research_private/factors.json）"
        )
    factors = [e for e in f.get("factors", []) if e.get("code")]
    out = discovery.diff_catalog(
        discovery.scan_libraries(market=mk),
        {e["code"] for e in factors},
        catalog_library={
            e["code"]: discovery.source_of(e.get("wind_source", "")) for e in factors
        },
    )
    meta = f.get("meta", {})
    out["dataset"] = dataset
    out["market"] = mk
    out["snapshot_at"] = meta.get("built_at")
    out["snapshot_source"] = meta.get("source")
    # 毕业管道状态（CUSTOM → CN 镜像差）：新挖的因子若卡在桥前，扫描面板要指出来，
    # 否则用户只看到「快照差异」却不知道差异的源头在哪一站。
    # **CN 专属**（毕业桥的目标是 CN rd_mined 目录）：非 CN 面返回 None，
    # 不把 A 股管道的实况戴到港股页面上（前端据 None 隐藏该段）。
    out["pipeline"] = pipeline_state() if mk == "CN" else None
    return out


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


def snapshot_status(dataset: str = "classic", market: str = "CN") -> dict:
    """快照状态：是否已生成 / 构建中 / 日志进度。供前端「一键计算」入口。"""
    mk = _market_key(market)
    d = store.artifact_dir(dataset, mk)
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
    part_dir = d / frwindows.PANEL_DIR
    has_parts = part_dir.is_dir() and any(part_dir.glob("year=*.parquet"))
    return {
        "exists": has_parts or (d / "factor_panel.parquet").exists(),
        "running": pid is not None,
        "built_at": meta.get("built_at"),
        "window": meta.get("window"),
        "n_factors": meta.get("n_factors") or meta.get("n_factors_computed"),
        "n_dates": meta.get("n_dates"),
        "partitioned": bool(meta.get("partitioned", has_parts)),
        "survivorship_bias": meta.get("survivorship_bias"),
        "step": step,
        "log_tail": log_tail,
        "dataset": dataset,
        "market": mk,
    }


_BUILD_SCRIPTS = {
    "classic": "build_factor_research.py",
    "private": "build_factor_panel_private.py",
}


def start_build(
    dataset: str = "classic",
    market: str = "CN",
    start: str | None = None,
    end: str | None = None,
    append: bool = False,
) -> dict:
    """启动快照构建（后台子进程；已在构建则直接返回运行中）。全部本地计算。

    ``start``/``end`` 给了就只重建该区间（年分区按年替换）；``append=True`` 为
    增量续拉：保留旧年分区，只替换本次区间，旧 factors/ic/coverage 合并。
    私有库增量必须已有 ``direction.json``（方向冻结表），否则拒绝 —— 不能拿
    新窗口的 IC 重算 sign。
    """
    mk = _market_key(market)
    d = store.artifact_dir(dataset, mk)
    if (pid := _build_running(d)) is not None:
        return {"started": False, "running": True, "pid": pid}
    if append and dataset == "private" and not (d / frwindows.DIRECTION_FILE).exists():
        return {
            "error": f"增量续拉需要先有一次全量构建生成 {frwindows.DIRECTION_FILE}"
        }
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
    cmd = [sys.executable, str(script)]
    if start:
        cmd += ["--start", start]
    if end:
        cmd += ["--end", end]
    if dataset == "private":
        # 私有面板构建器带 --market（classic 构建脚本没有该参数、也不服务非 CN）。
        # 显式传规范键：脚本缺省是 CN，漏传的后果是「港股页点了计算，默默建了 A 股」。
        # direction：首次构建冻结校准窗方向；增量续拉只读冻结表，绝不重算符号。
        cmd += ["--market", mk]
        if append:
            # 先读冻结表，再把本块新增因子（校准窗无数据、按块内 IC 兜底）补写进去：
            # 不补写的话，这些因子的 fallback sign 每次续拉都可能变，不可复现。
            cmd += [
                "--append",
                "--direction-file",
                str(d / frwindows.DIRECTION_FILE),
                "--direction-out",
                str(d / frwindows.DIRECTION_FILE),
            ]
        else:
            # 已有冻结表时必须先读后写：全量重建也复用同一份 sign，避免换一次
            # 构建窗口就把方向重算一遍（那正是本轮要修的样本外泄漏）。
            if (d / frwindows.DIRECTION_FILE).exists():
                cmd += ["--direction-file", str(d / frwindows.DIRECTION_FILE)]
            cmd += ["--direction-out", str(d / frwindows.DIRECTION_FILE)]
    elif append:
        return {"error": "classic 不支持增量续拉（全量重建约 6 分钟），请去掉 append"}
    log = open(d / _BUILD_LOG, "a", encoding="utf-8")  # noqa: SIM115 - 交给子进程持有
    log.write(
        f"\n===== build started {pd.Timestamp.now().isoformat(timespec='seconds')}"
        f" market={mk} =====\n"
    )
    proc = subprocess.Popen(  # noqa: S603 - 固定脚本路径，无用户输入
        cmd,
        cwd=str(root),
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    (d / _BUILD_PID).write_text(str(proc.pid), encoding="utf-8")
    return {
        "started": True,
        "running": True,
        "pid": proc.pid,
        "dataset": dataset,
        "market": mk,
    }


# ---------------------------------------------------------------------------
# 挖掘→研究 毕业管道：CUSTOM rd_mined →（毕业桥）→ CN rd_mined
# ---------------------------------------------------------------------------
# 「新挖到的因子怎么进研究报告」的完整旅程（每一站都在这里可观测）：
#   RD-Agent 演化 → PG rd_agent_factors → 物化 quantcustom/rd_mined
#   →【毕业桥 promote_rd_mined.py --register】镜像到 quantdb/rd_mined + 刷新字段注册
#   →【重建私人库快照 / 报告快照】→ 因子研究 / 因子报告可见
# 这条链上任一站没走完，因子研究页的扫描面板就报哪一站（见 pipeline_state）。
_PROMOTE_LOG = "promote.log"
_PROMOTE_PID = "promote.pid"


def _numeric_cols_of_latest(root: Path, parts: dict) -> set[str] | None:
    """目录下最新分区的数值列集合（只读 footer）；读不了返回 None。"""
    if not parts:
        return None
    latest = root / max(parts) / "data.parquet"
    try:
        import pyarrow.parquet as pq

        sch = pq.ParquetFile(str(latest)).schema_arrow
    except Exception:  # noqa: BLE001 - 管道状态是提示性的，读不到就置 None
        return None
    numeric = ("float", "double", "int", "decimal")
    return {c for c in sch.names if str(sch.field(c).type).startswith(numeric)}


def pipeline_state() -> dict | None:
    """毕业管道状态：CUSTOM 盘上 vs CN 盘上（**只读**，绝不做任何写入）。

    判据与毕业桥共用实现（``promote_rd_mined.plan_sync`` / ``scan_partitions``），
    避免这里说「已同步」而毕业桥还在报「待拷贝」的两套口径。
    脚本不可用（部署形态变动）返回 None —— 前端隐藏该段，不报假状态。
    """
    try:
        from backend.scripts.promote_rd_mined import (
            plan_sync,
            resolve_roots,
            scan_partitions,
        )
    except Exception:  # noqa: BLE001
        return None
    src_root, dst_root = resolve_roots()
    src = scan_partitions(src_root)
    dst = scan_partitions(dst_root)
    plan = plan_sync(src, dst)
    src_cols = _numeric_cols_of_latest(src_root, src)
    dst_cols = _numeric_cols_of_latest(dst_root, dst)
    pending_factors = (
        sorted(src_cols - dst_cols)
        if src_cols is not None and dst_cols is not None
        else []
    )
    if not src:
        state = "empty"  # CUSTOM 还没有产出（未启用挖掘 / 挖掘线程空闲）
    elif pending_factors or plan.to_copy:
        state = "pending"  # 有新的没毕业
    else:
        state = "synced"  # 已毕业（快照收没收是另一件事，由扫描差异告诉用户）
    return {
        "state": state,
        "custom_n_factors": len(src_cols) if src_cols is not None else None,
        "cn_n_factors": len(dst_cols) if dst_cols is not None else None,
        "pending_factors": len(pending_factors),
        "pending_factor_names": pending_factors[:50],
        "pending_partitions": len(plan.to_copy),
        # scan_partitions 的键是分区目录名（"dt=20240102"），对外只给日期
        "custom_last_dt": max(src).split("=", 1)[1] if src else None,
        "cn_last_dt": max(dst).split("=", 1)[1] if dst else None,
    }


def _promote_running(d: Path) -> int | None:
    """毕业桥进程 PID（无则 None）。带 cmdline 校验防 PID 复用。"""
    pf = d / _PROMOTE_PID
    if not pf.exists():
        return None
    try:
        pid = int(pf.read_text().strip())
        os.kill(pid, 0)
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        return None
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="ignore")
        if "promote_rd_mined" not in cmdline:
            return None
    except OSError:
        pass
    return pid


def promote_status() -> dict:
    """毕业桥状态：是否运行中 / 日志最新一行（跑完看最后一行就知道结果）。

    **CN 专属**（毕业桥目标 = CN rd_mined 目录，与 market 参数无关；
    非 CN 前端隐藏该入口，见 scan_sources 里 pipeline=None 的同款理由）。
    """
    d = store.artifact_dir("private")
    pid = _promote_running(d)
    log_tail: list[str] = []
    step = ""
    lf = d / _PROMOTE_LOG
    if lf.exists():
        try:
            lines = [
                ln
                for ln in lf.read_text(encoding="utf-8", errors="ignore").splitlines()
                if ln.strip()
            ]
            log_tail = lines[-12:]
            step = lines[-1][:160] if lines else ""
        except OSError:
            pass
    return {"running": pid is not None, "pid": pid, "step": step, "log_tail": log_tail}


def start_promote() -> dict:
    """一键毕业：``promote_rd_mined.py --register``（镜像 + 刷新 CN 字段）。

    **刻意不带 --publish**：发布 CN 训练目录是决定「这批因子进不进训练口径」的
    显式闸门，留给「训练数据集」页或人手工执行 —— 研究页的按钮只把因子带到盘面。
    """
    d = store.artifact_dir("private")
    if (pid := _promote_running(d)) is not None:
        return {"started": False, "running": True, "pid": pid}
    d.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[4]
    script = root / "backend" / "scripts" / "promote_rd_mined.py"
    if not script.exists():
        return {"error": f"毕业桥脚本缺失: {script}"}
    log = open(d / _PROMOTE_LOG, "a", encoding="utf-8")  # noqa: SIM115 - 交给子进程持有
    log.write(
        f"\n===== promote started {pd.Timestamp.now().isoformat(timespec='seconds')} =====\n"
    )
    proc = subprocess.Popen(  # noqa: S603 - 固定脚本路径，无用户输入
        [sys.executable, str(script), "--register"],
        cwd=str(root),
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    (d / _PROMOTE_PID).write_text(str(proc.pid), encoding="utf-8")
    return {"started": True, "running": True, "pid": proc.pid}


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
    market: str = "CN",
) -> dict:
    """自定义权重合成 +（可选）阈值过滤 → 实时回测。

    weights: {factor_code: 权重}（内部按 Σ|w| 归一）
    threshold: 合成打分的过滤下限（全局，z 刻度；None=不过滤）
    thresholds: 每因子过滤下限 {code: z}（选股前先按各因子阈值筛股）
    """
    mk = _market_key(market)
    fmeta = _meta_map(dataset, mk)
    weights = {c: float(w) for c, w in (weights or {}).items() if c in fmeta and w != 0}
    if not weights:
        return {"error": "weights 为空"}
    codes = list(weights)
    scores = store.scores_for(codes, dataset, mk, start, end)
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

    fwd = store.fwd_returns(dataset, mk)
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
    # BRAIN 口径 Fitness：与单因子榜同源（含样本不足门控）。
    k.update(
        scorecard.brain_metrics(
            bt["ret"].to_numpy(dtype=float), bt["turnover"].to_numpy(dtype=float)
        )
    )
    k = scorecard.gate_brain(k)
    ctx = _range_ctx(start, end, dataset, mk)
    k_ex = scorecard.excess_vs(ctx["benches"], k, ctx["bench_order"])

    names = _instrument_names(mk)
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
            "name": ctx["bench_names"].get(c, c),
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
    market: str = "CN",
) -> dict:
    """在所选因子上网格搜索（非负、和为 1），夏普/年化/超额各给一组最优权重。

    候选池 = 各因子月末 top-150 的并集（面板限制），组合内缺失因子记 0 分（与 compose 同口径）；
    展示口径以 compose 全样本精确回测为准。「超额」目标 = 本市场主基准（FUTURES 无
    基准 → 该目标恒为 None，前端隐藏）。
    """
    t0 = time.time()
    mk = _market_key(market)
    ctx = _range_ctx(start, end, dataset, mk)
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
    primary = ctx["bench_order"][0] if ctx["bench_order"] else None
    prim = ctx["benches"].get(primary, {}).get("kpi", {}) if primary else {}
    bench_ann = prim.get("annual_return")
    excess = annual - bench_ann if bench_ann is not None else np.full(C, np.nan)

    def _winner(values: np.ndarray) -> dict | None:
        if not np.isfinite(values).any():
            return None
        i = int(np.nanargmax(values))
        ex = round(float(excess[i]), 4) if np.isfinite(excess[i]) else None
        out = {
            "weights": {
                c: round(float(w), 3) for c, w in zip(codes, W[:, i], strict=False)
            },
            "sharpe": round(float(sharpe[i]), 3) if np.isfinite(sharpe[i]) else None,
            "annual_return": round(float(annual[i]), 4),
            "max_drawdown": round(float(mdd[i]), 4),
            "win_rate": round(float(win[i]), 4),
            # 新键（按市场口径）：前端统一读它；CN 另带旧键 excess_300 一期兼容
            "excess_primary": ex,
        }
        if mk == "CN":
            out["excess_300"] = ex
        return out

    objectives = {
        "sharpe": _winner(sharpe),
        "annual_return": _winner(annual),
        "excess_primary": _winner(excess),
    }
    if mk == "CN":
        objectives["excess_300"] = objectives["excess_primary"]  # 旧键一期兼容
    out = {
        "objectives": objectives,
        "bench_primary": (
            {"code": primary, "name": ctx["bench_names"].get(primary, primary)}
            if primary
            else None
        ),
        "n_combos": int(C),
        "n_months": int(T),
        "codes": codes,
        "elapsed_ms": int((time.time() - t0) * 1000),
    }
    return store._sanitize(out)
