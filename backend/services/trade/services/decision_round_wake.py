"""决策轮**唤醒评估器**（Stage 2 第二批·丙1 四类触发 + 丙2 日预算护栏）——纯逻辑。

细案 ``docs/arena总控回路回归_细案_20261014.md``「Stage 2 第二批」：上游总控在
整点槽位之外还有一层**事件驱动**的唤醒评估（波动/板块/L2/新闻四类触发器，
``live_hourly_analysis.py:1482-1562``）——盘中异动随时把控制权叫醒，而不是等到
下一个整点。本模块是那层评估器的**纯函数**半边：阈值、判据、冷却、预算、状态
编解码；取数（持仓/快照/新闻文件/Redis）与执行（合成槽位走 ``round_tick``）在
``decision_round_tick.wake_tick`` 与 ``decision_round_blocks``。

三处与上游的既定口径差异（细案「要推翻清单」6–9，落码时如实标注、不许静默）：

1. **L2 触发=五档失衡代理**：QM 无盘中逐笔 L2 源，用快照五档失衡
   ``imb5=(Σbidv−Σaskv)/(Σbidv+Σaskv)`` 相对上次轮次的 Δ≥0.3 触发，detail 里
   标注「五档代理」。
2. **板块触发=门控**：QM 无盘中板块涨幅源。判据函数照建（:func:`sector_triggers`
   可单测），调用方传 ``None`` 源 → 恒空；评估器每日首评在状态键里留一次可见
   note（不静默）。
3. **基线锚 = 上次轮次**：波动/L2 的基线取「上次轮次完成后的首评值」——逐次
   60 秒两两对比会漏慢漂移（每分钟涨 1pp，任何单次差值都到不了 3pp）。轮次一
   出现（LOG 键新条目）基线即重置为当时值。

护栏（丙2 提前同批，上游当日 78 轮翻车即无护栏）：距上次**任何**轮不足
``MIN_ROUND_GAP_MIN`` 不评；同码同向 ``SAME_DIR_COOLDOWN_MIN`` 冷却；唤醒轮
``DAILY_WAKE_LIMIT``/日封顶。本模块不 import redis / 不碰文件——状态进出的
编解码纯函数在此，读写方在 tick/blocks。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from backend.shared.env_flags import env_flag

logger = logging.getLogger(__name__)

# ── 阈值（移植上游 ``live_hourly_analysis.py:49-74``，常量同名同值） ──────

#: 持仓浮盈相对「上次轮次基线」的变动阈值（百分点）。
TRIGGER_PNL_PP = 3.0
#: 当日涨跌幅绝对值阈值（%）。
TRIGGER_DAY_CHG_PCT = 5.0
#: 五档失衡相对基线的变动阈值（L2 代理口径）。
TRIGGER_IMB_DELTA = 0.3
#: 新闻 impact 触发阈值（仅持仓命中）。
TRIGGER_NEWS_IMPACT = 1.0
#: 板块触发阈值（相对上次基线的 pp / 绝对 %）——当前无源，判据备用。
TRIGGER_SECTOR_PP = 3.0
TRIGGER_SECTOR_ABS = 5.0

#: 距上次**任何**轮（含整点槽位）不足该分钟数 → 不评（节流）。
MIN_ROUND_GAP_MIN = 20.0
#: 同码同向冷却（分钟）。
SAME_DIR_COOLDOWN_MIN = 60.0
#: 唤醒轮日预算（只拦唤醒触发轮；整点/补跑不受限）。
DAILY_WAKE_LIMIT = 20
#: 评估器最小评估间隔（秒）——runner 循环 30s，评估 60s 一次足够。
WAKE_EVAL_MIN_INTERVAL_S = 60.0
#: 新闻新鲜窗（分钟）：超过则该简报不算「新」（8h，与上游一致）。
NEWS_FRESH_MIN = 480.0

#: 状态键（trade 库 db2 原生；按日）与每轮唤醒上下文键（按 round_id）。
WAKE_STATE_KEY = "trade:decision-round:wake:state:{day}"
WAKE_CTX_KEY = "trade:decision-round:wake:ctx:{round_id}"
STATE_TTL_S = 2 * 86400
CTX_TTL_S = 6 * 3600

#: 触发种类中文名（渲染与审计共用）。
KIND_ZH: dict[str, str] = {
    "volatility": "波动",
    "book": "盘口",
    "news": "新闻",
    "sector": "板块",
}


def wake_enabled() -> bool:
    """唤醒评估器总开关（env ``QM_DECISION_WAKE_ENABLED``，未设 = 开）。

    读法走 :func:`backend.shared.env_flags.env_flag`（唯一实现，只有小写
    ``true`` 判开——``1``/``yes``/``on`` 按该口径判**关**，别照宽松词表改回）。
    """
    return env_flag("QM_DECISION_WAKE_ENABLED", default=True)


# ── 观测与触发（纯数据） ─────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class NewsHit:
    """简报中命中持仓的一条（``news_brief/latest.json`` 的 holdings/decisions）。"""

    code: str
    name: str = ""
    verdict: str = ""
    impact: float | None = None
    event_type: str = ""
    headline: str = ""
    decision: str = ""
    pre5: float | None = None
    layer: int = 0
    decision_id: str = ""


@dataclass(frozen=True, slots=True)
class WakeObs:
    """一次评估的观测快照（取数在 blocks 层，本模块只算）。

    ``pnl_pct`` = 快照现价对成本；``day_chg`` = 快照当日涨跌；``imb5`` = 五档
    失衡（指数/坏值 → ``None``）。``brief_ts`` = 简报文件 ts 的 epoch（0=无），
    ``news_hits`` = 简报 holdings ∩ 持仓。
    """

    codes: tuple[str, ...] = ()
    names: Mapping[str, str] = field(default_factory=dict)
    pnl_pct: Mapping[str, float | None] = field(default_factory=dict)
    day_chg: Mapping[str, float | None] = field(default_factory=dict)
    imb5: Mapping[str, float | None] = field(default_factory=dict)
    brief_ts: float = 0.0
    news_hits: tuple[NewsHit, ...] = ()


@dataclass(frozen=True, slots=True)
class WakeTrigger:
    """一条触发明细（kind ∈ :data:`KIND_ZH`）。``direction`` ∈ {"up","down",""}。"""

    kind: str
    code: str = ""
    name: str = ""
    direction: str = ""
    detail: str = ""
    #: P0 新闻优先：不占日唤醒预算，冷却缩短到 5 分钟。
    priority: bool = False

    def label(self) -> str:
        zh = KIND_ZH.get(self.kind, self.kind)
        who = f" {self.name}({self.code})" if self.code else ""
        return f"{zh}{who}"


# ── 状态编解码（读写方：tick 原生客户端 / 测试替身） ─────────────────


@dataclass(frozen=True, slots=True)
class WakeState:
    """唤醒状态（按日）。``baselines`` = {code: {"pnl","chg","imb5"}}；异常一律置空。"""

    day: str = ""
    last_eval_ts: float = 0.0
    last_round_ts: float = 0.0
    wake_count: int = 0
    news_ts: float = 0.0
    baselines: Mapping[str, Mapping[str, float | None]] = field(default_factory=dict)
    #: "code|direction" → epoch 秒（冷却到点）。
    cooldowns: Mapping[str, float] = field(default_factory=dict)
    #: 最近一次的通知性留痕（板块无源 / 预算用尽 / 抑制说明），运营在状态键可见。
    last_note: str = ""
    #: 板块无源留痕的日期（每日只记一次）。
    sector_gap_day: str = ""


def _num(value: Any) -> float | None:
    """JSON/字符串 → float；坏值一律 ``None``（口径同 decision_blocks._f）。"""
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _json_obj(raw: Any) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
        except ValueError:
            return {}
        return value if isinstance(value, Mapping) else {}
    return {}


def decode_state(raw: Mapping[str, Any] | None, *, day: str) -> WakeState:
    """状态 Hash（全字符串）→ :class:`WakeState`；坏 JSON/坏字段按默认值，不炸。"""
    info = raw or {}
    baselines: dict[str, dict[str, float | None]] = {}
    for code, entry in _json_obj(info.get("baselines")).items():
        if not isinstance(entry, Mapping):
            continue
        baselines[str(code)] = {
            "pnl": _num(entry.get("pnl")),
            "chg": _num(entry.get("chg")),
            "imb5": _num(entry.get("imb5")),
        }
    cooldowns: dict[str, float] = {}
    for key, ts in _json_obj(info.get("cooldowns")).items():
        moment = _num(ts)
        if moment is not None:
            cooldowns[str(key)] = moment
    count = _num(info.get("wake_count"))
    return WakeState(
        day=day,
        last_eval_ts=_num(info.get("last_eval_ts")) or 0.0,
        last_round_ts=_num(info.get("last_round_ts")) or 0.0,
        wake_count=int(count) if count is not None else 0,
        news_ts=_num(info.get("news_ts")) or 0.0,
        baselines=baselines,
        cooldowns=cooldowns,
        last_note=str(info.get("last_note") or ""),
        sector_gap_day=str(info.get("sector_gap_day") or ""),
    )


def encode_state(state: WakeState) -> dict[str, str]:
    """→ 状态 Hash 字段（全字符串；baselines/cooldowns 为 JSON）。

    冷却表**保留全部条目**（TTL 由状态键按日兜底）：同一交易日内条目数量级 =
    触发次数，封顶 20 轮/日，无膨胀风险。
    """
    return {
        "day": state.day,
        "last_eval_ts": f"{state.last_eval_ts:.0f}",
        "last_round_ts": f"{state.last_round_ts:.0f}",
        "wake_count": str(int(state.wake_count)),
        "news_ts": f"{state.news_ts:.0f}",
        "baselines": json.dumps(state.baselines, ensure_ascii=False),
        "cooldowns": json.dumps(state.cooldowns, ensure_ascii=False),
        "last_note": state.last_note,
        "sector_gap_day": state.sector_gap_day,
    }


# ── 纯判据 ───────────────────────────────────────────────────────────


def snapshot_pnl(cost: float | None, now_price: float | None) -> float | None:
    """(成本, 现价) → 浮盈 %；任一缺失/成本 ≤0/非有限 → ``None``（不编数）。"""
    if cost is None or now_price is None or cost <= 0 or now_price <= 0:
        return None
    return round((now_price - cost) / cost * 100.0, 4)


def volatility_triggers(obs: WakeObs, state: WakeState) -> tuple[WakeTrigger, ...]:
    """波动类：浮盈相对基线 |Δ|≥3pp 或当日 |涨跌|≥5%（每股至多一条）。

    浮盈优先：两条同时命中时 detail 带上当日涨跌，避免同股两条触发。
    """
    out: list[WakeTrigger] = []
    for code in obs.codes:
        name = str(obs.names.get(code) or code)
        base = state.baselines.get(code) or {}
        pnl = obs.pnl_pct.get(code)
        chg = obs.day_chg.get(code)
        base_pnl = _num(base.get("pnl")) if base else None
        detail = ""
        direction = ""
        if pnl is not None and base_pnl is not None:
            delta = pnl - base_pnl
            if abs(delta) >= TRIGGER_PNL_PP:
                direction = "up" if delta > 0 else "down"
                detail = f"浮盈 {base_pnl:+.2f}% → {pnl:+.2f}%（Δ{delta:+.2f}pp）"
        if not detail and chg is not None and abs(chg) >= TRIGGER_DAY_CHG_PCT:
            direction = "up" if chg > 0 else "down"
            detail = f"今日 {chg:+.2f}%（阈值 ±{TRIGGER_DAY_CHG_PCT:.0f}%）"
        if not detail:
            continue
        if chg is not None and "今日" not in detail:
            detail += f"；今日 {chg:+.2f}%"
        out.append(
            WakeTrigger(
                kind="volatility",
                code=code,
                name=name,
                direction=direction,
                detail=detail,
            )
        )
    return tuple(out)


def book_triggers(obs: WakeObs, state: WakeState) -> tuple[WakeTrigger, ...]:
    """盘口类（L2 代理口径）：五档失衡相对基线 |Δ|≥0.3。无基线（首评）不触发。"""
    out: list[WakeTrigger] = []
    for code in obs.codes:
        cur = obs.imb5.get(code)
        base = (state.baselines.get(code) or {}).get("imb5")
        base_v = _num(base)
        if cur is None or base_v is None:
            continue
        delta = cur - base_v
        if abs(delta) < TRIGGER_IMB_DELTA:
            continue
        name = str(obs.names.get(code) or code)
        out.append(
            WakeTrigger(
                kind="book",
                code=code,
                name=name,
                direction="up" if delta > 0 else "down",
                detail=(
                    f"五档失衡 {base_v:+.2f} → {cur:+.2f}（Δ{delta:+.2f}，"
                    "五档代理：QM 无盘中逐笔 L2 源）"
                ),
            )
        )
    return tuple(out)


def news_triggers(obs: WakeObs, state: WakeState) -> tuple[WakeTrigger, ...]:
    """新闻类：简报 ts 变新 且命中持仓。

    P0 必触发（priority=True，不占预算）；P1 触发；P2/WATCH/INFO 不触发。
    没有 decision 的旧简报仍按 |impact|≥1.0 兼容。
    """
    if obs.brief_ts <= state.news_ts:
        return ()
    out: list[WakeTrigger] = []
    for hit in obs.news_hits:
        direction = (
            "up" if hit.verdict == "利好" else "down" if hit.verdict == "利空" else ""
        )
        if hit.decision == "P0":
            pre5 = "" if hit.pre5 is None else f" pre5 {hit.pre5 * 100:+.1f}%"
            out.append(
                WakeTrigger(
                    kind="news",
                    code=hit.code,
                    name=hit.name,
                    direction=direction or "down",
                    detail=f"[P0] {hit.event_type}：{hit.headline}{pre5} L{hit.layer}",
                    priority=True,
                )
            )
            continue
        if hit.decision == "P1":
            out.append(
                WakeTrigger(
                    kind="news",
                    code=hit.code,
                    name=hit.name,
                    direction=direction or "down",
                    detail=f"[P1] {hit.event_type}：{hit.headline}",
                )
            )
            continue
        if hit.decision in ("P2", "WATCH", "INFO"):
            continue
        impact = hit.impact
        if impact is None or abs(impact) < TRIGGER_NEWS_IMPACT:
            continue
        out.append(
            WakeTrigger(
                kind="news",
                code=hit.code,
                name=hit.name,
                direction=direction,
                detail=f"[{hit.verdict or '情报'}] {hit.event_type}：{hit.headline}",
            )
        )
    return tuple(out)


def sector_triggers(
    sector_chg: Mapping[str, float] | None,
    position_sectors: Mapping[str, str] | None,
) -> tuple[WakeTrigger, ...]:
    """板块类判据（移植上游口径；**当前无源 → 调用方传 ``None``**，恒空）。

    ``sector_chg`` = {板块名: 当日涨跌 %}；``position_sectors`` = {持仓码: 板块}。
    启用路径见细案「要推翻清单 7」：板块指数（880xxx）进热集常驻源 + 持仓→板块
    归属日缓存接上后，wake_tick 换传两份真实映射即可，判据无需改。
    """
    if not sector_chg or not position_sectors:
        return ()
    out: list[WakeTrigger] = []
    seen: set[str] = set()
    for sector in position_sectors.values():
        if sector in seen:
            continue
        chg = sector_chg.get(sector)
        if chg is None:
            continue
        if abs(chg) >= TRIGGER_SECTOR_ABS:
            seen.add(sector)
            out.append(
                WakeTrigger(
                    kind="sector",
                    code="",
                    name=sector,
                    direction="up" if chg > 0 else "down",
                    detail=f"板块 {chg:+.2f}%（阈值 ±{TRIGGER_SECTOR_ABS:.0f}%）",
                )
            )
    return tuple(out)


def evaluate_triggers(
    obs: WakeObs, state: WakeState, now: datetime
) -> tuple[WakeTrigger, ...]:
    """四类触发合并（板块门控恒空）。``now`` 仅为接口稳定（时间判定都在调用方）。"""
    _ = now
    return (
        volatility_triggers(obs, state)
        + book_triggers(obs, state)
        + news_triggers(obs, state)
        + sector_triggers(None, None)
    )


def cooldown_key(trigger: WakeTrigger) -> str:
    """同码同向冷却键（无码触发按 kind 冷却）。"""
    if trigger.code:
        return f"{trigger.code}|{trigger.direction}"
    return f"{trigger.kind}|{trigger.direction}"


def filter_triggers(
    triggers: Sequence[WakeTrigger], state: WakeState, now: datetime
) -> tuple[tuple[WakeTrigger, ...], tuple[WakeTrigger, ...]]:
    """冷却过滤 → ``(可触发, 被抑制)``。冷却窗内同码同向不重复唤醒。"""
    now_s = now.timestamp()
    fire: list[WakeTrigger] = []
    held: list[WakeTrigger] = []
    for t in triggers:
        until = state.cooldowns.get(cooldown_key(t), 0.0)
        if until > now_s:
            held.append(t)
        else:
            fire.append(t)
    return tuple(fire), tuple(held)


def baseline_of(obs: WakeObs) -> dict[str, dict[str, float | None]]:
    """当前观测 → 新基线表（轮次后重置/首评初始化共用）。"""
    return {
        code: {
            "pnl": obs.pnl_pct.get(code),
            "chg": obs.day_chg.get(code),
            "imb5": obs.imb5.get(code),
        }
        for code in obs.codes
    }


def idle_state(
    state: WakeState, obs: WakeObs, *, now: datetime, log_round_ts: float
) -> WakeState:
    """无触发/被抑制时推进状态：心跳、基线（首评或新轮次后重置）、新闻锚。

    新闻锚推进条件：本轮**没有**产生新闻触发（被冷却抑制的新闻触发不推进锚——
    否则一次冷却会把该条新闻永久吞掉）。
    """
    now_s = now.timestamp()
    baselines = dict(state.baselines)
    last_round = state.last_round_ts
    if log_round_ts > state.last_round_ts:
        baselines = baseline_of(obs)
        last_round = log_round_ts
    elif not baselines and obs.codes:
        baselines = baseline_of(obs)
    news_ts = state.news_ts
    # 走到本函数时新闻触发只可能是**被冷却抑制**（可触发的走 woken_state）：
    # 有挂起的就不推进锚（否则一次冷却把该条新闻永久吞掉），没有才标记已读。
    pending_news = bool(news_triggers(obs, state))
    if obs.brief_ts > news_ts and not pending_news:
        news_ts = obs.brief_ts
    return replace(
        state,
        last_eval_ts=now_s,
        last_round_ts=max(last_round, log_round_ts),
        news_ts=news_ts,
        baselines=baselines,
    )


def woken_state(
    state: WakeState,
    obs: WakeObs,
    *,
    now: datetime,
    fired: Sequence[WakeTrigger],
    log_round_ts: float,
) -> WakeState:
    """唤醒轮派出后推进状态：计数 +1、落冷却、基线重置、新闻锚推进。"""
    now_s = now.timestamp()
    cooldowns = dict(state.cooldowns)
    for t in fired:
        minutes = 5.0 if t.priority else SAME_DIR_COOLDOWN_MIN
        cooldowns[cooldown_key(t)] = now_s + minutes * 60.0
    counts_toward_budget = any(not t.priority for t in fired)
    return replace(
        state,
        last_eval_ts=now_s,
        last_round_ts=max(state.last_round_ts, log_round_ts),
        wake_count=state.wake_count + (1 if counts_toward_budget else 0),
        news_ts=max(state.news_ts, obs.brief_ts),
        baselines=baseline_of(obs),
        cooldowns=cooldowns,
    )


def budget_ok(state: WakeState) -> bool:
    """日预算（唤醒轮计数 < 上限）。"""
    return state.wake_count < DAILY_WAKE_LIMIT


def context_text(
    fired: Sequence[WakeTrigger],
    now: datetime,
    *,
    suppressed: Sequence[WakeTrigger] = (),
) -> str:
    """唤醒上下文块（进提示词 ``extra_context``）。"""
    lines = [f"【本轮为触发唤醒】（{now:%H:%M}，非整点槽位——盘中触发器把控制权叫醒）"]
    for t in fired:
        lines.append(f"- {t.label()}：{t.detail}")
    if suppressed:
        names = "、".join(t.label() for t in suppressed)
        lines.append(
            f"- （同码同向冷却中、本轮不重复唤醒：{names}）"
        )
    lines.append("- 本轮结论只针对以上触发事项；其余持仓按既有纪律管理。")
    return "\n".join(lines)


def one_line_note(
    fired: Sequence[WakeTrigger], suppressed: Sequence[WakeTrigger]
) -> str:
    """状态键 ``last_note`` 的一行留痕（运营面）。"""
    fire = "、".join(t.label() for t in fired) or "无"
    tail = (
        f"；冷却抑制：{'、'.join(t.label() for t in suppressed)}" if suppressed else ""
    )
    return f"唤醒触发：{fire}{tail}"
