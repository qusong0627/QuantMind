"""QMT 止盈/止损执行器运维 CLI（容器内运行）。

用法（容器 ``quantmind`` 内）：

    python backend/scripts/qmt_sltp_ctl.py --status
    python backend/scripts/qmt_sltp_ctl.py --enable
    python backend/scripts/qmt_sltp_ctl.py --disable
    python backend/scripts/qmt_sltp_ctl.py --mode limit_floor
    python backend/scripts/qmt_sltp_ctl.py --arm 600036.SH --stop 0.05 --take 0.10
    python backend/scripts/qmt_sltp_ctl.py --arm 600036.SH --entry 41.5 --stop 0.03 --qty 100
    python backend/scripts/qmt_sltp_ctl.py --rm 600036.SH
    python backend/scripts/qmt_sltp_ctl.py --reset 600036.SH
    python backend/scripts/qmt_sltp_ctl.py --evaluate     # 只读：算一遍触发线，不下单

``--evaluate`` 只读行情与柜台，不产生任何委托，适合盘中确认规则是否生效。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, "/app" if os.path.isdir("/app/backend") else os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.services.live_trading.services import sltp_executor as ex  # noqa: E402
from backend.services.live_trading.services.tdx_quote_feed import (  # noqa: E402
    check_sltp_trigger,
    load_sltp_config,
)
from backend.services.trade_shared.deps import get_redis  # noqa: E402


def _dump(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _load(redis):
    return ex.load_config(redis), ex.load_state(redis)


def _save(redis, cfg):
    saved = ex.save_config(redis, cfg)
    return saved


def cmd_arm(redis, args) -> None:
    cfg, _ = _load(redis)
    rule = {
        "symbol": ex.normalize_symbol(args.arm),
        "enabled": True,
        "side": "SELL",
        "entry_price": args.entry,
        "quantity": args.qty,
        "stop_loss_pct": args.stop,
        "take_profit_pct": args.take,
        "trailing_stop_pct": args.trail,
    }
    rules = [r for r in cfg.get("rules") or [] if ex.normalize_symbol(r.get("symbol", "")) != rule["symbol"]]
    rules.append(rule)
    cfg["rules"] = rules
    saved = _save(redis, cfg)
    print(f"已武装 {rule['symbol']}（共 {len(saved['rules'])} 条规则）")
    _dump(saved["rules"][-1])


def cmd_rm(redis, args) -> None:
    cfg, _ = _load(redis)
    target = ex.normalize_symbol(args.rm)
    rules = [r for r in cfg.get("rules") or [] if ex.normalize_symbol(r.get("symbol", "")) != target]
    cfg["rules"] = rules
    _save(redis, cfg)
    # 同步清掉状态，避免同名规则复用时沿用旧状态
    state = ex.load_state(redis)
    state.get("rules", {}).pop(target, None)
    ex.save_state(redis, state)
    print(f"已移除 {target}（剩余 {len(rules)} 条规则）")


async def cmd_evaluate(redis, args) -> None:
    cfg, state = _load(redis)
    rules = [r for r in cfg.get("rules") or [] if r.get("enabled", True)]
    if not rules:
        print("无启用规则")
        return
    from backend.services.live_trading.services.qmt_exec_client import get_qmt_exec_client

    client = get_qmt_exec_client()
    try:
        await client.refresh_settings()
    except Exception as exc:  # noqa: BLE001
        print(f"读取通道配置失败: {exc}")
    if not client.configured:
        print("QMT 执行端未配置/未启用（QMT_EXEC_ENABLED、QMT_EXEC_ACCOUNT_ID）")
        return
    fallback = load_sltp_config(str(cfg.get("tenant_id") or "default"), str(cfg.get("user_id") or "1"))
    ticks = await client.get_full_tick([r["symbol"] for r in rules])
    positions = await client.get_positions()
    for rule in rules:
        symbol = ex.normalize_symbol(rule["symbol"])
        st = (state.get("rules") or {}).get(symbol) or {}
        tick = (ticks or {}).get(symbol) or {}
        price = float(tick.get("lastPrice") or 0)
        pos = ex._find_position(positions, symbol)
        entry = rule.get("entry_price") or st.get("entry_price") or ex._position_entry_price(pos)
        can_use = float((pos or {}).get("can_use_volume") or 0)
        tcfg = ex.trigger_config(
            {**rule, "highest_price": st.get("highest_price") or price}, fallback
        )
        triggered, reason = check_sltp_trigger(price, float(entry or 0), tcfg)
        qty, note = ex.align_sell_quantity(symbol, rule.get("quantity") or 0, can_use)
        print(
            f"{symbol}: 现价={price or '-'} 成本={entry or '-'} 可用={can_use:g} "
            f"最高={st.get('highest_price') or '-'} 状态={st.get('status') or 'armed'} "
            f"触发={'是' if triggered else '否'} {reason or ''} 计划卖出={qty:g} {note or ''}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="QMT 止盈止损执行器运维 CLI")
    parser.add_argument("--status", action="store_true", help="打印配置与状态")
    parser.add_argument("--enable", action="store_true")
    parser.add_argument("--disable", action="store_true")
    parser.add_argument("--mode", choices=["limit_floor", "market"], help="保护价模式")
    parser.add_argument("--interval", type=float, help="轮询间隔（秒）")
    parser.add_argument("--alert-sec", type=float, help="未成交告警阈值（秒）")
    parser.add_argument(
        "--policy",
        choices=["alert_only", "cancel", "requote_at_protect_price"],
        help="未成交余量策略",
    )
    parser.add_argument("--close-reminder", type=float, help="收盘前提醒窗口（秒）")
    parser.add_argument("--arm", metavar="SYMBOL", help="新增/替换一条规则")
    parser.add_argument("--entry", type=float, help="成本价（缺省取柜台持仓成本）")
    parser.add_argument("--qty", type=float, help="卖出数量（缺省取柜台可用全量）")
    parser.add_argument("--stop", type=float, help="止损比例，如 0.05")
    parser.add_argument("--take", type=float, help="止盈比例，如 0.10")
    parser.add_argument("--trail", type=float, help="移动止损回撤比例，如 0.03")
    parser.add_argument("--rm", metavar="SYMBOL", help="删除规则")
    parser.add_argument("--reset", nargs="*", metavar="SYMBOL", help="重新武装（留空=全部）")
    parser.add_argument("--evaluate", action="store_true", help="只读评估（不下单）")
    args = parser.parse_args()

    redis = get_redis()

    if args.status or not any(
        [args.enable, args.disable, args.mode, args.interval, args.alert_sec,
         args.policy, args.close_reminder,
         args.arm, args.rm, args.reset is not None, args.evaluate]
    ):
        cfg, state = _load(redis)
        _dump({"config": cfg, "state": state})
        return

    touched = False
    if (
        args.enable
        or args.disable
        or args.mode
        or args.interval is not None
        or args.alert_sec is not None
        or args.policy
        or args.close_reminder is not None
    ):
        cfg, _ = _load(redis)
        if args.enable:
            cfg["enabled"] = True
        if args.disable:
            cfg["enabled"] = False
        if args.mode:
            cfg["protect_price_mode"] = args.mode
        if args.interval is not None:
            cfg["poll_interval_sec"] = max(1.0, args.interval)
        if args.alert_sec is not None:
            cfg["pending_alert_sec"] = max(0.0, args.alert_sec)
        if args.policy:
            cfg["remainder_policy"] = args.policy
        if args.close_reminder is not None:
            cfg["close_reminder_sec"] = max(0.0, args.close_reminder)
        _save(redis, cfg)
        print(
            f"配置已更新: enabled={cfg['enabled']} mode={cfg['protect_price_mode']} "
            f"interval={cfg['poll_interval_sec']}s alert={cfg['pending_alert_sec']}s "
            f"policy={cfg['remainder_policy']}"
        )
        touched = True

    if args.arm:
        cmd_arm(redis, args)
        touched = True
    if args.rm:
        cmd_rm(redis, args)
        touched = True
    if args.reset is not None:
        state = ex.reset_rules(redis, args.reset or None)
        print(f"已重新武装: {sorted((state.get('rules') or {}).keys())}")
        touched = True
    if args.evaluate:
        asyncio.run(cmd_evaluate(redis, args))
        touched = True

    if not touched:
        parser.print_help()


if __name__ == "__main__":
    main()
