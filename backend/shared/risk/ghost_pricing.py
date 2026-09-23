"""影子代价账·定价核心（纯函数，无 I/O）——把拦下的决策换算成「事后代价」（P1.6）。

定价问句
--------
`ghost.py` 把留痕变成行；本模块回答每行的那个反事实问题：
**假如当时放行了，这笔单到 t1/t5/t20/t60 会赚还是会亏？**

代价 = 放行分支的收益（相对全市场等权）——即"这条规则花了多少钱"。
符号沿用 `ghost.cost_of`：``cost = sign(side) × excess``，``cost > 0`` 读作花了钱。

入场口径（**与本仓其它模块不同，是本模块自己的约定**）
----------------------------------------------------
**拦次日开盘入场**（``entry = 拦下那天的下一个交易日``），四期出场都在收盘
（tN = 入场日起第 N 个交易日收盘，t1 即当日开盘→当日收盘）。

为什么不是拦下当天：留痕里**没有任何价格字段**（`risk_gate_service._record` 的
字段是 ts/tenant/uid/symbol/side/qty/source/verdict/enforced/version），拦下那天的
盘中价无从取得。若用"当天开盘价"入场，对上午 10:30 被拦的单就是**前视**——
那个价格在决策之前。次开是**唯一**既统一又绝无前视的可成交价格。

已知的方向性偏差（写在这里以免被误当成无偏估计）：上午被拦的单会因此**晚一天**
入场，快信号的成本会被**低估**。四期口径一致，便于横向比较；不要把它当作
"当日入场的精确成本"。

可成交性（反事实必须先能成交，否则无从谈起）
--------------------------------------------
入场日开盘即封在板上的单**买不到/卖不掉**（一字板），停牌（无成交）同理。
这类行不是"代价为 0"，而是**这个问题问不出来** → 显式状态 ``untradable``，
与 0 严格区分（``None`` 纪律：不可得绝不退化成 0）。

各期状态的取值（报告按状态分组，绝不静默丢行）
----------------------------------------------
``ok``           有数，可计价
``not_matured``  出场日还没到（今天就是 2026-09-23，t20/t60 大多如此）
``untradable``   入场日一字板/停牌 → 反事实成交不了
``no_data``      缺 bar（数据缺口；**与停牌不同**，停牌是有 bar 且成交量为 0）
``retried``      仅 ``enforced=True`` 的行：拦截后不久又被成交，该次拦截没有改变结果

``retried`` 为什么只对 ``enforced`` 行成立
-----------------------------------------
影子期（``shadow=true``，本项目当前状态）留痕里 ``enforced=false``：判定照跑、
**不拦单**，单照常成交。此时"当天有成交"是**实现分支**，不是污染——反事实分支
（不成交）正是要拿来比的那一支。翻闸后（``enforced=true``）单真被拦，这时若同键
同日又被成交，说明拦了等于没拦（重试成功），成本≈0，才该剔除。
详见 `docs/local/` 的 P1.6 设计与 gate_registry 模块头。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from backend.shared.risk.ghost import GhostRow, cost_of, num

#: 远期窗口（交易日）：t1=入场日收盘；tN=入场日起第 N 个交易日收盘
HORIZONS: tuple[int, ...] = (1, 5, 20, 60)

#: 拦截后多久内又成交算「重试成功」（仅 enforced 行）。300s 取自实测：影子期
#: 重试成交的时差 p50=0s、p75=21s，300s 已远超该簇；放宽会把"另一笔独立单"
#: 误判成重试。
RETRY_WINDOW_S = 300.0

#: 价格比较容差（元）。涨停价由 `compute_limits` 按板别规则精确取整，
#: 触板时 open 与 limit_up **逐分相等**，故这里只需吸收浮点误差。
PRICE_EPS = 1e-9

#: 各期状态（见模块头；报告按此分组）
H_OK = "ok"
H_NOT_MATURED = "not_matured"
H_UNTRADABLE = "untradable"
H_NO_DATA = "no_data"
H_RETRIED = "retried"

#: 可计入代价统计的状态（**唯一**判断处，报告与统计都走它）
COUNTED_STATES: tuple[str, ...] = (H_OK,)


def horizon_key(h: int) -> str:
    """期名：1 → `t1`（与隔壁记分卡的 horizons 同名，便于对照）。"""
    return f"t{h}"


def is_retry_superseded(row: GhostRow, fill_ts: float | None) -> bool:
    """该行的拦截是否被"随后又成交"抵消（**仅对真被拦的行成立**）。

    影子放行（`enforced=False`）恒为 False：那时单本来就成交了，成交是实现分支，
    见模块头。`fill_ts` 与 `row.ts` 均为 epoch 秒，无需时区换算（同为绝对时刻）。
    """
    if not row.enforced:
        return False
    ts = float(row.ts or 0.0)
    f = num(fill_ts)
    if f is None or ts <= 0:
        return False
    delta = f - ts
    return 0.0 <= delta <= RETRY_WINDOW_S


# ── 交易日历（纯函数：只吃一串有序交易日，不碰日历来源）─────────────────
def next_trading_day(block_date: str, trading_days: Sequence[str]) -> str | None:
    """拦下那天**之后**的第一个交易日（入场日）。日历用尽 → None（不是猜）。

    `trading_days` 为升序 ISO 日期（YYYY-MM-DD）。拦下日恰是交易日时取**下一个**
    ——次开入场口径（见模块头），不是当天。
    """
    for d in trading_days:
        if d > str(block_date or ""):
            return d
    return None


def horizon_days(
    entry_day: str,
    trading_days: Sequence[str],
    horizons: Iterable[int] = HORIZONS,
) -> dict[int, str | None]:
    """各期出场交易日：入场日为第 1 个交易日，tN 取第 N 个（不足 → None）。

    返回 None 表示**还没到期**，调用方据此记 `not_matured`——不是"没有收益"。
    """
    try:
        i0 = list(trading_days).index(entry_day)
    except ValueError:
        return {int(h): None for h in horizons}
    seq = list(trading_days)
    return {
        int(h): (seq[i0 + int(h) - 1] if i0 + int(h) - 1 < len(seq) else None)
        for h in horizons
    }


def window_return(entry_px: float | None, exit_px: float | None) -> float | None:
    """开→收区间收益。任一端不可得或入场价非正 → None（绝不退化成 0）。"""
    a, b = num(entry_px), num(exit_px)
    if a is None or b is None or a <= 0:
        return None
    return b / a - 1.0


def excess_of(ret: float | None, bench: float | None) -> float | None:
    """超额 = 标的收益 − 全市场等权收益。任一侧不可得 → None。"""
    r, b = num(ret), num(bench)
    if r is None or b is None:
        return None
    return r - b


def cross_section_mean(rets: Iterable[float | None]) -> float | None:
    """全市场等权基准：截面均值，None/非有限值一律剔除；无有效样本 → None。

    基准与标的**必须同窗口形状**（皆是"入场日开盘→第 N 日收盘"），否则会把
    隔夜跳空算进超额。调用方负责用同一个 `window_return` 算基准。
    """
    vals = [v for v in (num(r) for r in rets) if v is not None]
    if not vals:
        return None
    mean = sum(vals) / len(vals)
    return mean if math.isfinite(mean) else None


# ── 入场可成交性（一字板 / 停牌）───────────────────────────────────────
def entry_unfillable_reason(
    side: str,
    *,
    open_px: float | None,
    limit_up: float | None,
    limit_down: float | None,
    volume: float | None,
    has_bar: bool,
) -> str | None:
    """入场日开盘能不能成交；能则 None，不能则一句原因（进状态，不进代价统计）。

    判据（只用入场那一刻的信息，无前视）：

    * 无 bar / 成交量为 0 → 停牌或缺数据；
    * 买入且开盘 ≥ 涨停价 → 封在涨停买不到（一字板）；
    * 卖出且开盘 ≤ 跌停价 → 封在跌停卖不掉。

    比的是**价格**而非百分比阈值：涨停价由 `compute_limits` 按板别精确取整，
    触板时逐分相等；百分比口径只在拿不到价格时才需要（见 `limit_threshold`）。
    """
    if not has_bar:
        return "入场日无行情（停牌或数据缺口）"
    v = num(volume)
    if v is None or v <= 0:
        return "入场日成交量为 0（停牌）"
    o = num(open_px)
    if o is None or o <= 0:
        return "入场日开盘价无效"
    side_l = str(side or "").strip().lower()
    up, dn = num(limit_up), num(limit_down)
    if side_l == "buy" and up is not None and math.isfinite(up) and o >= up - PRICE_EPS:
        return "入场日开盘涨停（买不到）"
    if side_l == "sell" and dn is not None and o > PRICE_EPS and o <= dn + PRICE_EPS:
        return "入场日开盘跌停（卖不掉）"
    return None


@dataclass(frozen=True)
class PriceInput:
    """一条行的市场侧输入（全是**已取好**的数，本模块不碰数据源）。"""

    entry_day: str | None = None
    entry_px: float | None = None
    #: 入场可否成交：None=没试过；False=一字板/停牌；True=可成交
    tradable: bool | None = None
    #: 不可成交时的原因（人读；`tradable=False` 时应有值）
    reason: str = ""
    #: 各期出场收盘价（缺期用 None）
    exit_px: Mapping[int, float | None] | None = None
    #: 各期全市场等权基准收益（同窗口形状）
    bench: Mapping[int, float | None] | None = None
    #: 入场日**还没到日历上**（`entry_day is None` 且原因在此）。
    #: 与"有入场日但那只票没有 bar"是两件事：前者过一天就能算（`not_matured`），
    #: 后者是数据缺口（`no_data`）——混起来会让"等一天"的账看起来像坏数据。
    entry_pending: bool = False


def _horizon_entry(
    *,
    state: str,
    ret: float | None = None,
    bench: float | None = None,
    excess: float | None = None,
    cost: float | None = None,
) -> dict[str, Any]:
    """一期的记录形状（非 ok 状态时各数值一律 None，见模块头）。"""
    return {"state": state, "ret": ret, "bench": bench, "excess": excess, "cost": cost}


def price_row(
    row: GhostRow,
    inp: PriceInput,
    *,
    priced_at: str,
    retry_fill_ts: float | None = None,
) -> GhostRow:
    """给一行回填定价（返回新对象，不可变）。**任何情况下都回填 `priced_at`**。

    四种状态（见模块头）互斥且显式；`cost` 只在 `ok` 时非 None，其余一律 None
    ——统计口径读 `COUNTED_STATES`，绝不用 0 冒充"没代价"。
    """
    exit_px = dict(inp.exit_px or {})
    bench = dict(inp.bench or {})

    superseded = is_retry_superseded(row, retry_fill_ts)
    untradable = inp.tradable is False

    fwd: dict[str, Any] = {}
    for h in HORIZONS:
        k = horizon_key(h)
        if superseded:
            fwd[k] = _horizon_entry(state=H_RETRIED)
            continue
        if untradable:
            fwd[k] = _horizon_entry(state=H_UNTRADABLE)
            continue
        if inp.entry_day is None or inp.entry_px is None:
            # `entry_pending` **只在入场日本身取不到时**才算"还没到"：已经有入场日
            # 却缺价，是那天的数据缺口（该去查），不是日历没走到的等待。
            still_waiting = inp.entry_day is None and inp.entry_pending
            fwd[k] = _horizon_entry(
                state=H_NOT_MATURED if still_waiting else H_NO_DATA
            )
            continue
        px = exit_px.get(h)
        if px is None:
            fwd[k] = _horizon_entry(state=H_NOT_MATURED)
            continue
        ret = window_return(inp.entry_px, px)
        b = num(bench.get(h))
        exc = excess_of(ret, b)
        fwd[k] = _horizon_entry(
            state=H_OK, ret=ret, bench=b, excess=exc, cost=cost_of(row.side, exc)
        )

    return row.priced(
        entry_date=inp.entry_day,
        entry_px=inp.entry_px,
        tradable=inp.tradable,
        fwd=fwd,
        priced_at=priced_at,
    )


#: 重算时**无权**覆盖 `ok` 的状态：它们都在说"今天的行情读不到"，
#: 而 `ok` 说的是"那一天我读到过"——过去的观测不会因为今天读失败而消失。
_DOWNGRADE_BLOCKED: tuple[str, ...] = (H_NOT_MATURED, H_NO_DATA, H_UNTRADABLE)


def merge_fwd(
    prev: Mapping[str, Any] | None,
    new: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[int, ...]]:
    """两轮定价结果合并：**已观测到的 `ok` 不可被降级**（返回合并结果 + 被拒降级的期）。

    为什么需要这条：定价会被反复跑（每天补未到期的期、补回填的行情）。重跑发生在
    "后来"，那一轮的读盘可能失败（分区没落、DuckDB 读空、日历短了）——同一行于是被
    重算成 `not_matured`/`no_data`。若直接覆盖，**已经算出来的数会凭空消失**，报表上
    只表现为"样本数莫名变少了"，且没有任何一处报错。

    例外（唯一）：`ok → retried` **允许**。`retried` 说的是"这单后来又被成交了"，
    是关于**单据**的事实而非行情可得性；它恰恰是**晚到才发现**的那类事实，锁住 `ok`
    会把一个本该剔除的样本永久留在分母里。

    其余方向一概放行：`not_matured → ok`（到期了）、`no_data → ok`（行情回填了）、
    `ok → ok`（前复权价被除权改写，新值更当前）。
    """
    merged: dict[str, Any] = {}
    rejected: list[int] = []
    for h in HORIZONS:
        k = horizon_key(h)
        n = new.get(k)
        p = (prev or {}).get(k)
        p_state = str(p.get("state") or "") if isinstance(p, Mapping) else ""
        n_state = str(n.get("state") or "") if isinstance(n, Mapping) else ""
        if not isinstance(n, Mapping):
            # 新一轮根本没出这一期（不该发生）：保留旧的，且这不算"降级"
            merged[k] = dict(p) if isinstance(p, Mapping) else n
            continue
        if p_state == H_OK and n_state in _DOWNGRADE_BLOCKED and isinstance(p, Mapping):
            merged[k] = dict(p)
            rejected.append(h)
            continue
        merged[k] = dict(n)
    # 保留 `new` 里本模块不认识的键（别把别人写的字段吃掉）
    for k, v in new.items():
        merged.setdefault(k, v)
    return merged, tuple(rejected)


def price_row_monotone(
    row: GhostRow,
    inp: PriceInput,
    *,
    priced_at: str,
    retry_fill_ts: float | None = None,
) -> tuple[GhostRow, tuple[int, ...]]:
    """``price_row`` + `merge_fwd`（对**该行已存的** `fwd` 合并）。

    重跑定价的**唯一入口**：直接调 `price_row` 会在一次失败的读盘上抹掉旧数
    （见 :func:`merge_fwd`）。返回 (新行, 被拒降级的期号)。
    """
    fresh = price_row(row, inp, priced_at=priced_at, retry_fill_ts=retry_fill_ts)
    merged, rejected = merge_fwd(row.fwd, fresh.fwd or {})
    return replace(fresh, fwd=merged), rejected


def costs_by_horizon(row: GhostRow, h: int) -> float | None:
    """取某期成本（仅 `ok` 有值）——报告与统计的唯一读数入口。"""
    if not row.fwd:
        return None
    ent = row.fwd.get(horizon_key(h))
    if not isinstance(ent, Mapping) or ent.get("state") not in COUNTED_STATES:
        return None
    return num(ent.get("cost"))


def state_of(row: GhostRow, h: int) -> str:
    """取某期状态；没定过价的行返回空串（与"没到期"区分）。"""
    if not row.fwd:
        return ""
    ent = row.fwd.get(horizon_key(h))
    return str(ent.get("state") or "") if isinstance(ent, Mapping) else ""


__all__ = [
    "COUNTED_STATES",
    "HORIZONS",
    "H_NOT_MATURED",
    "H_NO_DATA",
    "H_OK",
    "H_RETRIED",
    "H_UNTRADABLE",
    "PRICE_EPS",
    "RETRY_WINDOW_S",
    "PriceInput",
    "costs_by_horizon",
    "cross_section_mean",
    "entry_unfillable_reason",
    "excess_of",
    "horizon_days",
    "horizon_key",
    "is_retry_superseded",
    "merge_fwd",
    "next_trading_day",
    "price_row",
    "price_row_monotone",
    "state_of",
    "window_return",
]
