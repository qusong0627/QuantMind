#!/usr/bin/env python3
"""私人因子库快照构建（因子工作台「私人因子库」数据集）。

市场分层（2026-10-16，``--market`` 默认 ``CN``，零行为变化）：同一脚本为
CN/HK/US/FUTURES 各自构建快照，产物写 ``<市场根>/factor_research_private/``
（市场根经 ``feature_source.resolve_market_root`` 唯一解析）。非 CN 口径差异
（缺哪层口径的事实全部写入 meta，绝不冒充经典 CN 口径）：

- ``kept``/``l1l2`` 两种来源是 A 股口径产物（七库筛选 / L2 逐笔）→ 强制
  ``auto``，请求值与原因记入 meta（``source_requested``/``source_skip_reason``）；
- 股票池无 ST/退市过滤（instrument_detail 为 A 股专属）→ 仅「近 120 日 60%
  有效历史」，事实记入 meta（``universe``）；
- **不读经典目录**（classic 为 A 股专属）：持有期收益本地重算、基准按市场
  注册表写（HK=恒生系 / US=标普系 / FUTURES 无指数数据 → 空表，缺文件=没建过、
  空表=本市场无基准，两信号不混）；
- 本脚本自产 ``stock_snapshot.parquet``（名称←security_master、行业←sector、
  市值/PE/PB←valuation 最新分区〔仅 US，注册表裁决；HK 分区脏、FUT 无目录
  → 诚实置空〕）；CN 的仍由 build_factor_research.py 写经典目录，此处不重复。

来源（``--source``，默认 ``auto``）：
- ``auto``（默认）：**自动扫描 ``6_ml_datasets/`` 下的全部因子数据集**（alpha_library 429、
  alpha360 360、l1_factors 110、l2_factors 211、jq110 109、tdxgs 88、features_daily 47 等，
  合计约 1300 个因子）——只读取你已算好的因子数据，不重算因子本身；
- ``l1l2``：仅 L1+L2 两个数据集（约 321 个）；
- ``kept``：多库联合筛选的最终保留清单（``screening/factor_selection.json`` 的 kept）。

跳过规则：``alpha_library_labels``（标签非因子）、``l1_l2_factors``（L1∪L2 冗余并集）；
跨数据集重名列按优先级（l1 → l2 → 各因子库 → features_daily）保留首次出现。

产物（<市场根>/factor_research_private/）:
    factors.json          目录（code/display_name/来源库/方向/IC 指标）
    direction.json        方向冻结表（校准窗 2020-01~2024-12 的 sign，跨窗口复用）
    coverage.json         逐月股票池/因子覆盖 + 退市缺口审计（survivorship_bias）
    factor_panel.parquet  每因子每期前 150 名（rank/symbol/score/raw/fwd_ret，同经典口径）
    factor_panel/year=YYYY.parquet  同上，按年分区（运行时只读相交年份）
    monthly_scores/year=YYYY.parquet 宽表分片（合成按列裁剪读取；不再写 8–12G 单文件）
    ic.parquet            月频 RankIC（定向后：正值=有效方向）
    fwd_returns.parquet / benchmarks.parquet  持有期收益与本市场基准
    stock_snapshot.parquet 个股最新截面（非 CN 由本脚本写；CN 在经典目录，见上）
    metrics.json          构建元信息（状态接口用）

口径（与经典数据集一致）：
- 采样日（月末）、成本（0.2%×换手）同经典；股票池 CN 同经典（非 ST/退市 +
  近 120 日 60% 有效），非 CN 仅历史过滤（见上）；
- 打分 = 截面 pct rank → 正态分位（±4 截断）；方向按**校准窗 IC 冻结**
  （默认 2020-01-01~2024-12-31；已冻结条目优先，绝不按评估窗重算），
  原始值原样保留在 raw 列。

用法（仓库根）:
    python3 backend/scripts/build_factor_panel_private.py                   # CN 默认 2016-07 至今
    python3 backend/scripts/build_factor_panel_private.py --source l1l2     # 仅 L1+L2
    python3 backend/scripts/build_factor_panel_private.py --source kept     # 筛选保留清单
    python3 backend/scripts/build_factor_panel_private.py --market HK      # 港股（默认仍 2020 起）
    python3 backend/scripts/build_factor_panel_private.py --smoke 200       # 冒烟（200 只）
    python3 backend/scripts/build_factor_panel_private.py --start 2025-01-01 \\
        --direction-file <root>/direction.json --append                     # 增量续拉
    FACTOR_RESEARCH_OUT=<dir> / --out <dir> 可改输出目录（冒烟/分块隔离）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.services.engine.factor_research import analysis  # noqa: E402
from backend.services.engine.factor_research import data as frdata  # noqa: E402
from backend.services.engine.factor_research import engine  # noqa: E402
from backend.services.engine.factor_research import windows as frwindows  # noqa: E402
from backend.services.engine.factor_research.catalog import BY_CODE  # noqa: E402
from backend.services.engine.factor_research import discovery  # noqa: E402

# 兼容旧导入名：实际窗口由 main() 按市场解析（CN=2016-07 起，非 CN=2020 起）。
LOOKBACK_START = frwindows.LEGACY_LOOKBACK_START
DEFAULT_START = frwindows.LEGACY_START
PANEL_K = 150
# 扫描的唯一实现已提到 `factor_research.discovery`：构建与「扫描」接口共用同一份，
# 否则扫描会给出与重算不符的承诺（按钮说「没有新因子」而重算算出别的结果）。
# 下面两个名字保留为**转调别名**，供本脚本既有调用点（`main()`）与既有测试引用；
# 其余常量（L1L2_SOURCES/AUTO_PRIORITY/AUTO_SKIP/DROP_COLS/LIB_LABELS）已无本地
# 引用，需要时直接从 `discovery` 取，别再在这里复制一份。
KEPT_LIB_SOURCES = discovery.KEPT_LIB_SOURCES
_lib_label = discovery.lib_label


def _write_wide_scores(wide_parts: list[pd.DataFrame], path: Path) -> int:
    """宽表打分落盘（多因子合成按列取数用）。返回列数（含两个索引列）。

    不走 `pd.concat(axis=1) → reset_index() → to_parquet()`：那条路对整表复制三遍
    （concat 一份、reset_index 一份、arrow 表一份）。因子数上到 2700+ 时，float64
    单表就是 9.9 GB，峰值 ~29 GB，实测被 OOM 杀掉。这里按列直接拼 arrow 表 ——
    各分片的列本来就是现成数组，`pa.table` 直接引用，不再整表复制。

    因子列统一压成 float32：打分段是「截面 pct rank → 正态分位（±4 截断）」，
    7 位有效数字绰绰有余，体积和峰值内存都减半（旧快照存的是 float64，读侧
    `read_parquet(columns=[...])` 再 melt，对 dtype 无假设）。

    列名跨分片必须唯一：arrow 允许重名，一旦漏网，下游按列取数取到哪一份就说不清。
    扫描阶段已按优先级去重，这里兜底报错。
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    index = wide_parts[0].index
    cols: dict[str, object] = {}
    for lvl, name in enumerate(index.names):
        cols[name] = pa.array(pd.Series(index.get_level_values(lvl)))
    for part in wide_parts:
        for c in part.columns:
            if c in cols:
                raise ValueError(f"宽表列名重复：{c}（跨分片重名会让下游按列取数取错）")
            arr = part[c].to_numpy()
            cols[c] = arr if arr.dtype == np.float32 else arr.astype(np.float32)
    pq.write_table(pa.table(cols), path)
    return len(cols)


def _write_wide_scores_partitioned(wide_files: list[Path], out: Path) -> int:
    """宽表按年落盘 ``monthly_scores/year=YYYY.parquet``（不写整份 8–12G 单文件）。

    每个因子库的宽表先落临时文件（``out/_tmp_wide/*.parquet``），再逐年按列合并。
    这样峰值只持有**一个库**的宽表 + **一年**的合并表；若把 15 个库的宽表全留在
    内存里，10 年（131 期 × 4585 因子 × 5727 只 × 4B）约 13.7G，叠加 vals/scores
    会把 60G 主机顶到危险区。轴序与 ``_write_wide_scores`` 完全相同。
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    if not wide_files:
        return 2
    # 年份来自各临时文件的 trade_date（它是 MultiIndex 的一层，pandas 的
    # columns= 选不到索引列，必须用 pyarrow 只读这一列）。
    years: set[int] = set()
    for f in wide_files:
        arr = pq.read_table(f, columns=["trade_date"]).column("trade_date")
        s = pd.to_datetime(arr.to_pandas())
        years.update(int(y) for y in s.dt.year.unique())
    n_cols = 2
    out_dir = out / frwindows.SCORES_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    for year in sorted(years):
        lo = pd.Timestamp(year=year, month=1, day=1)
        hi = pd.Timestamp(year=year, month=12, day=31)
        cols: dict[str, object] = {}
        index = None
        for f in wide_files:
            df = pd.read_parquet(
                f, filters=[("trade_date", ">=", lo), ("trade_date", "<=", hi)]
            )
            if df.empty:
                continue
            if not isinstance(df.index, pd.MultiIndex):
                df = df.set_index(["trade_date", "symbol"])
            if index is None:
                index = df.index
            for c in df.columns:
                if c in cols:
                    raise ValueError(
                        f"宽表列名重复：{c}（跨分片重名会让下游按列取数取错）"
                    )
                arr = df[c].to_numpy()
                cols[c] = arr if arr.dtype == np.float32 else arr.astype(np.float32)
        if index is None:
            continue
        for lvl, name in enumerate(index.names):
            cols[name] = pa.array(pd.Series(index.get_level_values(lvl)))
        n_cols = len(cols)
        p = out_dir / f"year={year}.parquet"
        tmp = p.with_name(p.name + ".tmp")
        pq.write_table(pa.table(cols), tmp)
        os.replace(tmp, p)
    # 年份清单：运行时据此断言请求区间内不缺年份（与 factor_panel 同口径）
    manifest = out_dir / frwindows.PARTITION_MANIFEST
    old_years: set[int] = set()
    if manifest.exists():
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
            old_years = {int(y) for y in raw.get("years", [])}
        except (OSError, ValueError, TypeError, AttributeError):
            old_years = set()
    tmp = manifest.with_name(manifest.name + ".tmp")
    tmp.write_text(
        json.dumps(
            {"years": sorted(old_years | years), "updated_at": pd.Timestamp.now().isoformat(timespec="seconds")},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.replace(tmp, manifest)
    return n_cols


def _out_dir(market: str = "CN") -> Path:
    env = os.environ.get("FACTOR_RESEARCH_OUT")  # 冒烟测试用临时目录
    if env:
        d = Path(env)
        d.mkdir(parents=True, exist_ok=True)
        return d
    from backend.shared.feature_source import resolve_market_root

    d = resolve_market_root(market) / "factor_research_private"
    d.mkdir(parents=True, exist_ok=True)
    return d


# 以下 5 个名字是**转调别名**，供本脚本既有调用点（`main()` 用 `_load_sources`）
# 与既有测试（`test_factor_panel_private_scan.py` 用 `_load_auto`）引用。
# 不要在这里重新长出实现：扫描接口读的是 `discovery`，两边一旦分叉，
# 「有哪些因子」就会有两种答案，而没有任何一层会报错。
_load_kept = discovery.load_kept
_load_l1l2 = discovery.load_l1l2
_load_auto = discovery.load_auto
_load_sources = discovery.load_sources
_numeric_names = discovery.numeric_factor_names


def _day_matrix(f: Path, names: list[str], symbols: pd.Index) -> np.ndarray:
    """读取单日分区 → (F, S) float32，按 symbols 对齐；**分区缺该列时整行留 NaN**。

    列清单来自该库的**最新分区**（`discovery.load_auto` 只读 `parts[-1]`），而历史
    分区的 schema 会变：`features_daily` 的 `return_*d` 在 2026-09-21 改名成
    `future_return_*d`，拿新列名去读 2016 年的分区时 pyarrow 直接抛

        ArrowInvalid: No match for FieldRef.Name(future_return_1d)

    `columns=` 是整批读，一列对不上就整个重算死在该库上；而写盘全在库循环**之后**，
    所以用户白等 5 分钟后快照一个字节没变（2026-10-07 由扫描→重算这条路径实测）。
    故先按分区实际 schema 取交集，缺列留 NaN——新列只在它真正存在的那段有值，这与
    `_score_row_block` 对 NaN 的处理一致。

    行数必须**恒等于** `len(names)`：调用方 `vals[di] = ...` 是按 names 的位置写入
    (F, S) 缓冲区的，少一行就是整体错位。
    """
    import pyarrow.parquet as pq

    have = set(pq.ParquetFile(f).schema_arrow.names)
    pos = [i for i, c in enumerate(names) if c in have]
    out = np.full((len(names), len(symbols)), np.nan, dtype=np.float32)
    if pos:
        use = [names[i] for i in pos]
        df = pd.read_parquet(f, columns=["symbol", *use])
        df = df.set_index("symbol").reindex(symbols)
        out[pos] = df[use].to_numpy(dtype=np.float32).T
    return out


def _score_row_block(v: np.ndarray) -> np.ndarray:
    """(F, S) 原始值 → 截面打分（pct rank → 正态分位 ±4；NaN 保持）。"""
    n = np.isfinite(v).sum(axis=1)
    df = pd.DataFrame(np.where(np.isfinite(v), v, np.nan))
    rk = df.rank(axis=1, method="average", na_option="keep")
    pct = rk.sub(0.5).div(pd.Series(n).replace(0, np.nan), axis=0)
    sc = np.clip(engine._norm_ppf_np(pct.to_numpy(dtype=float)), -4, 4)
    return sc.astype(np.float32), rk


def _wide_frame(
    scores_o: np.ndarray, idx: pd.MultiIndex, names: list[str]
) -> pd.DataFrame:
    """(D=日, F=因子, S=标的) 打分 → 宽表（行=idx 的 (日, 标的) 展开，列=因子）。

    必须先 ``transpose(0, 2, 1)`` 再 reshape：宽表行序是「日主序 × 标的」，而
    scores_o 的内存序是「日 × 因子 × 标的」。直接 ``reshape(D*S, F)`` 是按内存序
    错位切分 —— 行 (d, s) 列 j 得到 ``orig(d, (s*F+j)//S, (s*F+j)%S)``，数值仍落
    [-4, 4]、不抛任何异常（2026-10-09 与面板逐值比对 150/150 不一致实锤，
    「多因子合成」经 store.scores_for() 吃了这份错位宽表）。
    """
    return pd.DataFrame(
        # float32：分位数打分精度足够，宽表体积与峰值内存均减半
        scores_o.transpose(0, 2, 1).reshape(len(idx), len(names)).astype(np.float32),
        index=idx,
        columns=names,
    )


def _stock_snapshot_non_cn(market: str, daily: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """非 CN 自产个股快照（列与经典 ``_build_stocks_snapshot`` 同构，前端零改动）。

    - name ← security_master（cn_name → en_name → symbol；FUTURES 无主表 → symbol）；
    - industry ← sector/（industry 列优先、空串回退 sector 列；HK 实测全空 → 整列
      缺省，界面按缺省渲染，绝不填假行业）；
    - total_mv_yi/pe_ttm/pb ← 最新估值分区（供给由 ``frdata.VALUATION_SNAPSHOT_MARKETS``
      裁决：仅 US；HK 分区脏、FUT 无目录 → NaN）；
    - avg_amount_yi ← 面板最近 252 行日均成交额（单位换算走 ``frdata.amount_yi_factor``，
      CN 万元 / 非 CN 元的差异只在那一处收口）。
    """
    close = daily["close"]
    snap = pd.DataFrame({"symbol": close.columns})
    master = frdata.load_security_master(market)
    if master is not None:
        m = master.drop_duplicates("symbol").set_index("symbol")
        name = m["cn_name"].replace("", pd.NA).fillna(m["en_name"].replace("", pd.NA))
    else:
        name = pd.Series(dtype=object)
    nm = name.reindex(snap["symbol"])
    # 名称缺省回退代码本身（个股表按 name 列展示，空串会渲染成空白而非缺省样式）
    snap["name"] = nm.fillna(
        pd.Series(snap["symbol"].to_numpy(), index=nm.index)
    ).to_numpy()
    sect = frdata.load_sector_map(market)
    if sect is not None:
        s = sect.drop_duplicates("symbol").set_index("symbol")
        ind = s["industry"].replace("", pd.NA) if "industry" in s.columns else pd.Series(
            dtype=object, index=s.index
        )
        if "sector" in s.columns:
            ind = ind.fillna(s["sector"].replace("", pd.NA))
    else:
        ind = pd.Series(dtype=object)
    snap["industry"] = ind.reindex(snap["symbol"]).to_numpy()
    val = frdata.load_valuation_snapshot(market)
    for col, src in (("total_mv_yi", "total_mv"), ("pe_ttm", "pe_ttm"), ("pb", "pb")):
        if val is not None and src in val.columns:
            v = pd.to_numeric(
                val.drop_duplicates("symbol").set_index("symbol")[src], errors="coerce"
            )
            if src == "total_mv":
                v = v / 1e8  # 元 → 亿元（与经典 stock_snapshot 同口径）
            snap[col] = v.reindex(snap["symbol"]).to_numpy(dtype=float)
        else:
            snap[col] = np.nan
    amt = daily["amount"].tail(252).mean() * frdata.amount_yi_factor(market)
    snap["avg_amount_yi"] = amt.reindex(snap["symbol"]).to_numpy(dtype=float)
    return snap


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", type=int, default=0, help="冒烟：只取前 N 只股票")
    ap.add_argument(
        "--market",
        default="CN",
        help="市场：CN/HK/US/FUTURES（默认 CN；非 CN 仅支持 --source auto）",
    )
    ap.add_argument(
        "--source",
        choices=["auto", "l1l2", "kept"],
        default="auto",
        help="因子来源：auto=自动扫描 6_ml_datasets（默认）；l1l2=仅 L1+L2；kept=筛选保留清单",
    )
    ap.add_argument(
        "--start",
        default=None,
        help=f"评估窗首月（默认 CN={frwindows.CN_START}，非 CN={frwindows.LEGACY_START}）",
    )
    ap.add_argument("--end", default=None, help="评估窗截止日（默认最新）")
    ap.add_argument(
        "--lookback-start",
        default=None,
        help=f"预热起点（默认 CN={frwindows.CN_LOOKBACK_START}，非 CN={frwindows.LEGACY_LOOKBACK_START}）",
    )
    ap.add_argument(
        "--direction-start",
        default=frwindows.DIRECTION_START,
        help="方向校准窗起点（sign 只在这个窗口算一次）",
    )
    ap.add_argument(
        "--direction-end",
        default=frwindows.DIRECTION_END,
        help="方向校准窗终点",
    )
    ap.add_argument(
        "--direction-file",
        default=None,
        help="读取已冻结的 direction.json（跨块/增量续拉时必须传，禁止重算符号）",
    )
    ap.add_argument(
        "--direction-out",
        default=None,
        help="把本次算出的方向写入 direction.json（已有条目优先，不覆盖）",
    )
    ap.add_argument("--out", default=None, help="输出目录（默认按市场解析）")
    ap.add_argument(
        "--append",
        action="store_true",
        help="增量续拉：保留旧年分区，只替换本次区间；旧 factors/ic/coverage 合并",
    )
    ap.add_argument(
        "--no-partition",
        action="store_true",
        help="只写整份 factor_panel/monthly_scores（冒烟/非 CN 小数据用；生产勿开）",
    )
    args = ap.parse_args()
    from backend.shared.feature_source import resolve_market_root, validate_market

    mk = validate_market(args.market)
    t0 = time.time()
    out = Path(args.out) if args.out else _out_dir(mk)
    out.mkdir(parents=True, exist_ok=True)
    qroot = resolve_market_root(mk)
    default_start, default_lookback = frwindows.defaults_for_market(mk)
    start_s = frwindows.parse_start(args.start, default_start)
    end_s = frwindows.parse_end(args.end)
    lookback_s = frwindows.parse_start(args.lookback_start, default_lookback)
    start_ts = pd.Timestamp(start_s)
    end_ts = pd.Timestamp(end_s) if end_s else None
    # 非 CN：kept/l1l2 两种来源为 A 股口径产物（七库筛选 / L1·L2 逐笔）→ 强制 auto；
    # 请求值与原因写进 meta（不静默假装按请求跑，也不让构建白等一次报错）。
    source = args.source
    source_skip_reason = ""
    if mk != "CN" and source != "auto":
        source_skip_reason = (
            f"{source} 为 A 股口径产物（七库筛选 / L1·L2 逐笔），{mk} 市场不供给，已改用 auto"
        )
        source = "auto"
        print(f"[0/6] ⚠ {source_skip_reason}")
    external, fr_kept = _load_sources(source, qroot, mk)
    src_desc = " + ".join(f"{_lib_label(k)} {len(v)}" for k, v in external.items())
    print(f"[1/6] 市场 {mk} · 来源({source})：{src_desc} + factor_research {len(fr_kept)}")

    # 多读 FWD_BUFFER_DAYS：块末月的持有期收益要用下一采样日的价格，不能因为
    # 本次只建到 2019-12 就让 2019-12 的 fwd 变成 NaN（边界断链）。
    load_end = (
        (end_ts + pd.Timedelta(days=frwindows.FWD_BUFFER_DAYS)).strftime("%Y%m%d")
        if end_ts is not None
        else "20991231"
    )
    daily = frdata.load_daily_panel(lookback_s, load_end, market=mk)
    # 股票池：CN 走 instrument（ST/退市口径）；非 CN 无对应物 → instr=None，
    # 只做历史过滤（缺哪层过滤的事实由 meta.universe 说明，绝不冒充经典口径）。
    instr = frdata.load_instrument() if mk == "CN" else None
    close = daily["close"]
    if args.smoke:
        syms = list(close.columns)[: args.smoke]
        for k in daily:
            daily[k] = daily[k][syms]
        close = daily["close"]
    if close.empty:
        print(f"[!] {mk} 行情面板为空——请先同步本市场行情（见数据管理页）")
        return 1
    all_dates = frdata.month_end_dates(close.index)
    dates = [
        d
        for d in all_dates
        if d >= start_ts and (end_ts is None or d <= end_ts)
    ]
    if not dates:
        print(
            f"[!] {mk} 无可用采样日（面板 {close.index.min().date()}~"
            f"{close.index.max().date()}，需覆盖 {start_s} 之后）"
        )
        return 1
    # fwd 采样日 = 评估日 + 下一个采样日（仅用于算持有期收益，不进面板）
    fwd_dates = list(dates)
    if end_ts is not None:
        nxt = [d for d in all_dates if d > dates[-1]]
        if nxt:
            fwd_dates.append(nxt[0])
    universe_all = frdata.build_universe(instr, close)
    universe = universe_all.reindex(dates).fillna(False)
    print(
        f"      采样日 {len(dates)}（{dates[0].date()} ~ {dates[-1].date()}）× {close.shape[1]} 只"
        f" · 预热 {lookback_s} · fwd 至 {fwd_dates[-1].date()}"
    )

    # 持有期收益：CN 优先复用经典快照（同采样日），缺失时自算；
    # 非 CN **不读经典目录**（classic 为 A 股专属），一律本地重算。
    classic = qroot / "factor_research"
    fwd = None
    fp = classic / "fwd_returns.parquet"
    if mk == "CN" and fp.exists():
        fl = pd.read_parquet(fp)
        fwd = (
            fl.pivot(index="trade_date", columns="symbol", values="fwd_ret")
            .reindex(index=dates, columns=close.columns)
            .astype(np.float64)
        )
        if bool(np.isfinite(fwd.to_numpy()).any()):
            print("      持有期收益：复用经典快照")
        else:
            fwd = None
    if fwd is None:
        fwd = analysis.forward_returns(
            close, fwd_dates, universe_all.reindex(fwd_dates).fillna(False)
        ).reindex(index=dates)
        print("      持有期收益：本地重算")

    # 逐库计算
    idx = pd.MultiIndex.from_product(
        [dates, close.columns], names=["trade_date", "symbol"]
    )
    uni = universe.to_numpy(dtype=bool)
    fwd_np = fwd.to_numpy(dtype=np.float64)
    wide_parts: list[pd.DataFrame] = []
    wide_files: list[Path] = []
    tmp_wide_dir = out / "_tmp_wide"
    if not args.no_partition:
        tmp_wide_dir.mkdir(parents=True, exist_ok=True)
    ic_parts: list[pd.DataFrame] = []
    panel_rows: list[pd.DataFrame] = []
    meta_entries: list[dict] = []
    # 覆盖率审计：因子×月、每月因子数、以及「行情有票但所有因子库都没值」的缺口
    # （退市股覆盖缺口是历史窗口的幸存者偏差来源，必须量化写进 coverage.json）。
    factor_months: dict[str, list[int]] = {}
    month_factor_count = np.zeros(len(dates), dtype=np.int32)
    has_factor_any = np.zeros((len(dates), close.shape[1]), dtype=bool)
    direction_frozen = frwindows.load_direction(args.direction_file)
    direction_fresh: list[dict] = []
    direction_names: list[str] = []

    if source == "kept":
        lib_order = KEPT_LIB_SOURCES
    else:
        lib_order = tuple(external.keys())  # 按 _load_* 构建顺序（auto/l1l2）
    for lib in lib_order:
        ks = external.get(lib, [])
        if not ks:
            continue
        tk = time.time()
        names = [k["name"] for k in ks]
        f_count = len(names)
        vals = np.full((len(dates), f_count, close.shape[1]), np.nan, dtype=np.float32)
        missing = 0
        for di, d in enumerate(dates):
            f = (
                qroot
                / "6_ml_datasets"
                / lib
                / f"dt={d.strftime('%Y%m%d')}"
                / "data.parquet"
            )
            if not f.exists():
                missing += 1
                continue
            vals[di] = _day_matrix(f, names, close.columns)
        if missing:
            print(f"      {lib}: {missing} 个采样日分区缺失（保持 NaN）")
        scores = np.full_like(vals, np.nan)
        ic = np.full((f_count, len(dates)), np.nan)
        factor_month_flags = np.zeros((f_count, len(dates)), dtype=bool)
        for di in range(len(dates)):
            v = np.where(uni[di][None, :], vals[di], np.nan)
            sc, rk = _score_row_block(v)
            scores[di] = sc
            valid_scores = np.isfinite(sc).any(axis=1)
            month_factor_count[di] += int(valid_scores.sum())
            factor_month_flags[valid_scores, di] = True
            fr = np.where(uni[di], fwd_np[di], np.nan)
            rf = pd.Series(fr).rank()
            sub = rk.notna().to_numpy() & rf.notna().to_numpy()[None, :]
            cnt = sub.sum(axis=1)
            # pandas 3 CoW：corrwith().to_numpy() 是**只读**视图，必须 copy=True 才能
            # 对样本不足的位置写 NaN（否则 numpy 直接抛 assignment destination is read-only）
            corr = rk.corrwith(rf, axis=1).to_numpy(dtype=float, copy=True)
            corr[~(cnt >= 30)] = np.nan
            ic[:, di] = corr
        # 退市覆盖：该日行情有票、但本因子库连原始值都没有的缺口（幸存者偏差来源）
        # 逐日累加，避免 np.isfinite(vals) 在 1125 因子 × 5727 只上再开一份 800MB 临时。
        for di in range(len(dates)):
            has_factor_any[di] |= np.isfinite(vals[di]).any(axis=0)
        # 方向：只认校准窗（默认 2020-01~2024-12），已冻结的条目优先；绝不按
        # 评估窗重算 —— 否则 2016–2019 的 sign 已经看过 2020+ 的 IC。
        sign, fresh = frwindows.calibrated_signs(
            ic,
            dates,
            names,
            cal_start=args.direction_start,
            cal_end=args.direction_end,
            frozen=direction_frozen,
        )
        for fi, name in enumerate(names):
            idxs = np.where(factor_month_flags[fi])[0]
            if idxs.size:
                factor_months[name] = [str(dates[i].date()) for i in idxs]
        scores_o = scores * sign[None, :, None]  # (D,F,S) × (1,F,1)
        ic_o = ic * sign[:, None]  # (F,D) × (F,1)
        print(
            f"[2/6] {lib}: {f_count} 因子 × {len(dates)} 期（{time.time() - tk:.0f}s，"
            f"反向 {int((sign < 0).sum())}，方向冻结 {int(sum(1 for e in fresh if e['source'] == 'frozen'))}）"
        )
        if not factor_month_flags.any():
            # 该库在本次窗口内没有任何有效截面（如只重建 2016–2019 时的 l2/gap/rd/
            # minute，它们分别从 2018/2020/2024 才开始）。跳过本库，不写空面板；
            # 全窗口构建时这些库在各自起始年后照常进入主库。
            print(f"      {lib}: 窗口内无数据，跳过（{f_count} 因子不计入本块）")
            del vals, scores, scores_o, ic
            continue
        # 无数据库不冻结方向：否则一个 2016 增量构建会把 l2/gap 全按 +1 写死，
        # 等 2018/2020 的数据到来时方向已错。
        direction_fresh.extend(fresh)
        direction_names.extend(names)
        del scores  # 只保留定向后的 scores_o；10 年窗口省 ~3.4G
        # 面板行
        fwd32 = fwd_np.astype(np.float32)
        fc, tds, rks, sy, sc_out, rw_out, fr_out = [], [], [], [], [], [], []
        fwd32 = fwd_np.astype(np.float32)
        for fi in range(f_count):
            for di in range(len(dates)):
                row = scores_o[di, fi]
                valid = np.isfinite(row)
                kk = min(PANEL_K, int(valid.sum()))
                if kk == 0:
                    continue
                filled = np.where(valid, row, -np.inf)
                top = np.argpartition(-filled, kk - 1)[:kk]
                top = top[np.argsort(-filled[top])]
                fc.append(np.full(kk, names[fi], dtype=object))
                tds.append(
                    np.full(kk, np.datetime64(dates[di]), dtype="datetime64[ns]")
                )
                rks.append(np.arange(1, kk + 1, dtype=np.int16))
                sy.append(close.columns.to_numpy()[top])
                sc_out.append(row[top])
                rw_out.append(vals[di, fi][top])
                fr_out.append(fwd32[di][top])
        panel_rows.append(
            pd.DataFrame(
                {
                    "factor_code": pd.Categorical(np.concatenate(fc)),
                    "trade_date": np.concatenate(tds),
                    "rank": np.concatenate(rks),
                    "symbol": pd.Categorical(np.concatenate(sy)),
                    "score": np.concatenate(sc_out).astype(np.float32),
                    "raw": np.concatenate(rw_out).astype(np.float32),
                    "fwd_ret": np.concatenate(fr_out).astype(np.float32),
                }
            )
        )
        # 面板行已取过 raw 值；宽表只用 scores_o，先释放 (D,F,S) 原始值（10 年 ~3.4G）
        del vals
        # 宽表打分（合成用）：分区模式立即落临时文件，避免 15 个库的宽表同时驻留
        _wide = _wide_frame(scores_o, idx, names)
        if args.no_partition:
            wide_parts.append(_wide)
        else:
            wp = tmp_wide_dir / f"{len(wide_files):03d}.parquet"
            _wide.to_parquet(wp)
            wide_files.append(wp)
            del _wide
        ic_parts.append(
            pd.DataFrame(ic_o, index=names, columns=pd.DatetimeIndex(dates))
        )
        # 目录条目
        sub_map = {k["name"]: (k.get("sublibrary") or lib) for k in ks}
        org_map = {k["name"]: k for k in ks}
        for fi, name in enumerate(names):
            k = org_map[name]
            icm = float(np.nanmean(ic_o[fi]))
            ics = float(np.nanstd(ic_o[fi]))
            sel_note = (
                f"筛选收录：RankIC {k.get('ic_mean')} · ICIR {k.get('icir')}。"
                if k.get("ic_mean") is not None
                else ""
            )
            meta_entries.append(
                {
                    "code": name,
                    "name_cn": name,
                    "display_name": k.get("display_name") or name,
                    "l1": _lib_label(lib),
                    "l2": sub_map[name],
                    "direction": int(sign[fi]),
                    "description": (
                        f"来源：{_lib_label(lib)} / {sub_map[name]}；方向按校准窗"
                        f"（{args.direction_start}~{args.direction_end}）IC 冻结（越大越好）。"
                        + sel_note
                    ),
                    "formula": "",
                    # 前缀取自 discovery：扫描侧要从这个字段反查来源库（source_of），
                    # 各写各的字符串就会对不上，差异里的库标签集体变空且不报错。
                    "wind_source": f"{discovery.SOURCE_PREFIX}{lib}",
                    "ic_mean": round(icm, 4) if np.isfinite(icm) else None,
                    "ic_std": round(ics, 4) if np.isfinite(ics) else None,
                    "ic_ir": round(icm / ics, 3)
                    if np.isfinite(ics) and ics > 0
                    else None,
                    "available": True,
                    "unavailable_reason": "",
                }
            )
        del scores_o, ic  # vals/scores 已在面板行后释放

    # factor_research 31：从经典快照拷贝（同口径，值已定向）
    if fr_kept:
        tk = time.time()
        names31 = [k["name"] for k in fr_kept]
        cp = pd.read_parquet(
            classic / "factor_panel.parquet", filters=[("factor_code", "in", names31)]
        )
        # 分块/增量构建时经典面板是全窗口；必须裁到本次 dates，否则私下面板会
        # 混入块外月份（原实现两边都是 2020–2026 全窗，没暴露）。
        cp = cp[pd.to_datetime(cp["trade_date"]).isin(dates)].reset_index(drop=True)
        panel_rows.append(cp)
        ms = pd.read_parquet(
            classic / "monthly_scores.parquet", filters=[("factor_code", "in", names31)]
        )
        piv = (
            ms.pivot_table(
                index=["trade_date", "symbol"],
                columns="factor_code",
                values="score",
                aggfunc="last",
            )
            .reindex(idx)
            .reindex(columns=names31)
        )
        if args.no_partition:
            wide_parts.append(piv)
        else:
            wp = tmp_wide_dir / f"{len(wide_files):03d}.parquet"
            piv.to_parquet(wp)
            wide_files.append(wp)
        ic31 = pd.read_parquet(
            classic / "ic.parquet", filters=[("factor_code", "in", names31)]
        )
        ic_parts.append(
            ic31.pivot(index="factor_code", columns="trade_date", values="ic").reindex(
                columns=pd.DatetimeIndex(dates)
            )
        )
        # 覆盖率：经典因子也计入「该日有因子值」的并集（与外部库同口径）
        date_pos = {pd.Timestamp(d): i for i, d in enumerate(dates)}
        sym_pos = {str(s): i for i, s in enumerate(close.columns)}
        for d, s in cp[["trade_date", "symbol"]].drop_duplicates().itertuples(
            index=False
        ):
            di = date_pos.get(pd.Timestamp(d))
            si = sym_pos.get(str(s))
            if di is not None and si is not None:
                has_factor_any[di, si] = True
        for name, grp in cp.groupby("factor_code", observed=True):
            ds = sorted({pd.Timestamp(x) for x in grp["trade_date"].unique()})
            factor_months[str(name)] = [str(x.date()) for x in ds if x in date_pos]
        for d, grp in cp.groupby("trade_date"):
            di = date_pos.get(pd.Timestamp(d))
            if di is not None:
                month_factor_count[di] += int(grp["factor_code"].nunique())
        for k in fr_kept:
            f = BY_CODE.get(k["name"], {})
            direction_names.append(k["name"])
            direction_fresh.append(
                {
                    "direction": int(f.get("direction", 1)),
                    "source": "catalog",
                    "calibration": None,
                    "ic_mean": k.get("ic_mean"),
                    "fallback": False,
                }
            )
            meta_entries.append(
                {
                    "code": k["name"],
                    "name_cn": f.get("name_cn", k["name"]),
                    "display_name": k.get("display_name")
                    or f.get("name_cn", k["name"]),
                    "l1": f.get("l1", "经典因子（demo 复刻）"),
                    "l2": f.get("l2", ""),
                    "direction": int(f.get("direction", 1)),
                    "description": f.get("description", ""),
                    "formula": f.get("formula", ""),
                    "wind_source": f.get("wind_source", ""),
                    "ic_mean": k.get("ic_mean"),
                    "ic_std": None,
                    "ic_ir": k.get("icir"),
                    "available": True,
                    "unavailable_reason": "",
                }
            )
        print(
            f"[3/6] factor_research 拷贝 {len(fr_kept)} 个（{time.time() - tk:.0f}s）"
        )

    # ── 覆盖率 / 方向冻结（先落审计，再落数据）─────────────────────
    kline_valid = close.notna().reindex(dates).fillna(False).to_numpy(dtype=bool)
    month_rows = []
    for di, d in enumerate(dates):
        kline_n = int(kline_valid[di].sum())
        covered = int((kline_valid[di] & has_factor_any[di]).sum())
        missing = kline_n - covered
        month_rows.append(
            {
                "date": str(d.date()),
                "universe": int(uni[di].sum()),
                "factor_count": int(month_factor_count[di]),
                "kline_symbols": kline_n,
                "factor_symbols": covered,
                "missing_symbols": missing,
                "missing_ratio": round(missing / kline_n, 4) if kline_n else 0.0,
            }
        )
    n_months = len(dates)
    factor_rows = {}
    for e in meta_entries:
        ds = factor_months.get(e["code"], [])
        factor_rows[e["code"]] = {
            "first_valid": ds[0] if ds else None,
            "last_valid": ds[-1] if ds else None,
            "months_with_data": len(ds),
            "months_in_window": n_months,
            "coverage": round(len(ds) / n_months, 4) if n_months else 0.0,
        }
    coverage = {
        "window": [str(dates[0].date()), str(dates[-1].date())],
        "lookback_start": lookback_s,
        "direction_calibration": {
            "start": args.direction_start,
            "end": args.direction_end,
        },
        "months": month_rows,
        "factors": factor_rows,
        "survivorship_bias": any(
            m["missing_ratio"] > 0.05 for m in month_rows if m["kline_symbols"]
        ),
        "survivorship_threshold": 0.05,
        "notes": [
            "missing_ratio = 当日行情有票、但所有因子库/经典拷贝都没有值的比例；"
            ">5% 记 survivorship_bias。",
            "退市股因子值未补齐时，历史窗口回测存在幸存者偏差，不得作为验收依据。",
        ],
    }
    if args.append and (out / frwindows.COVERAGE_FILE).exists():
        try:
            old = json.loads(
                (out / frwindows.COVERAGE_FILE).read_text(encoding="utf-8")
            )
            old_months = {m["date"]: m for m in old.get("months", [])}
            for m in month_rows:
                old_months[m["date"]] = m
            merged_months = sorted(old_months.values(), key=lambda x: x["date"])
            old_factors = dict(old.get("factors", {}))
            old_win = old.get("window") or []
            new_win = coverage["window"]
            overlap = bool(
                len(old_win) == 2
                and not (new_win[1] < old_win[0] or new_win[0] > old_win[1])
            )
            for code, row in factor_rows.items():
                prev = old_factors.get(code)
                if prev and not overlap:
                    # 非重叠增量：月数累加、首末有效日取全集的 min/max；
                    # 重叠重建：以本次为准（本次区间重新覆盖了旧数据）。
                    row = dict(row)
                    row["months_with_data"] = int(
                        prev.get("months_with_data", 0)
                    ) + int(row.get("months_with_data", 0))
                    fv = [x for x in (prev.get("first_valid"), row.get("first_valid")) if x]
                    lv = [x for x in (prev.get("last_valid"), row.get("last_valid")) if x]
                    row["first_valid"] = min(fv) if fv else None
                    row["last_valid"] = max(lv) if lv else None
                old_factors[code] = row
            n_all = len(merged_months)
            for row in old_factors.values():
                row["months_in_window"] = n_all
                row["coverage"] = (
                    round(row.get("months_with_data", 0) / n_all, 4) if n_all else 0.0
                )
            coverage["window"] = [merged_months[0]["date"], merged_months[-1]["date"]]
            coverage["months"] = merged_months
            coverage["factors"] = old_factors
            coverage["survivorship_bias"] = any(
                m["missing_ratio"] > 0.05 for m in merged_months if m["kline_symbols"]
            )
        except (OSError, ValueError, KeyError):
            pass  # 旧审计损坏不阻塞数据落盘；新审计照常写
    cov_tmp = out / (frwindows.COVERAGE_FILE + ".tmp")
    cov_tmp.write_text(
        json.dumps(coverage, ensure_ascii=False, indent=1, allow_nan=False),
        encoding="utf-8",
    )
    os.replace(cov_tmp, out / frwindows.COVERAGE_FILE)

    if direction_names:
        merged_dir = frwindows.merge_direction_entries(
            direction_frozen, direction_fresh, direction_names
        )
        if args.direction_out:
            frwindows.save_direction(
                args.direction_out,
                merged_dir,
                calibration=(args.direction_start, args.direction_end),
                extra={"market": mk, "factor_count": len(merged_dir)},
            )
            print(f"      方向冻结 → {args.direction_out}（{len(merged_dir)} 因子）")

    # 落盘
    print("[4/6] 落盘面板 / 宽表 / IC ...")
    panel = pd.concat(panel_rows, ignore_index=True)
    panel["trade_date"] = pd.to_datetime(panel["trade_date"])
    if args.no_partition:
        panel.to_parquet(out / "factor_panel.parquet", index=False)
        n_wide_cols = _write_wide_scores(wide_parts, out / "monthly_scores.parquet")
    else:
        if not args.append:
            panel.to_parquet(out / "factor_panel.parquet", index=False)
        elif not (out / "factor_panel.parquet").exists():
            panel.to_parquet(out / "factor_panel.parquet", index=False)
        # 年分区是运行时的首选读取路径；增量续拉只替换本次年份
        frwindows.write_year_partitions(panel, out, frwindows.PANEL_DIR)
        n_wide_cols = _write_wide_scores_partitioned(wide_files, out)
        if tmp_wide_dir.exists():
            import shutil

            shutil.rmtree(tmp_wide_dir, ignore_errors=True)
    ic_all = pd.concat(ic_parts, axis=0)
    ic_rows = ic_all.stack().reset_index()
    ic_rows.columns = ["factor_code", "trade_date", "ic"]
    ic_rows["ic"] = ic_rows["ic"].astype(np.float32)
    if args.append and (out / "ic.parquet").exists():
        ic_rows = (
            pd.concat([pd.read_parquet(out / "ic.parquet"), ic_rows], ignore_index=True)
            .drop_duplicates(["factor_code", "trade_date"], keep="last")
            .reset_index(drop=True)
        )
    ic_rows.to_parquet(out / "ic.parquet", index=False)

    print("[5/6] 收益 / 基准 / 个股快照 / 目录 ...")
    fwd_long = fwd.stack()
    fwd_df = pd.DataFrame(
        {
            "trade_date": fwd_long.index.get_level_values(0),
            "symbol": fwd_long.index.get_level_values(1),
            "fwd_ret": fwd_long.to_numpy(dtype=np.float32),
        }
    )
    if args.append and (out / "fwd_returns.parquet").exists():
        fwd_df = (
            pd.concat([pd.read_parquet(out / "fwd_returns.parquet"), fwd_df], ignore_index=True)
            .drop_duplicates(["trade_date", "symbol"], keep="last")
            .reset_index(drop=True)
        )
    fwd_df.to_parquet(out / "fwd_returns.parquet", index=False)
    bp = classic / "benchmarks.parquet"
    if mk == "CN" and bp.exists():
        bench = pd.read_parquet(bp)
        if args.append and (out / "benchmarks.parquet").exists():
            bench = (
                pd.concat([pd.read_parquet(out / "benchmarks.parquet"), bench], ignore_index=True)
                .drop_duplicates(["trade_date", "index_code"], keep="last")
                .reset_index(drop=True)
            )
        bench.to_parquet(out / "benchmarks.parquet", index=False)
    else:
        if mk == "CN":
            # 存量 CN 兜底清单（与 scorecard.BENCH_ORDER 同码；勿改，注册表 CN 码不同）
            bench_syms = ("000300.SH", "000906.SH", "000905.SH")
        else:
            from backend.services.engine.factor_report.blocks_common import (
                benchmarks_for_market,
            )

            bench_syms = tuple(sym for sym, _ in benchmarks_for_market(mk))
        rows = []
        for sym in bench_syms:
            s = (
                frdata.load_index_close(lookback_s, "20991231", sym, mk)
                .reindex(dates)
                .dropna()
            )
            if s.empty:
                continue
            b = (s / s.iloc[0]).rename("nav").reset_index()
            b.columns = ["trade_date", "nav"]
            b["index_code"] = sym
            rows.append(b)
        # 空表也落盘：缺文件=「没建过」、空表=「本市场无基准」（FUTURES 无指数数据），
        # 两个信号不混——缺文件会让上层指向「一键构建」，空表则如实说本市场无基准。
        bench = (
            pd.concat(rows, ignore_index=True)
            if rows
            else pd.DataFrame({"trade_date": [], "nav": [], "index_code": []})
        )
        if args.append and (out / "benchmarks.parquet").exists():
            bench = (
                pd.concat([pd.read_parquet(out / "benchmarks.parquet"), bench], ignore_index=True)
                .drop_duplicates(["trade_date", "index_code"], keep="last")
                .reset_index(drop=True)
            )
        bench.to_parquet(out / "benchmarks.parquet", index=False)
    if mk != "CN":
        snap = _stock_snapshot_non_cn(mk, daily)
        snap.to_parquet(out / "stock_snapshot.parquet", index=False)
        print(f"      个股快照：{len(snap)} 只（名称/行业/估值缺省处诚实置空）")

    if args.append and (out / "factors.json").exists():
        try:
            old = json.loads((out / "factors.json").read_text(encoding="utf-8"))
            old_map = {e["code"]: e for e in old.get("factors", [])}
            for e in meta_entries:
                old_map[e["code"]] = e
            meta_entries = list(old_map.values())
            old_window = old.get("meta", {}).get("window")
            if old_window:
                coverage["window"] = [
                    min(old_window[0], coverage["window"][0]),
                    max(old_window[1], coverage["window"][1]),
                ]
        except (OSError, ValueError, KeyError):
            pass  # 旧目录损坏不阻塞数据落盘；新目录照常写

    meta_entries.sort(key=lambda e: (e["l1"], e["l2"] or "", e["code"]))
    l1_order = []
    l2_order = {}
    for e in meta_entries:
        if e["l1"] not in l1_order:
            l1_order.append(e["l1"])
        l2s = l2_order.setdefault(e["l1"], [])
        if e["l2"] and e["l2"] not in l2s:
            l2s.append(e["l2"])
    pool_note = (
        "非 ST/退市（instrument 快照）+ 近 120 日 60% 有效历史"
        if mk == "CN"
        else f"{mk} 无 ST/退市口径（instrument_detail 为 A 股专属），仅近 120 日 60% 有效历史"
    )
    meta = {
        "dataset": "factor_research_private",
        "market": mk,
        "source": source,
        "source_requested": args.source,
        "source_skip_reason": source_skip_reason,
        "built_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "window": coverage["window"],
        "lookback_start": lookback_s,
        "n_dates": len(coverage.get("months", [])) or len(dates),
        "n_symbols": int(close.shape[1]),
        "n_factors": len(meta_entries),
        "panel_k": PANEL_K,
        "sources": {lib: len(v) for lib, v in external.items()}
        | {"factor_research": len(fr_kept)},
        "universe": pool_note,
        "direction_calibration": {
            "start": args.direction_start,
            "end": args.direction_end,
            "file": frwindows.DIRECTION_FILE,
        },
        "partitioned": not args.no_partition,
        "coverage_file": frwindows.COVERAGE_FILE,
        "survivorship_bias": bool(coverage["survivorship_bias"]),
        "appended": bool(args.append),
        "conventions": (
            "采样日/股票池/成本同经典数据集；方向按校准窗 IC 冻结（越大越好）"
            if mk == "CN"
            else "采样日/成本同经典数据集；方向按校准窗 IC 冻结（越大越好）；"
            f"股票池：{pool_note}"
        ),
    }
    (out / "factors.json").write_text(
        json.dumps(
            {
                "factors": meta_entries,
                "l1_order": l1_order,
                "l2_order": l2_order,
                "meta": meta,
            },
            ensure_ascii=False,
            indent=1,
            allow_nan=False,
        ),
        encoding="utf-8",
    )
    (out / "metrics.json").write_text(
        json.dumps({"meta": meta}, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(
        f"[6/6] 完成 → {out}（{len(meta_entries)} 因子 × {len(dates)} 期，"
        f"面板 {len(panel):,} 行，宽表 {n_wide_cols} 列，"
        f"survivorship_bias={coverage['survivorship_bias']}，总 {time.time() - t0:.0f}s）"
    )
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
