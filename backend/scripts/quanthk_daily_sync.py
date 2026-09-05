#!/usr/bin/env python3
"""港股数据同步入口 — 按勾选数据集分发到对应同步脚本。

支持的数据集（与后台 catalog 对应）:
  daily_forward                                        → akshare 增量回拉（不复权，与付费历史口径一致）
  index_daily / valuation / sector / f10 / income /
  balance / cashflow / splits / 4_analyst 系列          → 雅虎源（skip_kline=True，避免限流）
  akshare_valuation / akshare_financial / akshare_profile → akshare 源
  index_daily                                          → akshare 指数（akshare_index_sync）

用法:
  python backend/scripts/quanthk_daily_sync.py --days 5
  python backend/scripts/quanthk_daily_sync.py --datasets daily_forward,index_daily --days 5
"""

from __future__ import annotations

import sys
from typing import Any

from backend.scripts.global_market_sync import run as _yahoo_run

# akshare 港股基本面数据集 → akshare_hk_fundamental 的 field
AKSHARE_HK_FIELDS = {
    "akshare_valuation": "valuation",
    "akshare_financial": "financial",
    "akshare_profile": "profile",
    "dividend": "dividend",
}

# 雅虎数据段（global_market_sync 处理）
_YAHOO_DATASETS = {
    "daily_forward", "index_daily", "valuation", "sector", "f10",
    "income", "balance", "cashflow", "splits",
    "recommendations", "upgrades_downgrades", "earnings_history",
    "earnings_dates", "earnings_estimate", "revenue_estimate",
    "growth_estimates", "analyst_price_targets", "major_holders",
    "mutual_fund_holders", "calendar", "insider_transactions", "options_chain",
}


def _sync_akshare_kline(result: dict[str, Any], *, days: int, symbols: str | None = None) -> None:
    """akshare 港股日线增量回拉（不复权），写入 daily_forward 的唯一来源。"""
    from backend.scripts.quanthk_akshare_kline import sync as _ak_kline_sync

    try:
        kwargs: dict[str, Any] = {"days": days}
        if symbols:
            kwargs["symbols"] = [s.strip().zfill(5) for s in symbols.split(",") if s.strip()]
        result["akshare_kline"] = _ak_kline_sync(**kwargs)
    except Exception as exc:  # noqa: BLE001
        result["akshare_kline"] = {"error": str(exc)}


def _south_source_result(days: int) -> dict[str, Any]:
    """南向资金原始数据同步（独立爬虫，受数据源勾选控制）。"""
    try:
        from backend.shared.data_source_config import is_source_enabled

        enabled = is_source_enabled("HK", "hsgt_south")
    except Exception:  # noqa: BLE001
        enabled = True
    if not enabled:
        return {"status": "skipped", "reason": "未勾选南向资金"}
    try:
        from backend.scripts.quanthk_south_sync import sync as south_sync

        return south_sync(days=days)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": str(exc)}


def _ccass_source_result(days: int) -> dict[str, Any]:
    """CCASS 机构持仓原始数据同步（增量爬虫，受数据源勾选控制）。"""
    try:
        from backend.shared.data_source_config import is_source_enabled

        enabled = is_source_enabled("HK", "ccass_top50")
    except Exception:  # noqa: BLE001
        enabled = True
    if not enabled:
        return {"status": "skipped", "reason": "未勾选 CCASS"}
    try:
        from backend.scripts.quanthk_ccass_sync import run as ccass_sync

        return ccass_sync(days=days)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": str(exc)}


def _refresh_l1_dataset(result: dict[str, Any], *, days: int) -> None:
    """K线更新后增量重算 L1 因子日频分区（训练直连数据集）。"""
    try:
        from backend.scripts.build_ml_l1_dataset import build_l1

        result["l1_dataset"] = build_l1("hong_kong")
    except Exception as exc:  # noqa: BLE001
        result["l1_dataset"] = {"status": "error", "error": str(exc)}


def _refresh_signal_datasets(result: dict[str, Any]) -> None:
    """本地信号数据集刷新钩子（南向/CCASS 因子等）。

    对应生成器模块属内部资产、不入库；模块缺失（如新装环境）时优雅跳过。"""
    try:
        from backend.scripts.build_ml_signal_datasets import refresh_signal_datasets as _fn

        result["signal_datasets"] = _fn(result)
    except ModuleNotFoundError as exc:
        if "build_ml_signal_datasets" not in str(exc):
            raise
        result["signal_datasets"] = {"status": "skipped", "reason": "local module absent"}
    except Exception as exc:  # noqa: BLE001
        result["signal_datasets"] = {"status": "error", "error": str(exc)}


def run(*, days: int = 5, symbols: str | None = None, datasets: list[str] | None = None,
        fast: bool = False, **kwargs: Any) -> dict:
    """同步港股数据。datasets 为勾选的数据集名；None 时全量同步雅虎数据。

    按数据集分发到对应数据源脚本。hsgt_south（南向资金/港股通）走独立
    爬虫同步脚本，落盘 {quanthk}/2_base_sector/hsgt_south。
    """
    result: dict[str, Any] = {"market": "HK", "days": days, "datasets": datasets or []}

    if not datasets:
        # 全量定时路径：核心数据(K线→南向→L1→CCASS→南向/CCASS因子)先落盘保证每晚必达，
        # 雅虎元数据段(估值快照/财务序列/分析师)最后执行——任务有 3600s 硬限制，
        # 若被截断只损失元数据增量，次日续跑；雅虎必须 skip_kline(K线口径铁律)。
        _sync_akshare_kline(result, days=days, symbols=symbols)
        result["sources"] = {"hsgt_south": _south_source_result(days=days)}
        _refresh_l1_dataset(result, days=days)
        # CCASS 机构持仓：HKEX 披露晚到（T+1~T+2），每晚增量补齐；
        # 脚本按交易日增量、已存在分区跳过，数据齐全时秒级完成
        result["ccass"] = _ccass_source_result(days=days)
        # 个股预测/研究服务的 PG 快照（stock_daily_latest_hk）刷新最新交易日
        try:
            from backend.scripts.quanthk_sdl_sync import sync_day as _sdl_sync

            result["sdl_hk"] = _sdl_sync()
        except Exception as exc:  # noqa: BLE001
            result["sdl_hk"] = {"status": "error", "error": str(exc)}
        result["yahoo"] = _yahoo_run("HK", days=days, symbols=symbols, fast=fast, skip_kline=True)
        # 本地信号因子(南向/CCASS)在原始数据全部落盘后统一增量刷新
        _refresh_signal_datasets(result)
        return result

    # 南向资金（港股通）— 独立爬虫，按数据源勾选控制
    if "hsgt_south" in datasets:
        result["sources"] = {"hsgt_south": _south_source_result(days=days)}

    # 南向/CCASS 等信号数据集（本地可选模块）
    if "south_factors" in datasets or "ccass_factors" in datasets:
        _refresh_signal_datasets(result)

    # 雅虎负责元数据段；daily_forward 从雅虎清单里剔除（K线口径铁律）
    yahoo_ds = [d for d in datasets if d in _YAHOO_DATASETS and d != "daily_forward"]
    if yahoo_ds:
        result["yahoo"] = _yahoo_run("HK", days=days, symbols=symbols, fast=fast, skip_kline=True)

    # K线（daily_forward）— akshare 增量回拉，不复权口径与付费历史一致
    if "daily_forward" in datasets:
        _sync_akshare_kline(result, days=days, symbols=symbols)

    # L1 因子日频分区（训练直连数据集，随 K线 增量刷新）
    if "l1_factors" in datasets:
        _refresh_l1_dataset(result, days=days)

    # akshare 港股基本面（估值/财务/资料/分红）
    akshare_fields = []
    for ds in datasets:
        if ds in AKSHARE_HK_FIELDS:
            akshare_fields.append(AKSHARE_HK_FIELDS[ds])
    if akshare_fields:
        from backend.scripts.akshare_hk_fundamental import sync as ak_fund_sync

        result["akshare_fundamental"] = {}
        for field in akshare_fields:
            try:
                result["akshare_fundamental"][field] = ak_fund_sync(field)
            except Exception as exc:  # noqa: BLE001
                result["akshare_fundamental"][field] = {"error": str(exc)}

    # akshare 指数（index_daily）
    if "index_daily" in datasets:
        from backend.scripts.akshare_index_sync import sync as ak_index_sync

        try:
            result["akshare_index"] = ak_index_sync("HK")
        except Exception as exc:  # noqa: BLE001
            result["akshare_index"] = {"error": str(exc)}

    # 港股 CCASS 机构持仓（ccass_top50）
    if "ccass_top50" in datasets:
        from backend.scripts.quanthk_ccass_sync import run as ccass_sync

        try:
            result["ccass"] = ccass_sync(days=days)
        except Exception as exc:  # noqa: BLE001
            result["ccass"] = {"error": str(exc)}

    return result


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="港股数据同步")
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
