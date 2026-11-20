"""WP7 数据审计：每次训练后跑一遍廉价、确定性的 PIT/泄漏检查。

不做重活（特征逐日重算留给季度探针），只做能在秒级拿证据的项目：
1. 未来列命名扫描（return_/forward_/future_/label_/fwd_）；
2. 标签契约（T+1 执行、horizon≥1、公式可解析）；
3. trade_mask 覆盖（训练窗口内每个交易日都有 mask 行）；
4. ST 缓存覆盖（缺缓存的标的比例，warn）；
5. 幸存者偏差（pred 里是否含当前快照之外的退市股，info/warn）；
6. 数据指纹是否落盘；
7. 季度财务 PIT 探针产物（factor_research/pit_audit.json）摘要（info）。

结果写 qm_training_audit_reports，前端结果页展示，晋升闸读取。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session

logger = logging.getLogger(__name__)
_TASKS: set[asyncio.Task] = set()

DATA_ROOT = Path("/data")
QDB = DATA_ROOT / "quantdb"
TRADE_MASK = QDB / "5_technical_derived" / "trade_mask" / "trade_mask.parquet"
ST_CACHE = QDB / "factor_research" / "st_pit_cache"
PIT_PROBE = QDB / "factor_research" / "pit_audit.json"

_FUTURE_PATTERN = re.compile(r"^(return|forward|future|fwd|label)_", re.IGNORECASE)

_DDL = """
CREATE TABLE IF NOT EXISTS qm_training_audit_reports (
    run_id TEXT PRIMARY KEY,
    model_id TEXT,
    status TEXT NOT NULL DEFAULT 'ready',
    blocked BOOLEAN NOT NULL DEFAULT FALSE,
    report JSONB NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


def _check(check_id: str, level: str, ok: bool, detail: str) -> dict[str, Any]:
    return {
        "id": check_id,
        "level": level,  # fail | warn | info
        "status": "pass" if ok else "fail",
        "detail": detail,
    }


def _load_metadata(run_id: str) -> dict[str, Any]:
    path = DATA_ROOT / "training_jobs" / run_id / "metadata.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("audit: metadata unreadable for %s: %s", run_id, exc)
        return {}


def _pred_symbols_and_dates(run_id: str) -> tuple[set[str], set[str]]:
    path = DATA_ROOT / "training_jobs" / run_id / "pred.parquet"
    if not path.exists():
        return set(), set()
    try:
        frame = pd.read_parquet(path, columns=["symbol", "trade_date"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("audit: pred unreadable for %s: %s", run_id, exc)
        return set(), set()
    symbols = set(frame["symbol"].astype(str))
    dates = set(pd.to_datetime(frame["trade_date"]).dt.strftime("%Y%m%d"))
    return symbols, dates


def _mask_dates_and_symbols() -> tuple[set[str], set[str]]:
    frame = pd.read_parquet(TRADE_MASK, columns=["dt", "symbol"])
    return set(frame["dt"].astype(str)), set(frame["symbol"].astype(str))


_HIST_SAMPLE_DAYS = ("20160105", "20180102", "20200102", "20220104")


def _historical_symbols() -> set[str]:
    """历史交易日样本的股票池（用于幸存者偏差代理：pred 是否剔除了退市/长停股）。"""
    out: set[str] = set()
    for day in _HIST_SAMPLE_DAYS:
        path = QDB / "1_kline_data" / "daily_forward" / f"dt={day}" / "data.parquet"
        if not path.exists():
            continue
        try:
            out |= set(pd.read_parquet(path, columns=["symbol"])["symbol"].astype(str))
        except Exception as exc:  # noqa: BLE001
            logger.warning("audit: historical universe unreadable %s: %s", day, exc)
    return out


def to_suffix(symbol: str) -> str:
    s = str(symbol).strip().upper()
    if "." in s:
        return s
    if len(s) > 2 and s[:2] in ("SH", "SZ", "BJ"):
        return f"{s[2:]}.{s[:2]}"
    return s


def build_audit(run_id: str) -> dict[str, Any]:
    """返回 {blocked, checks, warnings, generated_at}；纯 IO，无 DB 写入。"""
    checks: list[dict[str, Any]] = []
    meta = _load_metadata(run_id)
    features = [
        str(x) for x in (meta.get("features") or meta.get("requested_features") or [])
    ]

    # 1) 未来列命名
    suspicious = [f for f in features if _FUTURE_PATTERN.match(f)]
    checks.append(
        _check(
            "future_feature_names",
            "fail",
            not suspicious,
            "未发现未来列命名" if not suspicious else f"疑似标签/未来列：{suspicious[:10]}",
        )
    )

    # 2) 标签契约
    label_formula = str((meta.get("label_formula") or "")).strip()
    horizon = int(meta.get("target_horizon_days") or 0)
    lag = int(meta.get("execution_lag_days") or 0)
    formula_ok = bool(
        re.search(r"close\s*\(\s*T\+", label_formula)
        and re.search(r"(open|close)\s*\(\s*T\+1\s*\)", label_formula)
    )
    if not label_formula:
        # 老模型 metadata/config 未记录公式：无法验证，不等同于「已泄漏」。
        checks.append(
            _check(
                "label_contract",
                "warn",
                False,
                f"未记录 label_formula（horizon={horizon} lag={lag}）；无法验证 T+1 执行口径",
            )
        )
    else:
        checks.append(
            _check(
                "label_contract",
                "fail",
                formula_ok and horizon >= 1 and lag >= 1,
                f"formula={label_formula} horizon={horizon} lag={lag}",
            )
        )

    # 3) 训练窗口的 trade_mask 覆盖
    _, pred_dates = _pred_symbols_and_dates(run_id)
    mask_dates, _ = _mask_dates_and_symbols()
    missing = sorted(pred_dates - mask_dates)
    missing_ratio = len(missing) / max(1, len(pred_dates))
    coverage_ok = len(pred_dates) > 0 and missing_ratio <= 0.02
    checks.append(
        _check(
            "trade_mask_coverage",
            "fail",
            coverage_ok,
            f"pred_days={len(pred_dates)} missing_mask_days={len(missing)}"
            f"（{missing_ratio:.1%}）"
            + (f"，如 {missing[:5]}" if missing else ""),
        )
    )

    # 4) ST 缓存覆盖（缺缓存 → 该票永远不按 ST 过滤；warn）
    pred_symbols, _ = _pred_symbols_and_dates(run_id)
    suffixes = {to_suffix(s) for s in pred_symbols}
    if suffixes:
        have = sum(1 for s in suffixes if (ST_CACHE / f"{s}.parquet").exists())
        ratio = have / len(suffixes)
        checks.append(
            _check(
                "st_cache_coverage",
                "warn",
                ratio >= 0.98,
                f"{have}/{len(suffixes)} ({ratio:.1%}) 标的已有 ST PIT 缓存",
            )
        )
    else:
        checks.append(_check("st_cache_coverage", "warn", False, "pred.parquet 为空或不可读"))

    # 5) 幸存者偏差代理：pred 是否覆盖历史交易样本里出现过的股票（含退市/长停）
    historical = {to_suffix(s) for s in _historical_symbols()}
    if historical:
        missing_hist = historical - suffixes
        ratio_hist = len(missing_hist) / len(historical)
        checks.append(
            _check(
                "survivorship_universe",
                "warn",
                ratio_hist <= 0.02,
                f"历史样本 {len(historical)} 只中 {len(missing_hist)} 只不在 pred"
                f"（{ratio_hist:.1%}）；>2% 提示可能剔除了退市/长停股",
            )
        )
    else:
        checks.append(
            _check("survivorship_universe", "warn", True, "无历史分区样本，跳过幸存者检查")
        )

    # 6) 数据指纹
    fingerprint = meta.get("data_fingerprint")
    checks.append(
        _check(
            "data_fingerprint",
            "warn",
            bool(fingerprint),
            "已记录" if fingerprint else "metadata 缺 data_fingerprint（不可复现审计）",
        )
    )

    # 7) 季度财务 PIT 探针（若已生成）
    if PIT_PROBE.exists():
        try:
            probe = json.loads(PIT_PROBE.read_text(encoding="utf-8"))
            suspicious_cols = [
                item.get("v5_column")
                for item in (probe.get("factors") or [])
                if item.get("status") == "pit_pending"
            ]
            checks.append(
                _check(
                    "financial_pit_probe",
                    "info",
                    not suspicious_cols,
                    "季度 PIT 探针：" + (f"疑似按报告期对齐 {suspicious_cols[:5]}" if suspicious_cols else "未发现异常"),
                )
            )
        except Exception as exc:  # noqa: BLE001
            checks.append(_check("financial_pit_probe", "info", True, f"探针产物不可读：{exc}"))
    else:
        checks.append(
            _check(
                "financial_pit_probe",
                "info",
                True,
                "未找到季度 PIT 探针产物（非本次训练的阻塞项）",
            )
        )

    blocked = any(c["level"] == "fail" and c["status"] == "fail" for c in checks)
    warnings = [c["detail"] for c in checks if c["level"] == "warn" and c["status"] == "fail"]
    return {
        "version": 1,
        "run_id": run_id,
        "blocked": blocked,
        "warnings": warnings,
        "checks": checks,
    }


async def ensure_table(session) -> None:
    await session.execute(text(_DDL))


async def upsert_audit(
    *, run_id: str, model_id: str | None, report: dict[str, Any] | None,
    status: str = "ready", error: str = "",
) -> None:
    async with get_session() as session:
        await ensure_table(session)
        payload = json.dumps(report or {}, ensure_ascii=False)
        await session.execute(
            text("""
                INSERT INTO qm_training_audit_reports
                    (run_id, model_id, status, blocked, report, error, generated_at, updated_at)
                VALUES (:run_id, :model_id, :status, :blocked, CAST(:report AS JSONB),
                        :error, NOW(), NOW())
                ON CONFLICT (run_id) DO UPDATE SET
                    model_id = COALESCE(EXCLUDED.model_id, qm_training_audit_reports.model_id),
                    status = EXCLUDED.status,
                    blocked = EXCLUDED.blocked,
                    report = EXCLUDED.report,
                    error = EXCLUDED.error,
                    updated_at = NOW()
            """),
            {
                "run_id": run_id,
                "model_id": model_id,
                "status": status,
                "blocked": bool((report or {}).get("blocked")),
                "report": payload,
                "error": error,
            },
        )
        await session.commit()


async def compute_and_store(run_id: str, model_id: str | None = None) -> dict[str, Any] | None:
    try:
        report = await asyncio.to_thread(build_audit, run_id)
    except Exception as exc:  # noqa: BLE001 — 审计失败不阻断训练回调
        logger.warning("[audit] %s failed: %s", run_id, exc)
        await upsert_audit(run_id=run_id, model_id=model_id, report=None, status="failed", error=str(exc))
        return None
    await upsert_audit(run_id=run_id, model_id=model_id, report=report)
    logger.info("[audit] %s done blocked=%s", run_id, report.get("blocked"))
    return report


def schedule_audit(run_id: str, model_id: str | None = None) -> None:
    task = asyncio.create_task(compute_and_store(run_id, model_id))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def fetch_audit(run_id: str) -> dict[str, Any] | None:
    async with get_session() as session:
        await ensure_table(session)
        row = (
            await session.execute(
                text("""
                    SELECT run_id, model_id, status, blocked, report, error, generated_at, updated_at
                    FROM qm_training_audit_reports WHERE run_id = :run_id
                """),
                {"run_id": run_id},
            )
        ).mappings().first()
    if not row:
        return None
    out = dict(row)
    if isinstance(out.get("report"), str):
        out["report"] = json.loads(out["report"])
    return out
