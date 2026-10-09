#!/usr/bin/env python3
"""《2800 个因子》清单里 **FULL 可行性**的因子 → 按日分区特征表。

清单来源 ``data/factor_defs/formulas.json``（2603 条，口径与缺口见
``data/factor_defs/README.md``）。本脚本只算 ``feasibility == "FULL"`` 的部分 ——
即**日频数据即可复现**的因子；分钟族（``MINUTE_2026``，1059 条）要等 ``min1_kline``
补齐八个月以上的历史再跑，财务族（``FINANCIAL_PIT``）要 ``m_anntime`` 点位。

产物与 ``alpha_library`` **同构**（``dt=YYYYMMDD/data.parquet``，``symbol``/``time`` +
因子列、float32），因此 ``backend/services/engine/factor_report/datasets.py`` 登记后
即可出 IC / 分层 / 换手报告。

公式求值走 :mod:`backend.shared.factor_dsl`（与 ``data/factor_defs/_lookahead_test.py``
的截断不变性体检**同一个引擎**：体检过了 2602/2603，这里就不再另写一遍解释器）。

运行::

    python backend/scripts/factor_defs_factors.py --limit 40      # 试点
    python backend/scripts/factor_defs_factors.py                 # 全量（约 4 小时）
"""
from __future__ import annotations

import argparse
import gc
import json
import logging
import shutil
import sys
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
for _p in (PROJECT_ROOT, PROJECT_ROOT / "backend" / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import alpha_library_factors as alf  # noqa: E402
from backend.shared.benchmark import resolve_benchmark  # noqa: E402
from backend.shared.factor_dsl import Evaluator, Node, parse  # noqa: E402

log = logging.getLogger("factor_defs")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

FORMULAS = PROJECT_ROOT / "data" / "factor_defs" / "formulas.json"
OUT_ROOT = alf.DATA_ROOT / "6_ml_datasets" / "factor_defs"
PARTIAL_ROOT = alf.DATA_ROOT / "6_ml_datasets" / "_partial_factor_defs"
START_DT = "20160101"
#: 每个计算批次的因子数。批次越大越快、内存越高：一批 N 条要同时驻留
#: N × (2604 日 × 5566 标的 × 8B) ≈ N × 116MB，故 60 条约 7GB 峰值。
BANK_SIZE = 60

#: 本脚本算不了、也不是「暂时缺数据」的因子所依赖的变量。
#:
#: 这几条要的是**指数成分股名单**（公式形如 ``IF(idx_w_X > 0, 1, 0)``），
#: 而 ``2_base_sector/index_weights/`` 只有**当前时点的一份快照**（无 dt 分区，
#: 见 README「数据可行性」），把它铺到 2016 年起的十年历史上＝拿今天的成分股
#: 去选中过去的成分股 —— 系统性前视 + 幸存者偏差，算出来的 IC 会假性偏高。
#: **宁可不算，也不出一份被污染的结论。** 等拿到 point-in-time 成分股再放开。
UNAVAILABLE_VARS = frozenset({
    "idx_w_000300", "idx_w_000852", "idx_w_000905",   # 有快照，但无历史
    "idx_w_399303", "idx_w_932000",                   # 连快照都没有
})

#: 常量占位因子（源清单自带的 ``feat_test_dummy = 1.0``）——不是因子，跳过。
SKIP_NAMES = frozenset({"feat_test_dummy"})


# ---------------------------------------------------------------------------
# 1. 因子清单
# ---------------------------------------------------------------------------

def load_factors(limit: int = 0, only: set[str] | None = None) -> list[dict]:
    """取 FULL 可行性因子，剔除依赖不可用变量的条目（剔除结果全部打日志）。"""
    recs = json.loads(FORMULAS.read_text(encoding="utf-8"))
    full = [r for r in recs if r["feasibility"] == "FULL"
            and r["name"] not in SKIP_NAMES]

    keep, blocked = [], []
    for r in full:
        hit = sorted(UNAVAILABLE_VARS.intersection(r.get("vars_used") or []))
        if hit:
            blocked.append((r["name"], hit))
        else:
            keep.append(r)

    if blocked:
        log.warning("剔除 %d 条（依赖不可用变量）：", len(blocked))
        for name, vs in blocked:
            log.warning("  - %s  依赖 %s", name, ",".join(vs))
    log.info("FULL 因子 %d 条 → 可算 %d 条（清单共 %d 条）",
             len(full), len(keep), len(recs))

    if only:
        keep = [r for r in keep if r["name"] in only]
        log.info("--only 过滤后剩 %d 条", len(keep))
    if limit:
        keep = keep[:limit]
        log.info("--limit 只取前 %d 条", len(keep))
    if not keep:
        raise SystemExit("没有可算的因子")
    return keep


# ---------------------------------------------------------------------------
# 2. 变量帧（DSL 的 38 个变量 → Node）
# ---------------------------------------------------------------------------

VALUATION_COLS = ("total_capital", "circulating_capital", "total_mv",
                  "float_mv", "revenue_ttm")


def load_valuation(dates: pd.Index, symbols: list[str]) -> dict[str, pd.DataFrame]:
    """``valuation`` 里 FULL 因子要用的列 → 每列一张宽表（对齐 kline 网格）。

    一次查询取全部列：分 5 次查会把 1100 万行扫 5 遍（``load_total_mv`` 就是这么干的）。
    """
    con = duckdb.connect()
    try:
        df = con.execute(
            f"SELECT symbol, time, {', '.join(VALUATION_COLS)} "
            f"FROM read_parquet('{alf.VALUATION}')"
        ).fetchdf()
    finally:
        con.close()
    df["time"] = pd.to_datetime(df["time"])
    df = df.drop_duplicates(subset=["symbol", "time"])
    out = {}
    for col in VALUATION_COLS:
        wide = df.pivot(index="time", columns="symbol", values=col)
        out[col] = wide.reindex(index=dates, columns=symbols)
    log.info("valuation loaded: %d 行 × %d 列", len(df), len(VALUATION_COLS))
    return out


def load_bench_frames(days: pd.Index, syms: list[str]) -> dict[str, pd.DataFrame]:
    """基准开/收/量 → 与个股同形的宽表（基准量全市场同值，语义即「指数」）。

    ``bench_v`` 不能像体检脚本那样拿常数 1 顶替 —— 有 4 条 FULL 因子
    （``feat_isnt_sb`` / ``feat_tnsm_tn`` / ``feat_stv_w_rtn``）真的在做
    「个股量 vs 基准量」的协动，填常数会让它们恒等于 0 或 NaN。
    指数日线有 volume，走同一份 ``resolve_benchmark`` 保证 o/c/v 同源同指数。
    """
    sym, bdf = resolve_benchmark(columns=("open", "close", "volume"))
    bdf = bdf.reindex(days)
    log.info("benchmark %s：%d/%d 个交易日有数", sym, int(bdf["close"].notna().sum()), len(days))
    n = len(syms)

    def bcast(s: pd.Series) -> pd.DataFrame:
        return pd.DataFrame(np.tile(s.to_numpy()[:, None], (1, n)),
                            index=days, columns=syms)

    return {"bench_o": bcast(bdf["open"]), "bench_c": bcast(bdf["close"]),
            "bench_v": bcast(bdf["volume"])}


def build_ind_onehot(syms: list[str], ind_map: pd.Series) -> np.ndarray:
    """``(n_sym, n_ind)`` **0/1 归属矩阵**，行序与 ``syms`` 严格一致。

    ⚠️ ``alf.IN`` 是按位置矩阵乘的：行序错位**不会报错**，只会把行业均值算到
    别的票头上。故本函数以 ``syms`` 为准 reindex，并断言行数。
    ⚠️ 行业未知的票**不塞进 UNKNOWN 桶**（``compute_a101`` 是塞的）：中性化是
    「减同行业均值」，把一堆互不相干的票凑成一桶再减均值没有经济含义；
    让它们的 ``IN(x) = x``（谁的桶都不属于 → 回填 0）更诚实。
    """
    code = ind_map.reindex(syms)
    known = code.notna()
    cats = sorted(set(code[known].tolist()))
    pos = {c: i for i, c in enumerate(cats)}
    oh = np.zeros((len(syms), len(cats)))
    for i, c in enumerate(code.to_numpy()):
        if not isinstance(c, float):  # reindex 缺的会是 NaN
            oh[i, pos[c]] = 1.0
    n_miss = int((~known).sum())
    log.info("行业映射：%d 个行业，%d/%d 只票有归属（%d 只未知，IN() 对其直通）",
             len(cats), len(syms) - n_miss, len(syms), n_miss)
    return oh


def build_frames(W: dict[str, pd.DataFrame], val: dict[str, pd.DataFrame],
                 bench: dict[str, pd.DataFrame]) -> dict[str, Node]:
    """DSL 的变量名 → Node。与 ``_lookahead_test.real_frames`` 的命名一一对应。"""
    ref = W["close"]
    F: dict[str, Node] = {}

    # ── daily_forward（口径/单位见 alpha_library_factors.load_kline）────
    #  ⚠️ `v` 是**未复权**成交量、`c` 是**前复权**价 —— 与 SPEC §2 一致；
    #     `turn = v / float_capital` 因此是真实换手率（股本也是原始股数）。
    for name, col in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"),
                      ("v", "volume"), ("A", "amount"), ("vwap", "vwap"),
                      ("dc", "prev_close"), ("ret", "ret"), ("log_vol", "log_vol")):
        F[name] = Node(W[col])
    for n in (5, 10, 15, 20, 30, 40, 50, 60, 81, 120, 150, 180):
        F[f"adv{n}"] = Node(W[f"adv{n}"])

    # ── valuation（SPEC §2：单位一律 SI 原始口径，不做万元/亿换算）────
    F["mv"] = Node(val["total_mv"])
    F["fmv"] = Node(val["float_mv"])
    F["share"] = Node(val["total_capital"])
    F["rev_ttm"] = Node(val["revenue_ttm"])
    F["turn"] = Node(W["volume"] / (val["circulating_capital"] + 1e-12))

    # ── 基准 ─────────────────────────────────────────────────────────
    F.update({k: Node(v) for k, v in bench.items()})
    F["bench_ret"] = Node(bench["bench_c"].pct_change())

    # ── 常量 ─────────────────────────────────────────────────────────
    F["f"] = Node(pd.DataFrame(np.ones(ref.shape), index=ref.index, columns=ref.columns))

    # 标量广播栅格（引擎在每次求值前把它绑到模块级 `_REF`）。名字带双下划线，
    # 不是 DSL 变量，不会出现在任何公式里 —— 但**必须**提供，否则 eval() 直接 KeyError。
    F["__ref__"] = F["c"]

    for k, v in F.items():
        if not v.df.index.equals(ref.index) or not v.df.columns.equals(ref.columns):
            raise ValueError(f"变量 {k} 的栅格与 close 不一致（行/列序必须完全一致）")
    log.info("变量帧就绪：%d 个", len(F))
    return F


# ---------------------------------------------------------------------------
# 3. 分批评值
# ---------------------------------------------------------------------------

def eval_bank(recs: list[dict], ev: Evaluator, ref: pd.DataFrame,
              failures: dict[str, str]) -> dict[str, pd.DataFrame]:
    """逐条求值，**单条失败只记账不中断**。

    全量跑两小时起步，一条公式踩到边界就整轮崩掉代价太大；但跳过必须是
    **响的**——每条失败都进 ``failures``，最终写进 MANIFEST 并汇总打印，
    决不允许「少算了几条」这件事只留在日志的中间几行里。
    """
    out: dict[str, pd.DataFrame] = {}
    for i, r in enumerate(recs, start=1):
        try:
            node = ev.eval(parse(r["formula"]))
            if not isinstance(node, Node):
                raise TypeError(f"顶层结果不是因子值（{type(node).__name__}）")
            df = node.df
            if df.shape != ref.shape:
                raise ValueError(f"形状 {df.shape} ≠ 网格 {ref.shape}")
        except Exception as e:  # noqa: BLE001 —— 逐条隔离
            failures[r["name"]] = f"{type(e).__name__}: {e}"
            log.error("求值失败 %s：%s\n    公式 %s", r["name"], e, r["formula"])
            continue
        out[r["name"]] = df
        if i % 10 == 0:
            log.info("  ... 本批 %d/%d", i, len(recs))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只算前 N 条（试点）")
    ap.add_argument("--only", default="", help="只算这些因子名（逗号分隔）")
    ap.add_argument("--bank-size", type=int, default=BANK_SIZE)
    ap.add_argument("--start-dt", default=START_DT)
    ap.add_argument("--max-symbols", type=int, default=None, help="试点用：只取前 N 只票")
    ap.add_argument("--out-root", default="", help="覆盖产出目录（试点用）")
    ap.add_argument("--dry-run", action="store_true", help="只求值不落盘（试点用）")
    args = ap.parse_args()

    t0 = time.time()
    recs = load_factors(args.limit, {s for s in args.only.split(",") if s} if args.only else None)
    out_root = Path(args.out_root) if args.out_root else OUT_ROOT
    partial_root = PARTIAL_ROOT if not args.out_root else out_root.parent / "_partial_pilot"

    W = alf.load_kline(max_symbols=args.max_symbols)
    days, syms = W["close"].index, list(W["close"].columns)
    log.info("网格 %d 交易日 × %d 标的（%s ~ %s）",
             len(days), len(syms), days[0].date(), days[-1].date())

    val = load_valuation(days, syms)
    bench = load_bench_frames(days, syms)
    F = build_frames(W, val, bench)
    del val
    gc.collect()
    ind_onehot = build_ind_onehot(syms, alf.load_industry_map())

    # 分钟帧不传：FULL 因子**零条**用日内算子（已核，见 README），
    # 传 None 兼可省掉 Evaluator 里那份逐日切分缓存。
    ev = Evaluator(F, ind_onehot, None)

    partials: list[Path] = []
    failures: dict[str, str] = {}
    n_bank = (len(recs) + args.bank_size - 1) // args.bank_size
    for b in range(n_bank):
        bank = recs[b * args.bank_size:(b + 1) * args.bank_size]
        t1 = time.time()
        vals = eval_bank(bank, ev, W["close"], failures)
        log.info("批次 %d/%d：%d 条求值完成（%.0fs，累计失败 %d）",
                 b + 1, n_bank, len(vals), time.time() - t1, len(failures))
        if not args.dry_run:
            root = partial_root / f"bank{b:03d}"
            alf.write_partitions(vals, start_dt=args.start_dt, out_root=root)
            partials.append(root)
        del vals
        gc.collect()

    if args.dry_run:
        log.info("dry-run：求值通过 %d 条 / 失败 %d 条，未落盘（%.0fs）",
                 len(recs) - len(failures), len(failures), time.time() - t0)
        return
    if failures:
        log.error("═" * 70)
        log.error("有 %d 条因子求值失败，**未计入本次产物**：", len(failures))
        for n, err in failures.items():
            log.error("  ✗ %s  %s", n, err[:160])
        log.error("═" * 70)

    total = alf.merge_partials(partials, out_root, start_dt=args.start_dt)
    # merge_partials 边合并边删各批的当日分区，剩下的空壳目录一并清掉
    shutil.rmtree(partial_root, ignore_errors=True)

    written = [r["name"] for r in recs if r["name"] not in failures]
    manifest = {
        "dataset": out_root.name,
        "source": "data/factor_defs/formulas.json",
        "feasibility": "FULL",
        "n_factors": len(written),
        "factors": written,
        "date_range": [str(days[0].date()), str(days[-1].date())],
        "n_symbols": len(syms),
        "start_dt": args.start_dt,
        "partitions": total,
        # 算不了的两类都**落在产物里**，免得后来人以为「FULL 就是这些」：
        # 1) 依赖不可用变量被前置剔除；2) 求值抛异常被隔离跳过。
        "excluded_vars": sorted(UNAVAILABLE_VARS),
        "eval_failures": failures,
        "engine": "backend/shared/factor_dsl.py",
    }
    (out_root / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("ALL DONE: %d 条 → %d 个分区，用时 %.0fs", len(recs), total, time.time() - t0)


if __name__ == "__main__":
    main()
