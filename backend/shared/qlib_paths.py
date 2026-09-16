"""
Qlib 数据路径统一解析
=====================
所有需要 Qlib provider_uri 的地方应通过本模块获取，避免硬编码 db/qlib_data。

优先级：
1. 环境变量 QLIB_PROVIDER_URI（显式覆盖，对全部市场生效，单容器部署建议只在调试时用）
2. /data/qlib/{market}_data（统一固定目录，QlibDataBuilder 的默认写入目标）
3. 各市场 .qlib_cache、/data/qlib_data/*、/app/db/qlib_data*（旧路径，兼容回退）
4. 项目相对路径 db/qlib_data（开发环境回退）

所有从请求/配置/常量拿到 provider_uri 的调用方，都应再经
``normalize_qlib_provider_uri`` 走一遍：客户端与旧配置里钉死的 ``/app/db/qlib_data``
是历史容器路径，若不归一化，会出现「夜间任务写 A 目录、回测读 B 目录」的分裂缓存。
"""

from __future__ import annotations

import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 各市场本地 parquet 数据目录（与 docker-compose 的 QM_QUANT*_DATA_DIR 对齐）
_MARKET_DATA_DIR: dict[str, str] = {
    "US": os.getenv("QM_QUANTUS_DATA_DIR", "/data/quantus"),
    "HK": os.getenv("QM_QUANTHK_DATA_DIR", "/data/quanthk"),
    "CRYPTO": os.getenv("QM_QUANTBC_DATA_DIR", "/data/quantbc"),
    "FUTURES": os.getenv("QM_QUANTFUTURES_DATA_DIR", "/data/quantfutures"),
}


# 历史遗留的 A 股容器路径：容器把仓库 ./db 挂在 /app/db，早期版本的默认值把
# 它当成了唯一缓存目录，并在其后拼上 /cn_data（该子目录其实从未存在）。
_LEGACY_CN_PREFIXES = ("/app/db/qlib_data", "db/qlib_data")


def is_qlib_provider_ready(provider_uri: str | Path) -> bool:
    """Return whether *provider_uri* contains the minimum day-frequency Qlib layout.

    A cache directory can be created before its calendar/instruments are written.
    Treating that directory as a valid provider makes Qlib fail much later while
    constructing an Exchange, with the misleading ``does not contain data for
    day`` error.
    """
    provider = Path(provider_uri).expanduser()
    return (
        provider.is_dir()
        and (provider / "calendars" / "day.txt").is_file()
        and (provider / "instruments" / "all.txt").is_file()
        and (provider / "features").is_dir()
    )


def resolve_qlib_provider_uri(market: str = "CN") -> str:
    """返回 Qlib provider_uri 绝对路径。

    market: "CN", "HK", "US", "CRYPTO" — 仅 CN 走 QuantDB 路径，
    其他市场仍使用 db/qlib_data/{market}_data。
    """
    env_val = os.getenv("QLIB_PROVIDER_URI", "").strip()
    if env_val:
        return env_val

    market_upper = market.upper()

    # 非 A 股市场：固定子目录
    _MARKET_SUBDIR: dict[str, str] = {
        "HK": "hk_data",
        "US": "us_data",
        "CRYPTO": "bc_data",
        "FUTURES": "futures_data",
    }
    # 各市场 .qlib_cache 缓存子目录名（QlibDataBuilder.for_market 生成）
    _CACHE_SUBDIR: dict[str, str] = {
        "HK": "hk_data",
        "US": "us_data",
        "CRYPTO": "bc_data",
        "FUTURES": "futures_data",
    }
    if market_upper in _MARKET_SUBDIR:
        subdir = _MARKET_SUBDIR[market_upper]
        # 统一固定目录优先（/data/qlib/{subdir}），其次各市场 .qlib_cache（历史遗留）
        fixed = Path(f"/data/qlib/{subdir}")
        if is_qlib_provider_ready(fixed):
            return str(fixed)
        market_data_dir = _MARKET_DATA_DIR.get(market_upper)
        if market_data_dir:
            cache_sub = _CACHE_SUBDIR.get(market_upper, subdir)
            cache_candidate = Path(market_data_dir) / ".qlib_cache" / cache_sub
            if is_qlib_provider_ready(cache_candidate):
                return str(cache_candidate)
        for candidate in (
            Path(f"/data/qlib_data/{subdir}"),
            Path(f"/app/db/qlib_data/{subdir}"),
            _PROJECT_ROOT / "db" / "qlib_data" / subdir,
        ):
            if is_qlib_provider_ready(candidate):
                return str(candidate)
        return str(_PROJECT_ROOT / "db" / "qlib_data" / subdir)

    # A 股 (CN)：优先固定目录（便于维护），其次 QuantDB 缓存路径
    for candidate in (
        Path("/data/qlib/cn_data"),
        Path("/data/quantdb/.qlib_cache/cn_data"),
        _PROJECT_ROOT / "data" / "quantdb" / ".qlib_cache" / "cn_data",
        Path("/app/db/qlib_data"),
        _PROJECT_ROOT / "db" / "qlib_data",
    ):
        if is_qlib_provider_ready(candidate):
            return str(candidate)

    return str(_PROJECT_ROOT / "db" / "qlib_data")


def normalize_qlib_provider_uri(provider_uri: str | None, market: str = "CN") -> str:
    """把调用方给的 provider_uri 归一到系统实际解析出的缓存目录。

    - 空值：直接返回 resolve_qlib_provider_uri(market)。
    - 旧容器路径（/app/db/qlib_data 及其 /cn_data 变体）：只要系统解析出的目录
      已就绪就改指它，避免同一份数据存在两套缓存、写入与读取分裂。
    - 其它显式路径（例如用户自带的 ~/.qlib 数据包）：原样保留，只做相对路径展开。
    """
    raw = str(provider_uri or "").strip()
    if not raw:
        return resolve_qlib_provider_uri(market)

    expanded = str(Path(raw).expanduser())
    if not raw.startswith(("/", "~", ".")):
        expanded = str(_PROJECT_ROOT / raw)

    norm = expanded.replace("\\", "/").rstrip("/")
    if not norm.startswith("/"):
        return expanded
    legacy = {p.rstrip("/") for p in _LEGACY_CN_PREFIXES}
    legacy |= {f"{p}/cn_data" for p in _LEGACY_CN_PREFIXES}
    if norm not in legacy:
        return expanded

    canonical = resolve_qlib_provider_uri(market)
    if Path(canonical) == Path(expanded):
        return expanded
    if is_qlib_provider_ready(canonical):
        return canonical
    # 系统解析出的目录还没建好（新装/迁移中），继续沿用旧路径别把回测打断
    return expanded


def resolve_qlib_data_dir(market: str = "CN") -> str:
    """resolve_qlib_provider_uri 的别名，语义更清晰。"""
    return resolve_qlib_provider_uri(market=market)


_PATH_MARKET_HINTS = {
    "cn_data": "CN",
    "hk_data": "HK",
    "us_data": "US",
    "bc_data": "CRYPTO",
    "futures_data": "FUTURES",
}


def guess_market_from_provider_uri(provider_uri: str | None) -> str:
    """从 provider_uri 路径片段猜测市场，未知按 CN。"""
    lowered = str(provider_uri or "").lower()
    for hint, market in _PATH_MARKET_HINTS.items():
        if hint in lowered:
            return market
    return "CN"


def fallback_to_ready_provider_uri(
    provider_uri: str | None, market: str | None = None
) -> str:
    """归一 provider_uri 并保证读取端可用：

    1. 先走 normalize_qlib_provider_uri（旧容器路径 → 规范目录）。
    2. 若结果目录未就绪（新部署常见：数据实际落在另一处），回退到
       resolve_qlib_provider_uri 解析出的就绪目录。
    3. 两者都未就绪则原样返回，让调用方报出真实缺失路径。
    """
    market = (market or guess_market_from_provider_uri(provider_uri)).upper()
    candidate = normalize_qlib_provider_uri(provider_uri, market=market)
    if is_qlib_provider_ready(candidate):
        return candidate
    resolved = resolve_qlib_provider_uri(market)
    if is_qlib_provider_ready(resolved):
        return resolved
    return candidate


def resolve_qlib_calendar_path(market: str = "CN") -> Path:
    """返回 Qlib 交易日历文件路径 (calendars/day.txt)。"""
    return Path(resolve_qlib_provider_uri(market=market)) / "calendars" / "day.txt"


def resolve_qlib_instruments_path(market: str = "CN") -> Path:
    """返回 Qlib instruments 文件路径 (instruments/all.txt)。"""
    return Path(resolve_qlib_provider_uri(market=market)) / "instruments" / "all.txt"
