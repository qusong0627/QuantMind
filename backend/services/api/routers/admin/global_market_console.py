"""QuantUS/QuantHK 管理控制台工厂。

为美股/港股生成一套本地数据管理路由（镜像 quantdb_console 的 catalog/preview/
sync 部分，去掉远端 SDK 相关端点）。两个市场复用同一套逻辑，仅数据目录与
数据集列表不同。

挂载示例:
    from .global_market_console import make_market_router
    router = make_market_router(market="US", env_var="QM_QUANTUS_DATA_DIR", default_dir="/data/quantus")
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.services.api.user_app.middleware.auth import require_admin

logger = logging.getLogger(__name__)

MAX_PREVIEW_ROWS = 200
MAX_SYMBOL_CHOICES = 500
MAX_JOB_HISTORY = 20

Layout = Literal["partition", "symbol", "single"]


@dataclass(frozen=True)
class DatasetSpec:
    dataset: str
    name: str
    category_id: str
    group: str
    rel_dir: str
    layout: Layout
    note: str = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _default_datasets(market: str) -> tuple[DatasetSpec, ...]:
    """US/HK 市场实际会落盘的数据集（对齐 QuantDB 目录结构）。

    ccass_top50 为港股专属（CCASS 机构持仓），仅 HK 市场展示。
    """
    if market == "BC":
        return (
            # 1 K线行情
            DatasetSpec(
                "daily_forward",
                "日线",
                "1",
                "kline",
                "1_kline_data/daily_forward",
                "partition",
                "Binance 日线",
            ),
            DatasetSpec(
                "min5_kline",
                "5分钟线",
                "1",
                "kline",
                "1_kline_data/min5_kline",
                "symbol",
                "Binance 5m，体积大，按需同步",
            ),
            DatasetSpec(
                "min1_kline",
                "1分钟线",
                "1",
                "kline",
                "1_kline_data/min1_kline",
                "symbol",
                "Binance 1m，体积大，按需同步",
            ),
            DatasetSpec(
                "index_daily",
                "指数日线",
                "1",
                "kline",
                "1_kline_data/index_daily",
                "partition",
            ),
            # 2 基础板块
            DatasetSpec(
                "instrument_detail",
                "标的详情",
                "2",
                "base_sector",
                "2_base_sector/instrument_detail",
                "single",
            ),
            DatasetSpec(
                "sector",
                "行业板块",
                "2",
                "base_sector",
                "2_base_sector/sector",
                "symbol",
            ),
            DatasetSpec(
                "f10", "基本面快照", "2", "base_sector", "2_base_sector/f10", "symbol"
            ),
            # 5 技术衍生
            DatasetSpec(
                "valuation",
                "估值",
                "5",
                "technical",
                "5_technical_derived/valuation",
                "partition",
                "Binance 收盘快照",
            ),
        )
    if market == "FUTURES":
        return (
            # 1 K线行情
            DatasetSpec(
                "daily_forward",
                "期货日K",
                "1",
                "kline",
                "1_kline_data/daily_forward",
                "partition",
                "期货/贵金属日K（国际 CL.FUT / 国内主力 / 上金所）",
            ),
            # 2 实时快照
            DatasetSpec(
                "futures_realtime",
                "实时行情",
                "2",
                "base_sector",
                "2_base_sector/futures_realtime",
                "symbol",
                "国际/国内期货实时快照",
            ),
            DatasetSpec(
                "warehouse_receipts",
                "交易所仓单",
                "2",
                "base_sector",
                "2_base_sector/warehouse_receipts",
                "partition",
                "DCE/CZCE/GFEX 仓单日报（SHFE 接口失效）",
            ),
            DatasetSpec(
                "member_positions",
                "会员持仓排名",
                "2",
                "base_sector",
                "2_base_sector/member_positions",
                "partition",
                "DCE/GFEX 前20会员多空持仓（东财源）",
            ),
            DatasetSpec(
                "contracts_daily",
                "分合约日K",
                "1",
                "kline",
                "2_base_sector/contracts_daily",
                "symbol",
                "国内分合约日K（含真实结算价/持仓量）",
            ),
            DatasetSpec(
                "cftc",
                "CFTC持仓",
                "2",
                "base_sector",
                "2_base_sector/cftc",
                "single",
                "CFTC COT 周度持仓（商品/商用）",
            ),
            DatasetSpec(
                "fx_daily",
                "汇率(中行牌价)",
                "2",
                "base_sector",
                "2_base_sector/fx_daily",
                "symbol",
                "主流货币兑人民币日度牌价（每100外币，USD/EUR/JPY/HKD 等）",
            ),
        )
    base = [
        # 1 K线行情
        DatasetSpec(
            "daily_forward",
            "日线",
            "1",
            "kline",
            "1_kline_data/daily_forward",
            "partition",
            "akshare 日线(不复权)"
        ),
        DatasetSpec(
            "index_daily",
            "指数日线",
            "1",
            "kline",
            "1_kline_data/index_daily",
            "partition",
        ),
        # 2 基础板块
        DatasetSpec(
            "instrument_detail",
            "标的详情",
            "2",
            "base_sector",
            "2_base_sector/instrument_detail",
            "single",
        ),
        DatasetSpec(
            "sector", "行业板块", "2", "base_sector", "2_base_sector/sector", "symbol"
        ),
        DatasetSpec(
            "f10", "基本面快照", "2", "base_sector", "2_base_sector/f10", "symbol"
        ),
        # 3 财务数据
        DatasetSpec(
            "income", "利润表", "3", "financial", "3_financial_data/income", "symbol"
        ),
        DatasetSpec(
            "balance",
            "资产负债表",
            "3",
            "financial",
            "3_financial_data/balance",
            "symbol",
        ),
        DatasetSpec(
            "cashflow",
            "现金流量表",
            "3",
            "financial",
            "3_financial_data/cashflow",
            "symbol",
        ),
        DatasetSpec(
            "dividend", "分红", "3", "financial", "3_financial_data/dividend", "symbol"
        ),
        DatasetSpec(
            "splits", "拆股", "3", "financial", "3_financial_data/splits", "symbol"
        ),
        # 5 技术衍生
        DatasetSpec(
            "valuation",
            "估值",
            "5",
            "technical",
            "5_technical_derived/valuation",
            "partition",
            "yahoo info 快照",
        ),
        # 4 分析师/持仓/期权（归入"分析预测"组）
        DatasetSpec(
            "recommendations",
            "分析师评级",
            "4",
            "analyst",
            "4_analyst/recommendations",
            "symbol",
        ),
        DatasetSpec(
            "upgrades_downgrades",
            "评级调整",
            "4",
            "analyst",
            "4_analyst/upgrades_downgrades",
            "symbol",
        ),
        DatasetSpec(
            "earnings_history",
            "盈利历史",
            "4",
            "analyst",
            "4_analyst/earnings_history",
            "symbol",
        ),
        DatasetSpec(
            "earnings_dates",
            "财报日期",
            "4",
            "analyst",
            "4_analyst/earnings_dates",
            "symbol",
        ),
        DatasetSpec(
            "earnings_estimate",
            "盈利预期",
            "4",
            "analyst",
            "4_analyst/earnings_estimate",
            "symbol",
        ),
        DatasetSpec(
            "revenue_estimate",
            "营收预期",
            "4",
            "analyst",
            "4_analyst/revenue_estimate",
            "symbol",
        ),
        DatasetSpec(
            "growth_estimates",
            "增长预期",
            "4",
            "analyst",
            "4_analyst/growth_estimates",
            "symbol",
        ),
        DatasetSpec(
            "analyst_price_targets",
            "目标价",
            "4",
            "analyst",
            "4_analyst/analyst_price_targets",
            "symbol",
        ),
        DatasetSpec(
            "major_holders",
            "主要股东",
            "4",
            "analyst",
            "4_analyst/major_holders",
            "symbol",
        ),
        DatasetSpec(
            "mutual_fund_holders",
            "共同基金持仓",
            "4",
            "analyst",
            "4_analyst/mutual_fund_holders",
            "symbol",
        ),
        DatasetSpec(
            "calendar", "分红/财报日历", "4", "analyst", "4_analyst/calendar", "symbol"
        ),
        DatasetSpec(
            "insider_transactions",
            "内部人交易",
            "4",
            "analyst",
            "4_analyst/insider_transactions",
            "symbol",
        ),
        DatasetSpec("options_chain", "期权链", "4", "analyst", "4_options", "symbol"),
    ]
    if market == "HK":
        base.append(
            DatasetSpec(
                "akshare_valuation",
                "估值(akshare)",
                "2",
                "base_sector",
                "2_base_sector/akshare_valuation",
                "symbol",
                "akshare 真实估值：PE/PB/PS/PCF + 排名",
            ),
        )
        base.append(
            DatasetSpec(
                "akshare_financial",
                "财务指标(akshare)",
                "2",
                "base_sector",
                "2_base_sector/akshare_financial",
                "symbol",
                "akshare 财务指标：EPS/ROE/市值/股息率 21项",
            ),
        )
        base.append(
            DatasetSpec(
                "akshare_profile",
                "公司资料(akshare)",
                "2",
                "base_sector",
                "2_base_sector/akshare_profile",
                "symbol",
                "akshare 公司资料：行业/董事长/员工数等",
            ),
        )
        base.append(
            DatasetSpec(
                "ccass_top50",
                "CCASS机构持仓",
                "2",
                "base_sector",
                "2_base_sector/ccass_top50",
                "partition",
                "港股CCASS top50机构持股，stock_code 5位",
            ),
        )
        base.append(
            DatasetSpec(
                "hsgt_south",
                "南向资金(港股通)",
                "2",
                "base_sector",
                "2_base_sector/hsgt_south",
                "partition",
                "港股通南向资金持仓，symbol 4位+.HK",
            ),
        )
        base.append(
            DatasetSpec(
                "ah_premium",
                "AH溢价",
                "2",
                "base_sector",
                "2_base_sector/ah_premium",
                "partition",
                "A/H 配对溢价率日截面（A收盘=本地QuantDB，汇率=中行折算价）",
            ),
        )
        base.append(
            DatasetSpec(
                "ah_membership",
                "AH配对清单",
                "2",
                "base_sector",
                "2_base_sector/ah_membership",
                "single",
            ),
        )
        base.append(
            DatasetSpec(
                "hsgt_membership",
                "港股通成分",
                "2",
                "base_sector",
                "2_base_sector/hsgt_membership",
                "single",
            ),
        )
        base.append(
            DatasetSpec(
                "index_weights",
                "指数成分权重",
                "2",
                "base_sector",
                "2_base_sector/index_weights",
                "symbol",
                "中证港股通系列指数成分权重",
            ),
        )
        base.append(
            DatasetSpec(
                "adjust_factors",
                "复权因子",
                "2",
                "base_sector",
                "2_base_sector/adjust_factors",
                "symbol",
                "由付费源昨收推算的复权因子链（daily_backward 用）",
            ),
        )
    if market == "US":
        base.append(
            DatasetSpec(
                "us_universe",
                "标的池(市值Top)",
                "2",
                "base_sector",
                "2_base_sector/us_universe",
                "single",
                "按市值 Top1000 扩容的标的池与新增代码清单",
            ),
        )
    if market in ("HK", "US", "FUTURES", "BC"):
        base.append(
            DatasetSpec(
                "l1_factors",
                "L1因子(日频)",
                "6",
                "ml",
                "6_ml_datasets/l1_factors",
                "partition",
                "本地计算的量价因子日频分区，训练直连读取；随每日同步自动刷新",
            ),
        )
    # 本地信号数据集（南向/CCASS 因子等）由内部模块动态提供；模块缺失时跳过
    try:
        from backend.scripts.build_ml_signal_datasets import SIGNAL_DATASET_SPECS as _sig_specs

        base.extend(DatasetSpec(**spec) for spec in _sig_specs)
    except ModuleNotFoundError as exc:
        if "build_ml_signal_datasets" not in str(exc):
            raise
    return tuple(base)


_GROUPS = [
    {"id": "kline", "name": "K线行情", "category_id": "1"},
    {"id": "base_sector", "name": "基础板块", "category_id": "2"},
    {"id": "financial", "name": "财务数据", "category_id": "3"},
    {"id": "analyst", "name": "分析师/持仓/期权", "category_id": "4"},
    {"id": "technical", "name": "技术衍生", "category_id": "5"},
    {"id": "ml", "name": "ML数据集", "category_id": "6"},
]


class SyncDatasetsRequest(BaseModel):
    datasets: list[str] = Field(..., min_length=1)
    days: int = Field(5, ge=1, le=365, description="同步最近多少个交易日")
    with_qlib: bool = Field(False, description="同步后重建 Qlib 缓存")


class DataSourcesRequest(BaseModel):
    sources: dict[str, bool] = Field(
        ..., description="数据源勾选状态 {source: enabled}"
    )


def _data_dir(env_var: str, default_dir: str) -> Path:
    env_val = os.getenv(env_var, "").strip()
    if env_val:
        return Path(env_val)
    p = Path(default_dir)
    if p.is_dir():
        return p
    if "US" in env_var:
        from backend.services.engine.data_platform.quantus_hub import (
            _resolve_quantus_data_dir,
        )

        return _resolve_quantus_data_dir()
    if "BC" in env_var:
        from backend.services.engine.data_platform.quantbc_hub import (
            _resolve_quantbc_data_dir,
        )

        return _resolve_quantbc_data_dir()
    if "FUTURES" in env_var:
        from backend.services.engine.data_platform.quantfutures_hub import (
            _resolve_quantfutures_data_dir,
        )

        return _resolve_quantfutures_data_dir()
    from backend.services.engine.data_platform.quanthk_hub import (
        _resolve_quanthk_data_dir,
    )

    return _resolve_quanthk_data_dir()


def _partition_dates(root: Path) -> list[str]:
    out = []
    for p in root.iterdir():
        if p.is_dir() and p.name.startswith("dt="):
            out.append(p.name[3:])
    out.sort()
    return out


_DATE_IN_NAME = re.compile(r"(20\d{6})")


def _dataset_dates(spec: DatasetSpec, d: Path) -> list[str]:
    dates = set(_partition_dates(d))
    for f in d.glob("*.parquet"):
        m = _DATE_IN_NAME.search(f.stem)
        if m:
            dates.add(m.group(1))
    return sorted(dates)


def _dataset_stats(spec: DatasetSpec, root: Path) -> dict[str, Any]:
    d = root / spec.rel_dir
    if not d.is_dir():
        return {"synced": False, "files": 0, "size_mb": 0.0}
    files = [f for f in d.rglob("*.parquet") if f.is_file()]
    size_mb = round(sum(f.stat().st_size for f in files) / 1024 / 1024, 1)
    stats: dict[str, Any] = {
        "synced": bool(files),
        "files": len(files),
        "size_mb": size_mb,
    }
    if spec.layout == "partition":
        dates = _dataset_dates(spec, d)
        if dates:
            stats["start_date"] = dates[0]
            stats["end_date"] = dates[-1]
            stats["partitions"] = len(dates)
    if files:
        latest = max(f.stat().st_mtime for f in files)
        stats["updated_at"] = (
            datetime.fromtimestamp(latest, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    return stats


def _json_safe(value: Any) -> Any:
    import numpy as np

    if value is None:
        return None
    if isinstance(value, (np.ndarray, list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (np.integer, int)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, (np.floating, float)):
        f = float(value)
        return None if (f != f or f in (float("inf"), float("-inf"))) else f
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if value is pd.NaT:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (str, bytes)):
        return value.decode("utf-8", "replace") if isinstance(value, bytes) else value
    return str(value)


def _json_safe_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        {str(k): _json_safe(v) for k, v in row.items()}
        for row in df.to_dict(orient="records")
    ]


def _pick_local_file(spec: DatasetSpec, root: Path, symbol: str | None) -> Path | None:
    d = root / spec.rel_dir
    if not d.is_dir():
        return None
    if spec.layout == "partition":
        dates = _partition_dates(d)
        for dt in reversed(dates):
            files = sorted((d / f"dt={dt}").glob("*.parquet"))
            if files:
                return files[0]
        flat = sorted(d.glob("*.parquet"))
        return flat[-1] if flat else None
    files = sorted(f for f in d.glob("*.parquet") if f.is_file())
    if not files:
        return None
    if symbol:
        target = symbol.strip().upper()
        for f in files:
            if f.stem.upper() == target:
                return f
        raise HTTPException(
            status_code=404, detail=f"{spec.dataset} 无 {symbol} 的本地文件"
        )
    return files[0]


def _symbol_choices(spec: DatasetSpec, root: Path, market: str) -> dict[str, Any]:
    if spec.layout != "symbol":
        return {}
    d = root / spec.rel_dir
    if not d.is_dir():
        return {}
    stems = sorted(f.stem for f in d.glob("*.parquet") if f.is_file())
    names: dict[str, str] = {}
    try:
        from backend.scripts.market_cn_names import _read_security_master

        names = {s: n for s, n in _read_security_master(market).items() if s in stems}
    except Exception as exc:  # noqa: BLE001
        logger.warning("security_master 中文名读取失败: %s", exc)
    return {
        "symbol_total": len(stems),
        "symbol_choices": stems[:MAX_SYMBOL_CHOICES],
        "symbol_names": names,
    }


def _build_catalog_payload(market: str, specs: tuple[DatasetSpec, ...], groups: list[dict[str, Any]], root: Path) -> dict[str, Any]:
    """同步组装目录载荷（含全量磁盘扫盘）。

    调用方必须经 asyncio.to_thread 执行，禁止在事件循环线程直接调用，
    否则单 worker 下 /health 会被长时间饿死并触发 watchdog 重启。
    """
    items = []
    for spec in specs:
        items.append(
            {
                "dataset": spec.dataset,
                "name": spec.name,
                "group": spec.group,
                "category_id": spec.category_id,
                "layout": spec.layout,
                "rel_dir": spec.rel_dir,
                "note": spec.note,
                **_dataset_stats(spec, root),
            }
        )
    out_groups = []
    for g in groups:
        members = [it for it in items if it["group"] == g["id"]]
        if not members:
            continue  # 该市场没有此类数据集时不渲染空分组
        out_groups.append(
            {
                **g,
                "dataset_count": len(members),
                "synced_count": sum(1 for it in members if it["synced"]),
                "files": sum(it["files"] for it in members),
                "size_mb": round(sum(it["size_mb"] for it in members), 1),
            }
        )
    return {
        "data_dir": str(root),
        "market": market,
        "groups": out_groups,
        "datasets": items,
        "timestamp": _now_iso(),
    }


def make_market_router(
    *, market: str, env_var: str, default_dir: str, sync_entry: str
) -> APIRouter:
    """生成美股/港股管理路由。

    Args:
        market: US / HK（仅用于标签）
        env_var: 数据目录环境变量（QM_QUANTUS_DATA_DIR / QM_QUANTHK_DATA_DIR）
        default_dir: 容器内默认数据目录（/data/quantus / /data/quanthk）
        sync_entry: 同步脚本路径（backend.scripts.quantus_daily_sync / quanthk_daily_sync）
    """
    router = APIRouter(dependencies=[Depends(require_admin)])  # 路由器级认证兜底
    DATASETS = _default_datasets(market)
    _BY_NAME = {ds.dataset: ds for ds in DATASETS}

    def _spec(dataset: str) -> DatasetSpec:
        spec = _BY_NAME.get(dataset)
        if spec is None:
            raise HTTPException(status_code=400, detail=f"未知数据集: {dataset}")
        return spec

    def _root() -> Path:
        return _data_dir(env_var, default_dir)

    # ------------------------------------------------------------------
    # 目录
    # ------------------------------------------------------------------
    @router.get("/catalog")
    async def get_catalog(current_user: dict = Depends(require_admin)):
        try:
            root = _root()
            payload = await asyncio.to_thread(
                _build_catalog_payload, market, DATASETS, _GROUPS, root
            )
            return {
                "success": True,
                "data": payload,
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("%s catalog failed: %s", market, exc, exc_info=True)
            raise HTTPException(status_code=500, detail=f"failed: {exc}") from exc

    # ------------------------------------------------------------------
    # 预览
    # ------------------------------------------------------------------
    @router.get("/preview")
    async def preview_dataset(
        dataset: str = Query(...),
        symbol: str | None = Query(None),
        limit: int = Query(50, ge=1, le=MAX_PREVIEW_ROWS),
        current_user: dict = Depends(require_admin),
    ):
        spec = _spec(dataset)
        root = _root()
        file_path = _pick_local_file(spec, root, symbol)
        if file_path is None:
            return {
                "success": True,
                "data": {
                    "dataset": dataset,
                    "name": spec.name,
                    "source": "local",
                    "rows_total": 0,
                    "columns": [],
                    "data": [],
                    **_symbol_choices(spec, root, market),
                    "timestamp": _now_iso(),
                },
            }
        try:
            df = pd.read_parquet(file_path)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "%s preview failed (%s): %s", market, dataset, exc, exc_info=True
            )
            raise HTTPException(status_code=500, detail=f"预览失败: {exc}") from exc
        columns = [{"name": str(c), "dtype": str(df[c].dtype)} for c in df.columns]
        records = _json_safe_records(df.head(limit))
        return {
            "success": True,
            "data": {
                "dataset": dataset,
                "name": spec.name,
                "source": "local",
                "file": str(file_path.relative_to(root)),
                "rows_total": int(len(df)),
                "column_count": len(columns),
                "columns": columns,
                "data": records,
                **_symbol_choices(spec, root, market),
                "timestamp": _now_iso(),
            },
        }

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    @router.get("/config")
    async def get_config(current_user: dict = Depends(require_admin)):
        return {
            "success": True,
            "data": {
                "market": market,
                "data_dir": str(_root()),
                "env_var": env_var,
                "sync_entry": sync_entry,
                "timestamp": _now_iso(),
            },
        }

    # ------------------------------------------------------------------
    # 数据源勾选配置
    # ------------------------------------------------------------------
    @router.get("/data-sources")
    async def get_data_sources(current_user: dict = Depends(require_admin)):
        from backend.shared.data_source_config import list_sources

        return {
            "success": True,
            "data": {
                "market": market,
                "sources": list_sources(market),
                "timestamp": _now_iso(),
            },
        }

    @router.post("/data-sources")
    async def save_data_sources(
        payload: DataSourcesRequest, current_user: dict = Depends(require_admin)
    ):
        from backend.shared.data_source_config import save_sources

        saved = save_sources(market, payload.sources)
        return {
            "success": True,
            "data": {
                "market": market,
                "sources": saved,
                "timestamp": _now_iso(),
            },
        }

    # ------------------------------------------------------------------
    # 同步任务
    # ------------------------------------------------------------------
    _jobs: dict[str, dict[str, Any]] = {}
    _jobs_lock = threading.Lock()
    _job_counter = itertools.count(1)
    _prefix = "gm"

    def _job_update(job_id: str, **fields: Any) -> None:
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job is not None:
                job.update(fields)

    def _run_sync_job(job_id: str, req: SyncDatasetsRequest) -> None:
        _job_update(job_id, stage="sync_parquet")
        try:
            # 动态导入市场同步入口
            import importlib

            mod = importlib.import_module(sync_entry)
            kwargs: dict[str, Any] = {"days": req.days, "datasets": list(req.datasets)}
            # BC 市场：勾选分钟数据集时同步对应分钟线
            if market == "BC":
                freq_map = {"min5_kline": "5m", "min1_kline": "1m"}
                minute_freqs = [f for d, f in freq_map.items() if d in req.datasets]
                if minute_freqs:
                    kwargs["minute_freqs"] = tuple(minute_freqs)
                    kwargs["minute_days"] = min(req.days, 90)
            result = mod.run(**kwargs)

            # Phase 2: 同步后重建 Qlib 缓存（勾选 with_qlib 时）
            qlib_cache = None
            if req.with_qlib:
                _job_update(job_id, stage="qlib_cache")
                try:
                    from backend.services.engine.qlib_data_builder import (
                        ensure_qlib_cache,
                    )

                    market_map = {
                        "US": "US",
                        "HK": "HK",
                        "BC": "CRYPTO",
                        "FUTURES": "FUTURES",
                    }
                    qlib_market = market_map.get(market, market)
                    provider_uri = ensure_qlib_cache(market=qlib_market)
                    qlib_cache = {"status": "ok", "provider_uri": provider_uri}
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "%s sync job %s: qlib cache failed: %s",
                        market,
                        job_id,
                        exc,
                        exc_info=True,
                    )
                    qlib_cache = {"status": "error", "reason": str(exc)}

            _job_update(
                job_id,
                status="completed",
                stage="done",
                done=len(req.datasets),
                results=[{"dataset": d, "status": "synced"} for d in req.datasets],
                summary=result,
                qlib_cache=qlib_cache,
                finished_at=_now_iso(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "%s sync job %s failed: %s", market, job_id, exc, exc_info=True
            )
            _job_update(job_id, status="failed", error=str(exc), finished_at=_now_iso())

    @router.post("/sync-datasets")
    async def sync_datasets(
        payload: SyncDatasetsRequest, current_user: dict = Depends(require_admin)
    ):
        for name in payload.datasets:
            _spec(name)
        # CCASS 抓取任务互斥：同时跑两个会相互触发 HKEX 封禁，直接拒绝
        if market == "HK" and "ccass_top50" in payload.datasets:
            with _jobs_lock:
                running_ccass = any(
                    j["status"] == "running" and "ccass_top50" in j["datasets"]
                    for j in _jobs.values()
                )
            if running_ccass:
                raise HTTPException(
                    status_code=409,
                    detail="已有 CCASS 同步任务运行中，请等待完成后再触发",
                )
        job_id = f"{_prefix}-{next(_job_counter)}"
        job = {
            "job_id": job_id,
            "status": "running",
            "stage": "sync_parquet",
            "datasets": list(payload.datasets),
            "days": payload.days,
            "total": len(payload.datasets),
            "done": 0,
            "results": [],
            "qlib_cache": None,
            "cancel_requested": False,
            "started_at": _now_iso(),
            "started_by": current_user.get("username") or current_user.get("user_id"),
        }
        with _jobs_lock:
            _jobs[job_id] = job
            for stale in sorted(_jobs)[:-MAX_JOB_HISTORY]:
                if _jobs[stale]["status"] != "running":
                    _jobs.pop(stale, None)
        threading.Thread(
            target=_run_sync_job, args=(job_id, payload), daemon=True
        ).start()
        return {"success": True, "data": {"job": job}}

    @router.get("/sync-jobs")
    async def list_sync_jobs(current_user: dict = Depends(require_admin)):
        with _jobs_lock:
            jobs = [dict(j) for j in _jobs.values()]
        jobs.sort(key=lambda j: j["started_at"], reverse=True)
        return {"success": True, "data": {"jobs": jobs, "timestamp": _now_iso()}}

    @router.get("/sync-jobs/{job_id}")
    async def get_sync_job(job_id: str, current_user: dict = Depends(require_admin)):
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}")
            return {"success": True, "data": {"job": dict(job)}}

    @router.post("/sync-jobs/{job_id}/cancel")
    async def cancel_sync_job(job_id: str, current_user: dict = Depends(require_admin)):
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}")
            if job["status"] != "running":
                raise HTTPException(
                    status_code=400, detail=f"任务状态为 {job['status']}，无法取消"
                )
            job["cancel_requested"] = True
        return {
            "success": True,
            "data": {
                "job_id": job_id,
                "status": "cancelling",
                "message": "取消信号已发送，当前数据集完成后将停止",
            },
        }

    return router
