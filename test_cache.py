import asyncio
import os
import sys

# Ensure backend module is in path
sys.path.insert(0, os.path.abspath('.'))

from backend.shared.market_db_manager import get_market_session
from sqlalchemy import text
from backend.shared.market_data.stock_daily_latest_cache import stock_latest_cache

async def main():
    print("1. Testing external PostgreSQL connection (106.53.100.144)...")
    try:
        async with get_market_session() as session:
            result = await session.execute(text("SELECT COUNT(*) FROM stock_daily_latest"))
            count = result.scalar()
            print(f"SUCCESS: Connected to remote PG! Total rows in stock_daily_latest: {count}")
    except Exception as e:
        print(f"FAILED to connect to remote PG: {e}")
        return

    print("\n2. Testing StockDailyLatestCache (batch_get_latest)...")
    try:
        symbols_to_test = ["SH600519", "SZ000858"]
        print(f"Fetching latest data for: {symbols_to_test}")
        data = await stock_latest_cache.batch_get_latest(symbols_to_test)
        
        for sym, record in data.items():
            print(f"  {sym}: close={record.get('close')}, trade_date={record.get('trade_date')}")
            
        print("SUCCESS: Cache fetch and DB compensation worked correctly!")
    except Exception as e:
        print(f"FAILED to fetch from cache/DB: {e}")

if __name__ == "__main__":
    asyncio.run(main())
