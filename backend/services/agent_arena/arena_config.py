"""arena 数据面的根与配置——QuantMind 原生落点。

与隔壁 `backend/config.py + config/backend.yaml` 的关系
------------------------------------------------------
隔壁的配置分两份：`config/backend.yaml`（真配置：data.root_dir / markets /
trading.fees）与 `data/config.yaml`（前端遗留，且已损坏不可解析）。`config/`
是券商私钥目录、**永不过迁移闸**，所以这里不做「读隔壁 yaml」，而是把
`backend.yaml` 里 arena 数据面实际用到的段**固化成本模块**（值取自
2026-09-29 的 `config/backend.yaml`，逐字段核对）。要改市场映射/费率时改这里，
别再散回 yaml——单一出处。

数据根判据（为什么不是「有 /data 就用 /data」）
----------------------------------------------
宿主的 `/data` 是**另一个无关遗留目录**（内含 quantdb/work，实测 2026-09-29），
容器里 `/app/data` 是**空目录**（compose 只挂 `/data`）——两个朴素判据都会选错。
真正的判据是 A1 落盘时留下的**哨兵文件**：`arena/quanttrader/MANIFEST.jsonl`
只有**真数据根**才有。候选顺序：显式 env（`QM_ARENA_DATA_ROOT`，测试/换机用）
→ `<repo>/data`（宿主，符号链接到真实数据盘）→ `/data`（容器挂载点）→
都无哨兵时按 `/.dockerenv` 回退。既有宿主又有容器时，哨兵保证两边选中同一份。

arena 可写状态（comp-config / broker-market / trade 缓存）落在
`<root>/arena/` 下——那儿本来就是 A1 为 arena 划出的命名空间。
"""

from __future__ import annotations

import os
from pathlib import Path

#: 仓库根（本文件在 backend/services/agent_arena/ 下）
PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: 真数据根才有的哨兵（A1 落盘封印）
_DATA_ROOT_SENTINEL = Path("arena") / "quanttrader" / "MANIFEST.jsonl"


def _looks_like_data_root(path: Path) -> bool:
    return (path / _DATA_ROOT_SENTINEL).is_file()


def data_root() -> Path:
    """arena 数据根（判定顺序见模块 docstring）。"""
    env = (os.getenv("QM_ARENA_DATA_ROOT") or "").strip()
    if env:
        return Path(env)
    for candidate in (PROJECT_ROOT / "data", Path("/data")):
        if _looks_like_data_root(candidate):
            return candidate
    # 未落盘（公开仓/全新机器）：容器里 /data 就是约定落点，宿主回退仓库 data/
    if Path("/.dockerenv").exists():
        return Path("/data")
    return PROJECT_ROOT / "data"


def arena_dir() -> Path:
    """arena 可写状态目录（comp-config / broker-market / trade 缓存）。"""
    return data_root() / "arena"


def logs_dir() -> Path:
    """arena 台账日志目录（live_ledger.json / live_trade_*.jsonl 等，A1 已落）。"""
    return data_root() / "logs"


# 落点一律走**函数**而不是模块级常量：常量会在 import 时固化路径，
# `QM_ARENA_DATA_ROOT` 在进程内切换（测试夹具最典型）就会失效且打到真实落点。
def comp_config_file() -> Path:
    """比赛配置落点（A1 已把隔壁 configs/comp-config.json 落到此处；分析引擎侧同读）。"""
    return arena_dir() / "comp-config.json"


def broker_market_file() -> Path:
    """总控「市场→券商」映射（隔壁在 config/ 私有区，QM 落点到 arena 命名空间）。"""
    return arena_dir() / "broker_market.json"


def trade_cache_file() -> Path:
    """position.jsonl 的 SQLite 查询缓存。

    隔壁写在数据根**父目录**（容器里就是 `/`），移植时改到 arena 目录；
    原逻辑只是缓存，换位置无行为差。
    """
    return arena_dir() / "trade_cache.sqlite"


def load_arena_config() -> dict:
    """arena 配置（形状与隔壁 `backend.yaml` 对齐，agent_data 等模块按它取值）。

    值来源：quant-Trader `config/backend.yaml`（2026-09-29 快照）——
    `data.root_dir` 改成 QM 数据根（下面 `get_data_root` 处理），市场映射、
    费率、api 段逐字段照抄（费率即 [[fee-two-rates-broker-vs-matching]] 里
    撮合口径的万 3 + 千 0.5 滑点，改动须过金样）。
    """
    return {
        "data": {"root_dir": str(data_root())},
        "markets": {
            "us": {
                "name": "美股市场（纳斯达克100）",
                "enabled": True,
                "data_dir": "agent_data",
                "benchmark_file": "Adaily_prices_QQQ.json",
                "currency": "USD",
            },
            "cn": {
                "name": "A股市场（上证50）",
                "enabled": True,
                "data_dir": "agent_data_astock",
                "benchmark_file": "A_stock/index_daily_sse_50.json",
                "currency": "CNY",
            },
            "hk": {
                "name": "港股市场（恒生指数）",
                "enabled": True,
                "data_dir": "agent_data_hk",
                "benchmark_file": "hsi_daily.json",
                "currency": "HKD",
            },
        },
        "trading": {
            "fees": {"buy_rate": 0.0003, "sell_rate": 0.0003, "slippage": 0.0005}
        },
        "api": {"refresh_interval": 5, "max_equity_points": 500},
    }


def get_data_root(config: dict) -> Path:
    """解析数据根（与隔壁同名函数同构；相对路径按仓库根解析）。"""
    root = config.get("data", {}).get("root_dir", "./data")
    path = Path(root)
    return path if path.is_absolute() else PROJECT_ROOT / path


def get_enabled_markets(config: dict) -> list[str]:
    return [
        name
        for name, cfg in config.get("markets", {}).items()
        if cfg.get("enabled", True)
    ]
