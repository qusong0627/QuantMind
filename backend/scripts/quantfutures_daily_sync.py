#!/usr/bin/env python3
"""期货数据同步入口 — 按勾选数据集分发到 akshare 期货同步脚本。

支持的数据集（与后台 catalog 对应）:
  daily_forward     期货/贵金属日K  → foreign_daily + cn_daily + sge_daily
  futures_realtime  实时行情快照    → foreign_realtime + cn_realtime
  warehouse_receipts 交易所仓单     → akshare_futures_extra.task_receipts
  member_positions  会员持仓排名    → akshare_futures_extra.task_member_positions
  contracts_daily   分合约日K       → akshare_futures_extra.task_contracts_daily
  cftc              CFTC持仓        → akshare_futures_extra.task_cftc
  fx_daily          汇率(中行牌价)  → akshare_futures_extra.task_fx
  options_risk      期权逐合约风险  → akshare_futures_options.task_options_risk
  options_stats     期权每日统计    → akshare_futures_options.task_options_stats
  （国内扩展二批 → akshare_futures_cn_ext，见 EXTRA_DATASET_TASKS）
  spot_basis/settle_params/trade_rule/contract_meta/exchange_daily/inventory_em/
  ccidx_index/fees_info/hog_chain/member_rank_sina

用法:
  python backend/scripts/quantfutures_daily_sync.py --days 5
  python backend/scripts/quantfutures_daily_sync.py --datasets daily_forward,futures_realtime
"""

from __future__ import annotations

import logging
import sys
from typing import Any

log = logging.getLogger("quantfutures_daily_sync")

# catalog 数据集名 → akshare_futures_sync 的 field（一个数据集可对应多个数据段）
DATASET_FIELDS: dict[str, list[str]] = {
    "daily_forward": ["foreign_daily", "cn_daily", "sge_daily"],
    "futures_realtime": ["foreign_realtime", "cn_realtime"],
}

# catalog 数据集名 → (模块简称, task 名)；task 函数统一签名 fn(days: int) -> dict
EXTRA_DATASET_TASKS: dict[str, tuple[str, str]] = {
    "warehouse_receipts": ("extra", "receipts"),
    "member_positions": ("extra", "member_positions"),
    "contracts_daily": ("extra", "contracts_daily"),
    "cftc": ("extra", "cftc"),
    "fx_daily": ("extra", "fx"),
    "options_risk": ("options", "options_risk"),
    "options_stats": ("options", "options_stats"),
    "spot_basis": ("cn_ext", "spot_basis"),
    "settle_params": ("cn_ext", "settle_params"),
    "trade_rule": ("cn_ext", "trade_rule"),
    "contract_meta": ("cn_ext", "contract_meta"),
    "exchange_daily": ("cn_ext", "exchange_daily"),
    "inventory_em": ("cn_ext", "inventory_em"),
    "ccidx_index": ("cn_ext", "ccidx_index"),
    "fees_info": ("cn_ext", "fees_info"),
    "hog_chain": ("cn_ext", "hog_chain"),
    "member_rank_sina": ("cn_ext", "member_rank_sina"),
}

_EXTRA_MODULES: dict[str, str] = {
    "extra": "backend.scripts.akshare_futures_extra",
    "cn_ext": "backend.scripts.akshare_futures_cn_ext",
    "options": "backend.scripts.akshare_futures_options",
}

# 非 field/task 直映的特殊数据集（L1 因子直读数据集为派生产物）
_SPECIAL_DATASETS: frozenset[str] = frozenset({"l1_factors"})

# 每日全量同步（datasets=None）时自动附加的扩展数据集；
# contracts_daily 请求量大（82 品种 × 15 合约月 ≈ 1200 次），仅面板显式勾选才运行。
# contract_meta/exchange_daily/member_rank_sina 在回填场景请求量中等（数百次），
# 不进默认集合，由面板勾选或回填 CLI 显式跑。
DEFAULT_EXTRA_DATASETS: tuple[str, ...] = (
    "warehouse_receipts",
    "member_positions",
    "cftc",
    "fx_daily",
    "options_risk",
    "options_stats",
    "spot_basis",
    "settle_params",
    "trade_rule",
    "inventory_em",
    "ccidx_index",
    "fees_info",
    "hog_chain",
)


def _refresh_l1_dataset(result: dict) -> None:
    """K线更新后增量重算 L1 因子日频分区（训练直读数据集）。"""
    try:
        from backend.scripts.build_ml_l1_dataset import build_l1

        result["l1_dataset"] = build_l1("futures")
    except Exception as exc:  # noqa: BLE001
        result["l1_dataset"] = {"status": "error", "error": str(exc)}


def _run_extra_tasks(datasets: list[str], days: int) -> dict:
    """运行扩展数据集同步（按注册表分发到各模块 task_*，统一签名 fn(days)）。"""
    import importlib

    out: dict[str, Any] = {}
    mods: dict[str, Any] = {}
    for ds in datasets:
        entry = EXTRA_DATASET_TASKS.get(ds)
        if entry is None:
            continue
        mod_key, task = entry
        if mod_key not in mods:
            mods[mod_key] = importlib.import_module(_EXTRA_MODULES[mod_key])
        fn = getattr(mods[mod_key], f"task_{task}", None)
        if fn is None:
            out[ds] = {
                "status": "error",
                "error": f"{_EXTRA_MODULES[mod_key]} 缺少 task_{task}",
            }
            continue
        try:
            out[ds] = fn(days)
        except Exception as exc:  # noqa: BLE001
            out[ds] = {"status": "error", "error": str(exc)}
    return out


def run(*, days: int = 5, datasets: list[str] | None = None, **kwargs: Any) -> dict:
    """同步期货数据。datasets 为勾选的 catalog 数据集名；None 时全量同步。"""
    from backend.scripts.akshare_futures_sync import sync as ak_futures_sync

    if not datasets:
        result = ak_futures_sync("all")
        if not isinstance(result, dict):
            result = {"result": result}
        # 全量同步附带日频扩展数据集（contracts_daily 请求量大，不默认跑）
        result["extras"] = _run_extra_tasks(list(DEFAULT_EXTRA_DATASETS), days)
        # L1 因子直读数据集随日K落盘后刷新（与港股同口径）
        _refresh_l1_dataset(result)
        return {"market": "futures", "days": days, "result": result}

    unknown = [
        ds
        for ds in datasets
        if ds not in DATASET_FIELDS
        and ds not in EXTRA_DATASET_TASKS
        and ds not in _SPECIAL_DATASETS
    ]
    if unknown:
        log.warning("quantfutures sync: 未知数据集被忽略: %s", unknown)
    guard: dict[str, Any] = {"unknown_datasets": unknown} if unknown else {}

    fields: list[str] = []
    extra_datasets: list[str] = []
    for ds in datasets:
        if ds in EXTRA_DATASET_TASKS:
            extra_datasets.append(ds)
        else:
            fields.extend(DATASET_FIELDS.get(ds, []))
    if not fields and not extra_datasets:
        return {
            "market": "futures",
            "days": days,
            "datasets": datasets,
            "result": {},
            **guard,
        }

    result: dict[str, Any] = {}
    for field in fields:
        result[field] = ak_futures_sync(field)
    if extra_datasets:
        result["extras"] = _run_extra_tasks(extra_datasets, days)

    # L1 因子日频分区（训练直读数据集，随 daily_forward 增量刷新）
    if "daily_forward" in datasets or "l1_factors" in datasets:
        _refresh_l1_dataset(result)
    return {
        "market": "futures",
        "days": days,
        "datasets": datasets,
        "result": result,
        **guard,
    }


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="期货数据同步")
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--datasets", default=None, help="逗号分隔数据集名")
    args, _ = parser.parse_known_args()

    ds = (
        [d.strip() for d in args.datasets.split(",") if d.strip()]
        if args.datasets
        else None
    )
    result = run(days=args.days, datasets=ds)
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
