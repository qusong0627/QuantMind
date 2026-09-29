#!/usr/bin/env python3
"""私人因子库快照构建（因子工作台「私人因子库」数据集）。

来源（``--source``，默认 ``auto``）：
- ``auto``（默认）：**自动扫描 ``6_ml_datasets/`` 下的全部因子数据集**（alpha_library 429、
  alpha360 360、l1_factors 110、l2_factors 211、jq110 109、tdxgs 88、features_daily 47 等，
  合计约 1300 个因子）——只读取你已算好的因子数据，不重算因子本身；
- ``l1l2``：仅 L1+L2 两个数据集（约 321 个）；
- ``kept``：多库联合筛选的最终保留清单（``screening/factor_selection.json`` 的 kept）。

跳过规则：``alpha_library_labels``（标签非因子）、``l1_l2_factors``（L1∪L2 冗余并集）；
跨数据集重名列按优先级（l1 → l2 → 各因子库 → features_daily）保留首次出现。

产物（<quantdb>/factor_research_private/）:
    factors.json          目录（code/display_name/来源库/方向/IC 指标）
    factor_panel.parquet  每因子每期前 150 名（rank/symbol/score/raw/fwd_ret，同经典口径）
    monthly_scores.parquet 宽表（trade_date, symbol, f1..fN float32）——合成按列裁剪读取
    ic.parquet            月频 RankIC（定向后：正值=有效方向）
    fwd_returns.parquet / benchmarks.parquet  持有期收益与三基准（复用经典快照；缺失时自算）
    metrics.json          构建元信息（状态接口用）

口径（与经典数据集一致）：
- 采样日（月末）、股票池（非 ST/退市、近 120 日 60% 有效）、成本（0.2%×换手）同经典；
- 打分 = 截面 pct rank → 正态分位（±4 截断）；方向按**全样本 IC 符号**自动统一
  （越大越好，供榜单/回测展示），原始值原样保留在 raw 列。

用法（仓库根）:
    python3 backend/scripts/build_factor_panel_private.py                   # 自动扫描 6_ml_datasets 全量
    python3 backend/scripts/build_factor_panel_private.py --source l1l2     # 仅 L1+L2
    python3 backend/scripts/build_factor_panel_private.py --source kept     # 筛选保留清单
    python3 backend/scripts/build_factor_panel_private.py --smoke 200       # 冒烟（200 只）
    FACTOR_RESEARCH_OUT=<dir> 可改输出目录（冒烟隔离）
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
from backend.services.engine.factor_research.catalog import BY_CODE  # noqa: E402
from backend.shared.quantdb_paths import resolve_quantdb_dir  # noqa: E402

LOOKBACK_START = "2018-06-01"
DEFAULT_START = "2020-01-01"
PANEL_K = 150
# 来源库（auto = 扫描 6_ml_datasets；l1l2 = L1/L2 因子数据；kept = 多库筛选保留清单）
L1L2_SOURCES = ("l1_factors", "l2_factors")
KEPT_LIB_SOURCES = ("alpha_library", "alpha360", "jq110", "tdxgs")
# auto 模式的优先序（重名列先到先得）；未列出的目录按字母序追加
AUTO_PRIORITY = (
    "l1_factors",
    "l2_factors",
    "alpha_library",
    "alpha360",
    "jq110",
    "tdxgs",
    "features_daily",
)
# auto 模式跳过的目录：标签集（非因子）与 L1∪L2 冗余并集
AUTO_SKIP = {"alpha_library_labels", "l1_l2_factors"}
LIB_LABELS = {
    "l1_factors": "L1 因子",
    "l2_factors": "L2 因子",
    "alpha_library": "Alpha 因子库",
    "alpha360": "Alpha360 量价",
    "jq110": "聚宽 JQ110",
    "tdxgs": "通达信指标",
    "features_daily": "每日特征（技术+估值）",
    "factor_research": "经典因子（demo 复刻）",
    "gap_mined": "空档挖掘因子",  # GAP_MINED_MARK
}


def _lib_label(lib: str) -> str:
    """库的中文标签；未登记的库回退成目录名。

    `auto` 的语义是「扫描 6_ml_datasets 全部因子数据集」，所以必须容忍没登记过的
    库：直接下标取字典的话，新增一个因子库就会在**跑完各库、落盘前**抛裸
    `KeyError`，几分钟算力白费且报错点离原因几百行。全脚本只此一处取标签。
    """
    return LIB_LABELS.get(lib, lib)


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


# 非因子列（标识与行情行情列不入库；date 为分区日期的冗余列）
DROP_COLS = {
    "symbol",
    "time",
    "dt",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
}


def _out_dir() -> Path:
    env = os.environ.get("FACTOR_RESEARCH_OUT")  # 冒烟测试用临时目录
    if env:
        d = Path(env)
        d.mkdir(parents=True, exist_ok=True)
        return d
    d = resolve_quantdb_dir() / "factor_research_private"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_kept() -> tuple[dict[str, list[dict]], list[dict]]:
    """五库联合筛选保留清单 → (外部库分组, factor_research 组)。"""
    p = (
        resolve_quantdb_dir()
        / "factor_research"
        / "screening"
        / "factor_selection.json"
    )
    if not p.exists():
        raise FileNotFoundError(f"筛选清单缺失（先跑 screen_factors.py）: {p}")
    sel = json.loads(p.read_text(encoding="utf-8"))
    external: dict[str, list[dict]] = {}
    fr_kept: list[dict] = []
    for k in sel.get("kept", []):
        if not k.get("name"):
            continue
        if k["library"] in KEPT_LIB_SOURCES:
            external.setdefault(k["library"], []).append(k)
        elif k["library"] == "factor_research":
            fr_kept.append(k)
    return external, fr_kept


def _load_l1l2(qroot: Path) -> tuple[dict[str, list[dict]], list[dict]]:
    """本地 L1/L2 因子数据的因子列清单 → (L1/L2 分组, 空)。"""
    external: dict[str, list[dict]] = {}
    for ds in L1L2_SOURCES:
        root = qroot / "6_ml_datasets" / ds
        parts = sorted(root.glob("dt=*/data.parquet"))
        if not parts:
            raise FileNotFoundError(
                f"{ds} 数据集缺失（先运行特征管线生成 {ds}）: {root}"
            )
        names = _numeric_names(parts[-1])
        external[ds] = [{"name": c, "display_name": c} for c in names]
    return external, []


def _load_auto(qroot: Path) -> tuple[dict[str, list[dict]], list[dict]]:
    """自动扫描 6_ml_datasets：全部含分区的因子数据集（按优先序去重）。"""
    root = qroot / "6_ml_datasets"
    if not root.exists():
        raise FileNotFoundError(f"6_ml_datasets 目录缺失: {root}")
    found = set()
    for d_ in root.iterdir():
        # 跳过：约定名、点目录、以及**下划线前缀**（临时/试跑目录的约定）。
        # 下划线必须排除，不只是为了整洁：`_`(0x5F) 字典序在字母前，试跑目录会排在
        # 正式库前面先占住列名，正式库的同名列随即被 seen 过滤掉 —— 因子还在，但
        # 用的是试跑那份（通常窗口短得多）的数据，且全程不报错。
        if not d_.is_dir() or d_.name in AUTO_SKIP or d_.name.startswith((".", "_")):
            continue
        parts = sorted(d_.glob("dt=*/data.parquet"))
        if parts:
            found.add(d_.name)
    order = [x for x in AUTO_PRIORITY if x in found] + sorted(
        found - set(AUTO_PRIORITY)
    )
    external: dict[str, list[dict]] = {}
    seen: set[str] = set()
    for ds in order:
        parts = sorted((root / ds).glob("dt=*/data.parquet"))
        names = [c for c in _numeric_names(parts[-1]) if c not in seen]
        if len(names) < 5:  # 非因子目录（记录/元数据）跳过
            continue
        seen.update(names)
        external[ds] = [{"name": c, "display_name": c} for c in names]
    return external, []


def _load_sources(source: str, qroot: Path) -> tuple[dict[str, list[dict]], list[dict]]:
    if source == "kept":
        return _load_kept()
    if source == "l1l2":
        return _load_l1l2(qroot)
    return _load_auto(qroot)


def _numeric_names(path: Path) -> list[str]:
    """分区内的数值型因子列（排除标识/行情列与字符串列）。"""
    import pyarrow as pa
    import pyarrow.parquet as pq

    out = []
    for field in pq.ParquetFile(path).schema_arrow:
        if field.name in DROP_COLS:
            continue
        if pa.types.is_integer(field.type) or pa.types.is_floating(field.type):
            out.append(field.name)
    return out


def _day_matrix(f: Path, names: list[str], symbols: pd.Index) -> np.ndarray:
    """读取单日分区 → (F, S) float32，按 symbols 对齐。"""
    df = pd.read_parquet(f, columns=["symbol", *names])
    df = df.set_index("symbol").reindex(symbols)
    return df[names].to_numpy(dtype=np.float32).T


def _score_row_block(v: np.ndarray) -> np.ndarray:
    """(F, S) 原始值 → 截面打分（pct rank → 正态分位 ±4；NaN 保持）。"""
    n = np.isfinite(v).sum(axis=1)
    df = pd.DataFrame(np.where(np.isfinite(v), v, np.nan))
    rk = df.rank(axis=1, method="average", na_option="keep")
    pct = rk.sub(0.5).div(pd.Series(n).replace(0, np.nan), axis=0)
    sc = np.clip(engine._norm_ppf_np(pct.to_numpy(dtype=float)), -4, 4)
    return sc.astype(np.float32), rk


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", type=int, default=0, help="冒烟：只取前 N 只股票")
    ap.add_argument(
        "--source",
        choices=["auto", "l1l2", "kept"],
        default="auto",
        help="因子来源：auto=自动扫描 6_ml_datasets（默认）；l1l2=仅 L1+L2；kept=筛选保留清单",
    )
    args = ap.parse_args()
    t0 = time.time()
    out = _out_dir()
    qroot = resolve_quantdb_dir()
    external, fr_kept = _load_sources(args.source, qroot)
    src_desc = " + ".join(f"{_lib_label(k)} {len(v)}" for k, v in external.items())
    print(f"[1/6] 来源({args.source})：{src_desc} + factor_research {len(fr_kept)}")

    daily = frdata.load_daily_panel(LOOKBACK_START, "20991231")
    instr = frdata.load_instrument()
    close = daily["close"]
    if args.smoke:
        syms = list(close.columns)[: args.smoke]
        for k in daily:
            daily[k] = daily[k][syms]
        close = daily["close"]
    dates = [
        d
        for d in frdata.month_end_dates(close.index)
        if d >= pd.Timestamp(DEFAULT_START)
    ]
    universe = frdata.build_universe(instr, close).reindex(dates).fillna(False)
    print(
        f"      采样日 {len(dates)}（{dates[0].date()} ~ {dates[-1].date()}）× {close.shape[1]} 只"
    )

    # 持有期收益 / 基准：优先复用经典快照（同采样日），缺失时自算
    classic = qroot / "factor_research"
    fwd = None
    fp = classic / "fwd_returns.parquet"
    if fp.exists():
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
        fwd = analysis.forward_returns(close, dates, universe)
        print("      持有期收益：本地重算")

    # 逐库计算
    idx = pd.MultiIndex.from_product(
        [dates, close.columns], names=["trade_date", "symbol"]
    )
    uni = universe.to_numpy(dtype=bool)
    fwd_np = fwd.to_numpy(dtype=np.float64)
    wide_parts: list[pd.DataFrame] = []
    ic_parts: list[pd.DataFrame] = []
    panel_rows: list[pd.DataFrame] = []
    meta_entries: list[dict] = []

    if args.source == "kept":
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
        for di in range(len(dates)):
            v = np.where(uni[di][None, :], vals[di], np.nan)
            sc, rk = _score_row_block(v)
            scores[di] = sc
            fr = np.where(uni[di], fwd_np[di], np.nan)
            rf = pd.Series(fr).rank()
            sub = rk.notna().to_numpy() & rf.notna().to_numpy()[None, :]
            cnt = sub.sum(axis=1)
            corr = rk.corrwith(rf, axis=1).to_numpy()
            corr[~(cnt >= 30)] = np.nan
            ic[:, di] = corr
        # 方向：全样本 IC 符号（正向因子保持，反向翻转，越大越好）
        with np.errstate(all="ignore"):
            ic_mean = np.nanmean(ic, axis=1)
        sign = np.where(np.isfinite(ic_mean) & (ic_mean < 0), -1.0, 1.0)
        scores_o = scores * sign[None, :, None]  # (D,F,S) × (1,F,1)
        ic_o = ic * sign[:, None]  # (F,D) × (F,1)
        print(
            f"[2/6] {lib}: {f_count} 因子 × {len(dates)} 期（{time.time() - tk:.0f}s，"
            f"反向 {int((sign < 0).sum())}）"
        )
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
        # 宽表打分（合成用）
        wide_parts.append(
            pd.DataFrame(
                # float32：分位数打分精度足够，宽表体积与峰值内存均减半
                scores_o.reshape(len(dates) * close.shape[1], f_count).astype(
                    np.float32
                ),
                index=idx,
                columns=names,
            )
        )
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
                        f"来源：{_lib_label(lib)} / {sub_map[name]}；方向按全样本 IC 自动统一（越大越好）。"
                        + sel_note
                    ),
                    "formula": "",
                    "wind_source": f"QuantDB 6_ml_datasets/{lib}",
                    "ic_mean": round(icm, 4) if np.isfinite(icm) else None,
                    "ic_std": round(ics, 4) if np.isfinite(ics) else None,
                    "ic_ir": round(icm / ics, 3)
                    if np.isfinite(ics) and ics > 0
                    else None,
                    "available": True,
                    "unavailable_reason": "",
                }
            )
        del vals, scores, scores_o, ic

    # factor_research 31：从经典快照拷贝（同口径，值已定向）
    if fr_kept:
        tk = time.time()
        names31 = [k["name"] for k in fr_kept]
        cp = pd.read_parquet(
            classic / "factor_panel.parquet", filters=[("factor_code", "in", names31)]
        )
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
        wide_parts.append(piv)
        ic31 = pd.read_parquet(
            classic / "ic.parquet", filters=[("factor_code", "in", names31)]
        )
        ic_parts.append(
            ic31.pivot(index="factor_code", columns="trade_date", values="ic").reindex(
                columns=pd.DatetimeIndex(dates)
            )
        )
        for k in fr_kept:
            f = BY_CODE.get(k["name"], {})
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

    # 落盘
    print("[4/6] 落盘面板 / 宽表 / IC ...")
    panel = pd.concat(panel_rows, ignore_index=True)
    panel["trade_date"] = pd.to_datetime(panel["trade_date"])
    panel.to_parquet(out / "factor_panel.parquet", index=False)
    n_wide_cols = _write_wide_scores(wide_parts, out / "monthly_scores.parquet")
    ic_all = pd.concat(ic_parts, axis=0)
    ic_rows = ic_all.stack().reset_index()
    ic_rows.columns = ["factor_code", "trade_date", "ic"]
    ic_rows["ic"] = ic_rows["ic"].astype(np.float32)
    ic_rows.to_parquet(out / "ic.parquet", index=False)

    print("[5/6] 收益 / 基准 / 目录 ...")
    fwd_long = fwd.stack()
    pd.DataFrame(
        {
            "trade_date": fwd_long.index.get_level_values(0),
            "symbol": fwd_long.index.get_level_values(1),
            "fwd_ret": fwd_long.to_numpy(dtype=np.float32),
        }
    ).to_parquet(out / "fwd_returns.parquet", index=False)
    bp = classic / "benchmarks.parquet"
    if bp.exists():
        pd.read_parquet(bp).to_parquet(out / "benchmarks.parquet", index=False)
    else:
        rows = []
        for sym in ("000300.SH", "000906.SH", "000905.SH"):
            s = (
                frdata.load_index_close(LOOKBACK_START, "20991231", sym)
                .reindex(dates)
                .dropna()
            )
            if s.empty:
                continue
            b = (s / s.iloc[0]).rename("nav").reset_index()
            b.columns = ["trade_date", "nav"]
            b["index_code"] = sym
            rows.append(b)
        pd.concat(rows, ignore_index=True).to_parquet(
            out / "benchmarks.parquet", index=False
        )

    meta_entries.sort(key=lambda e: (e["l1"], e["l2"] or "", e["code"]))
    l1_order: list[str] = []
    l2_order: dict[str, list[str]] = {}
    for e in meta_entries:
        if e["l1"] not in l1_order:
            l1_order.append(e["l1"])
        l2s = l2_order.setdefault(e["l1"], [])
        if e["l2"] and e["l2"] not in l2s:
            l2s.append(e["l2"])
    meta = {
        "dataset": "factor_research_private",
        "source": args.source,
        "built_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "window": [str(dates[0].date()), str(dates[-1].date())],
        "n_dates": len(dates),
        "n_symbols": int(close.shape[1]),
        "n_factors": len(meta_entries),
        "panel_k": PANEL_K,
        "sources": {lib: len(v) for lib, v in external.items()}
        | {"factor_research": len(fr_kept)},
        "conventions": "采样日/股票池/成本同经典数据集；方向按全样本 IC 符号自动统一（越大越好）",
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
        f"面板 {len(panel):,} 行，宽表 {n_wide_cols} 列，总 {time.time() - t0:.0f}s）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
