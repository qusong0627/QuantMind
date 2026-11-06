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


def normalize_method(method: str | None) -> str:
    return str(method or '').strip().lower()


def is_order_like(method: str | None) -> bool:
    return normalize_method(method) in ORDER_LIKE_METHODS


def is_trade_context(method: str | None) -> bool:
    """该方法是否走 adjust 主线程（挂了就会超时的那一类）。"""
    return normalize_method(method) in TRADE_CONTEXT_METHODS


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
