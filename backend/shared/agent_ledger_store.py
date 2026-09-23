"""分账账本落库读写侧（P2.7）——``apply_fill`` 记账、``import_legacy_seed`` 结转。

表结构见 :mod:`backend.shared.agent_ledger_contract`；记账算术见纯核心
:mod:`backend.shared.decision.agent_ledger`。本模块只做一件事：**把一笔成交
原子地记进那四张表**，并把账本读回成提示词要的形状。

**两个写入口，语义不同、互不重叠**（第三个写入者没有）：

* :func:`apply_fill` —— 记**一笔成交**，全书通用，任何时刻可调用；
* :func:`import_legacy_seed` —— 搬**迁入那一刻的状态**（P3 数据迁移），
  **只许在空账本上执行**（这三个 agent 一行都没有），否则整批拒绝。

三段职责，逐段可单测
--------------------
* **纯映射**（无 DB）：:func:`normalize_code` / :func:`position_deltas`；
* **读**：:func:`load_ledger` / :func:`load_agent_positions`；
* **写**：:func:`apply_fill` / :func:`import_legacy_seed`。

写纪律 1：**收调用方的事务**，自己不开事务、不 commit
------------------------------------------------------
``apply_fill`` 的第一个参数是调用方的 session。原因不是省事：成交入账要与
``Trade``/``Order`` 的累计成交同生共死——各写各的事务时，「成交行写了、账本没写」
这个中间态一旦被进程崩溃卡住，就是账本永久少一只票（``mine_of`` 看不见它，
agent 卖不掉自己的持仓），而**账本自身看不出任何异常**。同事务还顺带把「流水行的
唯一约束」与「持仓/现金改动」绑成一次提交。

写纪律 2：先落流水行，命中即**原地返回**
-----------------------------------------
顺序是「算好账 → 插流水行（`ON CONFLICT DO NOTHING RETURNING`）→ 插到了才改持仓」。
反过来的话，重投的第一件事就是把持仓改第二遍；先插流水行则重复投递在**第一步**
就停住，持仓与现金一个字节都不动。这就是「提交成功但进程以为失败 → 重投」那条
路径的护栏（A 股柜台与消费者都会这么重投，见 ``execution_stream_consumer._retry_or_dlq``）。

写纪律 3：**被判掉的成交也要落流水行**
---------------------------------------
卖非持仓、超卖、方向不认识、代码为空——这些 ``applied_volume=0`` 的行照样入库。
两个理由：① 「这次卖出没记上」是台账漂移最难查的一类事故，账本里必须留下痕迹；
② 留痕即**消费掉这个 fill_key**，重投不会在账本后来的状态上重新执行一次
（买入流水迟到时，一笔被拒的卖出会被「重新发现」成可执行 → 凭空多卖一笔）。

写纪律 4：`quota` 只用于**开户那一刻**
---------------------------------------
``quota`` 是绑定层的参数，不入库（见 contract 模块 docstring）。账户行已存在时，
本模块用**库里那个** ``virtual_cash``，调用方传什么都不影响已有账户。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from backend.shared.agent_ledger_contract import (
    ACCOUNT_TABLE,
    AGENT_COL_LEN,
    FILL_KEY_LEN,
    FILL_TABLE,
    POSITION_TABLE,
    ROUNDTRIP_TABLE,
)
from backend.shared.decision.agent_ledger import (
    BAD_TICK_TOLERANCE,
    DEFAULT_AGENT_QUOTA,
    LedgerChange,
    LegacySeed,
    agent_cash,
    agent_positions,
    record_buy,
    record_sell,
    sane_fill_price,
)
from backend.shared.utc_datetime import UTC, as_utc, to_utc_iso, utc_now

logger = logging.getLogger(__name__)

#: 账本版本号（与隔壁 `logs/live_ledger.json` 同号；读侧按它判形态）。
LEDGER_VERSION = 1

#: ``note`` 列的软上限（自由文本不该把一列撑爆；本模块生成的 note 远短于此）。
_NOTE_LIMIT = 1000

#: 方向的标准写法（``orders.side`` / ``OrderSide`` 的取值就是这两个）。
SIDE_BUY = "buy"
SIDE_SELL = "sell"

#: 持仓行的字段（写侧与判等键共用一份，避免两处各列一遍）
_POS_FIELDS = ("volume", "cost_price", "buy_ts", "last_ts")
_POS_COLS = "code, " + ", ".join(_POS_FIELDS)


@dataclass(frozen=True, slots=True)
class ApplyOutcome:
    """一次记账的结果。**四个字段都要被调用方看见**，不能只看 ``applied``。

    * ``applied``：实际记进账的数量（夹取后）。拿它写审计与「已记账」标记——
      拿入参数量去写就等于把没记上的差额当成已成交。
    * ``duplicate``：这个 fill_key 当天已记过，**账本一个字节都没动**。不是错误，
      是那条重投路径的正常收口；调用方据此跳过后续副作用（别重复发通知）。
    * ``note``：非空即代表本次记账与入参不一致（坏 tick、超卖、卖非持仓、方向不认识、
      或是重复投递）。**必须进日志**，不是可选项。
    * ``virtual_cash``：记账后该 agent 的虚拟现金（重复投递时 = 当前值）。
    """

    applied: float = 0.0
    duplicate: bool = False
    note: str = ""
    approx_price: bool = False
    virtual_cash: float | None = None


# ── 纯映射（无 DB）──────────────────────────────────────────────────


def normalize_code(code: Any) -> str:
    """输入口径的代码 → **后缀式**（``SH600036`` → ``600036.SH``）；空的返回空串。

    入参方向刻意宽容（本仓 PG/Redis/前端是前缀式，模型上下游写法混杂），出口只有
    一种。理由见 contract 模块 docstring——账本 `code` 若存前缀式，``mine_of``
    永远匹配不上 ``HoldingRow.code``，互卖防线与成本列会**同时静默失效**。
    """
    raw = str(code or "").strip()
    if not raw:
        return ""
    from backend.shared.stock_utils import StockCodeUtil

    try:
        return StockCodeUtil.to_suffix(raw) or raw
    except Exception:  # noqa: BLE001 - 归一失败不阻断记账（原样存，起码不丢码）
        return raw


def position_deltas(
    before: Mapping[str, Any] | None, after: Mapping[str, Any] | None
) -> tuple[list[dict[str, Any]], list[str]]:
    """持仓差异 → ``(要写的行, 要删的码)``。

    为什么 diff 而不是整段覆盖：这张表按 ``(租户, 账户, agent, code)`` 分行，
    整段覆盖要先删光再插——两个 fill 并发时（同一 agent 的多张委托）会把对方刚落的
    行删掉。diff 出来的写是幂等的，删只删**真的没了**的码。

    判等只看 ``(volume, cost_price)``：这两列变了行才需要写。时间戳列由同一批成交
    事件推出（量没变 ⇒ 没有成交 ⇒ 时间戳也没变），且读回来是字符串、内存里是
    datetime——把它们纳入判等会让每笔都判成「变了」，「无变化就不写」这条性质
    会静默消失。
    """
    out_rows: list[dict[str, Any]] = []
    for code, pos in (after or {}).items():
        row = pos if isinstance(pos, Mapping) else {}
        old = (before or {}).get(code)
        if isinstance(old, Mapping) and _identity(old) == _identity(row):
            continue
        out_rows.append({"code": code, **{k: row.get(k) for k in _POS_FIELDS}})
    removed = sorted(c for c in (before or {}) if c not in (after or {}))
    return out_rows, removed


def _identity(pos: Mapping[str, Any]) -> tuple[float | None, float | None]:
    """持仓的**判等键**（数量 + 成本，各自收成有限数）。"""
    cost = _finite(pos.get("cost_price"))
    return (_finite(pos.get("volume")), None if cost is None else round(cost, 4))


def _side_of(side: Any) -> str:
    """``OrderSide.BUY`` / ``"BUY"`` / ``" buy "`` → ``"buy"``；认不出的原样小写。

    ``OrderSide`` 是 ``str`` 子类枚举（``trade_shared/models/enums.py``），而
    ``str()`` 对它有版本差异（3.10 上是 ``"OrderSide.BUY"``），故先取 ``value``。
    """
    raw = getattr(side, "value", None)
    if raw is None:
        raw = getattr(side, "name", side)
    return str(raw or "").strip().lower()


# ── 读 ──────────────────────────────────────────────────────────────


async def load_ledger(
    session: Any,
    *,
    tenant_id: str = "default",
    user_id: str,
    agents: Sequence[str] | None = None,
) -> dict[str, Any]:
    """读回账本，形状与隔壁 ``logs/live_ledger.json`` 的 ``agents`` 段逐字一致::

        {"version": 1,
         "agents": {<模型名>: {"positions": {<后缀码>: {volume, cost_price,
                                                        buy_ts, last_ts}},
                               "virtual_cash": <float>}}}

    形状不是自选：提示词渲染的成本列/盈亏列（``decision/context``）与 ``mine_of``
    都按它取键，换形态等于两处口径各写一遍。

    **有账户行但没有持仓的 agent 也会出现**（``positions: {}``）：它的语义是
    「这家模型在这本账里，名下没有货」，与「这家模型压根没进分账」是两件事——
    前者卖不出去，后者是配置问题，调用点靠这个区别决定报哪一类告警。

    ``buy_ts`` / ``last_ts`` 一律 ``Z`` 结尾的 UTC 串（``to_utc_iso``）：纯核心的
    ``holding_days`` 与回合台账按这个口径解析，写别的形态会让持仓天数静默变成
    ``None``——而那是回合台账最需要的一个字段。
    """
    params: dict[str, Any] = {"t": tenant_id or "default", "u": user_id}
    clause = "tenant_id = :t AND user_id = :u"
    if agents:
        clause += " AND agent IN :ags"
        params["ags"] = list(agents)

    def _q(sql: str):
        """`IN :ags` 是**列表参数**，只有真带了这个占位符才能声明 expanding。"""
        stmt = text(sql)
        return stmt.bindparams(bindparam("ags", expanding=True)) if agents else stmt

    acc = await session.execute(
        _q(f"SELECT agent, virtual_cash FROM {ACCOUNT_TABLE} WHERE {clause}"), params
    )
    agents_out: dict[str, Any] = {}
    for row in acc.mappings().all():
        cash = _finite(row["virtual_cash"])
        agents_out[str(row["agent"])] = {
            "positions": {},
            # 现金列写 NULL 时回退到 quota：与纯核心 agent_cash 的缺失语义一致
            "virtual_cash": DEFAULT_AGENT_QUOTA if cash is None else cash,
        }

    pos = await session.execute(
        _q(f"SELECT agent, {_POS_COLS} FROM {POSITION_TABLE} WHERE {clause}"), params
    )
    for code, agent, fields in _group_positions(pos.mappings().all()):
        rec = agents_out.setdefault(
            agent, {"positions": {}, "virtual_cash": DEFAULT_AGENT_QUOTA}
        )
        rec["positions"][code] = fields
    return {"version": LEDGER_VERSION, "agents": agents_out}


async def load_agent_positions(
    session: Any,
    *,
    tenant_id: str = "default",
    user_id: str,
    agent: str,
) -> dict[str, Any]:
    """某 agent 名下的持仓段（``{后缀码: {...}}``）——``mine_of`` 的数据源。"""
    led = await load_ledger(
        session, tenant_id=tenant_id, user_id=user_id, agents=[agent]
    )
    rec = led["agents"].get(agent)
    return dict(rec.get("positions") or {}) if isinstance(rec, Mapping) else {}


def _group_positions(rows: Sequence[Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """持仓行 → ``(码, agent, 字段)``（跳过空码行）。"""
    out: list[tuple[str, str, dict[str, Any]]] = []
    for r in rows:
        m = dict(r)
        code = str(m.get("code") or "")
        if not code:
            continue
        out.append((code, str(m.get("agent") or ""), _pos_fields(m)))
    return out


def _pos_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    cost = _finite(row.get("cost_price"))
    return {
        "volume": _qty(row.get("volume")),
        "cost_price": 0.0 if cost is None else round(cost, 4),
        "buy_ts": to_utc_iso(_dt(row.get("buy_ts"))),
        "last_ts": to_utc_iso(_dt(row.get("last_ts"))),
    }


# ── 写（唯一入口）──────────────────────────────────────────────────


async def apply_fill(
    session: Any,
    *,
    tenant_id: str,
    user_id: str,
    agent: str,
    code: str,
    side: Any,
    volume: Any,
    price: Any,
    fill_key: str,
    trade_date: date | str,
    order_id: str = "",
    filled_at: datetime | None = None,
    quota: float = DEFAULT_AGENT_QUOTA,
    market: str = "CN",
    exit_reason: str | None = None,
    ref_price: Any = None,
) -> ApplyOutcome:
    """把一笔成交记进某 agent 的分账账本（**收调用方的 session，不 commit**）。

    :param fill_key: 幂等键。**当日唯一**，由调用方给（``trades._trade_idempotency_key``
        的产物：``exchange_trade_id``，否则 ``broker_order_id:exec_id``）。
    :param trade_date: **成交日**，由调用方给——不在这里取 ``now()``：重投事件要算出
        同一个日期，取处理时刻的话跨零点的重投会算出第二天、幂等键随之失效。
    :param ref_price: 可选参考价（实时价/昨收）。给了就过坏价闸
        （:func:`sane_fill_price`，±40%），越界按参考价记账并标 ``approx_price``；
        没给则不判——参考价都没有时「坏不坏」无从谈起，**不臆造一个**。
    :param exit_reason: 卖出理由（只记录，用于回合台账的行为归因），不参与判定。
    :param filled_at: 成交时刻（缺省 = 写入时刻）。

    返回 :class:`ApplyOutcome`；``note`` 非空必须进日志。异常情形与处理：

    * ``fill_key`` 为空/超长、``trade_date`` 形态不对、``quota`` 非数或为负 →
      **抛 ValueError**。这几个都是调用方的编程错误，宁可当场炸也不能静默换一个值
      ——换键等于把同一笔成交放进来两次，换日期等于当日唯一性作废。
    * 方向不认识 / 代码为空 / 数量价格非法 → 落一条 ``applied_volume=0`` 的流水并
      留 note（写纪律 3），不抛。
    """
    key = str(fill_key or "").strip()
    if not key:
        raise ValueError("fill_key 为空：分账账本拒绝无幂等键的记账（重投必双记）")
    if len(key) > FILL_KEY_LEN:
        raise ValueError(
            f"fill_key 超长（{len(key)} > {FILL_KEY_LEN}）：{key[:64]!r}…"
            "（键的形状变了要同步改契约列宽，别在这里截断——截断会把两笔并成一笔）"
        )
    cut = _finite(quota)
    if cut is None or cut < 0:
        raise ValueError(f"quota 非数或为负：{quota!r}（开户现金不接受静默回落）")

    day = _to_date(trade_date)
    stamp = as_utc(filled_at) if filled_at is not None else utc_now()
    code_s = normalize_code(code)
    code_col = (code_s or str(code or "").strip())[:32]
    direction = _side_of(side)

    # 开户 → 锁账户行（同一 agent 的并发 fill 在此串行）→ 读当前账本
    await _ensure_account(
        session, tenant_id=tenant_id, user_id=user_id, agent=agent, quota=cut
    )
    cash = await _lock_cash(session, tenant_id=tenant_id, user_id=user_id, agent=agent)
    before = await _positions_of(
        session, tenant_id=tenant_id, user_id=user_id, agent=agent
    )
    ledger: dict[str, Any] = {
        "version": LEDGER_VERSION,
        "agents": {agent: {"positions": before, "virtual_cash": cash}},
    }

    fill_px, approx = sane_fill_price(price, ref_price)
    notes: list[str] = []
    if approx:
        notes.append(
            f"坏 tick：成交价 {price!r} 与参考价 {ref_price!r} 偏离超过 "
            f"{BAD_TICK_TOLERANCE:.0%}，已按参考价 {fill_px} 记账"
        )
    if direction == SIDE_BUY:
        change = record_buy(ledger, agent, code_s, volume, fill_px, stamp, quota=cut)
    elif direction == SIDE_SELL:
        change = record_sell(
            ledger,
            agent,
            code_s,
            volume,
            fill_px,
            stamp,
            quota=cut,
            market=market,
            exit_reason=exit_reason,
        )
    else:
        change = LedgerChange(
            ledger=ledger,
            note=f"方向 {side!r} 不认识：只认 {SIDE_BUY}/{SIDE_SELL}，未记账",
        )
    if change.note:
        notes.append(change.note)
    note = "；".join(notes)[:_NOTE_LIMIT]

    if await _insert_fill(
        session,
        tenant_id=tenant_id,
        user_id=user_id,
        agent=agent,
        fill_key=key,
        trade_date=day,
        order_id=str(order_id or "")[:64],
        code=code_col,
        side=direction[:16],
        volume=_finite(volume) or 0.0,
        price=_finite(fill_px) or 0.0,
        applied_volume=change.applied,
        approx_price=approx,
        note=note,
        filled_at=stamp,
    ):
        # 写纪律 2：这个 fill_key 当天已经记过账了，账本一个字节都不动。
        # 「先插流水再做变更」保证重投在第一步就停住，而不是改完了才发现重复。
        return ApplyOutcome(
            duplicate=True,
            note=f"重复投递：{day} 的 {key} 已记账，本次未动账",
            approx_price=approx,
            virtual_cash=cash,
        )

    after = agent_positions(change.ledger, agent)
    upserts, removed = position_deltas(before, after)
    await _write_positions(
        session,
        tenant_id=tenant_id,
        user_id=user_id,
        agent=agent,
        upserts=upserts,
        removed=removed,
    )
    new_cash = agent_cash(change.ledger, agent, quota=cut)
    await _write_cash(
        session, tenant_id=tenant_id, user_id=user_id, agent=agent, cash=new_cash
    )
    rt = _roundtrip_of(change, direction)
    if rt is not None:
        await _write_roundtrip(
            session, tenant_id=tenant_id, user_id=user_id, agent=agent, row=rt
        )
    return ApplyOutcome(
        applied=change.applied,
        note=note,
        approx_price=approx,
        virtual_cash=new_cash,
    )


# ── 期初结转（P3 数据迁移）────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SeedWrite:
    """一个 agent 结转了什么（报告行）。"""

    agent: str
    virtual_cash: float
    positions: int
    cost: float


@dataclass(frozen=True, slots=True)
class SeedImportReport:
    """结转结果。``refused`` 非空 = **一行都没写**（含 dry-run 下的拒绝）。"""

    applied: bool
    dry_run: bool
    refused: tuple[str, ...] = ()
    agents: tuple[SeedWrite, ...] = ()
    accounts_written: int = 0
    positions_written: int = 0
    fills_written: int = 0
    fills_skipped: int = 0

    @property
    def cost_total(self) -> float:
        return round(sum(a.cost for a in self.agents), 2)


async def import_legacy_seed(
    session: Any,
    *,
    tenant_id: str,
    user_id: str,
    seed: LegacySeed,
    as_of: date | str,
    dry_run: bool = False,
) -> SeedImportReport:
    """把隔壁账本的 ``agents`` 段结转成本仓账本的**期初状态**（收调用方的 session）。

    与 :func:`apply_fill` 的关系：两者写的是同一批表，但语义不同——``apply_fill``
    记的是**这一笔成交**，本函数搬的是**迁入那一刻的状态**。故有两条独有纪律：

    1. **只许在空账本上执行**：这三个 agent 只要在账户表/持仓表/流水表里已有任何一行，
       整批拒绝（一行不写）。半本账上再叠一层期初状态，等于同一批仓记两遍——
       ``used`` 翻倍、``virtual_cash`` 被覆盖，且**新账本里看不出这是怎么来的**。
       拒绝时逐 agent 报出脏在哪张表、几行，由人来决定（本函数不删不改任何行）。
    2. **流水行照写**（``applied_volume = volume`` 的买入行，``fill_key`` 带
       :data:`SEED_FILL_PREFIX`）：账本状态要能从流水推回来，否则「流水即事实」在
       迁入这一刻就断了。对账侧（体检 C14）按同一前缀把这类行单独计数，不当异常。

    ``as_of``：结转日。**由调用方给**，且只用于没有 ``buy_ts`` 的持仓的流水日期
    （有 ``buy_ts`` 的按它的 **UTC 日**记，与 ``post_fill_for_order`` 的缺省口径一致）。

    返回 :class:`SeedImportReport`；**不 commit**（写纪律 1）。``dry_run=True``
    时只做到「查空账本」这一步，一行不写，报告里照样给出将写入的内容。
    """
    from backend.shared.decision.agent_ledger import seed_fill_key

    if not seed.ok:
        return SeedImportReport(
            applied=False, dry_run=dry_run, refused=tuple(seed.problems)
        )
    if not seed.agents:
        # 空计划不是错误：文件里没有 agent（或全被 problems 挡掉）。如实报 0。
        return SeedImportReport(applied=True, dry_run=dry_run)

    names = [a.agent for a in seed.agents]
    dirty = await _seed_dirty_agents(
        session, tenant_id=tenant_id, user_id=user_id, agents=names
    )
    if dirty:
        return SeedImportReport(applied=False, dry_run=dry_run, refused=tuple(dirty))

    writes: list[SeedWrite] = []
    accounts = positions = fills = skipped = 0
    day = _to_date(as_of)
    for a in seed.agents:
        writes.append(SeedWrite(a.agent, a.virtual_cash, len(a.positions), a.used))
        if dry_run:
            continue
        await session.execute(
            pg_insert(_table(ACCOUNT_TABLE)).values(
                tenant_id=str(tenant_id or "default")[:64],
                user_id=str(user_id or "")[:64],
                agent=a.agent[:AGENT_COL_LEN],
                virtual_cash=a.virtual_cash,
            )
        )
        accounts += 1
        upserts = [
            {
                "code": p.code,
                "volume": p.volume,
                "cost_price": p.cost_price,
                "buy_ts": p.buy_ts,
                "last_ts": p.last_ts,
            }
            for p in a.positions
        ]
        await _write_positions(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            agent=a.agent,
            upserts=upserts,
            removed=(),
        )
        positions += len(upserts)
        for p in a.positions:
            buy_day = p.buy_ts.date() if p.buy_ts is not None else day
            duplicated = await _insert_fill(
                session,
                tenant_id=str(tenant_id or "default")[:64],
                user_id=str(user_id or "")[:64],
                agent=a.agent[:AGENT_COL_LEN],
                fill_key=seed_fill_key(p.code)[:FILL_KEY_LEN],
                order_id="",
                trade_date=buy_day,
                code=p.code[:32],
                side=SIDE_BUY,
                volume=p.volume,
                price=p.cost_price,
                applied_volume=p.volume,
                approx_price=False,
                note=_SEED_NOTE,
                filled_at=p.buy_ts or utc_now(),
            )
            if duplicated:
                skipped += 1
            else:
                fills += 1

    return SeedImportReport(
        applied=True,
        dry_run=dry_run,
        agents=tuple(writes),
        accounts_written=accounts,
        positions_written=positions,
        fills_written=fills,
        fills_skipped=skipped,
    )


#: 结转流水的 ``note``（人话：这一行为什么没有对应的本仓成交）。
_SEED_NOTE = "期初结转：迁入时就持有的仓（来源=隔壁 live_ledger.json，非本仓成交）"


async def _seed_dirty_agents(
    session: Any, *, tenant_id: str, user_id: str, agents: Sequence[str]
) -> list[str]:
    """这三个 agent 在账本三张表里有没有既有行；有则返回逐条拒绝理由。"""
    t = str(tenant_id or "default")
    u = str(user_id or "")
    labels = (
        (ACCOUNT_TABLE, "账户行"),
        (POSITION_TABLE, "持仓行"),
        (FILL_TABLE, "流水行"),
    )
    reasons: list[str] = []
    for table, label in labels:
        res = await session.execute(
            text(
                f"SELECT agent, COUNT(*) FROM {table} "
                "WHERE tenant_id = :t AND user_id = :u AND agent IN :ags "
                "GROUP BY agent"
            ).bindparams(bindparam("ags", expanding=True)),
            {"t": t, "u": u, "ags": list(agents)},
        )
        for row in res.all():
            reasons.append(
                f"{row[0]}: 已有{label} {int(row[1])} 行——拒绝结转"
                "（空账本才许结转：否则同一批仓会被记两遍）"
            )
    return reasons


def _roundtrip_of(change: LedgerChange, direction: str) -> dict[str, Any] | None:
    """卖出记账产出的**最后一条**回合记录（``_write_roundtrip`` 的入参）。

    只在「卖出且真的记上了」时取：其余情形账本里没有回合记录，硬取会把买入也当成
    一次回合。取 ``[-1]`` 是因为纯核心每笔卖出追加一条（本次就是最后一条）。
    """
    if direction != SIDE_SELL or change.applied <= 0:
        return None
    rows = change.ledger.get("roundtrips")
    if not isinstance(rows, list) or not rows:
        return None  # 保留兜底：核心若改了产出形状，宁可少写一行也不写错一行
    row = rows[-1]
    return dict(row) if isinstance(row, Mapping) else None


async def _ensure_account(
    session: Any, *, tenant_id: str, user_id: str, agent: str, quota: float
) -> None:
    """开户（**只在新账户时**写入 ``quota`` 作初始现金）；已存在则什么都不做。

    ``ON CONFLICT DO NOTHING`` 而不是 ``DO UPDATE``：更新会把「现金已被交易改过」
    这件事抹回满额（写纪律 4）。
    """
    stmt = pg_insert(_table(ACCOUNT_TABLE)).values(
        tenant_id=str(tenant_id or "default")[:64],
        user_id=str(user_id or "")[:64],
        agent=str(agent or "")[:AGENT_COL_LEN],
        virtual_cash=quota,
    )
    await session.execute(
        stmt.on_conflict_do_nothing(index_elements=["tenant_id", "user_id", "agent"])
    )


async def _lock_cash(
    session: Any, *, tenant_id: str, user_id: str, agent: str
) -> float:
    """锁住该 agent 的账户行并取回现金——同一 agent 的并发 fill 在此串行。

    这也是记账算术的读基数：先锁后读，读到的一定是「上一个 fill 提交完之后」的值。
    """
    res = await session.execute(
        text(
            f"SELECT virtual_cash FROM {ACCOUNT_TABLE} "
            "WHERE tenant_id = :t AND user_id = :u AND agent = :a FOR UPDATE"
        ),
        {"t": tenant_id or "default", "u": user_id, "a": agent},
    )
    row = res.first()
    if row is None:
        # 理论上不可达（上一句刚 INSERT）：真发生了就是并发删账户/事务被回滚，
        # 此时按 0 记账会把「账户没了」写成「钱花光了」——宁可炸。
        raise RuntimeError(
            f"账户行刚建好就查不到：{tenant_id}/{user_id}/{agent}（分账记账中止）"
        )
    cash = _finite(row[0])
    return DEFAULT_AGENT_QUOTA if cash is None else cash


async def _positions_of(
    session: Any, *, tenant_id: str, user_id: str, agent: str
) -> dict[str, Any]:
    res = await session.execute(
        text(
            f"SELECT {_POS_COLS} FROM {POSITION_TABLE} "
            "WHERE tenant_id = :t AND user_id = :u AND agent = :a"
        ),
        {"t": tenant_id or "default", "u": user_id, "a": agent},
    )
    out: dict[str, Any] = {}
    for code, _agent, fields in _group_positions(res.mappings().all()):
        out[code] = fields
    return out


async def _insert_fill(session: Any, **row: Any) -> bool:
    """落流水行；**返回 True 表示这个 fill_key 当天已记过**（本行没插进去）。

    ``RETURNING id`` 是判据：``ON CONFLICT DO NOTHING`` 命中时不返回行，故「有没有
    插进去」不需要第二次查询（多一次查询就多一个竞态窗口）。
    """
    table = _table(FILL_TABLE)
    stmt = (
        pg_insert(table)
        .values(**row)
        .on_conflict_do_nothing(
            index_elements=["tenant_id", "user_id", "trade_date", "fill_key"]
        )
        .returning(table.c.id)
    )
    res = await session.execute(stmt)
    return res.first() is None


async def _write_positions(
    session: Any,
    *,
    tenant_id: str,
    user_id: str,
    agent: str,
    upserts: Sequence[Mapping[str, Any]],
    removed: Sequence[str],
) -> None:
    rows = [
        {
            "tenant_id": str(tenant_id or "default")[:64],
            "user_id": str(user_id or "")[:64],
            "agent": str(agent or "")[:AGENT_COL_LEN],
            "code": str(u.get("code") or "")[:32],
            "volume": _finite(u.get("volume")) or 0.0,
            "cost_price": _finite(u.get("cost_price")) or 0.0,
            "buy_ts": _dt(u.get("buy_ts")),
            "last_ts": _dt(u.get("last_ts")),
        }
        for u in (upserts or ())
        if str(u.get("code") or "")
    ]
    if rows:
        table = _table(POSITION_TABLE)
        stmt = pg_insert(table).values(rows)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["tenant_id", "user_id", "agent", "code"],
                set_={
                    "volume": stmt.excluded.volume,
                    "cost_price": stmt.excluded.cost_price,
                    "buy_ts": stmt.excluded.buy_ts,
                    "last_ts": stmt.excluded.last_ts,
                    "updated_at": text("NOW()"),
                },
            )
        )
    if removed:
        await session.execute(
            text(
                f"DELETE FROM {POSITION_TABLE} WHERE tenant_id = :t AND user_id = :u "
                "AND agent = :a AND code IN :codes"
            ).bindparams(bindparam("codes", expanding=True)),
            {
                "t": tenant_id or "default",
                "u": user_id,
                "a": agent,
                "codes": [str(c) for c in removed],
            },
        )


async def _write_cash(
    session: Any, *, tenant_id: str, user_id: str, agent: str, cash: float
) -> None:
    await session.execute(
        text(
            f"UPDATE {ACCOUNT_TABLE} SET virtual_cash = :c, updated_at = NOW() "
            "WHERE tenant_id = :t AND user_id = :u AND agent = :a"
        ),
        {"c": cash, "t": tenant_id or "default", "u": user_id, "a": agent},
    )


async def _write_roundtrip(
    session: Any, *, tenant_id: str, user_id: str, agent: str, row: Mapping[str, Any]
) -> None:
    await session.execute(
        pg_insert(_table(ROUNDTRIP_TABLE)).values(
            tenant_id=str(tenant_id or "default")[:64],
            user_id=str(user_id or "")[:64],
            agent=str(agent or "")[:AGENT_COL_LEN],
            market=str(row.get("market") or "CN")[:16],
            code=str(row.get("code") or "")[:32],
            volume=_finite(row.get("volume")) or 0.0,
            cost_price=_finite(row.get("cost_price")) or 0.0,
            sell_price=_finite(row.get("sell_price")) or 0.0,
            realized_pnl=_finite(row.get("realized_pnl")) or 0.0,
            pnl_pct=_finite(row.get("pnl_pct")),
            buy_ts=_dt(row.get("buy_ts")),
            # NOT NULL 列；核心保证写的是可解析的 Z 串，兜底只防「核心改了形状」
            sell_ts=_dt(row.get("sell_ts")) or utc_now(),
            holding_days=_finite(row.get("holding_days")),
            closed=bool(row.get("closed")),
            exit_reason=str(row.get("exit_reason") or "")[:32],
        )
    )


# ── 类型/取值收口 ───────────────────────────────────────────────────


def _to_date(v: Any) -> date:
    """成交日：只认 ``date`` / ``datetime`` / ``YYYY-MM-DD``；**形态不对当场抛**。

    不回落成「今天」：这是幂等键的一半，静默换一个日期等于把当日唯一性作废
    （跨零点重投会算成另一天，同一笔成交记两遍）。
    """
    if isinstance(v, datetime):
        return as_utc(v).date()
    if isinstance(v, date):
        return v
    s = str(v or "").strip()
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        raise ValueError(
            f"trade_date 不是 ISO 形态：{v!r}（幂等键的一半，不接受静默回落）"
        )
    return date.fromisoformat(s)


def _dt(v: Any) -> datetime | None:
    """列值 → aware UTC（``Z`` 结尾 / ``+00:00`` / ``datetime`` / ``date`` 都吃）。"""
    if isinstance(v, datetime):
        return as_utc(v)
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day, tzinfo=UTC)
    s = str(v or "").strip()
    if not s:
        return None
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        return as_utc(datetime.fromisoformat(s))
    except ValueError:
        return None


def _finite(x: Any) -> float | None:
    """宽松转 float（``bool``／非数／非有限 → ``None``）——与纯核心 ``_finite`` 同语义。"""
    if x is None or isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _qty(x: Any) -> float | int:
    """整数股数读成 int（与隔壁 JSON 的 ``"volume": 700`` 同形，对账时肉眼可比）。"""
    f = _finite(x)
    if f is None:
        return 0
    return int(f) if float(f).is_integer() else round(f, 4)


# ── 轻量 Table（纯台账表，不建 ORM 模型；只建一次）────────────────────

_TABLES: dict[str, Any] = {}


def _table(name: str) -> Any:
    """``pg_insert`` 用的轻量 Table（列类型只影响绑定，不参与建表）。

    列清单与 contract 的 DDL 同口径，但**只列写入方碰得到的那些**：这里不是第二份
    DDL（那份在 contract 模块，有测试逐语句比对），`pg_insert` 也只认表里有的列。
    """
    cached = _TABLES.get(name)
    if cached is not None:
        return cached
    from sqlalchemy import Column, Integer, MetaData, String, Table as _T
    from sqlalchemy.types import Boolean, Date, DateTime, Float

    cols = _cols(name, String, Float, Date, DateTime, Boolean, Integer)
    _TABLES[name] = _T(name, MetaData(), *[Column(c, t) for c, t in cols])
    return _TABLES[name]


def _cols(name: str, String, Float, Date, DateTime, Boolean, Integer):  # noqa: N803
    base = (
        ("tenant_id", String(64)),
        ("user_id", String(64)),
        ("agent", String(AGENT_COL_LEN)),
    )
    if name == ACCOUNT_TABLE:
        return base + (("virtual_cash", Float), ("updated_at", DateTime(timezone=True)))
    if name == POSITION_TABLE:
        return base + (
            ("code", String(32)),
            ("volume", Float),
            ("cost_price", Float),
            ("buy_ts", DateTime(timezone=True)),
            ("last_ts", DateTime(timezone=True)),
            ("updated_at", DateTime(timezone=True)),
        )
    if name == FILL_TABLE:
        return (
            ("id", Integer),
            *base,
            ("fill_key", String(FILL_KEY_LEN)),
            ("order_id", String(64)),
            ("trade_date", Date),
            ("code", String(32)),
            ("side", String(16)),
            ("volume", Float),
            ("price", Float),
            ("applied_volume", Float),
            ("approx_price", Boolean),
            ("note", String),
            ("filled_at", DateTime(timezone=True)),
        )
    if name == ROUNDTRIP_TABLE:
        return (
            ("id", Integer),
            *base,
            ("market", String(16)),
            ("code", String(32)),
            ("volume", Float),
            ("cost_price", Float),
            ("sell_price", Float),
            ("realized_pnl", Float),
            ("pnl_pct", Float),
            ("buy_ts", DateTime(timezone=True)),
            ("sell_ts", DateTime(timezone=True)),
            ("holding_days", Float),
            ("closed", Boolean),
            ("exit_reason", String(32)),
        )
    raise KeyError(f"没有 {name} 的列定义（新增表要在这里登记）")


__all__ = [
    "LEDGER_VERSION",
    "SIDE_BUY",
    "SIDE_SELL",
    "ApplyOutcome",
    "SeedImportReport",
    "SeedWrite",
    "apply_fill",
    "import_legacy_seed",
    "load_agent_positions",
    "load_ledger",
    "normalize_code",
    "position_deltas",
]
