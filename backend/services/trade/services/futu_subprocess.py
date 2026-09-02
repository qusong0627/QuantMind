#!/usr/bin/env python3
"""FutuBroker 子进程执行器。

futu SDK 的连接/等待模型与 asyncio 事件循环混用会死锁，故由
overseas_brokers.FutuBroker 以独立子进程方式调用本脚本。

用法:
  python futu_subprocess.py <host> <port> <rsa_key_path> <op> <payload> <output_path>

op:
  account — 查询账户（资产/持仓）
  place   — 下单（payload.order: code/price/quantity/order_type/trd_side/is_hk）
  cancel  — 撤单（payload.order_id）

每个 op 的处理函数与解析逻辑抽为纯函数（本文件可脱离 futu SDK 独立
单测，SDK 相关导入保持函数内懒加载）。
"""

import json
import math
import sys
import warnings

warnings.filterwarnings("ignore")

# ------------------------------------------------ 安全取值


def _as_float(value, default: float = 0.0) -> float:
    """Futu DataFrame 数值列可能返回 'N/A'、NaN 或 None（如 realized_pl），安全转 float。"""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(result) else result


def _as_str(value) -> str:
    """Futu DataFrame 单元格缺失/NaN → ''，避免 str(NaN)='nan' 污染下游字段。"""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value)


# ------------------------------------------------ op: account


def _parse_position_row(row) -> dict | None:
    """position_list_query 单行 → 持仓 dict；已平仓行（qty<=0）返回 None。

    Futu 对同一代码返回多行：当前持仓(qty>0) + 已平仓行(qty=0, realized_pl)。
    nominal_price 是实时价（current_price 列不存在）；缺失时按市值/数量兜底。
    """
    qty = _as_float(row.get("qty"))
    if qty <= 0:
        return None
    market_value = _as_float(row.get("market_val"))
    last_price = _as_float(row.get("nominal_price"))
    if not last_price and market_value:
        last_price = market_value / qty
    return {
        "code": _as_str(row.get("code")),
        "volume": qty,
        "available_volume": _as_float(row.get("can_sell_qty")),
        "price": last_price,
        "market_value": market_value,
        "cost": _as_float(row.get("cost_price")),
        "name": _as_str(row.get("stock_name")),
        "currency": _as_str(row.get("currency")) or "HKD",
    }


def _merge_position(existing: dict, new: dict) -> dict:
    """同代码多行（拆仓/多笔）合并：累加数量/市值/可卖，成本按数量加权。"""
    qty = existing["volume"] + new["volume"]
    market_value = existing["market_value"] + new["market_value"]
    return {
        **existing,
        "volume": qty,
        "available_volume": existing["available_volume"] + new["available_volume"],
        "price": market_value / qty,
        "market_value": market_value,
        "cost": (existing["cost"] * existing["volume"] + new["cost"] * new["volume"])
        / qty,
    }


def _aggregate_positions(rows) -> dict:
    """多行持仓 → {code: 合并后持仓}，跳过已平仓行。"""
    positions: dict[str, dict] = {}
    for row in rows:
        pos = _parse_position_row(row)
        if pos is None:
            continue
        code = pos.pop("code")
        positions[code] = (
            _merge_position(positions[code], pos) if code in positions else pos
        )
    return positions


def _read_account(ctx, trd_env) -> dict:
    """accinfo_query → 资产摘要 dict；查询失败返回空 dict。"""
    ret, data = ctx.accinfo_query(trd_env=trd_env)
    if ret != 0 or not len(data):
        return {}
    row = data.iloc[0]
    return {
        "total_asset": _as_float(row.get("total_assets")),
        "cash": _as_float(row.get("cash")),
        "market_value": _as_float(row.get("market_val")),
    }


def _op_account(ctx, env, payload) -> dict:
    out = _read_account(ctx, env)
    ret, plist = ctx.position_list_query(trd_env=env)
    out["positions"] = (
        _aggregate_positions(p for _, p in plist.iterrows())
        if ret == 0 and len(plist)
        else {}
    )
    return out


# ------------------------------------------------ op: place / cancel


_EMPTY_PLACE_RESULT = {
    "order_id": "",
    "status": "",
    "filled_quantity": 0.0,
    "filled_price": 0.0,
    "message": "SUBMITTED",
}


def _place_result_from_row(row) -> dict:
    """place_order 结果行 → 对外返回 dict；空行返回纯默认（SUBMITTED）。"""
    if row is None:
        return dict(_EMPTY_PLACE_RESULT)
    err_msg = _as_str(row.get("last_err_msg"))
    return {
        "order_id": _as_str(row.get("order_id")),
        "status": _as_str(row.get("order_status")),
        "filled_quantity": _as_float(row.get("dealt_qty")),
        "filled_price": _as_float(row.get("dealt_avg_price")),
        "message": err_msg or "SUBMITTED",
    }


def _op_place(ctx, env, payload) -> dict:
    from futu import OrderType, TrdSide

    order = payload["order"]
    order_type = {"MARKET": OrderType.MARKET, "NORMAL": OrderType.NORMAL}.get(
        order["order_type"], OrderType.NORMAL
    )
    trd_side = {"BUY": TrdSide.BUY, "SELL": TrdSide.SELL}.get(
        order["trd_side"], TrdSide.BUY
    )
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
        return {"success": False, "message": str(data)}
    # place_order 返回单行 DataFrame；dealt_qty/dealt_avg_price 对即时成交的
    # 模拟单 >0，透传才能在 trading_engine 即时落成交记录。
    row = data.iloc[0] if data is not None and len(data) else None
    out = _place_result_from_row(row)
    out["success"] = True
    return out


def _op_cancel(ctx, env, payload) -> dict:
    from futu import ModifyOrderOp

    ret, data = ctx.modify_order(
        ModifyOrderOp.CANCEL,
        order_id=payload["order_id"],
        qty=0,
        price=0,
        trd_env=env,
    )
    return {"success": ret == 0, "message": str(data) if ret != 0 else "CANCELLED"}


# ------------------------------------------------ 入口


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

    from futu import OpenSecTradeContext, TrdEnv, TrdMarket

    ctx = OpenSecTradeContext(
        filter_trdmarket=TrdMarket.HK,
        host=host,
        port=port,
        security_firm="FUTUSECURITIES",
        is_encrypt=True,
    )
    try:
        env = TrdEnv.REAL if payload.get("env") == "REAL" else TrdEnv.SIMULATE
        handlers = {
            "account": _op_account,
            "place": _op_place,
            "cancel": _op_cancel,
        }
        handler = handlers.get(op)
        out = (
            handler(ctx, env, payload)
            if handler is not None
            else {"success": False, "message": f"unknown op: {op}"}
        )
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(out, ensure_ascii=False))
        return 0
    finally:
        ctx.close()


if __name__ == "__main__":
    sys.exit(main())
