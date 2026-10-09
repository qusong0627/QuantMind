#!/usr/bin/env python3
"""实盘分账记账本（A股，**薄封装**）。

算法全部收敛在 `scripts/account_protocol.py`（QIFI 式协议层）；
本模块只做"绑定"：台账文件路径 + 额度（¥10 万）+ 本市场 IO + A 股特有的坏价闸。

规则（2026-08-31 用户确认）：
  - 每个 agent 初始额度 AGENT_QUOTA = ¥100,000，累计买入成本不得超过该额度
  - 买入：记录到该 agent 名下，used = Σ volume×cost_price（加仓按加权成本）
  - 卖出：从持有该股票的 agent 名下扣减，释放额度
  - 2026-08-31 已买的 5 只（约 ¥92 万）属于总账户，不入分账
  - 持有同一股票的 agent 用 find_holder 查找（轮候分配下每只只归属一个 agent）

迁移说明见 docs/ACCOUNT_PROTOCOL_PLAN.md：本模块原与 hk/us_ledger 各自复制一份
实现（已实测漂移）。现三市场共用协议层，一致性由 tests/test_account_protocol.py
的 golden 基线 + 三市场一致性断言守住。

用法：
  python scripts/live_ledger.py            # 打印当前分账状态
  python -m pytest tests/test_live_roundtrips.py tests/test_account_protocol.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import account_protocol as _p  # noqa: E402

LEDGER_FILE = Path(__file__).resolve().parent.parent / "logs" / "live_ledger.json"
ROUNDTRIP_LOG = Path(__file__).resolve().parent.parent / "logs" / "live_roundtrips.jsonl"
AGENT_QUOTA = 100_000.0


# ---------- IO（绑定层） ----------
def load_ledger() -> dict:
    """读账本；文件不存在/损坏时返回空账本（不抛异常）。"""
    if not LEDGER_FILE.is_file():
        return {"version": 1, "agents": {}}
    try:
        return json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {"version": 1, "agents": {}}


def save_ledger(ledger: dict) -> None:
    """原子写账本：先写 tmp 再 rename，避免半写文件。

    同时把累积的回合记录落盘（协议层统一实现，三市场共用 live_roundtrips.jsonl，
    靠记录里的 market 字段区分）。**落盘成功才从账本里清掉**，失败留待下次重试。
    """
    ledger = _p.flush_roundtrips(ledger, ROUNDTRIP_LOG)
    LEDGER_FILE.parent.mkdir(exist_ok=True)
    tmp = LEDGER_FILE.with_name(LEDGER_FILE.name + ".tmp")
    tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(LEDGER_FILE)


# ---------- A 股特有：坏价闸 ----------
def sane_fill_price(fp: float, ref: float) -> tuple[float, bool]:
    """成交/行情价护栏：|fp/ref-1| > 40% 视为坏 tick（全市场最大涨跌停 ±30%，
    留余量）。越界返回 (ref, True) 供 approx 记账，正常返回 (fp, False)。
    ref <= 0 时不判断。2026-09-08 实录：001312 桥报成交价 4.789 vs 实时 17.5，
    坏成本入账虚增 pro 虚拟净值 ~1.4 万。"""
    if fp <= 0 or ref <= 0:
        return fp, False
    ratio = fp / ref
    if 0.6 <= ratio <= 1.4:
        return fp, False
    return round(ref, 2), True


# ---------- 协议层委托（本市场只钉额度与市场标识） ----------
_positions = _p.positions
agent_used = _p.agent_used
position_cost = _p.position_cost   # 单票成本（单票集中度闸取数，2026-09-18）
find_holder = _p.find_holder
load_deferred = _p.load_deferred
_holding_days = _p.holding_days
_roundtrip_row = _p.roundtrip_row


def agent_remaining(ledger: dict, agent: str) -> float:
    return _p.agent_remaining(ledger, agent, AGENT_QUOTA)


def agent_virtual_cash(ledger: dict, agent: str) -> float:
    return _p.agent_virtual_cash(ledger, agent, AGENT_QUOTA)


def record_buy(ledger: dict, agent: str, code: str, volume: int,
               cost_price: float, ts: str) -> dict:
    return _p.record_buy(ledger, agent, code, volume, cost_price, ts, quota=AGENT_QUOTA)


def record_sell(ledger: dict, agent: str, code: str, volume: int,
                sell_price: float, ts: str, exit_reason: str | None = None) -> dict:
    return _p.record_sell(ledger, agent, code, volume, sell_price, ts,
                          exit_reason, quota=AGENT_QUOTA, market="A")


def save_deferred(ledger: dict, agent: str, side: str, code: str, volume: int,
                  reason: str, ts: str) -> dict:
    return _p.save_deferred(ledger, agent, side, code, volume, reason, ts)


def clear_deferred(ledger: dict, agent: str, side: str, code: str) -> dict:
    return _p.clear_deferred(ledger, agent, side, code)


# ---------- 延期单登记（A 股断链关键字判定 + IO，属绑定层） ----------
def defer_on_exc(agent: str, side: str, code: str, volume: int,
                 exc: Exception, ts: str) -> bool:
    """桥断链/拒单时登记延期单（命中断链关键字才登记），返回是否登记。

    2026-09-01 事故教训：行情断开时 4 次减仓决策全部被透传拒单且无人重试，
    白白浪费一个交易时段——被拒意图落盘，恢复后由 replay_deferred.py 重放。
    """
    msg = str(exc)
    if not any(k in msg for k in ("断开", "Connection", "refused", "拒绝", "超时")):
        return False
    try:
        save_ledger(save_deferred(load_ledger(), agent, side, code, volume,
                                  msg[:120], ts))
        return True
    except OSError:
        return False


def main() -> None:
    ledger = load_ledger()
    agents = ledger.get("agents") or {}
    if not agents:
        print("📒 分账账本为空（尚无 agent 分账持仓）")
        return
    for agent in sorted(agents):
        used = agent_used(ledger, agent)
        print(f"📒 {agent}: 已用 ¥{used:,.0f} / ¥{AGENT_QUOTA:,.0f}"
              f" 剩余 ¥{agent_remaining(ledger, agent):,.0f}")
        for code, p in sorted(_positions(ledger, agent).items()):
            print(f"   {code} ×{p['volume']} @ {p['cost_price']} = "
                  f"¥{p['volume'] * p['cost_price']:,.0f}")


if __name__ == "__main__":
    main()
