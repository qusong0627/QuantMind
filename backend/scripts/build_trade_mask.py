"""构建可交易性掩码（理想轨 vs 可交易轨的分母）。

产物：``5_technical_derived/trade_mask/trade_mask.parquet``（**稀疏**：只存被挡的行）
```
dt(int32) / symbol(str) / blocked_long(bool) / blocked_short(bool)
```

为什么要独立产物而不是在报告构建里现算：逐日装载全市场 ``DailyBar`` 实测
**0.12–0.22 s/天**，2604 天 ≈ **7 分钟**；而因子报告构建（alpha_library 439s、
factor_defs 1536s）本身已经很长，再叠 7 分钟且每天重跑一遍没有意义 ——
掩码只依赖行情与规则，**与因子集无关**，一次构建、所有数据集共用，
增量补新日期即可（``--start`` 给最近一天会自动重扫该段）。

口径与 ST 处理**全在** ``factor_report/tradability.py`` 的模块 docstring 里，本脚本只
负责日期列表、调用与落盘。

## 用法

    python3 backend/scripts/build_trade_mask.py                       # 2016 至今
    python3 backend/scripts/build_trade_mask.py --start 2026-09-01    # 增量补一段
    python3 backend/scripts/build_trade_mask.py --limit-days 20       # 冒烟

## 与报告构建的关系

``build_factor_report.py`` 用 ``--trade-mask <path>`` 指向本产物（默认自动探测同一位置）；
掩码缺失时该数据集**不产可交易轨**，meta 记 ``tradable: "missing"`` 并在前端显式提示，
**不报错也不写 0**。掩码与本脚本的日期范围不必完全一致：报告只对掩码覆盖到的日期算可交易轨。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backend.services.engine.factor_report import tradability as TB  # noqa: E402
from backend.shared.quantdb_paths import resolve_quantdb_subdir  # noqa: E402

log = logging.getLogger("build_trade_mask")


def list_trading_days(start: str, end: str) -> list[int]:
    """交易日列表 = ``valuation`` 的 hive 分区名（该数据集逐日全市场落盘）。

    用 valuation 而不是 daily_forward 分区：前者是「有估值就有交易」的日报，
    与因子报告的日期集合同源（报告构建器也按它对齐），避免掩码比报告多出几天空值。
    """
    import glob

    val_dir = resolve_quantdb_subdir("5_technical_derived", "valuation")
    out = []
    for p in sorted(glob.glob(str(Path(val_dir) / "dt=*"))):
        dt = os.path.basename(p).split("=", 1)[1]
        if len(dt) == 8 and dt.isdigit() and start <= dt <= end:
            out.append(int(dt))
    return out


def default_out_path() -> Path:
    return Path(resolve_quantdb_subdir("5_technical_derived", "trade_mask")) / "trade_mask.parquet"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="构建可交易性掩码（涨跌停/停牌）")
    p.add_argument("--start", default="2016-01-01", help="起始日 YYYY-MM-DD")
    p.add_argument("--end", default="2099-12-31", help="结束日 YYYY-MM-DD")
    p.add_argument("--out", default=None, help="产物路径（默认 QuantDB 5_technical_derived/trade_mask/）")
    p.add_argument("--limit-days", type=int, default=0, help="只跑最近 N 天（冒烟）")
    p.add_argument("--market", default="CN", help="市场（目前只支持 CN：涨跌停规则仅 A 股有）")
    args = p.parse_args()

    if args.market.upper() != "CN":
        log.error("可交易掩码目前只覆盖 CN（涨跌停/ST 规则是 A 股特有）；收到 %s", args.market)
        return 2

    t0 = time.time()
    days = list_trading_days(args.start.replace("-", ""), args.end.replace("-", ""))
    if args.limit_days:
        days = days[-args.limit_days :]
    if not days:
        log.error("区间内没有交易日（看 valuation 分区）—— 不写空产物")
        return 1
    log.info("区间 %s ~ %s：%d 个交易日", days[0], days[-1], len(days))

    df = TB.build_mask(days, market="CN")
    if df.empty:
        # 零项即失败：没扫到任何被挡行，多半是数据不可用而不是「市场很平静」
        log.error("掩码为空 —— 逐日行情可能取不到，拒绝写空产物")
        return 1

    out = Path(args.out) if args.out else default_out_path()
    stat = TB.write_mask(df, out)
    n_days = int(df["dt"].nunique())
    log.info(
        "落盘 %s：%d 行 / %d 天（多头被挡 %d 只次、空头被挡 %d 只次，%.0fs）",
        stat["path"], stat["rows"], n_days, stat["blocked_long"], stat["blocked_short"], time.time() - t0,
    )
    meta = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_s": round(time.time() - t0, 1),
        "range": [days[0], days[-1]],
        "n_days": n_days,
        "rows": stat["rows"],
        "blocked_long": stat["blocked_long"],
        "blocked_short": stat["blocked_short"],
        "coverage_frac": round(n_days / len(days), 4),
        "st_policy": "静态快照含前视偏差 → ST 标的整票不入掩码（见 factor_report/tradability.py）",
        "source": "services/simulation/services/local_market_data.DailyBar（平台唯一权威实现）",
    }
    mp = out.parent / "meta.json"
    tmp = mp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, mp)
    if n_days < len(days):
        log.warning("有 %d 天没有产出被挡行（该日可能全市场可交易，也可能行情缺失）", len(days) - n_days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
