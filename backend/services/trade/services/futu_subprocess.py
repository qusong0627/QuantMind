#!/usr/bin/env python3
"""FutuBroker 子进程执行器。

futu SDK 的连接/等待模型与 asyncio 事件循环混用会死锁，故由
overseas_brokers.FutuBroker 以独立子进程方式调用本脚本。

用法:
  python futu_subprocess.py <host> <port> <rsa_key_path> <op> <payload> <output_path>

op:
  account      — 查询账户（资产/持仓）
  account_both — 一次握手查 REAL+SIMULATE 两套账户（arena 双卡；省一次 RSA 握手）
  orders       — 当日订单历史（order_list_query）
  closed       — 已平仓行（qty==0 且 realized_pl!=0）
  snapshot     — 实时快照（走行情连接 OpenQuoteContext，免订阅）
  place        — 下单（payload.order: code/price/quantity/order_type/trd_side）
  cancel       — 撤单（payload.order_id）

place/cancel 在 payload 带 unlock_pwd_md5 且 env==REAL 时先解锁交易
（解锁凭据由服务层注入，见 futu_live）；失败短路不下单。

每个 op 的处理函数与解析逻辑抽为纯函数（本文件可脱离 futu SDK 独立
单测，SDK 相关导入保持函数内懒加载）。

**硬超时**：OpenD 未登录时 SDK 会挂在握手上永不返回（2026-10-08 实测僵死进程
驻留 ~296MB），而调用方 kill 掉 docker exec 客户端杀不到容器内的它——本脚本
自己装 SIGALRM 看门狗兜底（``HARD_TIMEOUT_S``），到点写失败 JSON 后 ``os._exit``。
"""

import json
import math
import os
import re
import signal
import sys
import warnings

warnings.filterwarnings("ignore")

#: 子进程硬超时（秒）；``FUTU_SUBPROCESS_TIMEOUT_S`` 可覆盖。
#: 必须**小于**所有调用方超时，否则看门狗没机会说话：
#: futu_live 的 45s 子进程超时、港股循环降级路径的 30s docker exec 超时。
HARD_TIMEOUT_S = 20

#: 结果已落盘后给 ``ctx.close()`` 的宽限（秒）；超了直接硬退，不陪 SDK 线程耗着。
CLOSE_GRACE_S = 5


def _timeout_from_env() -> int:
    """环境变量覆盖硬超时；缺失/非法/非正值一律回落默认（不静默关掉看门狗）。"""
    raw = os.environ.get("FUTU_SUBPROCESS_TIMEOUT_S", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return HARD_TIMEOUT_S
    return value if value > 0 else HARD_TIMEOUT_S


def _install_hard_timeout(seconds: int) -> None:
    """装 SIGALRM 看门狗：到点抛 ``TimeoutError``（signal 只能装在主线程，本脚本即主线程）。"""

    def _fire(signum, frame):
        raise TimeoutError(
            f"futu 子进程硬超时 {seconds}s——OpenD 未登录时 SDK 会挂在握手上"
            "（docker logs futu-opend 末行应停在「请输入账号」）"
        )

    signal.signal(signal.SIGALRM, _fire)
    signal.alarm(seconds)


def _exit_on_alarm(seconds: int, code: int = 0) -> None:
    """收尾阶段的看门狗：结果已落盘后再挂死也没关系，直接硬退。

    SDK 的连接线程**不是 daemon**：主线程一退，解释器会等它——实测进程能带着
    traceback 僵在退出路径上（这就是容器里那些驻留 1 小时的进程）。
    """
    signal.signal(signal.SIGALRM, lambda *_: os._exit(code))
    signal.alarm(seconds)


def _write_result(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False))

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


# ------------------------------------------------ 港股代码归一


#: 支持的港股写法：HK.700 / HK.0700 / HK.00700 / HK00700 / 700.HK / 00700.HK
_HK_CODE_RE = re.compile(r"^(?:HK[.\s]?(\d{1,5})|(\d{1,5})\.HK)$")


def _norm_hk_code(raw) -> str:
    """港股代码统一为 QuantMind 后缀式 '00700.HK'（zfill(5)，幂等）。

    富途 SDK 给的是 'HK.00700'，QuantMind 名表/前端 symbolAliases 认的是
    '00700.HK'——在子进程出口一次归一，下游（前端 reshape、HK 分析循环）
    不再各自兜底。非港股形态（US.AAPL / SH.600036 / CL.FUT 等）原样返回。
    """
    code = _as_str(raw).strip().upper()
    if not code or code.startswith("US."):
        return code
    match = _HK_CODE_RE.match(code)
    if not match:
        return code
    return f"{(match.group(1) or match.group(2)).zfill(5)}.HK"


def _to_futu_code(code: str) -> str:
    """归一后的 '00700.HK' → 富途 SDK 形态 'HK.00700'；其余原样（US./SH. 直通）。"""
    if code.endswith(".HK"):
        return f"HK.{code[:-3]}"
    return code


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
    """多行持仓 → {code: 合并后持仓}，跳过已平仓行；code 归一到 '00700.HK'。"""
    positions: dict[str, dict] = {}
    for row in rows:
        pos = _parse_position_row(row)
        if pos is None:
            continue
        code = _norm_hk_code(pos.pop("code"))
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


def _op_account_both(ctx, env, payload) -> dict:
    """一次握手查 REAL+SIMULATE 两套账户（arena 双卡）。

    单腿异常不拖垮另一腿：该腿置 null、errors[env] 记原因；
    两条腿都异常时上层按 503 处理（读面唯一例外，见路由层）。
    """
    from futu import TrdEnv

    out: dict = {"errors": {}}
    for leg, leg_name, leg_env in (
        ("real", "REAL", TrdEnv.REAL),
        ("simulate", "SIMULATE", TrdEnv.SIMULATE),
    ):
        try:
            data = _op_account(ctx, leg_env, payload)
            out[leg] = {**data, "env": leg_name}
        except Exception as e:  # noqa: BLE001 单腿失败不拖垮另一腿
            out[leg] = None
            out["errors"][leg_name] = str(e)
    return out


# ------------------------------------------------ op: orders / closed / snapshot


def _parse_order_row(row) -> dict:
    """order_list_query 单行 → 对外订单 dict（同 live_trading 委托字段口径）。

    注意：去重/合并是前端的事，这里逐行透传 Futu 原样（含终态行）。
    """
    return {
        "order_id": _as_str(row.get("order_id")),
        "code": _norm_hk_code(row.get("code")),
        "name": _as_str(row.get("stock_name")),
        "trd_side": _as_str(row.get("trd_side")),
        "order_type": _as_str(row.get("order_type")),
        "order_status": _as_str(row.get("order_status")),
        "qty": _as_float(row.get("qty")),
        "price": _as_float(row.get("price")),
        "dealt_qty": _as_float(row.get("dealt_qty")),
        "dealt_avg_price": _as_float(row.get("dealt_avg_price")),
        "create_time": _as_str(row.get("create_time")),
        "last_err_msg": _as_str(row.get("last_err_msg")),
    }


def _op_orders(ctx, env, payload) -> dict:
    ret, olist = ctx.order_list_query(trd_env=env)
    return {
        "orders": (
            [_parse_order_row(o) for _, o in olist.iterrows()]
            if ret == 0 and len(olist)
            else []
        )
    }


def _parse_closed_row(row) -> dict | None:
    """已平仓行判据：qty==0 且 realized_pl!=0；当前持仓行返回 None。"""
    qty = _as_float(row.get("qty"))
    realized = _as_float(row.get("realized_pl"))
    if qty != 0 or realized == 0:
        return None
    return {
        "code": _norm_hk_code(row.get("code")),
        "name": _as_str(row.get("stock_name")),
        "cost_price": _as_float(row.get("cost_price")),
        "last_price": _as_float(row.get("nominal_price")),
        "realized_pl": realized,
        "currency": _as_str(row.get("currency")) or "HKD",
    }


def _op_closed(ctx, env, payload) -> dict:
    ret, plist = ctx.position_list_query(trd_env=env)
    closed = []
    if ret == 0 and len(plist):
        for _, row in plist.iterrows():
            parsed = _parse_closed_row(row)
            if parsed is not None:
                closed.append(parsed)
    return {"closed": closed}


def _parse_snapshot_row(row) -> tuple[str, dict]:
    """快照单行 → (归一后代码, 快照 dict)；day_chg 为百分比（涨 1% = 1.0）。"""
    code = _norm_hk_code(row.get("code"))
    prev = _as_float(row.get("prev_close_price"))
    last = _as_float(row.get("last_price"))
    return code, {
        "name": _as_str(row.get("stock_name")),
        "last_price": last,
        "prev_close": prev,
        "day_chg": (last - prev) / prev * 100 if prev and last else 0.0,
        "volume": _as_float(row.get("volume")),
        "turnover": _as_float(row.get("turnover")),
    }


def _op_snapshot(ctx, env, payload) -> dict:
    codes = [
        _to_futu_code(_norm_hk_code(c))
        for c in payload.get("codes", [])
        if _as_str(c).strip()
    ]
    snaps: dict[str, dict] = {}
    if codes:
        ret, data = ctx.get_market_snapshot(code_list=codes)
        if ret == 0 and len(data):
            for _, row in data.iterrows():
                code, parsed = _parse_snapshot_row(row)
                if code:
                    snaps[code] = parsed
    return {"snapshot": snaps}


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


def _maybe_unlock(ctx, payload) -> dict | None:
    """REAL 单带 unlock_pwd_md5 时先解锁交易；失败返回错误 dict（调用方短路）。

    解锁凭据由服务层（futu_live）读券商配置后注入子进程 payload——HTTP
    路由层从不见该值，审计日志因此天然无密钥。
    """
    md5 = _as_str(payload.get("unlock_pwd_md5")).strip()
    if not md5 or _as_str(payload.get("env")).upper() != "REAL":
        return None
    ret, data = ctx.unlock_trade(password_md5=md5)
    if ret != 0:
        return {"success": False, "message": f"unlock_failed: {data}"}
    return None


def _op_place(ctx, env, payload) -> dict:
    from futu import OrderType, TrdSide

    unlock_err = _maybe_unlock(ctx, payload)
    if unlock_err is not None:
        return unlock_err

    order = payload["order"]
    # 下单代码归一到富途 SDK 形态（HK.00700）；US./SH. 等非港股形态原样透传
    code = _to_futu_code(_norm_hk_code(order["code"]))
    order_type = {"MARKET": OrderType.MARKET, "NORMAL": OrderType.NORMAL}.get(
        order["order_type"], OrderType.NORMAL
    )
    trd_side = {"BUY": TrdSide.BUY, "SELL": TrdSide.SELL}.get(
        order["trd_side"], TrdSide.BUY
    )
    ret, data = ctx.place_order(
        code=code,
        price=float(order["price"]),
        qty=float(order["quantity"]),
        order_type=order_type,
        trd_side=trd_side,
        trd_env=env,
        # 港股单用市价保护价（adjust_limit=0 让 SDK 自动取保护价）；非港股不传
        adjust_limit=0.0 if code.upper().startswith("HK.") else None,
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

    unlock_err = _maybe_unlock(ctx, payload)
    if unlock_err is not None:
        return unlock_err

    ret, data = ctx.modify_order(
        ModifyOrderOp.CANCEL,
        order_id=payload["order_id"],
        qty=0,
        price=0,
        trd_env=env,
    )
    return {"success": ret == 0, "message": str(data) if ret != 0 else "CANCELLED"}


# ------------------------------------------------ 入口


def _open_ctx(op: str, host: str, port: int, market: str = "HK"):
    """按 op 选连接：snapshot 走行情连接（OpenQuoteContext），其余走交易连接。

    market=HK/US 决定交易连接的 filter_trdmarket（富途支持港股/美股；
    既有 FutuBroker 调用不带 market → 默认 HK，与扩容前行为一致）。
    """
    if op == "snapshot":
        from futu import OpenQuoteContext

        return OpenQuoteContext(host=host, port=port, is_encrypt=True)

    from futu import OpenSecTradeContext, TrdMarket

    trd_market = TrdMarket.US if str(market).upper() == "US" else TrdMarket.HK
    return OpenSecTradeContext(
        filter_trdmarket=trd_market,
        host=host,
        port=port,
        security_firm="FUTUSECURITIES",
        is_encrypt=True,
    )


def main() -> int:
    host, port, rsa_key, op, payload, output_path = (
        sys.argv[1],
        int(sys.argv[2]),
        sys.argv[3],
        sys.argv[4],
        json.loads(sys.argv[5]),
        sys.argv[6],
    )

    # 早于 SDK 导入/握手生效：挂死点在 ctx 的任何一次 RPC 上。
    _install_hard_timeout(_timeout_from_env())

    from futu.common.sys_config import SysConfig

    SysConfig.set_init_rsa_file(rsa_key)

    from futu import TrdEnv

    # ctx 的构造就要连 OpenD（挂死点在这里，不在某个 op 里）——必须**在 try 内**，
    # 否则看门狗抛的 TimeoutError 会绕过失败落盘，进程带着 traceback 卡在退出路径。
    ctx = None
    try:
        ctx = _open_ctx(op, host, port, payload.get("market", "HK"))
        env = TrdEnv.REAL if payload.get("env") == "REAL" else TrdEnv.SIMULATE
        handlers = {
            "account": _op_account,
            "account_both": _op_account_both,
            "orders": _op_orders,
            "closed": _op_closed,
            "snapshot": _op_snapshot,
            "place": _op_place,
            "cancel": _op_cancel,
        }
        handler = handlers.get(op)
        out = (
            handler(ctx, env, payload)
            if handler is not None
            else {"success": False, "message": f"unknown op: {op}"}
        )
        _write_result(output_path, out)
        return 0
    except TimeoutError as exc:
        # 不能走 finally 的 ctx.close()——同一个握手，一样会挂死，看门狗就白装了。
        # 写完失败结果直接 os._exit：调用方按文件内容判失败，不依赖退出码。
        _write_result(output_path, {"success": False, "message": str(exc)})
        print(f"[futu_subprocess] {exc}", file=sys.stderr)
        os._exit(2)
    finally:
        if ctx is not None:
            _exit_on_alarm(CLOSE_GRACE_S)
            try:
                ctx.close()
            except Exception:  # noqa: BLE001 结果已落盘，收尾失败不该改结论
                pass


if __name__ == "__main__":
    sys.exit(main())
