#!/usr/bin/env python3
"""候选池 → 通达信桥实盘买卖（人工路径，默认 dry-run）。

买入流程（--mode buy，默认）：
  1. 选股：最新 ``data/reports/stock_picks/*_picks.json``（--source picks，默认）或
     ``*_agent_picks.json``（--source agent，与决策轮同一份池）；--picks-file 可指定
  2. 桥探活：GET /api/v1/health
  3. 账户：POST /api/v1/account/query（可用资金 / 现持仓）
  4. 行情：桥日K（逐票；旧的 TdxAiData 模块已随 quant-Trader 退役，不再依赖）
  5. 计算：涨跌停/停牌过滤 → 板块手数 → 单票 ≤ 策略配置比例（TradingPolicy）
  6. 下单：POST /api/v1/plans/execute
  7. 记账：成交按累计量台阶写 **PG 分账账本**（qm_agent_ledger_*，与决策轮同一本账）
     + logs/live_trade_YYYYMMDD.jsonl 流水 + orders/query 委托验证

卖出流程（--mode sell）：
  1. 账户查询现持仓 → 2. 桥日K（限价=现价×0.99，T+1 可用量检查）
  3. 卖出 → 4. 成交写 PG 分账（持有归属从 PG 账本取）+ 委托验证
  标的：--sell-all 全部持仓 | --sell-codes 600519.SH,000858.SZ 指定 | --sell-pct 0.5 每只卖一半
  提示：当日买入 T+1 不可卖（available_volume=0），桥会拒绝，脚本会跳过

额度与账本（2026-10-30 修）：
  - 额度逐 agent 读 ``config/trading_policy.json``（TradingPolicy），不再有 ¥10 万常量；
  - 文件账本 ``logs/live_ledger.json`` 自 2026-09-30 起冻结，本脚本**只读 PG 活账**；
  - 手工成交按累计量台阶入 PG 分账；未终态/超时的余量不自动跟踪，必须人工核对。

安全：
  - 默认 --dry-run：只演练（选股/资金/价格/拟下量），不下单
  - --execute 才真下
  - 账本/策略不可读 → 直接退出，不用旧账本或默认额度兜底

用法：
  python scripts/live_trade_picks.py                            # buy dry-run
  python scripts/live_trade_picks.py --source agent --top 5     # 决策池个股 dry-run
  python scripts/live_trade_picks.py --execute --source picks   # 实盘买入（夜间池）
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
from live_ledger_pg import (  # noqa: E402
    agent_quota,
    agent_remaining,
    agent_used,
    ensure_pg_runtime,
    holders_by_age,
    load_env as load_pg_env,
    load_ledger,
    load_policy,
    settle_fill,
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


def sell_flow(broker, args, log_line) -> int:
    """卖出流程：桥持仓 → 桥日K → 限价卖出；成交写 PG 分账。

    标的：--sell-all（默认）全部持仓 | --sell-codes 指定；
    每只可卖量 × --sell-pct，板块手数取整；跌停/停牌跳过；T+1 当日买入不可卖
    （available=0 跳过）。持有归属从 PG 活账取；多家持有时提示后按第一顺位记。
    """
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
    try:
        policy = load_policy()
        ledger = load_ledger()
    except Exception as exc:  # noqa: BLE001 账本/策略读不到就不许按猜的归属记账
        print(f"❌ 分账账本/策略不可读：{type(exc).__name__}: {exc}")
        print("   本工具不再回退文件账本（logs/live_ledger.json 自 2026-09-30 已冻结）")
        return 2
    print("📒 PG 分账（额度按 TradingPolicy）：")
    for agent in enabled_agents():
        print(
            f"   {agent}: 已用 ¥{agent_used(ledger, agent):,.0f}"
            f" / ¥{agent_quota(policy, agent):,.0f}"
            f" 剩余 ¥{agent_remaining(ledger, agent, policy):,.0f}"
        )
    # 在途卖单提示（不拦截，人工路径以操作员意图为准）：自动路径（哨兵/整点轮）
    # 可能刚下过同一代码的止损单，再下一单柜台会按可用量驳回或造成超卖
    from live_fills import inflight_codes

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

    # 行情：桥日K（旧的 TdxAiData 数据源模块已随 quant-Trader 退役；
    # 缺失时不再 import 失败，桥本身就是行情面）
    bars_map: dict = {}
    for t in targets:
        c = t.get("stock_code")
        try:
            bars_map[c] = broker.get_klines(c, interval="daily")[-5:]
        except Exception:
            bars_map[c] = []

    sold = []
    record_errors: list[str] = []
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
        allocations = holders_by_age(ledger, code)
        if allocations:
            plan_text = " + ".join(f"{a} {v}股" for a, v in allocations)
            print(f"📉 {code}: 现价 ¥{price:.2f} ({chg:+.2f}%) 拟卖 {vol}/{avail} 股"
                  f" 限价 ¥{limit:.2f} · 按 FIFO 减仓 [{plan_text}]")
            if len(allocations) > 1:
                print(f"   ℹ️  {code}: 共享账户 {len(allocations)} 家 agent 持有——"
                      f"本笔按买入时间 FIFO 逐家扣减")
        else:
            print(f"📉 {code}: 现价 ¥{price:.2f} ({chg:+.2f}%) 拟卖 {vol}/{avail} 股"
                  f" 限价 ¥{limit:.2f} · 无分账 agent")
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
        # 分账记账（PG 活账）：下单≠成交——成交按桥回报的累计量台阶入账；
        # 没成交/未终态如实打印，不拿委托限价预扣（2026-09-11 前的老毛病）。
        order_id = str(result.get("order_id") or "")
        if not allocations:
            print(f"   ℹ️  {code}: 无分账 agent 持有该代码，跳过记账（仅桥账户变动）")
        elif not order_id:
            print(f"   ⚠️  {code}: 桥未返回委托号——成交无法跟踪，未记分账；请人工核对当日委托")
            record_errors.append(f"{code}: 无委托号")
        else:
            settlement = settle_fill(
                broker, allocations=allocations, code=code, side="sell",
                order_id=order_id, limit_price=limit, timeout_s=args.fill_timeout,
            )
            if settlement["filled"] > 0:
                parts = " / ".join(
                    f"{agent} -{qty}股"
                    for agent, qty in settlement["by_agent"].items()
                )
                print(f"   📒 分账释放: {parts} {code}"
                      f" 均价 ¥{settlement['price']:.2f}")
                ledger = load_ledger()
                if not settlement["terminal"]:
                    print(f"   ⚠️  {code}: 委托未终态（{settlement['status'] or '—'}），"
                          f"余量未跟踪；请核对当日委托")
                    log_line({"ts": now_cn().isoformat(), "mode": "fill_unfinished",
                              "agent": ",".join(settlement["by_agent"]), "code": code,
                              "side": "sell", "filled": settlement["filled"],
                              "status": settlement["status"]})
            else:
                print(f"   ⏳ {code}: 尚无成交（{settlement['status'] or '无终态'}）——未记分账")
            if settlement["error"]:
                print(f"   ❌ {settlement['error']}")
                record_errors.append(f"{code}: {settlement['error']}")
        time.sleep(1)  # 桥限流 60 req/min

    if args.execute and sold:
        print("\n📋 当日委托:")
        for o in broker.get_orders():
            print(f"   {o.get('stock_code')} {o.get('side')} "
                  f"{o.get('total_volume')}股 status={o.get('status')} id={o.get('order_id')}")
    print(f"\n{'✅ 卖出完成' if args.execute else '🟡 卖出演练完成'}: 受理 {len(sold)} 笔")
    if record_errors:
        print(f"\n❌ 有 {len(record_errors)} 笔成交未完整入分账（请人工核对）：")
        for item in record_errors:
            print(f"   - {item}")
        return 2
    return 0


_PICKS_DIR = Path(__file__).resolve().parent.parent / "data" / "reports" / "stock_picks"
_SIDE_RANK = {"BUY": 3, "ADD": 2, "HOLD": 1}


def load_picks(source: str, top: int, min_side: str, picks_file: str = "") -> list[dict]:
    """候选读取：默认夜间池 ``*_picks.json``；``agent`` = 决策轮同一份池。

    两种 schema 归一成 ``{code(后缀式), name, side, score}``：
    - 夜间池：``{"candidates": [{"symbol": "688687", ...}]}``
    - 决策池：``{"picks": [{"code": "003816.SZ", ...}]}``

    ``--source md`` 已无实现（``select_from_reports.py`` 随 quant-Trader 退役）。
    """
    if source == "md":
        raise SystemExit(
            "❌ --source md 依赖的 select_from_reports.py 已随 quant-Trader 退役；"
            "请用 --source picks / --source agent，或 --picks-file 指定 JSON"
        )
    if picks_file:
        path = Path(picks_file)
        if not path.is_file():
            raise SystemExit(f"❌ 候选文件不存在：{path}")
    else:
        pattern = "*_agent_picks.json" if source == "agent" else "*_picks.json"
        if source == "agent":
            files = sorted(
                _PICKS_DIR.glob("*_agent_picks.json"),
                key=lambda p: (p.stat().st_mtime, p.name),
            )
        else:
            # 夜间池：排除 *_agent_picks.json（决策池的 glob 也命中 *_picks.json）
            files = sorted(
                (
                    p
                    for p in _PICKS_DIR.glob("*_picks.json")
                    if not p.name.endswith("_agent_picks.json")
                ),
                key=lambda p: (p.stat().st_mtime, p.name),
            )
        if not files:
            raise SystemExit(f"❌ 找不到候选文件：{_PICKS_DIR}/{pattern}")
        path = files[-1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"❌ 候选文件不可读：{path}（{exc}）") from exc
    raw = (
        data.get("picks") or data.get("candidates") or []
        if isinstance(data, dict)
        else data
    )
    from backend.shared.stock_utils import StockCodeUtil

    rank = _SIDE_RANK.get(str(min_side).upper(), 3)
    rows: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or item.get("symbol") or "").strip()
        if not code:
            continue
        side = str(item.get("side") or "HOLD").upper()
        if _SIDE_RANK.get(side, 0) < rank:
            continue
        try:
            score = float(item.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        rows.append(
            {
                "code": StockCodeUtil.to_suffix(code),
                "name": str(item.get("name") or ""),
                "side": side,
                "score": score,
            }
        )
    rows.sort(key=lambda row: row["score"], reverse=True)
    print(f"📄 候选来源：{path}（{len(rows)} 只，side≥{min_side}）")
    return rows[: max(int(top), 1)]


def main() -> None:
    ap = argparse.ArgumentParser(description="候选池 → 通达信桥实盘下单（默认 dry-run）")
    ap.add_argument("--execute", action="store_true", help="真下单（默认仅 dry-run 演练）")
    ap.add_argument(
        "--source",
        choices=["picks", "agent", "md"],
        default="picks",
        help="picks=夜间池（默认）；agent=决策轮同一份池；md=已退役",
    )
    ap.add_argument("--picks-file", default="", help="显式指定 picks JSON（覆盖 --source）")
    ap.add_argument("--top", type=int, default=5, help="取前 N 只候选")
    ap.add_argument(
        "--per-stock-pct",
        type=float,
        default=None,
        help="单票占剩余额度比例（缺省=该 agent 的 TradingPolicy.per_stock_pct）",
    )
    ap.add_argument("--min-side", choices=["BUY", "ADD", "HOLD"], default="BUY",
                    help="最低 side 门槛（默认 BUY：只买推荐买入的）")
    ap.add_argument("--mode", choices=["buy", "sell"], default="buy",
                    help="buy=买入（默认）；sell=卖出持仓")
    ap.add_argument("--sell-all", action="store_true", help="卖出全部持仓")
    ap.add_argument("--sell-codes", help="卖出指定股票（逗号分隔：600519.SH,000858.SZ）")
    ap.add_argument("--sell-pct", type=float, default=1.0,
                    help="每只卖出比例（默认 1.0=全部可卖量；按板块手数取整，"
                         "取整后不足 1 手的标的跳过）")
    ap.add_argument("--fill-timeout", type=int, default=60,
                    help="成交回报轮询上限（秒）；超时未终态会如实告警（默认 60）")
    args = ap.parse_args()

    # PG 分账是唯一账本：解释器/环境先就位，失败即退出（不回退文件账本）。
    ensure_pg_runtime()
    load_pg_env()

    mode = "🔴 实盘执行" if args.execute else "🟡 DRY-RUN 演练（不下单）"
    print(f"{mode}  {now_cn():%F %T}  [mode={args.mode}]")

    broker = TdxBridgeBroker()
    if args.mode == "sell":
        raise SystemExit(sell_flow(broker, args, log_line))

    # 1. 选股（仅买入模式）
    picks = load_picks(args.source, args.top, args.min_side, args.picks_file)
    if not picks:
        print("❌ 无候选（side 门槛未命中），流程终止")
        raise SystemExit(0)
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

    # 4. 行情：桥日K（旧的 TdxAiData 数据源模块已随 quant-Trader 退役）
    bars_map: dict = {}
    for p in picks:
        code = p["code"]
        try:
            bars_map[code] = latest_klines(broker, code)
        except Exception as e:
            bars_map[code] = []
            log_line({"ts": now_cn().isoformat(), "code": code, "name": p["name"],
                      "stage": "quote", "error": str(e)})

    # 5. 逐票下单 —— 实盘分账：候选按 score 轮流分配给各 agent，
    #    每 agent 用自己剩余额度（TradingPolicy 授予额度 − 已用，且不超过账上现金）。
    agents = enabled_agents()
    try:
        policy = load_policy()
        ledger = load_ledger()
    except Exception as exc:  # noqa: BLE001 账本/策略读不到就不许按猜的额度下单
        print(f"❌ 分账账本/策略不可读：{type(exc).__name__}: {exc}")
        print("   本工具不再回退文件账本（logs/live_ledger.json 自 2026-09-30 已冻结）")
        raise SystemExit(2) from exc
    print(f"📒 分账 agent {len(agents)} 个（额度按 TradingPolicy）：")
    for a in agents:
        print(f"   {a}: 已用 ¥{agent_used(ledger, a):,.0f}"
              f" / ¥{agent_quota(policy, a):,.0f}"
              f" 剩余 ¥{agent_remaining(ledger, a, policy):,.0f}")
    placed = []
    planned_cost = 0.0  # dry-run 也累计，现金兜底在演练时同样生效
    record_errors: list[str] = []

    for i, p in enumerate(picks):
        agent = agents[i % len(agents)]
        code, name = p["code"], p["name"]
        remaining = agent_remaining(ledger, agent, policy)
        if remaining < 100:
            print(f"⏭️  [{agent}] {code} {name}: 额度已用完（剩余 ¥{remaining:,.0f}），跳过")
            continue
        pct = (
            args.per_stock_pct
            if args.per_stock_pct is not None
            else policy.for_agent(agent).per_stock_pct
        )
        bars = bars_map.get(code) or []
        try:
            o = compute_order(bars, remaining, pct, code)
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
              f" → {agent_used(ledger, agent) + o['cost']:,.0f} / ¥{agent_quota(policy, agent):,.0f}）")

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
        # 分账记账（PG 活账）：记到该 agent 名下（券商持仓按股票合并，账本按 agent 分开）。
        # 下单≠成交：只按桥回报的累计成交量台阶入账；未成交/未终态如实告警，不预扣。
        order_id = str(result.get("order_id") or "")
        if not order_id:
            print(f"   ⚠️  [{agent}] {code}: 桥未返回委托号——成交无法跟踪，未记分账；"
                  f"请人工核对当日委托")
            record_errors.append(f"{code}: 无委托号")
        else:
            settlement = settle_fill(
                broker,
                allocations=[(agent, int(o["volume"]))],
                code=code,
                side="buy",
                order_id=order_id,
                limit_price=o["limit_price"],
                timeout_s=args.fill_timeout,
            )
            if settlement["filled"] > 0:
                print(f"   📒 分账入账: {agent} +{settlement['filled']}股 {code}"
                      f" 均价 ¥{settlement['price']:.2f}")
                ledger = load_ledger()  # 下一只票按最新账本算剩余额度
                if not settlement["terminal"]:
                    print(f"   ⚠️  {code}: 委托未终态（{settlement['status'] or '—'}），"
                          f"余量未跟踪；请核对当日委托")
                    log_line({"ts": now_cn().isoformat(), "mode": "fill_unfinished",
                              "agent": agent, "code": code, "side": "buy",
                              "filled": settlement["filled"],
                              "status": settlement["status"]})
            else:
                print(f"   ⏳ {code}: 尚无成交（{settlement['status'] or '无终态'}）——未记分账")
            if settlement["error"]:
                print(f"   ❌ {settlement['error']}")
                record_errors.append(f"{code}: {settlement['error']}")
        time.sleep(1)  # 桥限流 60 req/min

    # 6. 委托验证
    if args.execute and placed:
        print("\n📋 当日委托:")
        for o in broker.get_orders():
            print(f"   {o.get('stock_code')} {o.get('side')} "
                  f"{o.get('total_volume')}股 status={o.get('status')} id={o.get('order_id')}")

    try:
        ledger = load_ledger()
    except Exception as exc:  # noqa: BLE001 复读失败不覆盖本轮已打印的结果
        print(f"\n⚠️ 账本复读失败（{type(exc).__name__}: {exc}）：以下为最后一次读到的账本")
    print("\n📒 分账额度（PG 活账）:")
    for a in agents:
        print(f"   {a}: 已用 ¥{agent_used(ledger, a):,.0f}"
              f" / ¥{agent_quota(policy, a):,.0f}"
              f" 剩余 ¥{agent_remaining(ledger, a, policy):,.0f}")

    print(f"\n{'✅ 流程完成' if args.execute else '🟡 演练完成（--execute 真下单）'}"
          f"：共受理 {len(placed)} 笔")
    log_line({"ts": now_cn().isoformat(), "mode": "execute" if args.execute else "dry_run",
              "picks": len(picks), "placed": len(placed), "cash": cash})
    if record_errors:
        print(f"\n❌ 有 {len(record_errors)} 笔成交未完整入分账（请人工核对）：")
        for item in record_errors:
            print(f"   - {item}")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
