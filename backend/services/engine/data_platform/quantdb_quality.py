"""QuantDB 同步语义质量闸门（A1）。

同步器已经做了 sha256/etag 级「文件没坏」校验，但回答不了三个业务问题：
1. 最近 N 个交易日该到的分区到齐了吗（上游 release 漏发/本地写失败）；
2. 最新分区的行数是否断崖（截断/半截文件）；
3. 列集是否漂移（上游加列/删列，训练特征按名取数会静默错位）。

本模块**只读**落盘 parquet，不修改数据；结果写 state 目录的 latest.json，
异常通过既有的 data_quality_alerts + 站内通知 fan-out。默认 `warn` 模式
只告警不阻断同步；`block` 模式由调用方决定是否跳过后面的 PG/Qlib 阶段。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq

from backend.shared.quantdb_datasets import DATASETS

logger = logging.getLogger(__name__)

CALENDAR_REL = "2_base_sector/trading_calendar"
CALENDAR_FILE = "trading_days.parquet"
DEFAULT_LOOKBACK_DAYS = 10
DEFAULT_ROW_DROP_RATIO = 0.5
DEFAULT_ROW_HISTORY = 20
MIN_HISTORY_PARTITIONS = 5
_SEVERITY_ORDER = {"warning": 1, "error": 2}

# 已知 T+1 数据集：两融数据次日才落盘，按前一交易日判定，避免每晚误报。
DATASET_EXTRA_LAG_DAYS: dict[str, int] = {
    "margin_trading": 1,
}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dataset_rel_dir(dataset: str) -> tuple[str, str] | None:
    """数据集 → (rel_dir, layout)；未知数据集返回 None。"""
    for spec in DATASETS:
        if spec.dataset == dataset:
            return spec.rel_dir, spec.layout
    return None


def partition_dates(root: str | Path, rel_dir: str) -> list[str]:
    """有 data.parquet 的 dt=YYYYMMDD 分区日期（升序，空壳目录不算）。"""
    base = Path(root) / rel_dir
    if not base.is_dir():
        return []
    dates: list[str] = []
    for part in base.glob("dt=*"):
        if not part.is_dir() or not (part / "data.parquet").is_file():
            continue
        digits = part.name[3:]
        if len(digits) == 8 and digits.isdigit():
            dates.append(digits)
    return sorted(set(dates))


def load_trading_days(root: str | Path) -> tuple[list[str], str]:
    """交易日历（YYYYMMDD 升序）。

    优先读 ``trading_calendar/trading_days.parquet``（TradingDate/IsTradingDay）；
    读不到时退回 daily_forward 的分区日期——此时「最新一天是否缺失」无法判定，
    但历史缺口/行数/列集检查仍然有效，且不会误报最新日。
    """
    root = Path(root)
    cal_path = root / CALENDAR_REL / CALENDAR_FILE
    if cal_path.is_file():
        try:
            frame = pd.read_parquet(cal_path)
            if {"TradingDate", "IsTradingDay"} <= set(frame.columns):
                days = frame.loc[frame["IsTradingDay"] == 1, "TradingDate"]
                out = sorted({str(int(v)) for v in days if pd.notna(v)})
                if out:
                    return out, "trading_calendar"
        except Exception as exc:  # noqa: BLE001 - 日历坏了退回分区日期
            logger.warning("读取交易日历失败，回退 daily_forward 分区: %s", exc)

    fallback = partition_dates(root, "1_kline_data/daily_forward")
    return fallback, "daily_forward"


def _expected_latest(
    trading_days: list[str], today: date, max_lag_days: int
) -> tuple[str, str]:
    """返回 (应到最新日, 该日是否因 today 未收盘被放宽)。

    max_lag_days=0：严格要求最近一个交易日；
    >=1：仅当最近交易日就是 today（当天数据可能还没下来）时，放宽到前一个交易日。
    周末/节假日没有 today 分区是常态，不触发放宽。
    """
    days = [d for d in trading_days if d <= today.strftime("%Y%m%d")]
    if not days:
        return "", ""
    latest = days[-1]
    if max_lag_days >= 1 and latest == today.strftime("%Y%m%d") and len(days) >= 2:
        return days[-2], latest
    return latest, ""


def _shift_expected(expected: str, trading_days: list[str], n: int) -> str:
    """把应到最新日沿交易日历回退 n 天（用于已知 T+1 数据集）。"""
    if n <= 0 or not expected or not trading_days:
        return expected
    days = [d for d in trading_days if d <= expected]
    if not days:
        return expected
    idx = len(days) - 1 - n
    return days[idx] if idx >= 0 else days[0]


def _row_count(path: Path) -> int:
    return int(pq.ParquetFile(path).metadata.num_rows)


def _schema_columns(path: Path) -> list[str]:
    return [str(name) for name in pq.ParquetFile(path).schema_arrow.names]


def _symbol_count(path: Path) -> int | None:
    try:
        frame = pd.read_parquet(path, columns=["symbol"])
    except Exception:  # noqa: BLE001 - 非标准数据集可能无 symbol
        return None
    if frame.empty:
        return 0
    return int(frame["symbol"].astype(str).nunique())


def _issue(
    dataset: str,
    issue_type: str,
    severity: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "dataset": dataset,
        "issue_type": issue_type,
        "severity": severity,
        "message": message,
        "details": details or {},
    }


# ---------------------------------------------------------------------------
# 单项检查
# ---------------------------------------------------------------------------


def check_coverage(
    dataset: str,
    root: Path,
    trading_days: list[str],
    *,
    expected_latest: str,
    relaxed_from: str,
    lookback_days: int,
) -> list[dict[str, Any]]:
    dates = partition_dates(root, _dataset_rel_dir(dataset)[0])  # type: ignore[index]
    actual = set(dates)
    if not dates:
        return [
            _issue(
                dataset,
                "latest_lag",
                "error",
                f"{dataset} 无任何 dt 分区",
                {"expected_latest": expected_latest, "partitions": 0},
            )
        ]

    issues: list[dict[str, Any]] = []
    if expected_latest and expected_latest not in actual:
        issues.append(
            _issue(
                dataset,
                "latest_lag",
                "error",
                f"{dataset} 最新分区缺失：应到 {expected_latest}，实际最新 {dates[-1]}",
                {
                    "expected_latest": expected_latest,
                    "actual_latest": dates[-1],
                    "relaxed_from": relaxed_from or None,
                },
            )
        )
    elif relaxed_from:
        # 今天的数据还没下来：只记录，不告警
        logger.debug(
            "%s 允许 today(%s) 分区未到，按 %s 判定",
            dataset,
            relaxed_from,
            expected_latest,
        )

    window = [d for d in trading_days if not expected_latest or d <= expected_latest]
    window = window[-max(1, lookback_days) :]
    missing = [d for d in window if d not in actual]
    # 最新日缺失已由 latest_lag 报过，避免同一天重复报两类
    missing = [d for d in missing if d != expected_latest]
    if missing:
        issues.append(
            _issue(
                dataset,
                "missing_partition",
                "warning",
                f"{dataset} 近 {lookback_days} 个交易日缺 {len(missing)} 个分区: "
                + ", ".join(missing[:8]),
                {"missing": missing, "window": window},
            )
        )
    return issues


def check_row_count(
    dataset: str,
    root: Path,
    *,
    row_drop_ratio: float = DEFAULT_ROW_DROP_RATIO,
    history: int = DEFAULT_ROW_HISTORY,
) -> list[dict[str, Any]]:
    rel_dir = _dataset_rel_dir(dataset)[0]  # type: ignore[index]
    dates = partition_dates(root, rel_dir)
    if len(dates) < MIN_HISTORY_PARTITIONS + 1:
        return []
    latest = dates[-1]
    latest_path = root / rel_dir / f"dt={latest}" / "data.parquet"
    try:
        latest_rows = _row_count(latest_path)
        prev_rows = [
            _row_count(root / rel_dir / f"dt={d}" / "data.parquet")
            for d in dates[-(history + 1) : -1]
        ]
    except Exception as exc:  # noqa: BLE001 - 单个坏文件不让整轮失败
        return [
            _issue(
                dataset,
                "check_failed",
                "warning",
                f"{dataset} 行数检查失败: {exc}",
                {"latest": latest},
            )
        ]
    prev_rows = [n for n in prev_rows if n > 0]
    if not prev_rows:
        return []
    median = float(pd.Series(prev_rows).median())
    threshold = median * (1.0 - row_drop_ratio)
    if median > 0 and latest_rows < threshold:
        symbols = _symbol_count(latest_path)
        return [
            _issue(
                dataset,
                "row_count_drop",
                "error",
                f"{dataset} 最新分区({latest})行数断崖: {latest_rows} vs 近期中位 {median:.0f}",
                {
                    "latest": latest,
                    "latest_rows": latest_rows,
                    "median_rows": int(median),
                    "threshold": int(threshold),
                    "latest_symbols": symbols,
                },
            )
        ]
    return []


def check_schema(
    dataset: str,
    root: Path,
    state_dir: Path,
) -> list[dict[str, Any]]:
    rel_dir = _dataset_rel_dir(dataset)[0]  # type: ignore[index]
    dates = partition_dates(root, rel_dir)
    if not dates:
        return []
    latest = dates[-1]
    path = root / rel_dir / f"dt={latest}" / "data.parquet"
    try:
        columns = _schema_columns(path)
    except Exception as exc:  # noqa: BLE001
        return [
            _issue(
                dataset,
                "check_failed",
                "warning",
                f"{dataset} schema 检查失败: {exc}",
                {"latest": latest},
            )
        ]
    schema_hash = hashlib.sha256("\n".join(columns).encode()).hexdigest()
    state_path = state_dir / "schema" / f"{dataset}.json"
    previous: dict[str, Any] | None = None
    try:
        if state_path.is_file():
            previous = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = None

    # 首次运行没有跨日状态：拿前一个分区做基线（同一晚重写/截断最常在这里暴露）。
    if previous is None and len(dates) >= 2:
        prev_path = root / rel_dir / f"dt={dates[-2]}" / "data.parquet"
        try:
            prev_columns = _schema_columns(prev_path)
            previous = {
                "hash": hashlib.sha256("\n".join(prev_columns).encode()).hexdigest(),
                "columns": prev_columns,
            }
        except Exception:  # noqa: BLE001 - 前一分区读不到就只记录无基线
            previous = None

    issues: list[dict[str, Any]] = []
    if previous and previous.get("hash") and previous["hash"] != schema_hash:
        old_cols = set(previous.get("columns") or [])
        new_cols = set(columns)
        added = sorted(new_cols - old_cols)
        removed = sorted(old_cols - new_cols)
        issues.append(
            _issue(
                dataset,
                "schema_drift",
                "warning",
                f"{dataset} 列集漂移：+{len(added)} / -{len(removed)}",
                {
                    "latest": latest,
                    "old_hash": previous["hash"][:16],
                    "new_hash": schema_hash[:16],
                    "added": added,
                    "removed": removed,
                },
            )
        )

    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(
            {
                "hash": schema_hash,
                "columns": columns,
                "updated_at": _now_iso(),
                "latest": latest,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.replace(tmp, state_path)
    return issues


# ---------------------------------------------------------------------------
# 告警（同日同问题去重）
# ---------------------------------------------------------------------------


def _load_alert_state(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()}
    except (OSError, ValueError):
        return {}


def _save_alert_state(path: Path, state: dict[str, str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _alert_fingerprint(issue: dict[str, Any]) -> str:
    raw = f"{issue['dataset']}|{issue['issue_type']}|{issue['message']}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _emit_alerts(
    issues: list[dict[str, Any]],
    *,
    state_dir: Path,
    today: str,
    alert_service: Any,
) -> int:
    """写 data_quality_alerts；同一 fingerprint 同一天只写一次。返回写入数。"""
    if alert_service is False or not issues:
        return 0
    service = alert_service
    if service is None:
        try:
            from backend.services.engine.data_platform.alert_service import (
                get_alert_service,
            )

            service = get_alert_service()
        except Exception as exc:  # noqa: BLE001 - 告警失败不影响同步
            logger.warning("质量告警服务不可用: %s", exc)
            return 0

    state_path = state_dir / "alert_state.json"
    seen = _load_alert_state(state_path)
    # 只保留最近 30 天的去重记录，避免状态文件随时间无限增长
    try:
        cutoff = (datetime.strptime(today, "%Y%m%d") - timedelta(days=30)).strftime(
            "%Y%m%d"
        )
        seen = {k: v for k, v in seen.items() if v >= cutoff}
    except ValueError:
        pass
    written = 0
    for issue in issues:
        fp = _alert_fingerprint(issue)
        if seen.get(fp) == today:
            continue
        try:
            service.write_alert(
                alert_type=f"quantdb_sync_{issue['issue_type']}",
                severity=issue["severity"],
                message=issue["message"],
                market="CN",
                field=issue["dataset"],
                source="quantdb",
                details={"dataset": issue["dataset"], **issue["details"]},
                notify_admins=True,
            )
            seen[fp] = today
            written += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("写质量告警失败: %s", exc)
    if written:
        _save_alert_state(state_path, seen)
    return written


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def default_datasets(root: str | Path) -> list[str]:
    """默认检查面：本地**已有 dt 分区**的 partition 数据集。

    不检查「从未同步过的 optional 数据集」（如未开启的 V5 宽表）与
    非标准布局（如 tick_data 的按 symbol 文件），否则正常同步后会因为
    一个用户没启用的数据集而报红。同步钩子仍会显式传入本轮请求的数据集，
    核心数据集缺失不会被漏掉。
    """
    root = Path(root)
    out: list[str] = []
    for spec in DATASETS:
        if spec.layout != "partition":
            continue
        base = root / spec.rel_dir
        if not base.is_dir():
            continue
        if any(base.glob("dt=*/data.parquet")):
            out.append(spec.dataset)
    return out


def run_quality_gate(
    datasets: list[str] | None,
    root: str | Path,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    max_lag_days: int = 1,
    today: date | None = None,
    mode: str = "warn",
    alert_service: Any = None,
    state_dir: str | Path | None = None,
    row_drop_ratio: float = DEFAULT_ROW_DROP_RATIO,
) -> dict[str, Any]:
    """对指定数据集跑质量闸门并落报告。

    mode: ``warn`` 只告警；``block`` 时若存在 error → ``blocked=True``（由调用方决定是否
    跳过后续阶段）。本函数不抛异常：单数据集失败降级为 ``check_failed``。
    """
    root = Path(root)
    today = today or date.today()
    today_str = today.strftime("%Y%m%d")
    state = Path(state_dir) if state_dir else root / ".quality"
    state.mkdir(parents=True, exist_ok=True)

    trading_days, calendar_source = load_trading_days(root)
    expected_latest, relaxed_from = _expected_latest(trading_days, today, max_lag_days)

    names = list(datasets) if datasets is not None else default_datasets(root)
    report: dict[str, Any] = {
        "checked_at": _now_iso(),
        "today": today.isoformat(),
        "root": str(root),
        "calendar_source": calendar_source,
        "expected_latest": expected_latest,
        "relaxed_from_today": relaxed_from or None,
        "mode": mode,
        "lookback_days": lookback_days,
        "datasets": {},
        "issues": [],
        "ok": True,
        "blocked": False,
    }

    for dataset in names:
        spec = _dataset_rel_dir(dataset)
        if spec is None:
            report["datasets"][dataset] = {"skipped": True, "reason": "unknown_dataset"}
            continue
        rel_dir, layout = spec
        if layout != "partition":
            report["datasets"][dataset] = {
                "skipped": True,
                "reason": f"layout={layout}",
            }
            continue
        try:
            dates = partition_dates(root, rel_dir)
            # 非 dt 分区布局（如 tick_data 按 symbol 文件）：显式请求时跳过，
            # 不能当成「最新分区缺失」——那会把正常目录报成故障。
            if not dates:
                base = root / rel_dir
                has_non_dt = False
                if base.is_dir():
                    try:
                        has_non_dt = any(
                            p.is_file() or (p.is_dir() and not p.name.startswith("dt="))
                            for p in base.iterdir()
                        )
                    except OSError:
                        has_non_dt = False
                if has_non_dt:
                    report["datasets"][dataset] = {
                        "skipped": True,
                        "reason": "no_dt_partitions",
                        "partitions": 0,
                        "latest": None,
                        "issues": [],
                    }
                    continue

            extra_lag = DATASET_EXTRA_LAG_DAYS.get(dataset, 0)
            ds_expected = _shift_expected(expected_latest, trading_days, extra_lag)
            issues: list[dict[str, Any]] = []
            issues += check_coverage(
                dataset,
                root,
                trading_days,
                expected_latest=ds_expected,
                relaxed_from=relaxed_from,
                lookback_days=lookback_days,
            )
            issues += check_row_count(dataset, root, row_drop_ratio=row_drop_ratio)
            issues += check_schema(dataset, root, state)
            report["datasets"][dataset] = {
                "skipped": False,
                "partitions": len(dates),
                "latest": dates[-1] if dates else None,
                "expected_latest": ds_expected,
                "issues": issues,
            }
            report["issues"].extend(issues)
        except Exception as exc:  # noqa: BLE001 - 单库失败不拖垮整轮
            logger.warning("[quality] %s 检查失败: %s", dataset, exc, exc_info=True)
            issue = _issue(
                dataset, "check_failed", "warning", f"{dataset} 检查失败: {exc}"
            )
            report["datasets"][dataset] = {
                "skipped": False,
                "partitions": 0,
                "latest": None,
                "issues": [issue],
            }
            report["issues"].append(issue)

    report["ok"] = not any(i["severity"] == "error" for i in report["issues"])
    report["blocked"] = bool(mode == "block" and not report["ok"])
    report["alerts_written"] = _emit_alerts(
        report["issues"], state_dir=state, today=today_str, alert_service=alert_service
    )

    # 落报告（原子写；报告本身失败不影响同步）
    try:
        tmp = state / "latest.json.tmp"
        tmp.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, state / "latest.json")
    except OSError as exc:
        logger.warning("质量报告落盘失败: %s", exc)
    return report
