"""推理质量回填：用真实收益计算模型在生产环境的滚动 IC。

每日推理分数（engine_signal_scores）与 T+H 真实收益（特征快照 mom_ret_Nd）
join 后，按交易日截面算 Rank IC，写入 qm_model_inference_quality 表。

回填天然滞后 H 天（需等未来收益兑现）。幂等：trade_date+model_id 唯一键 upsert。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session

logger = logging.getLogger(__name__)

_DEFAULT_DATA_DIR = "/app/db/feature_snapshots"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _as_date(value: Any) -> date:
    """把 'YYYY-MM-DD' / date / datetime 统一成 date，供 DATE 列绑定。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def _rank_ic_from_scores(df: pd.DataFrame, pred_col: str = "score", label_col: str = "label") -> float:
    """单日截面 Rank IC（Spearman）。与 train.py _rank_ic_series 同算法。"""
    df = df[[pred_col, label_col]].dropna()
    if len(df) < 10:
        return float("nan")
    rp = df[pred_col].rank(method="average").to_numpy()
    rl = df[label_col].rank(method="average").to_numpy()
    if np.std(rp) == 0 or np.std(rl) == 0:
        return float("nan")
    rp_centered = rp - rp.mean()
    rl_centered = rl - rl.mean()
    denom = np.sqrt((rp_centered**2).sum() * (rl_centered**2).sum())
    if denom == 0:
        return float("nan")
    return float((rp_centered * rl_centered).sum() / denom)


def _pearson_ic_from_scores(df: pd.DataFrame, pred_col: str = "score", label_col: str = "label") -> float:
    df = df[[pred_col, label_col]].dropna()
    if len(df) < 3:
        return float("nan")
    x, y = df[pred_col].to_numpy(), df[label_col].to_numpy()
    if np.std(x) == 0 or np.std(y) == 0:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


def _resolve_parquet_path(data_dir: str, trade_date: str, market: str) -> str | None:
    """按市场解析特征快照 parquet（与推理模板 _resolve_parquet_path 一致）。"""
    market_upper = str(market or "").upper()
    _MARKET_PARQUET = {
        "HK": "model_features_hk.parquet",
        "US": "model_features_us.parquet",
        "CRYPTO": "model_features_crypto.parquet",
        "FUTURES": "model_features_futures.parquet",
    }
    if market_upper in _MARKET_PARQUET:
        p = f"{data_dir}/{_MARKET_PARQUET[market_upper]}"
        import os
        if os.path.exists(p):
            return p
    year = int(trade_date[:4])
    p = f"{data_dir}/model_features_{year}.parquet"
    import os
    if os.path.exists(p):
        return p
    return None


def _load_real_returns_snapshot(data_dir: str, trade_date: str, market: str,
                                horizon: int) -> pd.DataFrame:
    """读特征快照，构造 T 日对未来 H 日真实收益（复用 train.py 标签构造）。

    返回 DataFrame: [symbol, label] where label = 未来 H 日收益（截面 rank 前原始值）。
    """
    parquet_path = _resolve_parquet_path(data_dir, trade_date, market)
    if parquet_path is None:
        logger.warning("无法解析特征快照: date=%s market=%s", trade_date, market)
        return pd.DataFrame(columns=["symbol", "label"])

    pf = pq.ParquetFile(parquet_path)
    names = set(pf.schema_arrow.names)
    horizon_col = f"mom_ret_{horizon}d"
    cols = ["symbol", "trade_date"]
    if "instrument" in names and "symbol" not in names:
        cols = ["instrument", "trade_date"]
    if "mom_ret_1d" in names:
        cols.append("mom_ret_1d")
    if horizon_col in names:
        cols.append(horizon_col)

    df = pq.read_table(parquet_path, columns=cols).to_pandas()
    if "instrument" in df.columns and "symbol" not in df.columns:
        df = df.rename(columns={"instrument": "symbol"})
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df = df[df["trade_date"] == trade_date].copy()

    # 构造未来 H 日收益：mom_ret_{H}d 是过去收益，shift(-H) 后为未来 H 日收益
    if horizon_col in df.columns:
        df["label"] = df.groupby("symbol")[horizon_col].shift(-horizon)
    elif "mom_ret_1d" in df.columns:
        df["label"] = (
            df.groupby("symbol")["mom_ret_1d"]
            .transform(lambda s: (1 + s).rolling(horizon).apply(np.prod, raw=True) - 1)
            .shift(-horizon)
        )
    else:
        logger.warning("特征快照无 mom_ret_1d/%s，无法计算真实收益", horizon_col)
        return pd.DataFrame(columns=["symbol", "label"])

    df = df[df["label"].notna()][["symbol", "label"]].copy()
    df["symbol"] = df["symbol"].astype(str)
    return df


def _load_real_returns_quantdb(trade_date: str, horizon: int) -> pd.DataFrame:
    """从 QuantDB 后复权日线构造 T 日 → 未来 H 个交易日的真实收益。

    A 股训练/推理已改为直读 QuantDB，feature_snapshots 目录在新部署里是空的，
    旧路径取不到数据时用这里兜底。symbol 统一转 prefix 口径，与 PG 中的
    推理分数（engine_signal_scores）对齐。
    """
    import glob as _glob
    import os as _os
    from backend.shared.stock_utils import StockCodeUtil

    data_dir = _os.getenv("QM_QUANTDB_DATA_DIR", "/data/quantdb")
    base = _os.path.join(data_dir, "1_kline_data", "daily_backward")
    if not _os.path.isdir(base):
        base = _os.path.join(data_dir, "1_kline_data", "daily_unadjusted")
    if not _os.path.isdir(base):
        logger.warning("QuantDB 日线目录不存在: dir=%s", data_dir)
        return pd.DataFrame(columns=["symbol", "label"])

    start = str(trade_date).replace("-", "")
    dts = sorted(
        p.split("=", 1)[1]
        for p in _os.listdir(base)
        if p.startswith("dt=") and p.split("=", 1)[1].isdigit() and p.split("=", 1)[1] >= start
    )
    if len(dts) <= horizon:
        logger.warning(
            "QuantDB 无足够的未来交易日: date=%s horizon=%d available=%d",
            trade_date,
            horizon,
            len(dts),
        )
        return pd.DataFrame(columns=["symbol", "label"])

    d0, d1 = dts[0], dts[horizon]
    frames = []
    for d in (d0, d1):
        files = sorted(_glob.glob(_os.path.join(base, f"dt={d}", "*.parquet")))
        if not files:
            return pd.DataFrame(columns=["symbol", "label"])
        frames.append(
            pd.concat(
                [pq.read_table(f, columns=["symbol", "close"]).to_pandas() for f in files]
            ).assign(_dt=d)
        )

    piv = pd.concat(frames).pivot_table(
        index="symbol", columns="_dt", values="close", aggfunc="last"
    )
    if d0 not in piv.columns or d1 not in piv.columns:
        return pd.DataFrame(columns=["symbol", "label"])

    out = ((piv[d1] / piv[d0]) - 1.0).dropna().rename("label").reset_index()
    out["symbol"] = out["symbol"].map(StockCodeUtil.to_prefix)
    return out[["symbol", "label"]]


def _load_real_returns(data_dir: str, trade_date: str, market: str,
                       horizon: int) -> pd.DataFrame:
    """真实收益入口：优先 feature_snapshots，取不到时回退 QuantDB（A 股）。"""
    df = _load_real_returns_snapshot(data_dir, trade_date, market, horizon)
    if not df.empty:
        return df
    if str(market or "").upper() in ("CN", "A", ""):
        logger.info(
            "特征快照无 %s/%s 的真实收益，回退 QuantDB 日线", trade_date, market
        )
        return _load_real_returns_quantdb(trade_date, horizon)
    return df


class InferenceQualityBackfill:
    """推理质量回填：真实 IC 计算与入库。"""

    async def ensure_tables(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS qm_model_inference_quality (
              model_id TEXT NOT NULL,
              trade_date DATE NOT NULL,
              signals_count INTEGER NOT NULL DEFAULT 0,
              coverage REAL,
              ic REAL,
              rank_ic REAL,
              horizon_days INTEGER NOT NULL DEFAULT 5,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              PRIMARY KEY (model_id, trade_date)
            );
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_qm_model_inference_quality_model_date
              ON qm_model_inference_quality (model_id, trade_date DESC);
            """,
        ]
        async with get_session() as session:
            for stmt in statements:
                await session.execute(text(stmt))

    async def _get_scores_for_date(self, tenant_id: str, user_id: str, model_id: str,
                                   trade_date: str) -> pd.DataFrame | None:
        """读某模型某日推理分数（engine_signal_scores via run 定位）。"""
        # data_trade_date 是 DATE 列，asyncpg 不接受字符串，统一转成 date
        trade_date = _as_date(trade_date)
        async with get_session(read_only=True) as session:
            run_row = (
                await session.execute(
                    text(
                        """
                        SELECT r.run_id, r.signals_count
                        FROM qm_model_inference_runs r
                        WHERE r.tenant_id = :tenant_id
                          AND r.user_id = :user_id
                          AND r.model_id = :model_id
                          AND r.data_trade_date = :trade_date
                          AND r.status = 'completed'
                          AND r.signals_count > 0
                        ORDER BY r.created_at DESC
                        LIMIT 1
                        """
                    ),
                    {"tenant_id": tenant_id, "user_id": user_id, "model_id": model_id, "trade_date": trade_date},
                )
            ).mappings().first()
            if not run_row:
                return None
            run_id = run_row["run_id"]
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT symbol, fusion_score
                        FROM engine_signal_scores
                        WHERE run_id = :run_id
                          AND tenant_id = :tenant_id
                          AND user_id = :user_id
                        """
                    ),
                    {"run_id": run_id, "tenant_id": tenant_id, "user_id": user_id},
                )
            ).mappings().all()
        if not rows:
            return None
        df = pd.DataFrame([dict(r) for r in rows])
        df = df.rename(columns={"fusion_score": "score"})
        # 统一成 prefix 口径（PG 层），与 QuantDB 侧经 StockCodeUtil 转换后的 symbol 对齐
        from backend.shared.stock_utils import StockCodeUtil

        df["symbol"] = df["symbol"].map(lambda s: StockCodeUtil.to_prefix(str(s)))
        return df

    async def backfill_date(self, *, tenant_id: str = "default", user_id: str = "",
                            model_id: str, trade_date: str, market: str = "CN",
                            horizon: int = 5, data_dir: str = _DEFAULT_DATA_DIR) -> dict[str, Any]:
        """回填单个 (model_id, trade_date) 的质量数据。幂等 upsert。"""
        # 落库用 date，parquet 路径/日志继续用 YYYY-MM-DD 字符串
        trade_date_db = _as_date(trade_date)
        scores_df = await self._get_scores_for_date(tenant_id, user_id, model_id, trade_date)
        if scores_df is None or scores_df.empty:
            return {"model_id": model_id, "trade_date": trade_date, "status": "no_scores", "rank_ic": None}

        returns_df = _load_real_returns(data_dir, trade_date, market, horizon)
        if returns_df.empty:
            return {"model_id": model_id, "trade_date": trade_date, "status": "no_returns", "rank_ic": None}

        merged = scores_df.merge(returns_df, on="symbol", how="inner")
        if len(merged) < 10:
            return {"model_id": model_id, "trade_date": trade_date, "status": "too_few",
                    "rank_ic": None, "matched": int(len(merged))}

        rank_ic = _rank_ic_from_scores(merged)
        ic = _pearson_ic_from_scores(merged)
        coverage = len(merged) / len(scores_df) if len(scores_df) else 0.0

        now = _now_utc()
        async with get_session() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO qm_model_inference_quality (
                      model_id, trade_date, signals_count, coverage, ic, rank_ic,
                      horizon_days, created_at, updated_at
                    ) VALUES (
                      :model_id, :trade_date, :signals_count, :coverage, :ic, :rank_ic,
                      :horizon_days, :created_at, :created_at
                    )
                    ON CONFLICT (model_id, trade_date) DO UPDATE SET
                      signals_count = EXCLUDED.signals_count,
                      coverage = EXCLUDED.coverage,
                      ic = EXCLUDED.ic,
                      rank_ic = EXCLUDED.rank_ic,
                      horizon_days = EXCLUDED.horizon_days,
                      updated_at = EXCLUDED.updated_at
                    """
                ),
                {
                    "model_id": model_id,
                    "trade_date": trade_date_db,
                    "signals_count": int(len(scores_df)),
                    "coverage": float(coverage) if np.isfinite(coverage) else None,
                    "ic": float(ic) if np.isfinite(ic) else None,
                    "rank_ic": float(rank_ic) if np.isfinite(rank_ic) else None,
                    "horizon_days": int(horizon),
                    "created_at": now,
                },
            )
            await session.commit()

        return {"model_id": model_id, "trade_date": trade_date, "status": "ok",
                "rank_ic": float(rank_ic) if np.isfinite(rank_ic) else None,
                "ic": float(ic) if np.isfinite(ic) else None,
                "matched": int(len(merged))}

    async def backfill_recent(self, *, tenant_id: str = "default", user_id: str = "",
                              model_id: str, market: str = "CN", horizon: int = 5,
                              days: int = 60, data_dir: str = _DEFAULT_DATA_DIR) -> dict[str, Any]:
        """回填某模型最近 N 天（截至 H 天前的交易日）的质量数据。"""
        # 收集该模型最近的推理交易日（已完成、有信号）
        async with get_session(read_only=True) as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT DISTINCT ON (data_trade_date) data_trade_date
                        FROM qm_model_inference_runs
                        WHERE tenant_id = :tenant_id
                          AND user_id = :user_id
                          AND model_id = :model_id
                          AND status = 'completed'
                          AND signals_count > 0
                        ORDER BY data_trade_date DESC
                        LIMIT :limit
                        """
                    ),
                    {"tenant_id": tenant_id, "user_id": user_id, "model_id": model_id, "limit": int(days)},
                )
            ).mappings().all()
        dates = [str(r["data_trade_date"])[:10] for r in rows]

        results = []
        for d in dates:
            res = await self.backfill_date(
                tenant_id=tenant_id, user_id=user_id, model_id=model_id,
                trade_date=d, market=market, horizon=horizon, data_dir=data_dir,
            )
            results.append(res)
        ok = [r for r in results if r.get("status") == "ok"]
        return {
            "model_id": model_id,
            "processed": len(dates),
            "ok": len(ok),
            "rank_ic_mean": float(np.mean([r["rank_ic"] for r in ok if r.get("rank_ic") is not None])) if ok else None,
        }

    async def refresh_ensemble_weights(self, model_dir) -> dict[str, Any]:
        """为单个融合模型刷新 recent_ic 动态权重，写 weight_snapshot.json。

        权重算法：近30日每源模型生产 rank_ic 指数衰减（exp(-d/10)）加权，
        负值截 0、归一化；全零回退 config 静态权重。覆盖率<30% 的模型 ×0.5。
        """
        import json as _json
        from pathlib import Path as _Path

        model_dir = _Path(model_dir)
        config_path = model_dir / "ensemble_config.json"
        if not config_path.exists():
            return {"status": "not_ensemble"}

        with open(config_path, encoding="utf-8") as f:
            config = _json.load(f)
        if str(config.get("weight_strategy") or "") != "recent_ic":
            return {"status": "not_recent_ic", "strategy": config.get("weight_strategy")}

        models = config.get("models") or []
        # 收集各源模型近30日 rank_ic（滞后 5 日，取真实收益兑现后的）
        weights_raw: dict[str, float] = {}
        coverage_map: dict[str, float] = {}
        async with get_session(read_only=True) as session:
            for m in models:
                mid = str(m.get("model_id") or "")
                if not mid:
                    continue
                rows = (
                    await session.execute(
                        text(
                            """
                            SELECT trade_date, rank_ic, coverage
                            FROM qm_model_inference_quality
                            WHERE model_id = :mid
                              AND rank_ic IS NOT NULL
                            ORDER BY trade_date DESC
                            LIMIT 30
                            """
                        ),
                        {"mid": mid},
                    )
                ).mappings().all()
                if not rows:
                    continue
                # 指数衰减加权：最近的权重最大
                total = 0.0
                w_sum = 0.0
                coverages = []
                for i, r in enumerate(rows):
                    decay = float(np.exp(-i / 10.0))
                    ic = float(r["rank_ic"]) if r["rank_ic"] is not None else 0.0
                    total += ic * decay
                    w_sum += decay
                    if r["coverage"] is not None:
                        coverages.append(float(r["coverage"]))
                mean_ic = total / w_sum if w_sum > 0 else 0.0
                if mean_ic > 0:
                    weights_raw[mid] = mean_ic
                    coverage_map[mid] = float(np.mean(coverages)) if coverages else 1.0

        if not weights_raw:
            # 无生产数据 → 回退 config 静态权重
            static = {str(m.get("model_id")): float(m.get("weight") or 0.0) for m in models if m.get("model_id")}
            static_w = {k: v for k, v in static.items() if v > 0}
            tot = sum(static_w.values()) or 1.0
            snapshot = {k: v / tot for k, v in static_w.items()}
            snapshot["_updated_at"] = _now_utc().isoformat()
            (model_dir / "weight_snapshot.json").write_text(
                _json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return {"status": "fallback_static", "model_id": str(model_dir.name)}

        # 覆盖率惩罚
        for mid in list(weights_raw.keys()):
            if coverage_map.get(mid, 1.0) < 0.3:
                weights_raw[mid] *= 0.5

        tot_w = sum(weights_raw.values())
        if tot_w <= 0:
            return {"status": "zero_weight", "model_id": str(model_dir.name)}

        normalized = {k: v / tot_w for k, v in weights_raw.items()}
        normalized["_updated_at"] = _now_utc().isoformat()
        (model_dir / "weight_snapshot.json").write_text(
            _json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {
            "status": "ok",
            "model_id": str(model_dir.name),
            "weights": {k: round(v, 4) for k, v in normalized.items() if k != "_updated_at"},
        }


inference_quality_backfill = InferenceQualityBackfill()
