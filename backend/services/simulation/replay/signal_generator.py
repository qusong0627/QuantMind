"""回放信号直读器：信号 = 模型 pred.parquet 的历史分数，无预生成步骤。

信号来源是模型目录下的 pred.parquet（训练产出 + 每日推理回写的全量历史
分数，与生产个股分数曲线/推理覆盖同一数据源）。创建回放会话不再后台
「生成信号」，会话创建即就绪；每步推演时由 ReplaySignalLoader 按需读取
上一数据日的分数（进程内缓存，同一模型只解析一次全量文件）。

T+1 偏移说明：
  pred.parquet 里的 trade_date 是「数据日 D」（用 D 日的行情/特征推理）。
  信号在 D+1（下一个交易日）才生效。
  load_signals_for_date(T) 实际读取 T 的上一交易日（数据日）的分数 ——
  与 engine_signal_scores 的语义完全一致，无前视偏差。
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.simulation.services.local_market_data import (
    get_local_market_data,
)
from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _resolve_model_dir(model_id: str | None = None) -> Path:
    """定位模型目录。优先 model_id 对应的子目录。"""
    base = Path(os.getenv("MODELS_PRODUCTION", "/app/models/production"))
    if model_id:
        candidate = base / model_id
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(f"找不到可用模型目录 (base={base}, model_id={model_id})")


def _dt_int_to_date(dt_int: int) -> date:
    return date(dt_int // 10000, (dt_int % 10000) // 100, dt_int % 100)


def _find_pred_parquet(model_dir: Path) -> Path | None:
    """定位模型目录的 pred.parquet（兼容 pred/ 子目录存法）。"""
    for candidate in (model_dir / "pred.parquet", model_dir / "pred" / "pred.parquet"):
        if candidate.is_file():
            return candidate
    return None


def _load_pred_parquet(parquet_file: Path) -> pd.DataFrame | None:
    """全量直读训练产出的历史分数（pred.parquet）。

    列：symbol（SH/SZ 前缀式）/ trade_date（数据日 D）/ label / pred（或
    fusion_score）/ split。只保留 symbol / trade_date / score 三列。
    失败返回 None。
    """
    import duckdb

    try:
        con = duckdb.connect()
        try:
            cols = [
                r[0]
                for r in con.execute(
                    f"SELECT * FROM read_parquet('{parquet_file}') LIMIT 0"
                ).description
            ]
            score_col = (
                "pred"
                if "pred" in cols
                else "fusion_score"
                if "fusion_score" in cols
                else None
            )
            date_col = (
                "trade_date" if "trade_date" in cols
                else ("date" if "date" in cols else None)
            )
            if not score_col or not date_col or "symbol" not in cols:
                logger.error("回放信号: pred.parquet 缺必要列: %s", cols)
                return None
            frame = con.execute(
                f"""
                SELECT symbol,
                       CAST({date_col} AS DATE) AS trade_date,
                       CAST({score_col} AS DOUBLE) AS score
                FROM read_parquet('{parquet_file}')
                WHERE CAST({score_col} AS DOUBLE) IS NOT NULL
                """,
            ).fetchdf()
        finally:
            con.close()
        if frame.empty:
            return None
        return frame.dropna(subset=["symbol", "trade_date", "score"])
    except Exception as exc:  # noqa: BLE001 - 直读失败由上层按无信号处理
        logger.error("回放信号: pred.parquet 读取失败: %s", exc)
        return None


# ---------------------------------------------------------------------------
# pred.parquet 进程内缓存
# ---------------------------------------------------------------------------

# 每步推演都临时读一次上千万行的全量文件太浪费：同一模型目录只解析一次，
# 按数据日切片缓存；文件被每日推理回写更新（mtime 变化）后自动失效重读。
_PRED_FRAME_CACHE: dict[str, dict[str, Any]] = {}
_PRED_CACHE_MAX = 4


def _get_pred_day_frame(model_dir: Path, data_day: date) -> pd.DataFrame | None:
    """取模型在数据日 data_day 的全市场分数切片（带缓存）。"""
    key = str(model_dir)
    entry = _PRED_FRAME_CACHE.get(key)
    pred_file = _find_pred_parquet(model_dir)
    if pred_file is None:
        return None
    mtime = pred_file.stat().st_mtime
    if entry is None or entry["mtime"] != mtime or data_day not in entry["by_day"]:
        df = _load_pred_parquet(pred_file)
        if df is None or df.empty:
            _PRED_FRAME_CACHE.pop(key, None)
            return None
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        by_day = {d: g for d, g in df.groupby(df["trade_date"].dt.date)}
        if len(_PRED_FRAME_CACHE) >= _PRED_CACHE_MAX:
            _PRED_FRAME_CACHE.pop(next(iter(_PRED_FRAME_CACHE)))
        entry = {"mtime": mtime, "by_day": by_day}
        _PRED_FRAME_CACHE[key] = entry
        logger.info(
            "回放信号: 缓存 %s 的 pred.parquet（%d 天分数）",
            pred_file,
            len(by_day),
        )
    return entry["by_day"].get(data_day)


# ---------------------------------------------------------------------------
# ReplaySignalLoader: day_runner 用它替代 SignalLoader
# ---------------------------------------------------------------------------


class ReplaySignalLoader:
    """直读模型 pred.parquet 加载指定会话、指定交易日的信号。

    接口与 SignalLoader.load_signals_for_date 兼容，返回 list[SignalScore]。
    模型目录优先取会话 strategy_params 里的 _model_dir（创建时由 router
    固化，用户训练模型也适用），旧会话回退按 model_id 解析生产目录。
    """

    async def load_signals_for_date(
        self,
        db: AsyncSession,  # noqa: ARG002 - 兼容 SignalLoader 接口；分数直读文件不查库
        session_id: uuid.UUID,
        trade_date: date,
        min_score: float | None = None,
        limit: int | None = None,
    ) -> list:
        """加载指定会话在 trade_date 生效的信号（= 上一数据日的模型分数）。

        min_score 默认 None（不过滤），因为 LightGBM 回归输出可正可负，
        过滤策略由 RebalanceCalculator 的 topk/min_score 参数决定。
        """
        from backend.services.simulation.models.replay import ReplaySession
        from backend.services.simulation.services.signal_loader import (
            SignalScore,
        )
        from sqlalchemy import select

        row = (
            (
                await db.execute(
                    select(ReplaySession).where(
                        ReplaySession.session_id == session_id
                    )
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            return []

        params = row.strategy_params or {}
        dir_str = str(params.get("_model_dir") or "").strip()
        if dir_str:
            model_dir = Path(dir_str)
        else:
            # 旧会话未固化模型目录：只能按 model_id 回退生产目录
            try:
                model_dir = _resolve_model_dir(row.model_id)
            except FileNotFoundError as exc:
                logger.warning(
                    "回放信号: 会话 %s 无法定位模型目录: %s", session_id, exc
                )
                return []

        pred_file = _find_pred_parquet(model_dir)
        if pred_file is None:
            logger.warning(
                "回放信号: 模型目录缺少 pred.parquet: %s", model_dir
            )
            return []

        # T+1 偏移：trade_date 生效的信号来自上一交易日（数据日）的分数
        sessions = await asyncio.to_thread(get_local_market_data()._sessions)
        td_int = int(trade_date.strftime("%Y%m%d"))
        before = [d for d in sessions if d < td_int]
        if not before:
            return []
        data_day = _dt_int_to_date(before[-1])

        day_df = _get_pred_day_frame(model_dir, data_day)
        if day_df is None or day_df.empty:
            logger.warning(
                "回放信号: %s 在数据日 %s 无模型分数（%s）",
                session_id,
                data_day,
                pred_file,
            )
            return []

        scores = day_df["score"].to_numpy(dtype="float64")
        order = scores.argsort()[::-1]
        if limit:
            order = order[:limit]
        result = [
            SignalScore(
                symbol=StockCodeUtil.to_suffix(
                    str(day_df["symbol"].iloc[i]).upper()
                ),
                score=float(scores[i]),
                trade_date=trade_date,
                run_id="replay",
                tenant_id="replay",
                user_id="replay",
            )
            for i in order
            if min_score is None or float(scores[i]) >= min_score
        ]
        return result


replay_signal_loader = ReplaySignalLoader()
