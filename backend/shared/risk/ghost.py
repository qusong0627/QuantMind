"""影子代价账（纯核心）——把**被风控拦下的**决策变成可事后定价的行（P1.6）。

解决什么问题
------------
风控留痕只回答了"拦了什么"（`qm:risk:decisions` 里的 rule_id + 一句 reason），
没有任何数据能回答"**拦对了吗**"。日复一日加规则 = 单向棘轮：每条规则都有存在
的理由，没有一条会因为**代价大于收益**而下线。影子账给每条规则记一本账：
如果当时放行，后来是涨是跌？涨了=这条规则在花钱，跌了=它在省钱。

与隔壁（quant-Trader）的关系——**同类不同源**
-------------------------------------------
隔壁的 `scripts/ghost_ledger.py` 之所以要在交易路径里**主动记事件**，是因为它的
否决只以 stdout 中文句子存在，不记就永远没了。QuantMind 不需要：风控网关已经把
每一次判定结构化写进 `qm:risk:decisions:{date}`（rule_id/action/reason/evidence/
checked/enforced/version/tier），所以本模块是**留痕的派生读**——纯函数，从一条
留痕解析出若干行，**不进入交易主链路**（这是最强的观测姿态：零热路径风险）。

代价的定义（符号约定，全局唯一）
--------------------------------
``cost = sign(side) × excess``，其中 ``excess`` = 标的收益 − 全市场等权基准：

* 被拦的是**买入**（sign=+1）：没买到。标的后来涨了 → 我们错过了收益 → 成本为正；
* 被拦的是**卖出**（sign=−1）：没卖成，仓位留着。标的后来跌了 → 我们被套住了
  → 成本为正。

两种情形下 ``cost > 0`` 都读作"这条规则花了钱"，``cost < 0`` 读作"这条规则省了钱"。
隔壁只覆盖买入路径（其账本里 action 恒为 buy），卖出侧是本模块新定义的口径。

幂等键与去重纪律
----------------
键 = `tenant|uid|日|标的|方向|规则`（**含规则**：同一天同一只票被「现金不足」和
「集中度」分别拦下是两件事，合并记就丢了一条规则的成本）。同规则同日同标的同方向
**只记一次**——多轮反复拦同一只票不是多个独立样本，重复计数会把 n 撑成假的
（与隔壁同纪律，其依据见 `ghost_ledger.py` 模块头）。

两条实测防线（2026-09-23 在真留痕上撞出来的，不是想象的）
---------------------------------------------------------
**① 标的先归一。** 留痕的 `symbol` 是**谁写谁的口径**：实测 845 条里 495 条后缀式
（`600036.SH`）350 条前缀式（`SH600036`），同一只票两种写法都有。`ghost_id` 含标的，
不归一的话上游换个写法就能把一笔单拆成两行、把 n 悄悄撑大。落库口径统一取**前缀式**
（PG / Redis / 前端的既有口径），非 A 股的代码原样保留（`to_prefix` 认不出就原样返回）。

**② 测试租户不进账。** 集成测试会真往 `qm:risk:decisions` 里写决策，租户名每次随机
（`t-pending-life-4e4040` 这种），于是**每跑一次测试就给每条规则灌进一批假样本**——
它们进 n、进均值、进结论，而账面上看不出任何异常。实测 845 条里 230 条属此类。
故按 `TEST_TENANT_PREFIXES` 拒收，且**由调用方打印拒收条数**（静默过滤比污染更糟）。

本模块**不做 I/O**：解析、去重、定价的纯逻辑都在这里；读写落库与报表在
`backend/scripts/risk_ghost_*.py`。
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha1
from typing import Any

from backend.shared.risk.gate_registry import (
    KIND_VETO,
    gate_spec,
    kind_of,
    priced_kinds,
)
from backend.shared.stock_utils import StockCodeUtil

#: 集成测试的租户前缀（**实测清单**，不是猜的：各测试文件里的 `_TEST_TENANT_PREFIX`）。
#: 这些租户的决策会真的进决策流，但它们从未对应过一笔真单——计进代价就是假样本。
TEST_TENANT_PREFIXES: tuple[str, ...] = (
    "t-pending-life",  # backend/tests/test_pending_order_lifecycle.py:27
    "t-mktacct",  # backend/tests/test_ledger_accounts_market.py:23
    "t-hotset",  # backend/tests/test_hot_set_builder.py:21
    "t-p1-04-test",  # backend/tests/test_ledger_contract.py:94
    "_t_p206",  # backend/tests/test_shadow_compare_service.py:32
)

#: 追加拒收前缀（逗号分隔）——新测试套件用了新前缀时不必改代码
_ENV_EXCLUDE_TENANTS = "QM_GHOST_EXCLUDE_TENANTS"

CST = timezone(timedelta(hours=8))

#: 理由留痕上限（与留痕侧 decisions 的 2000 字符预算无关：影子账只需要够复盘）
REASON_MAX = 200

SIDE_BUY = "buy"
SIDE_SELL = "sell"

#: 会被记进影子账的判定动作 —— WARN 不拦单，没有"放行后会怎样"的问题
BLOCKING_ACTIONS: tuple[str, ...] = ("REJECT", "HALT")

#: 方向 → 成本符号（见模块头"代价的定义"）
_SIGN: dict[str, int] = {SIDE_BUY: 1, SIDE_SELL: -1}


def num(v: Any) -> float | None:
    """有限浮点或 None（脏值不进统计口径；bool 不算数字）。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def test_tenant_prefixes() -> tuple[str, ...]:
    """拒收前缀（默认清单 + `QM_GHOST_EXCLUDE_TENANTS` 追加）。

    每次调用重读 env：测试要能临时改，而默认值不该在 import 期冻死。
    """
    extra = tuple(
        p.strip() for p in str(os.getenv(_ENV_EXCLUDE_TENANTS) or "").split(",") if p.strip()
    )
    return TEST_TENANT_PREFIXES + extra


def is_test_tenant(tenant: Any, *, prefixes: Sequence[str] | None = None) -> bool:
    """该租户是不是测试夹具（见模块头②）。判定只看前缀，大小写敏感（租户 id 是原样字符串）。"""
    t = str(tenant or "")
    if not t:
        return False
    return any(t.startswith(p) for p in (test_tenant_prefixes() if prefixes is None else prefixes))


def normalize_symbol(symbol: Any) -> str:
    """标的归一成**前缀式**（见模块头①）。

    `to_prefix` 认不出的（美股 Ticker、港股 `0700.HK` 等）原样返回——本模块只保证
    「同一种写法恒等」，不负责给非 A 股代码定口径。空值返回空串（调用方据此跳过）。
    """
    raw = str(symbol or "").strip()
    if not raw:
        return ""
    try:
        return StockCodeUtil.to_prefix(raw) or raw
    except Exception:  # noqa: BLE001 - 归一失败不该丢掉这一行，原样保留更诚实
        return raw


def cost_sign(side: str) -> int:
    """方向 → 成本符号；未知方向返回 0（调用方据此跳过定价，不猜）。"""
    return _SIGN.get(str(side or "").strip().lower(), 0)


def cost_of(side: str, excess: float | None) -> float | None:
    """成本 = sign(side) × excess。任一侧不可得即 None（绝不退化成 0）。"""
    e = num(excess)
    sign = cost_sign(side)
    if e is None or sign == 0:
        return None
    return sign * e


@dataclass(frozen=True)
class GhostRow:
    """一条被拦决策（影子账的行；定价字段建行时空着，由定价器回填）。"""

    date: str  # 拒绝日（CST，YYYY-MM-DD）
    rule_id: str
    kind: str
    tenant: str
    uid: str
    symbol: str  # 留痕原样（前缀口径 SH600000）；定价时再转后缀
    side: str
    quantity: float | None
    source: str
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)
    enforced: bool = False
    version: int = 0
    ts: float = 0.0
    registered: bool = True

    # ── 定价回填位（建行时为空）─────────────────────────────────────
    entry_date: str | None = None
    entry_px: float | None = None
    tradable: bool | None = None
    fwd: dict[str, Any] | None = None
    priced_at: str | None = None

    @property
    def is_priced(self) -> bool:
        """**走过定价器**（≠ 有代价数）。

        未到期 / 一字板买不到 / 缺 bar 的行同样「走过定价器」，但成本是 None。
        代价统计**只认** `ghost_pricing.costs_by_horizon`（按状态取数），
        不要拿本属性当"可统计样本"的判据——那会把不可计价的行算进分母。
        """
        return bool(self.fwd)

    def to_record(self) -> dict[str, Any]:
        """落库/落盘用的普通 dict（含 id）。"""
        rec = {
            "id": ghost_id(self),
            "date": self.date,
            "rule_id": self.rule_id,
            "kind": self.kind,
            "tenant": self.tenant,
            "uid": self.uid,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "source": self.source,
            "reason": self.reason,
            "evidence": dict(self.evidence),
            "enforced": self.enforced,
            "version": self.version,
            "ts": self.ts,
            "registered": self.registered,
            "entry_date": self.entry_date,
            "entry_px": self.entry_px,
            "tradable": self.tradable,
            "fwd": self.fwd,
            "priced_at": self.priced_at,
        }
        return rec

    def priced(
        self,
        *,
        entry_date: str | None,
        entry_px: float | None,
        tradable: bool | None,
        fwd: dict[str, Any] | None,
        priced_at: str,
    ) -> GhostRow:
        """回填定价结果（返回新对象——不可变，避免共享行被就地改写）。"""
        return replace(
            self,
            entry_date=entry_date,
            entry_px=entry_px,
            tradable=tradable,
            fwd=fwd,
            priced_at=priced_at,
        )


def ghost_id(row: GhostRow) -> str:
    """幂等键：`tenant|uid|日|标的|方向|规则` 的 sha1 前 16 hex。

    含方向：同一标的同日的**买**被拦与**卖**被拦是两件事（成本符号相反）。
    """
    key = "|".join(
        (
            str(row.tenant or ""),
            str(row.uid or ""),
            str(row.date or ""),
            str(row.symbol or ""),
            str(row.side or "").lower(),
            str(row.rule_id or ""),
        )
    )
    return sha1(key.encode()).hexdigest()[:16]


def _day_of(ts: Any, fallback: str = "") -> str:
    """epoch 秒 → CST 日期（YYYY-MM-DD）。**按中国市场时区解释**，不用 UTC。

    这条不是形式主义：留痕的 ts 是 epoch 秒（`time.time()`），UTC 与 CST 差 8 小时，
    用 UTC 会把 00:00–08:00 CST 的单记到前一天，跨日统计全错。
    """
    f = num(ts)
    if f is None or f <= 0:
        return str(fallback or "")[:10]
    return datetime.fromtimestamp(f, tz=CST).strftime("%Y-%m-%d")


def parse_decisions(raw: Any) -> list[dict[str, Any]]:
    """把留痕里的 `decisions` 字段归一成 list[dict]（脏值一律当空，不抛）。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(raw, list):
        return []
    return [d for d in raw if isinstance(d, Mapping)]


def rows_from_decision_entry(
    entry: Mapping[str, Any], *, exclude_tenants: Sequence[str] | None = None
) -> list[GhostRow]:
    """一条留痕 → 若干影子账行（纯函数；脏值跳过而不是抛）。

    只取 ``action`` 为 REJECT/HALT 的规则命中：``decisions`` 里的 WARN 与
    ``checked`` 里跑过且放行的规则都**没有**"放行后会怎样"的问题。

    同一单被多条规则拦下 → 每规则一行（成本各归各的规则）。标的不明/方向不明/
    时间无法解释的行直接跳过：**宁可少记，不可造行**（造出来的行会被当作样本
    参与统计，而它对应的"决策"从未存在）。

    测试租户（见模块头②）整条拒收——调用方要自己数一下拒了多少条并打出来。
    `exclude_tenants=None` 走默认清单；传 `()` 表示**不拒收**（逃生口，
    读侧 `--include-test-tenants` 与之一致）。
    """
    if is_test_tenant(entry.get("tenant"), prefixes=exclude_tenants):
        return []
    decisions = parse_decisions(entry.get("decisions"))
    if not decisions:
        return []

    symbol = normalize_symbol(entry.get("symbol"))
    side = str(entry.get("side") or "").strip().lower()
    if not symbol or cost_sign(side) == 0:
        return []

    day = _day_of(entry.get("ts"))
    if not day:
        return []

    tenant = str(entry.get("tenant") or "default")
    uid = str(entry.get("uid") or "")
    quantity = num(entry.get("qty"))
    source = str(entry.get("source") or "")
    enforced = str(entry.get("enforced") or "").lower() == "true"
    try:
        version = int(entry.get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    ts = num(entry.get("ts")) or 0.0

    rows: list[GhostRow] = []
    for d in decisions:
        action = str(d.get("action") or "").upper()
        if action not in BLOCKING_ACTIONS:
            continue
        rule_id = str(d.get("rule_id") or "").strip()
        if not rule_id:
            continue
        evidence = d.get("evidence")
        rows.append(
            GhostRow(
                date=day,
                rule_id=rule_id,
                kind=kind_of(rule_id),
                tenant=tenant,
                uid=uid,
                symbol=symbol,
                side=side,
                quantity=quantity,
                source=source,
                reason=str(d.get("reason") or "")[:REASON_MAX],
                evidence=dict(evidence) if isinstance(evidence, Mapping) else {},
                enforced=enforced,
                version=version,
                ts=ts,
                registered=gate_spec(rule_id) is not None,
            )
        )
    return rows


@dataclass(frozen=True)
class RekeyPlan:
    """标的归一后的键迁移计划（纯数据，见 :func:`plan_rekey`）。"""

    #: (旧 id, 新 id, 规范标的)——就地改名，**保住已算好的价**
    renames: tuple[tuple[str, str, str], ...] = ()
    #: 撞键的旧行 id（同一笔单的另一种写法，删）
    doomed: tuple[str, ...] = ()
    #: 撞键时"被删的那个"带着价而保留者没有 → 把这行补写回去（价不能丢）
    carried: tuple[GhostRow, ...] = ()

    @property
    def n_changes(self) -> int:
        return len(self.renames) + len(self.doomed)

    @property
    def is_empty(self) -> bool:
        return self.n_changes == 0


def plan_rekey(rows: Sequence[GhostRow]) -> RekeyPlan:
    """把**已经落库的**非规范标的行迁到规范键上（纯函数）。

    为什么需要：`ghost_id` 含标的，所以 2026-09-23 加归一那天起，同一笔单的
    "旧写法行"与"新写法行"是两个不同的键。实测 615 行真账里 495 行是非规范写法、
    其中 **83 行与规范行撞键**——也就是说同一笔单已经被记了两遍，两遍都在 n 里。
    不迁移的话，重跑 `extract` 会在这 495 行旁边再长出一批规范行，n 直接翻倍。

    为什么不直接删掉重抽：留痕是会过期的（Redis Stream 有裁剪），重抽未必还能
    拿到同样那批决策——**能保住就绝不重建**。

    三条纪律：

    * 规范化行是天然保留者（它的标的本来就对），非规范行只在"这个新键还没人占"
      时才改名（`:attr:`RekeyPlan.renames`）；
    * 新键已被占 → 这一行是**同一笔单的重复记录**，删（`:attr:`RekeyPlan.doomed`）；
    * 删之前先看价：被删的行有价而保留者没有 → 把价补给保留者
      （`:attr:`RekeyPlan.carried`）。"重定价即可复原"是真的，但那要依赖行情还在，
      没有理由在这里赌。
    """
    keepers: dict[str, GhostRow] = {}
    renames: list[tuple[str, str, str]] = []
    doomed: list[str] = []
    carried: dict[str, GhostRow] = {}

    # 先登记规范行：它们占住的键就是不可让的
    for r in rows:
        if normalize_symbol(r.symbol) == r.symbol:
            keepers.setdefault(ghost_id(r), r)

    for r in rows:
        canon = normalize_symbol(r.symbol)
        if canon == r.symbol:
            continue
        old_id = ghost_id(r)
        new_id = ghost_id(replace(r, symbol=canon))
        survivor = keepers.get(new_id)
        if survivor is not None:  # 撞键：同一笔单的另一种写法
            doomed.append(old_id)
            if r.fwd and not survivor.fwd:
                carried[new_id] = replace(
                    survivor,
                    entry_date=r.entry_date,
                    entry_px=r.entry_px,
                    tradable=r.tradable,
                    fwd=r.fwd,
                    priced_at=r.priced_at,
                )
            continue
        keepers[new_id] = r
        renames.append((old_id, new_id, canon))

    return RekeyPlan(
        renames=tuple(renames),
        doomed=tuple(doomed),
        carried=tuple(carried.values()),
    )


def dedup_rows(rows: Iterable[GhostRow]) -> list[GhostRow]:
    """按幂等键去重，**保留最早的一条**（首次触发才是"这个决策被拦下了"的那次）。

    保留顺序：先按 (date, ts, symbol, rule_id) 排序再取首条，故同一批输入无论顺序
    如何都得到同一结果（幂等写库的前提）。
    """
    ordered = sorted(rows, key=lambda r: (r.date, r.ts, r.symbol, r.rule_id, r.side))
    seen: dict[str, GhostRow] = {}
    for r in ordered:
        seen.setdefault(ghost_id(r), r)
    return list(seen.values())


def extract_rows(
    entries: Iterable[Mapping[str, Any]], *, exclude_tenants: Sequence[str] | None = None
) -> list[GhostRow]:
    """多天留痕 → 全部影子账行（已去重）。`exclude_tenants` 语义同 `rows_from_decision_entry`。"""
    out: list[GhostRow] = []
    for e in entries:
        out.extend(rows_from_decision_entry(e, exclude_tenants=exclude_tenants))
    return dedup_rows(out)


def priced_rows(rows: Sequence[GhostRow]) -> list[GhostRow]:
    """可计入代价的行（`kind ∈ priced_kinds()` 且方向可定）。

    structural 类规则被排除是**口径**而非省略：放行也不会成交，代价恒为 0，
    把它们计入会让"整手校验"这类规则看起来在花钱（详见 gate_registry 模块头）。
    """
    return [r for r in rows if r.kind in priced_kinds() and cost_sign(r.side) != 0]


def summary_lines(rows: Sequence[GhostRow]) -> list[str]:
    """人读的一行摘要（CLI 用；报告面在 risk_ghost_report.py）。"""
    n_all = len(rows)
    n_priced = len(priced_rows(rows))
    n_enforced = sum(1 for r in rows if r.enforced)
    days = sorted({r.date for r in rows})
    window = f"{days[0]}~{days[-1]}" if days else "-"
    return [
        f"影子账行数: {n_all}（可计价 {n_priced}，翻闸后 {n_enforced}）",
        f"窗口: {window}",
        f"规则数: {len({r.rule_id for r in rows})}",
    ]


__all__ = [
    "REASON_MAX",
    "RekeyPlan",
    "plan_rekey",
    "SIDE_BUY",
    "SIDE_SELL",
    "TEST_TENANT_PREFIXES",
    "GhostRow",
    "cost_of",
    "cost_sign",
    "dedup_rows",
    "extract_rows",
    "is_test_tenant",
    "normalize_symbol",
    "num",
    "ghost_id",
    "parse_decisions",
    "priced_rows",
    "rows_from_decision_entry",
    "summary_lines",
    "test_tenant_prefixes",
    "KIND_VETO",
]
