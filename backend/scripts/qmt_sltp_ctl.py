"""QMT 止盈/止损执行器运维 CLI（容器内运行）。

用法（容器 ``quantmind`` 内）：

    python backend/scripts/qmt_sltp_ctl.py --status
    python backend/scripts/qmt_sltp_ctl.py --enable
    python backend/scripts/qmt_sltp_ctl.py --disable
    python backend/scripts/qmt_sltp_ctl.py --mode aggressive
    python backend/scripts/qmt_sltp_ctl.py --arm 600036.SH --stop 0.05 --take 0.10
    python backend/scripts/qmt_sltp_ctl.py --arm 600036.SH --entry 41.5 --stop 0.03 --qty 100
    python backend/scripts/qmt_sltp_ctl.py --arm 600036.SH --stop-price 44.6     # 绝对价止损
    python backend/scripts/qmt_sltp_ctl.py --arm 600036.SH --take-price 52.0     # 绝对价止盈
    python backend/scripts/qmt_sltp_ctl.py --arm 600036.SH --move-trigger 105 --move-to 100
    python backend/scripts/qmt_sltp_ctl.py --arm 600036.SH --move-trigger 105 --move-to 105
    python backend/scripts/qmt_sltp_ctl.py --arm 600036.SH --stop 0.05 --reduce-pct 0.33
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

sys.path.insert(
    0,
    "/app"
    if os.path.isdir("/app/backend")
    else os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)

from backend.services.live_trading.services import sltp_executor as ex  # noqa: E402
from backend.services.live_trading.services.tdx_quote_feed import (  # noqa: E402
    check_sltp_trigger,
    load_sltp_config,
)
from backend.services.trade_shared.deps import get_redis  # noqa: E402


def _dump(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _load(redis):
    """读配置 + 状态。

    配置**读失败直接抛错**（``read_config_strict``）：CLI 的写路径都是「读-改-写」，
    把「读不到」当空配置再写回会把规则表整份抹掉（真单链路上等于静默解除所有止损）。
    注意不能用 ``load_config``：它读失败回落默认，而 ``RedisClient.get`` 恰好会把
    读失败吞成 ``None``——那句「读不到」就永远不会抛出来。
    """
    return ex.read_config_strict(redis), ex.load_state(redis)


def _save(redis, cfg):
    # 写后回读确认：`RedisClient.set` 写失败是静默的，不确认就会打印「已武装」
    # 而 Redis 里一条都没有（用户以为有止损，实盘裸奔）。
    return ex.save_config_strict(redis, cfg)


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
        "stop_loss_price": args.stop_price,
        "take_profit_price": args.take_price,
        "move_stop_trigger": args.move_trigger,
        "move_stop_to": args.move_to,
        "reduce_pct": args.reduce_pct,
    }
    # 组合口径复用执行器单源校验：非法**整条拒绝**（与 API/执行器一致）。
    # 不先校验的话 save_config 会静默丢弃该规则，CLI 却报「已武装」——
    # 用户以为有止损，实际一条都没落下。
    reason = ex.rule_reject_reason(ex.normalize_rule(rule))
    if reason:
        print(f"规则被拒绝（未写入）：{reason}", file=sys.stderr)
        raise SystemExit(2)
    # 人工 `--arm` 是**显式接管**：同标的的旧规则（不管是人工的还是决策层挂的）
    # 一律被这条顶掉，归属回到空（人工）。决策层下一轮会看到「该标的已有人工规则」
    # 并按人工优先处理（见 decision/watch_writer.py），不会反过来把它删掉。
    rules = [
        r
        for r in cfg.get("rules") or []
        if ex.normalize_symbol(r.get("symbol", "")) != rule["symbol"]
    ]
    rules.append(rule)
    cfg["rules"] = rules
    saved = _save(redis, cfg)
    armed = next(
        (
            r
            for r in saved["rules"]
            if ex.normalize_symbol(r.get("symbol", "")) == rule["symbol"]
        ),
        None,
    )
    if armed is None:  # 兜底：写入后被拒（正常已在上面拦住）
        print(
            f"规则未落库：{saved.get('rejected_rules') or '原因不明'}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    print(f"已武装 {rule['symbol']}（共 {len(saved['rules'])} 条规则）")
    _dump(armed)


def cmd_rm(redis, args) -> None:
    cfg, _ = _load(redis)
    target = ex.normalize_symbol(args.rm)
    rules = [
        r
        for r in cfg.get("rules") or []
        if ex.normalize_symbol(r.get("symbol", "")) != target
    ]
    cfg["rules"] = rules
    _save(redis, cfg)
    # 同步清掉状态，避免同名规则复用时沿用旧状态。
    # 走 `removed=` 而不是整份覆盖：整份覆盖的输入是上面那次读，读失败（Redis 抖动，
    # `RedisClient.get` 会把它吞成空状态）就会把**全部**规则状态一起抹掉。
    state = ex.load_state(redis)
    ex.save_state(redis, state, removed={target})
    print(f"已移除 {target}（剩余 {len(rules)} 条规则）")


def evaluate_lines(
    cfg: dict, state: dict, ticks: dict, positions: list, fallback: dict
) -> list[str]:
    """只读预演的每行输出（纯函数：喂给它的行情/持仓就是全部输入）。

    **与执行器同源**：``trigger_inputs``（含棘轮抬高后的有效防守位）与
    ``plan_sell_quantity``（含 ``reduce_pct`` 部分减仓）都调执行器的函数。
    这里各写一份的话，预演说「不触发」而实盘卖出只是时间问题。
    """
    lines: list[str] = []
    for rule in [r for r in cfg.get("rules") or [] if r.get("enabled", True)]:
        symbol = ex.normalize_symbol(rule["symbol"])
        st = (state.get("rules") or {}).get(symbol) or {}
        tick = (ticks or {}).get(symbol) or {}
        price = float(tick.get("lastPrice") or 0)
        pos = ex._find_position(positions, symbol)
        entry = (
            rule.get("entry_price")
            or st.get("entry_price")
            or ex._position_entry_price(pos)
        )
        can_use = float((pos or {}).get("can_use_volume") or 0)
        tcfg = ex.trigger_config(ex.trigger_inputs(rule, st), fallback)
        triggered, reason = check_sltp_trigger(price, float(entry or 0), tcfg)
        qty, note = ex.plan_sell_quantity(symbol, rule, can_use)
        lines.append(
            f"{symbol}: 现价={price or '-'} 成本={entry or '-'} 可用={can_use:g} "
            f"最高={st.get('highest_price') or '-'} 防守位={tcfg.get('stop_loss_price') or '-'} "
            f"状态={st.get('status') or 'armed'} "
            f"触发={'是' if triggered else '否'} {reason or ''} 计划卖出={qty:g} {note or ''}"
        )
    return lines


async def cmd_evaluate(redis, args) -> None:
    cfg, state = _load(redis)
    rules = [r for r in cfg.get("rules") or [] if r.get("enabled", True)]
    if not rules:
        print("无启用规则")
        return
    from backend.services.live_trading.services.qmt_exec_client import (
        get_qmt_exec_client,
    )

    client = get_qmt_exec_client()
    try:
        await client.refresh_settings()
    except Exception as exc:  # noqa: BLE001
        print(f"读取通道配置失败: {exc}")
    if not client.configured:
        print("QMT 执行端未配置/未启用（QMT_EXEC_ENABLED、QMT_EXEC_ACCOUNT_ID）")
        return
    fallback = load_sltp_config(
        str(cfg.get("tenant_id") or "default"), str(cfg.get("user_id") or "1")
    )
    ticks = await client.get_full_tick([r["symbol"] for r in rules])
    positions = await client.get_positions()
    for line in evaluate_lines(cfg, state, ticks, positions, fallback):
        print(line)


def main() -> None:
    parser = argparse.ArgumentParser(description="QMT 止盈止损执行器运维 CLI")
    parser.add_argument("--status", action="store_true", help="打印配置与状态")
    parser.add_argument("--enable", action="store_true")
    parser.add_argument("--disable", action="store_true")
    parser.add_argument(
        "--mode", choices=list(ex.VALID_PROTECT_MODES), help="保护价模式"
    )
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
    parser.add_argument(
        "--stop-price", type=float, help="绝对价止损（元），与 --stop 取更紧者"
    )
    parser.add_argument(
        "--take-price", type=float, help="绝对价止盈（元），与 --take 取更早触发者"
    )
    parser.add_argument(
        "--move-trigger", type=float, help="棘轮触发价：现价上触即抬防守"
    )
    parser.add_argument(
        "--move-to",
        type=float,
        help="棘轮目标防守价（须不高于触发价；相等即零间隙棘轮）",
    )
    parser.add_argument("--reduce-pct", type=float, help="部分减仓比例 (0,1]，如 0.33")
    parser.add_argument("--rm", metavar="SYMBOL", help="删除规则")
    parser.add_argument(
        "--reset", nargs="*", metavar="SYMBOL", help="重新武装（留空=全部）"
    )
    parser.add_argument("--evaluate", action="store_true", help="只读评估（不下单）")
    args = parser.parse_args()

    try:
        redis = get_redis()
        _dispatch(redis, args, parser)
    except Exception as exc:  # noqa: BLE001 - 读配置失败时报错退出，绝不写回空配置
        print(f"执行失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def _dispatch(redis, args, parser) -> None:
    if args.status or not any(
        [
            args.enable,
            args.disable,
            args.mode,
            args.interval,
            args.alert_sec,
            args.policy,
            args.close_reminder,
            args.arm,
            args.rm,
            args.reset is not None,
            args.evaluate,
        ]
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
