"""训练目录页 per-feature 统计聚合 —— /fields 响应内嵌 ``stats`` 的唯一实现。

数据源与口径（优先级从高到低）：
1. 因子报告快照 ``<数据集>/report/factor_report.json``（日频评估口径，l1_factors 是
   110 因子 × 2359 日；factor_defs 快照 14MB——**只抽取精简索引缓存，绝不整包驻留**）；
2. 私域快照 ``factor_research_private/factors.json``（研究口径：82 采样日、方向已统一
   为「越大越好」——IC 与报告口径**不可比**，命中必须带 ``source="research_snapshot"``
   徽标，其余统计键一律 None）；
3. 都无 → None（前端渲染「—」，**绝不伪造 0**）。

设计约束：
- 只读 + mtime/TTL 缓存（模式镜像 ``factor_research/store.py`` 的 ``_cached_obj`` /
  ``_file_key``：构建脚本重跑后旧缓存随 mtime 立即失效，不必等 TTL）；
- 轻依赖：仅 stdlib + ``datasets`` 注册表 + ``quantdb_paths``（**不拖 numpy/pandas**，
  api 进程内可直接调用）；
- ``build_fields_stats`` 承诺**绝不抛**：它挂在 /fields 响应里，统计层故障不得把
  字段列表本身拖垮；
- 数值守卫 ``_num`` / ``_int``：NaN/Inf/bool/字符串 → None——既拦脏快照，也防
  ``json.dump`` 把 NaN 原样写出后 Starlette（``allow_nan=False``）在响应层 500。
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from backend.shared.quantdb_paths import resolve_quantdb_dir

from .datasets import DATASETS, dataset_dir

log = logging.getLogger(__name__)

TTL_SECONDS = 600  # 与 factor_research/store.py 同拍
STALE_DAYS = 30  # 快照生成时间距今超过该天数 → stale 提示（页面不把它当新鲜数据）
FALLBACK_DIR = "factor_research_private"
FALLBACK_FILE = "factors.json"

# 报告 item → stats 载荷的标量键（口径=因子报告默认 horizon fwd_ret_5）
_FLOAT_KEYS = (
    "ic_mean",
    "icir",
    "t_value",
    "turnover",
    "monotonicity",
    "win_rate",
    "n_valid_mean",
)

# normalize_market 的 CN 别名（router 调用前已归一，这里只做兜底；不 import
# quantdb_factor_reader —— 那会把 numpy/pandas 拖进本模块）
_CN_ALIASES = {"A", "A_SHARE", "SSE", "CN"}

_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _cached(key: str, loader):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < TTL_SECONDS:
            return hit[1]
    obj = loader()
    with _lock:
        _cache[key] = (now, obj)
    return obj


def _mtime_key(path: Path, prefix: str) -> str:
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = 0
    return f"{prefix}:{path}:{mtime}"


def _num(value) -> float | None:
    """isinstance+isfinite 双守卫；bool 是 int 子类，先排掉。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    out = float(value)
    return out if math.isfinite(out) else None


def _int(value) -> int | None:
    """天数列：必须是有限整数（2355.0 收，12.5 拒）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    out = float(value)
    if not math.isfinite(out) or not out.is_integer():
        return None
    return int(out)


def _fmt_day(value) -> str | None:
    """'20170103' / '2020-01-23' / '2026-10-10T…' → 'YYYY-MM-DD'（展示口径统一）。

    区间与快照日期都走它——同一张态势条上混着两种日期格式是展示事故。
    """
    if value is None:
        return None
    digits = str(value).strip().split(" ")[0].replace("-", "").replace("/", "")
    if len(digits) > 8:
        digits = digits[:8]
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    return None


def _parse_ts(text) -> datetime | None:
    if not isinstance(text, str) or not text.strip():
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
    return None


def _is_cn_market(market) -> bool:
    return str(market or "CN").upper().strip() in _CN_ALIASES


@dataclass(frozen=True)
class ReportIndex:
    dataset: str
    report_date: str | None  # YYYY-MM-DD（generated_at 优先，缺则文件 mtime）
    window: dict  # {n_dates, start, end, horizon}
    stale: bool
    factors: dict[str, dict]  # 列名 → stats 载荷（source="report"）


@dataclass(frozen=True)
class ResearchIndex:
    built_at: str | None
    window: dict  # {n_dates, start, end}（82 采样日口径）
    factors: dict[str, dict]  # code → stats 载荷（source="research_snapshot"）


def report_dataset_for(source_dataset: str) -> str | None:
    """source_dataset → 报告数据集名；不在报告注册表内返回 None。

    **精确匹配**（上游 ``_validate_source`` 已收口），且**不兜底**到默认数据集：
    ccass_factors 这类没有报告的源若被兜到 alpha_library，页面会把别的库的指标
    安在这个库的因子上——静默错数。
    """
    key = str(source_dataset or "")
    return key if key in DATASETS else None


def _extract_report_stat(item: dict) -> dict:
    stat: dict = {key: _num(item.get(key)) for key in _FLOAT_KEYS}
    stat["ic_neutral_days"] = _int(item.get("ic_neutral_days"))
    library = item.get("library")
    stat["library"] = library if isinstance(library, str) and library else None
    stat["source"] = "report"
    return stat


def load_report_index(dataset: str) -> ReportIndex | None:
    """读报告快照并抽精简索引（mtime+TTL 缓存）；缺失/损坏 → None（fail-soft）。

    ``dataset`` 必须是 ``datasets.DATASETS`` 的键（调用方经 ``report_dataset_for``
    收口）；兜底函数 ``dataset_dir`` 对未知键会静默落到 alpha_library，所以这里
    再挡一道。
    """
    if dataset not in DATASETS:
        return None
    path = dataset_dir(dataset) / "report" / "factor_report.json"

    def _load() -> ReportIndex | None:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("因子报告快照解析失败(%s): %s", dataset, exc)
            return None
        if not isinstance(payload, dict):
            return None
        meta = payload.get("meta")
        meta = meta if isinstance(meta, dict) else {}
        factors: dict[str, dict] = {}
        for item in payload.get("factors") or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if isinstance(name, str) and name:
                factors[name] = _extract_report_stat(item)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        ts = _parse_ts(meta.get("generated_at")) or (
            datetime.fromtimestamp(mtime) if mtime else None
        )
        return ReportIndex(
            dataset=dataset,
            report_date=ts.strftime("%Y-%m-%d") if ts else None,
            window={
                "n_dates": _int(meta.get("n_dates")),
                "start": _fmt_day(meta.get("start")),
                "end": _fmt_day(meta.get("end")),
                "horizon": meta.get("horizon")
                if isinstance(meta.get("horizon"), str)
                else None,
            },
            stale=bool(ts) and (datetime.now() - ts).days > STALE_DAYS,
            factors=factors,
        )

    return _cached(_mtime_key(path, "report"), _load)


def load_research_fallback() -> ResearchIndex | None:
    """私域研究快照兜底（有则返回；缺失/损坏 → None）。

    自读自缓存而不复用 ``store.factors_meta``：store 模块顶部 import pandas，
    本模块要保持在「只有 stdlib」的依赖面上（同款 mtime/TTL 键控在本地实现）。
    """
    path = resolve_quantdb_dir() / FALLBACK_DIR / FALLBACK_FILE

    def _load() -> ResearchIndex | None:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("私域因子快照解析失败(%s): %s", path, exc)
            return None
        if not isinstance(payload, dict):
            return None
        factors: dict[str, dict] = {}
        for item in payload.get("factors") or []:
            if not isinstance(item, dict):
                continue
            code = item.get("code")
            if not isinstance(code, str) or not code:
                continue
            factors[code] = {
                "source": "research_snapshot",
                "ic_mean": _num(item.get("ic_mean")),
                "icir": _num(item.get("ic_ir")),
                "t_value": None,
                "turnover": None,
                "monotonicity": None,
                "win_rate": None,
                "n_valid_mean": None,
                "ic_neutral_days": None,
                "library": None,
            }
        meta = payload.get("meta")
        meta = meta if isinstance(meta, dict) else {}
        raw_window = meta.get("window")
        raw_window = raw_window if isinstance(raw_window, list) else []
        built_at = meta.get("built_at")
        return ResearchIndex(
            built_at=_fmt_day(built_at),
            window={
                "n_dates": _int(meta.get("n_dates")),
                "start": _fmt_day(raw_window[0]) if len(raw_window) > 0 else None,
                "end": _fmt_day(raw_window[1]) if len(raw_window) > 1 else None,
            },
            factors=factors,
        )

    return _cached(_mtime_key(path, "research"), _load)


def build_fields_stats(market: str | None, source_dataset: str, columns) -> dict:
    """聚合入口：返回 ``{"stats": {列名: 载荷|None}, "meta": {...}}``，承诺绝不抛。

    报告快照是 A 股（CN）根下产物：非 CN 市场一律收口（meta.reason 说明），
    即便同名快照文件在盘上也不读——那可能是另一个市场的同名目录。
    """
    cols = [str(col) for col in (columns or [])]
    stats: dict[str, dict | None] = dict.fromkeys(cols)
    meta: dict = {
        "available": False,  # 报告快照是否可用（缺失时前端看 reason；兜底命中仍可渲染行内值）
        "reason": None,
        "dataset": None,
        "report_date": None,
        "window": None,
        "matched": 0,
        "total": len(cols),
        "fallback_used": 0,
        "fallback_window": None,
        "stale": False,
        "rebuild_hint": None,
    }
    try:
        if not _is_cn_market(market):
            meta["reason"] = "因子报告仅覆盖 A 股（CN）市场，其他市场暂无逐因子质量统计"
            return {"stats": stats, "meta": meta}

        dataset = report_dataset_for(source_dataset)
        index = load_report_index(dataset) if dataset else None
        if index is not None:
            meta.update(
                available=True,
                dataset=dataset,
                report_date=index.report_date,
                window=index.window,
                stale=index.stale,
            )
            for col in cols:
                stat = index.factors.get(col)
                if stat is not None:
                    stats[col] = stat
        elif dataset is None:
            meta["reason"] = "该因子源尚未构建因子报告快照"
        else:
            meta["reason"] = "因子报告快照缺失或损坏"
            meta["rebuild_hint"] = f"可在因子研究报告页重建「{dataset}」快照"

        missing = [col for col in cols if stats[col] is None]
        if missing:
            fallback = load_research_fallback()
            if fallback is not None:
                meta["fallback_window"] = fallback.window
                for col in missing:
                    stat = fallback.factors.get(col)
                    if stat is not None:
                        stats[col] = stat
    except Exception:  # noqa: BLE001 —— 挂在 /fields 上，统计层故障不得拖垮字段列表
        log.exception(
            "训练目录统计聚合失败(market=%s, source=%s)", market, source_dataset
        )
        stats = dict.fromkeys(cols)
        meta.update(
            available=False,
            reason="统计聚合内部错误，请查看服务日志",
            dataset=None,
            report_date=None,
            window=None,
            fallback_used=0,
            fallback_window=None,
            stale=False,
            rebuild_hint=None,
        )

    meta["fallback_used"] = sum(
        1
        for col in cols
        if stats[col] is not None and stats[col].get("source") == "research_snapshot"
    )
    meta["matched"] = sum(1 for col in cols if stats[col] is not None)
    return {"stats": stats, "meta": meta}
