"""arena 原生数据面契约测试（A2：族 1 台账 + 配置族 + 族 8 静态文件；A3：族 2 行情实验室）。

契约 = `electron/src/features/local-live/agent-arena/arena/api/client.ts` 的
类型定义 + `{success, data}` 信封。测试用合成数据根（`QM_ARENA_DATA_ROOT`
指到 tmp），逐族断言**响应形状与数字口径**（价格滑点/费率/FIFO 平仓），
外加两条安全/解析判据：静态文件越界 404、数据根哨兵判定。
A3 段另钉四条：复权口径按目录分派、错误路径分档（400/404 vs 200+success:false）、
`/batch/runs` 不被 `/batch/{run_id}` 吞掉（声明序）、job 的空报告收敛为 null。

本文件与 `backend/services/agent_arena/` 自 2026-11-13 起随仓分发（运行期数据
仍不入仓；见 .gitignore）。测试不依赖真实数据根：全部夹具用
`QM_ARENA_DATA_ROOT` 指到 tmp。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

BASE = "/api/v1/agent-arena"


# ---------- 夹具 ----------


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )


def _write_parquet(path: Path, create_sql: str) -> None:
    """用 duckdb 写合成 parquet（A3 夹具：quantdb K线 / 名称表 / 指数成分）。"""
    import duckdb

    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"COPY ({create_sql}) TO '{path}' (FORMAT PARQUET)")
    con.close()


def _merged_row(symbol: str, bars: dict[str, str]) -> dict:
    """Alpha Vantage 形状的一行 merged.jsonl（每行一只股票）。"""
    return {
        "Meta Data": {"2. Symbol": symbol},
        "Time Series (Daily)": {
            date: {
                "1. open": price,
                "1. buy price": price,
                "4. close": price,
                "4. sell price": price,
            }
            for date, price in bars.items()
        },
    }


def _build_arena_fixture(root: Path) -> None:
    # 哨兵（真数据根判据，实际解析走 env 覆盖；保留是为了与生产同构）
    sentinel = root / "arena" / "quanttrader" / "MANIFEST.jsonl"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("{}\n", encoding="utf-8")

    # ---- US：alpha 买 10 @09-01、清仓 @09-03 ----
    _write_jsonl(
        root / "agent_data" / "alpha" / "position" / "position.jsonl",
        [
            {
                "date": "2026-09-01",
                "id": 1,
                "this_action": {"action": "buy", "symbol": "AAPL", "amount": 10},
                "positions": {"CASH": 9000.0, "AAPL": 10},
            },
            {
                "date": "2026-09-03",
                "id": 2,
                "this_action": {"action": "sell", "symbol": "AAPL", "amount": 10},
                "positions": {"CASH": 9100.0},
            },
        ],
    )
    _write_jsonl(
        root / "agent_data" / "alpha" / "log" / "2026-09-01" / "log.jsonl",
        [
            {
                "signature": "s1",
                "timestamp": "2026-09-01T10:00:00",
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                },
                "new_messages": [{"role": "assistant", "content": "hi"}],
            }
        ],
    )
    _write_jsonl(
        root / "merged.jsonl",
        [_merged_row("AAPL", {"2026-09-01": "100.0", "2026-09-03": "110.0"})],
    )

    # ---- CN：glm 持有 600519；当日晚间有桥成交回报（fill）----
    _write_jsonl(
        root / "agent_data_astock" / "glm" / "position" / "position.jsonl",
        [
            {
                "date": "2026-09-02",
                "id": 1,
                "this_action": {"action": "buy", "symbol": "600519.SH", "amount": 100},
                "positions": {"CASH": 850000.0, "600519.SH": 100},
            }
        ],
    )
    _write_jsonl(
        root / "agent_data_astock" / "glm" / "log" / "2026-09-02" / "log.jsonl",
        [
            {
                "signature": "cn-1",
                "timestamp": "2026-09-02T10:30:00",
                "usage": {
                    "prompt_tokens": 200,
                    "completion_tokens": 80,
                    "total_tokens": 280,
                },
                "new_messages": [],
            }
        ],
    )
    _write_jsonl(
        root / "A_stock" / "merged.jsonl",
        [_merged_row("600519.SH", {"2026-09-02": "1400.0"})],
    )
    (root / "logs").mkdir(parents=True, exist_ok=True)
    _write_jsonl(
        root / "logs" / "live_trade_20260904.jsonl",
        [
            {
                "ts": "2026-09-04T10:00:00",
                "agent": "glm",
                "code": "600519.SH",
                "side": "sell",
                "fill": {
                    "filled_price": 1500.0,
                    "filled_volume": 100,
                    "cost_price": 1400.0,
                },
            }
        ],
    )

    # ---- 配置与静态文件 ----
    (root / "arena").mkdir(parents=True, exist_ok=True)
    (root / "arena" / "comp-config.json").write_text(
        json.dumps({"selection": {"cn": {"alpha": ["monk"]}}}), encoding="utf-8"
    )
    (root / "benchmark_nasdaq100.json").write_text("[]", encoding="utf-8")

    # ---- A3：quantdb K线（族 2 行情实验室）----
    # 不复权：8 根有效 + 1 根 open=NaN（必须被服务层剔除）→ count=8
    _write_parquet(
        root
        / "quantdb"
        / "1_kline_data"
        / "daily_unadjusted"
        / "dt=2026-09-01"
        / "part.parquet",
        """
        SELECT CAST(time AS DATE) AS time, CAST(o AS DOUBLE) AS open,
               CAST(h AS DOUBLE) AS high, CAST(l AS DOUBLE) AS low,
               CAST(c AS DOUBLE) AS close, CAST(v AS DOUBLE) AS volume, symbol
        FROM (VALUES
            ('2026-08-20', 100.0, 102.0,  99.0, 101.0, 1000.0, '600519.SH'),
            ('2026-08-21', 101.0, 103.0, 100.0, 102.5, 1100.0, '600519.SH'),
            ('2026-08-24', 102.5, 104.0, 101.0, 103.0, 1200.0, '600519.SH'),
            ('2026-08-25', 103.0, 103.5, 101.5, 102.0,  900.0, '600519.SH'),
            ('2026-08-26', 102.0, 105.0, 102.0, 104.5, 1500.0, '600519.SH'),
            ('2026-08-27', 104.5, 106.0, 104.0, 105.0, 1300.0, '600519.SH'),
            ('2026-08-28', 105.0, 105.5, 103.0, 103.5,  800.0, '600519.SH'),
            ('2026-08-31', 103.5, 104.0, 102.0, 102.8,   NULL, '600519.SH'),
            ('2026-09-01', CAST('NaN' AS DOUBLE), 110.0, 100.0, 105.0, 999.0, '600519.SH')
        ) AS t(time, o, h, l, c, v, symbol)
        """,
    )
    # 后复权：同日不同价 → 证明 adj 按目录分派（3 根，顺带供「K线不足」400 用例）
    _write_parquet(
        root
        / "quantdb"
        / "1_kline_data"
        / "daily_backward"
        / "dt=2026-09-01"
        / "part.parquet",
        """
        SELECT CAST(time AS DATE) AS time, CAST(o AS DOUBLE) AS open,
               CAST(h AS DOUBLE) AS high, CAST(l AS DOUBLE) AS low,
               CAST(c AS DOUBLE) AS close, CAST(v AS DOUBLE) AS volume, symbol
        FROM (VALUES
            ('2026-08-28', 55.0, 56.0, 54.0, 55.5, 700.0, '600519.SH'),
            ('2026-08-31', 55.5, 56.0, 54.5, 55.0, 750.0, '600519.SH'),
            ('2026-09-01', 55.0, 55.5, 54.0, 54.6, 760.0, '600519.SH')
        ) AS t(time, o, h, l, c, v, symbol)
        """,
    )
    _write_parquet(
        root
        / "quantdb"
        / "2_base_sector"
        / "instrument_detail"
        / "instrument_detail.parquet",
        """
        SELECT * FROM (VALUES
            ('600519.SH', '贵州茅台'), ('000001.SZ', '平安银行'), ('300750.SZ', '宁德时代')
        ) AS t(Symbol, Name)
        """,
    )
    _write_parquet(
        root / "quantdb" / "2_base_sector" / "index_weights" / "000300.SH.parquet",
        "SELECT * FROM (VALUES ('000001.SZ'), ('600519.SH')) AS t(Symbol)",
    )

    # ---- A3：批量回测产物（lab_batch，两个批次测时间倒序）----
    (root / "lab_batch").mkdir(parents=True, exist_ok=True)
    (root / "lab_batch" / "20260929-090000-zz500.json").write_text(
        json.dumps(
            {
                "run_id": "20260929-090000-zz500",
                "created": "2026-09-29 09:00:00",
                "pool": "zz500",
                "pool_label": "中证500",
                "adj": "backward",
                "strategies": ["sma_cross"],
                "universe": 0,
                "counts": {},
                "elapsed_sec": 0.1,
                "ranking": [],
                "rows": [],
                "skipped": [],
                "errors": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (root / "lab_batch" / "20260930-120000-hs300.json").write_text(
        json.dumps(
            {
                "run_id": "20260930-120000-hs300",
                "created": "2026-09-30 12:00:00",
                "pool": "hs300",
                "pool_label": "沪深300",
                "adj": "backward",
                "strategies": ["sma_cross", "rsi_reversal"],
                "universe": 2,
                "counts": {"rows": 3},
                "elapsed_sec": 1.5,
                "ranking": [{"strategy": "sma_cross", "mean_net_pct": 6.5}],
                "rows": [
                    {
                        "symbol": "600519.SH",
                        "name": "贵州茅台",
                        "strategy": "sma_cross",
                        "net_pct": 5.0,
                        "dd_pct": -2.0,
                        "sharpe": 1.2,
                        "profit_factor": 1.5,
                        "trades": 4,
                    },
                    {
                        "symbol": "600519.SH",
                        "name": "贵州茅台",
                        "strategy": "rsi_reversal",
                        "net_pct": -1.0,
                        "dd_pct": -4.0,
                        "sharpe": 0.2,
                        "profit_factor": 0.9,
                        "trades": 6,
                    },
                    {
                        "symbol": "000001.SZ",
                        "name": "平安银行",
                        "strategy": "sma_cross",
                        "net_pct": 8.0,
                        "dd_pct": -1.0,
                        "sharpe": 2.0,
                        "profit_factor": 2.2,
                        "trades": 3,
                    },
                ],
                "skipped": [{"symbol": "300750.SZ", "reason": "K线不足 250 根"}],
                "errors": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # ---- A3：Pine 策略库（两条：0001 有编辑版/版本/报告，0002 只有备注）----
    pine = root / "pine_library"
    pine.mkdir(parents=True, exist_ok=True)
    (pine / "index.json").write_text(
        json.dumps(
            {
                "generated": "2026-09-30",
                "source_dir": "source",
                "total": 2,
                "usable": 2,
                "categories": [{"name": "趋势", "count": 1}],
                "items": [
                    {
                        "id": "0001",
                        "title": "双均线",
                        "category": "趋势",
                        "file": "trend.pine",
                    },
                    {
                        "id": "0002",
                        "title": "RSI 摆动",
                        "category": "震荡",
                        "file": "rsi.pine",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (pine / "source").mkdir(parents=True, exist_ok=True)
    (pine / "source" / "0001.pine").write_text(
        "//@version=5\nstrategy('t1')\n", encoding="utf-8"
    )
    (pine / "source" / "0002.pine").write_text(
        "//@version=5\nstrategy('t2')\n", encoding="utf-8"
    )
    (pine / "edited").mkdir(parents=True, exist_ok=True)
    (pine / "edited" / "0001.pine").write_text(
        "//@version=5\nstrategy('t1-edited')\n", encoding="utf-8"
    )
    (pine / "notes").mkdir(parents=True, exist_ok=True)
    (pine / "notes" / "0002.json").write_text(
        json.dumps(
            {
                "id": "0002",
                "note": "等回踩",
                "tags": ["震荡"],
                "rating": 3,
                "status": "观察中",
                "by": "user",
                "updated": "2026-09-30T10:00:00+00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (pine / "versions" / "0001").mkdir(parents=True, exist_ok=True)
    (pine / "versions" / "0001" / "20260930T120000000000.pine").write_text(
        "//@version=5\nstrategy('t1-old')\n", encoding="utf-8"
    )
    (root / "pine_transpile" / "0001").mkdir(parents=True, exist_ok=True)
    (root / "pine_transpile" / "0001" / "job.json").write_text(
        json.dumps({"status": "done", "stage": "backtest"}), encoding="utf-8"
    )
    (root / "pine_transpile" / "0001" / "report.json").write_text(
        json.dumps(
            {
                "symbol": "600519.SH",
                "adj": "backward",
                "stats": {"Net profit": {"pct": 4.2}},
                "trades": 4,
            }
        ),
        encoding="utf-8",
    )
    (root / "pine_transpile" / "0001" / "meta.json").write_text(
        json.dumps({"title": "双均线", "model": "m", "generated": "2026-09-30"}),
        encoding="utf-8",
    )
    # 0002：只有排队请求、没跑过 → job=queued、report=null（不是 {}）
    (root / "pine_transpile" / "queue").mkdir(parents=True, exist_ok=True)
    (root / "pine_transpile" / "queue" / "0002.json").write_text(
        "{}\n", encoding="utf-8"
    )


@pytest.fixture()
def arena_root(tmp_path, monkeypatch):
    root = tmp_path / "data_root"
    _build_arena_fixture(root)
    monkeypatch.setenv("QM_ARENA_DATA_ROOT", str(root))
    # 跨用例缓存全清：TTL 缓存（空前缀=全清）、sqlite 句柄、quantdb 名称表
    from backend.services.agent_arena.ttl_cache import ttl_invalidate

    ttl_invalidate(())
    monkeypatch.setattr("backend.services.agent_arena.trade_store._DB_PATH", None)
    monkeypatch.setattr(
        "backend.services.api.routers.agent_arena.agents_family._QUANTDB_NAMES", None
    )
    monkeypatch.setattr("backend.services.agent_arena.market_lab._names_cache", None)
    return root


# ---- 全局 DB 池纪律（本文件 real-* / l2 / ledger 真读 PG；范式抄
# tests/test_decision_ledger_store.py 的 _ensure_db_pool/_close_db）----
# TestClient 每个用例起一条 portal 事件循环，asyncpg 池绑定**创建它的 loop**：
# 下游不关 → 下一个用例摸到已死 loop 的连接（attached to a different loop /
# Event loop is closed）；上游不探 → 被前序文件（套件顺序固定，非抖动）的
# 陈旧池绑死。两头都必须在**本用例的 portal loop 内**做，才关得干净。


async def _dispose_db_pool() -> None:
    """下游收尾：在 portal loop 还活着时关池，不把脏池留给下个用例。"""
    try:
        from backend.shared.database_manager_v2 import close_database

        await close_database()
    except Exception:  # noqa: BLE001 收尾尽力而为；若真没关掉，下一次探测会现形
        pass


async def _probe_db_or_rebuild() -> None:
    """上游守卫：探不通（前序用例把池绑死在已关闭的 loop 上）就重建。"""
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import close_database, get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
        return
    except Exception:  # noqa: BLE001 陈旧池子即刷新
        await close_database()
    async with get_session(read_only=True) as probe:
        await probe.execute(text("SELECT 1"))


@pytest.fixture()
def client(arena_root):
    from backend.services.api.routers.agent_arena import router as arena_router
    from backend.services.api.user_app.middleware.auth import get_current_user

    app = FastAPI()
    app.include_router(arena_router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": 1}
    with TestClient(app) as c:
        # 池的探与关都跑在**本用例的 portal loop 内**（见上方纪律注释）
        c.portal.call(_probe_db_or_rebuild)
        yield c
        c.portal.call(_dispose_db_pool)


# ---------- 族 1：agent 台账 ----------


def test_agents_list_returns_envelope_and_metadata(client):
    # Arrange 见夹具
    # Act
    res = client.get(f"{BASE}/agents", params={"market": "us"})
    # Assert
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    (alpha,) = body["data"]
    assert alpha["name"] == "alpha"
    assert alpha["total_records"] == 2
    assert alpha["cash"] == 9100.0
    assert alpha["latest_date"] == "2026-09-03"


def test_positions_returns_records_and_404_when_agent_missing(client):
    ok = client.get(f"{BASE}/agents/alpha/positions", params={"market": "us"})
    assert ok.status_code == 200
    assert len(ok.json()["data"]) == 2

    missing = client.get(f"{BASE}/agents/nobody/positions", params={"market": "us"})
    assert missing.status_code == 404


def test_trades_are_enriched_with_slippage_prices(client):
    """买卖两侧都按滑点模型补 price/notional（买 +0.05%、卖 -0.05%）。"""
    res = client.get(f"{BASE}/agents/alpha/trades", params={"market": "us"})
    assert res.status_code == 200
    rows = res.json()["data"]
    by_action = {r["action"]: r for r in rows}
    assert by_action["buy"]["price"] == pytest.approx(100.05)
    assert by_action["buy"]["notional"] == pytest.approx(1000.5)
    assert by_action["sell"]["price"] == pytest.approx(109.945)
    assert by_action["sell"]["notional"] == pytest.approx(1099.45)


def test_performance_summary_shape_and_numbers(client):
    res = client.get(f"{BASE}/agents/alpha/performance", params={"market": "us"})
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["agent"] == "alpha"
    assert len(data["points"]) == 2
    s = data["summary"]
    assert s["start_equity"] == pytest.approx(10000.0)
    assert s["end_equity"] == pytest.approx(9100.0)
    assert s["total_return"] == pytest.approx(-0.09)
    assert s["max_drawdown"] == pytest.approx(0.09)
    # 扩展指标键必须齐（前端 Summary 类型逐键消费）
    for key in (
        "sharpe",
        "win_rate",
        "profit_factor",
        "closed_trades",
        "total_fee",
        "fee_ratio",
        "avg_hold_days",
        "position_time_ratio",
        "biggest_win",
        "biggest_loss",
        "avg_trade_pnl",
        "expectancy",
        "avg_trade_size",
        "median_trade_size",
        "median_hold_days",
    ):
        assert key in s, key
    assert s["closed_trades"] == 1
    assert s["win_rate"] == 1.0


def test_performance_404_when_no_data(client):
    res = client.get(f"{BASE}/agents/nobody/performance", params={"market": "us"})
    assert res.status_code == 404


def test_logs_limit_and_usage_preserved(client):
    res = client.get(f"{BASE}/agents/alpha/logs", params={"market": "us", "limit": 1})
    assert res.status_code == 200
    rows = res.json()["data"]
    assert len(rows) == 1
    assert rows[0]["signature"] == "s1"
    assert rows[0]["usage"]["total_tokens"] == 150


def test_trade_detail_rebuilds_fifo_closed_trade(client):
    res = client.get(
        f"{BASE}/agents/alpha/trade-detail", params={"market": "us", "limit": 25}
    )
    assert res.status_code == 200
    (t,) = res.json()["data"]
    assert t["symbol"] == "AAPL"
    assert t["entry_price"] == pytest.approx(100.05)
    assert t["exit_price"] == pytest.approx(109.945)
    assert t["qty"] == 10
    assert t["hold_days"] == 2
    assert t["pnl"] > 0


def test_trade_detail_merges_cn_live_fills(client, monkeypatch):
    """cn 平仓明细走 PG 回合台账：live=True、成本取 cost_price、减仓带 partial。"""
    from backend.services.agent_arena import live_family as lf_service

    async def _rows(session, agent):
        assert agent == "glm"
        return [
            {
                "agent": "glm",
                "code": "600519.SH",
                "volume": 100,
                "cost_price": 1400.0,
                "sell_price": 1500.0,
                "realized_pnl": 10000.0,
                # 瞬时列 aware UTC；卖出自北京 2026-09-04 15:00
                "buy_ts": datetime(2026, 9, 1, 1, 30, tzinfo=timezone.utc),
                "sell_ts": datetime(2026, 9, 4, 7, 0, tzinfo=timezone.utc),
                "holding_days": 3,
                "closed": True,
                "exit_reason": "signal",
            }
        ]

    monkeypatch.setattr(lf_service, "load_agent_roundtrips_pg", _rows)
    res = client.get(
        f"{BASE}/agents/glm/trade-detail", params={"market": "cn", "limit": 25}
    )
    assert res.status_code == 200
    (row,) = res.json()["data"]
    assert row["live"] is True and row["partial"] is False
    assert row["symbol"] == "600519.SH"
    assert row["exit_date"] == "2026-09-04"
    assert row["qty"] == 100
    assert row["entry_price"] == pytest.approx(1400.0)
    assert row["exit_price"] == pytest.approx(1500.0)
    assert row["pnl"] == pytest.approx((1500.0 - 1400.0) * 100)


def test_holdings_after_full_exit_is_cash_only(client):
    res = client.get(f"{BASE}/agents/alpha/holdings", params={"market": "us"})
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["holdings"] == []
    assert data["cash"] == pytest.approx(9100.0)
    assert data["total_equity"] == pytest.approx(9100.0)


def test_prices_contract(client):
    res = client.get(f"{BASE}/prices", params={"market": "us"})
    assert res.status_code == 200
    quote = res.json()["data"]["AAPL"]
    assert quote["price"] == pytest.approx(110.0)
    assert quote["date"] == "2026-09-03"
    assert quote["prev_close"] == pytest.approx(100.0)
    assert quote["change_pct"] == pytest.approx(0.1)


def test_stock_names_cn_includes_static_table(client):
    res = client.get(f"{BASE}/stock-names", params={"market": "cn"})
    assert res.status_code == 200
    assert res.json()["data"]["600519.SH"] == "贵州茅台"


def test_token_usage_aggregates_cn_logs(client):
    res = client.get(f"{BASE}/token-usage")
    assert res.status_code == 200
    glm = res.json()["data"]["agents"]["glm"]
    assert glm["calls"] == 1
    assert glm["total_tokens"] == 280
    assert glm["last_ts"] == "2026-09-02T10:30:00"


def test_overview_shape_and_tdx_default(client):
    res = client.get(f"{BASE}/overview")
    assert res.status_code == 200
    data = res.json()["data"]
    (alpha,) = data["markets"]["us"]
    assert alpha["name"] == "alpha"
    assert alpha["summary"]["end_equity"] == pytest.approx(9100.0)
    # A2 阶段 tdx 恒关（arena 分析管线未迁移），见 agents_family._tdx_live_status
    assert data["tdx"]["real_trading_enabled"] is False


def test_metrics_services_probe_qm_ports(client):
    res = client.get(f"{BASE}/metrics")
    assert res.status_code == 200
    data = res.json()["data"]
    assert set(data["services"]) == {"api", "engine", "trade", "stream"}
    assert all(v in ("up", "down") for v in data["services"].values())
    assert data["markets"]["us"]["agents"] == 1
    assert data["markets"]["cn"]["agents"] == 1
    assert isinstance(data["latest_trade_age_sec"], int)
    assert isinstance(data["generated_at"], int)


# ---------- 配置族 ----------


def test_comp_config_get_returns_catalog_and_selection(client):
    res = client.get(f"{BASE}/comp-config", params={"market": "cn"})
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["market"] == "cn"
    assert {m["id"] for m in data["catalog"]} == {
        "baseline",
        "monk",
        "awareness",
        "leverage",
    }
    assert data["selection"] == {"alpha": ["monk"]}


def test_comp_config_put_cleans_invalid_ids_and_partitions_by_market(client):
    res = client.put(
        f"{BASE}/comp-config",
        params={"market": "hk"},
        json={"selection": {"glm": ["monk", "bogus", "awareness"]}},
    )
    assert res.status_code == 200
    assert res.json()["data"]["selection"] == {"glm": ["monk", "awareness"]}

    # 按市场分区：hk 写入不影响 cn；落盘在 QM 数据根的 arena/ 下
    cn = client.get(f"{BASE}/comp-config", params={"market": "cn"}).json()["data"]
    assert cn["selection"] == {"alpha": ["monk"]}
    # 同分区再写一次（覆盖）
    client.put(f"{BASE}/comp-config", params={"market": "cn"}, json={"selection": {}})
    cn2 = client.get(f"{BASE}/comp-config", params={"market": "cn"}).json()["data"]
    assert cn2["selection"] == {}


def test_comp_config_put_rejects_bad_body(client):
    bad_shape = client.put(
        f"{BASE}/comp-config", params={"market": "cn"}, json={"selection": []}
    )
    assert bad_shape.status_code == 400
    bad_ids = client.put(
        f"{BASE}/comp-config", params={"market": "cn"}, json={"selection": {"m": "x"}}
    )
    assert bad_ids.status_code == 400


def test_broker_market_default_then_put_validated(client, arena_root):
    res = client.get(f"{BASE}/broker-market")
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["mapping"] == {"cn": "tdx", "hk": "tiger", "us": "ibkr"}
    assert "tdx" in data["choices"]["cn"]

    put = client.put(
        f"{BASE}/broker-market", json={"values": {"cn": "tdx", "hk": "nope"}}
    )
    assert put.status_code == 200
    assert put.json()["data"]["mapping"]["cn"] == "tdx"
    # 非法券商值被忽略（hk 保持默认，而不是写进 "nope"）
    assert put.json()["data"]["mapping"]["hk"] == "tiger"
    assert (arena_root / "arena" / "broker_market.json").is_file()


# ---------- 族 8：静态文件 ----------


def test_static_file_served_from_data_root(client):
    res = client.get(f"{BASE}/data/benchmark_nasdaq100.json")
    assert res.status_code == 200
    assert res.json() == []


def test_static_file_outside_root_is_404(client, arena_root):
    """越界路径必须 404（上游实现对 ../ 不做处理，是移植时堵掉的安全洞）。"""
    outside = arena_root.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    res = client.get(f"{BASE}/data/../outside.txt")
    assert res.status_code == 404

    # HTTP 客户端可能归一化 ../，再直接调 handler 断言守卫本身
    from backend.services.api.routers.agent_arena.static_family import arena_data_file

    with pytest.raises(HTTPException) as excinfo:
        arena_data_file("../outside.txt", _current_user={})
    assert excinfo.value.status_code == 404


def test_static_missing_file_is_404(client):
    assert client.get(f"{BASE}/data/nope.json").status_code == 404


# ---------- 数据根解析 ----------


def test_data_root_prefers_sentinel_candidate(monkeypatch):
    """无 env 覆盖时按哨兵文件选真数据根（宿主 <repo>/data 或容器 /data）。"""
    monkeypatch.delenv("QM_ARENA_DATA_ROOT", raising=False)
    from backend.services.agent_arena.arena_config import data_root

    root = data_root()
    assert (root / "arena" / "quanttrader" / "MANIFEST.jsonl").is_file()


# ---------- 族 2：行情实验室（A3）----------


def test_lab_symbols_search_by_code_and_name(client):
    r = client.get(f"{BASE}/market-lab/symbols", params={"q": "6005"})
    assert r.status_code == 200
    assert r.json() == {
        "success": True,
        "data": [{"code": "600519.SH", "name": "贵州茅台"}],
    }
    by_name = client.get(f"{BASE}/market-lab/symbols", params={"q": "茅台"}).json()[
        "data"
    ]
    assert [x["code"] for x in by_name] == ["600519.SH"]
    limited = client.get(
        f"{BASE}/market-lab/symbols", params={"q": "", "limit": 2}
    ).json()["data"]
    assert len(limited) == 2  # 全量 3 条，limit 生效


def test_lab_klines_adj_dispatch_dates_and_nan_filter(client):
    """不复权 8 根（NaN 行剔除、volume NULL 归 0）；后复权走另一目录。"""
    d = client.get(
        f"{BASE}/market-lab/klines",
        params={"symbol": "600519", "adj": "unadjusted", "limit": 60},
    ).json()["data"]
    assert d["symbol"] == "600519.SH" and d["name"] == "贵州茅台"
    assert d["adj"] == "unadjusted" and d["count"] == 8 == len(d["bars"])
    assert d["bars"][0]["date"] == "2026-08-20"  # 旧→新
    assert d["bars"][-1]["date"] == "2026-08-31"
    assert d["bars"][-1]["volume"] == 0  # NULL → 0
    assert set(d["bars"][0]) == {"date", "open", "high", "low", "close", "volume"}

    win = client.get(
        f"{BASE}/market-lab/klines",
        params={
            "symbol": "600519",
            "adj": "unadjusted",
            "limit": 60,
            "start": "20260824",
            "end": "20260827",
        },
    ).json()["data"]
    assert [b["date"] for b in win["bars"]] == [
        "2026-08-24",
        "2026-08-25",
        "2026-08-26",
        "2026-08-27",
    ]

    bwd = client.get(
        f"{BASE}/market-lab/klines",
        params={"symbol": "600519", "adj": "backward", "limit": 60},
    ).json()["data"]
    assert bwd["count"] == 3 and bwd["bars"][-1]["close"] == 54.6

    bad = client.get(
        f"{BASE}/market-lab/klines",
        params={"symbol": "600519", "adj": "bogus", "limit": 60},
    )
    assert bad.status_code == 200  # 入参错误走信封（上游语义）
    assert bad.json()["success"] is False and "K线查询失败" in bad.json()["error"]


def test_lab_klines_symbol_injection_is_parameterized(client):
    """symbol 里的引号/SQL 必须当普通字符串（改写点③）——注入载荷查 0 行。

    上游 f-string 拼 SQL 时该载荷会倾出全表（DuckDB 表函数甚至可读任意
    本地文件）；参数化后合法输入行为逐位不变，载荷只会匹配不到任何 symbol。
    """
    evil = client.get(
        f"{BASE}/market-lab/klines",
        params={"symbol": "x' OR 1=1 --", "adj": "unadjusted", "limit": 60},
    ).json()
    assert evil["success"] is True
    assert evil["data"]["count"] == 0 and evil["data"]["bars"] == []


def test_lab_strategies_registry_shape(client):
    data = client.get(f"{BASE}/market-lab/strategies").json()["data"]
    ids = {s["id"] for s in data}
    assert {
        "sma_cross",
        "ema_trend",
        "rsi_reversal",
        "boll_breakout",
        "donchian",
        "atr_trend",
    } <= ids
    for s in data:
        assert set(s) == {"id", "name", "desc", "params"}


def test_lab_backtest_error_paths_are_http_400(client):
    # 未知策略 → 400（HTTPException 档，不走信封）
    r = client.post(
        f"{BASE}/market-lab/backtest", json={"strategy": "nope", "symbol": "600519"}
    )
    assert r.status_code == 400 and "未知策略" in r.json()["detail"]
    # 已知策略但后复权只有 3 根 → K线不足 400
    r2 = client.post(
        f"{BASE}/market-lab/backtest",
        json={"strategy": "sma_cross", "symbol": "600519", "adj": "backward"},
    )
    assert r2.status_code == 400 and "K线不足" in r2.json()["detail"]
    # 缺 symbol → 空 K 线 → 同 400
    r3 = client.post(f"{BASE}/market-lab/backtest", json={"strategy": "sma_cross"})
    assert r3.status_code == 400


def test_lab_backtest_passes_kwargs_and_reports_runtime_error(client, monkeypatch):
    captured: dict = {}

    def fake_run_backtest(**kw):
        captured.update(kw)
        return {
            "stats": {},
            "trades": [],
            "equity": [],
            "indicators": None,
            "meta": {"strategy": kw["strategy_id"], "name": "双均线"},
        }

    monkeypatch.setattr(
        "backend.services.agent_arena.market_lab.run_backtest", fake_run_backtest
    )
    r = client.post(
        f"{BASE}/market-lab/backtest",
        json={
            "strategy": "sma_cross",
            "symbol": "600519.SH",
            "params": {"fast": 3},
            "start": "20260101",
        },
    )
    assert r.status_code == 200 and r.json()["success"] is True
    assert captured == {
        "strategy_id": "sma_cross",
        "symbol": "600519.SH",
        "adj": "backward",  # payload 缺省 → 回测口径
        "start": "20260101",
        "end": "",
        "params": {"fast": 3},
    }

    # 运行时缺失（B1 前的 pynecore 未装）→ 200 + success:false 的诚实降级
    def raising(**kw):
        raise RuntimeError("PyneCore 运行时未安装：pip install 'pynesys-pynecore[cli]'")

    monkeypatch.setattr("backend.services.agent_arena.market_lab.run_backtest", raising)
    r2 = client.post(
        f"{BASE}/market-lab/backtest",
        json={"strategy": "sma_cross", "symbol": "600519"},
    )
    assert r2.status_code == 200
    assert r2.json()["success"] is False and "回测失败" in r2.json()["error"]


def test_lab_batch_runs_not_swallowed_by_run_id_route(client):
    """/batch/runs 必须命中字面量路由，不能被 /batch/{run_id} 吞成 404。"""
    data = client.get(f"{BASE}/market-lab/batch/runs").json()["data"]
    assert [d["run_id"] for d in data] == [
        "20260930-120000-hs300",
        "20260929-090000-zz500",
    ]
    assert "rows" not in data[0] and "ranking" not in data[0]  # 摘要形状
    assert data[0]["pool"] == "hs300"


def test_lab_batch_pools_counts_missing_file_as_zero(client):
    data = client.get(f"{BASE}/market-lab/batch/pools").json()["data"]
    by_id = {d["id"]: d for d in data}
    assert {"sz50", "hs300", "zz500", "zz800", "zz1000", "kc50", "cyb"} == set(by_id)
    assert by_id["hs300"]["label"] == "沪深300" and by_id["hs300"]["count"] == 2
    assert by_id["zz500"]["count"] == 0  # 缺成分文件不炸整表


def test_lab_batch_detail_filter_sort_and_404(client):
    d = client.get(f"{BASE}/market-lab/batch/20260930-120000-hs300").json()["data"]
    assert d["run_id"] == "20260930-120000-hs300" and len(d["rows"]) == 3
    assert d["ranking"][0]["strategy"] == "sma_cross"
    assert d["skipped"][0]["symbol"] == "300750.SZ" and d["counts"] == {"rows": 3}

    by_net = client.get(
        f"{BASE}/market-lab/batch/20260930-120000-hs300",
        params={"strategy": "sma_cross", "sort": "net"},
    ).json()["data"]
    assert [r["net_pct"] for r in by_net["rows"]] == [8.0, 5.0]
    by_trades = client.get(
        f"{BASE}/market-lab/batch/20260930-120000-hs300",
        params={"strategy": "sma_cross", "sort": "trades"},
    ).json()["data"]
    assert [r["trades"] for r in by_trades["rows"]] == [4, 3]

    assert client.get(f"{BASE}/market-lab/batch/no-such-run").status_code == 404


def test_lab_library_list_filters_and_edited_priority(client):
    d = client.get(f"{BASE}/market-lab/library").json()["data"]
    assert d["filtered"] == 2 and [i["id"] for i in d["items"]] == ["0001", "0002"]
    assert d["total"] == 2 and d["usable"] == 2
    cat = client.get(f"{BASE}/market-lab/library", params={"category": "震荡"}).json()[
        "data"
    ]
    assert [i["id"] for i in cat["items"]] == ["0002"]
    q = client.get(f"{BASE}/market-lab/library", params={"q": "均线"}).json()["data"]
    assert [i["id"] for i in q["items"]] == ["0001"]
    page = client.get(
        f"{BASE}/market-lab/library", params={"limit": 1, "offset": 1}
    ).json()["data"]
    assert [i["id"] for i in page["items"]] == ["0002"]

    # edited/ 优先于 source/（列表不带源码，单条才带）
    s1 = client.get(f"{BASE}/market-lab/library/0001").json()["data"]
    assert s1["edited"] is True and "t1-edited" in s1["source"]
    s2 = client.get(f"{BASE}/market-lab/library/0002").json()["data"]
    assert s2["edited"] is False and "t2" in s2["source"]

    assert client.get(f"{BASE}/market-lab/library/9999").status_code == 404
    assert client.get(f"{BASE}/market-lab/library/bad-id!").status_code == 404


def test_lab_note_reads_index_and_400_on_bad_id(client):
    n1 = client.get(f"{BASE}/market-lab/library/0001/note").json()["data"]
    assert n1 == {
        "id": "0001",
        "note": "",
        "tags": [],
        "rating": None,
        "status": "待研究",
        "by": "user",
        "updated": "",
    }
    n2 = client.get(f"{BASE}/market-lab/library/0002/note").json()["data"]
    assert n2["note"] == "等回踩" and n2["tags"] == ["震荡"]
    assert n2["status"] == "观察中" and n2["rating"] == 3

    idx = client.get(f"{BASE}/market-lab/notes").json()["data"]
    assert idx == {"ids": ["0002"], "count": 1}

    # note 端点非法 id 给 400（与 get_source 的 404 分档不同，照抄上游）
    assert client.get(f"{BASE}/market-lab/library/!!/note").status_code == 400


def test_lab_versions_transpile_and_job_null_report(client):
    v = client.get(f"{BASE}/market-lab/library/0001/versions").json()["data"]
    assert v["count"] == 1
    assert v["versions"][0]["version"] == "20260930T120000000000"
    assert client.get(f"{BASE}/market-lab/library/bad!/versions").status_code == 404

    t = client.get(f"{BASE}/market-lab/transpile").json()["data"]
    assert set(t) == {"0001"}  # 0002 只有排队请求，无 meta/report → 不算条目
    assert t["0001"]["title"] == "双均线" and t["0001"]["has_report"] is True
    assert t["0001"]["net_pct"] == 4.2 and t["0001"]["job"]["status"] == "done"

    j1 = client.get(f"{BASE}/market-lab/library/0001/job").json()["data"]
    assert j1["job"]["status"] == "done" and j1["report"]["symbol"] == "600519.SH"
    # 0002 只在队列 → queued；没报告时 report 必须是 null（JS 里 {} 是 truthy，会炸 UI）
    j2 = client.get(f"{BASE}/market-lab/library/0002/job").json()["data"]
    assert j2["job"]["status"] == "queued" and j2["report"] is None


# ---------- 族 2 写面（B2：编辑/备注/对话/采用/回滚/转写回测入队） ----------
# 写端点只落文件、执行在宿主 worker；这里锁的是「落对了文件 + 错误分档」，
# 而不是任何执行行为（执行链路的测试见 test_pine_*.py）。


def test_lab_save_source_writes_edited_and_reset_removes(client, arena_root):
    edited = arena_root / "pine_library" / "edited" / "0002.pine"
    r = client.put(
        f"{BASE}/market-lab/library/0002",
        json={"source": "//@version=5\nstrategy('t2-edited')\n"},
    )
    assert r.status_code == 200 and r.json()["success"] is True
    assert r.json()["data"]["lines"] == 2
    assert edited.read_text(encoding="utf-8") == "//@version=5\nstrategy('t2-edited')\n"
    # 生效来源翻到 edited（get_source 的 edited 标记同步翻真）
    assert client.get(f"{BASE}/market-lab/library/0002").json()["data"]["edited"] is True

    # 空源码 → 400；非法 id → 400（写面统一 ValueError→400，读面才是 404）
    assert client.put(f"{BASE}/market-lab/library/0002",
                      json={"source": "   "}).status_code == 400
    assert client.put(f"{BASE}/market-lab/library/bad!",
                      json={"source": "x"}).status_code == 400

    d1 = client.delete(f"{BASE}/market-lab/library/0002").json()["data"]
    assert d1 == {"id": "0002", "removed": True}
    assert not edited.exists()
    d2 = client.delete(f"{BASE}/market-lab/library/0002").json()["data"]
    assert d2["removed"] is False  # 幂等：再删一次如实报「没有可删的」


def test_lab_note_save_roundtrip_and_validation(client, arena_root):
    r = client.put(f"{BASE}/market-lab/library/0001/note", json={
        "note": "突破放量才做", "tags": ["趋势", "量能"], "rating": 4,
        "status": "观察中"})
    assert r.status_code == 200
    got = client.get(f"{BASE}/market-lab/library/0001/note").json()["data"]
    assert got["note"] == "突破放量才做" and got["tags"] == ["趋势", "量能"]
    assert got["rating"] == 4 and got["status"] == "观察中"
    assert (arena_root / "pine_library" / "notes" / "0001.json").exists()
    assert client.get(f"{BASE}/market-lab/notes").json()["data"]["ids"] == ["0001", "0002"]

    # rating 越界与非法 id 都是 400（越界报错而不是静默截断）
    assert client.put(f"{BASE}/market-lab/library/0001/note",
                      json={"rating": 6}).status_code == 400
    assert client.put(f"{BASE}/market-lab/library/!!/note",
                      json={"note": "x"}).status_code == 400


def test_lab_chat_enqueue_poll_and_guards(client, arena_root):
    import re

    qdir = arena_root / "pine_library" / "chat" / "queue"
    r = client.post(f"{BASE}/market-lab/library/0001/chat",
                    json={"message": "这个策略在震荡市表现如何？"})
    assert r.status_code == 200 and r.json()["success"] is True
    data = r.json()["data"]
    assert data["queued"] is True
    assert re.match(r"^0001-\d{8}T\d{6}-[0-9a-f]{6}$", data["job_id"])
    assert data["request"]["mode"] == "ask"
    assert (qdir / f"{data['job_id']}.json").exists()

    # 还没被 worker 消费 → 轮询给 queued（不是 404）；线程还没写过 → 空线程
    j = client.get(f"{BASE}/market-lab/library/0001/chat/{data['job_id']}").json()["data"]
    assert j["status"] == "queued" and j["reply"] == ""
    th = client.get(f"{BASE}/market-lab/library/0001/chat").json()["data"]
    assert th == {"id": "0001", "count": 0, "messages": []}

    # 上一条还在处理 → 再发 400；未知 mode / 空消息 / 非法 id 同样 400
    assert client.post(f"{BASE}/market-lab/library/0001/chat",
                       json={"message": "再来一条"}).status_code == 400
    assert client.post(f"{BASE}/market-lab/library/0002/chat",
                       json={"message": "x", "mode": "chat"}).status_code == 400
    assert client.post(f"{BASE}/market-lab/library/0002/chat",
                       json={"message": "  "}).status_code == 400
    assert client.post(f"{BASE}/market-lab/library/bad!/chat",
                       json={"message": "x"}).status_code == 400
    # 线程读取的非法 id 走 404（上游分档：对话读面照 chat_thread 的语义）
    assert client.get(f"{BASE}/market-lab/library/bad!/chat").status_code == 404
    # 编造的 job_id → 404（queued 判据之外一律「任务不存在」）
    assert client.get(
        f"{BASE}/market-lab/library/0001/chat/0001-20260101T000000-abcdef"
    ).status_code == 404


def test_lab_chat_apply_candidate_writes_edited_and_requeues(client, arena_root):
    lib = arena_root / "pine_library"
    job_id = "0001-20261008T101500-abcdef"
    cand_dir = lib / "chat" / "0001" / job_id
    cand_dir.mkdir(parents=True, exist_ok=True)
    (lib / "chat" / "jobs").mkdir(parents=True, exist_ok=True)
    candidate_pine = "//@version=5\nstrategy('t1-candidate')\n"
    (cand_dir / "candidate.pine").write_text(candidate_pine, encoding="utf-8")
    (lib / "chat" / "jobs" / f"{job_id}.json").write_text(json.dumps({
        "job_id": job_id, "id": "0001", "status": "done", "stage": "compare",
        "reply": "已按你要求加上量能过滤", "problems": []}), encoding="utf-8")

    r = client.post(f"{BASE}/market-lab/library/0001/chat/{job_id}/apply",
                    json={"symbol": "600519.SH"})
    assert r.status_code == 200 and r.json()["success"] is True
    got = r.json()["data"]
    assert got["applied"] is True and got["queued"] is True
    assert (lib / "edited" / "0001.pine").read_text(encoding="utf-8") == candidate_pine
    # 采用前先快照了「当前这版」（t1-edited），快照名符合版本正则
    assert (lib / "versions" / "0001" / f"{got['snapshot']}.pine").read_text(
        encoding="utf-8") == "//@version=5\nstrategy('t1-edited')\n"
    q = json.loads((arena_root / "pine_transpile" / "queue" / "0001.json").read_text(
        encoding="utf-8"))
    assert q["force"] is True and q["symbol"] == "600519.SH"

    # 已在队列里 → 本次采用照样生效，但如实回报「没重跑」（不是静默成功）
    r2 = client.post(f"{BASE}/market-lab/library/0001/chat/{job_id}/apply",
                     json={"symbol": "600519.SH"})
    d2 = r2.json()["data"]
    assert d2["applied"] is True and d2["queued"] is False
    assert "已在队列里" in d2["queue_error"]

    # 静态没过的候选不能采用 → 400
    (cand_dir / "meta.json").write_text(json.dumps({"problems": ["禁止动态执行"]}),
                                        encoding="utf-8")
    (lib / "chat" / "jobs" / f"{job_id}.json").write_text(json.dumps({
        "job_id": job_id, "id": "0001", "status": "done",
        "problems": ["禁止动态执行：eval("]}), encoding="utf-8")
    assert client.post(
        f"{BASE}/market-lab/library/0001/chat/{job_id}/apply", json={}
    ).status_code == 400
    # 空 body 也合法（全默认）：不存在的任务 → 400
    assert client.post(
        f"{BASE}/market-lab/library/0001/chat/0001-20260101T000000-abcdef/apply",
        json={},
    ).status_code == 400


def test_lab_revert_uses_latest_snapshot_and_keeps_it_reversible(client, arena_root):
    lib = arena_root / "pine_library"
    r = client.post(f"{BASE}/market-lab/library/0001/revert", json={})
    assert r.status_code == 200 and r.json()["success"] is True
    got = r.json()["data"]
    assert got["reverted"] == "20260930T120000000000"
    assert (lib / "edited" / "0001.pine").read_text(encoding="utf-8") == \
        "//@version=5\nstrategy('t1-old')\n"
    # 回滚前把「当前版」（t1-edited）也快照了 → 还能切回来，且用掉的那版已被消费
    assert got["queued"] is True  # report 有 symbol=600519.SH → 自动重跑
    q = json.loads((arena_root / "pine_transpile" / "queue" / "0001.json").read_text(
        encoding="utf-8"))
    assert q["force"] is True
    versions = client.get(f"{BASE}/market-lab/library/0001/versions").json()["data"]
    assert versions["count"] == 1

    # 没有快照可回滚 → 400；非法版本号 → 400
    assert client.post(f"{BASE}/market-lab/library/0002/revert", json={}).status_code == 400
    assert client.post(f"{BASE}/market-lab/library/0001/revert",
                       json={"version": "不是版本"}).status_code == 400


def test_lab_backtest_enqueue_dedup_and_error_mapping(client, arena_root, monkeypatch):
    r = client.post(f"{BASE}/market-lab/library/0001/backtest",
                    json={"symbol": "600519.SH", "adj": "backward"})
    assert r.status_code == 200 and r.json()["data"]["queued"] is True
    q = json.loads((arena_root / "pine_transpile" / "queue" / "0001.json").read_text(
        encoding="utf-8"))
    assert q["id"] == "0001" and q["force"] is False and q["model"] == ""

    # 已在队列里 → 400；未知复权 → 400；非法 id → 400
    assert client.post(f"{BASE}/market-lab/library/0001/backtest",
                       json={"symbol": "600519.SH"}).status_code == 400
    assert client.post(f"{BASE}/market-lab/library/0002/backtest",
                       json={"symbol": "600519.SH", "adj": "sideways"}).status_code == 400
    assert client.post(f"{BASE}/market-lab/library/bad!/backtest",
                       json={"symbol": "600519.SH"}).status_code == 400

    # 其余异常 → 200 + success:false（对外语义照抄上游，不 5xx）
    from backend.services.agent_arena import pine_library as pl

    def boom(*a, **k):
        raise RuntimeError("队列磁盘只读")

    monkeypatch.setattr(pl, "enqueue_backtest", boom)
    r2 = client.post(f"{BASE}/market-lab/library/0001/backtest", json={})
    assert r2.status_code == 200
    assert r2.json()["success"] is False and "入队失败" in r2.json()["error"]


# ---------- 族 A4：实况（/live/*） ----------
# 文件类用合成 logs/（arena_root/logs/）；real-* / l2 真读本机 PG（本地独有=集成
# 测试，断言之于环境取真实数据也成立的形状与口径）；桥与行情源用 monkeypatch
# （桥在线与否不该决定测试成败），其中 asset 拍平为 number 是唯一有意偏离上游处，
# 单钉回归。age_sec 与 ts 互相对账——naive-UTC 快照列读错会在这里现形。


def _write_live_logs(root: Path, files: dict) -> None:
    """往合成数据根的 logs/ 写 live_* 文件（值：list[dict] → jsonl；str → 原文）。"""
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    for name, rows in files.items():
        if isinstance(rows, str):
            (logs / name).write_text(rows, encoding="utf-8")
        else:
            _write_jsonl(logs / name, rows)


def _stub_pg_closed_empty(monkeypatch) -> None:
    """把 /live/closed 的 PG 回合台账源钉为空（文件口径用例隔离真库）。

    2026-10-20 起 /live/closed 以 PG 回合台账（closed=true）为主源；本文件的
    「仅文件重建」用例必须显式置空，否则会读到容器真库里的现网 agent 清仓。
    """
    from backend.services.agent_arena import live_family as lf_service

    async def _empty(session, limit=60):
        return []

    monkeypatch.setattr(lf_service, "load_closed_pg", _empty)


def _stub_pg_fills_empty(monkeypatch) -> None:
    """把 /live/trades 的 PG 真单成交源钉为空。

    本文件的 client 夹具真连 PG（见上方池纪律注释，2026-10-09 起 /live/trades
    合并 PG 真单成交），需要「仅文件口径」断言的用例统一走这个替身。
    """
    from backend.services.agent_arena import live_family as lf_service

    async def _empty(session, limit=200):
        return []

    monkeypatch.setattr(lf_service, "load_real_fills_pg", _empty)


def _stub_pg_ledger_empty(monkeypatch) -> None:
    """把 /live/equity 的 PG 活账源钉为空（文件口径用例隔离容器真库）。

    2026-10-20 起 /live/equity 在文件线之外还会按 PG 活账重建分账曲线；
    「新机无 logs/」用例必须连 PG 源一起置空，否则读到现网 agent 的历史净值。
    """
    from backend.services.agent_arena import live_family as lf_service

    async def _empty(session, **_kwargs):
        return {"agents": {}}

    monkeypatch.setattr(lf_service, "read_pg_ledger", _empty)


def test_live_file_endpoints_survive_missing_logs(client, arena_root, monkeypatch):
    """logs/ 目录缺失（新机首启）→ 三个文件类端点空成功，不 500。

    共用夹具的 logs/ 里有一条 glm 卖出成交（A2 交易明细用例特意造的），
    这里把数据根指到一个不存在的空目录，才还原「新机尚无 logs/」的形态；
    data_root() 按函数读 env，切换即时生效。PG 源钉空 → 还原「文件空 ⇒ 空成功」。
    """
    monkeypatch.setenv("QM_ARENA_DATA_ROOT", str(arena_root / "empty_root"))
    _stub_pg_fills_empty(monkeypatch)
    _stub_pg_closed_empty(monkeypatch)
    _stub_pg_ledger_empty(monkeypatch)
    assert client.get(f"{BASE}/live/equity").json()["data"] == {
        "total": [],
        "agents": {},
    }
    assert client.get(f"{BASE}/live/trades").json()["data"] == []
    assert client.get(f"{BASE}/live/closed").json()["data"] == []


def test_live_equity_sorted_points_and_envelope(client, arena_root):
    _write_live_logs(
        arena_root,
        {
            "live_equity.jsonl": [
                {
                    "date": "2026-09-02",
                    "ts": "2026-09-02T15:00:00+08:00",
                    "value": 920000,
                },
                {
                    "date": "2026-09-01",
                    "ts": "2026-09-01T15:00:00+08:00",
                    "value": 918000,
                },
                {
                    "date": "2026-09-01",
                    "ts": "2026-09-01T15:00:00+08:00",
                    "value": 100000,
                    "agent": "alpha",
                },
                {
                    "date": "2026-09-03",
                    "ts": "2026-09-03T15:00:00+08:00",
                },  # 无 value → 跳过
            ]
        },
    )
    body = client.get(f"{BASE}/live/equity").json()
    assert body["success"] is True
    # 乱序追加 → 输出必须按 ts 升序（前端折线按序连线，乱序画反）
    assert [p["value"] for p in body["data"]["total"]] == [918000.0, 920000.0]
    assert [p["value"] for p in body["data"]["agents"]["alpha"]] == [100000.0]


def test_live_trades_filter_fill_confirm_and_price_none(client, arena_root, monkeypatch):
    _stub_pg_fills_empty(monkeypatch)  # 纯文件口径（PG 源真库有历史成交）
    _write_live_logs(
        arena_root,
        {
            "live_trade_20260901.jsonl": [
                {
                    "ts": "2026-09-01T09:31:00+08:00",
                    "mode": "execute",
                    "agent": "alpha",
                    "code": "002074.SZ",
                    "side": "buy",
                    "price": 21.0,
                    "result": {"order_id": "1", "filled_price": 21.0},
                },
                {
                    "ts": "2026-09-01T10:00:00+08:00",
                    "mode": "analysis",
                    "agent": "alpha",
                },
                {
                    "ts": "2026-09-01T13:00:00+08:00",
                    "mode": "fill_confirm",
                    "agent": "alpha",
                    "code": "002074.SZ",
                    "volume": 100,
                    "price": 21.5,
                    "side": "sell",
                },
                {
                    "ts": "2026-09-01T14:00:00+08:00",
                    "mode": "sell",
                    "agent": "alpha",
                    "code": "600036.SH",
                    "result": {"order_id": "2"},
                },  # 无价行
            ],
            "live_trade_20260902.jsonl": [
                {
                    "ts": "2026-09-02T09:31:00+08:00",
                    "mode": "execute",
                    "agent": "alpha",
                    "code": "600036.SH",
                    "side": "buy",
                    "price": 33.0,
                    "result": {},
                },
            ],
        },
    )
    body = client.get(f"{BASE}/live/trades").json()
    assert body["success"] is True
    recs = body["data"]
    # analysis 行不进（只收买卖 mode）；跨文件合并、最新在前
    assert len(recs) == 4
    assert recs[0]["ts"] == "2026-09-02T09:31:00+08:00"
    assert recs[-1]["ts"] == "2026-09-01T09:31:00+08:00"
    fc = next(r for r in recs if r["mode"] == "fill_confirm")
    # fill_confirm 平铺字段 → 补一层 fill 让前端 hasFill 判定放行
    assert fc["fill"] == {"order_id": None, "filled_price": 21.5, "filled_volume": 100}
    # 缺价行显式 price=None（桥补价 deferred = 上游「桥不可用」同款分支）
    no_price = next(r for r in recs if r["mode"] == "sell")
    assert no_price["price"] is None
    assert (
        len(client.get(f"{BASE}/live/trades", params={"limit": 2}).json()["data"]) == 2
    )


def test_live_trades_merges_pg_fills_with_bridge_order_dedup(
    client, arena_root, monkeypatch
):
    """PG 真单成交与文件行合并：桥委托号重复的丢弃、文件缺的补进、按 ts 倒序。

    2026-10-09：文件写入者随 quant-Trader 退役停摆，强平/守护直连成交（如
    2026-09-24 14:41 的 600036.SH 卖出）只在 PG——不合并就永远不显示；
    而两源重叠期（09-10~09-24）同一桥单两边都记，按 orders.exchange_order_id
    （= 文件行 order_id，同为 TDX 委托编号空间）去重，文件行保留原样。
    """
    _write_live_logs(
        arena_root,
        {
            "live_trade_20260924.jsonl": [
                {
                    "ts": "2026-09-24T10:00:59.159927+08:00",
                    "mode": "execute",
                    "agent": "glm-5.3-flash",
                    "code": "600176.SH",
                    "side": "sell",
                    "volume": 100,
                    "price": 44.41,
                    "fill": {
                        "order_id": "119667",
                        "filled_price": 44.41,
                        "filled_volume": 100,
                    },
                },
            ],
        },
    )
    from backend.services.agent_arena import live_family as lf_service

    def _pg_row(ts: str, code: str, bid: str, agent: str | None) -> dict:
        return {
            "ts": ts,
            "mode": "qm_fill",
            "source": "qm_pg",
            "agent": agent,
            "code": code,
            "side": "sell",
            "volume": 100,
            "price": 44.41,
            "fill": {"order_id": bid, "filled_price": 44.41, "filled_volume": 100},
            "result": {"order_id": bid, "status": "filled"},
        }

    async def _fake_pg(session, limit=200):
        return [
            # 桥单 119667 文件已记过 → 去重丢弃（防重叠期双显）
            _pg_row("2026-09-24T10:00:58.021466+08:00", "600176.SH", "119667", "glm-5.3-flash"),
            # 文件没有的桥单（强平/守护直连）→ 补进展示
            _pg_row("2026-09-24T14:41:38.513272+08:00", "600036.SH", "601234", None),
        ]

    monkeypatch.setattr(lf_service, "load_real_fills_pg", _fake_pg)
    recs = client.get(f"{BASE}/live/trades").json()["data"]
    # 文件 1 行（原样，mode 不变）+ PG 新 1 行；最新在前
    assert [r["ts"] for r in recs] == [
        "2026-09-24T14:41:38.513272+08:00",
        "2026-09-24T10:00:59.159927+08:00",
    ]
    assert recs[0]["mode"] == "qm_fill"
    assert recs[0]["agent"] is None  # orders.agent 为空（P2.7 前）不吞行
    assert recs[0]["fill"]["filled_volume"] == 100  # 前端 hasFill 判据放行
    assert recs[1]["mode"] == "execute"


def test_live_closed_reconstructs_and_dedupes_backfill(
    client, arena_root, monkeypatch
):
    # 本用例口径 = 文件账本（夹带 beta 持仓基线）。/live/closed 是 PG 优先，
    # 容器真库现有在产 agent（deepseek-v4-* 等）→ 不隔离会拿真账当基线，
    # 夹具账本被架空（beta 卖出被误判清仓）。打桩 PG 空 → 走文件兜底。
    from backend.services.agent_arena import live_family

    async def _empty_pg(session, *, tenant_id="default"):
        return {"version": 1, "agents": {}}

    monkeypatch.setattr(live_family, "read_pg_ledger", _empty_pg)
    _stub_pg_closed_empty(monkeypatch)
    _write_live_logs(
        arena_root,
        {
            "live_trade_20260901.jsonl": [
                {
                    "ts": "2026-09-01T09:30:00+08:00",
                    "mode": "execute",
                    "agent": "alpha",
                    "code": "002074.SZ",
                    "side": "buy",
                    "fill": {
                        "filled_volume": 100,
                        "filled_price": 20.0,
                        "order_id": "b1",
                    },
                },
                {
                    "ts": "2026-09-03T10:00:00+08:00",
                    "mode": "sell",
                    "agent": "alpha",
                    "code": "002074.SZ",
                    "side": "sell",
                    "cost_price": 20.0,
                    "fill": {
                        "filled_volume": 100,
                        "filled_price": 22.0,
                        "order_id": "s1",
                    },
                },
                {
                    "ts": "2026-09-01T09:30:00+08:00",
                    "mode": "execute",
                    "agent": "beta",
                    "code": "600036.SH",
                    "side": "buy",
                    "fill": {"filled_volume": 200, "filled_price": 30.0},
                },
                {
                    "ts": "2026-09-03T10:00:00+08:00",
                    "mode": "sell",
                    "agent": "beta",
                    "code": "600036.SH",
                    "side": "sell",
                    "cost_price": 30.0,
                    "fill": {"filled_volume": 100, "filled_price": 31.0},
                },
            ],
            "live_ledger.json": json.dumps(
                {
                    "version": 1,
                    "agents": {
                        "beta": {
                            "positions": {
                                "600036.SH": {"volume": 100, "cost_price": 30.0}
                            }
                        }
                    },
                },
                ensure_ascii=False,
            ),
            "live_closed_backfill.jsonl": [
                {
                    "ts": "2026-08-20T10:00:00+08:00",
                    "agent": "alpha",
                    "symbol": "002074.SZ",
                    "exit_date": "2026-09-03",
                    "approx": True,
                    "qty": 100,
                    "exit_price": 21.0,
                    "pnl": 100.0,
                },
                {
                    "ts": "2026-08-10T10:00:00+08:00",
                    "agent": "gamma",
                    "symbol": "300750.SZ",
                    "exit_date": "2026-08-10",
                    "approx": True,
                    "qty": 50,
                    "exit_price": 100.0,
                    "pnl": 50.0,
                },
            ],
        },
    )
    body = client.get(f"{BASE}/live/closed").json()
    assert body["success"] is True
    rows = body["data"]
    # alpha：账本无仓 + 卖出事件 → 倒走判清仓，成本取事件自带 → pnl 可算
    live_alpha = [r for r in rows if r["agent"] == "alpha"]
    assert len(live_alpha) == 1
    assert live_alpha[0]["exit_price"] == 22.0 and live_alpha[0]["pnl"] == 200.0
    assert "approx" not in live_alpha[0]  # 实时重建优先于历史补录
    # 买入日归因（2026-10-15 加）：倒走补记最早贡献买入 = 09-01 的那笔
    assert live_alpha[0]["entry_date"] == "2026-09-01"
    assert live_alpha[0]["hold_days"] == pytest.approx(2.02, abs=0.01)
    # beta 账本还持 100 股 → 其卖出不是清仓
    assert not any(r["agent"] == "beta" for r in rows)
    # gamma 只在补录里 → 保留
    assert any(r["agent"] == "gamma" and r.get("approx") for r in rows)


def test_roundtrip_to_closed_row_rejects_partial_and_maps_fields():
    """回合台账 → 已完成行：只认清仓（closed=true），减仓行必须返回 None。

    2026-10-20 用户口径：「已完成」只统计真正清仓的股票，减仓不计入。
    """
    from datetime import datetime, timezone

    from backend.services.agent_arena.live_family import roundtrip_to_closed_row

    base = {
        "agent": "glm-5.3-flash",
        "code": "600338.SH",
        "volume": 800.0,
        "cost_price": 11.83,
        "sell_price": 11.08,
        "realized_pnl": -600.0,
        "buy_ts": datetime(2026, 10, 14, 5, 1, tzinfo=timezone.utc),
        "sell_ts": datetime(2026, 10, 20, 1, 37, 7, tzinfo=timezone.utc),
        "holding_days": 6.0,
    }
    assert roundtrip_to_closed_row({**base, "closed": False}) is None
    row = roundtrip_to_closed_row({**base, "closed": True})
    assert row is not None
    assert row["agent"] == "glm-5.3-flash"
    assert row["symbol"] == "600338.SH"
    assert row["exit_date"] == "2026-10-20"  # UTC 01:37 → 北京 09:37，不得回退一天
    assert row["entry_date"] == "2026-10-14"
    assert row["qty"] == 800
    assert row["pnl"] == -600.0
    assert row["ts"].endswith("Z")


def test_live_closed_merges_pg_roundtrips_and_dedupes_file_rows(
    client, arena_root, monkeypatch
):
    """PG 回合台账（closed=true）与文件重建合并：PG 优先、同键去重。

    文件写入者冻结于 09-29，今天的真清仓只在 PG 回合台账里——不接这条源，
    「已完成」会永远停在旧快照。
    """
    from backend.services.agent_arena import live_family

    async def _empty_pg(session, *, tenant_id="default"):
        return {"version": 1, "agents": {}}

    monkeypatch.setattr(live_family, "read_pg_ledger", _empty_pg)
    _write_live_logs(
        arena_root,
        {
            "live_closed_backfill.jsonl": [
                {
                    "ts": "2026-09-03T10:00:00+08:00",
                    "agent": "alpha",
                    "symbol": "002074.SZ",
                    "exit_date": "2026-09-03",
                    "qty": 100,
                    "exit_price": 22.0,
                    "pnl": 200.0,
                }
            ],
        },
    )

    async def _fake_pg_closed(session, limit=60):
        return [
            {
                "ts": "2026-10-20T01:37:07Z",
                "agent": "glm-5.3-flash",
                "symbol": "600338.SH",
                "exit_date": "2026-10-20",
                "qty": 800,
                "entry_price": 11.83,
                "exit_price": 11.08,
                "notional": 8864.0,
                "fee": 0.0,
                "pnl": -600.0,
                "entry_date": "2026-10-14",
                "hold_days": 6.0,
            },
            # 同 agent+symbol+exit_date 的文件行：PG 优先，只留一行
            {
                "ts": "2026-10-20T02:00:00Z",
                "agent": "deepseek-v4-flash",
                "symbol": "002469.SZ",
                "exit_date": "2026-10-20",
                "qty": 100,
                "entry_price": 6.77,
                "exit_price": 6.73,
                "notional": 673.0,
                "fee": 0.0,
                "pnl": -4.0,
                "entry_date": "2026-10-19",
                "hold_days": 1.0,
            },
        ]

    monkeypatch.setattr(live_family, "load_closed_pg", _fake_pg_closed)
    rows = client.get(f"{BASE}/live/closed").json()["data"]
    assert any(r["agent"] == "glm-5.3-flash" and r["exit_date"] == "2026-10-20" for r in rows)
    assert any(r["agent"] == "alpha" for r in rows)  # 文件历史仍在
    # 去重：同一 (agent, symbol, exit_date) 只出一行
    keys = [(r["agent"], r["symbol"], r["exit_date"]) for r in rows]
    assert len(keys) == len(set(keys))


def test_live_closed_entry_attribution_multibuy_partial_and_missing(
    client, arena_root, monkeypatch
):
    """买入日归因的边界（2026-10-15）：

    - delta 两笔买入 + 中途部分卖出 + 清仓：entry = 最早贡献买入（部分卖出
      的份额是更早买进的，倒走要跨过它继续补记买入需求）；
    - epsilon 只有卖出、日志里没有买点（记录中途才开始）：entry_date 与
      hold_days 保持 None（宁缺勿造），前端显示「—」。
    """
    # 同 reconstructs 用例：PG 打桩为空走文件账本（真库在产 agent 不参与）
    from backend.services.agent_arena import live_family

    async def _empty_pg(session, *, tenant_id="default"):
        return {"version": 1, "agents": {}}

    monkeypatch.setattr(live_family, "read_pg_ledger", _empty_pg)
    _stub_pg_closed_empty(monkeypatch)
    _write_live_logs(
        arena_root,
        {
            "live_trade_20260910.jsonl": [
                {
                    "ts": "2026-09-01T09:30:00+08:00",
                    "mode": "execute",
                    "agent": "delta",
                    "code": "002074.SZ",
                    "side": "buy",
                    "fill": {"filled_volume": 200, "filled_price": 20.0},
                },
                {
                    "ts": "2026-09-02T09:30:00+08:00",
                    "mode": "execute",
                    "agent": "delta",
                    "code": "002074.SZ",
                    "side": "buy",
                    "fill": {"filled_volume": 100, "filled_price": 21.0},
                },
                {
                    "ts": "2026-09-02T14:00:00+08:00",
                    "mode": "sell",
                    "agent": "delta",
                    "code": "002074.SZ",
                    "side": "sell",
                    "fill": {"filled_volume": 150, "filled_price": 21.5},
                },
                {
                    "ts": "2026-09-04T10:00:00+08:00",
                    "mode": "sell",
                    "agent": "delta",
                    "code": "002074.SZ",
                    "side": "sell",
                    "cost_price": 20.33,
                    "fill": {"filled_volume": 150, "filled_price": 22.0},
                },
                {
                    "ts": "2026-09-05T10:00:00+08:00",
                    "mode": "sell",
                    "agent": "epsilon",
                    "code": "600036.SH",
                    "side": "sell",
                    "cost_price": 30.0,
                    "fill": {"filled_volume": 100, "filled_price": 31.0},
                },
            ],
            "live_ledger.json": json.dumps(
                {"version": 1, "agents": {}}, ensure_ascii=False
            ),
        },
    )
    rows = client.get(f"{BASE}/live/closed").json()["data"]
    delta = [r for r in rows if r["agent"] == "delta"]
    assert len(delta) == 1  # 部分卖出那笔不是清仓
    assert delta[0]["exit_date"] == "2026-09-04"
    assert delta[0]["entry_date"] == "2026-09-01"  # 跨过部分卖出补记 → 最早买入
    assert delta[0]["hold_days"] == pytest.approx(3.02, abs=0.01)
    eps = [r for r in rows if r["agent"] == "epsilon"]
    assert len(eps) == 1 and eps[0]["entry_date"] is None
    assert eps[0]["hold_days"] is None


def test_bj_wall_converts_utc_buy_ts_to_beijing():
    """buy_ts（UTC Z 串）→ 北京墙钟：3:07 UTC 显示成 11:07 而非凌晨。"""
    from backend.services.agent_arena.live_family import _bj_wall

    assert _bj_wall("2026-09-14T03:07:00Z") == "2026-09-14T11:07"
    assert _bj_wall("2026-09-14T03:07:00+00:00") == "2026-09-14T11:07"
    # 已是 +08:00 的 aware 串（文件账本）不得二次平移
    assert _bj_wall("2026-09-01T09:31:00+08:00") == "2026-09-01T09:31"
    assert _bj_wall("") == ""
    assert _bj_wall("bogus") == "bogus"


def test_live_ledger_pg_priority_with_file_fallback(client, arena_root, monkeypatch):
    from backend.services.agent_arena import live_family

    _write_live_logs(
        arena_root,
        {
            "live_ledger.json": json.dumps(
                {
                    "version": 1,
                    "agents": {
                        "alpha": {
                            "positions": {
                                "002074.SZ": {
                                    "volume": 100,
                                    "cost_price": 21.07,
                                    "buy_ts": "2026-09-01T09:31:00+08:00",
                                },
                                "600036.SH": {
                                    "volume": 200,
                                    "cost_price": 33.0,
                                    "buy_ts": "2026-09-02T09:31:00+08:00",
                                },
                            }
                        }
                    },
                },
                ensure_ascii=False,
            )
        },
    )

    async def _empty_pg(session, *, tenant_id="default"):
        return {"version": 1, "agents": {}}

    monkeypatch.setattr(live_family, "read_pg_ledger", _empty_pg)
    a = client.get(f"{BASE}/live/ledger").json()["data"]["agents"]["alpha"]
    # PG 空 → 文件兜底：quota 十万、used=Σ(数量×成本)、positions 按 code 排序
    assert a["quota"] == 100000
    assert a["used"] == round(100 * 21.07 + 200 * 33.0, 2)
    assert a["remaining"] == round(100000 - (2107.0 + 6600.0), 2)
    assert [p["code"] for p in a["positions"]] == ["002074.SZ", "600036.SH"]
    assert a["positions"][0]["buy_ts"] == "2026-09-01T09:31"

    async def _busy_pg(session, *, tenant_id="default"):
        return {
            "version": 1,
            "agents": {
                "beta": {
                    "positions": {
                        "600036.SH": {
                            "volume": 100,
                            "cost_price": 30.0,
                            "buy_ts": "2026-09-14T03:07:35.714953Z",
                        }
                    }
                }
            },
        }

    monkeypatch.setattr(live_family, "read_pg_ledger", _busy_pg)
    # PG 有行 → 活账唯一事实，文件不再参与（两源不合并）；
    # PG buy_ts 为 UTC Z 串 → 展示北京墙钟（裸截断显成凌晨 03:07，实为 11:07）
    busy = client.get(f"{BASE}/live/ledger").json()["data"]["agents"]
    assert list(busy) == ["beta"]
    assert busy["beta"]["positions"][0]["buy_ts"] == "2026-09-14T11:07"


def test_live_ledger_quota_overrun_remaining_floored():
    """纯函数：超额占用时 remaining 夹到 0，name 缺失回退 code。"""
    from backend.services.agent_arena.live_family import ledger_view

    doc = {
        "agents": {
            "alpha": {
                "positions": {
                    "002074.SZ": {
                        "volume": 10000,
                        "cost_price": 21.07,
                        "buy_ts": "2026-09-01T09:31:00+08:00",
                    }
                }
            }
        }
    }
    out = ledger_view(doc, {})
    a = out["agents"]["alpha"]
    assert a["used"] > a["quota"] and a["remaining"] == 0.0
    assert a["positions"][0]["name"] == "002074.SZ"  # names 缺 → 回退 code


def test_live_account_flattens_asset_number_and_pnl(client, monkeypatch):
    from backend.services.agent_arena import live_family

    async def _fake_bridge():
        return {
            "account_id": "tdx-default-10000001",
            "asset": {
                "currency": "CNY",
                "balance": 100.0,
                "cash": 500.0,
                "asset": 917654.51,
                "market_value": 300.0,
            },
            "positions": [
                {
                    "stock_code": "002074.SZ",
                    "cost_price": 21.07,
                    "total_volume": 100,
                    "available_volume": 100,
                }
            ],
            "channel_used": "tdx",
        }

    monkeypatch.setattr(live_family, "_bridge_account_query", _fake_bridge)
    monkeypatch.setattr(
        live_family,
        "read_snapshots",
        lambda client, codes: {"002074.SZ": {"Now": "26.07"}},
    )

    async def _fake_ledger(session, *, tenant_id="default"):
        return {
            "agents": {
                "alpha": {
                    "positions": {
                        "002074.SZ": {
                            "volume": 100,
                            "cost_price": 21.07,
                            "buy_ts": "2026-09-14T03:07:00Z",  # UTC Z 串
                        }
                    }
                }
            }
        }

    monkeypatch.setattr(live_family, "read_pg_ledger", _fake_ledger)
    body = client.get(f"{BASE}/live/account").json()
    assert body["success"] is True
    d = body["data"]
    # 桥的 asset 是 dict（上游直接透传 = 前端显示 bug）；唯一有意偏离 = 拍平 number
    assert isinstance(d["asset"], (int, float)) and d["asset"] == 917654.51
    assert d["account_id"] == "tdx-default-10000001"  # 其余桥键透传
    p = d["positions"][0]
    assert p["last_price"] == 26.07
    assert p["pnl_pct"] == round((26.07 - 21.07) / 21.07 * 100, 2)
    assert p["pnl"] == round((26.07 - 21.07) * 100, 2)
    assert p["position_value"] == round(26.07 * 100, 2)
    # 买时由 UTC 转北京墙钟（2026-10-15 修：原样截断显示凌晨 3:07 实为 11:07）
    assert p["buy_time"] == "2026-09-14T11:07"


def test_live_account_asset_fallback_sum_and_bridge_502(client, monkeypatch):
    from backend.services.agent_arena import live_family

    async def _no_total():
        return {"asset": {"cash": 100.0, "market_value": 250.5}, "positions": []}

    monkeypatch.setattr(live_family, "_bridge_account_query", _no_total)
    assert client.get(f"{BASE}/live/account").json()["data"]["asset"] == 350.5

    async def _boom():
        raise RuntimeError("桥查询失败: connect refused")

    monkeypatch.setattr(live_family, "_bridge_account_query", _boom)
    res = client.get(f"{BASE}/live/account")
    assert res.status_code == 502
    assert res.json()["detail"].startswith("桥查询失败")


def test_live_real_account_shape_and_unknown_channel_422(client):
    body = client.get(f"{BASE}/live/real-account", params={"account": "tdx"}).json()
    assert body["success"] is True
    d = body["data"]
    assert d is not None, "tdx 家族最新快照应存在（桥每 30s 同步）"
    assert d["total_asset"] and d["total_asset"] > 0
    assert d["account_id"].startswith("tdx-default-")
    assert isinstance(d["positions"], list)
    for p in d["positions"]:
        assert {"symbol", "name", "cost_price", "volume"} <= set(p)
    # 未知通道 → 422（上游 _real_account_id 同款）
    assert (
        client.get(f"{BASE}/live/real-account", params={"account": "bogus"}).status_code
        == 422
    )


def test_live_real_accounts_age_sec_matches_ts(client):
    """age_sec 与 ts 互证：snapshot_at 是 naive-UTC 列，读成上海墙钟会错 8 小时。"""
    from datetime import datetime, timezone

    body = client.get(f"{BASE}/live/real-accounts").json()
    assert body["success"] is True
    rows = {r["account"]: r for r in body["data"]}
    assert set(rows) == {"tdx", "qmt"}
    tdx = rows["tdx"]
    assert tdx["label"] and tdx["channel"]
    assert tdx["age_sec"] is None or tdx["age_sec"] >= 0
    if tdx["ts"] and tdx["age_sec"] is not None:
        ts = datetime.fromisoformat(tdx["ts"].replace("Z", "+00:00"))
        gap = (datetime.now(timezone.utc) - ts).total_seconds()
        assert abs(gap - tdx["age_sec"]) < 300


def test_live_real_ledger_ascending_dedup_envelope(client):
    body = client.get(f"{BASE}/live/real-ledger", params={"account": "tdx"}).json()
    assert body["success"] is True
    rows = body["data"]
    assert isinstance(rows, list)
    # 上游形状：data 是数组，account/account_id 在信封顶层
    assert body["account"] == "tdx"
    assert body["account_id"].startswith("tdx-default-")
    dates = [r["date"] for r in rows]
    # 同日多行取后写（DISTINCT ON 口径）→ 无重复；输出升序（前端取末元素当最新）
    assert dates == sorted(dates) and len(dates) == len(set(dates))
    for r in rows:
        assert {
            "date",
            "total_asset",
            "cash",
            "market_value",
            "daily_return_pct",
            "total_return_pct",
            "position_count",
            "source",
        } <= set(r)
    assert (
        client.get(f"{BASE}/live/real-ledger", params={"account": "bogus"}).status_code
        == 422
    )


def test_live_l2_factors_shape_and_symbol_filter(client):
    body = client.get(f"{BASE}/live/l2-factors", params={"limit": 5}).json()
    assert body["success"] is True
    rows = body["data"]
    assert len(rows) <= 5
    for r in rows:
        assert {"ts", "symbol", "stock_code", "now_price", "factors"} <= set(r)
    if rows:
        sym = rows[0]["symbol"]  # snapshot.symbol 是前缀式
        fb = client.get(
            f"{BASE}/live/l2-factors", params={"symbol": sym, "limit": 3}
        ).json()
        assert fb["data"] and all(r["symbol"] == sym for r in fb["data"])


def test_live_query_bounds_rejected(client):
    assert client.get(f"{BASE}/live/trades", params={"limit": 0}).status_code == 422
    assert client.get(f"{BASE}/live/closed", params={"limit": 999}).status_code == 422
    assert client.get(f"{BASE}/live/l2-factors", params={"limit": 0}).status_code == 422


def test_live_indices_cn_reads_both_snapshot_field_schemas(client, monkeypatch):
    from backend.services.agent_arena import live_family

    monkeypatch.setattr(
        live_family,
        "read_snapshots",
        lambda client, codes: {
            "000001.SH": {"Now": "3200.0", "PreClose": "3150.0"},
            "000300.SH": {"now": "4100", "pre_close": "4000"},  # 原始推送字段名
            "000016.SH": {"now": "abc"},  # 垃圾价 → 跳过
        },
    )
    body = client.get(f"{BASE}/live/indices", params={"market": "cn"}).json()
    assert body["success"] is True
    rows = {r["code"]: r for r in body["data"]["indices"]}
    assert rows["000001.SH"]["last"] == 3200.0
    assert rows["000001.SH"]["change_pct"] == round((3200 / 3150 - 1) * 100, 2)
    assert rows["000300.SH"]["change_pct"] == round((4100 / 4000 - 1) * 100, 2)
    assert "000016.SH" not in rows


def test_live_snap_last_tolerates_shapes():
    from backend.services.agent_arena.live_family import _snap_last

    assert _snap_last(None) == 0.0
    assert _snap_last({}) == 0.0
    assert _snap_last({"Now": "26.07"}) == 26.07
    assert _snap_last({"now": 26.07}) == 26.07
    assert _snap_last({"Now": "abc"}) == 0.0


def test_live_news_uniform_envelope(client):
    """news 是代理到本机 8000 的 HTTP——无论通不通都必须 200 + success（上游语义）。"""
    body = client.get(f"{BASE}/live/news", params={"hours": 2, "limit": 3}).json()
    assert body["success"] is True
    assert isinstance(body["data"], dict)
    assert "articles" in body["data"] or "error" in body["data"]


# ---------- 族 6：桥与券商读取面（A4 余量） ----------


def test_bridge_broker_alias_map():
    from backend.services.agent_arena import bridge_family as bf

    assert bf.map_broker("qmt") == "qmt_exec"
    assert bf.map_broker("QMT") == "qmt_exec"
    assert bf.map_broker("tiger") == "tiger"
    assert bf.map_broker("futu") == "futu"
    assert bf.map_broker("ib") == "ib"
    # trade 原生名 tdx/qmt_exec 不在上游面上（上游四键 = tiger/futu/ib/qmt）
    assert bf.map_broker("tdx") is None
    assert bf.map_broker("") is None


def test_bridge_qmt_account_reshape(client, monkeypatch):
    from backend.services.agent_arena import bridge_family as bf

    async def _fake():
        return bf.qmt_account_data(
            {
                "account_id": "10000001",
                "cash": 1000.0,
                "frozen_cash": 5.0,
                "market_value": 6000.0,
                "total_asset": 7000.0,
            },
            [
                {
                    "symbol": "SH600036",
                    "stock_code": "600036.SH",
                    "instrument_name": "招商银行",
                    "volume": 100,
                    "can_use_volume": 60,
                    "open_price": 30.0,
                    "avg_price": 28.5,
                    "market_value": 3000.0,
                },
                {  # 清仓残留行（volume<=0）——上游同款过滤掉
                    "stock_code": "000001.SZ",
                    "instrument_name": "平安银行",
                    "volume": 0,
                    "can_use_volume": 0,
                    "avg_price": 10.0,
                    "market_value": 0.0,
                },
                {  # 拿不到市值 → last_price=0（「0=拿不到价」消费方约定）
                    "stock_code": "601318.SH",
                    "instrument_name": "中国平安",
                    "volume": 200,
                    "can_use_volume": 200,
                    "avg_price": -1.5,  # 摊薄成本可为负，原样透传
                    "market_value": 0.0,
                },
            ],
        )

    monkeypatch.setattr(bf, "qmt_account", _fake)
    body = client.get(f"{BASE}/qmt/account").json()
    assert body["success"] is True
    d = body["data"]
    assert d["broker"] == "qmt" and d["account_id"] == "10000001"
    assert d["position_count"] == 2 and len(d["positions"]) == 2
    a = d["asset"]
    # 上游 asset 是 dict（currency/balance 按 TDX 桥口径补齐），不是平铺数字
    assert a["currency"] == "CNY" and a["asset"] == 7000.0 and a["balance"] == 7000.0
    assert a["cash"] == 1000.0 and a["frozen_cash"] == 5.0
    assert a["market_value"] == 6000.0
    p = d["positions"][0]
    assert p["stock_code"] == "600036.SH" and p["stock_name"] == "招商银行"
    assert p["cost_price"] == 28.5 and p["total_volume"] == 100
    assert p["available_volume"] == 60
    assert p["last_price"] == 30.0  # round(3000/100, 4)
    q = d["positions"][1]
    assert q["last_price"] == 0 and q["cost_price"] == -1.5


def test_bridge_qmt_account_failure_is_200_success_false(client, monkeypatch):
    from backend.services.agent_arena import bridge_family as bf

    async def _boom():
        raise RuntimeError("QMT RPC 超时（5.0s）")

    monkeypatch.setattr(bf, "qmt_account", _boom)
    res = client.get(f"{BASE}/qmt/account")
    # 上游语义：桥失败不让接口炸，200 + success:false（前端面板降级为提示）
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is False
    assert body["error"].startswith("QMT 查询失败")


def test_bridge_qmt_status_maps_two_gates(client, monkeypatch):
    from backend.services.agent_arena import bridge_family as bf

    async def _fake():
        return bf.qmt_status_data(
            {"enabled": False, "account_type": "STOCK"},
            {
                "ok": True,
                "result": {
                    "version": "bigqmt-1.2",
                    "account_type": "STOCK",
                    "server_time": "2026-09-30 10:00:00",
                    "rpc_revision": 7,
                },
                "account_id": "10000001",
            },
        )

    monkeypatch.setattr(bf, "qmt_status", _fake)
    body = client.get(f"{BASE}/qmt/status").json()
    assert body["success"] is True
    d = body["data"]
    # 本侧总闸=enabled；Windows 侧闸桥报不出来 → None（「不知道」≠「确认为关」）
    assert d["allow_trading"] is False and d["allow_order_methods"] is None
    assert d["version"] == "bigqmt-1.2" and d["rpc_revision"] == 7
    assert d["account_id"] == "10000001"


def test_bridge_qmt_status_allows_true_gates():
    from backend.services.agent_arena import bridge_family as bf

    d = bf.qmt_status_data(
        {"enabled": True},
        {"ok": True, "result": {"allow_order_methods": True}, "account_id": "1"},
    )
    assert d["allow_trading"] is True and d["allow_order_methods"] is True


def test_bridge_broker_config_alias_rewrites_broker_and_label(client, monkeypatch):
    import httpx

    from backend.services.api.routers.agent_arena import bridge_family as br

    seen = {}

    async def _fake_forward(request, upstream_path, user):
        seen["path"] = upstream_path
        return httpx.Response(
            200,
            json={
                "success": True,
                "broker": "qmt_exec",
                "label": "大 QMT(执行端)",
                "fields": {"account_id": "10000001", "redis_password_configured": True},
            },
        )

    monkeypatch.setattr(br, "_forward_get", _fake_forward)
    body = client.get(f"{BASE}/broker-config/qmt").json()
    assert seen["path"] == "/api/v1/broker-config/qmt_exec"
    assert body["broker"] == "qmt" and body["label"] == "迅投 QMT"
    assert body["fields"]["account_id"] == "10000001"

    # 其余三键名不变（label 直接用 trade 的，与上游逐字段同文案）
    async def _fake_forward_tiger(request, upstream_path, user):
        seen["path"] = upstream_path
        return httpx.Response(
            200,
            json={
                "success": True,
                "broker": "tiger",
                "label": "老虎证券",
                "fields": {},
            },
        )

    monkeypatch.setattr(br, "_forward_get", _fake_forward_tiger)
    body = client.get(f"{BASE}/broker-config/tiger").json()
    assert seen["path"] == "/api/v1/broker-config/tiger"
    assert body["broker"] == "tiger" and body["label"] == "老虎证券"


def test_bridge_broker_config_unknown_is_404(client):
    res = client.get(f"{BASE}/broker-config/tdx")
    assert res.status_code == 404
    assert "未知券商" in res.json()["detail"]


def test_bridge_tdx_endpoints_passthrough(client, monkeypatch):
    import httpx

    from backend.services.api.routers.agent_arena import bridge_family as br

    async def _fake_forward(request, upstream_path, user):
        payload = (
            {"enabled": True, "bridge_url": "http://10.0.0.9:8550"}
            if upstream_path.endswith("/tdx/config")
            else {"available": False, "error": "桥地址或 token 未配置"}
        )
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(br, "_forward_get", _fake_forward)
    cfg = client.get(f"{BASE}/tdx/config").json()
    assert cfg["enabled"] is True and cfg["bridge_url"] == "http://10.0.0.9:8550"
    ov = client.get(f"{BASE}/tdx/overview").json()
    assert ov["available"] is False and ov["error"]


def test_bridge_real_trading_status_passthrough_keeps_upstream_status(
    client, monkeypatch
):
    import httpx

    from backend.services.api.routers.agent_arena import bridge_family as br

    async def _ok(request, upstream_path, user):
        return httpx.Response(
            200,
            json={"status": "not_running", "mode": "SIMULATION", "strategy": None},
        )

    monkeypatch.setattr(br, "_forward_get", _ok)
    body = client.get(f"{BASE}/real-trading/status").json()
    assert body["status"] == "not_running" and body["mode"] == "SIMULATION"

    async def _denied(request, upstream_path, user):
        return httpx.Response(403, json={"detail": "需要管理员权限"})

    monkeypatch.setattr(br, "_forward_get", _denied)
    res = client.get(f"{BASE}/real-trading/status")
    assert res.status_code == 403  # 上游码原样透传，不吞成 200
    assert res.json()["detail"] == "需要管理员权限"


def test_bridge_write_endpoints_forward_method_path_and_body(client, monkeypatch):
    """A5 收口：tdx/config POST、broker-config PUT/test——方法/路径/请求体原样到 trade。"""
    import httpx

    from backend.services.api.routers.agent_arena import bridge_family as br

    seen: list[tuple[str, str, bytes]] = []

    async def _fake(request, upstream_path, user):
        seen.append((request.method, upstream_path, await request.body()))
        return httpx.Response(200, json={"success": True})

    monkeypatch.setattr(br, "_forward_write", _fake)
    r = client.post(
        f"{BASE}/tdx/config",
        json={"bridge_url": "http://10.0.0.9:8550", "bridge_token": "t"},
    )
    assert r.status_code == 200 and r.json()["success"] is True
    client.put(f"{BASE}/broker-config/qmt", json={"values": {"account_id": "10000001"}})
    client.post(f"{BASE}/broker-config/tiger/test", json={})
    assert seen[0][0] == "POST" and seen[0][1] == "/api/v1/tdx/config"
    assert b"bridge_url" in seen[0][2]
    assert seen[1][0] == "PUT" and seen[1][1] == "/api/v1/broker-config/qmt_exec"
    assert b"10000001" in seen[1][2]
    assert seen[2][0] == "POST" and seen[2][1] == "/api/v1/broker-config/tiger/test"

    # 未知券商自产 404，不打 trade（与 GET 同款 map_broker 前置拦截）
    res = client.put(f"{BASE}/broker-config/tdx", json={"values": {}})
    assert res.status_code == 404 and "未知券商" in res.json()["detail"]
    assert len(seen) == 3

    # 上游校验错误（如 token 无效）原样透传
    async def _bad(request, upstream_path, user):
        return httpx.Response(400, json={"detail": "bridge_token 无效"})

    monkeypatch.setattr(br, "_forward_write", _bad)
    res = client.post(f"{BASE}/tdx/config", json={})
    assert res.status_code == 400 and res.json()["detail"] == "bridge_token 无效"


# ---------- 族 9：数据面板（A5，服务层 + 路由层） ----------


@pytest.fixture()
def dp_svc():
    """族 9 服务层：进出都清 catalog 缓存（模块级 30s TTL 会跨用例串数据根）。"""
    from backend.services.agent_arena import data_panel as svc

    for m in svc.MARKET_IDS:
        svc.invalidate_catalog(m)
    yield svc
    for m in svc.MARKET_IDS:
        svc.invalidate_catalog(m)


def _build_dp_root(root: Path) -> None:
    """合成 quantdb 数据根：两日分区 + 平铺日期名 + 季度分区 + symbol 目录 + 未知目录。"""
    _write_parquet(
        root / "1_kline_data/daily_forward/dt=20260901/data.parquet",
        # 2 万行把体积顶过 0.05MB，size_mb 保留一位小数后才不为 0
        "SELECT '000001.SZ' AS symbol, i * 0.01 AS close FROM range(20000) t(i)",
    )
    _write_parquet(
        root / "1_kline_data/daily_forward/dt=20260902/data.parquet",
        "SELECT '000001.SZ' AS symbol, 11.3 AS close",
    )
    _write_parquet(
        root / "1_kline_data/index_daily/20260901.parquet",
        "SELECT '000300.SH' AS symbol, 4100.0 AS close",
    )
    _write_parquet(
        root / "1_kline_data/min5_kline/SH600036.parquet",
        "SELECT '2026-09-30 14:55:00' AS time, 30.0 AS close",
    )
    _write_parquet(
        root / "2_base_sector/margin_trading/quarter=2026Q1/data.parquet",
        "SELECT '000001.SZ' AS symbol, 1.0 AS rzye",
    )
    _write_parquet(root / "9_custom/stuff.parquet", "SELECT 1 AS x")


def test_dp_service_catalog_stats_dates_and_groups(dp_svc, tmp_path, monkeypatch):
    root = tmp_path / "dp_root"
    _build_dp_root(root)
    monkeypatch.setattr(dp_svc, "_native_root", lambda market: root)
    monkeypatch.setattr(dp_svc, "_load_state", lambda: {})

    cat = dp_svc.build_catalog("quantdb")
    assert cat["data_dir"] == str(root) and cat["exists"] is True
    assert cat["market"] == "quantdb" and cat["timestamp"].endswith("Z")
    by = {d["dataset"]: d for d in cat["datasets"]}
    f = by["daily_forward"]
    assert f["synced"] is True and f["files"] == 2 and f["size_mb"] > 0
    assert (f["start_date"], f["end_date"], f["partitions"]) == (
        "20260901",
        "20260902",
        2,
    )
    assert f["layout"] == "partition" and f["rel_dir"] == "1_kline_data/daily_forward"
    assert "updated_at" in f
    # 平铺文件名日期也计入（index_daily 是单文件 20260901.parquet）
    assert (
        by["index_daily"]["start_date"] == "20260901"
        and by["index_daily"]["partitions"] == 1
    )
    # quarter= 分区计入（margin_trading 是 partition 布局的季度分区）
    assert (
        by["margin_trading"]["start_date"] == "2026Q1"
        and by["margin_trading"]["partitions"] == 1
    )
    # 未落盘数据集：synced False、无日期键
    assert by["income"]["synced"] is False and by["income"]["files"] == 0
    assert "start_date" not in by["income"]
    # 组按「规格成员」出现（上游语义），不按落盘：bond_etf 三个数据集全没落盘，组仍在
    groups = {g["id"]: g for g in cat["groups"]}
    assert set(groups) == {
        "kline",
        "base_sector",
        "financial",
        "bond_etf",
        "technical",
        "ml",
    }
    assert (
        groups["bond_etf"]["dataset_count"] == 3
        and groups["bond_etf"]["synced_count"] == 0
    )
    assert (
        groups["kline"]["dataset_count"] == 7 and groups["kline"]["synced_count"] == 3
    )
    assert groups["kline"]["files"] == 4  # 2 daily + 1 index + 1 min5
    assert groups["base_sector"]["files"] == 1
    assert groups["ml"]["synced_count"] == 0 and groups["ml"]["dataset_count"] == 5


def test_dp_service_scan_folder_unknown_and_missing_root(dp_svc, tmp_path, monkeypatch):
    root = tmp_path / "dp_scan"
    _build_dp_root(root)

    scan = dp_svc.scan_folder("quantdb", str(root))
    assert scan["exists"] is True and scan["root"] == str(root)
    by = {d["dataset"]: d for d in scan["datasets"]}
    assert by["daily_forward"]["files"] == 2 and by["daily_forward"]["bytes"] > 0
    assert "name" in by["daily_forward"] and "layout" in by["daily_forward"]
    assert len(scan["unknown"]) == 1 and scan["unknown"][0]["rel_dir"] == "9_custom"
    assert scan["unknown"][0]["files"] == 1 and scan["unknown"][0]["bytes"] > 0
    assert scan["total_files"] == 6  # 5 规格内 + 1 未知
    assert (
        scan["total_bytes"]
        == sum(d["bytes"] for d in scan["datasets"]) + scan["unknown"][0]["bytes"]
    )

    miss = dp_svc.scan_folder("quantdb", str(tmp_path / "nope"))
    assert (
        miss["exists"] is False and miss["datasets"] == [] and miss["total_files"] == 0
    )

    # 空 root → 回落当前数据根
    monkeypatch.setattr(dp_svc, "_load_state", lambda: {"quantdb": str(root)})
    hit = dp_svc.scan_folder("quantdb", "")
    assert hit["root"] == str(root) and hit["exists"] is True


def test_dp_service_preview_symbol_pick_and_lookup_errors(
    dp_svc, tmp_path, monkeypatch
):
    root = tmp_path / "dp_prev"
    _build_dp_root(root)
    monkeypatch.setattr(dp_svc, "_load_state", lambda: {"quantdb": str(root)})

    pv = dp_svc.preview_dataset("quantdb", "min5_kline", None, 10)
    assert pv["source"] == "local" and pv["rows_total"] == 1
    assert pv["file"] == "1_kline_data/min5_kline/SH600036.parquet"
    assert pv["symbol_total"] == 1 and pv["symbol_choices"] == ["SH600036"]
    assert pv["column_count"] == 2
    assert any(c["name"] == "close" for c in pv["columns"])

    # symbol 精确匹配（大小写不敏感）
    pv2 = dp_svc.preview_dataset("quantdb", "min5_kline", "sh600036", 10)
    assert pv2["file"] == pv["file"]

    # 分区布局取最新日期目录
    pv3 = dp_svc.preview_dataset("quantdb", "daily_forward", None, 10)
    assert pv3["file"] == "1_kline_data/daily_forward/dt=20260902/data.parquet"
    assert "symbol_choices" not in pv3  # 非 symbol 布局不给候选列

    with pytest.raises(LookupError):
        dp_svc.preview_dataset("quantdb", "nope", None, 10)
    with pytest.raises(LookupError):
        dp_svc.preview_dataset("quantdb", "min5_kline", "SH000001", 10)
    with pytest.raises(LookupError):
        dp_svc.preview_dataset("quantdb", "income", None, 10)  # 本地无数据


def test_dp_service_json_safe_edge_values():
    import numpy as np
    import pandas as pd

    from backend.services.agent_arena.data_panel import _json_safe

    assert _json_safe(float("nan")) is None
    assert _json_safe(float("inf")) is None and _json_safe(float("-inf")) is None
    assert _json_safe(np.float64(1.5)) == 1.5
    assert _json_safe(np.int64(3)) == 3 and isinstance(_json_safe(np.int64(3)), int)
    assert _json_safe(np.bool_(True)) is True
    assert _json_safe(pd.NaT) is None
    assert _json_safe(pd.Timestamp("2026-09-30")) == "2026-09-30T00:00:00"
    assert _json_safe(b"\xe4\xb8\xad") == "中"
    assert _json_safe(np.array([1, 2])) == [1, 2]
    assert _json_safe({"a": float("nan")}) == {"a": None}
    assert _json_safe(None) is None and _json_safe(True) is True


def test_dp_service_state_roundtrip_and_catalog_cache(dp_svc, tmp_path, monkeypatch):
    state_file = tmp_path / "dp_state.json"
    monkeypatch.setattr(dp_svc, "_state_file", lambda: state_file)
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    _write_parquet(
        root_a / "1_kline_data/daily_forward/dt=20260901/data.parquet", "SELECT 1 AS x"
    )
    _write_parquet(
        root_b / "1_kline_data/daily_forward/dt=20260901/data.parquet", "SELECT 1 AS x"
    )
    monkeypatch.setattr(dp_svc, "_native_root", lambda market: root_a)

    assert dp_svc.data_root("quantdb") == root_a
    dp_svc.set_data_root("quantdb", str(root_b))
    assert dp_svc.data_root("quantdb") == root_b
    assert json.loads(state_file.read_text(encoding="utf-8"))["quantdb"] == str(root_b)

    # TTL 缓存：同根第二次调用返回同一份对象；改盘面不重扫
    cat1 = dp_svc.build_catalog("quantdb")
    _write_parquet(
        root_b / "1_kline_data/daily_forward/dt=20260902/data.parquet", "SELECT 1 AS x"
    )
    cat2 = dp_svc.build_catalog("quantdb")
    assert cat2 is cat1 and cat2["data_dir"] == str(root_b)
    # 失效后重扫（set_data_root 已带失效——这里是手动失效路径）
    dp_svc.invalidate_catalog("quantdb")
    cat3 = dp_svc.build_catalog("quantdb")
    files = {d["dataset"]: d for d in cat3["datasets"]}["daily_forward"]["files"]
    assert files == 2


def test_dp_router_markets_envelope(client):
    body = client.get(f"{BASE}/data-platform/markets").json()
    assert body["success"] is True
    assert [m["id"] for m in body["data"]] == [
        "quantdb",
        "quanthk",
        "quantus",
        "quantfutures",
    ]
    q = body["data"][0]
    assert q["label"] == "A股市场" and q["code"] == "QuantDB" and q["flag"] == "🇨🇳"
    assert q["beta"] is False and q["default_root"] == "/data/quantdb"
    assert [m["beta"] for m in body["data"][1:]] == [True, True, True]


def test_dp_router_unknown_market_is_400(client):
    for path in ("catalog", "root", "scan"):
        res = client.get(f"{BASE}/data-platform/quantbc/{path}")
        assert res.status_code == 400, path
        assert "未知市场" in res.json()["detail"]
    res = client.get(f"{BASE}/data-platform/quantbc/preview", params={"dataset": "x"})
    assert res.status_code == 400


def test_dp_router_preview_error_mapping_and_limit_bounds(client, monkeypatch):
    from backend.services.agent_arena import data_panel as svc

    def _raise_lookup(*a, **k):
        raise LookupError("未知数据集: nope")

    monkeypatch.setattr(svc, "preview_dataset", _raise_lookup)
    res = client.get(
        f"{BASE}/data-platform/quantdb/preview", params={"dataset": "nope"}
    )
    assert res.status_code == 404 and "未知数据集" in res.json()["detail"]

    def _raise_boom(*a, **k):
        raise RuntimeError("pyarrow 炸了")

    monkeypatch.setattr(svc, "preview_dataset", _raise_boom)
    res = client.get(f"{BASE}/data-platform/quantdb/preview", params={"dataset": "d"})
    assert res.status_code == 500
    assert res.json()["success"] is False and res.json()["error"].startswith("预览失败")

    # limit 边界与上游 Query(ge=1, le=200) 逐位一致；缺 dataset → 422
    monkeypatch.setattr(svc, "preview_dataset", lambda *a, **k: {"ok": 1})
    assert (
        client.get(
            f"{BASE}/data-platform/quantdb/preview", params={"dataset": "d", "limit": 0}
        ).status_code
        == 422
    )
    assert (
        client.get(
            f"{BASE}/data-platform/quantdb/preview",
            params={"dataset": "d", "limit": 999},
        ).status_code
        == 422
    )
    assert client.get(f"{BASE}/data-platform/quantdb/preview").status_code == 422
    assert (
        client.get(
            f"{BASE}/data-platform/quantdb/preview",
            params={"dataset": "d", "limit": 200},
        ).status_code
        == 200
    )


def test_dp_router_root_set_and_scan_envelope(client, monkeypatch, tmp_path):
    from backend.services.agent_arena import data_panel as svc

    def _boom(*a, **k):  # 不应被触达（校验先于落库）
        raise AssertionError("校验失败路径不应调 set_data_root")

    monkeypatch.setattr(svc, "set_data_root", _boom)
    res = client.post(
        f"{BASE}/data-platform/quantdb/root", json={"root": str(tmp_path / "nope")}
    )
    assert res.status_code == 404 and "目录不存在" in res.json()["detail"]
    assert (
        client.post(
            f"{BASE}/data-platform/quantdb/root", json={"root": "  "}
        ).status_code
        == 400
    )

    calls = {}

    def _set(market, root):
        calls.update(market=market, root=root)
        return root

    monkeypatch.setattr(svc, "set_data_root", _set)
    target = tmp_path / "okroot"
    target.mkdir()
    res = client.post(f"{BASE}/data-platform/quantdb/root", json={"root": str(target)})
    assert res.status_code == 200 and res.json()["data"] == {
        "market": "quantdb",
        "root": str(target),
    }
    assert calls == {"market": "quantdb", "root": str(target)}

    monkeypatch.setattr(svc, "data_root", lambda m: target)
    body = client.get(f"{BASE}/data-platform/quanthk/root").json()
    assert body["success"] is True and body["data"] == {
        "market": "quanthk",
        "root": str(target),
    }

    seen = {}

    def _scan(market, root_str):
        seen.update(market=market, root=root_str)
        return {"root": root_str, "exists": True, "datasets": [], "unknown": []}

    monkeypatch.setattr(svc, "scan_folder", _scan)
    body = client.get(
        f"{BASE}/data-platform/quantdb/scan", params={"root": "/x"}
    ).json()
    assert body["success"] is True and seen == {"market": "quantdb", "root": "/x"}


# ---------- 族 10：/quantmind/* 受限透传（A5） ----------


def test_qm_family_tdx_forward_and_status_passthrough(client, monkeypatch):
    import httpx

    from backend.services.api.routers.agent_arena import quantmind_family as qf

    seen: list[tuple[str, str]] = []

    async def _fake(request, upstream_path, user):
        seen.append((request.method, upstream_path))
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(qf, "_forward_trade", _fake)
    assert client.get(f"{BASE}/quantmind/tdx/rolling-config").json()["ok"] is True
    assert (
        client.put(
            f"{BASE}/quantmind/tdx/rolling-config", json={"score_threshold": 80}
        ).status_code
        == 200
    )
    assert client.get(f"{BASE}/quantmind/tdx/sltp-config").status_code == 200
    assert (
        client.put(
            f"{BASE}/quantmind/tdx/sltp-config", json={"enabled": True}
        ).status_code
        == 200
    )
    assert client.post(f"{BASE}/quantmind/tdx/push-signals", json={}).status_code == 200
    assert (
        client.post(
            f"{BASE}/quantmind/tdx/rolling-signals", json={"trade_date": "20260930"}
        ).status_code
        == 200
    )
    assert seen == [
        ("GET", "/api/v1/tdx/rolling-config"),
        ("PUT", "/api/v1/tdx/rolling-config"),
        ("GET", "/api/v1/tdx/sltp-config"),
        ("PUT", "/api/v1/tdx/sltp-config"),
        ("POST", "/api/v1/tdx/push-signals"),
        ("POST", "/api/v1/tdx/rolling-signals"),
    ]

    # trade 的 400 校验（run_id 与 trade_date 互斥）原样透传，不吞不包
    async def _bad(request, upstream_path, user):
        return httpx.Response(400, json={"detail": "run_id 与 trade_date 互斥"})

    monkeypatch.setattr(qf, "_forward_trade", _bad)
    res = client.post(f"{BASE}/quantmind/tdx/rolling-signals", json={})
    assert res.status_code == 400 and "互斥" in res.json()["detail"]


def test_qm_family_admin_loopback_market_guard(client, monkeypatch):
    import httpx

    from backend.services.api.routers.agent_arena import quantmind_family as qf

    seen: list[tuple[str, str]] = []

    async def _fake(request, upstream_path, user):
        seen.append((request.method, upstream_path))
        return httpx.Response(200, json={"success": True, "data": {"ok": 1}})

    monkeypatch.setattr(qf, "_forward_self", _fake)
    assert client.get(f"{BASE}/quantmind/admin/data-platform/quantdb/info").json()[
        "data"
    ] == {"ok": 1}
    assert (
        client.post(
            f"{BASE}/quantmind/admin/data-platform/quanthk/sync-datasets",
            json={"datasets": ["daily_forward"]},
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"{BASE}/quantmind/admin/data-platform/quantfutures/sync-jobs"
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"{BASE}/quantmind/admin/data-platform/quantus/sync-jobs/j1/cancel"
        ).status_code
        == 200
    )
    assert [p for _, p in seen] == [
        "/api/v1/admin/data-platform/quantdb/info",
        "/api/v1/admin/data-platform/quanthk/sync-datasets",
        "/api/v1/admin/data-platform/quantfutures/sync-jobs",
        "/api/v1/admin/data-platform/quantus/sync-jobs/j1/cancel",
    ]

    # 路径白名单：市场不在列、前缀不在列 → 404，且不打任何转发
    assert (
        client.get(f"{BASE}/quantmind/admin/data-platform/banana/info").status_code
        == 404
    )
    assert client.get(f"{BASE}/quantmind/system/info").status_code == 404
    assert (
        client.get(f"{BASE}/quantmind/admin/data-platform/quantdb").status_code == 404
    )
    assert len(seen) == 4

    # 回环走调用者本人 Bearer：admin 面 403 原样透传（与上游注入 admin 不同——更小权限）
    async def _denied(request, upstream_path, user):
        return httpx.Response(403, json={"detail": "需要管理员权限"})

    monkeypatch.setattr(qf, "_forward_self", _denied)
    assert (
        client.get(f"{BASE}/quantmind/admin/data-platform/quantdb/info").status_code
        == 403
    )


# ---------- 族 11：分析触发（立即分析台账，2026-10-08） ----------


def test_analysis_trigger_writes_pending_job(client, arena_root):
    """live 触发 → 200 + pending 任务行落账（容器数据根 logs/analysis_jobs.jsonl）。"""
    resp = client.post(
        f"{BASE}/analysis/trigger", json={"type": "live", "agents": ["a", "b"]}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    job = body["data"]
    assert job["status"] == "pending" and job["type"] == "live"
    assert job["agents"] == ["a", "b"] and job["id"].startswith("job-")

    ledger_file = arena_root / "logs" / "analysis_jobs.jsonl"
    rows = [json.loads(line) for line in ledger_file.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["id"] == job["id"]


def test_analysis_trigger_dedups_pending(client):
    """同 type+agents 的 pending 直接返回旧任务（连点不堆积）；换 agents/换 type 是新任务。"""
    first = client.post(f"{BASE}/analysis/trigger", json={"type": "live", "agents": "all"})
    second = client.post(f"{BASE}/analysis/trigger", json={"type": "live", "agents": "all"})
    assert first.json()["data"]["id"] == second.json()["data"]["id"]

    other = client.post(
        f"{BASE}/analysis/trigger", json={"type": "live", "agents": ["a"]}
    )
    assert other.json()["data"]["id"] != first.json()["data"]["id"]
    news = client.post(f"{BASE}/analysis/trigger", json={"type": "news"})
    assert news.json()["data"]["id"] != first.json()["data"]["id"]
    assert news.json()["data"]["agents"] == "all"


def test_analysis_trigger_validation(client):
    """坏输入在路由层拦下：非法 type / 空 agents / 超长项 → 400。"""
    assert client.post(f"{BASE}/analysis/trigger", json={"type": "banana"}).status_code == 400
    assert (
        client.post(f"{BASE}/analysis/trigger", json={"type": "live", "agents": []}).status_code
        == 400
    )
    assert (
        client.post(f"{BASE}/analysis/trigger", json={"type": "live", "agents": ["x" * 65]}).status_code
        == 400
    )
    assert (
        client.post(
            f"{BASE}/analysis/trigger",
            json={"type": "live", "agents": [f"m{i}" for i in range(11)]},
        ).status_code
        == 400
    )


def test_analysis_jobs_newest_first_and_limit(client):
    """台账列表：最新在前、limit 生效。"""
    for name in ("a", "b", "c"):
        client.post(f"{BASE}/analysis/trigger", json={"type": "live", "agents": [name]})
    rows = client.get(f"{BASE}/analysis/jobs", params={"limit": 2}).json()["data"]
    assert [r["agents"] for r in rows] == [["c"], ["b"]]


def test_analysis_ledger_tolerant_load_and_update(client, arena_root):
    """坏行只丢那行（旧平台 09-12 教训）；update_job 不覆盖并发追加的新行。"""
    from backend.services.agent_arena import analysis_ledger as ledger

    client.post(f"{BASE}/analysis/trigger", json={"type": "live", "agents": ["a"]})
    ledger_file = arena_root / "logs" / "analysis_jobs.jsonl"
    with ledger_file.open("a", encoding="utf-8") as f:
        f.write("{坏行\n")

    rows = ledger.load_jobs()  # 坏行跳过、好行照读
    assert len(rows) == 1
    assert ledger.update_job(rows[0]["id"], status="running") is True

    # 并发追加（模拟 api 在 worker 处理间隙写入）→ update 后必须还在
    with ledger_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"id": "job-x", "status": "pending"}, ensure_ascii=False) + "\n")
    assert ledger.update_job(rows[0]["id"], status="done", done_ts="2099-01-01T00:00:00+08:00")
    ids = {r["id"] for r in ledger.load_jobs()}
    assert ids == {rows[0]["id"], "job-x"}
    assert [r for r in ledger.load_jobs() if r["id"] == "job-x"][0]["status"] == "pending"


def test_analysis_ledger_cleanup_keeps_pending(client, arena_root):
    """3 天前终态清掉；pending 与近期终态保留。"""
    from backend.services.agent_arena import analysis_ledger as ledger

    client.post(f"{BASE}/analysis/trigger", json={"type": "live", "agents": ["keep"]})
    ledger_file = arena_root / "logs" / "analysis_jobs.jsonl"
    with ledger_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"id": "old", "status": "done", "done_ts": "2020-01-01T00:00:00+08:00"}, ensure_ascii=False) + "\n")
        f.write(json.dumps({"id": "new", "status": "done", "done_ts": "2099-01-01T00:00:00+08:00"}, ensure_ascii=False) + "\n")
    assert ledger.cleanup_old() == 1
    ids = {r["id"] for r in ledger.load_jobs()}
    assert "old" not in ids and "new" in ids and len(ids) == 2
