"""风控网关（T-RC-02）：OrderRouter 内嵌接线的 IO 适配层——唯一判定入口。

架构（与 `docs/风险控制体系_设计方案.md` §五 / 实施细案 T-RC-02 对齐）：
- **判定核心**：`backend/shared/risk/`（纯函数，规则/状态机/fail-closed）；
- **本模块职责**：配置装载（Redis 热读 + 版本号）→ 上下文构建（账户/行情/急停/次数）→
  调用 `RiskGateCore.evaluate` → 决策全量留痕（`qm:risk:decisions` 流 + `qm:risk:metrics` 计数）
  → 按模式放行/拦截；
- **先影子后生效（ADR-0009）**：`shadow=true`（默认）时判定照跑、留痕照记、**不拦单**；
  翻闸 = 一次配置变更（`shadow=false`），全程留版本号；
- **fail-closed**：判定/上下文构建异常或配置不可读 → 拒单（错误如实入决策流与计数）。

配置：`qm:risk:config`（Hash）字段 `enabled`("true") / `shadow`("true") / `version`(int) /
`rules`(JSON：rule_id → params；不在表内的规则不启用，L0 急停/时段 always_on)。
档位：`qm:risk:tier`（Hash，见 `shared/risk/tiers.py`）——全账户**买入侧**参数的动态
上限，与配置取"更严者"合入 `rules`（只收紧，永不放宽）；档位未配置时不影响任何行为。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.shared.programmatic_trading_disclosure import log_high_frequency_warning
from backend.shared.risk import RiskContext, RiskGateCore, get_rule

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DirectOrderReq:
    """非 OrderRouter 直连路径的最小订单视图（TDX 桥循环 / QMT 执行端接线用，T-RC-02b）。

    与 OrderRequest 同形（build_context 按 getattr 读取），trading_mode=REAL 时
    账户上下文取 real_account_snapshots 最近快照（而非模拟账户）。
    """

    tenant_id: str
    user_id: int
    symbol: str
    side: str
    quantity: float
    price: float | None = None
    order_type: str = "market"
    trading_mode: str = "REAL"
    source: str = "tdx_bridge"
    remarks: str | None = None
    strategy_id: str = ""
    client_order_id: str = ""


CST = timezone(timedelta(hours=8))
CONFIG_KEY = "qm:risk:config"
DECISIONS_KEY = "qm:risk:decisions:{date}"
METRICS_KEY = "qm:risk:metrics:{date}"
DECISIONS_MAXLEN = 20000

# 初始启用规则（配置初始化用；影子期只留痕不拦单，翻闸前按影子报告校准参数）
DEFAULT_RULES: dict[str, dict[str, Any]] = {
    "l0.clock_drift": {"max_skew_ms": 500.0},
    "l1.available_cash": {},
    "l1.t1_sellable": {},
    "l1.position_cap": {"max_pct": 0.15},
    # 单**笔**买入的体量上限：默认与 position_cap 同值（0.15）＝开箱即不额外收紧，
    # 真正起作用是被档位压到 0.10（`shared/risk/tiers.py` 的 per_stock_pct）时。
    "l1.per_order_pct": {"max_pct": 0.15},
    # 当日新开仓标的上限：3 与隔壁风险预算 calm 档同值（caution 2 / defensive 1 由档位压）
    "l1.new_buys_per_day": {"max_new_buys": 3},
    # 总敞口上限（全链路唯一一条"总额"闸）。1.0 = 现金账户的自然上限（持仓市值 > 权益
    # 即融资）。这 1.0 是**配置侧**上限；实际生效值还要与动态档位层取更严者（见
    # `shared/risk/tiers.py`），但档位只收紧、不放宽，故此处是配置兜底而非最终值。
    "l1.leverage_cap": {"max_leverage": 1.0},
    "l1.daily_loss_limit": {"max_loss_pct": 3.0},
    "l3.max_order_value": {"max_value": 1_000_000.0},
    "l3.price_deviation": {"max_dev": 0.02, "sanity_max_dev": 0.20},
    "l3.stale_quote": {"max_age_s": 5.0},
    "l3.order_frequency": {"max_per_minute": 60},
    "l3.cancel_ratio": {"max_ratio": 0.40, "min_orders": 10},
    "l3.lot_size": {"default_lot": 100, "star_lot": 200},
    "l6.book_invalid": {},
}

_FORCED_EXIT_PREFIXES = (
    "sltp:",
    "flatten:",
    "forced-exit:",
    "forced_exit:",
    "flat-",
    "mir-",
)

_CORE = RiskGateCore()
_quote_client: Any = None  # 远端行情 Redis（懒建；快照读用）


def _client(redis: Any):
    """兼容 RedisClient 包装（.client）与原生 redis 客户端（与 tiers 同款）。

    `callable` 那半句是必需的：**原生 `redis.Redis` 自己有一个 `client()` 方法**
    （redis-py 的连接工厂），裸 `getattr(redis, "client", redis)` 会把这个方法当成
    包装拆出来，调用方随后在方法对象上 `.hgetall`（2026-09-23 定档首次真跑在
    tiers 侧踩中同款）。包装的 `.client` 是实例属性（真客户端对象，不可调用）。
    """
    client = getattr(redis, "client", None)
    return client if client is not None and not callable(client) else redis


def _date_key(template: str) -> str:
    return template.format(date=datetime.now(tz=CST).strftime("%Y%m%d"))


# ── 配置 ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RiskConfig:
    enabled: bool = False
    shadow: bool = True
    version: int = 0
    rules: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 档位（`shared/risk/tiers.py`）：level/source 进留痕，供事后回答"这单是在哪个档位下判的"；
    # applied = 档位实际改写了哪些规则参数（空 = 档位未生效或未收紧任何东西）。
    tier_level: str = ""
    tier_source: str = ""
    tier_applied: dict[str, dict[str, Any]] = field(default_factory=dict)


def _as_bool(v: Any, default: bool) -> bool:
    s = str(v if v is not None else "").strip().lower()
    if not s:
        return default
    return s not in {"0", "false", "no", "off"}


def load_config(redis: Any) -> RiskConfig | None:
    """读配置。键不存在 → None（视为未启用，放行并计数 config_missing）；读失败 → 抛（fail-closed）。"""
    raw = _client(redis).hgetall(CONFIG_KEY) or {}
    if not raw:
        return None
    rules: dict[str, dict[str, Any]] = {}
    try:
        rules = json.loads(raw.get("rules") or "{}") or {}
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"qm:risk:config.rules 解析失败: {exc}") from exc
    try:
        version = int(raw.get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    from backend.shared.risk.tiers import apply_to_rules, load_tier

    tier = load_tier(redis)  # 内部收敛一切失效姿态，绝不抛（见 tiers 模块 docstring）
    merged, applied, tier_problems = apply_to_rules(rules, tier)
    _warn_if_order_rate_reaches_hft(rules)
    _warn_if_unknown_rules(rules)
    # 档位启用的规则不算"配置漏配"——那是档位对买入侧的权威，不是错位
    _warn_if_missing_rules(rules, exempt=frozenset(applied))
    _warn_if_tier_problems(tier, applied, tier_problems)
    return RiskConfig(
        enabled=_as_bool(raw.get("enabled"), False),
        shadow=_as_bool(raw.get("shadow"), True),
        version=version,
        rules=merged,
        tier_level=tier.level,
        tier_source=tier.source,
        tier_applied=applied,
    )


def _warn_if_order_rate_reaches_hft(rules: dict[str, dict[str, Any]]) -> None:
    """下单频率配置撞上高频认定线时告警。

    只告警、不改配置、不拒绝加载：撞线不违法，但要额外向券商报告并接受更严监管，
    真正的风险是**用户不知道自己已经在那一侧**。判定与阈值见
    `shared/programmatic_trading_disclosure.py`（法规常量的唯一出处）。
    """
    rule = rules.get("l3.order_frequency") or {}
    log_high_frequency_warning(rule.get("max_per_minute"), source=f"redis:{CONFIG_KEY}")


_unknown_rules_warned: frozenset[str] = frozenset()


def _warn_if_unknown_rules(rules: dict[str, dict[str, Any]]) -> None:
    """配置里有本进程注册表不认识的规则 id → 告警（只在集合变化时打一次）。

    这是「配置与代码版本错位」的探针。`RiskGateCore.evaluate` 按**注册表**遍历
    （`for spec in self._specs`），配置里多出来的条目会被**静默忽略**：往 Redis 写了
    新规则的配置、而服务进程还跑着旧代码时，闸门看着"已启用"，实际一次都不会执行。
    留痕同样看不出来——跳过不产生 decision，于是「没拦过」与「根本没跑过」长得
    一模一样（正是 `l1.leverage_cap` 这种拦下来才留痕的规则最危险的失效形态）。
    只告警、不改配置、不拒加载：配置本身没错，错的是进程版本。
    """
    global _unknown_rules_warned
    unknown = frozenset(r for r in rules if get_rule(r) is None)
    if unknown and unknown != _unknown_rules_warned:
        logger.error(
            "[RiskGate] 配置含未注册规则 %s —— 本进程代码不认识，执行时被静默跳过。"
            "多半是配置已更新而服务进程未重启（热加载＝kill -TERM 该服务子进程，看门狗重拉）",
            sorted(unknown),
        )
    _unknown_rules_warned = unknown


_missing_rules_warned: frozenset[str] = frozenset()


def _warn_if_missing_rules(
    rules: dict[str, dict[str, Any]], *, exempt: frozenset[str] = frozenset()
) -> None:
    """``DEFAULT_RULES`` 里有、而配置里**没有**的规则 id → 告警（只在集合变化时打一次）。

    ``_warn_if_unknown_rules`` 的反向探针：那边查"配置比代码新"，这边查"**代码比配置新**"。
    `RiskGateCore.evaluate` 只跑配置显式列出的规则（``always_on`` 除外），所以新增一条
    规则、代码上了线、却没人往 ``qm:risk:config.rules`` 补条目时，新规则**一次都不会
    执行**；留痕里"没拦过"与"根本没跑过"依旧长得一模一样（决策流的 ``checked`` 会诚实
    地不含它，前提是有人想到去比对）。

    只告警、不改配置、不拒加载：加规则是**配置变更**（走 ``risk_ctl`` 的 setdefault 路径，
    由用户拍板），本函数只保证这个错位不无声。空配置不告警——整层未启用是有意为之。

    ``exempt`` = 档位层**刚刚启用**的规则 id：那是档位对买入侧的权威行为（有意），
    不是"漏配"，报出来只会制造噪声、掩盖真正的漏配。
    """
    global _missing_rules_warned
    if not rules:
        _missing_rules_warned = frozenset()
        return
    missing = frozenset(
        r
        for r in DEFAULT_RULES
        if r not in rules
        and r not in exempt
        and not getattr(get_rule(r), "always_on", False)
    )
    if missing and missing != _missing_rules_warned:
        logger.warning(
            "[RiskGate] 配置缺少代码已内置的规则 %s —— 未列出即不执行（always_on 除外）。"
            "有意不启用则忽略；若是新增规则漏配，用 risk_ctl 的 setdefault 补条目",
            sorted(missing),
        )
    _missing_rules_warned = missing


_tier_warned: frozenset[str] = frozenset()


def _warn_if_tier_problems(
    tier: Any, applied: dict[str, dict[str, Any]], problems: tuple[str, ...]
) -> None:
    """档位层的失效/降级 → 告警（同集合只打一次，不逐单刷屏）。

    这是档位层的**暴露面**：档位读不到、过期、结构坏、缺键、映射未接……全部在
    这里变得可见。少了它，档位层的失效形态就是"静默按更松的参数跑"——本层存在的
    理由恰恰是消灭这个形态（隔壁 09-18 的"档位日期早于应定档日，照常 fail-open"）。
    档位未配置（absent）不告警：那是"没启用"，不是"坏了"。
    """
    global _tier_warned
    if tier.source in ("absent", "") and not problems:
        _tier_warned = frozenset()
        return
    marks = frozenset(
        [f"source={tier.source}"] + list(problems) + [f"applied={sorted(applied)}"]
    )
    if marks != _tier_warned:
        if tier.source in ("stale", "fallback"):
            logger.warning(
                "[RiskTier] 档位不可信（source=%s，level=%s）——已按买入侧防守参数收紧；"
                "问题：%s",
                tier.source,
                tier.level or "-",
                list(problems) or "无",
            )
        elif problems:
            logger.warning("[RiskTier] 档位应用有问题：%s", list(problems))
        if applied:
            logger.info(
                "[RiskTier] 档位 %s（%s）收紧生效：%s",
                tier.level or "-",
                tier.source,
                {rid: p for rid, p in applied.items()},
            )
    _tier_warned = marks


# ── 上下文构建 ───────────────────────────────────────────────────────


def _is_forced_exit(remarks: str | None) -> bool:
    r = str(remarks or "").strip().lower()
    return any(r.startswith(p) for p in _FORCED_EXIT_PREFIXES)


def _quote_snapshot(symbol: str) -> dict[str, Any]:
    """远端行情快照（best-effort；失败返回 {}）。"""
    global _quote_client
    try:
        if _quote_client is None:
            from backend.shared.remote_quote_config import make_sync_client

            _quote_client = make_sync_client()
        if _quote_client is None:
            return {}
        from backend.shared.stock_utils import StockCodeUtil

        prefix = (StockCodeUtil.to_prefix(symbol) or symbol).lower()
        return _quote_client.hgetall(f"market:snapshot:{prefix}") or {}
    except Exception:  # noqa: BLE001
        return {}


def _f(v: Any) -> float | None:
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def _positions_lookup(positions: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    """持仓键容错查找（前缀/后缀/纯数字三形态）。"""
    if not positions:
        return None
    if symbol in positions:
        return positions[symbol]
    from backend.shared.stock_utils import StockCodeUtil

    for form in (StockCodeUtil.to_prefix(symbol), StockCodeUtil.to_suffix(symbol)):
        if form and form in positions:
            return positions[form]
    return None


def _sum_position_value(rows: Any) -> float | None:
    """持仓合计市值（元）—— 总杠杆闸（``l1.leverage_cap``）的分子。

    口径与 sim 引擎 Lua 的 ``total_market_value = Σ(volume × price)`` 对齐。与"账户自报
    合计"（真账户快照的 ``market_value`` **列** / 模拟账户的顶层 ``market_value``）的实测
    关系（2026-09-23）：模拟账户两者逐分相等；真账户 qmt 逐分相等、tdx 逐行大 312–521 元
    （~0.6%——其行市值由 QuantDB 收盘价回填、非券商 mark）。两口径**取大**由调用方完成
    （见真/模拟账户分支），本函数只负责"逐行口径"本身。

    - **跳过 0 量行**：真账户快照含已清仓历史行（P0.4 那 4 个幻影标的即此类），实测
      市值为 0.0；但一旦出现残留市值就会虚增分子、**误拒买单**。判 `== 0` 而非 `> 0`
      ——负 volume 是空头，同样是敞口，不能漏。
    - **取绝对值**：分母 ``total_assets`` = 现金 + 持仓市值 = 净资产，故分子按**总敞口**
      计；空头净掉多头会低估风险。（A 股现无融券、sim 负量即删，此处是口径正确性而非现状。）
    - **缺 ``market_value`` 即返回 None**（交规则 fail-closed），**不**沿用
      ``position_pct`` 那种静默跳过：总杠杆闸是全栈唯一的总敞口上限，分子少算
      = 闸门名存实亡且悄无声息；宁可拒买（响亮、可恢复）。
    - **容器或元素形态不认识同样返回 None**：``'[{"volume":100}]'`` 这种双层编码的 JSON
      字符串、``{"SH600036": "oops"}`` 这类值不是 dict 的映射，迭代下去全被当"非 dict"
      跳过 → 合计 0.0，与"空仓"长得一模一样——那是**静默关闸**。唯一容忍的是 ``None``
      元素（空行占位）。

    入参接受 ``list[dict]``（真账户 payload）或 ``dict[str, dict]``（模拟账户映射）。
    """
    if rows is None:
        return None  # 结构不可得 ≠ 没有持仓：与"空表 = 合计 0"必须区分
    # 非容器类型（如双层编码的 JSON 字符串 ``'[{"volume":100,...}]'``）**必须**拒掉：
    # 迭代字符串得到的是一个个字符，全被当作非 dict 跳过 → 合计 0.0，与"空仓"长得一模一样
    # —— 那是**静默关闸**（本规则是全栈唯一总敞口上限）。实测 `'[]'` / `'[{"volume":…}]'`
    # / `{"SH600036": "oops"}` 三形态都会落到 0.0，故在此显式 fail-closed。
    if not isinstance(rows, (list, tuple, dict)):
        return None
    if isinstance(rows, dict):
        rows = rows.values()
    total = 0.0
    for p in rows or ():
        if p is None:
            continue  # 空行占位（如 `[None]`）：与"没有持仓"同义，容忍
        # 元素形态不认识（字符串/数字/嵌套列表）= 结构不是我们认识的那个 → fail-closed。
        # **不能** continue：那正是 M3 那个静默 0.0 的元素级版本。
        if not isinstance(p, dict):
            return None
        if _f(p.get("volume")) == 0.0:
            continue
        mv = _f(p.get("market_value"))
        if mv is None:
            return None
        total += abs(mv)
    return total


def _last_close_fallback(symbol: str) -> float | None:
    """最近一根已收盘日线（QuantDB 前复权）——快照缺失时金额类校验的兜底价。

    只用于金额估算（资金/占比/单笔上限）；行情时效由 queued_intent 语义单独裁定。
    单测可 monkeypatch 本函数（同步执行，调用方 to_thread）。
    """
    try:
        from datetime import date as _date, timedelta as _td

        from backend.services.trade.services.sentinel_backfill import (
            INDEX_SYMBOLS,
            _dt_int,
            _hub,
        )

        text = str(symbol or "").strip().upper()
        view = "qdb_index_daily" if text in INDEX_SYMBOLS else "qdb_daily_forward"
        if text not in INDEX_SYMBOLS:
            from backend.shared.stock_utils import StockCodeUtil

            text = StockCodeUtil.to_suffix(text) or text
        start = _date.today() - _td(days=15)
        df = _hub().fetch_series(
            view, text, _dt_int(start), _dt_int(_date.today()), columns=["close"]
        )
        if df is None or df.empty:
            return None
        rows = df.dropna(subset=["close"]).sort_values("dt")
        if rows.empty:
            return None
        close = float(rows.iloc[-1]["close"])
        return close if close > 0 else None
    except Exception:  # noqa: BLE001 - 兜底失败=价格不可得（金额规则 fail-closed）
        return None


async def _real_daily_pnl_pct(
    db: Any,
    *,
    tenant: str,
    account_id: str,
    snapshot_date: Any,
    today_cst: Any,
    total_asset: float | None,
) -> float | None:
    """真账户当日盈亏（%）——分子用调用方那行快照的权益，分母取日度台账的日初权益。

    **作用域按 account_id 精确匹配**（不是按 user 别名）：快照行与台账行由同一写入方
    用同一个 ``account_id`` 落库，而同一 (tenant,user) 下 tdx/qmt 是两座互不相交的
    真账户——按 user 找台账会在两座账户之间掷硬币。
    ``snapshot_date != today_cst`` 直接返回 None（历史某天的盈亏不是今天的），
    连查询都不发。任何异常 → None（该规则按无依据放行，绝不猜 0.0）。
    """
    from backend.services.trade.services.real_account_ledger_service import (
        resolve_daily_pnl_pct,
    )

    if not account_id or snapshot_date is None or snapshot_date != today_cst:
        return None
    try:
        from sqlalchemy import text as _sql_text

        row = (
            await db.execute(
                _sql_text(
                    "SELECT day_open_equity FROM real_account_ledger_daily_snapshots "
                    "WHERE tenant_id = :t AND account_id = :a AND snapshot_date = :d "
                    "ORDER BY last_snapshot_at DESC, id DESC LIMIT 1"
                ),
                {"t": tenant, "a": account_id, "d": snapshot_date},
            )
        ).fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[RiskGate] 日度台账读取失败（l1.daily_loss_limit 将按无依据放行）"
            " tenant=%s account=%s: %s",
            tenant,
            account_id,
            exc,
        )
        return None
    if not row:
        logger.warning(
            "[RiskGate] 日度台账无当日行（l1.daily_loss_limit 将按无依据放行）"
            " tenant=%s account=%s date=%s",
            tenant,
            account_id,
            snapshot_date,
        )
        return None
    return resolve_daily_pnl_pct(total_asset=total_asset, day_open_equity=row[0])


async def build_context(
    req: Any,
    *,
    db: Any,
    redis: Any,
    need_counts: bool = False,
    need_daily_pnl: bool = False,
) -> RiskContext:
    """OrderRequest → RiskContext（纯读；任何子项失败仅缺省该字段并留痕于 evidence）。

    ``need_counts`` / ``need_daily_pnl`` = "配置里启用了对应规则"，只在为真时才发那几条
    查询——规则没启用时读到的数字没有任何消费者，白付一次库往返。
    """
    now_ts = time.time()
    side = str(getattr(req, "side", "") or "").strip().upper()
    symbol = str(getattr(req, "symbol", "") or "")
    qty = int(float(getattr(req, "quantity", 0) or 0))
    price = _f(getattr(req, "price", None))
    order_type = str(getattr(req, "order_type", "market") or "market").strip().upper()
    remarks = getattr(req, "remarks", None)
    uid = int(getattr(req, "user_id", 0) or 0)
    tenant = str(getattr(req, "tenant_id", "") or "default")
    trading_mode = str(getattr(req, "trading_mode", "") or "").upper()
    source = str(getattr(req, "source", "") or "")

    # 盘后入队语义（2026-09-18 影子实测修复）：OrderRouter 盘后接单进入 pending 队列，
    # 下一交易时段由派发环节申报——时段/行情时效约束不应在入队时刻拒绝；强平类不适用。
    try:
        from backend.shared.risk.builtin_rules import CN_SESSION_DEFAULT, _hm_ok

        in_window = _hm_ok(datetime.now(tz=CST).strftime("%H:%M"), CN_SESSION_DEFAULT)
    except Exception:  # noqa: BLE001 - 判定失败按在场处理（保守：走 reject 路径）
        in_window = True
    forced_exit = _is_forced_exit(remarks)
    queued_intent = (not in_window) and (not forced_exit) and side in ("BUY", "SELL")

    # 急停（fail-closed：读失败按已急停）
    kill = False
    try:
        from backend.services.live_trading.services.real_mirror_service import (
            kill_switch_on,
        )

        kill = bool(kill_switch_on(redis))
    except Exception:  # noqa: BLE001
        kill = True

    # 行情（快照优先；缺失回落到最近收盘供金额类规则——来源如实标注）
    snap = _quote_snapshot(symbol)
    last_price = _f(snap.get("Now"))
    ts = _f(snap.get("timestamp"))
    quote_age = (now_ts - ts) if ts and ts > 0 else None
    price_source = "snapshot" if (last_price and last_price > 0) else ""
    if not price_source:
        fallback_close = await asyncio.to_thread(_last_close_fallback, symbol)
        if fallback_close:
            last_price = fallback_close
            quote_age = None  # 兜底价无"快照时效"语义（时效规则按 queued_intent 裁定）
            price_source = "fallback_close"

    # 账户快照（best-effort）；REAL=真账户最近快照（TDX/QMT 直连接线），否则模拟账户
    available_cash = total_assets = position_pct = sellable = None
    total_position_value = (
        None  # 总杠杆闸的分子；两分支各自算出，缺则保持 None（fail-closed）
    )
    account_age_s: float | None = None  # 快照时点 → 判定时刻（秒）；None = 时点不可得
    account_source = ""  # 这些数字读自哪座账户（只作留痕，不参与判定）
    # 当日盈亏（%，负=亏）——`l1.daily_loss_limit` 的唯一输入，两分支各自算出。
    # None = 基线/权益不可得（规则据此放行）；**绝不用 0.0 顶替**（0.0 = 当日打平，
    # 那是"今天没亏"的假事实，规则会照着它放行）。
    daily_pnl_pct: float | None = None
    # 当日零点（上海墙钟，aware）——账户块与频率计数块共用同一个"今天"，
    # 避免同一概念在同一函数里各算一遍（两处口径漂开时，没有任何断言拦得住）。
    day_start_cst = datetime.now(tz=CST).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if trading_mode == "REAL":
        try:
            from sqlalchemy import bindparam
            from sqlalchemy import text as _sql_text

            from backend.shared.real_positions import (
                active_broker_type,
                snapshot_source_for_broker,
            )
            from backend.shared.simulation_account_keys import (
                ledger_user_id_candidates,
            )
            from backend.shared.utc_datetime import as_utc, utc_now

            # ① 选源：**同一 (tenant,user) 下 tdx_bridge 与 qmt_exec 是两座互不相交的
            #    真实账户**（实测规模差 ~25 倍、持仓不重叠）。不限定 source 的"取最新一行"
            #    等于在两座账户间掷硬币——拿 A 的权益判 B 的杠杆，比不判更危险。
            #    本单来源自带券商（tdx_*/qmt_*）就用它；不自带（manual/desk/copilot）
            #    退回平台自己的活跃券商解析（与持仓哨兵/账户页读的是同一只账户）。
            #    两个方向都用 shared/real_positions 的规范映射（禁止在这里另写一份）。
            snap_source = snapshot_source_for_broker(source)
            if not snap_source:
                snap_source = snapshot_source_for_broker(active_broker_type())
            # ② 选户：user_id 与落库侧同口径。历史别名（00000001 / 1 / 0 / admin）**必须**
            #    能互相读到，否则本闸对着空账户 fail-closed，把全部真单拦死；
            #    别名的唯一实现在 shared/simulation_account_keys（禁止手写）。
            sql = (
                "SELECT cash, total_asset, market_value, payload_json, snapshot_at, "
                "source, snapshot_date, account_id FROM real_account_snapshots "
                "WHERE tenant_id = :t AND user_id IN :u"
                + (" AND source = :s" if snap_source else "")
                + " ORDER BY snapshot_at DESC, id DESC LIMIT 1"
            )
            stmt = _sql_text(sql).bindparams(bindparam("u", expanding=True))
            params: dict[str, Any] = {
                "t": tenant,
                "u": ledger_user_id_candidates(uid),
            }
            if snap_source:
                params["s"] = snap_source
            row = (await db.execute(stmt, params)).fetchone()
            if row:
                available_cash = _f(row[0])
                total_assets = _f(row[1])
                col_market_value = _f(row[2])
                account_source = str(row[5] or "")
                # ③ 时点：该列是 naive UTC（写入侧惯例）。**不能**用 SQL 侧
                #    ``now() - snapshot_at`` 算年龄——会话时区是 Asia/Shanghai，会凭空
                #    多出 8 小时；也不能对 None 直接调 as_utc（None → utc_now()，
                #    年龄会假成 0.0，即把"时点不可得"伪装成"刚更新"）。
                snap_at = row[4]
                if snap_at is not None:
                    account_age_s = (utc_now() - as_utc(snap_at)).total_seconds()
                payload = (
                    row[3] if isinstance(row[3], dict) else json.loads(row[3] or "{}")
                )
                from backend.shared.stock_utils import StockCodeUtil

                target = (StockCodeUtil.to_suffix(symbol) or symbol).upper()
                for p in payload.get("positions") or []:
                    if str(p.get("symbol") or "").strip().upper() == target:
                        sellable = int(float(p.get("available_volume") or 0))
                        mv = _f(p.get("market_value"))
                        if total_assets and mv is not None:
                            position_pct = mv / total_assets
                        break
                # ④ 分子取两口径中的**较大者**：券商自报的账户级 ``market_value`` 列 ×
                #    payload 逐行重建值。取大 = 偏严（本闸是全栈唯一总敞口上限，"少算"
                #    是静默失效方向）。实测两座账户：qmt 两口径逐分相同；tdx 逐行口径大
                #    312–521 元（~0.6%，其行市值由 QuantDB 收盘回填、非券商 mark）。
                #    也不能只认列：实测 76,604 行里 2,753 行列值 = 0，其中确有 payload
                #    带持仓的行——只认列会在这些快照上把分子算成 0（静默关闸）。
                #    "结构不可得"（``positions`` 键缺失 / 非容器类型）不再直接判 None：
                #    列值在时它是券商自报的权威总额，用了不算静默（比 fail-closed 更准）；
                #    两口径都不可得才留 None 交规则拒买。
                #    "快照里没有持仓"（键在、表空或全 0 量）= 合计 0.0 可信——**不能**按
                #    "有没有 0 量以上的行"判：清仓后的账户恰好全是 0 量行，那样会把正常
                #    状态误判成快照故障、拦死买单。
                rows_mv = (
                    _sum_position_value(payload["positions"] or [])
                    if payload.get("positions") is not None
                    else None
                )
                candidates = [v for v in (col_market_value, rows_mv) if v is not None]
                total_position_value = max(candidates) if candidates else None
                # ⑤ 当日盈亏（`l1.daily_loss_limit` 的输入）。分子 = **本行**权益
                #    （与其他账户字段同一行、同一时刻），分母 = 日度台账的日初权益
                #    （`real_account_ledger_daily_snapshots.day_open_equity`，写侧口径
                #    是"上一交易日收盘权益"，与账户页 `today_pnl` 同源）。
                #    两个前提是刻意的：
                #    · 账户键取**这一行快照自己的** account_id——(tenant,user) 下
                #      tdx/qmt 是两座互不相交的真账户，按 user 找台账等于在两座之间
                #      掷硬币；而快照行与台账行由同一写入方用同一个 account_id 落库。
                #    · 快照日期不是**今天**（上海口径）就不算：桥断档时最新行仍是
                #      前几天的，照算得出的是"历史某天的当日盈亏"，拿它拦单=凭昨天的
                #      亏损拒今天的买单（或凭昨天的盈利放行今天的亏损）。
                #    读不到一律 None（规则据此放行），绝不猜 0.0。
                if need_daily_pnl:
                    daily_pnl_pct = await _real_daily_pnl_pct(
                        db,
                        tenant=tenant,
                        account_id=str(row[7] or ""),
                        snapshot_date=row[6],
                        today_cst=day_start_cst.date(),
                        total_asset=total_assets,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[RiskGate] 真账户快照读取失败（字段按缺省，fail-closed 语义由规则裁定）: %s",
                exc,
            )
    else:
        try:
            from backend.services.trade_shared.simulation_manager import (
                SimulationAccountManager,
            )

            manager = SimulationAccountManager(redis)
            account = await manager.get_account(uid, tenant)
            if account:
                account_source = "sim"
                # 模拟账户是**判定时刻现场读**的账本（不落快照表），年龄恒为 0——
                # 这不是"没测"，是"新鲜由构造保证"；写 None 会让陈旧告警误报。
                account_age_s = 0.0
                available_cash = _f(account.get("cash"))
                total_assets = _f(account.get("total_asset"))
                positions_all = account.get("positions") or {}
                pos = _positions_lookup(positions_all, symbol)
                if pos:
                    sellable = int(float(pos.get("available_volume") or 0))
                    mv = _f(pos.get("market_value"))
                    if total_assets and mv is not None:
                        position_pct = mv / total_assets
                # 总持仓市值（供 l1.leverage_cap）：与真账户分支**同一实现、同一口径**——
                # 同样取"账户自报合计"与"逐行重建"的较大者（取大=偏严；实测两者逐分
                # 相等，此处是口径对称而非行为差异）。账户创建时即带 `positions: {}`，
                # 故缺键属异常结构 → 该口径留 None（再由账户自报合计兜底）。
                rows_mv = (
                    _sum_position_value(account["positions"] or {})
                    if account.get("positions") is not None
                    else None
                )
                candidates = [
                    v
                    for v in (_f(account.get("market_value")), rows_mv)
                    if v is not None
                ]
                total_position_value = max(candidates) if candidates else None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[RiskGate] 账户快照读取失败（字段按缺省，fail-closed 语义由规则裁定）: %s",
                exc,
            )
        # 当日盈亏（`l1.daily_loss_limit` 的输入）：分子 = 本行的当前总资产，
        # 分母 = 模拟基金快照服务的日初基线（账户页 `today_pnl` / 策略播报**同一口径**：
        # "种子注入不计入盈亏"、新市场首日用种子等语义都在那边，禁止在这里另算一份）。
        # **独立 try**：失败要按自己的语义留痕——混进上面那句"账户快照读取失败"里，
        # 事后分不清是账户没读到还是基线没读到。
        if need_daily_pnl and total_assets:
            try:
                from decimal import Decimal

                from backend.services.simulation.services.fund_snapshot_service import (
                    SimulationFundSnapshotService,
                )
                from backend.services.trade.services.real_account_ledger_service import (
                    resolve_daily_pnl_pct,
                )

                # 初始资金取用户级 settings（账户页 CN 分支 / 策略播报同一取法；
                # 它同时是 `get_baselines` 在"无历史"时的基线兜底）
                settings = await manager.get_settings(user_id=uid, tenant_id=tenant)
                baselines = await SimulationFundSnapshotService.get_baselines(
                    tenant_id=tenant,
                    user_id=str(uid),
                    initial_capital=Decimal(str(settings.get("initial_cash") or 0.0)),
                    market="CN",
                )
                daily_pnl_pct = resolve_daily_pnl_pct(
                    total_asset=total_assets,
                    day_open_equity=float(baselines.get("day_open_equity") or 0.0),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[RiskGate] 模拟账户日初基线读取失败（l1.daily_loss_limit 将按无依据放行）: %s",
                    exc,
                )

    # 次数（仅启用了频率/撤单率/当日新开仓规则时才查库）；REAL=orders 真单表，否则 sim_orders
    orders_last_minute = orders_today = cancels_today = 0
    # 当日已成交买入的标的（去重）——`l1.new_buys_per_day` 的输入。
    # **保持 None 直到查询成功**：None = 计数不可得（该规则据此 fail-closed 拒新开仓），
    # 与 ()（今天还没买过）不是一回事，绝不预置成"空集合"。
    opened_today: tuple[str, ...] | None = None
    # 当日零点（上海墙钟，aware）——REAL 表是 naive 列、sim 表是 timestamptz，
    # 两种口径都从这一个"今天"派生（定义在账户块之前的 `day_start_cst`）
    if need_counts and trading_mode == "REAL":
        try:
            from sqlalchemy import text as _sql_text

            # orders.created_at/updated_at 为 naive UTC（写入侧惯例）——参数同口径
            day_start = day_start_cst.astimezone(timezone.utc).replace(tzinfo=None)
            minute_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
                seconds=60
            )
            row = (
                await db.execute(
                    _sql_text(
                        "SELECT "
                        " count(*) FILTER (WHERE created_at >= :day) AS today, "
                        " count(*) FILTER (WHERE created_at >= :minute) AS last_min, "
                        " count(*) FILTER (WHERE status = 'cancelled' AND updated_at >= :day) AS cancels "
                        "FROM orders WHERE tenant_id = :t AND user_id = :u "
                        "AND trading_mode::text = 'REAL'"
                    ),
                    {
                        "t": tenant,
                        "u": str(uid),
                        "day": day_start,
                        "minute": minute_ago,
                    },
                )
            ).fetchone()
            if row:
                orders_today = int(row[0] or 0)
                orders_last_minute = int(row[1] or 0)
                cancels_today = int(row[2] or 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[RiskGate] 真单频率计数查询失败: %s", exc)
        # 当日已成交买入的标的（去重）——`l1.new_buys_per_day` 的输入。
        # **独立 try**：失败语义与上面的计数相反（计数按 0 = 放行方向，这里按 None =
        # fail-closed 拒新开仓），混在一个 except 里日志就分不清哪种降级发生了。
        #
        # 为什么取**并集**（created_at ∪ filled_at）而不是二选一：orders 的两个时间列
        # 口径不同且都不可单独信任——created_at 是 naive UTC（本文件既有查询的惯例），
        # filled_at 的写入方口径不一（order_service / qmt_exec_reconciler 写
        # `datetime.now()`＝**上海墙钟**，tdx_push_service 写 submitted_at 派生值）。
        # 并集只会**多算**（收紧方向），不会把昨天的仓算成今天的空档；A 股成交只发生在
        # 09:30–15:00 CST，两个口径的当日零点都在它之前，不会互相误伤。
        #
        # 口径与隔壁 `daily_buy_codes` 对齐：判据是"**有成交量**的买单"
        # （那边是 `vol > 0`），不是"状态标签等于 filled"——部分成交同样已经开了仓。
        try:
            rows = (
                await db.execute(
                    _sql_text(
                        "SELECT DISTINCT symbol FROM orders "
                        "WHERE tenant_id = :t AND user_id = :u "
                        "AND trading_mode::text = 'REAL' "
                        "AND side::text = 'buy' AND filled_quantity > 0 "
                        "AND (created_at >= :day OR filled_at >= :day_cst)"
                    ),
                    {
                        "t": tenant,
                        "u": str(uid),
                        "day": day_start,
                        "day_cst": day_start_cst.replace(tzinfo=None),
                    },
                )
            ).fetchall()
            opened_today = tuple(str(r[0]) for r in rows if r and r[0])
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[RiskGate] 当日新开仓标的查询失败（l1.new_buys_per_day 将 fail-closed 拒新开仓）: %s",
                exc,
            )
    elif need_counts:
        try:
            from sqlalchemy import String, cast, func, or_, select

            from backend.services.simulation.models.order import SimOrder

            # sim_orders 的 created_at/filled_at 均为 timestamptz（aware）——
            # 同一零点即可覆盖两列，无需 REAL 那样的双口径并集
            day_start = day_start_cst
            base = (
                select(func.count())
                .select_from(SimOrder)
                .where(
                    SimOrder.tenant_id == tenant,
                    cast(SimOrder.user_id, String) == str(uid),
                )
            )
            orders_last_minute = int(
                (
                    await db.execute(
                        base.where(
                            SimOrder.created_at
                            >= datetime.now(timezone.utc) - timedelta(seconds=60)
                        )
                    )
                ).scalar()
                or 0
            )
            orders_today = int(
                (
                    await db.execute(base.where(SimOrder.created_at >= day_start))
                ).scalar()
                or 0
            )
            cancels_today = int(
                (
                    await db.execute(
                        base.where(
                            SimOrder.status == "cancelled",
                            SimOrder.cancelled_at >= day_start,
                        )
                    )
                ).scalar()
                or 0
            )
            # 当日已成交买入的标的（去重）——口径同 REAL 分支（"有成交量的买单"，
            # 见那边的说明），只是两列同为 aware，一个零点就够。
            # **内层 try**：失败语义与计数相反（计数 0 = 放行方向，这里 None =
            # fail-closed 拒新开仓），不能让日志把两种降级混成一句。
            try:
                rows = (
                    await db.execute(
                        select(SimOrder.symbol)
                        .where(
                            SimOrder.tenant_id == tenant,
                            cast(SimOrder.user_id, String) == str(uid),
                            SimOrder.side == "buy",
                            SimOrder.filled_quantity > 0,
                            or_(
                                SimOrder.created_at >= day_start,
                                SimOrder.filled_at >= day_start,
                            ),
                        )
                        .distinct()
                    )
                ).fetchall()
                opened_today = tuple(str(r[0]) for r in rows if r and r[0])
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[RiskGate] 当日新开仓标的查询失败（l1.new_buys_per_day 将 fail-closed 拒新开仓）: %s",
                    exc,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[RiskGate] 频率计数查询失败: %s", exc)

    amount = None
    if price is not None and qty:
        amount = price * qty
    elif last_price is not None and qty:
        amount = last_price * qty  # 市价单：以最新价估额（供资金/占比规则）

    return RiskContext(
        market="CN",
        symbol=symbol,
        side=side,
        order_type=order_type,
        price=price,
        quantity=qty,
        amount=amount,
        client_order_id=str(getattr(req, "client_order_id", "") or ""),
        forced_exit=forced_exit,
        strategy_id=str(getattr(req, "strategy_id", "") or ""),
        queued_intent=queued_intent,
        price_source=price_source,
        available_cash=available_cash,
        sellable_volume=sellable,
        total_assets=total_assets,
        total_position_value=total_position_value,
        account_age_s=account_age_s,
        account_source=account_source,
        daily_pnl_pct=daily_pnl_pct,
        position_pct=position_pct,
        last_price=last_price,
        quote_age_s=quote_age,
        orders_last_minute=orders_last_minute,
        orders_today=orders_today,
        cancels_today=cancels_today,
        opened_today=opened_today,
        now_ts=now_ts,
        kill_switch=kill,
    )


# ── 判定与留痕 ───────────────────────────────────────────────────────


async def check_direct_order(
    *,
    tenant_id: str,
    user_id: Any,
    symbol: str,
    side: str,
    quantity: float,
    price: float | None,
    order_type: str = "market",
    source: str = "tdx_bridge",
    remarks: str | None = None,
    redis_client: Any = None,
) -> RiskCheck:
    """直连路径（TDX 滚动/L2/QMT）过闸便捷入口：自建只读会话 + trade Redis。

    fail-closed 纪律与 OrderRouter 内嵌一致：判定异常/闸不可用 → passed=False（拒单），
    调用方应按拒单处理并留痕。影子期（默认）恒放行、判定照记。
    """
    raw_uid = str(user_id if user_id is not None else "").strip()
    uid = int(raw_uid) if raw_uid.isdigit() else 0
    req = DirectOrderReq(
        tenant_id=str(tenant_id or "default"),
        user_id=uid,
        symbol=str(symbol or ""),
        side=str(side or "").lower(),
        quantity=float(quantity or 0),
        price=float(price) if price else None,
        order_type=str(order_type or "market"),
        trading_mode="REAL",
        source=str(source or "tdx_bridge"),
        remarks=remarks,
    )
    if redis_client is None:
        from backend.services.trade_shared.redis_client import (
            get_redis as _get_trade_redis,
        )

        redis_client = _get_trade_redis()
        if getattr(redis_client, "client", None) is None:
            redis_client.connect()
    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as db:
        return await check_order(req, db=db, redis=redis_client)


@dataclass(frozen=True)
class RiskCheck:
    passed: bool
    enforced: bool = False  # True=强制模式下的拦截；False=影子放行
    rule_id: str | None = None
    reason: str = ""
    version: int = 0


@dataclass(frozen=True)
class RiskVerdict:
    """判定全貌（预检用）：比 RiskCheck 多出 `decisions` 全表与原始 verdict 词。

    「会不会被拦」与「被哪几条拦」是两件事：影子期下 `passed` 恒 True，只看它
    永远看不到 `l1.position_cap` 已经超了 —— 推送确认面板要的正是后者。
    """

    passed: bool
    verdict: str  # pass | warn | reject | halt | disabled | error
    enforced: bool = False
    rule_id: str | None = None
    reason: str = ""
    version: int = 0
    shadow: bool = False
    decisions: list[dict[str, Any]] = field(default_factory=list)


_PASS = RiskCheck(passed=True)

#: ``checked`` 字段长度上限（字符）。今日 15 条规则 ≈ 200 字符，留足余量。
_CHECKED_MAXLEN = 600


def _join_checked(checked: tuple[str, ...]) -> str:
    """``checked`` 编码：逗号连接；超长时按**完整 id** 截断并标注还差几条。

    截断必须可见：把被截断的清单当成完整清单读，会把"没跑过"误读成"跑过且放行"——
    恰好是本字段存在的意义（影子报告靠它统计每条规则的覆盖率）。``+Nmore`` 后缀
    即"还有 N 条没写下"；不留后缀的裸截断（旧实现 ``[:600]``）会截在 id 中间，
    产出半个 id，两种误读都指向"闸门看起来跑过"。
    """
    out: list[str] = []
    used = 0
    for rid in checked:
        if used + len(rid) + 1 > _CHECKED_MAXLEN - 12:  # 12 = "+9999more" 的上限
            return ",".join(out) + f",+{len(checked) - len(out)}more"
        out.append(rid)
        used += len(rid) + 1
    return ",".join(out)


def _record(
    redis: Any,
    req: Any,
    *,
    verdict: str,
    enforced: bool,
    version: int,
    decisions: list[dict[str, Any]] | None = None,
    error: str | None = None,
    checked: tuple[str, ...] | None = None,
    tier: RiskConfig | None = None,
) -> None:
    """决策留痕（best-effort：留痕失败不改变放行/拦截结果，但计数 errors）。

    ``checked`` = 该单实际跑过闸门的规则 id（引擎的 ``checked_rules``；编码见
    `_join_checked`）。与 ``decisions`` 的分工：后者只有**出结论**的规则（拦或告警），
    前者含**跑过且放行**的规则。影子期正是靠它回答「这条闸门到底评估了多少单」——
    只看 decisions，一条从没触发过的规则与一条根本没加载的规则，在留痕里长得一模一样。

    **有意不带 ``checked`` 的三条路径**（都没有跑过任何规则，不是遗漏）：
    配置不可读（verdict=error/`l0.config`）、未启用（verdict=disabled）、判定异常
    （verdict=error/`l0.evaluate`）。它们各自带 ``error`` 字段或独立的 verdict 值，
    读留痕时按 verdict 区分即可，不必也不该给它们编一份"跑过的规则"。

    ``tier`` = 该单判定时生效的档位（level/source + 档位改写的参数）。与 ``version``
    同性质：都是"这一单是在什么约束下判的"的审计坐标。档位未配置时**不写该字段**
    （absent 与"配了但没生效"在留痕里必须能区分，故只写非空值）。
    """
    try:
        client = _client(redis)
        pipe = client.pipeline(transaction=False)
        fields = {
            "ts": f"{time.time():.3f}",
            "tenant": str(getattr(req, "tenant_id", "") or "default"),
            "uid": str(getattr(req, "user_id", 0)),
            "symbol": str(getattr(req, "symbol", "") or ""),
            "side": str(getattr(req, "side", "") or ""),
            "qty": str(getattr(req, "quantity", 0)),
            "source": str(getattr(req, "source", "") or ""),
            "verdict": verdict,
            "enforced": "true" if enforced else "false",
            "version": str(version),
        }
        if decisions:
            fields["decisions"] = json.dumps(decisions, ensure_ascii=False)[:2000]
        if checked:
            fields["checked"] = _join_checked(checked)
        if error:
            fields["error"] = str(error)[:500]
        if tier is not None and tier.tier_source not in ("", "absent"):
            # 判据是 **source** 而不是 level：结构坏掉的档位没有档位名（level=""），
            # 按 level 判会把"档位坏了"写成和"没配过档位"一模一样——那正是本层要
            # 消灭的形态（`-` 表示读侧拿不到可信档位名）
            fields["tier"] = f"{tier.tier_level or '-'}/{tier.tier_source}"[:80]
            if tier.tier_applied:
                fields["tier_applied"] = json.dumps(
                    tier.tier_applied, ensure_ascii=False
                )[:500]
        pipe.xadd(
            _date_key(DECISIONS_KEY), fields, maxlen=DECISIONS_MAXLEN, approximate=True
        )
        metrics = _date_key(METRICS_KEY)
        pipe.hincrby(metrics, "evaluated", 1)
        if verdict == "reject":
            pipe.hincrby(metrics, "rejected", 1)
            if not enforced:
                pipe.hincrby(metrics, "shadow_rejected", 1)
        elif verdict == "halt":
            pipe.hincrby(metrics, "halted", 1)
        elif verdict == "warn":
            pipe.hincrby(metrics, "warned", 1)
        for d in decisions or []:
            rid = str(d.get("rule_id") or "")
            if rid:
                pipe.hincrby(metrics, f"r:{rid}", 1)
        if tier is not None and tier.tier_source not in ("", "absent"):
            # 影子报告要能回答"今天多少单是在防守档下判的"——档位是判定坐标的一部分；
            # 不可信的档位（stale/fallback）单独分桶，且**不能**因为没有档位名就不计数
            pipe.hincrby(metrics, f"tier:{tier.tier_level or 'unknown'}", 1)
            if tier.tier_source in ("stale", "fallback"):
                pipe.hincrby(metrics, f"tier_source:{tier.tier_source}", 1)
        if error:
            pipe.hincrby(metrics, "errors", 1)
        pipe.expire(metrics, 35 * 86400)
        pipe.execute()
    except Exception as exc:  # noqa: BLE001 - 留痕失败不改判
        logger.warning("[RiskGate] 决策留痕失败: %s", exc)


async def evaluate_order(
    req: Any, *, db: Any, redis: Any, record: bool = True
) -> RiskVerdict:
    """风控判定**唯一实现**。`record=False` 即预检：同一套规则、同一份上下文，但不落留痕。

    预检必须走 `record=False`：`_record` 每次调用都会 `hincrby evaluated`，逐笔预检一次
    10 只候选就等于往当日 metrics 里灌 10 次判定，影子报告会显示「今天拦了 N 单」而
    实际一单未发 —— 那是把「没发生的事」写进了证据。见 `preflight_order`。
    """
    try:
        cfg = load_config(redis)
    except Exception as exc:  # noqa: BLE001 - 配置不可读 = fail-closed
        if record:
            _record(
                redis,
                req,
                verdict="reject",
                enforced=True,
                version=0,
                error=f"config: {exc}",
            )
        return RiskVerdict(
            passed=False,
            verdict="error",
            enforced=True,
            rule_id="l0.config",
            reason=f"风控配置不可读（fail-closed）: {exc}"[:180],
        )
    if cfg is None or not cfg.enabled:
        # 未启用：不判定不拦单（计数一次 disabled，便于运维确认部署状态）
        if record:
            _record(redis, req, verdict="disabled", enforced=False, version=0)
        return RiskVerdict(passed=True, verdict="disabled")

    try:
        need_counts = any(
            k in cfg.rules
            for k in (
                "l3.order_frequency",
                "l3.cancel_ratio",
                "l1.new_buys_per_day",
            )
        )
        ctx = await build_context(
            req,
            db=db,
            redis=redis,
            need_counts=need_counts,
            need_daily_pnl="l1.daily_loss_limit" in cfg.rules,
        )
        verdict = _CORE.evaluate(ctx, cfg.rules, version=cfg.version)
    except Exception as exc:  # noqa: BLE001 - 判定异常 = fail-closed
        if record:
            _record(
                redis,
                req,
                verdict="reject",
                enforced=True,
                version=cfg.version,
                error=f"evaluate: {exc}",
            )
        return RiskVerdict(
            passed=False,
            verdict="error",
            enforced=True,
            rule_id="l0.evaluate",
            reason=f"风控判定异常（fail-closed）: {exc}"[:180],
            version=cfg.version,
        )

    decisions = [
        {
            "rule_id": d.rule_id,
            "level": d.level,
            "action": d.action,
            "reason": d.reason,
            "evidence": dict(d.evidence),
        }
        for d in verdict.decisions
    ]
    if verdict.halt:
        v, primary = "halt", next((d for d in decisions if d["action"] == "HALT"), None)
    elif verdict.rejects:
        v, primary = "reject", decisions[0] if decisions else None
    elif verdict.warns:
        v, primary = "warn", None
    else:
        v, primary = "pass", None

    enforced = (not cfg.shadow) and v in ("reject", "halt")
    if record:
        _record(
            redis,
            req,
            verdict=v,
            enforced=enforced,
            version=cfg.version,
            decisions=decisions,
            checked=verdict.checked_rules,
            tier=cfg,
        )

    if cfg.shadow or v == "pass" or v == "warn":
        return RiskVerdict(
            passed=True,
            verdict=v,
            enforced=False,
            version=cfg.version,
            shadow=bool(cfg.shadow),
            decisions=decisions,
        )
    rule_id = str((primary or {}).get("rule_id") or "risk")
    reason = str((primary or {}).get("reason") or "风控拦截")
    if record:
        logger.warning(
            "[RiskGate] 拒单 %s %s: [%s] %s",
            getattr(req, "side", ""),
            getattr(req, "symbol", ""),
            rule_id,
            reason,
        )
    return RiskVerdict(
        passed=False,
        verdict=v,
        enforced=True,
        rule_id=rule_id,
        reason=reason,
        version=cfg.version,
        shadow=bool(cfg.shadow),
        decisions=decisions,
    )


async def check_order(req: Any, *, db: Any, redis: Any) -> RiskCheck:
    """OrderRouter 内嵌调用点：返回 RiskCheck（passed=False 即拒单）。"""
    v = await evaluate_order(req, db=db, redis=redis, record=True)
    if v.verdict == "disabled":
        return _PASS
    return RiskCheck(
        passed=v.passed,
        enforced=v.enforced,
        rule_id=v.rule_id,
        reason=v.reason,
        version=v.version,
    )


async def preflight_order(req: Any, *, db: Any, redis: Any) -> RiskVerdict:
    """推送前预检：判定照跑、**不落留痕**，并回传 decisions 全表。

    与真实下单共用 `evaluate_order`，所以「预检说会过、下单却被拒」只可能来自
    下单那一刻的上下文变化（时段推移、资金变化），不会是两套口径。
    """
    return await evaluate_order(req, db=db, redis=redis, record=False)
