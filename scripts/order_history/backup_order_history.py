"""历史委托备份脚本：把现有 orders（含通达信桥委托）备份到 order_history 归档表。

用途：
1. 一次性迁移：把 orders 表已有的 REAL 委托（备注=通达信桥委托）备份进 order_history，
   避免桥当日委托列表滚出后历史丢失。
2. 可重复执行：按 (broker_type, exchange_order_id, trade_date) 唯一键幂等，
   ON CONFLICT DO NOTHING 自动跳过已归档记录。
3. 多交易所推断：按 symbol 后缀自动判定 market/exchange（A股/港股/美股/期货/加密）。

运行方式（quantmind 容器内）：
    docker exec -i -w /app quantmind python3 - < scripts/backup_order_history.py
    # 或直接执行（带 docker cp）：
    docker cp backup_order_history.py quantmind:/tmp/ && docker exec -w /app quantmind python3 /tmp/backup_order_history.py

参数（环境变量，可选）：
    OH_DRY_RUN=1        只统计不写入
    OH_LIMIT=100        最多备份 N 条（默认全部）
    OH_FROM=orders      数据源表，默认 orders
"""

import asyncio
import json
import os
import sys
from datetime import datetime

from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session

DRY_RUN = os.getenv("OH_DRY_RUN") == "1"
LIMIT = int(os.getenv("OH_LIMIT", "0") or "0")
FROM_TABLE = os.getenv("OH_FROM", "orders")


def infer_market_exchange(symbol: str) -> tuple[str, str, str]:
    """按 symbol 后缀推断 (market, exchange, currency)。"""
    s = (symbol or "").strip().upper()
    if s.endswith(".SH"):
        return "CN", "SSE", "CNY"
    if s.endswith(".SZ"):
        return "CN", "SZSE", "CNY"
    if s.endswith(".BJ"):
        return "CN", "BSE", "CNY"
    if s.endswith(".HK"):
        return "HK", "HKEX", "HKD"
    if s.endswith(".CN") or ".FUT" in s or s.startswith(("RB", "CL", "AU")):
        return "FUTURES", "SHFE", "CNY"
    if s.endswith(("USDT", "BTC", "ETH")) or s in ("BTCUSDT", "ETHUSDT"):
        return "CRYPTO", "CRYPTO", "USDT"
    # 纯字母代码默认美股；纯数字默认 A股（000001 → SZSE）
    if s.isalpha():
        return "US", "NASDAQ", "USD"
    if s.isdigit() and s.startswith(("6", "9")):
        return "CN", "SSE", "CNY"
    return "CN", "SZSE", "CNY"


def infer_broker_type(remarks: str, symbol: str) -> str:
    r = (remarks or "").lower()
    if "通达信" in r or "tdx" in r:
        return "tdx"
    if "futu" in r or "富途" in r:
        return "futu"
    if "tiger" in r or "老虎" in r:
        return "tiger"
    if "ib" in r or "盈透" in r:
        return "ib"
    if "qmt" in r or "迅投" in r:
        return "qmt"
    return "manual"


async def main():
    async with get_session() as db:
        # 读取源表（orders 快照）
        limit_sql = f" LIMIT {LIMIT}" if LIMIT > 0 else ""
        result = await db.execute(
            text(f"""
                SELECT order_id, tenant_id, user_id, portfolio_id, strategy_id,
                       symbol, symbol_name, side, order_type, status,
                       quantity, filled_quantity, price, average_price, stop_price,
                       order_value, filled_value, commission,
                       submitted_at, filled_at, cancelled_at, expired_at,
                       client_order_id, exchange_order_id, remarks, created_at
                FROM {FROM_TABLE}
                WHERE status IN ('filled', 'partially_filled', 'cancelled', 'rejected', 'expired')
                ORDER BY created_at
                {limit_sql}
            """)
        )
        cols = list(result.keys()) if hasattr(result, "keys") else None
        rows = result.fetchall()

        total = len(rows)
        print(f"[备份] 源表 {FROM_TABLE}: 找到 {total} 条终态委托"
              + (" (DRY-RUN 只统计)" if DRY_RUN else ""))

        if DRY_RUN or total == 0:
            return

        inserted = skipped = 0
        for row in rows:
            d = dict(zip([c for c in (cols or [])], row))
            symbol = str(d.get("symbol") or "")
            market, exchange, currency = infer_market_exchange(symbol)
            broker = infer_broker_type(d.get("remarks"), symbol)
            trade_date = d.get("submitted_at") or d.get("created_at") or datetime.utcnow()
            order_type = str(d.get("order_type") or "market").lower()
            # status 归一化：PG orderstatus 是 filled 等小写；trades 表侧保持一致
            status = str(d.get("status") or "submitted").lower()
            # 费用估算：沿用 tdx_push_service 的估算（佣金+印花税+过户费）
            filled_value = float(d.get("filled_value") or 0)
            commission = float(d.get("commission") or 0)
            stamp_duty = 0.0
            transfer_fee = 0.0
            if filled_value > 0 and commission <= 0:
                try:
                    from backend.services.trade.services.tdx_push_service import estimate_order_fee
                    fee = estimate_order_fee(filled_value, str(d.get("side") or "buy"))
                    commission = float(fee)
                except Exception:
                    pass
            if str(d.get("side") or "").lower() == "sell" and filled_value > 0 and market == "CN":
                stamp_duty = round(filled_value * 0.0005, 2)
                transfer_fee = round(filled_value * 0.00001, 2)

            payload = {
                "order_id": str(d.get("order_id")),
                "symbol": symbol,
                "side": d.get("side"),
                "order_type": order_type,
                "status": status,
                "quantity": d.get("quantity"),
                "filled_quantity": d.get("filled_quantity"),
                "price": d.get("price"),
                "average_price": d.get("average_price"),
                "order_value": d.get("order_value"),
                "filled_value": filled_value,
                "commission": commission,
                "submitted_at": str(d.get("submitted_at")),
                "filled_at": str(d.get("filled_at")),
                "cancelled_at": str(d.get("cancelled_at")),
                "expired_at": str(d.get("expired_at")),
                "client_order_id": d.get("client_order_id"),
                "exchange_order_id": d.get("exchange_order_id"),
                "remarks": d.get("remarks"),
                "created_at": str(d.get("created_at")),
            }

            try:
                result = await db.execute(
                    text("""
                        INSERT INTO order_history (
                            history_id, tenant_id, user_id, account_id, portfolio_id, strategy_id,
                            market, exchange, currency, broker_type,
                            symbol, symbol_name, side, order_type, status,
                            quantity, filled_quantity, price, average_price, stop_price,
                            order_value, filled_value, commission, stamp_duty, transfer_fee, total_fee,
                            trade_date, submitted_at, filled_at, cancelled_at, expired_at,
                            client_order_id, exchange_order_id, source, remarks, raw_payload
                        ) VALUES (
                            gen_random_uuid(), :tenant_id, :user_id, NULL, :portfolio_id, :strategy_id,
                            :market, :exchange, :currency, :broker_type,
                            :symbol, :symbol_name, :side, :order_type, :status,
                            :quantity, :filled_quantity, :price, :average_price, :stop_price,
                            :order_value, :filled_value, :commission, :stamp_duty, :transfer_fee, :total_fee,
                            :trade_date, :submitted_at, :filled_at, :cancelled_at, :expired_at,
                            :client_order_id, :exchange_order_id, 'backfill', :remarks, :raw_payload
                        )
                        ON CONFLICT (broker_type, exchange_order_id, trade_date) DO NOTHING
                    """),
                    {
                        "tenant_id": str(d.get("tenant_id") or "default"),
                        "user_id": str(d.get("user_id") or "0"),
                        "portfolio_id": int(d.get("portfolio_id") or 0),
                        "strategy_id": d.get("strategy_id"),
                        "market": market,
                        "exchange": exchange,
                        "currency": currency,
                        "broker_type": broker,
                        "symbol": symbol,
                        "symbol_name": d.get("symbol_name"),
                        "side": str(d.get("side") or "buy").lower(),
                        "order_type": order_type,
                        "status": status,
                        "quantity": float(d.get("quantity") or 0),
                        "filled_quantity": float(d.get("filled_quantity") or 0),
                        "price": d.get("price"),
                        "average_price": d.get("average_price"),
                        "stop_price": d.get("stop_price"),
                        "order_value": float(d.get("order_value") or 0),
                        "filled_value": filled_value,
                        "commission": commission,
                        "stamp_duty": stamp_duty,
                        "transfer_fee": transfer_fee,
                        "total_fee": round(commission + stamp_duty + transfer_fee, 2),
                        "trade_date": trade_date,
                        "submitted_at": d.get("submitted_at"),
                        "filled_at": d.get("filled_at"),
                        "cancelled_at": d.get("cancelled_at"),
                        "expired_at": d.get("expired_at"),
                        "client_order_id": d.get("client_order_id"),
                        "exchange_order_id": d.get("exchange_order_id"),
                        "remarks": d.get("remarks"),
                        "raw_payload": json.dumps(payload, ensure_ascii=False, default=str),
                    },
                )
                if result.rowcount and result.rowcount > 0:
                    inserted += 1
                else:
                    skipped += 1
            except Exception as exc:
                print(f"  [ERR] {symbol} {d.get('side')} {status}: {exc}")

        await db.commit()
        print(f"[备份完成] 新增 {inserted} 条, 跳过(已存在) {skipped} 条")


if __name__ == "__main__":
    asyncio.run(main())
