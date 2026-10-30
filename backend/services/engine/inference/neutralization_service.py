"""推理中性化服务编排（同步 DB；批量/实时/回填共用）。

设计见 ``docs/推理中性化处理前端_生产级设计_20261030.md``。

关键约束：

- 只写影子表（``qm_neutral_runs`` / ``qm_neutral_scores``），**不改**
  ``engine_signal_scores`` 与任何下单链路；
- 幂等：同一 (model, data_trade_date, config_version) 重算会先删旧行再写；
- 降级不静默：控制变量缺失/样本不足/矩阵病态/覆盖率不足 → ``degraded``，
  分数仍落库但带 flag，enforce 消费方必须拒绝使用；
- 前视硬校验：控制变量日期不得晚于信号数据日。
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from collections.abc import Iterable

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from backend.shared.neutralization import (
    DEFAULT_CONTROLS,
    NeutralizationConfig,
    load_controls,
    neutralize_scores,
)
from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)

VALID_MODES = {"off", "shadow", "enforce"}
SCOPE_FULL = "full_market"
SCOPE_HOT = "hot_set"


def _sync_db_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    if "+asyncpg" in url:
        url = url.replace("+asyncpg", "+psycopg2")
    if not url.startswith("postgresql"):
        url = "postgresql+psycopg2://postgres:password@localhost:5432/quantmind"
    return url


_engine = None
_Session: sessionmaker | None = None


def get_session_factory() -> sessionmaker:
    global _engine, _Session
    if _Session is None:
        _engine = create_engine(_sync_db_url(), pool_pre_ping=True, future=True)
        _Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
    return _Session


@dataclass(frozen=True)
class NeutralizationOutcome:
    neutral_run_id: str
    model_id: str
    data_trade_date: date
    status: str
    n_symbols: int
    n_total: int
    coverage: float
    reason: str | None
    config_version: str


def _parse_json(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _resolve_control_date(base: date, lag: int, session: Session) -> date:
    """按 control_lag 回退到最近一个存在分区的交易日。"""
    if lag <= 0:
        return base
    from backend.shared.neutralization.controls import partition_path  # noqa: PLC0415

    candidate = base
    for _ in range(10):
        candidate = candidate - timedelta(days=1)
        if partition_path(candidate).exists():
            return candidate
        if candidate <= base - timedelta(days=10):
            break
    raise FileNotFoundError(f"control_lag={lag} 找不到可用控制变量分区（base={base}）")


def load_settings(
    session: Session,
    tenant_id: str,
    user_id: str,
    model_id: str,
    market: str = "CN",
) -> dict:
    row = (
        session.execute(
            text(
                """
            SELECT mode, universe_id, controls, min_n, winsor_pct, control_lag,
                   config_version, updated_by, updated_at
            FROM qm_neutralization_settings
            WHERE tenant_id=:t AND user_id=:u AND model_id=:m AND market=:mk
            """
            ),
            {"t": tenant_id, "u": user_id, "m": model_id, "mk": market},
        )
        .mappings()
        .first()
    )
    if row is None:
        cfg = NeutralizationConfig()
        return {
            "mode": "off",
            "universe_id": cfg.universe_id,
            "controls": list(cfg.controls),
            "min_n": cfg.min_n,
            "winsor_pct": cfg.winsor_pct,
            "control_lag": cfg.control_lag,
            "config_version": cfg.config_version,
            "updated_by": "",
            "updated_at": None,
            "exists": False,
        }
    controls = _parse_json(row["controls"]) or list(DEFAULT_CONTROLS)
    cfg = NeutralizationConfig(
        controls=tuple(controls),
        universe_id=row["universe_id"],
        min_n=int(row["min_n"]),
        winsor_pct=float(row["winsor_pct"]),
        control_lag=int(row["control_lag"]),
    )
    return {
        "mode": row["mode"],
        "universe_id": cfg.universe_id,
        "controls": list(cfg.controls),
        "min_n": cfg.min_n,
        "winsor_pct": cfg.winsor_pct,
        "control_lag": cfg.control_lag,
        "config_version": cfg.config_version,
        "updated_by": row["updated_by"],
        "updated_at": row["updated_at"],
        "exists": True,
    }


def upsert_settings(
    session: Session,
    *,
    tenant_id: str,
    user_id: str,
    model_id: str,
    market: str = "CN",
    mode: str,
    universe_id: str = "cn_all_tradable",
    controls: Iterable[str] = DEFAULT_CONTROLS,
    min_n: int = 300,
    winsor_pct: float = 0.01,
    control_lag: int = 0,
    operator: str = "",
) -> dict:
    if mode not in VALID_MODES:
        raise ValueError(f"非法模式: {mode}")
    cfg = NeutralizationConfig(
        controls=tuple(controls),
        universe_id=universe_id,
        min_n=int(min_n),
        winsor_pct=float(winsor_pct),
        control_lag=int(control_lag),
    )
    previous = load_settings(session, tenant_id, user_id, model_id, market)
    session.execute(
        text(
            """
            INSERT INTO qm_neutralization_settings (
                tenant_id, user_id, model_id, market, mode, universe_id, controls,
                min_n, winsor_pct, control_lag, config_version, updated_by,
                created_at, updated_at
            ) VALUES (
                :t, :u, :m, :mk, :mode, :universe, CAST(:controls AS jsonb),
                :min_n, :winsor, :lag, :cv, :op, NOW(), NOW()
            )
            ON CONFLICT (tenant_id, user_id, model_id, market) DO UPDATE SET
                mode=EXCLUDED.mode,
                universe_id=EXCLUDED.universe_id,
                controls=EXCLUDED.controls,
                min_n=EXCLUDED.min_n,
                winsor_pct=EXCLUDED.winsor_pct,
                control_lag=EXCLUDED.control_lag,
                config_version=EXCLUDED.config_version,
                updated_by=EXCLUDED.updated_by,
                updated_at=NOW()
            """
        ),
        {
            "t": tenant_id,
            "u": user_id,
            "m": model_id,
            "mk": market,
            "mode": mode,
            "universe": universe_id,
            "controls": json.dumps(list(controls), ensure_ascii=False),
            "min_n": min_n,
            "winsor": winsor_pct,
            "lag": control_lag,
            "cv": cfg.config_version,
            "op": operator,
        },
    )
    session.execute(
        text(
            """
            INSERT INTO qm_neutralization_audit
                (tenant_id, user_id, model_id, action, from_value, to_value, operator)
            VALUES (:t, :u, :m, 'set_mode', CAST(:before AS jsonb),
                    CAST(:after AS jsonb), :op)
            """
        ),
        {
            "t": tenant_id,
            "u": user_id,
            "m": model_id,
            "before": json.dumps(previous, default=str, ensure_ascii=False),
            "after": json.dumps(
                {
                    "mode": mode,
                    "universe_id": universe_id,
                    "controls": list(controls),
                    "min_n": min_n,
                    "winsor_pct": winsor_pct,
                    "control_lag": control_lag,
                    "config_version": cfg.config_version,
                },
                ensure_ascii=False,
            ),
            "op": operator,
        },
    )
    session.commit()
    return load_settings(session, tenant_id, user_id, model_id, market)


def _normalize_symbol(raw: Any) -> str:
    return StockCodeUtil.to_prefix(str(raw))


def _load_scores(
    session: Session, run_id: str, tenant_id: str, user_id: str
) -> pd.Series:
    rows = session.execute(
        text(
            """
            SELECT symbol, fusion_score
            FROM engine_signal_scores
            WHERE run_id=:r AND tenant_id=:t AND user_id=:u
              AND fusion_score IS NOT NULL
            """
        ),
        {"r": run_id, "t": tenant_id, "u": user_id},
    ).all()
    if not rows:
        raise ValueError(f"run {run_id} 无可用分数")
    records: dict[str, float] = {}
    for symbol, score in rows:
        key = _normalize_symbol(symbol)
        # 同一标的多种代码格式并存时保留绝对值更大的分数（信息量更足）
        if key not in records or abs(float(score)) > abs(records[key]):
            records[key] = float(score)
    return pd.Series(records, name="raw_score")


def _resolve_run_context(session: Session, run_id: str) -> tuple[str, str, str, date]:
    row = (
        session.execute(
            text(
                """
            SELECT tenant_id, user_id, model_id, data_trade_date
            FROM qm_model_inference_runs WHERE run_id=:r
            """
            ),
            {"r": run_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        # 实时链路只在 engine_feature_runs 有 run 行（历史原因无 inference_runs）
        row = (
            session.execute(
                text(
                    """
                SELECT tenant_id, user_id, model_name AS model_id,
                       trade_date AS data_trade_date
                FROM engine_feature_runs WHERE run_id=:r
                """
                ),
                {"r": run_id},
            )
            .mappings()
            .first()
        )
    if row is None:
        raise ValueError(f"未知推理 run: {run_id}")
    return row["tenant_id"], row["user_id"], row["model_id"], row["data_trade_date"]


def compute_for_run(
    run_id: str,
    *,
    tenant_id: str | None = None,
    user_id: str | None = None,
    model_id: str | None = None,
    data_trade_date: date | None = None,
    universe_scope: str = SCOPE_FULL,
    force: bool = False,
) -> NeutralizationOutcome | None:
    """为一次推理 run 生成中性化影子结果。

    模型未配置或模式为 ``off`` 时返回 ``None``（不写任何东西）。``force=True``
    供回填/重算使用：忽略模式，但仍写影子表。
    """
    SessionFactory = get_session_factory()
    with SessionFactory() as session:
        tid, uid = tenant_id, user_id
        mid, dtd = model_id, data_trade_date
        if not tid or not uid or not mid or dtd is None:
            r_tid, r_uid, r_mid, r_dtd = _resolve_run_context(session, run_id)
            tid = tid or r_tid
            uid = uid or r_uid
            mid = mid or r_mid
            dtd = dtd or r_dtd
        # 推理 run 的归属账号（实时链路可能是系统账号 admin）
        run_tid, run_uid = tid, uid

        settings = load_settings(session, tid, uid, mid)
        # 找不到本账号配置时回退到模型属主：实时链路的 run 归属常是系统账号，
        # 而中性化配置挂在模型属主名下。写侧也落属主，治理面板才看得到实时影子结果。
        if not settings["exists"]:
            owner = (
                session.execute(
                    text(
                        """
                        SELECT tenant_id, user_id FROM qm_user_models
                        WHERE model_id=:m
                        ORDER BY (user_id = :u) DESC, updated_at DESC
                        LIMIT 1
                        """
                    ),
                    {"m": mid, "u": uid},
                )
                .mappings()
                .first()
            )
            if owner and (owner["tenant_id"] != tid or owner["user_id"] != uid):
                candidate = load_settings(
                    session, owner["tenant_id"], owner["user_id"], mid
                )
                if candidate["exists"]:
                    settings = candidate
                    tid, uid = owner["tenant_id"], owner["user_id"]

        if not force and settings["mode"] == "off":
            return None

        cfg = NeutralizationConfig(
            controls=tuple(settings["controls"]),
            universe_id=settings["universe_id"],
            min_n=int(settings["min_n"]),
            winsor_pct=float(settings["winsor_pct"]),
            control_lag=int(settings["control_lag"]),
        )

        # 实时热集每 15 秒发布一次；同一模型/日期/口径 60 秒内只算一次，
        # 避免每轮都 delete+insert 并堆积诊断行（force=True 的回填/重算不受限）。
        if universe_scope == SCOPE_HOT and not force:
            recent = session.execute(
                text(
                    """
                    SELECT 1 FROM qm_neutral_runs
                    WHERE tenant_id=:t AND user_id=:u AND model_id=:m
                      AND data_trade_date=:dtd AND universe_scope='hot_set'
                      AND config_version=:cv
                      AND created_at > NOW() - INTERVAL '60 seconds'
                    LIMIT 1
                    """
                ),
                {
                    "t": tid,
                    "u": uid,
                    "m": mid,
                    "dtd": dtd,
                    "cv": cfg.config_version,
                },
            ).first()
            if recent:
                return None

        scores = _load_scores(session, run_id, run_tid, run_uid)
        neutral_run_id = f"nl_{dtd:%Y%m%d}_{uuid.uuid4().hex[:8]}"
        try:
            control_date = _resolve_control_date(dtd, cfg.control_lag, session)
            snapshot = load_controls(control_date, signal_data_date=dtd)
            result = neutralize_scores(scores, snapshot.frame, cfg)
        except Exception as exc:  # noqa: BLE001 - 降级为 failed 诊断行，不阻断推理
            logger.error("[NL] %s run=%s 计算失败: %s", mid, run_id, exc)
            session.execute(
                text(
                    """
                    INSERT INTO qm_neutral_runs (
                        neutral_run_id, tenant_id, user_id, model_id, source_run_id,
                        data_trade_date, universe_id, universe_scope, control_lag,
                        config_version, status, n_symbols, n_total, coverage, reason,
                        created_at
                    ) VALUES (
                        :nid, :t, :u, :m, :src, :dtd, :universe, :scope, :lag, :cv,
                        'failed', 0, :nt, 0, :reason, NOW()
                    )
                    """
                ),
                {
                    "nid": neutral_run_id,
                    "t": tid,
                    "u": uid,
                    "m": mid,
                    "src": run_id,
                    "dtd": dtd,
                    "universe": cfg.universe_id,
                    "scope": universe_scope,
                    "lag": cfg.control_lag,
                    "cv": cfg.config_version,
                    "nt": int(len(scores)),
                    "reason": str(exc)[:500],
                },
            )
            session.commit()
            return NeutralizationOutcome(
                neutral_run_id=neutral_run_id,
                model_id=mid,
                data_trade_date=dtd,
                status="failed",
                n_symbols=0,
                n_total=int(len(scores)),
                coverage=0.0,
                reason=str(exc)[:500],
                config_version=cfg.config_version,
            )

        diag = result.diagnostics
        status = result.status
        reason = result.reason
        session.execute(
            text(
                """
                INSERT INTO qm_neutral_runs (
                    neutral_run_id, tenant_id, user_id, model_id, source_run_id,
                    data_trade_date, universe_id, universe_scope, control_lag,
                    config_version, status, n_symbols, n_total, coverage, r2,
                    r2_pass1, betas, condition_number, exposure, reason, created_at
                ) VALUES (
                    :nid, :t, :u, :m, :src, :dtd, :universe, :scope, :lag, :cv,
                    :status, :n, :nt, :cov, :r2, :r2p1, CAST(:betas AS jsonb),
                    :cond, CAST(:exposure AS jsonb), :reason, NOW()
                )
                """
            ),
            {
                "nid": neutral_run_id,
                "t": tid,
                "u": uid,
                "m": mid,
                "src": run_id,
                "dtd": dtd,
                "universe": cfg.universe_id,
                "scope": universe_scope,
                "lag": cfg.control_lag,
                "cv": cfg.config_version,
                "status": status,
                "n": int(diag.get("n_symbols") or 0),
                "nt": int(diag.get("n_total") or 0),
                "cov": float(diag.get("coverage") or 0.0),
                "r2": diag.get("r2"),
                "r2p1": diag.get("r2_pass1"),
                "betas": json.dumps(diag.get("betas"), ensure_ascii=False),
                "cond": diag.get("condition_number"),
                "exposure": json.dumps(diag.get("exposure"), ensure_ascii=False),
                "reason": reason,
            },
        )

        # 幂等：同一口径版本重算时先清旧行
        session.execute(
            text(
                """
                DELETE FROM qm_neutral_scores
                WHERE tenant_id=:t AND user_id=:u AND model_id=:m
                  AND data_trade_date=:dtd AND config_version=:cv
                """
            ),
            {"t": tid, "u": uid, "m": mid, "dtd": dtd, "cv": cfg.config_version},
        )

        rows: list[dict[str, Any]] = []
        neutral = result.neutral_score
        for symbol, raw in scores.items():
            value = neutral.get(symbol)
            if value is None or pd.isna(value):
                flag = (
                    "missing_controls" if symbol in snapshot.frame.index else "degraded"
                )
                value = None
            else:
                flag = "ok" if status == "ok" else "degraded"
            control_row = (
                snapshot.frame.loc[symbol] if symbol in snapshot.frame.index else None
            )
            rows.append(
                {
                    "t": tid,
                    "u": uid,
                    "m": mid,
                    "dtd": dtd,
                    "sym": symbol,
                    "neutral": None if value is None else float(value),
                    "raw": float(raw),
                    "log_mv": None
                    if control_row is None
                    else float(control_row["log_mv"]),
                    "beta60": None
                    if control_row is None
                    else float(control_row["beta60"]),
                    "mom20": None
                    if control_row is None
                    else float(control_row["mom20"]),
                    "flag": flag,
                    "cv": cfg.config_version,
                    "nid": neutral_run_id,
                }
            )
        if rows:
            session.execute(
                text(
                    """
                    INSERT INTO qm_neutral_scores (
                        tenant_id, user_id, model_id, data_trade_date, symbol,
                        neutral_score, raw_score, log_mv, beta60, mom20, flag,
                        config_version, neutral_run_id, created_at
                    ) VALUES (
                        :t, :u, :m, :dtd, :sym, :neutral, :raw, :log_mv, :beta60,
                        :mom20, :flag, :cv, :nid, NOW()
                    )
                    ON CONFLICT (tenant_id, user_id, model_id, data_trade_date,
                                 symbol, config_version)
                    DO UPDATE SET
                        neutral_score=EXCLUDED.neutral_score,
                        raw_score=EXCLUDED.raw_score,
                        log_mv=EXCLUDED.log_mv,
                        beta60=EXCLUDED.beta60,
                        mom20=EXCLUDED.mom20,
                        flag=EXCLUDED.flag,
                        neutral_run_id=EXCLUDED.neutral_run_id,
                        created_at=NOW()
                    """
                ),
                rows,
            )
        session.commit()

    outcome = NeutralizationOutcome(
        neutral_run_id=neutral_run_id,
        model_id=mid,
        data_trade_date=dtd,
        status=status,
        n_symbols=int(diag.get("n_symbols") or 0),
        n_total=int(diag.get("n_total") or 0),
        coverage=float(diag.get("coverage") or 0.0),
        reason=reason,
        config_version=cfg.config_version,
    )
    logger.info(
        "[NL] %s run=%s status=%s n=%s/%s coverage=%.3f reason=%s",
        mid,
        run_id,
        status,
        outcome.n_symbols,
        outcome.n_total,
        outcome.coverage,
        reason,
    )
    return outcome


def list_diagnostics(
    session: Session,
    *,
    tenant_id: str,
    user_id: str,
    model_id: str,
    limit: int = 60,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[dict]:
    clauses = ["tenant_id=:t", "user_id=:u", "model_id=:m"]
    params: dict[str, Any] = {
        "t": tenant_id,
        "u": user_id,
        "m": model_id,
        "limit": int(limit),
    }
    if start_date is not None:
        clauses.append("data_trade_date >= :start")
        params["start"] = start_date
    if end_date is not None:
        clauses.append("data_trade_date <= :end")
        params["end"] = end_date
    rows = (
        session.execute(
            text(
                f"""
            SELECT neutral_run_id, source_run_id, data_trade_date, universe_scope,
                   status, n_symbols, n_total, coverage, r2, r2_pass1,
                   condition_number, exposure, reason, config_version, created_at
            FROM qm_neutral_runs
            WHERE {" AND ".join(clauses)}
            ORDER BY data_trade_date DESC, created_at DESC
            LIMIT :limit
            """
            ),
            params,
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def list_scores(
    session: Session,
    *,
    tenant_id: str,
    user_id: str,
    model_id: str,
    data_trade_date: date,
    limit: int = 200,
    offset: int = 0,
) -> tuple[list[dict], int]:
    params = {
        "t": tenant_id,
        "u": user_id,
        "m": model_id,
        "dtd": data_trade_date,
    }
    # 只读最近一次计算（同一日期可能有多个口径版本，混读会让分位不可比）
    latest_filter = """
        neutral_run_id = (
            SELECT neutral_run_id FROM qm_neutral_runs
            WHERE tenant_id=:t AND user_id=:u AND model_id=:m
              AND data_trade_date=:dtd
            ORDER BY created_at DESC
            LIMIT 1
        )
    """
    total = session.execute(
        text(
            f"""
            SELECT COUNT(*) FROM qm_neutral_scores
            WHERE tenant_id=:t AND user_id=:u AND model_id=:m AND data_trade_date=:dtd
              AND {latest_filter}
            """
        ),
        params,
    ).scalar_one()
    rows = (
        session.execute(
            text(
                f"""
            SELECT symbol, raw_score, neutral_score, flag, config_version
            FROM qm_neutral_scores
            WHERE tenant_id=:t AND user_id=:u AND model_id=:m AND data_trade_date=:dtd
              AND {latest_filter}
            ORDER BY neutral_score DESC NULLS LAST
            LIMIT :limit OFFSET :offset
            """
            ),
            {**params, "limit": int(limit), "offset": int(offset)},
        )
        .mappings()
        .all()
    )
    records = [dict(r) for r in rows]
    if records:
        # 明细展示需要股票名称；stocks 表是后缀式，转成前缀回填
        suffixes = [StockCodeUtil.to_suffix(r["symbol"]) for r in records]
        name_rows = session.execute(
            text("SELECT symbol, name FROM stocks WHERE symbol = ANY(:syms)"),
            {"syms": suffixes},
        ).all()
        name_map = {StockCodeUtil.to_prefix(sym): name for sym, name in name_rows}
        for record in records:
            record["name"] = name_map.get(record["symbol"])
    return records, int(total)


def list_audit(
    session: Session,
    *,
    tenant_id: str,
    user_id: str | None = None,
    model_id: str | None = None,
    limit: int = 100,
) -> list[dict]:
    clauses = ["tenant_id=:t"]
    params: dict[str, Any] = {"t": tenant_id, "limit": int(limit)}
    if user_id:
        clauses.append("user_id=:u")
        params["u"] = user_id
    if model_id:
        clauses.append("model_id=:m")
        params["m"] = model_id
    rows = (
        session.execute(
            text(
                f"""
            SELECT id, user_id, model_id, action, from_value, to_value, operator, created_at
            FROM qm_neutralization_audit
            WHERE {" AND ".join(clauses)}
            ORDER BY created_at DESC
            LIMIT :limit
            """
            ),
            params,
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def enabled_models(session: Session, *, tenant_id: str | None = None) -> list[dict]:
    """列出非 off 的模型配置（供扫描器/回填）。"""
    clause = "WHERE mode <> 'off'"
    params: dict[str, Any] = {}
    if tenant_id:
        clause += " AND tenant_id=:t"
        params["t"] = tenant_id
    rows = (
        session.execute(
            text(
                f"""
            SELECT tenant_id, user_id, model_id, market, mode, config_version
            FROM qm_neutralization_settings
            {clause}
            ORDER BY updated_at DESC
            """
            ),
            params,
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def latest_run_id(
    session: Session,
    *,
    tenant_id: str,
    user_id: str,
    model_id: str,
    data_trade_date: date,
) -> str | None:
    """某模型某数据日最近一次推理 run（重算时自动定位）。"""
    value = session.execute(
        text(
            """
            SELECT run_id FROM qm_model_inference_runs
            WHERE tenant_id=:t AND user_id=:u AND model_id=:m
              AND data_trade_date=:dtd AND status='completed'
            ORDER BY updated_at DESC NULLS LAST
            LIMIT 1
            """
        ),
        {"t": tenant_id, "u": user_id, "m": model_id, "dtd": data_trade_date},
    ).scalar()
    return str(value) if value else None


def overview(
    session: Session,
    *,
    tenant_id: str,
    user_id: str,
) -> list[dict]:
    """治理面板：所有已配置模型的当前模式 + 最近一次诊断。"""
    rows = (
        session.execute(
            text(
                """
            SELECT s.model_id, s.market, s.mode, s.universe_id, s.controls,
                   s.min_n, s.winsor_pct, s.control_lag, s.config_version,
                   s.updated_by, s.updated_at,
                   r.status AS last_status, r.data_trade_date, r.coverage,
                   r.r2, r.reason, r.universe_scope, r.created_at AS last_computed_at
            FROM qm_neutralization_settings s
            LEFT JOIN LATERAL (
                SELECT status, data_trade_date, coverage, r2, reason,
                       universe_scope, created_at
                FROM qm_neutral_runs r
                WHERE r.tenant_id=s.tenant_id AND r.user_id=s.user_id
                  AND r.model_id=s.model_id
                ORDER BY data_trade_date DESC, created_at DESC
                LIMIT 1
            ) r ON TRUE
            WHERE s.tenant_id=:t AND s.user_id=:u
            ORDER BY s.updated_at DESC
            """
            ),
            {"t": tenant_id, "u": user_id},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]
