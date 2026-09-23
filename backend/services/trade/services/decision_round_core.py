"""轮次的契约与纯函数（P2.8）：槽位表、位置戳、依赖注入接口、状态形状。

本模块**无 IO**：不读 Redis、不读 DB、不读文件——一切外界都从
:class:`RoundDeps` 的注入点进来（生产接线见 ``decision_round_io``，编排见
``decision_round``）。这条纪律让编排层能被全替身测试，也让「到点没到点」
「位置戳怎么记」「状态键长什么样」这类判断可以脱离环境单独验。

文件拆分的理由：编排 + 接线 + 契约三者合在一起会超过本仓 800 行的单文件上限
（用户规则 common/coding-style.md）。三块的分界不是按「层」而是按**谁能独立被测**：
本模块纯函数可单测，``decision_round_io`` 需要环境，``decision_round`` 需要替身。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from backend.shared.decision.contract import SCHEMA_INTRADAY, SCHEMA_REBALANCE

logger = logging.getLogger(__name__)

CST = ZoneInfo("Asia/Shanghai")

TENANT_ID = "default"

#: 轮次总开关。**只有 ``"true"`` 是开**（``shared.env_flags`` 唯一读取实现）；
#: 默认关——LLM 轮一开就要真花钱+真下单，开关必须显式。
ENV_FLAG = "QM_DECISION_ROUND_ENABLED"
#: 决策账户（``resolve_db_account_user`` 口径：管理员族收口 10000001）。
ENV_ACCOUNT_USER = "QM_DECISION_ACCOUNT_USER_ID"
#: 轮询间隔（秒）与槽位宽限窗（分钟）。
ENV_POLL_S = "QM_DECISION_ROUND_POLL_S"
ENV_GRACE_MIN = "QM_DECISION_ROUND_GRACE_MIN"

DEFAULT_POLL_S = 30
#: 槽位过了这个点就**不补**：迟到 45 分钟的决策价与现价已经不是一回事，
#: 拿它下单等于按旧价报单。窗口彼此不重叠（槽间隔 ≥60 分钟）。
DEFAULT_GRACE_MIN = 45

SLOT_KEY_PREFIX = "trade:decision-round:slot"
DONE_KEY_PREFIX = "trade:decision-round:done"
LAST_KEY = "trade:decision-round:last"
LOG_KEY = "trade:decision-round:log"
LOG_KEEP = 20
SLOT_TTL_S = 2 * 86400
DONE_TTL_S = 7 * 86400
LAST_TTL_S = 7 * 86400

#: 账户快照年龄超过这个值就告警（**不拦轮**：08:30 盘前读到的必然是昨日收盘那份，
#: 拦下来等于砍掉盘前决策）。年龄进审计与状态键，让「拿旧资金做的决策」可查。
ACCOUNT_AGE_WARN_MIN = 60.0

#: 「空持仓 + 明显非零市值」的判定阈值（占账户总资产的比例）。见
#: :func:`positions_consistency_issue`：这不是「空仓」而是**持仓面不可信**。
EMPTY_POSITIONS_MV_RATIO = 0.01

STATUS_OK = "ok"
STATUS_SKIPPED = "skipped"
STATUS_ABORTED = "aborted"
STATUS_LLM_FAILED = "llm_failed"
STATUS_ERROR = "error"


# ── 槽位表 ───────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class RoundSlot:
    """一个到点要跑的轮次（北京墙钟）。"""

    hhmm: str
    schema: str
    catch_up: bool = False

    def __post_init__(self) -> None:
        if len(self.hhmm) != 4 or not self.hhmm.isdigit():
            raise ValueError(f"槽位时刻必须是 4 位 HHMM，收到 {self.hhmm!r}")
        if self.schema not in (SCHEMA_REBALANCE, SCHEMA_INTRADAY):
            raise ValueError(f"未知 schema={self.schema!r}")

    @property
    def hour(self) -> int:
        return int(self.hhmm[:2])

    @property
    def minute(self) -> int:
        return int(self.hhmm[2:])

    @property
    def label(self) -> str:
        return f"{self.hhmm[:2]}:{self.hhmm[2:]}"

    def due_at(self, day: date) -> datetime:
        """该槽位当日的到点时刻（aware，北京时间）。"""
        return datetime(
            day.year, day.month, day.day, self.hour, self.minute, tzinfo=CST
        )


#: 槽位表（见模块 docstring 的时刻表）。**顺序即执行顺序**：同一个 tick 里可能有多
#: 个槽同时落在宽限窗内（服务重启后补跑），按时间从早到晚跑。
SLOTS: tuple[RoundSlot, ...] = (
    RoundSlot("0830", SCHEMA_INTRADAY),
    RoundSlot("0900", SCHEMA_INTRADAY),
    RoundSlot("0935", SCHEMA_REBALANCE),
    RoundSlot("1000", SCHEMA_INTRADAY),
    RoundSlot("1005", SCHEMA_REBALANCE, catch_up=True),
    RoundSlot("1100", SCHEMA_INTRADAY),
    RoundSlot("1105", SCHEMA_REBALANCE, catch_up=True),
    RoundSlot("1200", SCHEMA_INTRADAY),
    RoundSlot("1300", SCHEMA_INTRADAY),
    RoundSlot("1400", SCHEMA_INTRADAY),
    RoundSlot("1445", SCHEMA_INTRADAY),
)

SLOTS_BY_HHMM: dict[str, RoundSlot] = {s.hhmm: s for s in SLOTS}


def round_id_for(day: date, slot: RoundSlot) -> str:
    """``rnd-{YYYYMMDD}-{HHMM}``（进腿幂等键的 round 段）。"""
    return f"rnd-{day:%Y%m%d}-{slot.hhmm}"


# ── 纯函数：到点判定与位置戳 ─────────────────────────────────────────
def due_slots(
    now: datetime,
    *,
    grace_min: int = DEFAULT_GRACE_MIN,
    slots: Sequence[RoundSlot] = SLOTS,
) -> tuple[RoundSlot, ...]:
    """当前到点且在宽限窗内的槽位（按时刻升序；空 = 本轮无事）。

    晚于 ``due_at + grace_min`` 的槽位**不再补**（见 ``DEFAULT_GRACE_MIN``）。
    """
    out: list[RoundSlot] = []
    for slot in slots:
        due = slot.due_at(now.date())
        if due <= now and (now - due).total_seconds() <= grace_min * 60:
            out.append(slot)
    return tuple(sorted(out, key=lambda s: s.hhmm))


def slot_keys(day: date, slot: RoundSlot) -> tuple[str, str]:
    """``(槽位键, 当日该 schema 的 done 键)``。"""
    return (
        f"{SLOT_KEY_PREFIX}:{day:%Y%m%d}:{slot.hhmm}:{slot.schema}",
        f"{DONE_KEY_PREFIX}:{day:%Y%m%d}:{slot.schema}",
    )


#: 池行 → 闸门行时**必须原样带过去**的字段（缺一个，提示词里就少一列）。
POOLROW_FIELDS = ("code", "name", "industry", "score", "fusion", "rank", "remark")


def pool_row_to_gate_row(row: Any, price: float | None) -> dict[str, Any]:
    """候选池行 + 现价 → ``gates.filter_pool`` 吃的 **dict 行**。

    ``filter_pool`` 判「买得起一手」要 ``price``，而 ``PoolRow`` 里没有价（价在行情
    快照里）——两样拼在这里，且**位置戳与喂进去的是同一份 dict**（kept/dropped 回读
    同一批键），避免「筛的时候按 A、记戳的时候按 B」。

    ``industry``/``remark`` 一路带过去：闸门只认 ``code``/``name``/``price``，但只要
    中间少带一列，回到提示词时那一列就成了空——筛一遍池子不该把行业与理由筛没。
    """
    out: dict[str, Any] = {f: getattr(row, f, None) for f in POOLROW_FIELDS}
    out["code"] = str(out.get("code") or "")
    out["price"] = price
    return out


def gate_row_to_pool_row(row: Mapping[str, Any]) -> Any:
    """闸门行 → ``PoolRow``（只取池行自己的字段，``price`` 是闸门加的、不属于它）。

    文本列 ``None`` → ``""``：``PoolRow`` 的声明是 ``str = ""``，把 ``None`` 塞进去
    会在渲染时印出字面的 ``None``（提示词里那一列就成了噪声）。
    """
    from backend.shared.decision.context import PoolRow

    return PoolRow(
        code=str(row.get("code") or ""),
        name=str(row.get("name") or ""),
        industry=str(row.get("industry") or ""),
        score=as_float(row.get("score")),
        fusion=as_float(row.get("fusion")),
        rank=row.get("rank"),
        remark=str(row.get("remark") or ""),
    )


def _stamp_of(row: Mapping[str, Any], *, state: str, shown: bool, **extra: Any) -> dict:
    stamp: dict[str, Any] = {
        "state": state,
        "shown": shown,
        "rank": row.get("rank"),
        "score": row.get("score"),
        "fusion": row.get("fusion"),
    }
    stamp.update(extra)
    return stamp


def build_pool_stamps(
    *,
    kept: Sequence[Mapping[str, Any]],
    dropped: Sequence[tuple[Any, Any]],
    wanted: Iterable[str] = (),
) -> dict[str, dict[str, Any]]:
    """候选池位置戳 → ``{行的原始写法代码: 位置}``（审计行 ``pool_ctx`` 的取值来源）。

    三态与隔壁 ``decision_track.pool_stamp`` 同义：``shown``（进了模型视野）/
    ``dropped``（被剔除：买不起一手或标的边界）/``off``（池外）。``dropped`` 上**额外**
    记 ``rule``（``l1.unaffordable`` / ``l2.pool_row_invalid`` / …）与中文 ``reason``
    ——隔壁只记位置，本仓把「为什么被剔」也留下（``kind=unseen`` 的那类剔除与
    「被闸门否决的决策」在成本账上不是一回事，得能分开数）。

    ``wanted``：模型本轮提到的代码（**原始写法**）。模型可以点池外的票，这时得给
    它一条 ``off`` 戳——缺席与「池外」不是一回事（``pool_ctx=None`` 表示「本轮没有
    池位置信息」，不是「不在池里」）。
    """
    stamps: dict[str, dict[str, Any]] = {}
    for row in kept:
        code = str(row.get("code") or "").strip()
        if code:
            stamps[code] = _stamp_of(row, state="shown", shown=True)
    for row, verdict in dropped:
        code = str(row.get("code") or "").strip() if isinstance(row, Mapping) else ""
        if not code:
            continue
        stamps[code] = _stamp_of(
            row if isinstance(row, Mapping) else {},
            state="dropped",
            shown=False,
            rule=str(getattr(verdict, "rule", "") or ""),
            reason=str(getattr(verdict, "reason", "") or "")[:200],
        )
    for code in wanted:
        raw = str(code or "").strip()
        if raw and raw not in stamps:
            stamps[raw] = {"state": "off", "shown": False}
    return stamps


def merge_outcomes(
    *maps: Mapping[int, Mapping[str, Any]] | None,
) -> dict[int, dict[str, Any]]:
    """多段裁定按序号合并（执行段 ∪ 守护段）。

    两段的序号**按构造不相交**：``action`` 是单值，``watch`` 的行不进
    ``plan_orders``、``hold/sell/buy`` 的行不进 ``plan_watch``。真撞上了（上游把
    ``decisions`` 改坏）取**先给的那一段**并告警——这里不抛异常：一轮调度因为
    审计段的一处怪相整个炸掉，比留一条含糊的审计行更糟。
    """
    out: dict[int, dict[str, Any]] = {}
    for mapping in maps:
        for index, value in (mapping or {}).items():
            if index in out:
                logger.warning(
                    "[DecisionRound] 审计序号冲突 index=%s（%s vs %s）：保留先到的一段",
                    index,
                    out[index].get("reject_reason") or out[index].get("armed"),
                    value.get("reject_reason") or value.get("armed"),
                )
                continue
            out[int(index)] = dict(value)
    return out


# ── 依赖注入（一张表说清一轮碰了哪些外界）────────────────────────────
@dataclass(frozen=True, slots=True)
class LLMBinding:
    """一次解析里出的「模型名 + 调用器」——**不许分家**。

    模型名要进腿幂等键、守护规则归属（``llm_owner``）与审计行；调用器按同一个
    config 构造。分成两个字段各自解析，就会出现「审计写着 A、实际是 B 在决策」。
    """

    model: str
    decide: Callable[[str, str], Any]  # (prompt, schema) -> DecisionAttempt


@dataclass(frozen=True, slots=True)
class AccountRead:
    """实盘账户额度三数（**同一行快照**：cash / market_value / total_asset）。"""

    ok: bool = False
    cash: float | None = None
    market_value: float | None = None
    total_asset: float | None = None
    source: str = ""
    snapshot_at: str = ""
    age_min: float | None = None
    errors: tuple[str, ...] = ()

    @property
    def quota_total(self) -> float:
        """本轮额度总量 = cash + market_value（与 ``RebalanceContext`` 同口径）。"""
        return round(float(self.cash or 0.0) + float(self.market_value or 0.0), 2)

    @property
    def quota_used(self) -> float:
        return round(float(self.market_value or 0.0), 2)


@dataclass(frozen=True, slots=True)
class ExclusionRead:
    """排除名单读取结果（**名单缺失 ≠ 空名单**）。"""

    symbols: frozenset[str] = frozenset()
    present: bool = False
    note: str = ""


@dataclass(frozen=True, slots=True)
class RoundDeps:
    """一轮编排的全部外界（frozen；测试逐项换替身，生产见 :func:`default_round_deps`）。"""

    #: 决策账户（``resolve_db_account_user`` 口径）
    account_user: Callable[[], str]
    #: (tenant, user) → (prefix 键持仓表, 各源元信息)
    load_positions: Callable[[str, str], Awaitable[tuple[dict[str, dict], dict]]]
    #: (tenant, user) → 资金面
    load_account: Callable[[str, str], Awaitable[AccountRead]]
    #: "YYYYMMDD" → 池产物（文件不在 → None）
    load_pool: Callable[[str], Any]
    #: () → 排除名单读取结果
    load_excluded: Callable[[], ExclusionRead]
    #: () → 远端行情客户端（未配置 → None）
    quote_client: Callable[[], Any]
    #: (client, codes) → suffix 键快照表
    read_snaps: Callable[[Any, Sequence[str]], Mapping[str, Mapping[str, Any]]]
    #: () → 当日风险档位
    load_tier: Callable[[], Any]
    #: () → LLM 绑定
    load_llm: Callable[[], LLMBinding]
    #: () → 异步上下文管理器（DB 会话）
    open_db: Callable[[], Any]
    #: (db=…, batch=…, …) → ExecutionOutcome
    run_exec: Callable[..., Awaitable[Any]]
    #: (agent, plan) → WatchWriteResult
    write_watch: Callable[[str, Any], Any]
    #: (db, records) → 写入行数
    write_ledger: Callable[..., Awaitable[int]]
    #: (day) → 是否交易日
    is_trading_day: Callable[[date], Awaitable[bool]]
    #: (now) → 是否连续竞价时段
    is_trading_time: Callable[[datetime], bool]
    #: 现读实盘开关（一轮内不变）
    real_enabled: Callable[[], bool]
    #: () → 当前北京时间（aware）
    now: Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class RoundResult:
    """一轮的结果（状态键与 CLI 的取值来源；字段与状态键 JSON 同名）。"""

    status: str
    day: date
    slot: RoundSlot | None = None
    round_id: str = ""
    note: str = ""
    agent: str = ""
    mode: str = ""
    decisions: int = 0
    legs: int = 0
    submitted: int = 0
    failed: int = 0
    watch_armed: int = 0
    audit_rows: int = 0
    errors: tuple[str, ...] = ()
    meta: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def as_status(self, *, at: datetime) -> dict[str, Any]:
        """状态键 JSON（**与审计 ``context_meta`` 同源**，前端/体检直接读）。"""
        return {
            "ts": at.isoformat(),
            "day": self.day.isoformat(),
            "slot": self.slot.hhmm if self.slot else "",
            "slot_label": self.slot.label if self.slot else "",
            "schema": self.slot.schema if self.slot else "",
            "round_id": self.round_id,
            "status": self.status,
            "note": self.note,
            "agent": self.agent,
            "mode": self.mode,
            "decisions": self.decisions,
            "legs": self.legs,
            "submitted": self.submitted,
            "failed": self.failed,
            "watch_armed": self.watch_armed,
            "audit_rows": self.audit_rows,
            "errors": list(self.errors),
            **dict(self.meta),
        }


def as_float(value: object) -> float | None:
    """PG 数值列 → float（空/脏 → None，**不是 0**）。"""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


# ── 编排 ─────────────────────────────────────────────────────────────
def refusing_submitter(reason: str) -> Callable[..., Awaitable[Any]]:
    """拒发提交器：腿留在纸上，回执与真发**同形**（审计不漏行）。

    用在这一处：非交易时段的轮次。走的是与真发同一条 ``execute_batch`` 代码路径
    （计划同源、序号同源），只是每条腿的回执写成拒发理由——与隔壁把「连续竞价
    时段闸」放进下单检查链、拒单即留痕同形。
    """

    async def _submit(leg: Any, client_order_id: str | None, real: bool) -> Any:
        return SimpleNamespace(
            success=False,
            order_id="",
            message=reason,
            duplicate=False,
            mirror=None,
        )

    return _submit


def tier_numbers(tier: Any) -> tuple[float | None, int | None]:
    """档位 → ``(per_stock_pct, max_new_buys)``；取不到给 ``None``（用上下文默认值）。

    ``TierState.source`` 的三态（absent/doc/stale/fallback）由 tiers 模块自己收口成
    budget 里的值，这里不再二次判断——但 ``absent``（从未配置）时 budget 是空的，
    于是两个 ``None`` 让 ``build_context`` 用它自己的默认值（0.20 / 3）。
    """
    budget = getattr(tier, "budget", None) or {}
    pct = budget.get("per_stock_pct")
    buys = budget.get("max_new_buys")
    out_pct = (
        float(pct)
        if isinstance(pct, (int, float)) and not isinstance(pct, bool)
        else None
    )
    out_buys = (
        int(buys)
        if isinstance(buys, (int, float)) and not isinstance(buys, bool)
        else None
    )
    return out_pct, out_buys


def abort_result(day: date, slot: RoundSlot, reason: str, **meta: Any) -> RoundResult:
    return RoundResult(
        status=STATUS_ABORTED,
        day=day,
        slot=slot,
        round_id=round_id_for(day, slot),
        note=reason,
        errors=(reason,),
        meta=meta,
    )


def positions_consistency_issue(
    *, holdings: int, market_value: float | None, total_asset: float | None
) -> str:
    """持仓面是否与资金面自相矛盾；矛盾则返回理由（非空串），一致则返回 ``""``。

    **为什么需要它**：持仓为空有两种来源，长得很像但语义相反——「账户真的空仓」与
    「持仓这条链断了」（桥侧持仓查询失败的常见形态是 `payload.positions` 为空；源相对
    全源最新并不陈旧，于是 `merge_real_sources` 既不入库也不报错，只把
    ``positions=0`` 写进 meta）。不区分的话，模型会按「空仓 + 满额度」决策：买入照常过
    闸，而真实持仓的减仓腿被 `plan_orders` 判「无持仓」全部拒绝——**该卖的没卖，不该买
    的买了**，且状态键是绿的。

    判据只用**同一行快照里的两个数**（不引入新的取数）：持仓表为空，而市值占总资产的
    比例超过 :data:`EMPTY_POSITIONS_MV_RATIO`，就是自相矛盾。真空仓的账户市值为 0，
    不会被误伤；总资产读不到时按市值自身作分母（保守：宁可判矛盾）。
    """
    if holdings > 0:
        return ""
    mv = as_float(market_value) or 0.0
    if mv <= 0:
        return ""
    ta = as_float(total_asset)
    base = ta if (ta or 0.0) > 0 else mv
    if mv <= EMPTY_POSITIONS_MV_RATIO * base:
        return ""
    return (
        f"持仓面与资金面自相矛盾：持仓表为空而市值 {mv:,.0f} 占总资产 {base:,.0f} 的 "
        f"{mv / base * 100:.1f}%（判为持仓面不可信，不是空仓）——持仓链路可能断在"
        f"桥侧，照此决策会「该卖的没卖、不该买的买了」"
    )


def snap_price(snap: Mapping[str, Any] | None) -> float | None:
    """快照 → 现价（标准键 ``Now`` 优先，原始推送 ``now`` 兜底）。"""
    if not isinstance(snap, Mapping):
        return None
    for key in ("Now", "now"):
        price = as_float(snap.get(key))
        if price is not None and price > 0:
            return price
    return None


def context_meta(**kw: Any) -> dict[str, Any]:
    """审计行 ``context_meta``：本轮「据什么做的决策」（每行一份，同轮相同）。"""
    slot: RoundSlot = kw["slot"]
    account: AccountRead = kw["account"]
    tier = kw["tier"]
    dropped = kw["dropped"]
    quotes = kw["quotes"]
    attempt = kw["attempt"]
    excluded: ExclusionRead = kw["excluded"]
    return {
        "round": {
            "round_id": kw["round_id"],
            "slot": slot.hhmm,
            "schema": slot.schema,
            "day": kw["day"].isoformat(),
            "model": kw["model"],
            "mode": kw["mode"],
            "in_session": bool(kw["in_session"]),
        },
        "usage": dict(getattr(attempt, "usage", None) or {}),
        "calls": int(getattr(attempt, "calls", 0) or 0),
        "quota": {
            "total": kw["quota_total"],
            "used": kw["quota_used"],
            "per_stock_pct": kw["per_stock_pct"],
            "max_new_buys": kw["max_new_buys"],
        },
        "account": {
            "source": account.source,
            "snapshot_at": account.snapshot_at,
            "age_min": account.age_min,
            "total_asset": account.total_asset,
        },
        "pool": {
            "file": kw["pool_file"],
            "shown": len(kw["kept"]),
            "dropped": len(dropped),
            "dropped_rules": sorted(
                {str(getattr(v, "rule", "") or "") for _, v in dropped} - {""}
            ),
        },
        "quotes": {
            "rows": len(quotes),
            "stale": int(kw["stale"]),
            "client": bool(kw["quote_client"]),
        },
        "tier": {
            "level": str(getattr(tier, "level", "") or ""),
            "source": str(getattr(tier, "source", "") or ""),
        },
        "exclusion": {
            "present": excluded.present,
            "symbols": len(excluded.symbols),
        },
        "sources": dict((kw["pos_meta"] or {}).get("sources") or {}),
        "notes": list(kw["notes"]),
    }
