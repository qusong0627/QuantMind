"""标的 → 市场推断单测（首页仪表盘按市场取数的唯一口径）。"""

from sqlalchemy import Column, MetaData, String, Table, select
from sqlalchemy.dialects import postgresql

from backend.shared.symbol_market import market_of_symbol, symbol_market_clause


def test_market_of_symbol_all_markets():
    assert market_of_symbol("600036.SH") == "CN"
    assert market_of_symbol("000001.SZ") == "CN"
    assert market_of_symbol("830799.BJ") == "CN"
    assert market_of_symbol("600519") == "CN"  # 历史裸码
    assert market_of_symbol("00700.HK") == "HK"
    assert market_of_symbol("AAPL") == "US"
    assert market_of_symbol("BRK.B") == "US"
    assert market_of_symbol("RB0.CN") == "FUTURES"
    assert market_of_symbol("BTCUSDT") == "CRYPTO"
    # 空值/未知按 CN（与模拟引擎一致）
    assert market_of_symbol("") == "CN"


def _compiled(market: str) -> str:
    metadata = MetaData()
    table = Table("t", metadata, Column("symbol", String(20)))
    stmt = select(table.c.symbol).where(symbol_market_clause(table.c.symbol, market))
    return str(
        stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def test_symbol_market_clause_markets():
    assert ".SH" in _compiled("CN")
    assert ".HK" in _compiled("HK")
    assert ".CN" in _compiled("FUTURES")
    assert "USDT" in _compiled("CRYPTO")
    assert "NOT (" in _compiled("US")
    # CUSTOM 没有公开成交，直接 false，避免把其它市场算进来
    assert "false" in _compiled("CUSTOM")
