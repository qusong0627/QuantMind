"""因子研究时间窗 / 方向冻结 / 年分区的**口径唯一实现**。

2026-11-10 起工作台从「2020-01 至今的单快照」扩展为「2016-07 至今的主库」：

- 起始日：CN 默认 ``2016-07-01``。行情/估值/ST PIT 从 2016-01-04 起，股票池
  要求近 120 日有效历史 → 2016 年上半年为空窗，首个可用采样月末是 2016-07。
- 分区：``factor_panel/year=YYYY.parquet``、``monthly_scores/year=YYYY.parquet``、
  ``stock_snapshot/year=YYYY.parquet``。运行时只读与请求区间相交的年份，
  单窗口内存有界；构建/重建可按年替换，不重算全历史。
- 方向：私有因子库的 sign **只在校准窗**（默认 2020-01-01~2024-12-31）上算一次，
  冻结在 ``direction.json``，评估窗口（尤其 2016–2019）只读不改。
  旧实现按「本次构建全窗口 IC」翻符号，拿 2016–2019 做样本外时 direction 已经
  看过 2020+ 的 IC —— 那不是样本外。

本模块只放纯函数与文件约定，不 import 服务层，避免构建脚本/服务层各抄一份。
"""

from __future__ import annotations

import json
import os
import warnings
from collections.abc import Iterable
from pathlib import Path

import numpy as np
import pandas as pd

# ── 默认窗口（口径常量，改这里且只有这里）────────────────────────────
CN_START = "2016-07-01"
CN_LOOKBACK_START = "2016-01-04"
LEGACY_START = "2020-01-01"
LEGACY_LOOKBACK_START = "2018-06-01"
DIRECTION_START = "2020-01-01"
DIRECTION_END = "2024-12-31"
FWD_BUFFER_DAYS = 45
"""块构建的持有期缓冲：末月 fwd 需要下一采样日的价格。"""

DIRECTION_FILE = "direction.json"
COVERAGE_FILE = "coverage.json"
PANEL_DIR = "factor_panel"
SCORES_DIR = "monthly_scores"
SNAPSHOT_DIR = "stock_snapshot"
PARTITION_MANIFEST = "years.json"
"""分区目录的年份清单（断言完整性；防「中间年缺失但运行时静默少一段」）。"""


def defaults_for_market(market: str) -> tuple[str, str]:
    """(start, lookback_start)。CN 用 2016 扩展窗；非 CN 保持原 2020 口径 ——
    非 CN 数据面（尤其 HK/US 的估值/ST 口径）未做本轮体检，不能假装同样可用。"""
    if str(market).upper() == "CN":
        return CN_START, CN_LOOKBACK_START
    return LEGACY_START, LEGACY_LOOKBACK_START


def parse_start(value: str | None, default: str) -> str:
    """用户区间 → 起始日字符串（None 用默认；只做格式校验，不做业务裁剪）。"""
    raw = str(value or default).strip()
    pd.Timestamp(raw)  # 非法格式立即抛，别拖到读盘深处
    return raw


def parse_end(value: str | None) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    raw = str(value).strip()
    pd.Timestamp(raw)
    return raw


def year_of(value) -> int:
    return int(pd.Timestamp(value).year)


def selected_years(start: str | None, end: str | None) -> tuple[int, int] | None:
    """请求区间 → (起始年, 截止年)；None/空 → None（全部年份）。"""
    y0 = year_of(start) if start else None
    y1 = year_of(end) if end else None
    if y0 is None and y1 is None:
        return None
    return (y0 if y0 is not None else 1900, y1 if y1 is not None else 2999)


def year_files(dir_path: Path, start: str | None = None, end: str | None = None) -> list[Path]:
    """目录下与区间相交的 ``year=YYYY.parquet``（升序）。"""
    if not dir_path.is_dir():
        return []
    sel = selected_years(start, end)
    out: list[Path] = []
    for p in sorted(dir_path.glob("year=*.parquet")):
        try:
            y = int(p.name[len("year=") : -len(".parquet")])
        except ValueError:
            continue
        if sel is not None and not (sel[0] <= y <= sel[1]):
            continue
        out.append(p)
    return out


def files_stamp(files: Iterable[Path], extra: str = "") -> str:
    """文件集缓存键 = 路径 + mtime；重建落盘后旧缓存自动失效。"""
    parts = [f"{p}:{p.stat().st_mtime_ns}" for p in files]
    return "|".join(parts) + f"|{extra}"


def atomic_write_parquet(df: pd.DataFrame, path: Path, *, index: bool = False) -> None:
    """先写 tmp 再 replace：构建中断不会留下半截 parquet 被运行时读到。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp, index=index)
    os.replace(tmp, path)


def write_year_partitions(
    df: pd.DataFrame,
    out_dir: Path,
    name: str,
    *,
    date_col: str = "trade_date",
    index: bool = False,
) -> list[Path]:
    """按 ``date_col`` 的年份切分落盘 ``out_dir/name/year=YYYY.parquet``。

    同时维护 ``years.json`` 清单：运行时据此断言「请求区间内的年份一个不缺」，
    防止构建中断后静默读到少一段的面板。
    """
    if df.empty or date_col not in df.columns:
        return []
    years = pd.to_datetime(df[date_col]).dt.year
    d = out_dir / name
    written: list[Path] = []
    new_years: set[int] = set()
    for y, sub in df.groupby(years, sort=True):
        p = d / f"year={int(y)}.parquet"
        atomic_write_parquet(sub, p, index=index)
        written.append(p)
        new_years.add(int(y))
    manifest = d / PARTITION_MANIFEST
    old_years: set[int] = set()
    if manifest.exists():
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
            old_years = {int(y) for y in raw.get("years", [])}
        except (OSError, ValueError, TypeError, AttributeError):
            old_years = set()
    payload = {
        "years": sorted(old_years | new_years),
        "updated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
    }
    tmp = manifest.with_name(manifest.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, manifest)
    return written


def missing_years(
    dir_path: Path, start: str | None = None, end: str | None = None
) -> list[int]:
    """请求区间内、分区清单声明应存在但缺失的年份（无清单 → 空表，兼容旧快照）。"""
    manifest = dir_path / PARTITION_MANIFEST
    if not manifest.exists():
        return []
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        years = {int(y) for y in raw.get("years", [])}
    except (OSError, ValueError, TypeError, AttributeError):
        return []
    if not years:
        return []
    lo = year_of(start) if start else min(years)
    hi = year_of(end) if end else max(years)
    lo, hi = max(lo, min(years)), min(hi, max(years))
    if hi < lo:
        return []
    present = {
        int(p.name[len("year=") : -len(".parquet")])
        for p in dir_path.glob("year=*.parquet")
    }
    return [y for y in range(lo, hi + 1) if y not in present]



def date_bounds(start: str | None, end: str | None) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    """用户区间 → (起始时间, 截止时间)；``end=YYYY-MM`` 取当月最后一天。"""
    s = pd.Timestamp(start) if start else None
    e = None
    if end:
        raw = str(end)
        e = pd.Timestamp(raw if len(raw) > 7 else f"{raw}-01") + pd.offsets.MonthEnd(0)
    return s, e


def clip_dates(
    df: pd.DataFrame,
    column: str = "trade_date",
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """按 ``column`` 裁剪到 [start, end]；列不存在或区间为空 → 原样返回。"""
    if df.empty or column not in df.columns:
        return df
    s, e = date_bounds(start, end)
    if s is None and e is None:
        return df
    dt = pd.to_datetime(df[column])
    keep = pd.Series(True, index=df.index)
    if s is not None:
        keep &= dt >= s
    if e is not None:
        keep &= dt <= e
    return df[keep].reset_index(drop=True)


def read_year_partitions(
    dir_path: Path,
    *,
    start: str | None = None,
    end: str | None = None,
    date_col: str = "trade_date",
) -> pd.DataFrame | None:
    """读与区间相交的年份分区并拼回；没有分区 → None（调用方回退单文件）。"""
    files = year_files(dir_path, start, end)
    if not files:
        return None
    parts = [pd.read_parquet(p) for p in files]
    out = pd.concat(parts, ignore_index=True)
    return clip_dates(out, date_col, start, end)


# ── 方向冻结 ────────────────────────────────────────────────────────
def load_direction(path: Path | str | None) -> dict[str, dict]:
    """读取 ``direction.json``；缺失/损坏 → 空表（调用方按 fallback 处理）。"""
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = raw.get("factors") if isinstance(raw, dict) else None
    return entries if isinstance(entries, dict) else {}


def save_direction(
    path: Path | str,
    entries: dict[str, dict],
    *,
    calibration: tuple[str, str],
    extra: dict | None = None,
) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "calibration": {"start": calibration[0], "end": calibration[1]},
        "factors": entries,
    }
    if extra:
        payload.update(extra)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, allow_nan=False),
        encoding="utf-8",
    )
    os.replace(tmp, p)


def calibrated_signs(
    ic: np.ndarray,
    dates: Iterable[pd.Timestamp],
    names: list[str],
    *,
    cal_start: str,
    cal_end: str,
    frozen: dict[str, dict] | None = None,
) -> tuple[np.ndarray, list[dict]]:
    """每因子 sign：校准窗 IC 均值为负则 -1，否则 +1。

    ``ic`` 形状 (F, D)，``dates`` 与 D 对齐。``frozen`` 已存在的因子直接采用其
    ``direction``（跨块/跨窗口一致），只对缺失因子用本块校准窗补算。
    返回 (sign 向量, 每因子审计条目)。
    """
    ic = np.asarray(ic, dtype=np.float64)
    n_f = len(names)
    if ic.ndim != 2 or ic.shape[0] < n_f:
        raise ValueError(f"ic 形状 {ic.shape} 与因子数 {n_f} 不匹配")
    ic = ic[:n_f]
    n_d = ic.shape[1]
    if n_f == 0:
        return np.ones(0), []
    dts = np.asarray(pd.to_datetime(list(dates)))
    cal = (dts >= np.datetime64(pd.Timestamp(cal_start))) & (
        dts <= np.datetime64(pd.Timestamp(cal_end))
    )
    cal_present = bool(cal.any())
    if not cal_present:  # 构建窗口与校准窗不相交（如只重建 2016–2019）
        cal = np.ones(n_d, dtype=bool)
    ic_cal = ic[:, cal]
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        mean_cal = np.nanmean(ic_cal, axis=1)
        mean_all = np.nanmean(ic, axis=1)
    fallback = ~np.isfinite(mean_cal)
    mean_cal[fallback] = mean_all[fallback]
    signs = np.where(np.isfinite(mean_cal) & (mean_cal < 0), -1.0, 1.0)
    frozen = frozen or {}
    entries: list[dict] = []
    for i, name in enumerate(names):
        fz = frozen.get(name)
        if isinstance(fz, dict) and fz.get("direction") in (-1, 1):
            signs[i] = float(fz["direction"])
            entries.append(
                {
                    "direction": int(signs[i]),
                    "source": "frozen",
                    "calibration": fz.get("calibration"),
                    "ic_mean": fz.get("ic_mean"),
                    "fallback": bool(fz.get("fallback", False)),
                }
            )
            continue
        entries.append(
            {
                "direction": int(signs[i]),
                "source": "calibration"
                if (cal_present and not fallback[i])
                else "fallback",
                "calibration": [cal_start, cal_end],
                "ic_mean": round(float(mean_cal[i]), 6)
                if np.isfinite(mean_cal[i])
                else None,
                # 校准窗不在本次构建窗口内 = 方向不是在校准窗上算的，必须如实标记
                "fallback": bool(fallback[i] or not cal_present),
            }
        )
    return signs, entries


def merge_direction_entries(
    base: dict[str, dict], fresh: Iterable[dict], names: list[str]
) -> dict[str, dict]:
    """把本块算出的方向并进已有冻结表；已有条目**不覆盖**（方向一经冻结不改）。"""
    out = dict(base)
    for name, entry in zip(names, fresh, strict=False):
        out.setdefault(name, entry)
    return out
