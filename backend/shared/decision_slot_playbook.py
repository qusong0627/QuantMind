"""槽位守则：把“这个时间点知道什么、最多做几笔、什么必须放弃”写进提示词。

只**追加**，不删减现有块。所有结论来自 2016–2026 实测（见
``skills/market-regime-judge``）：

* 10:10 广度 <30% 当日 −0.20%（t−2.6），>60% +0.245%（t4.6）；
* 弱广度 + MCL 收缩 −0.236%（t−2.8）；10:10 是延续信号，不是拐点；
* 强广度日 Q1/Q2（10:10 落后票）收盘跑赢 Q5，弱广度日不接飞刀；
* 隔夜腿长期为负；14:50 的 alpha 只有“限价挂 14:50 价、只买成交票”可讨论，
  市价版本不可实盘。

``stage`` = 上一交易日市场阶段快照（``market_stage_snapshot``），用于把
“防守阶段默认不新开”写进本轮；没有快照时只给通用守则。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

CN_TZ = ZoneInfo("Asia/Shanghai")

#: 每个槽位的“本轮新开建议预算”（提示词约束；硬闸门仍按档位/额度）。
SLOT_NEW_BUY_BUDGET: dict[str, int] = {
    "0830": 0,
    "0900": 0,
    "0935": 1,
    "1000": 1,
    "1005": 1,
    "1015": 2,
    "1100": 1,
    "1105": 1,
    "1200": 0,
    "1300": 1,
    "1400": 1,
    "1445": 0,
}

_SLOT_TASK: dict[str, str] = {
    "0830": "盘前只做计划：列 watchlist、触发价、失效条件；09:15 竞价前不下单。",
    "0900": "盘前最后检查：隔夜外盘/新闻/缺口；09:15 竞价后才评估，不提前下单。",
    "0935": "开盘 5 分钟：只挂限价/观察，不追开盘缺口；等 10:10 广度确认再决定是否加。",
    "1000": "10:10 快照未出：实时热集广度只是代理，不当全市场；最多 1 笔，等 10:10。",
    "1005": "10:10 快照未出：同 10:00；不把热集广度当全市场结论。",
    "1015": "今日 10:10 快照刚出：优先用全市场广度/MCL5 与 Q1/Q2 落后票；强广度才做，弱广度不接刀。",
    "1100": "午前：对比实时广度与 10:10 基线；广度走弱则减，不逆势加。",
    "1105": "午前最后窗口：原则上不新开；只处理持仓与下午计划，避免午休跳空。",
    "1200": "午休无连续行情：只复盘/写下午计划，禁新单；价格以 11:30 收盘为参考。",
    "1300": "午后开盘前 5 分钟：不追开盘缺口；报价 age<2 分钟才定价；最多 1 笔。",
    "1400": "午后趋势：只做强广度且不追高；开始退出弱持仓；今日买入 T+1 不可卖。",
    "1445": "尾盘：0 新开；14:50 后只限价；弱票收盘卖，强票才留隔夜；写次日卖点/止损。",
}

_GENERIC = (
    "只使用已注入且带时点的块；10:10 之前不得引用 10:10；上一交易日阶段不是今日实时。"
)


def _stage_line(stage: Mapping[str, Any] | None) -> str:
    if not stage:
        return "- 阶段：上一交易日快照未取到；新开仓按通用风控，不因“感觉强”破例。"
    score = stage.get("score")
    state = stage.get("state") or "—"
    try:
        score_txt = f"{int(score):+d}"
    except (TypeError, ValueError):
        score_txt = "—"
    try:
        s = int(score)
    except (TypeError, ValueError):
        s = None
    if s is not None and s <= -5:
        rule = "防守阶段：本轮新开默认 0；只有实时广度>60% 且 MCL5>0 双确认才允许破例。"
    elif s is not None and s <= -1:
        rule = "偏空阶段：新开从严，单轮不超过 1 笔，且必须有明确板块/个股催化。"
    elif s is not None and s >= 2:
        rule = "偏多阶段：可按预算执行，但仍只做强势确认后的回踩/突破，不追高。"
    else:
        rule = "震荡阶段：只做确定性高的个股，不因大盘普涨而放宽。"
    return f"- 阶段：上一交易日 {score_txt}/±12（{state}）→ {rule}"


def build_slot_playbook_block(
    slot_hhmm: str,
    *,
    stage: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> str:
    """生成槽位守则块；未知槽位也给通用守则（不静默消失）。"""
    moment = now or datetime.now(CN_TZ)
    key = str(slot_hhmm or "").strip()
    budget = SLOT_NEW_BUY_BUDGET.get(key)
    task = _SLOT_TASK.get(key, "按实时状态与持仓执行；不确定就观察，不为凑交易而交易。")
    budget_txt = f"最多 {budget} 笔" if budget is not None else "按档位预算"
    if key in {"0830", "0900", "1200", "1445"}:
        budget_txt = "0 笔（禁新开）"
    lines = [
        f"【槽位守则｜{key or '未知'} {moment:%H:%M}】",
        f"- 本槽位任务：{task}",
        f"- 本轮新开预算：{budget_txt}；单票 ≤ 剩余额度 20%；优先限价，不追涨停/不追高。",
        "- 风险定额：每笔买入必须给 stop_loss 与 risk_amount（额度×0.3%~0.5%，"
        "20 万≈600~1000）；pct 按 risk_amount/(entry−stop) 反推；执行段按 0.4% "
        "单笔风险预算夹取，超预算会被缩小。",
        _stage_line(stage),
        "- 失效与退出：每笔买入必须带止损/失效条件；到 14:45 仍未走出 → 退出；"
        "弱广度（<30%）+ MCL 收缩 → 不新开。",
        "- 历史负向：首板/连板次日高开追买、弱广度接飞刀——不碰；隔夜只留强票。",
        f"- 数据纪律：{_GENERIC}",
    ]
    return "\n".join(lines)
