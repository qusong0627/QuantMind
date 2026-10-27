"""大盘情绪快照只读层（含历史回看）。

两种取数路径：

1. **最新一期**：读 ``market_sentiment_snapshot.py`` 产出的 JSON，API 进程零计算。
2. **历史回看（?date=YYYY-MM-DD）**：从广度面板现算。首次调用加载面板并算一遍
   扩张分位（约数秒），结果与全量特征帧在进程内缓存，之后按日切片是毫秒级。

无前视纪律：回看时所有信号只用该日及以前的数据（``build_payload(as_of=...)``）。
「之后真实走势」由 ``actual`` 字段单独给出，只用于对照，不参与信号计算。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pandas as pd
from fastapi import HTTPException, status

SNAPSHOT_NAME = "sentiment.json"
_SUBDIR = ("5_technical_derived", "market_breadth")
MIN_HISTORY_DAYS = 600

_LOCK = threading.Lock()
_CACHE: dict[str, object] = {}


def snapshot_path() -> Path:
    root = Path(os.getenv("QM_QUANTDB_DATA_DIR", "data/quantdb"))
    return root.joinpath(*_SUBDIR, SNAPSHOT_NAME)


def _read_snapshot() -> dict:
    path = snapshot_path()
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "message": "大盘情绪快照尚未生成",
                "hint": "运行 python -m backend.scripts.market_sentiment_snapshot",
            },
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 —— 任何解析失败都要显式暴露
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"message": "大盘情绪快照损坏", "error": str(exc)},
        ) from exc
    if not isinstance(payload, dict) or "stress" not in payload:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"message": "大盘情绪快照结构不合法（缺少 stress 字段）"},
        )
    # 旧格式快照（缺少回看所需字段）会让前端日期选择器把所有日期判成不可选，
    # 这里直接按最新交易日现算一份返回 —— 宁可慢一次，也不能给出去不了回看的产物。
    if not {"available_from", "available_to", "as_of"}.issubset(payload):
        df, feats = _frames()
        from backend.scripts.market_sentiment_snapshot import build_payload

        return build_payload(df, feats)
    return payload


def _frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    """加载面板与扩张分位（进程内缓存，只算一次）。"""
    with _LOCK:
        if "df" not in _CACHE:
            from backend.scripts.breadth_regime import build_features, load_panel
            from backend.scripts.market_sentiment_snapshot import PANEL_PATH

            df = load_panel(PANEL_PATH)
            _CACHE["df"] = df
            _CACHE["feats"] = build_features(df)
        return _CACHE["df"], _CACHE["feats"]  # type: ignore[return-value]


def _compute_asof(date: str) -> dict:
    from backend.scripts.market_sentiment_snapshot import build_payload

    df, feats = _frames()
    try:
        ts = pd.Timestamp(date)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail={"message": f"日期不合法: {date}"}) from exc

    pos = int(df.index.searchsorted(ts, side="right")) - 1
    if pos < MIN_HISTORY_DAYS:
        raise HTTPException(
            status_code=422,
            detail={
                "message": f"日期太早：{date} 之前不足 {MIN_HISTORY_DAYS} 个交易日",
                "available_from": df.index[MIN_HISTORY_DAYS].strftime("%Y-%m-%d"),
            },
        )
    trade_day = df.index[pos]
    key = trade_day.strftime("%Y-%m-%d")

    with _LOCK:
        payloads = _CACHE.setdefault("payloads", {})
        cached = payloads.get(key) if isinstance(payloads, dict) else None
    if cached is not None:
        return cached  # type: ignore[return-value]

    payload = build_payload(df, feats, as_of=trade_day)
    with _LOCK:
        payloads = _CACHE.setdefault("payloads", {})
        if isinstance(payloads, dict):
            if len(payloads) >= 32:
                payloads.clear()
            payloads[key] = payload
    return payload


def load_sentiment(date: str | None = None) -> dict:
    """最新一期读快照；指定日期走历史回看（服务端按最近交易日吸附）。"""
    if not date:
        return _read_snapshot()
    return _compute_asof(date)
