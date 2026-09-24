"""QMT 止盈/止损执行器：触发即用**保护价**下真单，保证"一定要卖"能落地。

背景与实测依据见 ``docs/QMT止损执行器与镜像链路完善计划.md``。核心决策：

* 触发规则复用 :func:`tdx_quote_feed.check_sltp_trigger`（与 TDX 桥 ``stop_loss_daemon``
  同口径），避免两套语义。
* 卖出保护价默认 ``aggressive`` = ``max(跌停价, 现价 × 0.99)``——
  **可成交且合法的最激进报价**。
  ⚠️ 2026-09-21 更正：旧实现直接报跌停价（``DownStopPrice``），依据是「挂 2.21
  成交 2.34」的实测；该结论只对当时那只票的工况（近跌停）成立，已**被证伪**——
  报跌停价属**越界申报** → 柜台废单，当日 002074 按跌停价报的 42 笔真单全废、
  0 成交，并形成「触发→废单→重布防→再触发」每 2 分钟一笔的死循环。
  口径单源在 :func:`lot_rules.aggressive_sell_price`。
* **不做追价/撤单重挂**：保护价已是当日最激进可报价，重挂只会丢队列优先级。
* 走内部真单链路（落 ``orders`` 表 + ``qmt_exec_poller`` 回收），天然绕过镜像闸门。
* 数量默认取柜台 ``can_use_volume`` 全量（全量卖出允许碎股）；部分卖出按板块整手对齐。
* 一次触发当日只执行一次（``armed → triggered → submitted → filled/…``），
  ``POST /reset`` 或改规则后重新武装。

**P1.3 三项扩展**（LLM 条件单口径：绝对价 / 棘轮 / 部分减仓）：

* ``stop_loss_price`` —— 绝对价硬止损（支撑位是价位，不是百分比）。
  与 ``stop_loss_pct`` 同时配置时取**更高（更紧）**的那条线（见 ``exit_rules``）。
* ``move_stop_trigger`` + ``move_stop_to`` —— **条件棘轮**：现价上触触发价即把防守
  抬到目标价（「上过 105 就把防守抬到保本」）。只升不降；``move_stop_to`` 必须低于
  ``move_stop_trigger``（否则武装即触发 → 整条拒绝）。
  抬高的防守位**跨交易日保留**（持仓拿几周是常态），但持仓一旦换了
  （成本价变动超阈值 / 总量变大）立即作废并告警——否则新开的仓会被上一个仓的
  防守位秒杀。``POST /reset`` 一并清掉，回到规则配置的初始防守位。
* ``reduce_pct`` —— 部分减仓（如 0.33 = 减三分之一，按板块整手对齐）。
  **单次触发语义不变**：减仓后规则即终态，剩余仓位要重新武装才继续受保护
  （通知里明说）；分档减仓由调用方分多轮改规则实现，而不是一条规则反复触发。

**配置纪律**：新字段任一项非法（``reduce_pct`` 越界、棘轮不成对/顺序反了、
绝对价非正）一律**整条拒绝**并记入 ``rejected_rules``（读时派生、不落盘），
绝不「清掉字段继续用」——把「减三分之一」静默执行成「全量卖出」是真金白银的错。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from backend.services.live_trading.services.lot_rules import (
    aggressive_sell_price,
    align_sell_quantity,
    is_full_position_sell,
    protect_sell_price,
)
from backend.services.live_trading.services.tdx_quote_feed import (
    check_sltp_trigger,
    load_sltp_config,
)
from backend.services.live_trading.services.trading_session import (
    TZ,
    is_trading_time,
    trade_date_str,
)
from backend.shared.exit_rules import ratchet_stop_price

logger = logging.getLogger(__name__)

CONFIG_KEY = "qmt:sltp:executor:config"
STATE_KEY = "qmt:sltp:executor:state"

#: 规则表容量上限（**单源**：API 契约与决策层写入端都引这里）。
#: 表是「一个标的一个状态位」的平表，撑爆它意味着后续人工改规则会被 API 拒收。
MAX_RULES = 50

#: 规则归属（P2.4）：``""`` = 人工（CLI / 控制面 API 挂的），``"llm:<agent>"`` = 决策层
#: 某个 agent 的整组（每轮整组替换，见 ``decision/watch_writer.py``）。
#: **测试与文档都按字符串**：不要引入 Enum——``str`` 子类的 ``==`` 在本仓踩过坑。
OWNER_MANUAL = ""
OWNER_LLM_PREFIX = "llm:"

# 规则状态机
ST_ARMED = "armed"
ST_TRIGGERED = "triggered"
ST_SUBMITTED = "submitted"
ST_PARTIAL = "partial"
ST_FILLED = "filled"
ST_CANCELLED = "cancelled"
ST_REJECTED = "rejected"
ST_FAILED = "failed"
ST_SKIPPED = "skipped"

_TERMINAL_STATES = {ST_FILLED, ST_CANCELLED, ST_REJECTED, ST_FAILED, ST_SKIPPED}
_LIVE_STATES = {ST_SUBMITTED, ST_PARTIAL}
_HISTORY_DAYS = 7

# ── 保护性重试（P4 附-①，见 docs/local/quant-trader-migration-plan.md「P4 附-c」）──
#: 触发失败的处置类别（``classify_dispatch_failure`` 的输出，重试与否的唯一判据）。
FAIL_PRICE = "price_reject"
FAIL_DETERMINISTIC = "deterministic"
FAIL_TRANSIENT = "transient"
FAIL_UNKNOWN = "unknown"
#: 暂时性失败当日自动重试上限（次/标的/日）：防「无限静默重试」。
_RETRY_MAX_PER_DAY = 3
#: 两次自动重试的最小间隔（秒）。轮询默认 3s——没有冷却挡着，3 次换代会在 9 秒内
#: 烧光当日预算，一次十秒级通道抖动之后就再也补不上这笔保护。
_RETRY_COOLDOWN_SEC = 60.0
#: 价格/参数类废单的拒因特征词（券商原话）。命中即回退人工，绝不自动重发同参数单
#: ——2026-09-21 的 42 笔越界价循环就是「自动重发同参数」的直接产物。
_PRICE_REJECT_MARKERS = ("废单", "涨跌幅", "价格超出", "无效价格", "价格非法")

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "user_id": "1",
    "tenant_id": "default",
    "poll_interval_sec": 3,
    "protect_price_mode": "aggressive",
    "pending_alert_sec": 300,  # 未成交告警 + 余量策略触发阈值（秒，0=立即）
    "remainder_policy": "alert_only",  # alert_only | cancel | requote_at_protect_price
    "close_reminder_sec": 300,  # 收盘前提醒窗口（秒）
    "rules": [],
}

# 保护价模式（**唯一白名单**，API 校验层引用此处，勿各写一份）：
#   aggressive  = max(跌停价, 现价 × 0.99)，默认可成交且合法的最激进报价
#   limit_floor = 原样报跌停价，**遗留**——仅「已封跌停要排队」时合法，其余越界废单
#   market      = 市价委托（柜台映射最新价）
VALID_PROTECT_MODES = ("aggressive", "limit_floor", "market")
DEFAULT_PROTECT_MODE = "aggressive"
VALID_REMAINDER_POLICIES = ("alert_only", "cancel", "requote_at_protect_price")


def _resolve_mode(cfg: dict[str, Any]) -> str:
    """配置 → 保护价模式（缺省/非法一律回落 ``aggressive``，绝不静默用遗留口径）。"""
    mode = str(cfg.get("protect_price_mode") or "").strip().lower()
    return mode if mode in VALID_PROTECT_MODES else DEFAULT_PROTECT_MODE


# A 股收盘（沪深连续竞价 15:00 截止）
_CLOSE_HOUR = 15
_CLOSE_MINUTE = 0
# 重挂价格判定：与保护价差异不超过该值视为同价（避免无意义撤挂丢失队列优先级）
_REQUOTE_PRICE_EPSILON = 0.01
# 连续多少轮拿不到该标的行情就告警（默认 3s 轮询 ≈ 30 秒；行情恢复后计数归零）
_TICK_MISS_ALERT_THRESHOLD = 10

DEFAULT_RULE: dict[str, Any] = {
    "symbol": "",
    # 归属标记（P2.4）：**必须在词表里**，否则 normalize_rule 会把 LLM 挂的规则
    # 静默洗成人工规则——下一轮整组替换就再也认不出自己那一组。
    "owner": OWNER_MANUAL,
    "enabled": True,
    "side": "SELL",
    "entry_price": None,
    "quantity": None,
    "stop_loss_pct": None,
    "take_profit_pct": None,
    "trailing_stop_pct": None,
    # P1.3：绝对价止损 / 条件棘轮（触发价 + 目标防守价）/ 部分减仓比例
    "stop_loss_price": None,
    "move_stop_trigger": None,
    "move_stop_to": None,
    "reduce_pct": None,
    # P1.3b：绝对价止盈（与 stop_loss_price 对称）
    "take_profit_price": None,
}

# 抬高的防守位与持仓对不上的判定阈值（P1.3）：
# 成本价变动超 0.5%（券商均价两位小数，正常波动不会这么大）或总量变大 → 视为换了持仓。
_STOP_ENTRY_TOLERANCE = 0.005

# 参与「规则级 float 转换」的字段（normalize_rule 与 API 契约共用一份清单）
_RULE_FLOAT_FIELDS = (
    "entry_price",
    "quantity",
    "stop_loss_pct",
    "take_profit_pct",
    "trailing_stop_pct",
    "stop_loss_price",
    "move_stop_trigger",
    "move_stop_to",
    "reduce_pct",
    "take_profit_price",
)


def rule_reject_reason(rule: dict[str, Any]) -> str:
    """P1.3 新字段的组合校验（**单源**：执行器与 API 契约都调它）。

    返回非空 = 该规则必须**整条拒绝**，不能「清掉非法字段接着用」：

    * ``reduce_pct`` 越界（如把 50% 写成 ``50``）→ 清掉它会退化成「全量卖出」，
      即把「减三分之一」执行成「清仓」；
    * 棘轮不成对 / ``move_stop_to > move_stop_trigger`` → 抬完立刻低于现价；
    * ``stop_loss_price`` / ``take_profit_price`` ≤ 0 → 无意义的触发线（关闭请给
      ``null``）；
    * ``quantity`` 与 ``reduce_pct`` 同给 → 绝对值与比例互斥，谁优先都是猜。

    ``move_stop_to == move_stop_trigger``（零间隙棘轮）**合法**——隔壁 LLM 决策
    语料里 ``move_stop`` 覆盖率 49.3% 都是这一形态，同轮去抖见 ``_apply_ratchet``。
    """
    rp_raw = rule.get("reduce_pct")
    rp = _to_float(rp_raw)
    if rp_raw is not None and (rp is None or not (0 < rp <= 1)):
        return f"reduce_pct={rp_raw!r} 非法（须在 (0,1]；拒绝整条而非回落全量卖出）"
    if rule.get("quantity") is not None and rp_raw is not None:
        return "quantity 与 reduce_pct 互斥（绝对值 vs 比例，二者只给一个）"
    trigger_raw, move_to_raw = rule.get("move_stop_trigger"), rule.get("move_stop_to")
    if (trigger_raw is None) != (move_to_raw is None):
        return "move_stop_trigger 与 move_stop_to 必须成对给出"
    if trigger_raw is not None:
        t, m = _to_float(trigger_raw), _to_float(move_to_raw)
        if t is None or m is None or not (0 < m <= t):
            return (
                f"move_stop_to={move_to_raw!r} 必须为正且不高于 "
                f"move_stop_trigger={trigger_raw!r}（高于则抬完立刻低于现价）"
            )
    sp_raw = rule.get("stop_loss_price")
    sp = _to_float(sp_raw)
    if sp_raw is not None and (sp is None or sp <= 0):
        return f"stop_loss_price={sp_raw!r} 非法（须为正数，关闭请给 null）"
    tp_raw = rule.get("take_profit_price")
    tp = _to_float(tp_raw)
    if tp_raw is not None and (tp is None or tp <= 0):
        return f"take_profit_price={tp_raw!r} 非法（须为正数，关闭请给 null）"
    return ""


# --------------------------------------------------------------------------
# 纯函数（可单测）
# --------------------------------------------------------------------------
def normalize_symbol(symbol: str) -> str:
    """任意口径 → 后缀式（``600036.SH``），规则表与状态键统一用它。"""
    raw = str(symbol or "").strip().upper()
    if not raw:
        return ""
    if "." in raw:
        return raw
    try:
        from backend.shared.stock_utils import StockCodeUtil

        suffix = StockCodeUtil.to_suffix(raw)
        if suffix:
            return suffix
    except Exception:  # noqa: BLE001 - 兜底不阻断
        pass
    return raw


def normalize_rule(raw: dict[str, Any]) -> dict[str, Any]:
    """清洗单条规则（未知字段忽略，数值安全转换）。"""
    rule = dict(DEFAULT_RULE)
    rule.update({k: v for k, v in (raw or {}).items() if k in DEFAULT_RULE})
    rule["symbol"] = normalize_symbol(rule.get("symbol"))
    side = str(rule.get("side") or "SELL").strip().upper()
    if side != "SELL":
        # 执行器是清仓语义：只允许卖出（买入会越止越买，建仓另走策略链路）
        logger.warning(
            "[SltpExec] 规则 side=%s 非法，按 SELL 处理: %s", side, rule["symbol"]
        )
        side = "SELL"
    rule["side"] = side
    for key in _RULE_FLOAT_FIELDS:
        value = rule.get(key)
        if value in ("", None):
            rule[key] = None
            continue
        try:
            rule[key] = float(value)
        except (TypeError, ValueError):
            rule[key] = None
    rule["enabled"] = bool(rule.get("enabled", True))
    # 归属一律**成串**（Redis 里手改过、老版本写过的可能是 null/数字）：
    # 归属不是数字也不是布尔，比较永远按归一后的字符串来。
    rule["owner"] = str(rule.get("owner") or "").strip()
    return rule


def llm_owner(agent: str) -> str:
    """agent 名 → 决策层规则归属标记；**空 agent 不构造**（返回 ``""``）。

    裸前缀 ``"llm:"`` 是危险的：它会让「任何没给 agent 名的写者」共用同一组，
    组内整组替换就变成彼此互删。调用方拿到 ``""`` 必须**拒绝写入**，不能当成人工组。
    """
    name = str(agent or "").strip()
    return f"{OWNER_LLM_PREFIX}{name}" if name else OWNER_MANUAL


def merge_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    raw = raw or {}
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({k: v for k, v in raw.items() if k in DEFAULT_CONFIG})
    # 兼容旧键：unfilled_alert_sec → pending_alert_sec（Phase 3.2 统一改名）
    if "pending_alert_sec" not in raw and "unfilled_alert_sec" in raw:
        cfg["pending_alert_sec"] = raw.get("unfilled_alert_sec")
    policy = str(cfg.get("remainder_policy") or "alert_only").strip().lower()
    cfg["remainder_policy"] = (
        policy if policy in VALID_REMAINDER_POLICIES else "alert_only"
    )
    cleaned = [
        normalize_rule(r) for r in (cfg.get("rules") or []) if isinstance(r, dict)
    ]
    rules: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    for rule in cleaned:
        reason = rule_reject_reason(rule)
        if reason:
            logger.warning("[SltpExec] 规则整条拒绝 %s: %s", rule.get("symbol"), reason)
            rejected.append({"symbol": rule.get("symbol") or "", "reason": reason})
            continue
        rules.append(rule)
    cfg["rules"] = [r for r in rules if r["symbol"]]
    #: 读时派生（每次 load_config 重算），不落盘——见 _persistable。
    cfg["rejected_rules"] = rejected
    cfg["enabled"] = bool(cfg.get("enabled"))
    return cfg


def trigger_config(
    rule: dict[str, Any], fallback: dict[str, Any] | None
) -> dict[str, Any]:
    """规则触发阈值：规则内显式值优先，缺省回落设置页（``load_sltp_config``）口径。

    设置页把「止损止盈」整个关掉（``enabled=False``）时不再回落到它的阈值——
    否则用户关掉的提醒会以「执行器缺省阈值」的名义继续触发真单。

    ``stop_loss_price`` / ``take_profit_price`` **无设置页回落**：绝对价是规则级信息
    （含棘轮抬高后的有效防守位，由调用方写进传入的 rule 副本），设置页没有对应字段。
    回落的话，用户调设置页里的止盈比例会让规则里的绝对价止盈线**悄悄变成另一条线**。
    """
    fb = fallback or {}
    if fb.get("enabled") is False:
        fb = {}
    cfg = {
        "stop_loss_pct": rule.get("stop_loss_pct")
        if rule.get("stop_loss_pct") is not None
        else fb.get("stop_loss_pct"),
        "take_profit_pct": rule.get("take_profit_pct")
        if rule.get("take_profit_pct") is not None
        else fb.get("take_profit_pct"),
        "trailing_stop_pct": rule.get("trailing_stop_pct")
        if rule.get("trailing_stop_pct") is not None
        else fb.get("trailing_stop_pct"),
        "stop_loss_price": _to_float(rule.get("stop_loss_price")),
        "take_profit_price": _to_float(rule.get("take_profit_price")),
        "highest_price": rule.get("highest_price"),
    }
    return cfg


def effective_stop_price(
    rule: dict[str, Any], state_item: dict[str, Any]
) -> float | None:
    """当前生效的**绝对防守价**：规则固定价与棘轮抬高值取更高（更紧）者。

    单源：执行器每轮评估、运维 CLI ``--evaluate`` 只读预演都走这里——
    两处各写一份 ``max(...)`` 的话，预演说「不触发」而实盘卖出只是时间问题。
    """
    candidates = [
        _to_float(rule.get("stop_loss_price")),
        _to_float((state_item or {}).get("stop_price")),
    ]
    return max([x for x in candidates if x], default=0.0) or None


def trigger_inputs(rule: dict[str, Any], state_item: dict[str, Any]) -> dict[str, Any]:
    """喂给 :func:`trigger_config` 的规则视图（叠加持仓态：最高价 + 有效防守位）。

    返回**新字典**，不改调用方的 rule（状态是持仓的，规则是配置的，两者不混写）。
    """
    return {
        **rule,
        "highest_price": (state_item or {}).get("highest_price"),
        "stop_loss_price": effective_stop_price(rule, state_item or {}),
    }


def plan_sell_quantity(
    symbol: str, rule: dict[str, Any], can_use: float
) -> tuple[float, str]:
    """计划卖出量（单源）：显式 ``quantity`` 优先，否则按 ``reduce_pct`` 取可用量比例。

    比例算出的是**零股**（1000 × 0.33 = 330），继续走 ``align_sell_quantity``
    做板别整手对齐——与手填量的口径完全一致（主板 300 / 创业板 200 / 碎股豁免）。
    """
    want = _to_float(rule.get("quantity")) or 0.0
    reduce_pct = _to_float(rule.get("reduce_pct"))
    if want <= 0 and reduce_pct is not None and 0 < reduce_pct <= 1:
        want = float(can_use or 0.0) * reduce_pct
    return align_sell_quantity(symbol, want, can_use)


def update_highest_price(previous: float | None, price: float) -> float:
    """最高价只升不降。"""
    prev = float(previous or 0)
    return max(prev, float(price or 0))


def rule_client_order_id(symbol: str, now_ts: float, generation: int = 1) -> str:
    """规则当日幂等委托号。

    同一个「标的 + 交易日 + 触发代数」永远得到同一个 ``client_order_id``：
    触发后进程崩溃/状态写回失败时按同号重试，调度器的 client_order_id 去重
    会返回已有委托而不是重复下单（下真单的链路不允许靠状态机兜底防重）。

    ``generation`` 是当日第几次触发；``POST /reset`` 重新武装后递增，
    保证「重新武装后再触发」仍能下出**新**单而不是被自己的旧号挡住。
    """
    day = datetime.fromtimestamp(float(now_ts), TZ).strftime("%Y%m%d")
    return f"sltp-{normalize_symbol(symbol)}-{day}-g{max(1, int(generation))}"


def is_retryable(state_item: dict[str, Any] | None) -> bool:
    """本轮是否可（重新）评估触发。

    ``armed`` 是常规状态；``triggered`` 但**没有任何委托号**说明上一次触发在
    落单前中断（进程被杀、状态写回后崩溃），允许重试——幂等由
    :func:`rule_client_order_id` 保证，重试不会变成重复下单。
    """
    item = state_item or {}
    status = str(item.get("status") or ST_ARMED)
    if status == ST_ARMED:
        return True
    return status == ST_TRIGGERED and not str(item.get("order_id") or "")


def is_live_state(state_item: dict[str, Any] | None) -> bool:
    """该状态是否对应一笔**在途真单**。

    摘规则（``qmt_sltp_ctl --rm``）时会连状态一起清掉；对已触发未终结的规则，
    清掉状态等于**丢掉这笔委托的跟踪与终态通知**。故写侧在清理前先问这一句。
    """
    return str((state_item or {}).get("status") or "") in _LIVE_STATES


def _detail_floor(detail: dict[str, Any] | None, symbol: str | None) -> float | None:
    """跌停价下限：优先桥的 ``DownStopPrice``（权威，含板别/ST/日期口径）。

    桥没给时退回本地由昨收重算（单源见 ``lot_rules.protect_sell_price``）；
    两者都拿不到 → ``None``，由调用方 fail-closed。
    """
    floor = _to_float((detail or {}).get("DownStopPrice"))
    if floor is not None and floor > 0:
        return floor
    pre_close = _to_float((detail or {}).get("PreClose"))
    if pre_close is None or not symbol:
        return None
    return protect_sell_price(symbol, pre_close)


def resolve_protect_price(
    mode: str,
    detail: dict[str, Any] | None,
    live_price: float,
    symbol: str | None = None,
) -> tuple[str | None, float, str]:
    """保护价决策（纯函数）：返回 ``(order_type, price, note)``，``None`` = fail-closed。

    * ``aggressive``（默认）→ ``max(跌停价, 现价 × 0.99)``，**可成交且合法的最激进报价**。
    * ``limit_floor``（**遗留**）→ 原样报跌停价。只在「已封跌停、要排队」时合法；
      其余工况属越界申报 → 废单（2026-09-21 002074 那 42 笔）。保留仅为兼容存量配置。
    * ``market`` → ``("MARKET", 0, …)``（柜台映射最新价委托）。

    fail-closed（返回 ``None``）的情形：拿不到跌停价下限、现价非法。调用方告警而非乱报价。
    """
    m = _resolve_mode({"protect_price_mode": mode})
    if m == "market":
        return "MARKET", 0.0, "市价委托（柜台映射最新价）"
    floor = _detail_floor(detail, symbol)
    if floor is None or floor <= 0:
        return (
            None,
            0.0,
            "拿不到跌停价下限（桥无 DownStopPrice 且无昨收），fail-closed 不下单",
        )
    if m == "limit_floor":
        logger.warning(
            "[SltpExec] protect_price_mode=limit_floor 为遗留口径（报跌停价属越界申报，"
            "非封板排队时会废单）；建议改为 aggressive。symbol=%s floor=%.2f",
            symbol,
            floor,
        )
        return (
            "LIMIT",
            round(float(floor), 2),
            f"跌停保护价 {float(floor):.2f}（遗留口径）",
        )
    px = _to_float(live_price)
    if px is None or px <= 0:
        return None, 0.0, f"现价不可用（{live_price!r}），fail-closed 不下单"
    price = aggressive_sell_price(symbol or "", None, px, floor=floor)
    if price is None:
        return None, 0.0, "激进保护价计算失败（现价/跌停价非法），fail-closed 不下单"
    return (
        "LIMIT",
        price,
        f"激进保护价 {price:.2f}（现价 {px:.2f} −1%，下限 {floor:.2f}）",
    )


def _to_float(value: Any) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def dispatch_failure_text(resp: dict[str, Any] | None) -> str:
    """派发信封 → 用户告警里的失败文案。

    顶层 ``message``/``detail`` 优先；派发层 ``execution="direct"`` 的失败信封把
    拒因放在**嵌套** ``result.message``（引擎原话，如 ``Broker拒绝: 废单：…``），
    也要取到 —— 否则只剩整个信封的 ``str()``，用户告警里会出现一段 Python 字典
    （2026-09-24 核实：夹具喂顶层 message，线上是嵌套，测试测不到）。
    """
    env = resp if isinstance(resp, dict) else {}
    nested = env.get("result")
    nested_msg = nested.get("message") if isinstance(nested, dict) else None
    return str(env.get("message") or env.get("detail") or nested_msg or resp)


def classify_dispatch_failure(resp: dict[str, Any] | None) -> str:
    """派发失败信封 → 处置类别（重试与否的**唯一**判据，纯函数）。

    「失败就重试」是错的 —— sltp「一次触发当日只执行一次」是 2026-09-21 事故
    （002074：42 笔越界价废单、0 成交；触发→废单→重新武装→再触发每 2 分钟一轮）
    之后的刻意设计。分类边界（设计原文见 docs/local/quant-trader-migration-plan.md
    「P4 附-c」）：

    * ``deterministic``：本层预检/风控拒（``execution ∈ {lot_blocked, risk_blocked}``）
      —— 参数不变结论不变；
    * ``price_reject``：券商价格/参数类废单 —— 同参数自动重发只会重演那 42 笔循环；
    * ``transient``：派发层给出了**明确结论**（``status="failed"`` **且有 order_id**，
      即委托行已落库、券商明确拒了这张单）且拒因不属上面两类 —— 通道/繁忙类，
      可换代有界重试；
    * ``unknown``：拿不到派发层的结论（``status="error"`` / 无 order_id / 形状不认识）
      —— 状态未知，绝不重试：重发可能变成第二张卖单（超时的 ``submitted`` 走不到
      这里，它在引擎侧就以成功+待核查返回）。
    """
    env = resp if isinstance(resp, dict) else {}
    if str(env.get("execution") or "") in {"lot_blocked", "risk_blocked"}:
        return FAIL_DETERMINISTIC
    text = dispatch_failure_text(env)
    if any(marker in text for marker in _PRICE_REJECT_MARKERS):
        return FAIL_PRICE
    if str(env.get("status") or "") == "failed" and str(env.get("order_id") or ""):
        return FAIL_TRANSIENT
    return FAIL_UNKNOWN


def order_status_to_rule_state(db_status: str) -> str | None:
    """DB 订单状态 → 规则状态（未知返回 None，保持原状）。"""
    s = str(db_status or "").strip().lower()
    return {
        "filled": ST_FILLED,
        "cancelled": ST_CANCELLED,
        "rejected": ST_REJECTED,
        "failed": ST_FAILED,
        "expired": ST_FAILED,
        "partially_filled": ST_PARTIAL,
        "submitted": ST_SUBMITTED,
        "pending": ST_SUBMITTED,
        "accepted": ST_SUBMITTED,
    }.get(s)


# --------------------------------------------------------------------------
# Redis 配置 / 状态
# --------------------------------------------------------------------------
def load_config(redis: Any) -> dict[str, Any]:
    try:
        raw = redis.get(CONFIG_KEY)
        return merge_config(raw if isinstance(raw, dict) else None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[SltpExec] 读取配置失败，用默认（关闭）: %s", exc)
        return merge_config(None)


def _raw_client(redis: Any) -> Any:
    """取底层 redis-py 客户端（有就用它，拿不到就退回对象自身）。

    ``trade_shared.redis_client.RedisClient`` 的 ``get``/``set`` 会把**任何**异常
    吞掉：``get`` 失败返回 ``None``、``set`` 失败静默不写。对执行器的轮询这只是
    「本轮空转」，但对**读-改-写**的写侧是致命的——「Redis 抖了」被伪装成
    「配置是空的」，接着整表写回就等于把所有规则（含人工挂的）一起抹掉。
    """
    return redis.client if hasattr(redis, "client") else redis


def read_key_strict(redis: Any, key: str) -> Any | None:
    """读一个键：**异常照抛**，``None`` 只表示「键确实不存在」。"""
    client = _raw_client(redis)
    if client is None:
        # 连接器没连上：get 只会返回 None、set 只会静默丢弃 → 必须当读失败
        raise RuntimeError("Redis 未连接（client 为空）")
    raw = client.get(key)
    if raw is None:
        return None
    if isinstance(raw, bytes):  # 未开 decode_responses 的客户端
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        raw = json.loads(raw)
    return raw


def read_config_strict(redis: Any) -> dict[str, Any]:
    """读配置：**读不到就抛错**，绝不拿默认配置冒充「没有规则」。

    与 :func:`load_config` 的分工是刻意划的：

    * ``load_config`` 是**显示/轮询**口径——读失败回落默认（执行器下一拍重试，
      代价是这一拍空转）；
    * ``read_config_strict`` 是**写侧**口径——任何写路径（CLI ``--arm``/``--rm``、
      控制面 PUT、决策层整组替换）都必须先过它，否则一次 Redis 抖动就会把规则表
      整份写成「我刚编出来的那几条」。

    调用方负责把异常转成「本次不写」。
    """
    raw = read_key_strict(redis, CONFIG_KEY)
    if raw is None:
        return merge_config(None)
    if not isinstance(raw, dict):
        raise ValueError(f"{CONFIG_KEY} 不是对象（{type(raw).__name__}）")
    return merge_config(raw)


def _persistable(cfg: dict[str, Any]) -> dict[str, Any]:
    """待落盘的配置：剔除**读时派生**的键（``rejected_rules`` 是每次读重算的
    诊断信息，写回去只会变成陈旧快照，还会让「同一条规则被拒两次」看起来像进了两次）。"""
    return {k: v for k, v in cfg.items() if k != "rejected_rules"}


def save_config(redis: Any, cfg: dict[str, Any]) -> dict[str, Any]:
    clean = merge_config(cfg)
    redis.set(CONFIG_KEY, _persistable(clean))
    return clean


def _config_fingerprint(cfg: dict[str, Any]) -> tuple[Any, ...]:
    """整表指纹（比对用）：开关 + 其余顶层配置 + 归一后的规则列表。

    ``rejected_rules`` 不在内：它是**读时派生**的诊断信息，两边各算一次，逐字比必然不等。
    """
    return (
        bool(cfg.get("enabled")),
        {k: cfg.get(k) for k in DEFAULT_CONFIG if k != "rules"},
        [normalize_rule(r) for r in (cfg.get("rules") or [])],
    )


def save_config_strict(redis: Any, cfg: dict[str, Any]) -> dict[str, Any]:
    """写配置并**回读确认**，写没生效就抛错（控制面 PUT / CLI 用）。

    ``RedisClient.set`` 会把写失败吞掉（只记一条日志），所以「写成功」不能靠返回值
    自证——运维在控制面上看到 200、以为止损挂上了，实际一条都没进 Redis，这是真单
    链路上最不能接受的一类静默失败。与决策层写入端（``decision/watch_writer.py``）
    的分工是粒度：那边要逐条回报「哪条没落库」，这里只要「写进去了没有」。
    """
    clean = save_config(redis, cfg)
    after = read_config_strict(redis)
    if _config_fingerprint(after) != _config_fingerprint(clean):
        raise RuntimeError("写入未生效：回读的规则表与写入内容不一致")
    return clean


def set_enabled(redis: Any, enabled: bool) -> dict[str, Any]:
    """只改总开关，不动规则表。

    与 :func:`save_config` 的区别是**读失败直接抛错**（调用方回 5xx）：把「读不到」
    当空配置再整体写回，会在 Redis 抖动时把规则表整份抹掉。这里只有真的读到
    （含键不存在 → 默认配置）才会写。

    ⚠️ 读必须走 :func:`read_config_strict`：``RedisClient.get`` 会把读失败吞成 ``None``，
    用普通 ``get`` 的话「读不到」永远抛不出来，这条防线就成了纸面上的。
    """
    cfg = merge_config(read_config_strict(redis))
    cfg["enabled"] = bool(enabled)
    return save_config_strict(redis, cfg)


def diff_state(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
) -> tuple[set[str], set[str]]:
    """状态快照对比：返回 ``(改动过的标的, 被删除的标的)``。"""
    dirty = {symbol for symbol, item in after.items() if item != before.get(symbol)}
    removed = {symbol for symbol in before if symbol not in after}
    return dirty, removed


def load_state(redis: Any, today: str | None = None) -> dict[str, Any]:
    """读规则状态；跨日自动重置为 armed（当日一次触发的语义）。

    **例外：棘轮抬高的防守位跨日保留**（``stop_price`` 及其凭据 ``stop_entry`` /
    ``stop_volume``）。它描述的是**持仓**的状态而不是当日触发次数：持仓拿几周是
    常态，隔夜丢掉抬高过的防守位等于每天开盘都悄悄放松保护。持仓换了的作废逻辑
    见 :func:`carried_stop_invalid_reason`（下一次评估时校验）。
    """
    day = today or trade_date_str()
    try:
        raw = redis.get(STATE_KEY)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[SltpExec] 读取状态失败，用空状态: %s", exc)
        raw = None
    state = raw if isinstance(raw, dict) else {}
    if str(state.get("date") or "") != day:
        carried: dict[str, Any] = {}
        for symbol, item in (state.get("rules") or {}).items():
            if not isinstance(item, dict):
                continue
            has_stop = _to_float(item.get("stop_price")) is not None
            has_high = _to_float(item.get("highest_price")) is not None
            if not has_stop and not has_high:
                continue
            carried[symbol] = {
                "status": ST_ARMED,
                "stop_price": item.get("stop_price") if has_stop else None,
                "stop_entry": item.get("stop_entry"),
                "stop_volume": item.get("stop_volume"),
                # 高水位同棘轮防守位一样跨日保留：trailing 的 H 是「**持仓以来**
                # 最高价」，丢掉它会让回撤线在次日退回开仓价、再被当日第一个报价
                # 重置成今日现价——只升不降的保护变成只降不升，每个交易日往下
                # 挪一档（详见 carried_high_water_invalid_reason）。
                "highest_price": item.get("highest_price") if has_high else None,
                "highest_entry": item.get("highest_entry"),
            }
        state = {"date": day, "rules": carried}
    state.setdefault("rules", {})
    for symbol, item in list(state["rules"].items()):
        if not isinstance(item, dict):
            state["rules"][symbol] = {"status": ST_ARMED}
        else:
            item.setdefault("status", ST_ARMED)
            item.setdefault("highest_price", None)
    return state


def carried_stop_invalid_reason(
    state_item: dict[str, Any],
    *,
    entry: float | None,
    volume: float | None,
) -> str:
    """跨日保留的抬高防守位是否已作废（纯函数，空串 = 保留）。

    作废判据（任一命中即作废）——两个信号都是为了防「**新持仓被旧防守位秒杀**」：

    * **成本价变了**（相对差 > ``_STOP_ENTRY_TOLERANCE``）：清仓后再买回、或加过仓；
    * **持仓总量变大**：同一只票的仓换过（冻结中的挂单不影响 ``volume``，
      故用总量而非可用量）。

    ``entry``/``volume`` 传 ``None`` = **读不到**（不是「没有」）→ 不作废：
    在读不到持仓时撤掉正在生效的保护，比保守留着更危险。
    """
    stop_entry = _to_float(state_item.get("stop_entry"))
    stop_volume = _to_float(state_item.get("stop_volume"))
    cur_entry = _to_float(entry)
    cur_volume = _to_float(volume)
    if _entry_changed(stop_entry, cur_entry):
        return f"持仓成本已变化（{stop_entry:.2f}→{cur_entry:.2f}）"
    if stop_volume and cur_volume and cur_volume > stop_volume + 1e-6:
        return f"持仓数量已增加（{stop_volume:g}→{cur_volume:g}）"
    return ""


def _entry_changed(recorded: float | None, current: float | None) -> bool:
    """记录的成本价与当前成本价是否已经不是同一个持仓（相对差 > 容差）。

    ``None``（读不到）**不算变化**——在读不到持仓时撤掉正在生效的保护，比保守
    留着更危险。两条跨日承载位（棘轮防守位 / 移动止损高水位）共用这一判据：
    各写一份的话，容差与缺失语义迟早分叉，而分叉的方向是「静默撤防」。
    """
    return bool(
        recorded
        and current
        and abs(current - recorded) > recorded * _STOP_ENTRY_TOLERANCE
    )


def carried_high_water_invalid_reason(
    state_item: dict[str, Any], *, entry: float | None
) -> str:
    """跨日保留的移动止损高水位是否已作废（纯函数，空串 = 保留）。

    高水位是**某个持仓**的成绩：旧仓冲到 150 之后清仓、在 120 重新买回，沿用
    150 会把回撤线定在 142.5，一笔浮盈中的新仓当场被卖——与
    :func:`carried_stop_invalid_reason` 防的是同一件事，只是换成了百分比形态。

    ``highest_entry`` 缺失（本次修复之前写入的旧状态）**不作废**：缺凭据 ≠ 持仓
    变了，与上面「读不到持仓不作废」同一条纪律。
    """
    if not _entry_changed(
        _to_float(state_item.get("highest_entry")), _to_float(entry)
    ):
        return ""
    return (
        f"持仓成本已变化（{_to_float(state_item.get('highest_entry')):.2f}"
        f"→{_to_float(entry):.2f}）"
    )


def save_state(
    redis: Any,
    state: dict[str, Any],
    *,
    dirty: set[str] | None = None,
    removed: set[str] | None = None,
) -> None:
    """写回规则状态。

    * 不带 ``dirty``：整份覆盖（reset / 初始化场景）。
    * 带 ``dirty``：先读回 Redis 现存状态，只覆盖本轮改动过的规则，其余保留。
      执行器每轮都写状态，整份覆盖会把并发的 ``POST /reset``、CLI ``--rm``/``--arm``
      一并冲掉（后写覆盖先写）——真单链路上「用户以为已经解除，执行器照旧触发」
      是不能接受的。

    **读回失败一律不写**（``_strict_get``）：``RedisClient.get`` 会把读失败吞成
    ``None``，而这条路径把「读不到」当成「没有现存状态」→ 写回一份只剩本轮改动
    的状态，等于把**所有在途委托的跟踪与棘轮防守位一并抹掉**。写不成只是这一轮
    状态没落盘（下一次评估重算，下单幂等由 ``rule_client_order_id`` 兜底），
    抹掉则是不可逆的。
    """
    try:
        if dirty is None and removed is None:
            redis.set(STATE_KEY, state)
            return
        try:
            current = read_key_strict(redis, STATE_KEY)
        except Exception as exc:  # noqa: BLE001
            logger.error("[SltpExec] 状态读回失败，本次不写（不用空状态覆盖）: %s", exc)
            return
        day = str(state.get("date") or "")
        if isinstance(current, dict) and str(current.get("date") or "") == day:
            merged = dict(current)
        else:
            merged = {"date": day, "rules": {}}
        rules = dict(merged.get("rules") or {})
        for symbol in removed or ():
            rules.pop(symbol, None)
        for symbol in dirty or ():
            item = state["rules"].get(symbol)
            if item is None:
                rules.pop(symbol, None)
            else:
                rules[symbol] = item
        merged["rules"] = rules
        merged["date"] = day or merged.get("date")
        redis.set(STATE_KEY, merged)
        # 与落盘一致（并发新增/删除同步进进程内视图，供本轮后续步骤使用）
        state["rules"] = rules
        state["date"] = merged["date"]
    except Exception as exc:  # noqa: BLE001
        logger.error("[SltpExec] 状态写回失败: %s", exc)


def reset_rules(redis: Any, symbols: list[str] | None = None) -> dict[str, Any]:
    """重新武装（全部或指定标的）。

    **棘轮抬高的防守位与移动止损的高水位一并清掉**：reset 的语义是「这条规则
    从头来过」，回到规则配置里的初始防守位（保留抬高值会让「重新武装」变成
    「继续用旧防守」，与规则表里的配置对不上，排障时看不出防守位是从哪来的）；
    高水位同理——它属于**上一个持仓轮次**。
    """
    state = load_state(redis)
    targets = (
        [normalize_symbol(s) for s in symbols]
        if symbols
        else list(state["rules"].keys())
    )
    for symbol in targets:
        if not symbol:
            continue
        item = state["rules"].get(symbol)
        if item is None:
            continue
        keep_entry = item.get("entry_price")
        state["rules"][symbol] = {
            "status": ST_ARMED,
            "highest_price": None,
            "highest_entry": None,
            "entry_price": keep_entry,
            # generation 保留：重新武装后再触发要下**新**单（委托号含代数），
            # 而崩溃重试复用同代委托号、交给调度器幂等去重
            "generation": int(item.get("generation") or 0),
        }
    save_state(redis, state)
    return state


# --------------------------------------------------------------------------
# 依赖注入（便于单测）
# --------------------------------------------------------------------------
@dataclass
class SltpDeps:
    client: Any
    redis: Any
    dispatch: Callable[[dict[str, Any], str], Awaitable[dict[str, Any]]]
    notify: Callable[..., Awaitable[Any]]
    order_reader: Callable[[str], Awaitable[dict[str, Any] | None]]
    now: Callable[[], float] = time.time
    positions: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None
    fallback_config: Callable[[str, str], dict[str, Any]] = load_sltp_config
    # 撤单（余量策略 cancel/requote 用）；未注入时策略退化为 alert_only
    cancel_order: Callable[[str], Awaitable[bool]] | None = None
    extras: dict[str, Any] = field(default_factory=dict)


async def _fetch_positions(deps: SltpDeps) -> list[dict[str, Any]]:
    if deps.positions is not None:
        return await deps.positions()
    try:
        return await deps.client.get_positions()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[SltpExec] 查询柜台持仓失败: %s", exc)
        return []


def _find_position(
    positions: list[dict[str, Any]], symbol: str
) -> dict[str, Any] | None:
    target = normalize_symbol(symbol)
    for item in positions or []:
        code = normalize_symbol(str(item.get("stock_code") or item.get("symbol") or ""))
        if code and code == target:
            return item
    return None


def _position_entry_price(position: dict[str, Any] | None) -> float | None:
    if not position:
        return None
    for key in ("open_price", "avg_price", "cost_price"):
        value = _to_float(position.get(key))
        if value and value > 0:
            return value
    return None


# --------------------------------------------------------------------------
# 主循环
# --------------------------------------------------------------------------
async def run_sltp_cycle(
    deps: SltpDeps, *, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """单轮：监控在途单 → 评估触发 → 下单。返回本轮摘要（便于日志/测试）。"""
    cfg = config or load_config(deps.redis)
    summary: dict[str, Any] = {
        "enabled": bool(cfg.get("enabled")),
        "evaluated": 0,
        "triggered": 0,
        "submitted": 0,
        "failed": 0,
        #: 本轮登记的暂时性失败重试（不落终态；终态才进 ``failed``）。它不是「失败」
        #: 也不是「成功」：日志/测试按它观察保护性重试，人不再需要从 status 猜。
        "retry_scheduled": 0,
        "monitored": 0,
    }
    if not cfg.get("enabled"):
        return summary

    rules = [r for r in (cfg.get("rules") or []) if r.get("enabled", True)]
    if not rules:
        return summary

    today = trade_date_str()
    state = load_state(deps.redis, today)
    # 本轮开始时的快照：结束/中途回写只覆盖改动过的规则（并发 reset/--rm 不被冲掉）
    before_rules = {symbol: dict(item) for symbol, item in state["rules"].items()}

    def _persist() -> None:
        dirty, removed = diff_state(before_rules, state["rules"])
        save_state(deps.redis, state, dirty=dirty, removed=removed)

    user_id = str(cfg.get("user_id") or "1")
    tenant_id = str(cfg.get("tenant_id") or "default")
    now = deps.now()

    # 1) 在途单监控（任何时段都做，保证收盘后仍能收到终态通知）
    await _monitor_pending(deps, cfg, state, user_id, tenant_id, now, summary)

    # 2) 交易时段内评估触发
    if not is_trading_time():
        await _notify_stranded_triggers(deps, state, user_id, tenant_id)
        _persist()
        return summary

    armed = [r for r in rules if is_retryable(state["rules"].get(r["symbol"]))]
    if not armed:
        _persist()
        return summary

    if not bool(getattr(deps.client, "configured", True)):
        return summary

    try:
        ticks = await deps.client.get_full_tick([r["symbol"] for r in armed])
    except Exception as exc:  # noqa: BLE001
        logger.warning("[SltpExec] 拉实时行情失败: %s", exc)
        return summary

    fallback_cfg: dict[str, Any] | None = None
    positions: list[dict[str, Any]] | None = None

    for rule in armed:
        symbol = rule["symbol"]
        st = state["rules"].setdefault(symbol, {"status": ST_ARMED})
        tick = (ticks or {}).get(symbol) or {}
        price = _to_float(tick.get("lastPrice"))
        summary["evaluated"] += 1
        if price is None or price <= 0:
            st["misses"] = int(st.get("misses") or 0) + 1
            st["last_tick_ts"] = now
            # 恰好到阈值时告警一次；行情恢复计数归零，可再次告警
            if st["misses"] == _TICK_MISS_ALERT_THRESHOLD:
                await deps.notify(
                    user_id,
                    f"{symbol} 行情缺失",
                    f"连续 {st['misses']} 轮未取到该标的实时行情，止盈止损规则暂时无法评估。"
                    "请检查行情通道/代码是否正确。",
                    "warning",
                    tenant_id=tenant_id,
                )
            continue
        st["misses"] = 0
        st["last_price"] = price
        # 高水位的更新在 entry 解析之后（见下）——它要连带记下「这笔高点是在哪个
        # 成本上创的」，而成本要到那里才知道。

        if fallback_cfg is None:
            try:
                fallback_cfg = deps.fallback_config(tenant_id, user_id) or {}
            except Exception:  # noqa: BLE001
                fallback_cfg = {}

        # entry_price：规则显式 → 状态缓存 → 柜台持仓成本价
        entry = _to_float(rule.get("entry_price")) or _to_float(st.get("entry_price"))
        if not entry and positions is None:
            positions = await _fetch_positions(deps)
            entry = _position_entry_price(_find_position(positions, symbol))
        if not entry or entry <= 0:
            if not st.get("entry_missing_notified"):
                st["entry_missing_notified"] = True
                await deps.notify(
                    user_id,
                    f"{symbol} 止损规则缺少成本价",
                    "规则未配置 entry_price 且柜台无持仓成本，无法计算触发线；已跳过。",
                    "warning",
                    tenant_id=tenant_id,
                )
            continue
        st["entry_price"] = entry

        # 高水位的凭据校验必须**在本轮抬高之前**：晚一步，本轮的上涨会先把
        # ``highest_entry`` 改写成当前成本，持仓变更就永远看不出来了。
        # （棘轮防守位的同类校验在 _apply_ratchet 里，那里没有这个先后问题。）
        invalid_hw = carried_high_water_invalid_reason(st, entry=entry)
        if invalid_hw:
            for key in ("highest_price", "highest_entry"):
                st.pop(key, None)
            await deps.notify(
                user_id,
                f"{symbol} 移动止损最高价已复位",
                f"{invalid_hw}，上一个持仓的高点作废，按当前持仓从现价重新累计。",
                "warning",
                tenant_id=tenant_id,
            )

        # 高水位（只升不降）：本轮的报价在**凭据校验之后**入账，并记下这笔高点
        # 是在哪个成本上创的——下一个交易日据此判断该不该作废。
        prev_high = _to_float(st.get("highest_price"))
        st["highest_price"] = update_highest_price(prev_high, price)
        if st["highest_price"] > (prev_high or 0.0):
            st["highest_entry"] = entry

        # 绝对防守位（规则固定价 / 棘轮抬高值）在本轮内落进 st，再由
        # trigger_inputs 一并交给判定——判定口径与状态落账同源，不会各算各的。
        positions, stop_this_cycle = await _apply_ratchet(
            deps,
            st,
            rule,
            price=price,
            entry=entry,
            positions=positions,
            user_id=user_id,
            tenant_id=tenant_id,
        )
        tcfg = trigger_config(trigger_inputs(rule, st), fallback_cfg)
        # 同轮去抖（零间隙棘轮的前置）：本轮触发判定一律用**抬升前**的防守价，
        # 棘轮抬高值下一轮才生效。否则 ``move_stop_to == move_stop_trigger``
        # （隔壁 49.3% 的决策形态）会在武装的同一轮当场卖出——「武装即触发」。
        # ``stop_this_cycle`` 由 ``_apply_ratchet`` 在**跨日承载位校验之后**取，
        # 拿到的必然是「本轮真正生效的防守价」：承载位被判作废时它是规则价而非
        # 上一个仓的残留价（否则新持仓会被旧防守位秒杀）。既有 ``m < t`` 形态
        # 在此处零行为变化——抬升前后都是「不触发」（抬升时价格 ≥ trigger >
        # move_to > 抬升前防守价，两种防守价都判不出触发）。
        tcfg["stop_loss_price"] = stop_this_cycle
        triggered, reason = check_sltp_trigger(price, entry, tcfg)
        if not triggered:
            continue

        st["status"] = ST_TRIGGERED
        st["reason"] = reason
        st["triggered_at"] = now
        # 先落 triggered 再下单：进程中断时留下「触发未落单」的痕迹，
        # 下一轮 is_retryable 会按同号重试（委托号幂等，不会重复下单）
        _persist()
        summary["triggered"] += 1
        logger.info("[SltpExec] 触发 %s: %s", symbol, reason)

        if positions is None:
            positions = await _fetch_positions(deps)
        position = _find_position(positions, symbol)
        await _execute_trigger(
            deps,
            cfg,
            state,
            st,
            rule,
            position,
            price,
            reason,
            user_id,
            tenant_id,
            now,
            summary,
        )

    _persist()
    return summary


async def _apply_ratchet(
    deps: SltpDeps,
    st: dict[str, Any],
    rule: dict[str, Any],
    *,
    price: float,
    entry: float,
    positions: list[dict[str, Any]] | None,
    user_id: str,
    tenant_id: str,
) -> tuple[list[dict[str, Any]] | None, float | None]:
    """条件棘轮 + 跨日防守位校验（每个可评估的规则每轮一次）。

    返回 ``(持仓列表, 本轮判定用的防守价)``——持仓可能被补拉过，调用方沿用以免
    重复查询柜台；生效的绝对防守价落在 ``st["stop_price"]``，由
    :func:`effective_stop_price` 统一读取（判定与 CLI 预演同源）。

    **第二项的返回为什么要单独给**：棘轮抬升是**下一轮才生效**的事件。零间隙棘轮
    （``move_stop_to == move_stop_trigger``，隔壁 LLM 决策 49.3% 的形态）抬升瞬间
    价格 ≥ 目标价，若同一轮就拿抬升后的价位判定，会当场卖出——「武装即触发」，
    等于这个保护位白设。故返回值是**校验后、抬升前**的防守价：

    * 必须在**跨日校验之后**取——承载位被判作废（持仓换了）时若取了作废的价位，
      新开的仓会被上一个仓的防守位秒杀，正是该校验要防的事；
    * 对既有的**有间隙**棘轮（``move_to < trigger``）此值恒等于抬升后的值
      （抬升前 cur < move_to < trigger ≤ 现价，两种取法都不触发），即**零行为变化**。

    三件事，顺序固定：

    1. **校验跨日保留的防守位**：持仓换了（成本/总量对不上）→ 作废 + 告警，
       防「新开的仓被上一个仓的防守位秒杀」；
    2. **条件棘轮**：现价上触触发价 → 抬到目标防守价（只升不降），落状态 + 通知；
    3. 规则**没配棘轮但状态里有抬高值**时保留该值（它是持仓的防守位，
       不是规则的字段；要去掉它走 ``POST /reset``）。
    """
    symbol = str(rule.get("symbol") or "")
    base_stop = _to_float(rule.get("stop_loss_price"))

    if _to_float(st.get("stop_price")) is not None:
        if positions is None:
            positions = await _fetch_positions(deps)
        pos = _find_position(positions, symbol)
        invalid = carried_stop_invalid_reason(
            st, entry=entry, volume=_to_float((pos or {}).get("volume"))
        )
        if invalid:
            for key in ("stop_price", "stop_entry", "stop_volume"):
                st.pop(key, None)
            await deps.notify(
                user_id,
                f"{symbol} 棘轮防守位已复位",
                f"{invalid}，抬高的防守位作废，按当前持仓与规则重新评估"
                + (f"（规则防守价 {base_stop:.2f}）。" if base_stop else "。"),
                "warning",
                tenant_id=tenant_id,
            )

    # 本轮判定用的防守价：取在**跨日校验之后、棘轮抬升之前**（见 docstring）。
    stop_this_cycle = effective_stop_price(rule, st)

    new_stop, note = ratchet_stop_price(
        trigger=_to_float(rule.get("move_stop_trigger")),
        move_to=_to_float(rule.get("move_stop_to")),
        current=stop_this_cycle,
        price=price,
    )
    if new_stop is None:
        # note 非空 = 配置非法（如目标价高于触发价）。告警一次且绝不武装——
        # 静默忽略的话，用户以为有棘轮，实际整轮防守都停在规则初始位。
        if note and not st.get("ratchet_config_notified"):
            st["ratchet_config_notified"] = True
            await deps.notify(
                user_id,
                f"{symbol} 棘轮配置无效",
                f"{note}。该规则本轮未武装棘轮，请修正规则后重新武装。",
                "error",
                tenant_id=tenant_id,
            )
        return positions, stop_this_cycle

    if positions is None:
        positions = await _fetch_positions(deps)
    pos = _find_position(positions, symbol)
    st["stop_price"] = new_stop
    st["stop_entry"] = entry
    st["stop_volume"] = _to_float((pos or {}).get("volume"))
    logger.info("[SltpExec] %s %s（防守位 %.2f）", symbol, note, new_stop)
    await deps.notify(
        user_id,
        f"{symbol} 防守位已抬高",
        f"{note}；现价跌破该价即卖出。棘轮只升不降且跨交易日保留，持仓变动时自动复位。",
        "info",
        tenant_id=tenant_id,
    )
    return positions, stop_this_cycle


async def _execute_trigger(
    deps: SltpDeps,
    cfg: dict[str, Any],
    state: dict[str, Any],
    st: dict[str, Any],
    rule: dict[str, Any],
    position: dict[str, Any] | None,
    price: float,
    reason: str,
    user_id: str,
    tenant_id: str,
    now: float,
    summary: dict[str, Any],
) -> None:
    symbol = rule["symbol"]
    can_use = _to_float((position or {}).get("can_use_volume")) or 0.0
    quantity, note = plan_sell_quantity(symbol, rule, can_use)
    if quantity <= 0:
        st["status"] = ST_SKIPPED
        st["skip_reason"] = note
        summary["failed"] += 1
        await deps.notify(
            user_id,
            f"{symbol} 触发未卖出",
            f"{reason}；但{note}。规则当日不再重试。",
            "warning",
            tenant_id=tenant_id,
        )
        return

    detail: dict[str, Any] | None = None
    mode = _resolve_mode(cfg)
    if mode != "market":
        try:
            detail = await deps.client.get_instrument_detail(symbol)
        except Exception as exc:  # noqa: BLE001
            detail = None
            logger.warning("[SltpExec] 取 %s 合约详情失败: %s", symbol, exc)
    order_type, order_price, price_note = resolve_protect_price(
        mode, detail, price, symbol
    )
    if order_type is None:
        st["status"] = ST_FAILED
        st["skip_reason"] = price_note
        summary["failed"] += 1
        await deps.notify(
            user_id,
            f"{symbol} 触发但保护价不可用",
            f"{reason}；{price_note}。现价 {price:.2f}，请人工介入。",
            "error",
            tenant_id=tenant_id,
        )
        return

    # 保护性重试的冷却闸：上一次是**暂时性失败**且冷却未到时本轮不发单。
    # 判定保持 triggered（无委托号 = 可重试），下一轮再看——条件不成立时根本
    # 走不到这里（本轮不会触发），成立时最多等一个冷却窗。首单（attempts=0）
    # 不受此闸约束。见 ``_RETRY_COOLDOWN_SEC``。
    prior_attempts = int(st.get("retry_attempts") or 0)
    last_failure_at = _to_float(st.get("last_failure_at"))
    if (
        prior_attempts > 0
        and last_failure_at is not None
        and (now - last_failure_at) < _RETRY_COOLDOWN_SEC
    ):
        st["status"] = ST_TRIGGERED  # 触发判定已成立，维持待重试态
        logger.info(
            "[SltpExec] %s 重试冷却中（%.0fs / %.0fs），本轮不报单",
            symbol,
            now - last_failure_at,
            _RETRY_COOLDOWN_SEC,
        )
        return

    generation = int(st.get("generation") or 0) + 1
    st["generation"] = generation
    # 当日同规则固定委托号：崩溃重试复用同号，由调度器幂等去重（不会重复下单）；
    # 换代重试（每次失败 generation+1）拿到**新号**，不会被自己的旧号挡成幂等命中。
    cid = rule_client_order_id(symbol, now, generation)
    remarks = f"sltp:{reason[:40]}" if reason else "sltp:trigger"
    order_data = {
        "symbol": symbol,
        "side": str(rule.get("side") or "SELL"),
        "quantity": float(quantity),
        "price": float(order_price),
        "order_type": order_type,
        "trading_mode": "REAL",
        "portfolio_id": 0,
        "strategy_id": None,
        "client_order_id": cid,
        "remarks": remarks,
        # P1.6 TCA 基准价 = **触发时的现价**（本函数一路用的 `price`，也是告警里那句
        # "现价 X"）：保护腿真正要回答的问题是"从看到价到成交，掉价多少"，而不是
        # "我的保护价排得贵不贵"（那是 cushion 的口径，另有其数）。
        "ref_price": price,
        # 卖光实时可用量 = 整仓卖出（零股合法）：数量来自柜台**实时**持仓，而派发层的
        # 整手预检只看当日快照 —— 快照比实时大时合法的碎股全清会被判 ``lot_blocked``，
        # 该止损的时候止损单发不出去（委托行已落库，每轮重试都被拒）。
        "full_position_sell": is_full_position_sell("SELL", quantity, can_use),
    }
    try:
        resp = await deps.dispatch(order_data, user_id)
    except Exception as exc:  # noqa: BLE001
        resp = {"status": "error", "message": str(exc)}
    if str((resp or {}).get("status")) != "success":
        st["failure"] = dispatch_failure_text(resp)
        fail_class = classify_dispatch_failure(resp)
        attempts = int(st.get("retry_attempts") or 0)
        if fail_class == FAIL_TRANSIENT and attempts < _RETRY_MAX_PER_DAY:
            # 暂时性失败：保持**可重试**（triggered 无委托号），下一轮重新判定条件，
            # 仍成立才以新代次再报。不落 order_id：那张被拒的单不是我们的在途单。
            st["retry_attempts"] = attempts + 1
            st["status"] = ST_TRIGGERED
            st["last_failure_at"] = now
            summary["retry_scheduled"] += 1
            logger.error(
                "[SltpExec] %s 下单失败（将自动重试 %d/%d）: %s",
                symbol,
                attempts + 1,
                _RETRY_MAX_PER_DAY,
                st["failure"],
            )
            await deps.notify(
                user_id,
                f"{symbol} 触发卖出失败（将自动重试）",
                f"{reason}；下单失败：{st['failure']}。保护条件仍成立时"
                f"{int(_RETRY_COOLDOWN_SEC)} 秒后自动重试（第 {attempts + 1}/{_RETRY_MAX_PER_DAY} 次，"
                "换新委托号）。",
                "warning",
                tenant_id=tenant_id,
            )
            return
        st["status"] = ST_FAILED
        summary["failed"] += 1
        logger.error("[SltpExec] %s 下单失败: %s", symbol, st["failure"])
        if fail_class == FAIL_TRANSIENT:
            # 到上限：当日终态 + 升级告警（终态后不再发放，结构上恰好一条）。
            await deps.notify(
                user_id,
                f"{symbol} 止损委托重试 {attempts} 次仍失败",
                f"{reason}；最近一次失败：{st['failure']}。已自动重试 {attempts} 次"
                f"（当日上限 {_RETRY_MAX_PER_DAY} 次）仍未报出委托，当日不再重试，"
                "请人工介入（手动卖出，或排查通道/柜台后重新武装）。",
                "error",
                tenant_id=tenant_id,
            )
            return
        hint = ""
        if fail_class == FAIL_PRICE:
            hint = "（价格类废单不做自动重发：同参数重发只会再次废单，请人工调整价位）"
        elif fail_class == FAIL_DETERMINISTIC:
            hint = "（预检/风控拒绝：参数不变结论不变，请先修正持仓/额度/数量）"
        await deps.notify(
            user_id,
            f"{symbol} 触发卖出失败",
            f"{reason}；下单失败：{st['failure']}。保护价 {price_note}，现价 {price:.2f}，"
            f"请人工介入。{hint}",
            "error",
            tenant_id=tenant_id,
        )
        return

    st.update(
        {
            "status": ST_SUBMITTED,
            "order_id": str((resp or {}).get("order_id") or ""),
            "client_order_id": cid,
            "quantity": float(quantity),
            "order_price": float(order_price),
            "order_type": order_type,
            "price_note": price_note,
            "submitted_at": now,
        }
    )
    summary["submitted"] += 1
    extra = f"（{note}）" if note else ""
    # 部分减仓（或手填少于可用）：这一步之后规则即终态，剩余仓位不再被评估。
    # 必须明说——否则用户以为「减一半」之后另一半还在防守里。
    tail = ""
    remainder = max(0.0, can_use - quantity)
    if remainder > 0:
        tail = (
            f"剩余 {remainder:g} 股本次未了结：触发即终态，本规则当日不再自动触发，"
            "如需继续保护请重新武装（POST /reset）或改规则。"
        )
    logger.info(
        "[SltpExec] 已提交 %s %s %s股 @%s order_id=%s",
        symbol,
        order_type,
        quantity,
        order_price or "市价",
        st["order_id"],
    )
    await deps.notify(
        user_id,
        f"{symbol} 触发卖出已提交",
        f"{reason}；以{price_note}报单 {quantity:g} 股{extra}，委托号 {st['order_id']}。{tail}",
        "info",
        tenant_id=tenant_id,
    )


def seconds_to_close(now_ts: float) -> float:
    """距当日 15:00 收盘的秒数（收盘后为负）。"""
    now_dt = datetime.fromtimestamp(float(now_ts), TZ)
    close_dt = now_dt.replace(
        hour=_CLOSE_HOUR, minute=_CLOSE_MINUTE, second=0, microsecond=0
    )
    return (close_dt - now_dt).total_seconds()


async def _notify_stranded_triggers(
    deps: SltpDeps, state: dict[str, Any], user_id: str, tenant_id: str
) -> None:
    """触发后没能落单（进程中断）且已过交易时段：告警一次，交人工处理。

    交易时段内这类规则会由 :func:`is_retryable` 自动重试，不需要告警；
    但收盘后才发现的（例如重启后已过 15:00）当天已经没有补救机会，
    必须让人知道「触发了但没卖出去」。
    """
    for symbol, st in state.get("rules", {}).items():
        if str(st.get("status")) != ST_TRIGGERED or str(st.get("order_id") or ""):
            continue
        if st.get("stranded_notified"):
            continue
        st["stranded_notified"] = True
        attempts = int(st.get("retry_attempts") or 0)
        # 两种来源都收口在这：进程中断（attempts=0）与保护性重试未成功（attempts>0，
        # 例如条件一直没再成立、或到上限前收盘）。文案必须区分，否则用户以为没人试过。
        how = (
            f"自动重试 {attempts} 次仍未成功" if attempts else "触发时执行器中断"
        )
        await deps.notify(
            user_id,
            f"{symbol} 止损触发未能下单",
            f"{st.get('reason') or '触发'}；但{how}且已收盘，当天未能报出委托。"
            "请人工确认是否手动卖出，或下一个交易日重新武装（POST /reset）。",
            "error",
            tenant_id=tenant_id,
        )


async def _monitor_pending(
    deps: SltpDeps,
    cfg: dict[str, Any],
    state: dict[str, Any],
    user_id: str,
    tenant_id: str,
    now: float,
    summary: dict[str, Any],
) -> None:
    alert_sec = float(cfg.get("pending_alert_sec") or 0)
    policy = str(cfg.get("remainder_policy") or "alert_only")
    close_window = float(cfg.get("close_reminder_sec") or 0)
    # 重挂基准要**当前市价**（aggressive 口径的目标价 = 现价 × 0.99）。本段平时不拉
    # 行情——触发评估段虽然拉，但那只覆盖 `is_retryable` 的规则，在途单不在其中。
    # 故仅在确实要重挂、且尚未处理过时补拉一次；拉不到就退回委托价（见下）。
    live_prices: dict[str, float] = {}
    if policy == "requote_at_protect_price" and bool(
        getattr(deps.client, "configured", True)
    ):
        need = [
            sym
            for sym, item in (state.get("rules") or {}).items()
            if item.get("status") in _LIVE_STATES and not item.get("remainder_applied")
        ]
        if need:
            try:
                ticks = await deps.client.get_full_tick(need)
                for sym in need:
                    px = _to_float(((ticks or {}).get(sym) or {}).get("lastPrice"))
                    if px is not None and px > 0:
                        live_prices[sym] = px
            except Exception as exc:  # noqa: BLE001
                logger.warning("[SltpExec] 重挂取实时行情失败: %s", exc)
    for symbol, st in list(state.get("rules", {}).items()):
        if st.get("status") not in _LIVE_STATES:
            continue
        order_id = str(st.get("order_id") or "")
        if not order_id:
            continue
        try:
            row = await deps.order_reader(order_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SltpExec] 读订单 %s 失败: %s", order_id, exc)
            continue
        if not row:
            continue
        summary["monitored"] += 1
        db_status = str(row.get("status") or "").lower()
        filled = _to_float(row.get("filled_quantity")) or 0.0
        st["filled_quantity"] = filled
        st["last_status"] = db_status
        mapped = order_status_to_rule_state(db_status)
        if mapped in _TERMINAL_STATES | {ST_PARTIAL}:
            st["status"] = mapped
        if mapped in _TERMINAL_STATES:
            if not st.get("terminal_notified"):
                st["terminal_notified"] = True
                remaining = max(0.0, (_to_float(st.get("quantity")) or 0.0) - filled)
                avg = _to_float(row.get("average_price"))
                level = "info" if db_status == "filled" else "warning"
                tail = f"，剩余 {remaining:g} 股未成交" if remaining > 0 else ""
                await deps.notify(
                    user_id,
                    f"{symbol} 止损委托已{_status_cn(db_status)}",
                    f"成交 {filled:g} 股"
                    + (f" @ {avg:.2f}" if avg else "")
                    + tail
                    + f"（委托号 {order_id}）。",
                    level,
                    tenant_id=tenant_id,
                )
            continue

        quantity = _to_float(st.get("quantity")) or 0.0
        remaining = max(0.0, quantity - filled)
        pending_sec = now - float(st.get("submitted_at") or now)

        # 收盘前提醒：A 股当日有效，尾盘未成交的余量将随日终自动失效
        if (
            close_window > 0
            and not st.get("close_reminder_notified")
            and 0 <= seconds_to_close(now) <= close_window
            and remaining > 0
        ):
            st["close_reminder_notified"] = True
            await deps.notify(
                user_id,
                f"{symbol} 止损委托临近收盘",
                f"距收盘不足 {int(close_window / 60)} 分钟，仍有 {remaining:g} 股未成交；"
                "A 股委托当日有效，未成交部分将随日终自动失效，请确认是否需要人工处理。",
                "warning",
                tenant_id=tenant_id,
            )

        if alert_sec > 0 and pending_sec < alert_sec:
            continue

        if not st.get("unfilled_notified"):
            st["unfilled_notified"] = True
            await deps.notify(
                user_id,
                f"{symbol} 止损委托未成交",
                f"已挂 {int(pending_sec)} 秒"
                + (f"，部分成交 {filled:g} 股" if filled else "")
                + f"，剩余 {remaining:g} 股未成交。跌停封死时无买盘无法卖出，"
                "系统按时间优先排队等待；也可人工撤单改价。",
                "warning",
                tenant_id=tenant_id,
            )

        # 余量策略（每规则当日只执行一次）
        if not st.get("remainder_applied"):
            await _apply_remainder_policy(
                deps,
                cfg,
                st,
                symbol,
                remaining,
                policy,
                user_id,
                tenant_id,
                now,
                summary,
                ref_price=live_prices.get(symbol),
            )


async def _apply_remainder_policy(
    deps: SltpDeps,
    cfg: dict[str, Any],
    st: dict[str, Any],
    symbol: str,
    remaining: float,
    policy: str,
    user_id: str,
    tenant_id: str,
    now: float,
    summary: dict[str, Any],
    ref_price: float | None = None,
) -> None:
    """未成交余量处理：alert_only（默认，仅提醒）/ cancel / requote_at_protect_price。

    ``ref_price`` = 本次调用时点的市价（由调用方批量拉取）；缺省时退回该规则最近
    一次的 tick 价，再退回原委托价——按陈旧基准改价不如保守留旧价。
    """
    if remaining <= 0:
        st["remainder_applied"] = True
        return
    if policy == "alert_only":
        st["remainder_applied"] = True
        return
    if deps.cancel_order is None:
        st["remainder_applied"] = True
        st["remainder_note"] = "cancel 未接线"
        logger.warning(
            "[SltpExec] %s 余量策略 %s 需要 cancel_order 依赖，未注入", symbol, policy
        )
        return

    order_id = str(st.get("order_id") or "")
    order_price = _to_float(st.get("order_price")) or 0.0
    mode = _resolve_mode(cfg)
    order_type = str(st.get("order_type") or "")

    # 重挂前置：仅当当前委托价与保护价确有偏离才值得撤挂（否则丢队列优先级）
    if policy == "requote_at_protect_price":
        if order_type != "LIMIT":
            st["remainder_applied"] = True
            return
        try:
            detail = await deps.client.get_instrument_detail(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SltpExec] %s 重挂取合约详情失败: %s", symbol, exc)
            return
        # 用**当前市价**算重挂目标价；三级兜底到原委托价（宁可不动，不按陈旧基准改价）。
        ref = ref_price or _to_float(st.get("last_price")) or order_price
        new_type, new_price, _note = resolve_protect_price(mode, detail, ref, symbol)
        if new_type is None:
            st["remainder_applied"] = True
            return
        if (
            new_type != "LIMIT"
            or abs(new_price - order_price) <= _REQUOTE_PRICE_EPSILON
        ):
            st["remainder_applied"] = True
            st["remainder_note"] = "价格未偏离保护价，保持排队"
            return

    try:
        cancelled = await deps.cancel_order(order_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[SltpExec] %s 余量撤单异常: %s", symbol, exc)
        return
    if not cancelled:
        st["remainder_applied"] = True
        st["remainder_note"] = "撤单未受理（可能已成交/已撤销），保持现状"
        return

    summary["remainder_action"] = summary.get("remainder_action", 0) + 1

    if policy == "cancel":
        st["remainder_applied"] = True
        st["remainder_note"] = f"已撤单，剩余 {remaining:g} 股未成交"
        await deps.notify(
            user_id,
            f"{symbol} 未成交余量已撤单",
            f"委托挂满 {int(now - float(st.get('submitted_at') or now))} 秒未全部成交，"
            f"已按余量策略撤销，剩余 {remaining:g} 股了结。",
            "warning",
            tenant_id=tenant_id,
        )
        return

    # requote：撤旧挂新（保护价、当日额度按新委托重新计）
    requote_count = int(st.get("requote_count") or 0) + 1
    cid = f"{rule_client_order_id(symbol, now, int(st.get('generation') or 1))}-r{requote_count}"
    order_data = {
        "symbol": symbol,
        "side": "SELL",
        "quantity": float(remaining),
        "price": float(new_price),
        "order_type": "LIMIT",
        "trading_mode": "REAL",
        "portfolio_id": 0,
        "strategy_id": None,
        "client_order_id": cid,
        "remarks": f"sltp:requote{(st.get('reason') or '')[:32]}",
        # P1.6 TCA 基准价 = 这次重挂所依据的价（`ref`：当前市价，三级兜底见上）。
        # 按定义它就是"决策时点我们看到的那个价"——兜底到原委托价时也一样如实记录，
        # 不假装它是新鲜行情。
        "ref_price": ref,
    }
    try:
        resp = await deps.dispatch(order_data, user_id)
    except Exception as exc:  # noqa: BLE001
        resp = {"status": "error", "message": str(exc)}
    if str((resp or {}).get("status")) != "success":
        st["remainder_applied"] = True
        st["remainder_note"] = f"重挂失败：{(resp or {}).get('message') or resp}"
        await deps.notify(
            user_id,
            f"{symbol} 余量重挂失败",
            f"旧委托已撤，重挂保护价 {new_price:.2f} 失败：{st['remainder_note']}。"
            f"剩余 {remaining:g} 股未了结，请人工介入。",
            "error",
            tenant_id=tenant_id,
        )
        return
    st.update(
        {
            "status": ST_SUBMITTED,
            "order_id": str((resp or {}).get("order_id") or ""),
            "client_order_id": cid,
            "quantity": float(remaining),
            "order_price": float(new_price),
            "submitted_at": now,
            "requote_count": requote_count,
            "remainder_note": f"已按保护价 {new_price:.2f} 重挂",
            "unfilled_notified": False,
            "close_reminder_notified": False,
        }
    )
    # 重挂后允许再观察一轮（保留 remainder_applied=False 会每轮重复撤挂，故置位）
    st["remainder_applied"] = True
    await deps.notify(
        user_id,
        f"{symbol} 未成交余量已重挂",
        f"旧委托价 {order_price:.2f} 偏离保护价，已撤单并重挂 {new_price:.2f}，"
        f"剩余 {remaining:g} 股。",
        "info",
        tenant_id=tenant_id,
    )


def _status_cn(status: str) -> str:
    return {
        "filled": "全部成交",
        "cancelled": "撤销",
        "rejected": "被柜台拒绝",
        "failed": "失败",
        "expired": "过期",
        "partially_filled": "部分成交",
    }.get(str(status).lower(), str(status))


def build_state_snapshot(redis: Any) -> dict[str, Any]:
    """给路由/CLI 的状态快照。"""
    return {"config": load_config(redis), "state": load_state(redis)}


# --------------------------------------------------------------------------
# 生产依赖与常驻任务
# --------------------------------------------------------------------------
def _build_default_deps(redis: Any, tenant_id: str = "default") -> SltpDeps:
    from sqlalchemy import select

    from backend.services.live_trading.services.internal_strategy_dispatcher import (
        dispatch_internal_strategy_order,
    )
    from backend.services.live_trading.services.qmt_exec_client import (
        get_qmt_exec_client,
    )
    from backend.shared.database_manager_v2 import get_session
    from backend.shared.notification_publisher import publish_notification_async
    from backend.services.trade_shared.models.order import Order

    client = get_qmt_exec_client()

    async def dispatch(order_data: dict[str, Any], user_id: str) -> dict[str, Any]:
        async with get_session() as db:
            return await dispatch_internal_strategy_order(
                order_data=order_data,
                user_id=str(user_id),
                tenant_id=tenant_id,
                redis=redis,
                db=db,
            )

    async def notify(
        user_id: str,
        title: str,
        content: str,
        level: str = "info",
        tenant_id: str = "default",
    ) -> Any:
        return await publish_notification_async(
            user_id=str(user_id),
            tenant_id=str(tenant_id or "default"),
            title=title,
            content=content,
            type="trading",
            level=level,
            action_url="/trading",
        )

    async def order_reader(order_id: str) -> dict[str, Any] | None:
        async with get_session(read_only=True) as db:
            row = (
                await db.execute(select(Order).where(Order.order_id == str(order_id)))
            ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "status": str(getattr(row.status, "value", row.status) or ""),
            "filled_quantity": float(getattr(row, "filled_quantity", 0) or 0),
            "average_price": float(getattr(row, "average_price", 0) or 0),
            "remarks": str(getattr(row, "remarks", "") or ""),
        }

    async def cancel_order(order_id: str) -> bool:
        from backend.services.trade_shared.deps import get_redis as _get_redis
        from backend.services.live_trading.services.trading_engine import TradingEngine

        async with get_session() as db:
            row = (
                await db.execute(select(Order).where(Order.order_id == str(order_id)))
            ).scalar_one_or_none()
            if row is None:
                return False
            engine = TradingEngine(db, _get_redis())
            return bool(await engine.cancel_order_execution(row))

    return SltpDeps(
        client=client,
        redis=redis,
        dispatch=dispatch,
        notify=notify,
        order_reader=order_reader,
        cancel_order=cancel_order,
    )


async def run_qmt_sltp_executor_task() -> None:
    """常驻循环（随 trade 服务启动；失败只记日志，不拖垮进程）。"""
    from backend.services.trade_shared.deps import get_redis

    logger.info("[SltpExec] 止盈止损执行器任务启动")
    last_error = ""
    while True:
        interval = float(DEFAULT_CONFIG["poll_interval_sec"])
        try:
            redis = get_redis()
            cfg = load_config(redis)
            interval = max(1.0, float(cfg.get("poll_interval_sec") or interval))
            if cfg.get("enabled"):
                deps = _build_default_deps(
                    redis, tenant_id=str(cfg.get("tenant_id") or "default")
                )
                summary = await run_sltp_cycle(deps, config=cfg)
                last_error = ""
                if (
                    summary.get("triggered")
                    or summary.get("submitted")
                    or summary.get("failed")
                    or summary.get("retry_scheduled")
                ):
                    logger.info(
                        "[SltpExec] 本轮：%s", json.dumps(summary, ensure_ascii=False)
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if message != last_error:
                logger.error("[SltpExec] 轮询异常: %s", exc, exc_info=True)
                last_error = message
        await asyncio.sleep(interval)
