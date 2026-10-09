#!/usr/bin/env python3
"""历史市场分析报告选股 → 通达信桥实盘买卖全流程。

买入流程（--mode buy，默认）：
  1. 选股：scripts/select_from_reports.py（最新 picks.json / --source md 市场分析报告个股）
  2. 桥探活：GET /api/v1/health
  3. 账户：POST /api/v1/account/query（可用资金 / 现持仓）
  4. 行情：TdxAiData 实时源批量日K（失败逐票回退桥）
  5. 计算：涨跌停/停牌过滤 → 100 股整数倍量 → 单票 ≤ 20% 资金、限价=现价×1.01
  6. 下单：POST /api/v1/plans/execute
  7. 记录：logs/live_trade_YYYYMMDD.jsonl + orders/query 委托验证

卖出流程（--mode sell）：
  1. 账户查询现持仓 → 2. 逐票实时行情（限价=现价×0.99，T+1 可用量检查）
  3. 卖出 → 4. 记录 + 委托验证
  标的：--sell-all 全部持仓 | --sell-codes 600519.SH,000858.SZ 指定 | --sell-pct 0.5 每只卖一半
  提示：当日买入 T+1 不可卖（available_volume=0），桥会拒绝，脚本会跳过

安全：
  - 默认 --dry-run：只演练（选股/资金/价格/拟下量），不下单
  - --execute 才真下

用法：
  python scripts/live_trade_picks.py                            # buy dry-run
  python scripts/live_trade_picks.py --source md --top 5        # 市场分析报告个股 dry-run
  python scripts/live_trade_picks.py --execute --source md      # 实盘买入（08-28 报告个股）
  python scripts/live_trade_picks.py --mode sell --dry-run      # 卖出演练
  python scripts/live_trade_picks.py --mode sell --execute --sell-all   # 实盘清仓
"""
import argparse
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_tools.brokers.tdx_bridge import TdxBridgeBroker  # noqa: E402
import gate_rules  # noqa: E402  规则 id 词表（影子代价账的分组键）
# 实盘分账（每 agent ¥10 万虚拟子账户）
from live_ledger import (  # noqa: E402
    AGENT_QUOTA,
    agent_remaining,
    agent_used,
    find_holder,
    load_ledger,
    record_buy,
    record_sell,
    save_ledger,
)

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


def enabled_agents() -> list:
    """实盘分账 agent 名单：astock_config.json 里 enabled 的模型。"""
    cfg = Path(__file__).resolve().parent.parent / "configs" / "astock_config.json"
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
        return [m["name"] for m in data.get("models", []) if m.get("enabled")]
    except Exception:  # noqa: BLE001
        return ["deepseek-v4-flash", "deepseek-v4-pro", "glm-5.3-flash"]

# 交易时间固定用市场本地时区（本机时区可能是 JST，不能依赖）
CN_TZ = ZoneInfo("Asia/Shanghai")


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def log_line(entry: dict) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    stamp = now_cn().strftime("%Y%m%d")
    with open(LOG_DIR / f"live_trade_{stamp}.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def latest_klines(broker, code: str) -> list:
    """日K 最近 5 根（前复权）。"""
    return broker.get_klines(code, interval="daily")[-5:]


def compute_order(bars: list, cash: float, pct: float, code: str = "") -> dict:
    """由 K 线计算下单参数：现价/涨跌幅/单量/限价（板块口径涨跌停+买入量合规化）。

    拒单返回里带 `rule`：稳定的规则 id（gate_rules 词表）——`reason` 是中文
    句子、可以改；影子代价账按 id 分组算成本，拿句子当键会在改文案那天断档
    （同 buy_gate.BuyDecision.rule）。
    """
    if len(bars) < 2:
        return {"ok": False, "reason": "K线不足", "rule": gate_rules.DATA_NO_QUOTE}
    last, prev = bars[-1], bars[-2]
    # 桥返回字符串价格、TdxAiData 可能返回 NaN，需 float + isfinite 检查（math 顶层导入）
    try:
        price = float(last.get("close") or last.get("open") or 0)
        prev_close = float(prev.get("close") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "价格解析失败", "rule": gate_rules.DATA_NO_QUOTE}
    if not math.isfinite(price) or not math.isfinite(prev_close) or not price or not prev_close:
        return {"ok": False, "reason": "无有效价格", "rule": gate_rules.DATA_NO_QUOTE}
    chg = (price - prev_close) / prev_close * 100
    # 涨停/跌停/停牌过滤（板块口径 ±10%/±20%/±30%；停牌=无最新 bar 或成交量为 0）
    from ashare_rules import (MIN_LOT_SLACK, at_limit_down, at_limit_up,
                              min_buy_cost, min_buy_qty, round_buy_qty)
    from live_fills import buy_limit_and_cost          # 限价口径的唯一出处

    if at_limit_up(code, chg):
        return {"ok": False, "reason": f"涨停（{chg:+.1f}%），不追", "rule": gate_rules.LIMIT_UP}
    if at_limit_down(code, chg):
        return {"ok": False, "reason": f"跌停（{chg:+.1f}%），不接", "rule": gate_rules.LIMIT_DOWN}
    if not last.get("volume"):
        return {"ok": False, "reason": "停牌或无成交", "rule": gate_rules.SYMBOL_HALTED}
    budget = cash * pct
    raw_vol = round_buy_qty(code, int(budget / price))
    # 资金量判据（2026-09-09）：最小可买股数（科创板 200，其余 100）× 现价 > 单票预算
    # → 这只票该 agent 根本买不起。提示词侧 filter_affordable 已按同一口径整行剔除，
    # 这里兜底（模型从对话历史/记忆里报出的、或预算刚被其他成交吃掉的情况）。
    need = min_buy_cost(code, price)
    if raw_vol <= 0 or raw_vol * price > budget * MIN_LOT_SLACK:
        return {"ok": False,
                "reason": f"资金不足：最小可买 {min_buy_qty(code)} 股需 ¥{need:,.0f}"
                          f" > 单票预算 ¥{budget:,.0f}（剩余额度×{pct:.0%}）",
                "rule": gate_rules.BUDGET_BELOW_MIN_LOT}
    if raw_vol < 100:
        return {"ok": False, "reason": "资金不足 1 手",
                "rule": gate_rules.BUDGET_BELOW_MIN_LOT}
    # 限价口径成本：见 live_fills.buy_limit_and_cost（三市场唯一出处）。
    # 原先这里与美/港股各写一遍，已实测漂移（美/港股那份按现价算）——收敛到一处。
    limit_price, cost = buy_limit_and_cost(price, raw_vol, "cn")
    return {
        "ok": True,
        "price": price,
        "prev_close": prev_close,
        "chg_pct": round(chg, 2),
        "volume": raw_vol,
        "limit_price": limit_price,   # 限价买：现价 +1%
        "cost": cost,
    }


def _pend_or_warn(order_id, agent: str, code: str, side: str,
                  vol: int, price: float) -> None:
    """未确认成交 → 挂 pending 交 reconcile 兜底；桥没给委托号则如实说明。

    add_pending 对空委托号会落一条 untracked_order 事件（不写假在途单），CLI 输出
    必须与之一致——原来无条件打印「已挂 pending」，操作员会以为成交已受跟踪。
    """
    from live_fills import add_pending

    add_pending(order_id, agent, code, side, vol, price, now_cn().isoformat())
    if order_id:
        print(f"   ⏳ {code}: 未确认成交 → 已挂 pending，reconcile 按真实成交兜底记账")
    else:
        print(f"   ⚠️ {code}: 桥未返回委托号——成交无法跟踪，"
              f"已落事件需人工核对当日委托")


def sell_flow(broker, args, log_line) -> int:
    """卖出流程：桥持仓 → TdxAiData 行情 → 限价卖出。
    标的：--sell-all（默认）全部持仓 | --sell-codes 指定；
    每只可卖量 × --sell-pct，100 股整数倍；跌停/停牌跳过；T+1 当日买入不可卖（available=0 跳过）。"""
    import requests

    try:
        h = requests.get(f"{broker.bridge_url}/api/v1/health", timeout=8)
        print(f"🏥 桥健康: status={h.json().get('status')} tdx_connected={h.json().get('tdx_connected')}")
    except Exception as e:
        print(f"❌ 桥不可达: {e}")
        return 1
    acct = broker._account_query()
    positions = acct.get("positions") or []
    if not positions:
        print("ℹ️ 无持仓，卖出流程无需执行")
        return 0
    print(f"💰 持仓 {len(positions)} 只:")
    for p in positions:
        print(f"   {p.get('stock_code')} 总量={p.get('total_volume')} 可卖={p.get('available_volume')}")
    ledger = load_ledger()
    # 在途卖单提示（不拦截，人工路径以操作员意图为准）：自动路径（哨兵/整点轮）
    # 可能刚下过同一代码的止损单，再下一单柜台会按可用量驳回或造成超卖
    from live_fills import inflight_codes, wait_fill

    pending_sell = inflight_codes("sell")

    # 标的选择
    if args.sell_codes:
        want = {c.strip() for c in args.sell_codes.split(",") if c.strip()}
        targets = [p for p in positions if p.get("stock_code") in want]
        skipped = [c for c in want if c not in {p.get("stock_code") for p in positions}]
        if skipped:
            print(f"⚠️ 未持仓: {', '.join(skipped)}")
    else:  # --sell-all 或默认全部持仓
        targets = positions
    if not targets:
        print("ℹ️ 无目标持仓可卖")
        return 0

    # 行情：TdxAiData 批量 → 回退桥
    from agent_tools.datasources import tdx_aidata

    codes = [t.get("stock_code") for t in targets]
    bars_map = {}
    if tdx_aidata.available():
        try:
            bars_map = tdx_aidata.get_klines_batch(codes, interval="daily", count=5)
        except Exception as e:
            print(f"⚠️  TdxAiData 行情失败: {e} → 回退桥")
    for t in targets:
        c = t.get("stock_code")
        if c not in bars_map or not bars_map[c]:
            try:
                bars_map[c] = broker.get_klines(c, interval="daily")[-5:]
            except Exception:
                bars_map[c] = []

    sold = []
    for t in targets:
        code = t.get("stock_code")
        avail = int(t.get("available_volume") or 0)
        if avail <= 0:
            print(f"⏭️  {code}: 可卖量 0（T+1 当日买入），跳过")
            continue
        bars = bars_map.get(code) or []
        if len(bars) < 2:
            print(f"⏭️  {code}: 行情不足，跳过")
            continue
        last, prev = bars[-1], bars[-2]
        try:
            price = float(last.get("close") or 0)
            prev_close = float(prev.get("close") or 0)
        except (TypeError, ValueError):
            price, prev_close = 0, 0
        if not math.isfinite(price) or not math.isfinite(prev_close) or not price or not prev_close:
            print(f"⏭️  {code}: 无有效价格，跳过")
            continue
        chg = (price - prev_close) / prev_close * 100
        # 板块口径统一走 ashare_rules（主板 ±10 / 创业板·科创 ±20 / 北交 ±30，含 ST）：
        # 原 `chg <= -9.9` 硬编码对 20% 板块会把 -10%~-20% 的可卖仓位误判成跌停跳过
        from ashare_rules import aggressive_sell_price, at_limit_down, round_sell_qty

        if at_limit_down(code, chg):
            print(f"⏭️  {code}: 跌停（{chg:+.1f}%），卖不出，跳过")
            continue
        # 手数合规：主板/创业板整手、科创/北交 ≥200/100 起 1 股递增 + 碎股一次性全清
        # （原 int(avail*pct/100)*100 在科创 250 股只想卖 200，剩 50 股烂账）
        vol = round_sell_qty(code, int(avail * args.sell_pct), avail)
        if vol <= 0:
            print(f"⏭️  {code}: 卖出量不足 1 手，跳过")
            continue
        # 限价卖口径唯一出处（2026-09-21 审查 MEDIUM-5）：旧实现自建 round() 而非
        # ashare_rules.limit_price 的 HALF_UP，且下限夹取各写各的。报跌停价在连续
        # 竞价属越界申报（41 笔废单实录），统一走 max(跌停价, 现价×0.99)。
        limit = aggressive_sell_price(code, prev_close, price)
        if limit is None:
            print(f"⏭️  {code}: 无法计算合法报单价，跳过")
            continue
        print(f"📉 {code}: 现价 ¥{price:.2f} ({chg:+.2f}%) 拟卖 {vol}/{avail} 股 限价 ¥{limit:.2f}")
        if code in pending_sell:
            print(f"   ⚠️  {code}: 已有在途卖单未确认（data/live_pending_orders.json）"
                  f"——再下同向单可能超卖，请先核对当日委托再决定")
        if not args.execute:
            continue
        try:
            result = broker.sell(None, None, code, vol, price=limit)
        except Exception as e:
            print(f"❌  {code} 卖出失败: {e}")
            log_line({"ts": now_cn().isoformat(), "mode": "sell",
                      "code": code, "volume": vol, "price": limit, "error": str(e)})
            continue
        from live_fills import ack_line

        print(ack_line(f"{code} 卖出", result))
        sold.append(result)
        log_line({"ts": now_cn().isoformat(), "mode": "sell",
                  "code": code, "volume": vol, "price": limit, "result": result})
        # 分账记账：下单≠成交——等成交回报按真实成交价/量扣减；未确认成交挂 pending
        # 由 reconcile 兜底（2026-09-11 前按委托限价即刻记账：跌停封死没卖出去也照样
        # 扣减持仓、释放额度，账本与真实持仓脱节）
        #
        # ⚠️ 待办（2026-09-12 审查 MEDIUM-2）：这里是**手工**下单路径，仍未接入
        # live_fills.settle_place_fill——部分成交的余量不会挂 pending 继续跟踪，
        # 且账本写不在 _file_lock 内（与 cron 的 record-only/reconcile 并发时可能丢
        # 更新）。无人值守的四条路径（09:35 调仓、整点轮）已收口；手工路径迁移待办。
        holder = find_holder(ledger, code)
        order_id = str(result.get("order_id") or "")
        fill = wait_fill(broker, order_id)
        fv = int((fill or {}).get("filled_volume") or 0)
        if fv > 0:
            fp = float(fill.get("filled_price") or limit)
            if holder:
                ledger = record_sell(ledger, holder, code, fv, fp, now_cn().isoformat())
                save_ledger(ledger)
                print(f"   📒 分账释放: {holder} -{fv}股 {code} 成交 ¥{fp:.2f}"
                      f"（剩余 ¥{agent_remaining(ledger, holder):,.0f}）")
        elif holder:
            _pend_or_warn(order_id, holder, code, "sell", vol, limit)
        else:
            print(f"   ℹ️  {code}: 无分账 agent 持有该代码，跳过记账（仅桥账户变动）")
        time.sleep(1)  # 桥限流 60 req/min

    if args.execute and sold:
        print("\n📋 当日委托:")
        for o in broker.get_orders():
            print(f"   {o.get('stock_code')} {o.get('side')} "
                  f"{o.get('total_volume')}股 status={o.get('status')} id={o.get('order_id')}")
    print(f"\n{'✅ 卖出完成' if args.execute else '🟡 卖出演练完成'}: 受理 {len(sold)} 笔")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="报告选股 → 通达信桥实盘下单")
    ap.add_argument("--execute", action="store_true", help="真下单（默认仅 dry-run 演练）")
    ap.add_argument("--source", choices=["picks", "md"], default="picks")
    ap.add_argument("--top", type=int, default=5, help="取前 N 只候选")
    ap.add_argument("--per-stock-pct", type=float, default=0.2,
                    help="单票占可用资金比例（默认 0.2）")
    ap.add_argument("--min-side", choices=["BUY", "ADD", "HOLD"], default="BUY",
                    help="最低 side 门槛（默认 BUY：只买推荐买入的）")
    ap.add_argument("--mode", choices=["buy", "sell"], default="buy",
                    help="buy=买入（默认）；sell=卖出持仓")
    ap.add_argument("--sell-all", action="store_true", help="卖出全部持仓")
    ap.add_argument("--sell-codes", help="卖出指定股票（逗号分隔：600519.SH,000858.SZ）")
    ap.add_argument("--sell-pct", type=float, default=1.0,
                    help="每只卖出比例（默认 1.0=全部可卖量；按板块手数取整，"
                         "取整后不足 1 手的标的跳过）")
    args = ap.parse_args()

    mode = "🔴 实盘执行" if args.execute else "🟡 DRY-RUN 演练（不下单）"
    print(f"{mode}  {now_cn():%F %T}  [mode={args.mode}]")

    broker = TdxBridgeBroker()
    if args.mode == "sell":
        return sell_flow(broker, args, log_line)

    # 1. 选股（仅买入模式）
    import subprocess

    sel = subprocess.run(
        [sys.executable, "scripts/select_from_reports.py", "--source", args.source,
         "--top", str(args.top), "--min-side", args.min_side, "--json"],
        capture_output=True, text=True, check=True)
    picks = json.loads(sel.stdout)
    if not picks:
        print("❌ 无候选（side≥BUY），流程终止")
        sys.exit(0)
    print(f"📋 候选 {len(picks)} 只:")
    for p in picks:
        print(f"   {p['code']} {p['name']} side={p['side']} score={p['score']:.3f}")

    # 2. 桥探活 + 3. 账户
    import requests

    try:
        h = requests.get(f"{broker.bridge_url}/api/v1/health", timeout=8)
        health = h.json()
        print(f"🏥 桥健康: status={health.get('status')} tdx_connected={health.get('tdx_connected')}")
    except Exception as e:
        print(f"❌ 桥不可达: {e}")
        sys.exit(1)
    acct = broker._account_query()
    asset = acct.get("asset") or {}
    cash = float(asset.get("cash") or 0)
    print(f"💰 可用资金 ¥{cash:,.2f}  |  持仓 {len(acct.get('positions') or [])} 只")
    if cash <= 0:
        print("❌ 无可用资金，终止")
        sys.exit(1)

    # 4. 行情：优先 TdxAiData 实时源（批量一次拿全部候选，省限流配额），失败逐票回退桥
    from agent_tools.datasources import tdx_aidata

    bars_map: dict = {}
    codes = [p["code"] for p in picks]
    if tdx_aidata.available():
        try:
            bars_map = tdx_aidata.get_klines_batch(codes, interval="daily", count=5)
            got = sum(1 for b in bars_map.values() if b)
            print(f"📡 实时行情(TdxAiData) {got}/{len(codes)} 只")
            if got == 0:
                bars_map = {}  # 整体空（限流）→ 全部回退桥
        except Exception as e:
            print(f"⚠️  TdxAiData 批量行情失败: {e} → 回退桥行情")
            bars_map = {}
    else:
        print("⚠️  TdxAiData 不可用 → 回退桥行情")
    for p in picks:  # 空/缺失的单只回退桥
        code = p["code"]
        if bars_map.get(code):
            continue
        try:
            bars_map[code] = latest_klines(broker, code)
        except Exception as e:
            bars_map[code] = []
            log_line({"ts": now_cn().isoformat(), "code": code, "name": p["name"],
                      "stage": "quote", "error": str(e)})

    # 5. 逐票下单 —— 实盘分账：候选按 score 轮流分配给各 agent，
    #    每 agent 用自己剩余额度（¥10 万 - 已用）买，累计不超；账户现金兜底。
    agents = enabled_agents()
    ledger = load_ledger()
    print(f"📒 分账 agent {len(agents)} 个（每 agent ¥{AGENT_QUOTA:,.0f} 额度）:")
    for a in agents:
        print(f"   {a}: 已用 ¥{agent_used(ledger, a):,.0f} 剩余 ¥{agent_remaining(ledger, a):,.0f}")
    placed = []
    planned_cost = 0.0  # dry-run 也累计，现金兜底在演练时同样生效
    from live_fills import wait_fill   # 记账口径：下单≠成交（见下单段）

    for i, p in enumerate(picks):
        agent = agents[i % len(agents)]
        code, name = p["code"], p["name"]
        remaining = agent_remaining(ledger, agent)
        if remaining < 100:
            print(f"⏭️  [{agent}] {code} {name}: 额度已用完（剩余 ¥{remaining:,.0f}），跳过")
            continue
        bars = bars_map.get(code) or []
        try:
            o = compute_order(bars, remaining, args.per_stock_pct, code)
        except Exception as e:
            print(f"⚠️  [{agent}] {code} {name} 行情失败: {e}")
            log_line({"ts": now_cn().isoformat(), "code": code, "name": name,
                      "stage": "quote", "error": str(e)})
            continue
        if not o["ok"]:
            print(f"⏭️  [{agent}] {code} {name}: {o['reason']}")
            continue
        if o["cost"] > remaining + 0.01:
            print(f"⏭️  [{agent}] {code} {name}: 需 ¥{o['cost']:,.0f} 超剩余额度 ¥{remaining:,.0f}，跳过")
            continue
        if o["cost"] + planned_cost > cash:
            print(f"⏭️  [{agent}] {code} {name}: 账户现金不足（拟买 ¥{o['cost']:,.0f}"
                  f" + 已计划 ¥{planned_cost:,.0f} > 现金 ¥{cash:,.0f}），跳过")
            continue
        planned_cost += o["cost"]
        print(f"📈 [{agent}] {code} {name}: 现价 ¥{o['price']:.2f} ({o['chg_pct']:+.2f}%)"
              f" 拟买 {o['volume']} 股 限价 ¥{o['limit_price']:.2f}"
              f" ≈¥{o['cost']:,.0f}（额度已用 ¥{agent_used(ledger, agent):,.0f}"
              f" → {agent_used(ledger, agent) + o['cost']:,.0f} / ¥{AGENT_QUOTA:,.0f}）")

        if not args.execute:
            continue  # dry-run：只演练

        try:
            result = broker.buy(None, None, code, o["volume"], price=o["limit_price"])
        except Exception as e:
            print(f"❌  [{agent}] {code} {name} 下单失败: {e}")
            log_line({"ts": now_cn().isoformat(), "mode": "execute",
                      "code": code, "name": name, "volume": o["volume"],
                      "price": o["limit_price"], "error": str(e)})
            continue
        from live_fills import ack_line

        print(ack_line(f"[{agent}] {code} {name}", result))
        placed.append(result)
        log_line({"ts": now_cn().isoformat(), "mode": "execute",
                  "code": code, "name": name, "volume": o["volume"],
                  "price": o["limit_price"], "result": result})
        # 分账记账：记到该 agent 名下（券商持仓按股票合并，账本按 agent 分开）。
        # 下单≠成交：等成交回报按真实成交价/量入账，未确认成交挂 pending 由 reconcile 兜底
        # ⚠️ 待办（2026-09-12 审查 MEDIUM-2）：手工路径同 sell_flow——未接 settle_place_fill，
        #    部分成交余量不跟踪、账本写不在 _file_lock 内。
        order_id = str(result.get("order_id") or "")
        fill = wait_fill(broker, order_id)
        fv = int((fill or {}).get("filled_volume") or 0)
        if fv > 0:
            fp = float(fill.get("filled_price") or o["price"])
            ledger = record_buy(ledger, agent, code, fv, fp, now_cn().isoformat())
            save_ledger(ledger)
        else:
            _pend_or_warn(order_id, agent, code, "buy", o["volume"], o["limit_price"])
        time.sleep(1)  # 桥限流 60 req/min

    # 6. 委托验证
    if args.execute and placed:
        print("\n📋 当日委托:")
        for o in broker.get_orders():
            print(f"   {o.get('stock_code')} {o.get('side')} "
                  f"{o.get('total_volume')}股 status={o.get('status')} id={o.get('order_id')}")

    print("\n📒 分账额度（当前账本）:")
    for a in agents:
        print(f"   {a}: 已用 ¥{agent_used(ledger, a):,.0f} / ¥{AGENT_QUOTA:,.0f}"
              f" 剩余 ¥{agent_remaining(ledger, a):,.0f}")

    print(f"\n{'✅ 流程完成' if args.execute else '🟡 演练完成（--execute 真下单）'}"
          f"：共受理 {len(placed)} 笔")
    log_line({"ts": now_cn().isoformat(), "mode": "execute" if args.execute else "dry_run",
              "picks": len(picks), "placed": len(placed), "cash": cash})


if __name__ == "__main__":
    main()
