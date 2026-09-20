#!/usr/bin/env python3
"""构建「筛选保留（去L2）273 因子」合并数据集 → 自定义市场（quantcustom）。

把筛选保留集里分布在 5 个库（alpha_library/alpha360/jq110/tdxgs/l1_factors）
的因子按 (trade_date × symbol) 合并为一份 parquet 分区，供**单次直读训练**使用
（平台直读训练限定单数据源，自定义市场是官方通道）。

产物：<quantcustom>/6_ml_datasets/l1_factors/dt=YYYYMMDD/data.parquet
    列：symbol + date + 273 因子列 + 全套 OHLCV（close 前复权，训练标签用）

口径（与回测一致）：
- 股票池：全 A 非 ST/退市（instrument_detail 静态口径，同 build_universe）；
- 可交易性：当日涨停或跌停 → 该 (日,股) 行 close 置 NaN（标签不可算 = 样本剔除）。
  阈值**逐日逐票**取 ``local_market_data.limit_pct``（板别 + 创业板改革日 + ST），
  不再复述 9.8/19.8/29.8 的静态表 —— 见 `_limit_thresholds`；
- 因子取值缺失写 NaN（训练端截面预处理负责填充）；
- close 用 daily_forward（前复权）。

增量模式（默认）：meta.json 记录筛选指纹 + 股票池 + 参数，指纹一致时只补写
缺失分区（供每日定时重建）；筛选集 / --start / --min-coverage 任一变化会自动
退回全量重建。`--full` 强制全量。

用法（仓库根）:
    python3 backend/scripts/build_factor_custom_dataset.py --start 2016-01-01
    python3 backend/scripts/build_factor_custom_dataset.py --smoke 300    # 冒烟
    python3 backend/scripts/build_factor_custom_dataset.py --full         # 强制全量

定时重建：由 Celery 市场同步调度以 market="CUSTOM" 挂载（默认每天 03:00，
见 backend/services/engine/tasks/market_sync_scheduler.py）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from collections.abc import Callable

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.shared.quantdb_paths import resolve_quantdb_dir  # noqa: E402

# 五个来源库 → 数据目录相对路径（key 与 screening 的 library 一致）
LIB_DIRS = {
    "alpha_library": ("6_ml_datasets", "alpha_library"),
    "alpha360": ("6_ml_datasets", "alpha360"),
    "jq110": ("6_ml_datasets", "jq110"),
    "tdxgs": ("6_ml_datasets", "tdxgs"),
    "l1_factors": ("6_ml_datasets", "l1_factors"),
}
DROP_COLS = {"symbol", "time", "dt", "date", "open", "high", "low", "volume", "amount"}

DEFAULT_START = "2016-01-01"
# 覆盖率阈值随窗口长度调整（全窗口口径）：6 年窗（2020 起）用 0.9 约留 3600 只；
# 10.7 年窗（2016 起）用 0.9 只剩 2579 只（老票偏置），0.65 约留 3270 只，
# 与短窗口径下的推理覆盖（~3600）基本持平，同时仍剔除交易不足窗口 70% 的票。
DEFAULT_MIN_COVERAGE = 0.65
_OHLCV = ("open", "high", "low", "close", "volume", "amount")


#: 涨跌停剔除规则的版本号。**判定口径一变就必须 +1** —— 否则 `can_incremental`
#: 会认定历史分区仍然有效，新口径只作用于往后新增的交易日，磁盘上于是留下
#: 两套口径拼起来的训练集（前半段旧线、后半段新线，且看不出接缝）。
LIMIT_RULE_VERSION = 2

#: 取整容差（**百分点**，仅本常量的单位）。沿用旧值 9.8 = 10 − 0.2 的既有余量：
#: 涨跌停价本身要按分取整，真正封板的票可能只显示 9.97%，不留余量会把真涨停判丢。
_LIMIT_SLACK_PCT = 0.2

#: 同上，换算成**比例**。用 Decimal 减而不是浮点减：`0.1 - 0.002` 与字面量 0.098
#: 在二进制下相差一个 ulp，直接比较会假阴性 —— 这里要的是逐位等于
#: `limit_pct - 0.002`，因为调用点拿它与 `ret = close/pre_close - 1` 做同单位比较。
_LIMIT_SLACK = Decimal(str(_LIMIT_SLACK_PCT)) / 100


def _as_trade_date(value: Any) -> date:
    """交易日解析：**两种格式都收** —— 分区标签 ``20260918`` 与 ISO ``2026-09-18``。

    调用点传的是 ``dt=YYYYMMDD`` 目录名，而用例写 ISO；只认一种的话接口就变成
    「只有测试跑得通」。这种不一致不会在单测里暴露，只在跑真数据时才炸。
    """
    s = str(value).strip()
    if len(s) == 8 and s.isdigit():
        return date(int(s[0:4]), int(s[4:6]), int(s[6:8]))
    return date.fromisoformat(s)


def _limit_thresholds(symbols, trade_date: Any) -> np.ndarray:
    """全池在 ``trade_date`` 当日的涨跌停阈值（**比例**，已扣取整余量）。

    ⚠️ 单位是**比例**（0.098）而非百分数（9.8）—— 调用点与
    `ret = ohlcv["close"] / prev_close - 1` 比较。写成百分数不会报错，只会让
    `ret.abs() >= threshold` 恒假、整个可交易性剔除静默失效，正是本项目最
    典型的一类失真。`tests/test_build_factor_custom_dataset.py` 有专门的
    单位护栏用例。

    口径**唯一事实源** = ``local_market_data.limit_pct``：板块前缀（300/301/302、
    688/689、北交所 43/83/87/88/92）、创业板 2020-08-24 注册制改革、ST 主板
    5%→10% 全在那里。

    旧实现按代码前缀返回 9.8/19.8/29.8 且**不看交易日**，于是 2020-08-24 之前的
    创业板（真实 10% 板）被套上 19.8% 的线 —— 那些年份的涨停日判不出来，以
    「可买入」的身份留在训练标签里，正是「漂亮数据」。同一处还漏了 302 前缀，
    使 302 票按主板 9.8% 被**过度**剔除（真实 20% 板上的普通阳线被当成涨停）。

    逐票调用约 1.9 µs，全量重建约 8.5M 次 ≈ 16 秒。这是每天 03:00 的离线任务，
    十几秒换「口径唯一」是划算的；为省这点时间去复述板块前缀表，等于把缺陷
    再抄一遍。

    ``is_st`` 传 False 不是疏漏而是**前提**：股票池在构建时已排除 ST（见模块
    docstring 的 instrument_detail 静态口径）。残余缺口是「当年是 ST、如今不是」
    的票 —— 它们历史窗口内的 5% 涨停仍判不出。不拿静态快照去补，因为那是单日
    口径，回放历史会带前视偏差，比漏判更严重。
    """
    from backend.services.simulation.services.local_market_data import limit_pct

    td = _as_trade_date(trade_date)
    return np.array(
        [
            float(
                limit_pct(
                    str(s),
                    is_st=False,  # fidelity: allow-limit-threshold — 股票池已排除 ST
                    trade_date=td,
                )
                - _LIMIT_SLACK
            )
            for s in symbols
        ],
        dtype=np.float64,
    )


def _limit_threshold(code: str, trade_date: Any) -> float:
    """单票阈值 —— `_limit_thresholds` 的标量包装（测试与单点调用用）。"""
    return float(_limit_thresholds([str(code)], trade_date)[0])


def _load_kept_columns(root: Path) -> dict[str, list[str]]:
    """读取筛选保留集（去 L2），按来源库归组为待合并列（列名排序保证确定性）。"""
    sel = json.loads(
        (root / "factor_research" / "screening" / "factor_selection.json").read_text(
            encoding="utf-8"
        )
    )
    cols_by_lib: dict[str, list[str]] = {}
    for k in sel.get("kept", []):
        lib, name = str(k.get("library") or ""), str(k.get("name") or "")
        if lib in LIB_DIRS and lib != "l2_factors" and name:
            cols_by_lib.setdefault(lib, []).append(name)
    for lib in cols_by_lib:
        cols_by_lib[lib] = sorted(set(cols_by_lib[lib]))
    return cols_by_lib


def _selection_fingerprint(cols_by_lib: dict[str, list[str]]) -> str:
    """筛选集指纹：库→列清单的稳定哈希，用于增量模式的一致性守卫。"""
    payload = json.dumps(
        {lib: sorted(cols) for lib, cols in cols_by_lib.items()},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _missing_dates(dates: list[str], out_root: Path) -> list[str]:
    """尚无输出分区的交易日（增量重建的补写清单）。"""
    return [d for d in dates if not (out_root / f"dt={d}" / "data.parquet").exists()]


def rebuild(
    *,
    start: str = DEFAULT_START,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
    smoke: int = 0,
    incremental: bool = True,
    force_full: bool = False,
    log: Callable[..., None] = print,
) -> dict[str, Any]:
    """合并五库因子为自定义市场数据集；返回摘要 dict（供调度任务记录）。"""
    t0 = time.time()
    root = resolve_quantdb_dir()

    # 1) 筛选保留集（去 L2）
    cols_by_lib = _load_kept_columns(root)
    total_cols = sum(len(v) for v in cols_by_lib.values())
    log(
        f"[1/5] 保留集(去L2)合并列数 {total_cols}: { {k: len(v) for k, v in cols_by_lib.items()} }"
    )
    fingerprint = _selection_fingerprint(cols_by_lib)

    # 2) 股票池：全 A 非 ST/退市（静态口径）
    instr = pd.read_parquet(
        root / "2_base_sector" / "instrument_detail" / "instrument_detail.parquet",
        columns=["Symbol", "IsSTGP", "IsQuitGP"],
    )
    ok_sym = instr[
        (instr["IsSTGP"].astype(str) == "0") & (instr["IsQuitGP"].astype(str) == "0")
    ]["Symbol"].tolist()
    dates = [
        d
        for d in sorted(
            p.name[3:] for p in (root / "1_kline_data" / "daily_forward").glob("dt=*")
        )
        if d >= start.replace("-", "")
    ]
    if not dates:
        log(f"[2/5] 源数据窗口为空（start={start}），无事可做")
        return {"written": 0, "mode": "noop", "reason": "empty source window"}

    custom_root = Path(os.getenv("QM_QUANTCUSTOM_DATA_DIR") or "/data/quantcustom")
    out_root = custom_root / "6_ml_datasets" / "l1_factors"
    meta_path = out_root / "meta.json"

    prev_meta: dict[str, Any] = {}
    if meta_path.is_file():
        try:
            prev_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log(f"[!] meta.json 读取失败（按全量处理）: {exc}")

    # 增量守卫：筛选集/参数一致且已有股票池时才允许增量补写
    can_incremental = bool(
        incremental
        and not force_full
        and not smoke
        and prev_meta.get("symbols")
        and prev_meta.get("selection_fingerprint") == fingerprint
        and float(prev_meta.get("min_coverage") or 0) == float(min_coverage)
        and str(prev_meta.get("start") or "") == start
        # 涨跌停口径也是「参数」：改了判定规则却不重建，历史分区仍是旧线，
        # 新分区是新线 —— 磁盘上并存两套口径且看不出接缝（历史 meta 无此键
        # 时取 0 ≠ 2，故老产物一律强制全量重建一次）。
        and int(prev_meta.get("limit_rule_version") or 0) == LIMIT_RULE_VERSION
    )
    if incremental and not force_full and not can_incremental:
        log(
            "[i] 元数据缺失或不一致"
            "（筛选集 / start / min-coverage / 涨跌停口径版本 变化）→ 全量重建"
        )

    log(f"[2/5] 股票池（非 ST/退市）: {len(ok_sym)} 只，窗口 {len(dates)} 日")
    if smoke:
        ok_sym = ok_sym[:smoke]
    elif can_incremental:
        # 复用登记在 meta 的股票池；与当前静态口径取交集（新 ST/退市即时剔除）
        static_ok = set(ok_sym)
        ok_sym = [s for s in prev_meta["symbols"] if s in static_ok]
        log(f"      增量模式复用股票池: {len(ok_sym)} 只")
    elif min_coverage > 0:
        # 覆盖过滤：窗口内有效收盘占比 ≥ min_coverage，剔除长期停牌/次新等
        # （数据驱动、无未来函数；把训练体量压到内存安全范围）。
        cnt: dict[str, int] = {}
        for dt in dates:
            d = pd.read_parquet(
                root / "1_kline_data" / "daily_forward" / f"dt={dt}" / "data.parquet",
                columns=["symbol", "close"],
            )
            close = d.set_index("symbol")["close"].reindex(ok_sym)
            for sym, v in close.items():
                if pd.notna(v) and v > 0:
                    cnt[sym] = cnt.get(sym, 0) + 1
        floor = int(len(dates) * min_coverage)
        ok_sym = [sym for sym in ok_sym if cnt.get(sym, 0) >= floor]
        log(f"      覆盖率 ≥ {min_coverage:.0%}: 保留 {len(ok_sym)} 只")

    sym_index = pd.Index(ok_sym)

    # 3) 逐日合并（5 库 × 单日分区 → 1 个自定分区）
    if can_incremental:
        write_dates = _missing_dates(dates, out_root)
        mode = "incremental"
    else:
        write_dates = list(dates)
        mode = "full"
    log(
        f"[3/5] 交易日 {len(dates)} 个（{dates[0]} ~ {dates[-1]}）→ {out_root}；待写 {len(write_dates)} 个（{mode}）"
    )

    def _ohlcv_of(dt: str) -> pd.DataFrame:
        d = pd.read_parquet(
            root / "1_kline_data" / "daily_forward" / f"dt={dt}" / "data.parquet",
            columns=["symbol", *_OHLCV],
        )
        return d.set_index("symbol").reindex(sym_index)[list(_OHLCV)]  # NaN 保留

    # 增量补写时首日的前收需要从源数据（前一个交易日）取，涨跌停判定才连续
    prev_close = None
    if write_dates and write_dates[0] != dates[0]:
        i0 = dates.index(write_dates[0])
        prev_close = _ohlcv_of(dates[i0 - 1])["close"]

    written = 0
    for i, dt in enumerate(write_dates):
        frames: list[pd.DataFrame] = []
        for lib, dirs in LIB_DIRS.items():
            cols = cols_by_lib.get(lib)
            if not cols:
                continue
            f = root.joinpath(*dirs) / f"dt={dt}" / "data.parquet"
            if not f.exists():
                continue
            df = pd.read_parquet(f, columns=["symbol", *cols])
            frames.append(df.set_index("symbol").reindex(sym_index)[cols])
        if not frames:
            continue
        cur = pd.concat(frames, axis=1)
        ohlcv = _ohlcv_of(dt)
        # 直读 REQUIRED_COLUMNS = symbol,date,open,high,low,close,volume,amount → 附全套 OHLCV
        for c in _OHLCV:
            cur[c] = ohlcv[c]
        if prev_close is not None:
            # 可交易性（标签 = 训练端由 close 计算的前向收益，置 NaN 即样本剔除）：
            #  当日涨停（买不进）或当日跌停（前一日标签卖不出）→ close(t) 置 NaN；
            #  同时使 t-1 的对应样本不可算（保守剔除，无虚假收益）。
            ret = ohlcv["close"] / prev_close - 1
            # 阈值**逐日**取：创业板 2020-08-24 两侧板规不同，用一条静态线
            # 会把改革前的涨停日放行成可成交样本（见 _limit_thresholds）。
            threshold = _limit_thresholds(sym_index, dt)
            blocked = (
                (ret.abs() >= threshold) | ohlcv["close"].isna() | prev_close.isna()
            )
            cur["close"] = ohlcv["close"].where(~blocked)
        else:
            cur["close"] = ohlcv["close"]
        prev_close = ohlcv["close"]
        cur.insert(
            0, "date", pd.Timestamp(dt).strftime("%Y-%m-%d")
        )  # reader 需 YYYY-MM-DD
        cur = cur.reset_index().rename(columns={"index": "symbol"})
        cur = cur.replace([np.inf, -np.inf], np.nan)
        d_out = out_root / f"dt={dt}"
        d_out.mkdir(parents=True, exist_ok=True)
        cur.to_parquet(d_out / "data.parquet", index=False)
        written += 1
        if i % 200 == 0:
            log(f"      {dt} 已有 {written} 个分区（{time.time() - t0:.0f}s）")

    # 4) 元数据（增量守卫依赖 symbols / fingerprint / 参数；冒烟运行不落 meta）
    if not smoke:
        meta = {
            "dataset": "quantcustom/6_ml_datasets/l1_factors",
            "built_at": datetime.now().isoformat(timespec="seconds"),
            "window": [dates[0], dates[-1]] if dates else None,
            "n_days": len(dates),
            "n_written": written,
            "mode": mode,
            "start": start,
            "min_coverage": float(min_coverage),
            "selection_fingerprint": fingerprint,
            "limit_rule_version": LIMIT_RULE_VERSION,
            "symbols": ok_sym,
            "universe": "全 A 非 ST/退市（静态口径）",
            "n_factors": total_cols,
            "factors_by_source": {k: len(v) for k, v in cols_by_lib.items()},
            "limit_source": "local_market_data.limit_pct（逐日逐票；板别 + 创业板 2020-08-24 + ST 主板 5%→10%），取整余量 -0.2pp",
            "exclusions": "当日涨停/跌停行 close 置 NaN（标签不可算 = 样本剔除）；次日跌停由标签自然不可实现未单列",
            "conventions": "close=daily_forward 前复权；因子缺失 NaN 由训练端截面预处理（中位数填充+1%/99%缩尾+Z-score）处理",
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    elapsed = time.time() - t0
    log(f"[5/5] 完成 → {out_root}（{mode}：写 {written} 分区，总 {elapsed:.0f}s）")
    return {
        "written": written,
        "mode": mode,
        "window": [dates[0], dates[-1]],
        "pool": len(ok_sym),
        "n_factors": total_cols,
        "elapsed_s": round(elapsed, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=DEFAULT_START, help="起始日（含）")
    ap.add_argument("--smoke", type=int, default=0, help="冒烟：只取前 N 只股票")
    ap.add_argument(
        "--min-coverage",
        type=float,
        default=DEFAULT_MIN_COVERAGE,
        help="窗口内有效收盘覆盖率下限（剔除长期停牌/次新/垃圾股；0=不过滤）",
    )
    ap.add_argument(
        "--full",
        action="store_true",
        help="强制全量重建（忽略 meta 的增量元数据）",
    )
    args = ap.parse_args()
    summary = rebuild(
        start=args.start,
        min_coverage=args.min_coverage,
        smoke=args.smoke,
        force_full=args.full,
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
