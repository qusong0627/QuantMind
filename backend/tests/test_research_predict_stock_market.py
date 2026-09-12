"""个股预测（/research/predict-stock）三市场分派回归测试。

本次修复的三个真实缺陷（全部实测确认）：
1. `_get_sdl_table('US')` 指向 `stock_daily_latest_us`，而该表此前在 PG 里不存在 →
   predict-stock 第一步就 UndefinedTableError；
2. `_read_pred_single_symbol` 对 symbol 一律做数字裁剪：AAPL → 空串，SQL 谓词
   `regexp_replace(symbol,'[^0-9]','','g') = ''` 对所有美股 ticker 都成立，
   LIMIT 1 静默取到任意一只（实测 AAPL@2026-08-05 取到 -0.047236，真值 +0.022226）；
3. `_load_stock_pred_history` 的 code6 为空即提前 return → 分数曲线只剩信号表 1 个点。

零 mock，直连本地 QuantUS parquet + 本地 PG。断言取向：真实值与市场分派契约，
不追求跨日不变的精确值（训练/数据每天在动）。

跑法（依赖在容器里）：
    docker exec -w /app/backend quantmind python -m pytest \
        tests/test_research_predict_stock_market.py -q --no-cov
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd
import pytest
from fastapi import HTTPException

from backend.services.api.routers.model_training import _load_stock_pred_history
from backend.services.api.routers.research_service import (
    _fetch_sdl_anchor,
    _get_sdl_table,
    _read_pred_single_symbol,
    _sdl_anchor_symbol_key,
    predict_single_stock,
)
from backend.services.engine.data_platform.quantus_hub import _resolve_quantus_data_dir

_TENANT, _USER = "default", "00000001"
_US_SYMBOL = "AAPL"
# 日线分区允许的滞后：超过即认为 QuantUS 停更 / 美股表没跟上同步
_SDL_LAG_DAYS = 15

# 异步用例共用一条 session 事件循环：asyncpg 连接池绑定创建它的 loop，
# 每个用例各起一条 loop 会让第二个用例拿到「attached to a different loop」。
_asyncio = pytest.mark.asyncio(loop_scope="session")


# ---- 本地数据/数据库取数助手（不 mock） ----


def _us_market_dir() -> Path:
    return _resolve_quantus_data_dir() / "1_kline_data" / "daily_forward"


def _latest_kline_day() -> str:
    parts = sorted(p.name[3:] for p in _us_market_dir().glob("dt=*"))
    assert parts, "QuantUS daily_forward 无任何分区"
    return parts[-1]


def _latest_kline_date() -> date:
    return datetime.strptime(_latest_kline_day(), "%Y%m%d").date()


def _kline_row(symbol: str, ymd: str) -> dict | None:
    """QuantUS 原始日线（未复权）单行。"""
    f = _us_market_dir() / f"dt={ymd}" / "data.parquet"
    if not f.is_file():
        return None
    con = duckdb.connect()
    try:
        df = con.execute(
            "SELECT symbol, CAST(time AS DATE) AS d, open, high, low, close "
            f"FROM read_parquet('{f}') WHERE symbol = ?",
            [symbol],
        ).fetchdf()
    finally:
        con.close()
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def _us_model_with_pred() -> dict | None:
    """任取一个挂了 pred.parquet 的美股模型（不写死模型 ID：训练随时新增）。"""
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "db"),
        port=int(os.getenv("DB_PORT", "5432")),
        user=os.getenv("DB_USER", "quantmind"),
        password=os.getenv("DB_PASSWORD", "quantmind2026"),
        dbname=os.getenv("DB_NAME", "quantmind"),
    )
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT model_id, storage_path FROM qm_user_models "
                "WHERE COALESCE(metadata_json->>'market','CN') = %s AND status IN ('ready','active') "
                "ORDER BY updated_at DESC",
                ["US"],
            )
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    for r in rows:
        base = Path(str(r.get("storage_path") or ""))
        if (base / "pred.parquet").is_file() or (
            base / "pred" / "pred.parquet"
        ).is_file():
            return r
    return None


def _pred_frame(storage_path: str) -> pd.DataFrame:
    con = duckdb.connect()
    try:
        return con.execute(
            f"SELECT * FROM read_parquet('{storage_path}/pred.parquet')"
        ).fetchdf()
    finally:
        con.close()


# ---- sdl 表与列集分派 ----


def test_sdl_table_market_mapping():
    assert _get_sdl_table("CN") == "stock_daily_latest"
    assert _get_sdl_table("HK") == "stock_daily_latest_hk"
    assert _get_sdl_table("US") == "stock_daily_latest_us"
    # 未知/缺省回退 CN 主表
    assert _get_sdl_table(None) == "stock_daily_latest"
    assert _get_sdl_table("XX") == "stock_daily_latest"


def test_us_sdl_table_populated_and_fresh():
    """stock_daily_latest_us 必须存在、有 90 日窗口、最新日跟得上 QuantUS。"""
    import psycopg2

    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "db"),
        port=int(os.getenv("DB_PORT", "5432")),
        user=os.getenv("DB_USER", "quantmind"),
        password=os.getenv("DB_PASSWORD", "quantmind2026"),
        dbname=os.getenv("DB_NAME", "quantmind"),
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*), COUNT(DISTINCT symbol), MAX(trade_date), MIN(trade_date) "
                "FROM stock_daily_latest_us"
            )
            rows, syms, max_d, min_d = cur.fetchone()
            cur.execute(
                "SELECT close, adj_factor, stock_name, industry, vol_std_20, amount "
                "FROM stock_daily_latest_us WHERE symbol = %s ORDER BY trade_date DESC LIMIT 1",
                [_US_SYMBOL],
            )
            aapl = cur.fetchone()
    finally:
        conn.close()

    assert rows > 0, "stock_daily_latest_us 为空：quantus_sdl_sync 未跑"
    assert syms > 100, f"美股标的数异常: {syms}"
    lag = (_latest_kline_date() - max_d).days
    assert lag <= _SDL_LAG_DAYS, (
        f"美股聚合表滞后 {lag} 天（QuantUS 已到 {_latest_kline_day()}）"
    )
    assert (max_d - min_d).days >= 60, "快照窗口不足 90 个交易日"

    close, adj_factor, stock_name, industry, vol_std_20, amount = aapl
    assert close and close > 0
    # QuantUS 无复权因子：adj_factor 恒 1、close 即真实成交价（_to_nominal_price 不再缩放）
    assert adj_factor == 1
    assert stock_name and stock_name != _US_SYMBOL, (
        "美股中文名没落上（security_master.cn_name）"
    )
    assert industry, "行业没落上（sector.industry）"
    # vol_std_20 是**小数**口径（0.0147=1.47%），与 CN/HK 百分数不同 → 必须 < 0.3
    assert 0 < vol_std_20 < 0.3, f"美股 vol_std_20 疑似百分数口径: {vol_std_20}"
    # amount 为美元原始成交额（≈ close×volume），不是「万元」
    assert amount and amount > 1e6


def test_sdl_anchor_symbol_key_normalizes():
    assert _sdl_anchor_symbol_key("AAPL") == "AAPL"
    assert _sdl_anchor_symbol_key("aapl") == "AAPL"
    assert _sdl_anchor_symbol_key("BRK.B") == _sdl_anchor_symbol_key("BRK-B") == "BRKB"
    assert _sdl_anchor_symbol_key("0700.HK") == "0700HK"


@_asyncio
async def test_fetch_sdl_anchor_us_close_matches_parquet():
    """美股锚点价必须来自真实日线，且带基准日上限（历史基准日不取最新行）。"""
    anchor = await _fetch_sdl_anchor(_US_SYMBOL, "US", None)
    assert anchor is not None, "美股锚点查不到 AAPL：表列集或 symbol 匹配有问题"
    assert anchor["trade_date"] is not None
    anchor_day = str(anchor["trade_date"]).replace("-", "")
    k = _kline_row(_US_SYMBOL, anchor_day)
    assert k is not None
    assert anchor["close"] == pytest.approx(k["close"], rel=1e-6)

    # 基准日上限：取历史某日时锚点不得晚于该日
    past = (_latest_kline_date() - timedelta(days=30)).isoformat()
    past_anchor = await _fetch_sdl_anchor(_US_SYMBOL, "US", past)
    assert past_anchor is not None
    assert str(past_anchor["trade_date"]) <= past


# ---- pred.parquet 直读的 symbol 匹配 ----


def test_us_pred_direct_read_returns_the_right_symbol():
    """AAPL 直读必须等于 pred.parquet 里 AAPL 那一行，而不是 LIMIT 1 的任意一只。"""
    model = _us_model_with_pred()
    if model is None:
        pytest.skip("没有带 pred.parquet 的美股模型")
    df = _pred_frame(str(model["storage_path"]))
    aapl = df[df["symbol"] == _US_SYMBOL].sort_values("trade_date")
    if aapl.empty:
        pytest.skip("pred.parquet 无 AAPL 行")
    row = aapl.iloc[-1]
    target_date = str(row["trade_date"])[:10]

    got = _read_pred_single_symbol(
        str(model["storage_path"]), target_date, _US_SYMBOL, "US"
    )
    assert got is not None, "美股 pred 直读没命中（symbol 匹配又退化成数字裁剪了？）"
    assert got == pytest.approx(float(row["pred"]), abs=1e-9)

    # 反向守卫：文件首行（旧实现 LIMIT 1 会取到的那一只）不能是同一分数
    first = df.iloc[0]
    if (
        str(first["symbol"]) != _US_SYMBOL
        and str(first["trade_date"])[:10] == target_date
    ):
        assert got != pytest.approx(float(first["pred"]), abs=1e-12)


def test_us_pred_direct_read_rejects_unknown_and_unjudged_symbols():
    """查不到的 ticker 返回 None；不传 market（CN 默认）时不再静默取任意一只。"""
    model = _us_model_with_pred()
    if model is None:
        pytest.skip("没有带 pred.parquet 的美股模型")
    df = _pred_frame(str(model["storage_path"]))
    target_date = str(df["trade_date"].max())[:10]
    path = str(model["storage_path"])

    assert _read_pred_single_symbol(path, target_date, "ZZZZ", "US") is None
    # 无 market（历史调用口径 = CN 数字裁剪）：纯字母 ticker 必须返回 None
    assert _read_pred_single_symbol(path, target_date, _US_SYMBOL) is None


def test_cn_pred_direct_read_unchanged():
    """CN 三种代码写法仍解析到同一只票（数字裁剪分支不许回归）。"""
    from backend.shared.stock_utils import StockCodeUtil

    cn = _find_cn_model()
    if cn is None:
        pytest.skip("没有带 pred.parquet 的 A 股模型")
    path = str(cn["storage_path"])
    df = _pred_frame(path)
    latest = df.sort_values("trade_date").iloc[-1]
    target_date = str(latest["trade_date"])[:10]
    suffix = StockCodeUtil.to_suffix(str(latest["symbol"]))
    if not suffix[:6].isdigit():
        pytest.skip("pred.parquet symbol 非 A 股数字口径")

    variants = [
        _read_pred_single_symbol(path, target_date, suffix, "CN"),  # 600519.SH
        _read_pred_single_symbol(
            path, target_date, StockCodeUtil.to_prefix(suffix), "CN"
        ),  # SH600519
        _read_pred_single_symbol(path, target_date, suffix[:6], "CN"),  # 600519
    ]
    assert all(v is not None for v in variants), f"CN 直读有写法未命中: {variants}"
    assert variants[0] == variants[1] == variants[2]


def _find_cn_model() -> dict | None:
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "db"),
        port=int(os.getenv("DB_PORT", "5432")),
        user=os.getenv("DB_USER", "quantmind"),
        password=os.getenv("DB_PASSWORD", "quantmind2026"),
        dbname=os.getenv("DB_NAME", "quantmind"),
    )
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT model_id, storage_path FROM qm_user_models "
                "WHERE COALESCE(metadata_json->>'market','CN') = 'CN' AND status IN ('ready','active') "
                "ORDER BY updated_at DESC LIMIT 30"
            )
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    for r in rows:
        base = Path(str(r.get("storage_path") or ""))
        if (base / "pred.parquet").is_file():
            return r
    return None


# ---- 端到端：predict_single_stock ----


@_asyncio
async def test_predict_single_stock_us_end_to_end():
    """US 不再 UndefinedTableError；锚点价来自真实日线；直读分数=pred.parquet 真值。"""
    model = _us_model_with_pred()
    if model is None:
        pytest.skip("没有带 pred.parquet 的美股模型")
    df = _pred_frame(str(model["storage_path"]))
    aapl = df[df["symbol"] == _US_SYMBOL].sort_values("trade_date")
    if aapl.empty:
        pytest.skip("pred.parquet 无 AAPL 行")
    row = aapl.iloc[-1]
    target_date = str(row["trade_date"])[:10]

    res = await predict_single_stock(
        _TENANT,
        _USER,
        symbol=_US_SYMBOL,
        model_id=str(model["model_id"]),
        target_date=target_date,
        market="US",
        execute=True,
    )
    data = res["data"]
    assert data["status"] == "success"
    assert data["symbol"] == _US_SYMBOL
    # 分数必须来自真实 pred.parquet（独立轻路线直读命中）
    assert data["data_source"] == "pred_parquet"
    assert data["predicted_score"] == pytest.approx(float(row["pred"]), abs=1e-4)
    # 锚点价 = 当日未复权真实收盘
    k = _kline_row(_US_SYMBOL, target_date.replace("-", ""))
    assert k is not None
    assert data["current_price"] == pytest.approx(k["close"], rel=1e-6)
    assert data["as_of_date"] == target_date


@_asyncio
async def test_predict_single_stock_us_unknown_symbol_is_404():
    """美股查不到的标的：显式 404（明细说明无行情），不是 500 / 不是假价格。"""
    with pytest.raises(HTTPException) as exc:
        await predict_single_stock(_TENANT, _USER, symbol="ZZZZ", market="US")
    assert exc.value.status_code == 404
    assert "行情" in str(exc.value.detail)


@_asyncio
@pytest.mark.parametrize("symbol,market", [("600519.SH", "CN"), ("0700.HK", "HK")])
async def test_predict_single_stock_cn_hk_regression(symbol, market):
    """CN/HK 主链路不许回归：出结果、锚点价为正、基准日有值。"""
    res = await predict_single_stock(_TENANT, _USER, symbol=symbol, market=market)
    data = res["data"]
    assert data["status"] == "success"
    assert data["current_price"] and data["current_price"] > 0
    assert data["stock_name"] and data["stock_name"] != symbol
    assert data["as_of_date"]


# ---- 分数曲线（pred 历史） ----


@_asyncio
async def test_stock_pred_history_us_ticker_mode():
    """美股 ticker 走整串匹配：能读出长序列（不是只剩信号表 1 个点）。"""
    model = _us_model_with_pred()
    if model is None:
        pytest.skip("没有带 pred.parquet 的美股模型")
    items, used = await _load_stock_pred_history(
        tenant_id=_TENANT,
        user_id=_USER,
        model_id=str(model["model_id"]),
        sym=_US_SYMBOL,
        cutoff=date.today() - timedelta(days=730),
        market="US",
    )
    assert used is not None
    assert len(items) > 20, f"美股分数曲线只有 {len(items)} 个点：symbol 匹配又断了"
    assert all(it["source"] == "pred_parquet" for it in items)
    assert all(it["trade_date"] for it in items)


def test_stock_inference_history_route_us():
    """路由层接线：GET /models/inference/stock/AAPL/history?market=US 直接出长序列。

    只覆盖鉴权依赖（不 mock 数据），验证 market 真的传到了 pred 匹配分支。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.services.api.routers import model_training as mt
    from backend.services.api.user_app.middleware.auth import get_current_user

    model = _us_model_with_pred()
    if model is None:
        pytest.skip("没有带 pred.parquet 的美股模型")

    app = FastAPI()
    app.include_router(mt.router, prefix="/models")
    app.dependency_overrides[get_current_user] = lambda: {
        "tenant_id": _TENANT,
        "user_id": _USER,
    }
    with TestClient(app) as client:
        resp = client.get(
            f"/models/inference/stock/{_US_SYMBOL}/history",
            params={"market": "US", "model_id": str(model["model_id"]), "days": 730},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["score_source"] == "pred_parquet", "美股分数曲线没走 pred.parquet"
    assert body["total"] > 20, f"美股分数曲线只有 {body['total']} 个点"
    assert body["items"][0]["trade_date"]


# ---- SHAP 归因的市场分派 ----


def test_shap_drivers_us_tree_model():
    """美股树模型的 SHAP 归因要能拿到（模型目录多一层市场段 + 快照用 instrument 列）。"""
    import glob
    import json

    from backend.services.api.routers.research_service import (
        _SHAP_TREE_FRAMEWORKS,
        _compute_shap_drivers_sync,
        _resolve_snapshot_parquet,
    )

    snap = _resolve_snapshot_parquet("US", date.today().year)
    assert snap is not None, "美股特征快照 model_features_us.parquet 缺失"
    con = duckdb.connect()
    try:
        as_of = con.execute(
            "SELECT MAX(CAST(trade_date AS DATE)) FROM read_parquet(?) WHERE instrument = ?",
            [str(snap), _US_SYMBOL],
        ).fetchone()[0]
    finally:
        con.close()
    if as_of is None:
        pytest.skip("美股快照里没有 AAPL")

    tree_mid = None
    for meta_path in glob.glob("/app/models/users/*/*/*/*/metadata.json"):
        try:
            meta = json.load(open(meta_path, encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(meta.get("market") or "").upper() != "US":
            continue
        if str(meta.get("framework") or "").lower() in _SHAP_TREE_FRAMEWORKS:
            tree_mid = Path(meta_path).parent.name
            break
    if tree_mid is None:
        pytest.skip("没有美股树模型")

    drivers = _compute_shap_drivers_sync(tree_mid, _US_SYMBOL, as_of.isoformat(), "US")
    assert drivers, "美股树模型 SHAP 归因为空：模型目录/快照列名的市场分派又断了"
    assert len(drivers) >= 3
    assert all(d["category"] == "模型SHAP" for d in drivers)
    assert all(d["name"] for d in drivers)
