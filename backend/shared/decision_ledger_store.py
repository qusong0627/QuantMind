"""决策审计表落库读写侧（P2.1d）——``Decision`` ↔ ``qm_decision_ledger``。

表结构见 :mod:`backend.shared.decision_ledger_contract`（与 `db_init.sql` 同口径，
有测试逐语句守着）。本模块是这张表**唯一的持久化出入口**。

三段职责，逐段可单测
--------------------
* **纯映射**（无 DB、无 IO）：:func:`decision_id` / :func:`pool_key` /
  :func:`iso_date` / :func:`kind_of` / :func:`record_values` / :func:`from_record`；
* **写**（:func:`upsert_rows`）：分两拨，见下；
* **读**（:func:`load_round` / :func:`load_day` / :func:`load_pending` /
  :func:`load_pool`）。

写纪律 1：审计字段**先写为准**，只有执行结果与定价可刷新
--------------------------------------------------------
`ON CONFLICT` 时能覆盖哪些列由 :func:`update_cols` **唯一表达**，分两拨：

* 决策拨（未定价）：命中已有行只刷新 ``armed`` / ``reject_reason`` / ``notes`` /
  ``order_id``——**不碰**「模型说了什么」（action/code/pct/价位/reason/invalidation）。
  重跑一轮时，第一次真的下过单、第二次因故没下，覆盖决策字段就等于抹掉「当时它
  说了什么」这件**只有审计表能回答**的事；而执行结果刷新是对的（后一次才是现状）。
* 定价拨（带 ``fwd``）：命中已有行只刷新定价列。与 P1.6 影子账同形——
  重跑抽取不得抹掉已算好的价（那边有一整段注释讲这个坑，本表同理）。

两拨都要**逐行**判断而不是按批（见 :func:`upsert_rows`）。

写纪律 2：`decided_at` 走 aware UTC
-----------------------------------
瞬时列一律 ``TIMESTAMPTZ`` + aware UTC（仓库口径）。入库前经 ``as_utc`` 收口，
naive 值按 UTC 解释而不是让 PG 按会话时区补——后者跨时区部署会把同一笔决策
记到相邻两天。
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from backend.shared.decision.contract import BUY as ACTION_BUY
from backend.shared.decision.contract import SELL as ACTION_SELL
from backend.shared.decision.contract import WATCH as ACTION_WATCH
from backend.shared.decision.contract import PCT_GIVEN, PCT_MISSING
from backend.shared.decision.contract import Decision
from backend.shared.decision_ledger_contract import ID_HEX_LEN, TABLE
from backend.shared.utc_datetime import UTC, as_utc

logger = logging.getLogger(__name__)

# ── 记分卡分类（隔壁 `decision_track._kind_of` 同口径） ──────────────────
KIND_BULLISH = "bullish"
KIND_POSITION = "position"
KIND_SELL = "sell"
#: 不跟踪（``hold`` 是「不动作」，没有可验证的收益语义）——**仍入库**，只是不进记分卡
KIND_NONE = "none"

#: 执行结果列（可刷新；写纪律 1）
_OUTCOME_COLS: tuple[str, ...] = ("armed", "reject_reason", "notes", "order_id")

#: 定价列（只在行确已定过价时写入；写纪律 1）
#: `tags` 与 `tradable` 同批：它们都由**入场日那天之前/当天**的行情推出，是定价的
#: 一部分；分两次写会出现「有价无标签」的中间态，报表就得分情况处理。
_PRICING_COLS: tuple[str, ...] = (
    "entry_date",
    "entry_px",
    "tradable",
    "fwd",
    "tags",
    "priced_at",
)

#: `notes` / `context_meta` 的取值上限（JSON 列不该被长文本撑爆）
_NOTE_LIMIT = 512
_REASON_LIMIT = 2000


def decision_id(
    round_id: str, index: int, code: str, action: str, agent: str = ""
) -> str:
    """**审计身份**：一轮里一条决策一个 id（重跑同内容幂等，内容变了就是新行）。

    为什么不用隔壁的 ``make_id``（agent|日|code|动作）当主键：那个键**每天只留
    第一条**，盘中改主意的第二次决策会撞键——撞键在本表意味着被覆盖或丢失，
    而这正是审计要回答的问题。隔壁的去重口径改由 :func:`pool_key` 承载。

    ``agent``（P2.9）**必须给**「一轮里有多家模型」的调用点：一轮 = 一个槽位
    （:func:`~backend.services.trade.services.decision_round_core.round_id_for`），
    两家模型同槽同码同动作、序号又都是 0，不带上 agent 就是**同一个主键**——
    后写的那家把先写的覆盖掉，而本表是「模型当时说了什么」的唯一出处。空 agent
    **逐字保留历史公式**（升级不改已入表行的 id；导入行 :func:`records_from_pool`
    另有 agent 维度的轮号，故不给）。
    """
    raw = f"{round_id}|{index}|{code}|{action}"
    if agent:
        raw = f"{raw}|{agent}"
    return hashlib.sha1(raw.encode()).hexdigest()[:ID_HEX_LEN]


def pool_key(agent: str, trade_date: str | date, code: str, action: str) -> str:
    """**记分卡身份**：公式与隔壁 `decision_track.make_id` 逐字同口径（sha1 前 16 hex）。

    与隔壁一致地使用**决策日**（不是入场日）；读侧 ``DISTINCT ON (pool_key)
    ORDER BY decided_at`` 即复现「每天第一条」的池。

    与存量池对账**要按 `(agent, 决策日, 标的, 动作)` 元组，不能比哈希**：
    隔壁把**模型原样写法**的代码喂进哈希（实测其 207 行存量池全是后缀式
    `001312.SZ`），本表传的是**归一后**的前缀式（`SZ001312`）——公式相同、
    输入不同，同一个决策在两边必然是两个哈希。元组对账还顺带免疫一件事：
    模型同一天把同一只票写成两种形式时，隔壁会裂成两条池行（其键是原样文本），
    本表会并成一条（实测那 207 行里没有这种裂法，此处是防御，不是已见的缺陷）。
    """
    day = (
        trade_date.isoformat() if isinstance(trade_date, date) else str(trade_date)[:10]
    )
    return hashlib.sha1(f"{agent}|{day}|{code}|{action}".encode()).hexdigest()[
        :ID_HEX_LEN
    ]


def iso_date(value: Any, *, what: str = "日期") -> date:
    """``YYYY-MM-DD`` → :class:`date`；**形态不对当场抛**，不交给标准库的版本差异。

    `date.fromisoformat` 在 **3.11+ 会接受** `20260909`、在 3.10 抛错——同一段码换个
    解释器就换一种读法。本仓已知两种日期写法并存（QuantDB 分区是 `YYYYMMDD`、幽灵层与
    存量池是 ISO），所以这条边界必须显式判形态，而不是让 `[:10]` 之类的松懈写法把
    「哪种写法」这件事变成解释器版本的函数。

    :param what: 出错信息里的主语（调用方点名是哪个字段，查起来不用猜）。
    """
    s = str(value or "").strip()
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        raise ValueError(
            f"{what}不是 ISO 形态：{value!r}"
            "（日期列一律 YYYY-MM-DD；紧凑写法 20260909 只有 3.11+ 的 fromisoformat 才接受）"
        )
    return date.fromisoformat(s)


def kind_of(action: str, code: str, held: Collection[str]) -> str:
    """决策 → 记分卡分类（隔壁 `decision_track._kind_of`，判据是**归一后**的代码）。

    ``watch`` 分两种：对已有持仓是持仓管理（``position``），对池外/未持有是看多
    意向（``bullish``）——同一动作两种语义，混在一起算就答不出「这个模型选股行不行」。
    """
    if action == ACTION_SELL:
        return KIND_SELL
    if action == ACTION_BUY:
        return KIND_BULLISH
    if action == ACTION_WATCH:
        return KIND_POSITION if code in held else KIND_BULLISH
    return KIND_NONE


@dataclass(frozen=True)
class DecisionRecord:
    """一行审计的完整形状（写读两侧只认它，不认裸 dict）。"""

    id: str
    pool_key: str
    round_id: str
    tenant_id: str
    user_id: str
    agent: str
    market: str
    trade_date: date
    decided_at: datetime
    code: str = ""
    code_raw: str = ""
    action: str = ""
    kind: str = KIND_NONE
    pct: float | None = None
    pct_state: str = "missing"
    pct_raw: str = ""
    confidence: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    move_stop: float | None = None
    invalidation: str = ""
    risk_amount: float | None = None
    reason: str = ""
    #: 执行结果（可刷新）
    armed: bool = False
    reject_reason: str = ""
    notes: tuple[str, ...] = ()
    order_id: str = ""
    #: 上下文快照（`pool_ctx` 为 None = 本轮未插桩，与「池外」是两件事）
    pool_ctx: Mapping[str, Any] | None = None
    context_meta: Mapping[str, Any] = field(default_factory=dict)
    #: 记分卡滚动列
    entry_date: date | None = None
    entry_px: float | None = None
    tradable: bool | None = None
    fwd: Mapping[str, Any] | None = None
    #: 入场前形态标签（`decision.tags.form_tags`；空 = 历史不够，不猜）
    tags: tuple[str, ...] = ()
    priced_at: datetime | None = None


def _sym(code: str) -> str:
    """模型写的代码 → PG 口径（前缀式）。归一失败则原样返回（不丢原始证据）。"""
    from backend.shared.stock_utils import StockCodeUtil

    raw = str(code or "").strip()
    if not raw:
        return ""
    try:
        return StockCodeUtil.to_prefix(raw) or raw
    except Exception:  # noqa: BLE001 - 归一失败不阻断审计入库
        return raw


def build_records(
    decisions: Iterable[Decision],
    *,
    round_id: str,
    agent: str,
    trade_date: date | str,
    decided_at: datetime,
    market: str = "CN",
    tenant_id: str = "default",
    user_id: str = "",
    held: Collection[str] = (),
    outcomes: Mapping[int, Mapping[str, Any]] | None = None,
    pool_ctx: Mapping[str, Mapping[str, Any]] | None = None,
    context_meta: Mapping[str, Any] | None = None,
) -> list[DecisionRecord]:
    """一轮决策 → 审计行（**逐条不丢**：含 ``hold`` 与无码行，含被拒的）。

    :param outcomes: 序号 → 执行结果（``armed``/``reject_reason``/``notes``/``order_id``），
        由 ``watch_map.WatchPlan.outcomes()`` 之类的映射层给出；缺席 = 尚无结果。
    :param pool_ctx: 代码（**原始写法**）→ 该行本轮候选池位置戳；缺席 = 写 NULL
        （NULL 与「池外」不是一回事，见 :attr:`DecisionRecord.pool_ctx`）。
    """
    day = (
        trade_date
        if isinstance(trade_date, date)
        else iso_date(trade_date, what="trade_date")
    )
    ts = as_utc(decided_at)
    held_set = set(held or ())
    ctx: Mapping[str, Any] = context_meta or {}
    out = outcomes or {}
    stamps = pool_ctx or {}

    records: list[DecisionRecord] = []
    for i, d in enumerate(decisions or ()):
        code_raw = str(d.code or "").strip()
        code = _sym(code_raw)
        action = str(d.action or "")
        res = out.get(i) or {}
        notes = res.get("notes") or ()
        records.append(
            DecisionRecord(
                id=decision_id(round_id, i, code, action, agent),
                pool_key=pool_key(agent, day, code, action),
                round_id=round_id,
                tenant_id=tenant_id or "default",
                user_id=user_id or "",
                agent=agent,
                market=market or "CN",
                trade_date=day,
                decided_at=ts,
                code=code,
                code_raw=code_raw,
                action=action,
                kind=kind_of(action, code, held_set),
                pct=d.pct.value if d.pct.is_given else None,
                pct_state=d.pct.state,
                pct_raw=str(d.pct.raw or "")[:32],
                confidence=float(d.confidence) if d.confidence is not None else None,
                stop_loss=d.stop_loss,
                take_profit=d.take_profit,
                move_stop=d.move_stop,
                invalidation=str(d.invalidation or ""),
                risk_amount=d.risk_amount,
                reason=str(d.reason or "")[:_REASON_LIMIT],
                armed=bool(res.get("armed", False)),
                reject_reason=str(res.get("reject_reason") or ""),
                notes=tuple(str(n)[:_NOTE_LIMIT] for n in notes),
                order_id=str(res.get("order_id") or ""),
                pool_ctx=dict(stamps[code_raw]) if code_raw in stamps else None,
                context_meta=dict(ctx),
            )
        )
    return records


#: 存量池导入行的 `round_id` 前缀（**可审计的出处**：读侧一眼看出这行不是本仓自行决策）。
POOL_IMPORT_PREFIX = "qt-pool"

#: 隔壁 `decision_pool.jsonl` 出现过的 kind 取值（本表 `kind_of` 的分类里没有 `none`）。
_POOL_KINDS = frozenset({KIND_BULLISH, KIND_POSITION, KIND_SELL})

#: 导入必需字段：缺一条就**不导**（审计行不许拿默认值填空）。
_POOL_REQUIRED = ("id", "agent", "date", "ts", "action", "kind", "code")


def records_from_pool(
    rows: Iterable[Mapping[str, Any]],
    *,
    round_prefix: str = POOL_IMPORT_PREFIX,
    tenant_id: str = "default",
    market: str = "CN",
) -> tuple[list[DecisionRecord], tuple[str, ...]]:
    """隔壁存量池的行 → 审计行（**决策原样收，行情段一律留空**）。

    返回 ``(记录, 逐条不合格原因)``。调用方见到原因就**一条都别写**：导入是「要么全进、
    要么不进」，最怕的是静默少几行——少掉的那几条，事后看起来与「那天模型没说话」
    完全一样。

    行情段（`entry_date`/`entry_px`/`tradable`/`fwd`/`tags`/`priced_at`）**刻意不搬**，
    哪怕隔壁有 137 行带着值：那些数出自 `daily_backward`（跨 vintage 拼缝的坏序列，
    见 P1.6 缺陷留档），搬过来等于把已知「符号可能翻转」的收益当历史战绩喂给模型。
    按迁移计划路线 (a)，这些一律用本仓 qfq 重算（`decision_ledger.py scorecard --apply`）。
    隔壁的 `entry_dt` 同理不搬——入场日由本仓日历重推。

    **`kind` 原样收，绝不重算**（本函数最容易做错的一处）：本表 `kind_of` 判 `watch`
    是 `position` 还是 `bullish` 要问「决策当时是否持有」，而那个持仓状态是隔壁运行时
    的东西。隔壁池里 watch 已分成 79 position / 68 bullish——这份**决策期知识**随系统
    下线就没了，用本仓当前持仓去重建只会把一批 position 记成 bullish，而「选股能力」
    与「持仓管理」混算正是 P2.1 要分开的两件事。

    `code_raw` 存隔壁原样写法（后缀式）、`code` 存本仓归一写法（前缀式）。对账按
    `(agent, 决策日, code_raw, action)` 元组，**不比哈希**：两边哈希输入不同，
    见 :func:`pool_key`。
    """
    out: list[DecisionRecord] = []
    problems: list[str] = []

    def _bad(i: int, why: str) -> None:
        problems.append(f"第 {i + 1} 行 {why}")

    for i, r in enumerate(rows or ()):
        missing = [k for k in _POOL_REQUIRED if not str(r.get(k) or "").strip()]
        if missing:
            _bad(i, f"缺字段 {'/'.join(missing)}")
            continue
        action = str(r["action"]).strip()
        if action not in (ACTION_BUY, ACTION_SELL, ACTION_WATCH):
            _bad(i, f"action 不认识：{action!r}")
            continue
        kind = str(r["kind"]).strip()
        if kind not in _POOL_KINDS:
            _bad(i, f"kind 不认识：{kind!r}")
            continue
        try:
            day = iso_date(r["date"], what="决策日（date）")
        except ValueError as exc:
            _bad(i, str(exc))
            continue
        try:
            ts = _decided_at(r["ts"])
        except ValueError as exc:
            _bad(i, str(exc))
            continue

        code_raw = str(r["code"]).strip()
        code = _sym(code_raw)
        agent = str(r["agent"]).strip()
        round_id = f"{round_prefix}:{str(r['id']).strip()}"
        pct = r.get("pct")
        # bool 是 int 的子类，`pct=True` 会静默变成 1.0——按非数处理
        given = isinstance(pct, (int, float)) and not isinstance(pct, bool)
        ctx = r.get("pool_ctx")
        out.append(
            DecisionRecord(
                # **不带 agent**（与决策轮那条相反）：这里的轮号 ``qt-pool:{隔壁 id}``
                # 本身就是隔壁按 (agent|日|code|动作) 算的，已含 agent 维度；再叠一层
                # 只会让「重跑导入」算出新 id ⇒ 老行留下、新行插进，导入不再幂等。
                id=decision_id(round_id, 0, code, action),
                pool_key=pool_key(agent, day, code, action),
                round_id=round_id,
                tenant_id=tenant_id or "default",
                user_id="",
                agent=agent,
                market=market or "CN",
                trade_date=day,
                decided_at=ts,
                code=code,
                code_raw=code_raw,
                action=action,
                kind=kind,
                pct=float(pct) if given else None,
                pct_state=PCT_GIVEN if given else PCT_MISSING,
                pct_raw=str(pct)[:32] if given else "",
                confidence=float(r["confidence"])
                if _isnum(r.get("confidence"))
                else None,
                stop_loss=float(r["stop_loss"]) if _isnum(r.get("stop_loss")) else None,
                take_profit=(
                    float(r["take_profit"]) if _isnum(r.get("take_profit")) else None
                ),
                reason=str(r.get("reason") or "")[:_REASON_LIMIT],
                # 隔壁池的 pool_ctx 是**一行一个**位置戳（不是按代码分组的映射）：
                # 缺席写 None = 本轮未插桩，与「池外」不是一回事，不许抹平成 {}
                pool_ctx=dict(ctx) if isinstance(ctx, Mapping) else None,
            )
        )
    return out, tuple(problems)


def _isnum(v: Any) -> bool:
    """真·数值（`bool` 不算：它会让 `stop_loss=True` 静默变成 1.0）。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def record_values(rec: DecisionRecord) -> dict[str, Any]:
    """`DecisionRecord` → 表列（含 id 与定价列，写入前由调用方按拨取舍）。"""
    return {
        "id": rec.id,
        "pool_key": rec.pool_key,
        "round_id": rec.round_id,
        "tenant_id": rec.tenant_id,
        "user_id": rec.user_id,
        "agent": rec.agent,
        "market": rec.market,
        "trade_date": _to_date(rec.trade_date),
        "decided_at": _decided_at(rec.decided_at),
        "code": rec.code,
        "code_raw": rec.code_raw,
        "action": rec.action,
        "kind": rec.kind,
        "pct": rec.pct,
        "pct_state": rec.pct_state,
        "pct_raw": rec.pct_raw,
        "confidence": rec.confidence,
        "stop_loss": rec.stop_loss,
        "take_profit": rec.take_profit,
        "move_stop": rec.move_stop,
        "invalidation": rec.invalidation,
        "risk_amount": rec.risk_amount,
        "reason": rec.reason,
        "armed": bool(rec.armed),
        "reject_reason": rec.reject_reason,
        "notes": list(rec.notes),
        "order_id": rec.order_id,
        "pool_ctx": dict(rec.pool_ctx) if rec.pool_ctx is not None else None,
        "context_meta": dict(rec.context_meta or {}),
        "entry_date": _to_date(rec.entry_date),
        "entry_px": rec.entry_px,
        "tradable": rec.tradable,
        "fwd": dict(rec.fwd) if rec.fwd is not None else None,
        "tags": list(rec.tags or ()),
        "priced_at": _to_dt(rec.priced_at),
    }


def update_cols(*, priced: bool) -> tuple[str, ...]:
    """upsert 命中已有行时要覆盖的列——**写纪律 1 的唯一表达处**。

    决策字段**永不出现在这里**：它们先写为准。把 ``action``/``pct``/``reason``
    加进来就等于允许后一轮抹掉前一轮的决策留痕。
    """
    return _PRICING_COLS if priced else _OUTCOME_COLS


def _to_date(v: Any) -> date | None:
    """``YYYY-MM-DD`` / date / datetime → ``date``（驱动只认 ``datetime.date``）。"""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v).strip()[:10])
    except ValueError:
        return None


def _to_dt(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return as_utc(v)
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day, tzinfo=UTC)
    try:
        return as_utc(datetime.fromisoformat(str(v).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def _decided_at(v: Any) -> datetime:
    """决策时刻：可解析就必须用给定值，**解析不出直接抛**。

    不能像 `as_utc` 那样回落到 ``utc_now()``：这是 NOT NULL 的审计列，静默回落到
    写入时刻 = 把「模型 10:03 说的」记成「我们 14:20 写库的时刻」——而这类错在表里
    与真值同形，事后没人能分辨。宁可当场炸。
    """
    dt = _to_dt(v)
    if dt is None:
        raise ValueError(f"decided_at 不是可解析的时间: {v!r}（审计列不接受静默回落）")
    return dt


def _as_json(v: Any) -> Any:
    """JSONB 列读回来的形状可能是 str（psycopg2 文本模式）——两种都要能吃。"""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return None
    return v


def from_record(rec: Any) -> DecisionRecord:
    """表行（RowMapping / 普通映射）→ `DecisionRecord`（列名别名在此收口）。"""
    m = dict(rec)
    notes = _as_json(m.get("notes"))
    pool_ctx = _as_json(m.get("pool_ctx"))
    meta = _as_json(m.get("context_meta"))
    fwd = _as_json(m.get("fwd"))
    tags = _as_json(m.get("tags"))
    return DecisionRecord(
        id=str(m.get("id") or ""),
        pool_key=str(m.get("pool_key") or ""),
        round_id=str(m.get("round_id") or ""),
        tenant_id=str(m.get("tenant_id") or "default"),
        user_id=str(m.get("user_id") or ""),
        agent=str(m.get("agent") or ""),
        market=str(m.get("market") or "CN"),
        trade_date=_to_date(m.get("trade_date")) or date(1970, 1, 1),
        decided_at=_to_dt(m.get("decided_at")) or as_utc(None),
        code=str(m.get("code") or ""),
        code_raw=str(m.get("code_raw") or ""),
        action=str(m.get("action") or ""),
        kind=str(m.get("kind") or KIND_NONE),
        pct=m.get("pct"),
        pct_state=str(m.get("pct_state") or "missing"),
        pct_raw=str(m.get("pct_raw") or ""),
        confidence=m.get("confidence"),
        stop_loss=m.get("stop_loss"),
        take_profit=m.get("take_profit"),
        move_stop=m.get("move_stop"),
        invalidation=str(m.get("invalidation") or ""),
        risk_amount=m.get("risk_amount"),
        reason=str(m.get("reason") or ""),
        armed=bool(m.get("armed")),
        reject_reason=str(m.get("reject_reason") or ""),
        notes=tuple(str(n) for n in notes) if isinstance(notes, list) else (),
        order_id=str(m.get("order_id") or ""),
        pool_ctx=dict(pool_ctx) if isinstance(pool_ctx, dict) else None,
        context_meta=dict(meta) if isinstance(meta, dict) else {},
        entry_date=_to_date(m.get("entry_date")),
        entry_px=m.get("entry_px"),
        tradable=m.get("tradable"),
        fwd=dict(fwd) if isinstance(fwd, dict) else None,
        tags=tuple(str(t) for t in tags) if isinstance(tags, list) else (),
        priced_at=_to_dt(m.get("priced_at")),
    )


def _dedup(rows: Sequence[DecisionRecord]) -> list[DecisionRecord]:
    """按主键去重（后写覆盖先写）。

    `ON CONFLICT` 对**同一条语句里**重复的键会报 `cannot affect row a second time`；
    一轮里两条完全相同的决策（同码同动作，序号不同）**不会**撞键（序号在 id 里），
    但重试拼接的两批会，故仍在客户端收口。
    """
    by_id: dict[str, DecisionRecord] = {}
    for r in rows:
        by_id[r.id] = r
    return list(by_id.values())


async def _upsert(session: Any, rows: Sequence[DecisionRecord], *, priced: bool) -> int:
    """一拨写入（`priced` 决定触发时覆盖哪组列，见写纪律 1）。"""
    if not rows:
        return 0
    cols = update_cols(priced=priced)
    prepared: list[dict[str, Any]] = []
    for r in rows:
        if priced and not r.fwd:  # 防御：带价那拨不该混进未定价的行
            continue
        if not priced and r.fwd:  # 防御：未定价那拨不该混进带价的行
            continue
        prepared.append(record_values(r))
    if not prepared:
        return 0
    stmt = pg_insert(_table()).values(prepared)
    set_: dict[str, Any] = {c: getattr(stmt.excluded, c) for c in cols}
    set_["updated_at"] = text("NOW()")
    await session.execute(stmt.on_conflict_do_update(index_elements=["id"], set_=set_))
    return len(prepared)


async def upsert_rows(session: Any, rows: Sequence[DecisionRecord]) -> int:
    """写入/刷新审计行（幂等，按 ``id``）。返回写入的行数。

    分两拨（带价 / 不带价）各发一条语句——**不能按批合并**：一批里只要有一行带价，
    同批其它行的定价列就会被 ``excluded.fwd = NULL`` 冲掉（P1.6 同款纪律）。
    """
    if not rows:
        return 0
    uniq = _dedup(rows)
    unpriced = [r for r in uniq if not r.fwd]
    priced = [r for r in uniq if r.fwd]
    n = await _upsert(session, unpriced, priced=False)
    n += await _upsert(session, priced, priced=True)
    return n


_COLS = (
    "id, pool_key, round_id, tenant_id, user_id, agent, market, trade_date, decided_at, "
    "code, code_raw, action, kind, pct, pct_state, pct_raw, confidence, stop_loss, "
    "take_profit, move_stop, invalidation, risk_amount, reason, armed, reject_reason, "
    "notes, order_id, pool_ctx, context_meta, entry_date, entry_px, tradable, fwd, tags, "
    "priced_at"
)


async def load_round(session: Any, round_id: str) -> list[DecisionRecord]:
    """一轮的全部审计行（按序号序返回——序号即 ``id`` 里的原始次序，用 ``code`` 无法还原）。"""
    res = await session.execute(
        text(f"SELECT {_COLS} FROM {TABLE} WHERE round_id = :r"), {"r": round_id}
    )
    return [from_record(r) for r in res.mappings().all()]


async def load_day(
    session: Any,
    trade_date: date | str,
    *,
    tenant_id: str = "",
    user_id: str = "",
    agent: str = "",
) -> list[DecisionRecord]:
    """某交易日的审计行（可选按租户/账户/模型过滤），按 ``decided_at`` 升序。"""
    where = ["trade_date = :d"]
    params: dict[str, Any] = {"d": _to_date(trade_date)}
    if tenant_id:
        where.append("tenant_id = :t")
        params["t"] = tenant_id
    if user_id:
        where.append("user_id = :u")
        params["u"] = user_id
    if agent:
        where.append("agent = :a")
        params["a"] = agent
    res = await session.execute(
        text(
            f"SELECT {_COLS} FROM {TABLE} WHERE {' AND '.join(where)} "
            "ORDER BY decided_at, id"
        ),
        params,
    )
    return [from_record(r) for r in res.mappings().all()]


async def load_pending(
    session: Any,
    *,
    since: date | str | None = None,
    until: date | str | None = None,
    limit: int = 5000,
) -> list[DecisionRecord]:
    """记分卡滚动记账的**候选集**：窗口内 ``kind <> 'none'`` 的全部行。

    **已定价的行也返回**——这是本查询唯一容易做错的地方。只按 ``priced_at IS NULL``
    捞（首版就是这么写的）会让 t20/t60 这类**晚到的期永远补不上**：一行在决策后第
    1 天定过价（t1 有数、t20 还是 `not_matured`），此后它再也不会回到队列里，
    报表上只表现为「t20 样本一直很少」，而**没有任何一处报错**。是否真要重算由
    调用方按逐期状态判定（`ghost_pricing.state_of` 的 not_matured/no_data），
    与 P1.6 影子账的 `_needs_pricing` 同一口径。

    窗口两端都按**决策日**（`entry_date` 在定价前恒为 NULL，且晚到的期正是靠
    决策日窗口兜住的）；调用方传 ``since`` 还有一个副作用：把一次取数限制在
    `GhostMarket.panel` 能承受的日期跨度内（见其 ``MAX_PANEL_DATES``）。

    只取 ``kind <> 'none'`` 的行：``hold`` 没有可验证的收益语义，给它们定价是把
    样本量灌水（隔壁 `decision_track` 也是这个口径）。
    """
    params: dict[str, Any] = {"n": int(limit), "none": KIND_NONE}
    clause = "kind <> :none"
    if until is not None:
        clause += " AND trade_date <= :u"
        params["u"] = _to_date(until)
    if since is not None:
        clause += " AND trade_date >= :s"
        params["s"] = _to_date(since)
    res = await session.execute(
        text(
            f"SELECT {_COLS} FROM {TABLE} WHERE {clause} "
            "ORDER BY trade_date, decided_at LIMIT :n"
        ),
        params,
    )
    return [from_record(r) for r in res.mappings().all()]


def _dedup_clause(inner: str) -> str:
    """把 `DISTINCT ON (pool_key)` 子查询重新按时间排好。

    `DISTINCT ON` 要求 `ORDER BY` **以去重键打头**，那样出来的行是按 `pool_key`
    的哈希序，报表要的却是时间序——故套一层再排。「每天第一条」的判据是
    `decided_at, id`（同秒两轮时用主键兜底，保证确定性）。
    """
    return f"SELECT * FROM ({inner}) _p ORDER BY trade_date, decided_at, id"


async def load_pool(
    session: Any,
    *,
    start: date | str | None = None,
    end: date | str | None = None,
    tenant_id: str = "",
    agent: str = "",
    limit: int = 20000,
    dedup: bool = True,
    include_unpriced: bool = False,
) -> list[DecisionRecord]:
    """**记分卡池**：``kind <> 'none'`` 的行，默认按 `pool_key` 去重。

    去重口径逐字复现隔壁 `decision_track` 的 `make_id` 池：同一 (agent, 决策日,
    标的, 动作) 只留**当天第一条**——盘中改主意的那几次仍留在审计表里（那是审计
    要回答的），只是不进记分卡（重复计样本会让样本量虚高）。

    ``dedup=False`` 给出审计视角的全量行，供逐条核对。
    ``include_unpriced=True`` 连未记账的行一起给（**只给导出/对账用**）：报表的
    分母要的是**可观测**的样本，没记账的行没有收益语义，混进去只会让 n 虚高。
    """
    where = ["kind <> :none"]
    if not include_unpriced:
        where.insert(0, "priced_at IS NOT NULL")
    params: dict[str, Any] = {"none": KIND_NONE, "n": int(limit)}
    if start is not None:
        where.append("trade_date >= :s")
        params["s"] = _to_date(start)
    if end is not None:
        where.append("trade_date <= :e")
        params["e"] = _to_date(end)
    if tenant_id:
        where.append("tenant_id = :t")
        params["t"] = tenant_id
    if agent:
        where.append("agent = :a")
        params["a"] = agent
    clause = " AND ".join(where)
    inner = f"SELECT {_COLS} FROM {TABLE} WHERE {clause} ORDER BY trade_date, decided_at, id"
    if dedup:
        inner = (
            f"SELECT DISTINCT ON (pool_key) {_COLS} FROM {TABLE} WHERE {clause} "
            "ORDER BY pool_key, decided_at, id"
        )
        inner = _dedup_clause(inner)
    res = await session.execute(text(f"{inner} LIMIT :n"), params)
    return [from_record(r) for r in res.mappings().all()]


_TABLE: Any = None


def _table() -> Any:
    """`qm_decision_ledger` 的轻量 Table（纯台账表，不建 ORM 模型），只建一次。"""
    global _TABLE
    if _TABLE is not None:
        return _TABLE
    from sqlalchemy import Column, MetaData, Table as _T
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.types import Boolean, Date, DateTime, Float, String

    cols = [
        Column("id", String(32), primary_key=True),
        Column("pool_key", String(32)),
        Column("round_id", String(64)),
        Column("tenant_id", String(64)),
        Column("user_id", String(64)),
        Column("agent", String(64)),
        Column("market", String(16)),
        Column("trade_date", Date),
        Column("decided_at", DateTime(timezone=True)),
        Column("code", String(32)),
        Column("code_raw", String(32)),
        Column("action", String(16)),
        Column("kind", String(16)),
        Column("pct", Float),
        Column("pct_state", String(16)),
        Column("pct_raw", String(32)),
        Column("confidence", Float),
        Column("stop_loss", Float),
        Column("take_profit", Float),
        Column("move_stop", Float),
        Column("invalidation", String),
        Column("risk_amount", Float),
        Column("reason", String),
        Column("armed", Boolean),
        Column("reject_reason", String),
        Column("notes", JSONB),
        Column("order_id", String(64)),
        Column("pool_ctx", JSONB),
        Column("context_meta", JSONB),
        Column("entry_date", Date),
        Column("entry_px", Float),
        Column("tradable", Boolean),
        Column("fwd", JSONB),
        Column("tags", JSONB),
        Column("priced_at", DateTime(timezone=True)),
        Column("created_at", DateTime(timezone=True)),
        Column("updated_at", DateTime(timezone=True)),
    ]
    _TABLE = _T(TABLE, MetaData(), *cols)
    return _TABLE


__all__ = [
    "KIND_BULLISH",
    "KIND_NONE",
    "KIND_POSITION",
    "KIND_SELL",
    "POOL_IMPORT_PREFIX",
    "DecisionRecord",
    "build_records",
    "decision_id",
    "from_record",
    "iso_date",
    "kind_of",
    "load_day",
    "load_pending",
    "load_pool",
    "load_round",
    "pool_key",
    "records_from_pool",
    "record_values",
    "update_cols",
    "upsert_rows",
]
