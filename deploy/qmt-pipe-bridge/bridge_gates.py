"""伴生桥的纯判定函数（可单测，不依赖 redis / Windows 管道）。

2026-11-06 事故链：QMT 策略主线程被**交易类查询**挂死后（``get_trade_detail_data``
只能在 adjust 主线程跑），容器侧的轮询请求继续入队；伴生桥每 15s 才超时放掉
一条，队列越积越多。**重启 QMT 后这些积压会立刻被灌进新策略**——三次实测
重启后存活时间 12h → 6min → 3min，积压一轮比一轮多。

这里的判定保证：
* 策略账户通道被判卡死时，**交易类只读请求**立即失败返回、绝不转发也不回灌；
* **订单/撤单永远放行**（不能因为查询通道坏了就丢真单）；
* **行情类方法照常**（``get_full_tick`` 等跑在后台 listener 线程，不受主线程
  死锁影响，故障期用户仍需要盘口）。
"""
from __future__ import annotations

import json

#: 订单/撤单类方法：任何情况下都放行（宁可让它超时，也不能本地丢掉）。
ORDER_LIKE_METHODS = frozenset({
    'order_stock', 'order_stock_async', 'order_stock_batch', 'passorder',
    'submit_order', 'submit_orders_batch',
    'cancel_order_stock', 'cancel_order_stock_sysid', 'cancel_order_stock_batch',
    'cancel_order', 'cancel_orders_batch',
})

#: 交易上下文方法集合 —— 与 QMT 侧 ``LISTENER_DEFERRED_METHODS`` 同源：
#: 它们必须由 adjust 主线程执行，主线程一挂就全部超时。故障期只对这些退避。
TRADE_CONTEXT_METHODS = frozenset({
    'sync_positions',
    'get_asset', 'get_positions', 'get_position_statistics', 'query_stock_position',
    'query_orders', 'query_trades', 'query_execution_snapshot',
    # 容器侧发的是 QMT 官方**线名**，靠 METHOD_ALIASES 映射到上面的方法；
    # 退避判定必须按线名来，否则积压照样会灌给新策略（2026-11-06 实测）。
    'query_stock_asset', 'query_stock_positions', 'query_position_statistics',
    'query_stock_orders', 'query_stock_trades',
    'describe_trade_detail_fields', 'reload_deployment',
    'query_account_infos', 'query_account_status',
    'query_credit_detail', 'query_credit_account', 'query_stk_compacts',
    'query_credit_subjects', 'query_credit_slo_code', 'query_credit_assure',
    'query_appointment_info', 'get_ipo_data',
    'get_assure_contract', 'get_enable_short_contract', 'get_unclosed_compacts',
    'get_closed_compacts', 'get_debt_contract', 'get_option_subject_position',
    'get_comb_option', 'get_new_purchase_limit', 'get_hkt_exchange_rate',
    'query_smt_secu_info', 'query_smt_secu_rate', 'get_value_by_order_id',
    'get_last_order_id', 'get_history_trade_detail_data',
})

#: 策略账户通道状态
STATE_UNKNOWN = 'unknown'
STATE_OK = 'ok'
STATE_UNRESPONSIVE = 'unresponsive'

#: 账户探针连续失败达到该值 → 判定 unresponsive
FAIL_THRESHOLD = 2

#: QMT 线名 → 策略侧 canonical 名（与 ``bigqmt_signal_trader.redis_rpc``
#: ``METHOD_ALIASES`` 同源）。风控/幂等/积压闸必须按 canonical 判定，否则
#: ``order_stock_async`` 这类别名会绕过只认 ``order_stock`` 的闸门。
METHOD_ALIASES = {
    'get_full_tick': 'get_ticks',
    'get_instrument_detail': 'get_instrument',
    'get_instrumentdetail': 'get_instrument',
    'getdividfactors': 'get_divid_factors',
    'query_stock_asset': 'get_asset',
    'query_stock_positions': 'get_positions',
    'query_position_statistics': 'get_position_statistics',
    'query_stock_orders': 'query_orders',
    'query_stock_trades': 'query_trades',
    'order_stock': 'submit_order',
    'order_stock_async': 'submit_order',
    'order_stock_batch': 'submit_orders_batch',
    'cancel_order_stock': 'cancel_order',
    'cancel_order_stock_sysid': 'cancel_order',
    'cancel_order_stock_batch': 'cancel_orders_batch',
}


def normalize_method(method: str | None) -> str:
    return str(method or '').strip().lower()


def canonical_method(method: str | None) -> str:
    """线名 → canonical 名；未知方法原样返回（小写）。"""
    name = normalize_method(method)
    return METHOD_ALIASES.get(name, name)


def is_order_like(method: str | None) -> bool:
    return canonical_method(method) in ORDER_LIKE_METHODS


def is_trade_context(method: str | None) -> bool:
    """该方法是否走 adjust 主线程（挂了就会超时的那一类）。"""
    return canonical_method(method) in TRADE_CONTEXT_METHODS


def should_drop_request(method: str | None, strategy_state: str) -> bool:
    """策略账户通道卡死时，这条请求是否应当立即失败（不转发）。

    只丢交易类只读请求：调用方早已等到超时，继续转发只会占住单线程管道，
    并在重启后把积压回灌给新策略。订单/撤单永远不丢，行情类不受影响。
    """
    if strategy_state != STATE_UNRESPONSIVE:
        return False
    if is_order_like(method):
        return False
    return is_trade_context(method)


def split_backlog(items, strategy_state: str, decode_payload=None):
    """RPC 队列条目 → (可清除的 ``[(raw, req)]``, 保留的 ``[raw]``)。

    这是「账户通道判死后**清掉已积压的交易类只读请求**」的纯判定，防止
    QMT 重启时把旧查询一次性灌给新策略（2026-11-06 实测存活 12h→6min→3min）。

    保留（fail-closed）：订单/撤单、行情类、以及**解码失败**的条目——解不开
    就当它有可能是订单，宁可让它超时，也不本地丢。
    """
    if decode_payload is None:
        from bigqmt_signal_trader.redis_rpc import (  # noqa: PLC0415
            decode_rpc_request_payload,
        )

        decode_payload = decode_rpc_request_payload
    purge: list[tuple[object, dict]] = []
    keep: list[object] = []
    for item in items or ():
        try:
            text = (
                item.decode("utf-8", "replace")
                if isinstance(item, (bytes, bytearray))
                else str(item)
            )
            req = json.loads(decode_payload(text))
            if not isinstance(req, dict):
                raise ValueError("not a dict")
            method = str(req.get("method") or "")
        except Exception:  # noqa: BLE001 - 解码失败按订单处理，保留
            keep.append(item)
            continue
        if should_drop_request(method, strategy_state):
            purge.append((item, req))
        else:
            keep.append(item)
    return purge, keep


def next_state(prev: str, probe_ok: bool, consecutive_failures: int,
               threshold: int = FAIL_THRESHOLD) -> str:
    """探针结果 → 新状态。

    * 探针成功：立即恢复 ok（哪怕之前判过 unresponsive）
    * 连续失败未达阈值：保持原状态（避免一次抖动就拒发请求）
    * 连续失败达阈值：unresponsive
    """
    if probe_ok:
        return STATE_OK
    if int(consecutive_failures) >= max(1, int(threshold)):
        return STATE_UNRESPONSIVE
    return prev


def refill_tokens(tokens: float, last_ts: float, now_ts: float,
                  interval_s: float, capacity: int) -> tuple[float, float]:
    """令牌桶补充：每 ``interval_s`` 秒回 1 个，上限 ``capacity``。

    比"最小间隔"多一个突发额度：positions 一轮要连发 get_asset + get_positions
    两条，硬间隔会把第二条永远挡在门外（pos_age 恒 -1）。
    """
    capacity = max(1, int(capacity))
    if float(interval_s or 0) <= 0:
        return float(capacity), float(now_ts)
    elapsed = max(0.0, float(now_ts) - float(last_ts or 0.0))
    tokens = min(float(capacity), float(tokens or 0.0) + elapsed / float(interval_s))
    return tokens, float(now_ts)


def take_token(tokens: float, last_ts: float, now_ts: float,
               interval_s: float, capacity: int,
               n: float = 1.0) -> tuple[bool, float, float]:
    """尝试取 ``n`` 个令牌 → (是否取到, 剩余令牌, 新时间戳)。

    ``n>1`` 用于**成对调用**（positions 一轮必须连发 get_asset + get_positions）：
    分开取会被"只够一条"卡成永远只发第一条，配对永远完不成（2026-11-06 实测
    pos_age 4028s）。
    """
    need = max(1.0, float(n))
    tokens, last_ts = refill_tokens(tokens, last_ts, now_ts, interval_s, capacity)
    if tokens >= need:
        return True, tokens - need, last_ts
    return False, tokens, last_ts
