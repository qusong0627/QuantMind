"""风险档位层（risk tiers）：全账户**买入侧**参数的动态上限（唯一实现）。

移植自隔壁 quant-Trader `scripts/risk_budget_agent.py`（确定性规则内核 v1，
含其 2026-09-08 / 09-12 / 09-18 三轮审计的结论）。分工与边界：

- **档位只收紧**：档位给出的每个参数与配置里的值取"更严者"（`apply_to_rules`），
  永不放宽——档位是**上限**，不是赋值。融资等放宽场景须显式改配置参数；
- **规则层只读参数**：`builtin_rules` 不感知档位，档位由本模块合进配置视图；
- **数据故障不得制造卖出**：档位不可信（缺失/损坏/过期）时只回退**买入侧**参数，
  杠杆/强减键不动（`FALLBACK_LIMITS`，隔壁 2026-09-18 评审 H-1）。

为什么要有这一层（隔壁的实况教训）：静态阈值只代表"最松的常态"。隔壁 09-08 实况
是预算定档"防守"、而主入口硬编码宽松档——**风险预算只兑现了一半**；今天（09-23）
它的 `logs/budget/2026-09-23.json` 生效值仍是 `leverage_max=1.0 / per_stock_pct=0.1 /
max_new_buys=1`，即真实上限由档位层而非硬编码决定。没有这一层，迁过来就是
"默认即最松，且没有任何东西能收紧它"。

失效姿态总表（每条都有测试）：

| 情形 | 姿态 |
|---|---|
| 从未配置（键不存在） | `absent`：不覆盖、不改行为，只告警一次（不是故障） |
| 文档缺日期 / 日期早于应定档日 | `stale`：买入侧回退防守，其余键取文档原值 |
| 结构异常 / JSON 坏 / budget 空 | `fallback`：只回退 `FALLBACK_LIMITS` |
| 档位名不认识 | `fallback`：**整份文档不可信**（数值可能属于另一档），不按名字硬套 |
| 单个值不可解析 | 该键回落到**档位表**对应值（买入侧回落到 FALLBACK），余键保留 |
| 文档缺买入侧键（版本漂移） | 缺的按 FALLBACK 补齐并留痕；杠杆键不猜 |
| Redis 读失败 | `fallback` + problems（**不抛**：一次抖动不该让全闸停摆） |
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from backend.shared.risk.registry import get_rule

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))  # 中国无夏令时（与 builtin_rules 同口径）

TIER_KEY = "qm:risk:tier"
TIER_DETAIL_KEY = "qm:risk:tier:{date}"
TIER_DETAIL_TTL = 35 * 86400
_SHOULD_BE_DAY_MAX_BACK = 30  # 回退搜索上限（防节假日表畸形导致死循环）

# ── 档位表（唯一出处；数值与隔壁 LEVELS 逐键一致）────────────────────

LEVELS: dict[str, dict[str, Any]] = {
    "calm": {
        "leverage_max": 1.5,
        "per_stock_pct": 0.20,
        "max_new_buys": 3,
        "leverage_trim_to": 1.3,
        "per_stock_pos_pct": 0.30,
        "label": "平静",
    },
    "caution": {
        "leverage_max": 1.2,
        "per_stock_pct": 0.15,
        "max_new_buys": 2,
        "leverage_trim_to": 1.15,
        "per_stock_pos_pct": 0.25,
        "label": "谨慎",
    },
    "defensive": {
        "leverage_max": 1.0,
        "per_stock_pct": 0.10,
        "max_new_buys": 1,
        "leverage_trim_to": 1.0,
        "per_stock_pos_pct": 0.15,
        "label": "防守",
    },
}

LEVEL_ORDER = {"calm": 0, "caution": 1, "defensive": 2}

#: 档位文档的键集（写读两侧同一常量，防漂移）
LIMIT_KEYS = (
    "leverage_max",
    "per_stock_pct",
    "max_new_buys",
    "leverage_trim_to",
    "per_stock_pos_pct",
)

#: 档位不可信时的回退：**只含买入侧参数**。三档 LEVELS 的这三个键恒 ≥ 本回退，
#: 因此"过期强制回退 / 缺键补齐"在买入侧只会收紧、绝不放松。
#: 有意不含 leverage_max / leverage_trim_to——强减是风险动作，数据故障不应触发
#: 强平；且压低 leverage_max 而 trim_to 保持原值会组合出"减到仍超限"的强平循环。
FALLBACK_LIMITS: dict[str, Any] = {
    "per_stock_pct": 0.10,
    "max_new_buys": 1,
    "per_stock_pos_pct": 0.15,
}

#: 档位键 → (规则 ID, 规则参数名, 更严方向)。`lower` = 值越小越严。
TARGETS: dict[str, tuple[str, str, str]] = {
    "leverage_max": ("l1.leverage_cap", "max_leverage", "lower"),
    "per_stock_pos_pct": ("l1.position_cap", "max_pct", "lower"),
    "per_stock_pct": ("l1.per_order_pct", "max_pct", "lower"),
    "max_new_buys": ("l1.new_buys_per_day", "max_new_buys", "lower"),
}

#: 已知但**尚无消费者**的键（有意为之，不算异常）：
#: - `leverage_trim_to`：减仓执行器（P2.6）的输入，只从档位文档读、不进规则参数。
PENDING_KEYS: frozenset[str] = frozenset({"leverage_trim_to"})

#: 映射已定、**规则待建**的键（P1.8 批次 B）。规则一旦注册，必须从此集合移除——
#: 测试钉死了这条自清理不变量（否则"待接"会变成永久借口，档位看着生效实则没有约束）。
#: **当前为空**：`l1.per_order_pct` 与 `l1.new_buys_per_day` 已注册（2026-09-23 批次 B），
#: 五个档位键全部有消费者。将来新增映射键、消费者尚未落地时再往这里登记。
TARGETS_PENDING: frozenset[str] = frozenset()

_INT_KEYS = frozenset({"max_new_buys"})

# ── 判定阈值（唯一出处）──────────────────────────────────────────────
VOL_HIGH = 1.2  # 指数 20 日收益率标准差（%）偏高
DD_HARD = 5.0  # 分账净值 20 日最大回撤（%）超限 → 直接防守
DD_DEEP = 3.0  # 回撤加深 → 至少谨慎
ZT_COLD = 25  # 涨停家数低于此 → 情绪偏冷
ZT_HOT = 90  # 高于此 → 情绪过热
MISSING_HARD = 2  # 关键输入缺失数 ≥ 此 → 防守


@dataclass(frozen=True)
class TierState:
    """一次档位解析的结果（纯数据；`source` 说明这份预算的可信度）。

    source：
      - ``absent``   从未配置（不是故障；不覆盖任何参数）
      - ``doc``      文档新鲜且结构完好（按文档值）
      - ``stale``    文档过期/缺日期（买入侧回退，其余键取文档原值）
      - ``fallback`` 文档不可读/结构异常（只回退 FALLBACK_LIMITS）
    """

    level: str = ""
    label: str = ""
    budget: Mapping[str, Any] = field(default_factory=dict)
    source: str = "absent"
    reasons: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    date: str = ""
    inputs: Mapping[str, Any] = field(default_factory=dict)


# ── 纯判定 ───────────────────────────────────────────────────────────


def should_be_dated_day(today: date, holidays: tuple[str, ...] = ()) -> date:
    """应定档日：今天（交易日），否则最近一个交易日。

    节假日表缺失时按周末近似——**近似失败的方向必须是"更严"**：把新鲜档位判成
    过期只会收紧买入侧；反之（把过期档位判成新鲜）会静默放宽，那才是要消灭的形态。
    """
    d = today
    for _ in range(_SHOULD_BE_DAY_MAX_BACK):
        if d.weekday() < 5 and d.isoformat() not in holidays:
            return d
        d -= timedelta(days=1)
    return today


def tier_stale_reason(
    doc: Mapping[str, Any] | None, *, today: date, holidays: tuple[str, ...] = ()
) -> str:
    """档位文档是否过期（返回原因，新鲜为 ""）。纯函数可测。

    只有**早于**应定档日才算过期；缺日期字段 = 不知道用的是哪天的档位，同样算。
    """
    want = should_be_dated_day(today, holidays)
    raw = str((doc or {}).get("date") or "")
    try:
        got = date.fromisoformat(raw)
    except ValueError:
        return (
            f"档位日期缺失/不可解析（{raw or '空'}），应定档日 {want.isoformat()}"
            "——定档任务或写入链路可能故障"
        )
    if got >= want:
        return ""
    return (
        f"档位日期 {got.isoformat()} 早于应定档日 {want.isoformat()}"
        "——定档任务或数据源可能故障，当天沿用旧档位买入侧已收紧"
    )


def decide_level(
    *, vol20: float | None, drawdown20: float | None, limit_up: int | None
) -> tuple[str, list[str]]:
    """纯判定：波动/回撤/情绪 → (档位, 触发因素)。

    fail-safe：风控输入缺失**不允许**落到最松档——关键输入缺 ≥2 项 → 防守；
    缺 1 项 → 至少谨慎。隔壁 09-08 的漏子正是"取不到就静默跳过"：数据故障反而
    放宽（只剩情绪闸），违反"风控故障不能跳过风控"。
    """
    reasons: list[str] = []
    missing: list[str] = []
    if vol20 is None:
        missing.append("指数波动")
    if drawdown20 is None:
        missing.append("分账回撤")
    if limit_up is None:
        missing.append("情绪温度")
    if vol20 is not None and vol20 >= VOL_HIGH:
        reasons.append(f"波动 {vol20}% 偏高")
    if drawdown20 is not None and drawdown20 >= DD_HARD:
        reasons.append(f"回撤 {drawdown20}% 超限")
    elif drawdown20 is not None and drawdown20 >= DD_DEEP:
        reasons.append(f"回撤 {drawdown20}% 加深")
    if limit_up is not None and limit_up < ZT_COLD:
        reasons.append(f"涨停仅 {limit_up} 家情绪偏冷")
    if limit_up is not None and limit_up > ZT_HOT:
        reasons.append(f"涨停 {limit_up} 家情绪过热")
    if drawdown20 is not None and drawdown20 >= DD_HARD:
        return "defensive", reasons
    if len(missing) >= MISSING_HARD:
        return "defensive", reasons + [
            f"风控数据缺失（{'、'.join(missing)}）→ 降级防守"
        ]
    if missing:
        reasons.append(f"风控数据缺失（{'、'.join(missing)}）→ 至少谨慎")
    if reasons:
        return "caution", reasons
    return "calm", []


def resolve_level(
    *,
    computed: str,
    prev_level: str | None,
    prev_date: str | None,
    today: date,
) -> tuple[str, str | None]:
    """防抖：**同一自然日只允许收紧**，隔日按最新状态恢复（隔壁实跑语义）。

    防抖不是单向棘轮——状态恢复了就该放松，否则档位会永久停在最严档。
    """
    if (
        prev_date == today.isoformat()
        and prev_level in LEVEL_ORDER
        and LEVEL_ORDER.get(computed, 0) < LEVEL_ORDER[prev_level]
    ):
        return prev_level, f"防抖：当日已定 {prev_level}，不再放宽"
    return computed, None


# ── 解析（纯）────────────────────────────────────────────────────────


def _fallback_state(problems: list[str], source: str = "fallback") -> TierState:
    return TierState(
        budget=dict(FALLBACK_LIMITS), source=source, problems=tuple(problems)
    )


def _coerce(key: str, raw: Any, level: str) -> tuple[Any | None, str]:
    """单值解析：坏值 → (None, 原因)。回退值由调用方按"买入侧/档位表"决定。"""
    try:
        return (int(raw) if key in _INT_KEYS else float(raw)), ""
    except (TypeError, ValueError):
        return None, f"档位键 {key} 的值不可解析（{raw!r}）"


def parse_tier_doc(
    doc: Mapping[str, Any] | None, *, today: date, holidays: tuple[str, ...] = ()
) -> TierState:
    """档位文档 → `TierState`（纯函数；一切失效姿态在此收敛，见模块 docstring 总表）。"""
    if doc is None:
        return TierState()  # absent：从未配置，不是故障
    if not isinstance(doc, Mapping):
        return _fallback_state([f"档位文档结构异常（顶层是 {type(doc).__name__}）"])
    raw_budget = doc.get("budget")
    if isinstance(raw_budget, str):
        try:
            raw_budget = json.loads(raw_budget)
        except ValueError as exc:
            return _fallback_state([f"档位预算不是合法 JSON（{str(exc)[:80]}）"])
    if not isinstance(raw_budget, Mapping):
        return _fallback_state(
            [f"档位预算字段缺失或结构异常（{type(raw_budget).__name__}）"]
        )
    level = str(doc.get("level") or "")
    level_problem = ""
    if level not in LEVELS:
        # 档位名不认识 = 写入侧是读侧不理解的版本 → **整份文档都不可信**：数值可能
        # 属于另一档（拼写错误 / 版本漂移）。只把名字改成"防守"而沿用文档数值，会造出
        # "标签说防守、参数是平静"的形态——正是本层要消灭的"静默按更松参数跑"。
        # 故按结构异常处理（买入侧回退收紧，杠杆键不猜：不制造强平）。
        # 不在此处提前返回：继续解析只为把其余诊断一并收集上来（下面统一 fallback）。
        level_problem = (
            f"档位名不认识（{level or '空'}），整份档位文档不可信——买入侧已回退防守"
        )
        level = "defensive"  # 仅供"值不可解析"时取兜底数值；文档数值一律不采信
    problems: list[str] = [level_problem] if level_problem else []
    unknown = [k for k in raw_budget if k not in LIMIT_KEYS]
    if unknown:
        # 边界处就报出来：写入侧比读侧新（或手写文档带了没接的键）时，静默丢弃会
        # 让"档位看着已生效、实际那一维从没被约束过"——正是本层要消灭的形态。
        problems.append(f"档位文档含未知键 {sorted(unknown)}，本层无消费者，已忽略")
    out: dict[str, Any] = {}
    for key in LIMIT_KEYS:
        if key not in raw_budget or raw_budget[key] is None:
            continue
        val, why = _coerce(key, raw_budget[key], level)
        if why:
            problems.append(why)
            val = FALLBACK_LIMITS.get(key, LEVELS[level].get(key))
            if val is None:
                continue
        out[key] = val
    if not out:
        problems.append("档位预算没有任何可用的档位字段（budget 为空或字段均不可解析）")
    if not out or level_problem:
        # 两条路都落到同一姿态：整份文档丢弃、只回退买入侧（`stale`/`fallback` 的区别
        # 只在标签：前者是"旧数值"、后者是"看不懂的数值"）
        return _fallback_state(problems)
    reasons_raw = doc.get("reasons")
    if isinstance(reasons_raw, str):
        try:
            reasons_raw = json.loads(reasons_raw)
        except ValueError:
            reasons_raw = []
    reasons = (
        tuple(str(r) for r in reasons_raw) if isinstance(reasons_raw, list) else ()
    )
    missing = [k for k in FALLBACK_LIMITS if k not in out]
    if missing:
        # 写读键集漂移（新键已上线、写入侧还是旧版）：缺的**买入侧**键按防守补齐，
        # 且必须留痕（隔壁 09-18 评审 MEDIUM：初版对缺键 continue → 新键静默不生效）
        out.update({k: FALLBACK_LIMITS[k] for k in missing})
        problems.append(f"档位缺少买入侧键 {missing}，已按防守回退补齐")
    stale = tier_stale_reason(doc, today=today, holidays=holidays)
    if stale:
        # 过期 = 整套档位不可信：买入侧三键强制回退防守；杠杆/强减键**取文档原值**
        # （不是买入侧刹车，见 FALLBACK_LIMITS 注释）
        return TierState(
            level=level,
            label=str(LEVELS[level].get("label", "")),
            budget={**out, **FALLBACK_LIMITS},
            source="stale",
            reasons=reasons,
            problems=tuple(problems + [stale]),
            date=str(doc.get("date") or ""),
        )
    return TierState(
        level=level,
        label=str(LEVELS[level].get("label", "")),
        budget=out,
        source="doc",
        reasons=reasons,
        problems=tuple(problems),
        date=str(doc.get("date") or ""),
        inputs=doc.get("inputs") if isinstance(doc.get("inputs"), Mapping) else {},
    )


# ── IO 层 ────────────────────────────────────────────────────────────


def _client(redis: Any):
    """兼容 RedisClient 包装（.client）与原生 redis 客户端（与风控同款）。

    `callable` 那半句是必需的：**原生 `redis.Redis` 自己有一个 `client()` 方法**
    （redis-py 的连接工厂），裸 `getattr(redis, "client", redis)` 会把这个方法当成
    包装拆出来，调用方随后在方法对象上 `.hset` → `'function' object has no
    attribute 'hset'`（2026-09-23 定档首次真跑踩中）。包装的 `.client` 是实例
    属性（真客户端对象，不可调用），据此区分。
    """
    client = getattr(redis, "client", None)
    return client if client is not None and not callable(client) else redis


def load_tier(
    redis: Any, *, today: date | None = None, holidays: tuple[str, ...] = ()
) -> TierState:
    """读当前档位。键不存在 → `absent`；读失败 → `fallback`（**不抛**）。

    读失败不抛的理由：抛出去会让每次判定都异常 → 闸门整体 fail-closed 拒掉所有
    买入，把一次 Redis 抖动放大成"全线不能买"。档位读不到的正确姿态是**收紧**。
    """
    today = today or datetime.now(tz=CST).date()
    try:
        raw = _client(redis).hgetall(TIER_KEY) or {}
    except Exception as exc:  # noqa: BLE001 - 读失败=回退收紧，不是停摆
        logger.warning("[RiskTier] 档位读取失败，回退买入侧防守参数: %s", exc)
        return _fallback_state(
            [f"档位读取失败（{type(exc).__name__}: {str(exc)[:120]}）"]
        )
    if not raw:
        return TierState()
    return parse_tier_doc(raw, today=today, holidays=holidays)


def save_tier(
    redis: Any,
    *,
    level: str,
    reasons: list[str] | tuple[str, ...] = (),
    inputs: Mapping[str, Any] | None = None,
    source: str = "producer",
    today: date | None = None,
) -> TierState:
    """写入档位（定档任务的唯一写入口）。未知档位名 → `ValueError`（fail-closed）。

    同时写当日明细（`qm:risk:tier:{date}`，带 TTL）——档位是**风控输入**，
    事后要能回答"那天为什么是这个档"，与决策留痕互为佐证。
    """
    if level not in LEVELS:
        raise ValueError(f"未知档位 {level!r}（合法值：{sorted(LEVELS)}）")
    today = today or datetime.now(tz=CST).date()
    budget = {k: LEVELS[level][k] for k in LIMIT_KEYS}
    fields = {
        "date": today.isoformat(),
        "level": level,
        "label": str(LEVELS[level].get("label", "")),
        "budget": json.dumps(budget, ensure_ascii=False),
        "reasons": json.dumps(list(reasons), ensure_ascii=False),
        "inputs": json.dumps(dict(inputs or {}), ensure_ascii=False),
        "source": str(source),
        "updated_at": datetime.now(tz=CST).isoformat(timespec="seconds"),
    }
    client = _client(redis)
    client.hset(TIER_KEY, mapping=fields)
    detail = TIER_DETAIL_KEY.format(date=today.strftime("%Y%m%d"))
    try:
        client.hset(detail, mapping=fields)
        client.expire(detail, TIER_DETAIL_TTL)
    except Exception as exc:  # noqa: BLE001 - 明细失败不影响主档位
        logger.warning("[RiskTier] 档位明细写入失败（不影响主档位）: %s", exc)
    logger.info(
        "[RiskTier] 定档 → %s（%s）by=%s", level, LEVELS[level]["label"], source
    )
    return TierState(
        level=level,
        label=str(LEVELS[level].get("label", "")),
        budget=budget,
        source="doc",
        reasons=tuple(str(r) for r in reasons),
        date=today.isoformat(),
        inputs=dict(inputs or {}),
    )


# ── 合并进规则配置（纯；只收紧）──────────────────────────────────────


def _rule_exists(rule_id: str) -> bool:
    import backend.shared.risk.builtin_rules  # noqa: F401 - 确保内置规则已注册

    return get_rule(rule_id) is not None


def _tighter(current: Any, candidate: Any, direction: str) -> Any:
    if current is None:
        return candidate
    try:
        return (
            min(current, candidate) if direction == "lower" else max(current, candidate)
        )
    except TypeError:
        return candidate


def apply_to_rules(
    rules: Mapping[str, Mapping[str, Any] | None], tier: TierState
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], tuple[str, ...]]:
    """把档位合进规则配置视图 → (merged, applied, problems)。**永不放宽**。

    - 与配置里的值取"更严者"（`min`）；配置更严时配置赢；
    - 规则**没在配置里启用**时，档位（只会更严的）参数把它启用起来——档位对买入侧
      是权威，"没配置"不等于"可以绕过档位"（隔壁 09-08 的"预算只兑现一半"）；
    - 指向未注册规则的键（映射按写入侧版本前进、规则还没跟上时）记入 problems
      并标注"待接"，不静默丢弃；
    - 未知键（写入侧比读侧新）同样上报。

    不可变：返回新字典，绝不原地改调用方（Redis 里的原始配置）数据。
    """
    merged: dict[str, dict[str, Any]] = {
        rid: dict(params or {}) for rid, params in rules.items()
    }
    applied: dict[str, dict[str, Any]] = {}
    if tier.source == "absent":
        return merged, applied, ()
    # 解析期的问题随档位态一并上抛：调用方只需看一个 channels 就能掌握"档位这次
    # 到底兑现了什么、哪里没兑现"（缺键补齐 / 未知键 / 过期 / 读失败都在这里）
    problems: list[str] = list(tier.problems)
    for key, value in tier.budget.items():
        target = TARGETS.get(key)
        if target is None:
            if key not in PENDING_KEYS:
                problems.append(f"档位键 {key} 没有消费者（未知键），本次未生效")
            continue
        rule_id, param, direction = target
        if not _rule_exists(rule_id):
            problems.append(
                f"档位键 {key} → 规则 {rule_id} 尚未注册（待接），本次未生效"
            )
            continue
        current = merged.get(rule_id, {}).get(param)
        effective = _tighter(current, value, direction)
        if effective != current:
            merged.setdefault(rule_id, {})[param] = effective
            applied.setdefault(rule_id, {})[param] = effective
    return merged, applied, tuple(problems)
