#!/usr/bin/env python3
"""美股数据同步入口 — 按勾选数据集分发到对应同步脚本。

支持的数据集（与后台 catalog 对应）:
  daily_forward / valuation / sector / f10 / income / balance / cashflow /
  dividend / splits / 4_analyst 系列 / 4_options  → 雅虎源（global_market_sync）
  index_daily                                      → akshare 指数（akshare_index_sync）

用法:
  python backend/scripts/quantus_daily_sync.py --days 5
  python backend/scripts/quantus_daily_sync.py --datasets daily_forward,index_daily --days 5
"""

from __future__ import annotations

import sys
from typing import Any

from backend.scripts.global_market_sync import run as _yahoo_run

# 雅虎数据段（global_market_sync 处理）
_YAHOO_DATASETS = {
    "daily_forward", "valuation", "sector", "f10",
    "income", "balance", "cashflow", "dividend", "splits",
    "recommendations", "upgrades_downgrades", "earnings_history",
    "earnings_dates", "earnings_estimate", "revenue_estimate",
    "growth_estimates", "analyst_price_targets", "major_holders",
    "mutual_fund_holders", "calendar", "insider_transactions", "options_chain",
}


def _refresh_l1_dataset(result: dict) -> None:
    """K线更新后增量重算 L1 因子日频分区（训练直读数据集）。"""
    try:
        from backend.scripts.build_ml_l1_dataset import build_l1

        result["l1_dataset"] = build_l1("us_stock")
    except Exception as exc:  # noqa: BLE001
        result["l1_dataset"] = {"status": "error", "error": str(exc)}


def _sync_index(result: dict) -> None:
    """akshare 指数日线增量同步（index_daily）。

    历史 bug：全量分支（datasets 为空）只跑雅虎数据段，从不触碰指数分区，
    导致 index_daily 长期停滞（实测停在 2026-08-27，同日个股已到 09-10），
    指数与个股并列展示时日期错位。两条分支都必须调用本函数。
    """
    from backend.scripts.akshare_index_sync import sync as ak_index_sync

    try:
        result["akshare_index"] = ak_index_sync("US")
    except Exception as exc:  # noqa: BLE001
        result["akshare_index"] = {"error": str(exc)}


def _refresh_sdl(result: dict) -> None:
    """刷新 stock_daily_latest_us（个股预测/研究服务的 PG 快照）。

    与港股同一约定：K 线落盘后同步最近 90 个交易日快照；缺这步时
    /research/predict-stock?market=US 第一步就报 relation does not exist。
    """
    from backend.scripts.quantus_sdl_sync import sync_day as _sdl_sync

    try:
        result["sdl_us"] = _sdl_sync()
    except Exception as exc:  # noqa: BLE001
        result["sdl_us"] = {"status": "error", "error": str(exc)}


def run(*, days: int = 5, symbols: str | None = None, datasets: list[str] | None = None,
        fast: bool = False, **kwargs: Any) -> dict:
    """同步美股数据。datasets 为勾选的数据集名；None 时全量同步雅虎数据。"""
    if not datasets:
        result = dict(_yahoo_run("US", days=days, symbols=symbols, fast=fast))
        result["market"] = "US"
        # 指数与个股必须同轮更新，否则 index_daily 停滞、页面日期错位
        _sync_index(result)
        # L1 因子直读数据集随日K落盘后刷新（与港股同口径）
        _refresh_l1_dataset(result)
        # 个股预测/研究服务的 PG 快照（stock_daily_latest_us）刷新最近 90 交易日
        _refresh_sdl(result)
        return result

    result: dict = {"market": "US", "days": days, "datasets": datasets}

    yahoo_ds = [d for d in datasets if d in _YAHOO_DATASETS]
    if yahoo_ds:
        result["yahoo"] = _yahoo_run("US", days=days, symbols=symbols, fast=fast)

    # akshare 指数（index_daily）
    if "index_daily" in datasets:
        _sync_index(result)

    # L1 因子日频分区（训练直读数据集，随 daily_forward 增量刷新）
    if "daily_forward" in datasets or "l1_factors" in datasets:
        _refresh_l1_dataset(result)
        _refresh_sdl(result)

    return result


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="美股数据同步")
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--symbols", default=None)
    parser.add_argument("--datasets", default=None, help="逗号分隔数据集名")
    args, _ = parser.parse_known_args()

    ds = [d.strip() for d in args.datasets.split(",") if d.strip()] if args.datasets else None
    result = run(days=args.days, symbols=args.symbols, datasets=ds)
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
