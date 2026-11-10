"""因子研究 —— 快照（artifact）读取层（支持多数据集 × 市场）。

数据集：
- ``classic``（默认）：demo 复刻 82 因子，产物由 ``build_factor_research.py`` 写
  ``<market_root>/factor_research/``（长表打分 + 经典目录 catalog.py）；
  **A 股专属**（demo 复刻仅 CN 构建），非 CN 请求抛 ``MarketUnsupported``。
- ``private``：筛选最终保留 327 因子，产物由 ``build_factor_panel_private.py`` 写
  ``<market_root>/factor_research_private/``（宽表打分 + factors.json 目录）。

市场分层（2026-10-16）：全部函数带 ``market``（缺省 ``"CN"``，零行为变化）。
目录一律经 ``resolve_market_root(market)`` 解析 —— **不允许**只按数据集名拼路径，
否则非 CN 会静默读到 CN 的快照。市场键经 ``validate_market`` 严格校验（未知市场
抛错，绝不落 CN）。

本层只读 + 短 TTL 缓存，不触发计算。
"""

from __future__ import annotations

import copy
import json
import math
import threading
import time
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

from backend.services.engine.factor_research import windows as frwindows

TTL_SECONDS = 600  # 快照读取缓存（构建脚本重跑后 10 分钟内自动失效）
_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()
# Panel 是窗口化后最大的对象（10 年 private ≈1.4G/份）；不能像元数据那样无限留。
# 只留最近 2 个窗口，服务层 _ctx_cache 另有 4 份上限，二者相加即内存上界。
_panel_cache: dict[str, tuple[float, object]] = {}
_MAX_PANELS = 2

# 数据集 → 产物目录名
DATASET_DIRS = {"classic": "factor_research", "private": "factor_research_private"}

# 合法数据集取值的唯一出处（须与 DATASET_DIRS 同步，有测试锁死）。
# 对外接口一律用它收口：dataset 会经 artifact_dir 拼进文件系统路径。
FactorDataset = Literal["classic", "private"]


class SnapshotMissing(FileNotFoundError):
    """快照产物不存在（没建过 / 正在重建）。

    单独成类型，是因为路由要靠它把「去点一键计算」这条路指对。原先路由按
    文案里的「缺失」二字判（`_snapshot_guard`），而**数据目录缺失**（6_ml_datasets
    未挂载/未生成）的报错也含这两个字，会被一并翻译成「请点快照-一键计算」——
    用户点了也只是白等一次几分钟的重建，真正的原因（数据没挂上）一个字都没提。
    """


class DedupMatrixMissing(RuntimeError):
    """去重相关矩阵缺失（private 未跑 build_factor_panel_corr.py / classic 无 corr.parquet）。

    与 ``SnapshotMissing`` 分开的原因同前：这不是「点快照-一键计算」能修的，
    路由据此给**各自**的重建指引（409），不把用户指向错误的按钮。
    """


class MarketUnsupported(ValueError):
    """该数据集在某市场不供给（classic 经典库为 A 股专属 demo 复刻）。

    独立成类型（而非文案判）：路由据此译 **422**（请求本身对，市场×数据集组合
    不供给），与「未知数据集」（400）和「快照缺失」（503）三条指引互不串门。
    继承 ``ValueError`` 以便既有捕获 ValueError 的调用面不炸，但路由须先判本类。
    """


def _sanitize(o):
    """NaN/Inf → None（快照 JSON 由 pandas 写出，可能含非有限值；FastAPI 序列化会 500）。"""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _sanitize(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_sanitize(v) for v in o]
    return o


def artifact_dir(dataset: str = "classic", market: str = "CN") -> Path:
    """数据集 × 市场 → 产物目录。**fail-closed**：未知 dataset/market 直接抛错。

    这是对外参数进入文件系统路径的唯一入口，原先写的是
    ``DATASET_DIRS.get(dataset, dataset)`` —— 未命中的取值被原样当路径组件，
    而 ``Path`` 保留 ``..``（由内核在 ``open()`` 时解析），于是
    ``?dataset=../../../../etc`` 就能读出库外文件；``POST /build`` 还会
    ``mkdir(parents=True)`` 在库外建目录。合法取值只有 ``DATASET_DIRS`` 的键。

    市场侧同款 fail-closed：``validate_market`` 对未知市场抛错（绝不落 CN），
    ``classic`` 为 A 股专属（非 CN 抛 ``MarketUnsupported`` → 路由 422），
    根解析走 ``resolve_market_root`` 唯一入口（CN 即原 ``resolve_quantdb_dir()``，
    零行为变化）。
    """
    from backend.shared.feature_source import resolve_market_root, validate_market

    try:
        name = DATASET_DIRS[dataset]
    except KeyError:
        raise ValueError(
            f"未知因子数据集 {dataset!r}（合法取值：{'、'.join(sorted(DATASET_DIRS))}）"
        ) from None
    mk = validate_market(market)
    if mk != "CN" and dataset == "classic":
        raise MarketUnsupported(
            f"经典因子库（classic）为 A 股专属 demo 复刻（build_factor_research.py），"
            f"{mk} 市场不供给；本市场请用「私人因子库」数据集"
        )
    return resolve_market_root(mk) / name


def _cached_obj(key: str, loader):
    """按 key 的 TTL 缓存读取（key 需含文件路径与过滤参数）。"""
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < TTL_SECONDS:
            return hit[1]
    obj = loader()
    with _lock:
        _cache[key] = (now, obj)
    return obj


def _panel_cached(key: str, loader):
    """Panel 专用 LRU（最多 2 个窗口）；窗口化后不能沿用无界元数据缓存。"""
    now = time.time()
    with _lock:
        hit = _panel_cache.get(key)
        if hit and now - hit[0] < TTL_SECONDS:
            return hit[1]
    obj = loader()
    with _lock:
        while len(_panel_cache) >= _MAX_PANELS:
            _panel_cache.pop(next(iter(_panel_cache)))
        _panel_cache[key] = (now, obj)
    return obj


def _file_key(p: Path, prefix: str, extra: str = "") -> str:
    """缓存 key = 路径 + mtime + 参数 —— 构建脚本重跑后旧缓存立即失效（不受 TTL 限制）。"""
    try:
        mtime = p.stat().st_mtime_ns
    except OSError:
        mtime = 0
    return f"{prefix}:{p}:{mtime}:{extra}"


def panel_stamp(
    dataset: str = "classic",
    market: str = "CN",
    start: str | None = None,
    end: str | None = None,
) -> str:
    """面板快照标识（目录/文件路径 + mtime + 窗口）。

    给上层当缓存键用：重建脚本一落盘，键就变，上层那份重算几十秒的上下文自动作废 ——
    不需要再靠 TTL 猜什么时候过期。窗口进键：同一份主库切 2016–2019 与 2020–2024
    是两个 Panel，不能互相命中。
    """
    root = artifact_dir(dataset, market)
    files = frwindows.year_files(root / frwindows.PANEL_DIR, start, end)
    if files:
        return "obj:" + frwindows.files_stamp(files, f"{start or ''}|{end or ''}")
    return _file_key(
        root / "factor_panel.parquet", "obj", f"{start or ''}|{end or ''}"
    )


def window_meta(dataset: str = "classic", market: str = "CN") -> dict:
    """快照的窗口/方向/覆盖率元信息（factors.json 或 metrics.json 的 meta 字段）。"""
    if dataset == "private":
        f = factors_meta("private", market) or {}
        return f.get("meta", {}) or {}
    m = metrics(dataset, market) or {}
    return m.get("meta", {}) or {}


def coverage(dataset: str = "classic", market: str = "CN") -> dict | None:
    """逐月/逐因子的覆盖率审计（coverage.json；无分区旧快照可能为 None）。"""
    return load_json(frwindows.COVERAGE_FILE, dataset, market)


def load_json(name: str, dataset: str = "classic", market: str = "CN"):
    p = artifact_dir(dataset, market) / name

    def _rd():
        if not p.exists():
            return None
        return _sanitize(json.loads(p.read_text(encoding="utf-8")))

    return _cached_obj(_file_key(p, "json"), _rd)


def load_parquet(
    name: str, dataset: str = "classic", market: str = "CN", **kwargs
) -> pd.DataFrame | None:
    p = artifact_dir(dataset, market) / name

    def _rd():
        if not p.exists():
            return None
        return pd.read_parquet(p, **kwargs)

    return _cached_obj(_file_key(p, "pq", repr(sorted(kwargs.items(), key=str))), _rd)


def metrics(dataset: str = "classic", market: str = "CN") -> dict:
    return load_json("metrics.json", dataset, market) or {
        "leaderboard": [],
        "metrics": {},
        "meta": {},
    }


def factors_meta(dataset: str = "private", market: str = "CN") -> dict | None:
    """私人因子库目录（factors.json：factors/l1_order/l2_order/meta）。"""
    return load_json("factors.json", dataset, market)


def holdings(market: str = "CN") -> dict:
    return load_json("holdings_latest.json", "classic", market) or {
        "date": None,
        "holdings": {},
    }


def ic_table(dataset: str = "classic", market: str = "CN") -> pd.DataFrame | None:
    return load_parquet("ic.parquet", dataset, market)


def corr_table(dataset: str = "classic", market: str = "CN") -> pd.DataFrame | None:
    return load_parquet("corr.parquet", dataset, market)


def dedup_corr(
    dataset: str = "classic", market: str = "CN"
) -> tuple[list[str], np.ndarray] | None:
    """去重相关矩阵 ``(names, 方阵)``；产物缺失返回 None（调用方抛 DedupMatrixMissing）。

    classic：``corr.parquet`` 长表（build_factor_research 生成；覆盖 73/82 名字，
    未覆盖者去重时按覆盖回退必保留）；
    private：``cross_corr.npz``（``backend/scripts/build_factor_panel_corr.py``
    生成，全量因子；面板重建后须重跑该脚本）。
    """
    from backend.services.engine.factor_research import dedup as dedup_lib

    if dataset == "classic":
        df = corr_table(dataset, market)
        if df is None or len(df) == 0:
            return None
        return dedup_lib.matrix_from_pairs(df)
    p = artifact_dir(dataset, market) / "cross_corr.npz"

    def _rd():
        if not p.exists():
            return None
        with np.load(p, allow_pickle=False) as z:
            return [str(s) for s in z["names"]], np.asarray(z["matrix"])

    return _cached_obj(_file_key(p, "npz"), _rd)


def benchmark_table(
    dataset: str = "classic", market: str = "CN"
) -> pd.DataFrame | None:
    """基准净值长表（trade_date, nav, index_code）。CN=沪深300/中证800/中证500；
    非 CN 基准代码由各市场私有面板构建器写入（HK=HSI.HK 等，FUTURES 为空表）。"""
    return load_parquet("benchmarks.parquet", dataset, market)


def panel(
    dataset: str = "classic",
    market: str = "CN",
    start: str | None = None,
    end: str | None = None,
):
    """月末名次面板（scorecard.Panel；进程内缓存，按窗口）。

    优先读 ``factor_panel/year=YYYY.parquet``（只读与请求区间相交的年份）；
    旧快照没有年分区时回退整份 ``factor_panel.parquet`` 再按日期裁剪。
    """
    from backend.services.engine.factor_research import scorecard

    root = artifact_dir(dataset, market)
    part_dir = root / frwindows.PANEL_DIR
    miss = frwindows.missing_years(part_dir, start, end)
    if miss:
        raise SnapshotMissing(
            f"年分区不完整：缺 {miss}（构建中断？请重建或回退整份快照）"
        )
    files = frwindows.year_files(part_dir, start, end)
    if files:
        key = "obj:" + frwindows.files_stamp(files, f"{start or ''}|{end or ''}")

        def _rd_parts():
            df = pd.concat([pd.read_parquet(p) for p in files], ignore_index=True)
            df = frwindows.clip_dates(df, "trade_date", start, end)
            if df.empty:
                return None  # 请求区间落在快照窗口外
            return scorecard.Panel(df)

        return _panel_cached(key, _rd_parts)

    p = root / "factor_panel.parquet"

    def _rd():
        if not p.exists():
            return None
        df = pd.read_parquet(p)
        df = frwindows.clip_dates(df, "trade_date", start, end)
        if df.empty:
            return None  # 请求区间落在快照窗口外
        return scorecard.Panel(df)

    return _panel_cached(
        _file_key(p, "obj", f"{start or ''}|{end or ''}"), _rd
    )


def stock_snapshot(market: str = "CN", as_of=None) -> pd.DataFrame | None:
    """个股截面元数据（名称/行业/市值/PE/PB/近一年日均成交额）。

    ``as_of`` 给了就优先读 ``stock_snapshot/year=YYYY.parquet``（历史窗口的
    持仓画像必须用当期截面，不能拿今天的最新快照回看 2016）；没有年分区/该年
    缺失时回退最新整份。数据集无关（股票池元数据同一份）：CN 优先经典目录。
    """
    from backend.shared.feature_source import validate_market

    mk = validate_market(market)

    def _from_years(root: Path) -> pd.DataFrame | None:
        d = root / frwindows.SNAPSHOT_DIR
        files = sorted(d.glob("year=*.parquet"))
        if not files:
            return None
        if as_of is not None:
            want = f"year={int(pd.Timestamp(as_of).year)}.parquet"
            hit = [p for p in files if p.name == want]
            if hit:
                return _cached_obj(_file_key(hit[0], "pq", "snap"), lambda: pd.read_parquet(hit[0]))
        latest = files[-1]
        return _cached_obj(_file_key(latest, "pq", "snap"), lambda: pd.read_parquet(latest))

    if mk == "CN":
        snap = _from_years(artifact_dir("classic", "CN"))
        if snap is not None:
            return snap
        df = load_parquet("stock_snapshot.parquet", "classic", "CN")
        if df is not None:
            return df
    snap = _from_years(artifact_dir("private", mk))
    if snap is not None:
        return snap
    return load_parquet("stock_snapshot.parquet", "private", mk)


def scores_for(
    codes: list[str],
    dataset: str = "classic",
    market: str = "CN",
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame | None:
    """按因子读取月末打分（长表：trade_date/symbol/factor_code/score）。

    优先读 ``monthly_scores/year=YYYY.parquet``（只取相交年份）；旧快照回退整份。
    private 年分区是宽表（按列裁剪 + melt），classic 是长表（factor_code 过滤）。
    """
    codes = list(codes)
    root = artifact_dir(dataset, market)
    miss = frwindows.missing_years(root / frwindows.SCORES_DIR, start, end)
    if miss:
        raise SnapshotMissing(
            f"打分年分区不完整：缺 {miss}（构建中断？请重建或回退整份快照）"
        )
    part_files = frwindows.year_files(root / frwindows.SCORES_DIR, start, end)
    if dataset != "private":
        if part_files:
            frames = []
            for f in part_files:
                df = pd.read_parquet(
                    f, filters=[("factor_code", "in", codes)]
                )
                if not df.empty:
                    frames.append(
                        frwindows.clip_dates(df, "trade_date", start, end)
                    )
            if not frames:
                return pd.DataFrame()
            return pd.concat(frames, ignore_index=True)
        return load_parquet(
            "monthly_scores.parquet",
            dataset,
            market,
            filters=[("factor_code", "in", codes)],
        )

    if part_files:
        import pyarrow.parquet as pq

        frames = []
        for f in part_files:
            names = set(pq.ParquetFile(f).schema.names)
            cols = [c for c in codes if c in names]
            if not cols:
                continue
            df = pd.read_parquet(f, columns=["trade_date", "symbol", *cols])
            df = frwindows.clip_dates(df, "trade_date", start, end)
            if df.empty:
                continue
            frames.append(
                df.melt(
                    id_vars=["trade_date", "symbol"],
                    value_vars=cols,
                    var_name="factor_code",
                    value_name="score",
                ).dropna(subset=["score"])
            )
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    p = root / "monthly_scores.parquet"
    if not p.exists():
        return None
    import pyarrow.parquet as pq

    names = set(pq.ParquetFile(p).schema.names)
    cols = [c for c in codes if c in names]
    if not cols:
        return pd.DataFrame()
    df = pd.read_parquet(p, columns=["trade_date", "symbol", *cols])
    df = frwindows.clip_dates(df, "trade_date", start, end)
    return df.melt(
        id_vars=["trade_date", "symbol"],
        value_vars=cols,
        var_name="factor_code",
        value_name="score",
    ).dropna(subset=["score"])


def fwd_returns(dataset: str = "classic", market: str = "CN") -> pd.DataFrame | None:
    return load_parquet("fwd_returns.parquet", dataset, market)


_EMPTY_SCREENING: dict = {
    "counts": {
        "candidates": 0,
        "kept": 0,
        "gated_out": 0,
        "deduped": 0,
        "total_considered": 0,
    },
    "kept": [],
    "dropped_gated": [],
    "dropped_duplicate": [],
    "gates": {},
    "cross_corr": "",
    "generated_at": None,
}


def screening(market: str = "CN") -> dict:
    """因子筛选结果（CN 七库联合筛选产物；缺失时返回空结构）。

    非 CN 诚实缺席（该产品为 A 股口径的七库筛选，别的市场不供给）：返回同构
    空结构 + ``available: false`` + ``reason`` —— 前端据此渲染阻断面而不是
    「暂未筛选」（后者会让人等一个永远不会跑的构建）。
    """
    from backend.shared.feature_source import validate_market

    mk = validate_market(market)
    if mk != "CN":
        return {
            **copy.deepcopy(_EMPTY_SCREENING),
            "available": False,
            "market": mk,
            "reason": (
                f"联合筛选为 A 股口径产物（七库筛选），{mk} 市场暂未接入；"
                "可用排行榜的 ICIR/覆盖率门槛做同类过滤"
            ),
        }
    # CN 存量行为逐字不变（含缺产物时的空结构形状）
    return load_json("screening/factor_selection.json", "classic", mk) or copy.deepcopy(
        _EMPTY_SCREENING
    )


def clear_cache() -> None:
    with _lock:
        _cache.clear()
