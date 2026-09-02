#!/usr/bin/env python3
"""FutuBroker 子进程执行器。

futu SDK 的连接/等待模型与 asyncio 事件循环混用会死锁，故由
overseas_brokers.FutuBroker 以独立子进程方式调用本脚本。

用法:
  python futu_subprocess.py <host> <port> <rsa_key_path> <op> <payload>

op:
  account — 查询账户（资产/持仓）
  place   — 下单（payload.order: code/price/quantity/order_type/trd_side/is_hk）
  cancel  — 撤单（payload.order_id）
"""
import json
import sys
import warnings

warnings.filterwarnings("ignore")


def _f(v, default: float = 0.0) -> float:
    """Futu DataFrame 数值列可能返回 'N/A' 字符串（如 realized_pl），安全转 float。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _query_account_dict(ctx, trd_env) -> dict:
    """accinfo_query + position_list_query → {total_asset, cash, market_value, positions}。"""
    out: dict = {}
    ret, data = ctx.accinfo_query(trd_env=trd_env)
    if ret == 0 and len(data):
        row = data.iloc[0]
        out = {
            "total_asset": _f(row.get("total_assets")),
            "cash": _f(row.get("cash")),
            "market_value": _f(row.get("market_val")),
        }
    ret2, plist = ctx.position_list_query(trd_env=trd_env)
    positions = {}
    if ret2 == 0 and len(plist):
        for _, p in plist.iterrows():
            qty = _f(p.get("qty"))
            # Futu 对同一代码返回多行：当前持仓(qty>0) + 已平仓行(qty=0, realized_pl)。
            # 跳过 qty<=0 的已平仓行，否则后入的 0 行会覆盖当前持仓。
            if qty <= 0:
                continue
            code = str(p.get("code", ""))
            mkt_val = _f(p.get("market_val"))
            cost = _f(p.get("cost_price"))
            # nominal_price 是实时价；current_price 列不存在（旧代码读错列恒为 0）
            last_price = _f(p.get("nominal_price"))
            if not last_price and qty and mkt_val:
                last_price = mkt_val / qty
            # 同一代码多行（拆仓/多笔）聚合：累加数量与市值、加权成本
            if code in positions:
                prev = positions[code]
                new_qty = prev["volume"] + qty
                prev["market_value"] = prev["market_value"] + mkt_val
                prev["available_volume"] = prev["available_volume"] + _f(p.get("can_sell_qty"))
                if new_qty:
                    prev["cost"] = (prev["cost"] * (new_qty - qty) + cost * qty) / new_qty
                    prev["price"] = prev["market_value"] / new_qty
                prev["volume"] = new_qty
            else:
                positions[code] = {
                    "volume": qty,
                    "available_volume": _f(p.get("can_sell_qty")),
                    "price": last_price,
                    "market_value": mkt_val,
                    "cost": cost,
                    "name": str(p.get("stock_name") or ""),
                    "currency": str(p.get("currency") or "HKD"),
                }
    out["positions"] = positions
    return out


def main() -> int:
    host, port, rsa_key, op, payload, output_path = (
        sys.argv[1],
        int(sys.argv[2]),
        sys.argv[3],
        sys.argv[4],
        json.loads(sys.argv[5]),
        sys.argv[6],
    )

    from futu.common.sys_config import SysConfig

    SysConfig.set_init_rsa_file(rsa_key)

    from futu import (
        ModifyOrderOp,
        OrderType,
        OpenSecTradeContext,
        TrdEnv,
        TrdMarket,
        TrdSide,
    )

    ctx = OpenSecTradeContext(
        filter_trdmarket=TrdMarket.HK,
        host=host,
        port=port,
        security_firm="FUTUSECURITIES",
        is_encrypt=True,
    )
    try:
        env = TrdEnv.REAL if payload.get("env") == "REAL" else TrdEnv.SIMULATE
        out: dict = {}

        if op == "account":
            out = _query_account_dict(ctx, env)

        elif op == "place":
            order = payload["order"]
            order_type = {
                "MARKET": OrderType.MARKET,
                "NORMAL": OrderType.NORMAL,
            }.get(order["order_type"], OrderType.NORMAL)
            trd_side = {
                "BUY": TrdSide.BUY,
                "SELL": TrdSide.SELL,
            }.get(order["trd_side"], TrdSide.BUY)
            ret, data = ctx.place_order(
                code=order["code"],
                price=float(order["price"]),
                qty=float(order["quantity"]),
                order_type=order_type,
                trd_side=trd_side,
                trd_env=env,
                adjust_limit=0.0 if order.get("is_hk") else None,
            )
            if ret != 0:
                out = {"success": False, "message": str(data)}
            else:
                # place_order 返回单行 DataFrame；提取标量字段。
                # dealt_qty/dealt_avg_price 对 MARKET 单即时成交的模拟单 >0，
                # 透传给 trading_engine 才能即时落成交记录，否则 SIMULATE 成交丢失。
                row = data.iloc[0] if len(data) else None
                order_id = ""
                status = ""
                filled_qty = 0.0
                filled_price = 0.0
                err_msg = ""
                if row is not None:
                    order_id = str(row.get("order_id", "") or "")
                    status = str(row.get("order_status", "") or "")
                    filled_qty = _f(row.get("dealt_qty"))
                    filled_price = _f(row.get("dealt_avg_price"))
                    err_msg = str(row.get("last_err_msg") or "")
                out = {
                    "success": True,
                    "order_id": order_id,
                    "status": status,
                    "filled_quantity": filled_qty,
                    "filled_price": filled_price,
                    "message": err_msg or "SUBMITTED",
                }

        elif op == "cancel":
            ret, data = ctx.modify_order(
                ModifyOrderOp.CANCEL,
                order_id=payload["order_id"],
                qty=0,
                price=0,
                trd_env=env,
            )
            out = {"success": ret == 0, "message": str(data) if ret != 0 else "CANCELLED"}

        else:
            out = {"success": False, "message": f"unknown op: {op}"}

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(out, ensure_ascii=False))
        return 0
    finally:
        ctx.close()


if __name__ == "__main__":
    sys.exit(main())
