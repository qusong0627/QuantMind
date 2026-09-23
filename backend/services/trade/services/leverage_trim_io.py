"""减仓执行器的取数层（P2.6）：账户/行情 → 腿，外加配置/状态/报告键。

**这一层管「外面长什么样」**，不管「什么时候跑」（worker 在 ``leverage_trim_runner``）、
「一轮里按什么顺序做」（编排在 ``leverage_trim``）、也不管「单子怎么发出去」
（``leverage_trim_submit``：报价/幂等号/派发/告警闸）。三项纪律：

* **读的账户就是下单去的账户**：账户经 QMT 执行端读，券商选定必须是 ``qmt_exec``。
  「按 A 账户的杠杆去减 B 账户的仓」是真实存在过的故障形态（两座真账户规模差 ~25 倍）。
* **Redis 一律走原生客户端**（:func:`native_redis_client` 或 :func:`_client` 解包）。
  ``trade_shared`` 的包装客户端 ``get/set`` 会**吞异常**并把值按 JSON 解，而配置/状态/
  报告键全靠 ``hgetall``/``lpush`` 这类原生接口——用包装客户端读写，异常会被这一层的
  ``except`` 收成一条 warning，表现为「暂停开关按不动、当日计数永远是 0、状态键空白」，
  而代码看起来一切正常（2026-09-24 评审 C1 的实测形态）。同 ``decision_round_io``。
* **fail-closed 的方向是「不卖」**：任何一处读不出 → ``errors`` 非空 → 本轮不动手。
  数据故障不是卖出信号。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from backend.services.trade.services.decision_round_core import (
    ENV_ACCOUNT_USER,
    TENANT_ID,
)
from backend.services.trade.services.leverage_trim_core import LegInput

logger = logging.getLogger(__name__)


# ── 键与开关（唯一出处）──────────────────────────────────────────────
CONFIG_KEY = "qm:risk:trim:config"
STATE_KEY_PREFIX = "qm:risk:trim:state"
LAST_KEY = "qm:risk:trim:last"
LOG_KEY = "qm:risk:trim:log"
LOG_KEEP = 20
LAST_TTL_S = 7 * 86400
STATE_TTL_S = 3 * 86400

#: 常驻 worker 开关。与决策轮同一条纪律：**只有 ``"true"`` 打开**（``env_flags``），
#: 且 ``trade/main.py`` 与本模块各判一次（前者决定建不建任务，后者决定跑不跑）。
ENV_FLAG = "QM_LEVERAGE_TRIM_ENABLED"
ENV_POLL_S = "QM_LEVERAGE_TRIM_POLL_S"

#: 备注前缀——**进强平族的唯一凭据**（风险闸按 remarks 前缀放宽价格偏离带）。
#: 改这个串必须同步改 ``risk_gate_service._FORCED_EXIT_PREFIXES``（有守卫测试钉住）。
REMARK_PREFIX = "trim:"

#: 本执行器只认这座券商：读的账户（QMT 客户端）与下单去的账户必须是同一座。
EXPECTED_BROKER = "qmt_exec"

DEFAULT_CONFIG: dict[str, Any] = {
    "paused": False,
    "interval_sec": 60,
    "protect_price_mode": "aggressive",
}

#: 节拍的上下夹取。上限不是审美：心跳 TTL 由 ``scheduler_registry`` 固定为 300s
#: （``leverage_trim`` 的 JobSpec），节拍一旦大于它，**活着的**执行器会被 C07 体检
#: 判成 stale/fail，而体检说明恰好写着「重跑 = ``schedule_ctl run leverage_trim --force``
#: = 立刻真减一次」——运维照提示照做就会真的发一轮减仓单。120s 保证每个 TTL 窗口
#: 至少两次心跳（评审 M6）。
MIN_INTERVAL_SEC = 5
MAX_INTERVAL_SEC = 120

#: 同一标的当日最多「尝试提交」几次失败就停手（详见模块 docstring 第 3 条）。
#: 判定在编排层（``leverage_trim`` 的停手闸），计数在
#: ``leverage_trim_submit``——本模块只当它的存放处（配置类的常量都在这一节）。
MAX_ATTEMPTS_PER_SYMBOL_PER_DAY = 3


# ── Redis 客户端（键位读写的唯一入口）────────────────────────────────
def native_redis_client() -> Any:
    """原生 redis-py 客户端（**交易库**）——配置/状态/报告键的可用客户端。

    与 ``decision_round_io.native_redis_client`` 同参同库（两处都读 ``REDIS_DB_TRADE``，
    守卫测试 ``test_leverage_trim.py::test_native_redis_client_factories_agree_on_the_keyspace``
    钉死）；
    理由见模块 docstring 第 2 条：本模块要用 ``hgetall``/``lpush``/``ltrim``/``set(ex=)``
    并且**要让失败可见**，包装客户端两样都给不了。
    """
    import redis as _redis_lib

    return _redis_lib.Redis(
        host=os.getenv("REDIS_HOST") or "redis",
        port=int(os.getenv("REDIS_PORT", "6379")),
        password=os.getenv("REDIS_PASSWORD") or None,
        db=int(os.getenv("REDIS_DB_TRADE", "2")),
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=5,
    )


def _client(redis: Any) -> Any:
    """把包装客户端解成本模块能用的客户端；已经是原生的（或测试替身）原样返回。

    ``trade_shared.redis_client.RedisClient`` 与 ``deps.get_redis()`` 返回的是**包装**：
    ``get`` 会把值按 JSON 解、``set`` 会把值按 JSON 编，而本模块的状态键存的**就是**
    JSON 串（经它读写会二次编解码），配置键要 ``hgetall``（包装根本没有这个方法）。
    更糟的是包装把异常吞成 ``logger.error`` —— 故障在代码里不留痕迹，表现为「暂停开关
    按不动、当日计数恒为 0、状态键空白」而一切看起来正常（2026-09-24 评审 C1）。

    **为什么不在入口就换掉客户端**：``deps.redis`` 要一并传给下单派发链
    （``dispatch_internal_strategy_order`` → ``OrderService``/``TradingEngine``/
    ``_mirror_notice_allowed``），那条链说的是**包装**的方言（``.publish_event``、
    ``getattr(redis, "client")``）。所以分工是：交给它们的仍是包装，**本模块自己的
    键位读写一律经这里解包**。两套指向同一座库（包装的 ``REDIS_DB`` 默认即
    ``REDIS_DB_TRADE``），故键位重合。

    ``callable`` 那半句不是防御性冗余：**原生 ``redis.Redis`` 自己有一个 ``client()``
    方法**（redis-py 的连接工厂）。只判 ``is not None`` 的话，把原生客户端（或任何
    ``.client`` 是方法的替身）喂进来会被拆成**方法对象**，随后 ``.hgetall/.get`` 全变成
    ``'function' object has no attribute …``。而失败方向恰是最坏的一种：配置读取失败会用
    **默认值**继续（``paused=False``）、状态读成空（计数恒 0、去重失效）、状态写静默丢弃
    ——就是 C1 那套症状原样复活，只是换了个入口。``shared/risk/tiers.py`` 的 ``_client``
    在 2026-09-23 定档首跑踩过同一个坑，那里写着同一句判据。
    """
    native = getattr(redis, "client", None)
    return native if native is not None and not callable(native) else redis


# ── 小工具 ──────────────────────────────────────────────────────────
def _finite(value: Any) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def _norm_code(value: Any) -> str:
    """任意形态代码 → 后缀式大写（``600036.SH``）；取不出返回空串。"""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        from backend.shared.stock_utils import StockCodeUtil

        out = StockCodeUtil.to_suffix(raw)
    except Exception:  # noqa: BLE001 归一失败不阻断读取（下面按原样大写兜底）
        out = ""
    return str(out or raw).strip().upper()


# ── 配置与状态 ──────────────────────────────────────────────────────
def load_config(redis: Any) -> dict[str, Any]:
    """读配置（键不存在 → 默认）。**读失败用默认值**：本执行器的默认姿态是停下
    （``paused`` 默认 False 只是因为 worker 另有一道 env 闸；配置读不到时真正的护栏是
    「档位/账户任一处读不出就不动手」，故此处不因一次 Redis 抖动把执行器钉死）。"""
    cfg = dict(DEFAULT_CONFIG)
    try:
        # ``hgetall`` 只有原生客户端有；包装客户端连这个方法都不存在（评审 C1 的病灶之一）。
        raw = _client(redis).hgetall(CONFIG_KEY) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[LeverageTrim] 配置读取失败，用默认值: %s", exc)
        return cfg
    for key in DEFAULT_CONFIG:
        if key in raw and raw[key] not in (None, ""):
            cfg[key] = raw[key]
    cfg["paused"] = str(cfg.get("paused")).strip().lower() in {"1", "true", "yes", "on"}
    try:
        cfg["interval_sec"] = min(
            MAX_INTERVAL_SEC,
            max(MIN_INTERVAL_SEC, int(float(cfg.get("interval_sec") or 60))),
        )
    except (TypeError, ValueError):
        cfg["interval_sec"] = int(DEFAULT_CONFIG["interval_sec"])
    return cfg


def state_key(day: str) -> str:
    return f"{STATE_KEY_PREFIX}:{day}"


def load_state(redis: Any, day: str) -> dict[str, Any]:
    """当日状态（尝试/已提交/已作废三种计数 + 告警去重）。读不到 → 空壳（不是故障）。"""
    try:
        raw = _client(redis).get(state_key(day))
        doc = json.loads(raw) if raw else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[LeverageTrim] 状态读取失败（按空状态继续）: %s", exc)
        return {}
    return doc if isinstance(doc, dict) else {}


def save_state(
    redis: Any, day: str, state: Mapping[str, Any], *, merge: bool = True
) -> None:
    """写当日状态（``merge=True`` 时与键上的现值**取大合并**）。

    合并的理由：**同一把状态键有两个写者**（常驻 worker 与操作员 ``--once``/``--force``）。
    后写的进程若整份覆盖，会把另一进程刚记下的尝试次数/已提交笔数/已作废号数/告警去重
    抹回旧值——前两者表现为「失败计数到不了上限，废单一直重试」，作废数回退表现为
    「幂等号代次重算成旧号」（HIGH-1 的死结），最后者表现为「同一条告警每轮再喊一次」。
    这些计数器都是**单调**的，故按 ``max`` 合并是安全方向；告警集合取并集。
    读不出现值时按传入值直写（一次 Redis 抖动不该让本轮的状态丢失）。
    """
    payload = dict(state)
    try:
        if merge:
            current = load_state(redis, day)
            # **空表不入档**：合并只为「不抹掉别人刚写的计数」，不是为了往状态文档里
            # 塞一串空壳（``{}`` 与缺键在读取侧同义，但文档该长成它真正的样子）。
            for key, merged in (
                (
                    "attempts",
                    _merge_counters(current.get("attempts"), payload.get("attempts")),
                ),
                (
                    "submitted",
                    _merge_counters(current.get("submitted"), payload.get("submitted")),
                ),
                (
                    "burned",
                    _merge_counters(current.get("burned"), payload.get("burned")),
                ),
            ):
                if merged or key in payload:
                    payload[key] = merged
            alerted = sorted(
                set(current.get("alerted") or ()) | set(payload.get("alerted") or ())
            )
            if alerted or "alerted" in payload:
                payload["alerted"] = alerted
        _client(redis).set(
            state_key(day), json.dumps(payload, ensure_ascii=False), ex=STATE_TTL_S
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[LeverageTrim] 状态写入失败: %s", exc)


def _as_count(value: Any) -> int:
    """计数字段的宽容解析：脏值按「没有这个数」处理（0），不抛。"""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _merge_counters(
    current: Mapping[str, Any] | None, incoming: Mapping[str, Any] | None
) -> dict[str, int]:
    """两张「按标的计数」表逐标的取大（单调计数器的唯一安全合并）。

    **两侧独立解析**：一侧是脏值（``"??"``）只作废那一侧，不许把另一侧的有效计数一起
    丢掉——合并存在的全部理由就是「别抹掉另一个写者刚记下的数」，而整对丢弃正是那种抹除。
    """
    left = current if isinstance(current, Mapping) else {}
    right = incoming if isinstance(incoming, Mapping) else {}
    out: dict[str, int] = {}
    for symbol in set(left) | set(right):
        out[str(symbol)] = max(
            _as_count(left.get(symbol)), _as_count(right.get(symbol))
        )
    return out


# ── 依赖注入（生产实现在 ``default_trim_deps``）──────────────────────
@dataclass
class TrimDeps:
    """一轮减仓的全部外界依赖。测试逐项替换即可，无需网络/账户/Redis。"""

    #: 账户与行情：QMT 执行端（``get_asset``/``get_positions``/``get_full_tick``/
    #: ``get_instrument_detail``）。**读的必须就是下单去的那座账户**。
    client: Any
    redis: Any
    dispatch: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
    notify: Callable[..., Awaitable[Any]]
    load_tier: Callable[[], Any]
    read_inflight: Callable[[], Awaitable[Any]]
    selected_broker: Callable[[], str]
    real_enabled: Callable[[], bool]
    is_trading_time: Callable[[], bool]
    now: Callable[[], datetime]
    user_id: str
    tenant_id: str = TENANT_ID
    expected_broker: str = EXPECTED_BROKER
    extras: dict[str, Any] = field(default_factory=dict)
    #: 走同一段取数/计划/报价代码，但**不提交、不记失败次数、不告警、不写状态**。
    #: 少了这道闸，跑三次 dry-run 就会把当日尝试次数顶到上限、让真 worker 误以为
    #: 「连续失败已停手」——一个只读演练把真执行器钉死，是典型的自伤。
    #: **两个来源**：操作员 ``--dry-run``（见 ``rehearsal``）与「暂停」态（只报不卖）。
    dry_run: bool = False
    #: ``--dry-run`` 的**演练**位（暂停不置）。差别只在**报告**：演练不是真轮次，
    #: 连状态键都不写——一份假的「上一轮」覆盖运维面板上的真轮次，比不写更糟；
    #: 暂停是**真轮次**（worker 确实跑了一轮并决定不动手），该出现在面板上。
    rehearsal: bool = False
    #: 风险配置视图（``risk_gate_service.load_config``）→ 闸门**正在强制执行**的杠杆上限。
    #: 闸门的上限是 ``min(配置值, 档位值)``，不并进来就会在 ``[配置上限, 档位上限)``
    #: 留一条「闸门拒买、没人压仓」的死区（评审 M2）。``None`` = 未接线的调用点
    #: （测试替身）：配置侧不参与，按档位单独走。
    load_risk_config: Callable[[], Any] | None = None


# ── 账户 / 行情 → 腿输入（纯函数，可单测）────────────────────────────
def _default_is_st(symbol: str) -> bool | None:
    from backend.services.trade.services.decision_executor import is_st_by_name

    return is_st_by_name(symbol)


def _default_threshold(symbol: str, *, is_st: bool, trade_date: date) -> float:
    from backend.services.simulation.services.local_market_data import limit_threshold

    return float(limit_threshold(symbol, is_st=is_st, trade_date=trade_date))


def build_legs(
    positions: Iterable[Mapping[str, Any]],
    ticks: Mapping[str, Any],
    *,
    inflight_keys: frozenset[tuple[str, str]],
    trade_date: date,
    is_st_of: Callable[[str], bool | None] = _default_is_st,
    threshold_of: Callable[..., float] = _default_threshold,
) -> list[LegInput]:
    """柜台持仓 + 桥行情 → 腿输入。

    三处口径都走既有唯一实现，本函数不复制：ST 判定 ``is_st_by_name``（三态：``None``
    = 名称不可得）、涨跌停阈值 ``local_market_data.limit_threshold``（板别/ST/历史改制）、
    在途键 ``read_inflight`` 的 ``(后缀式代码, "sell")``。

    阈值取不到（ST 未知 / 函数抛错）→ ``None``：跌停**不判**（照卖），与决策执行段
    ``quotes_for`` 同一条口径——「不知道」可以容忍，「猜错」会拦下一批本该卖出的单。
    """
    tick_map = {_norm_code(k): v for k, v in (ticks or {}).items()}
    legs: list[LegInput] = []
    for pos in positions or ():
        if not isinstance(pos, Mapping):
            continue
        symbol = _norm_code(pos.get("stock_code") or pos.get("symbol"))
        if not symbol:
            continue
        volume = _finite(pos.get("volume")) or 0.0
        tick = tick_map.get(symbol) or {}
        price = _finite(tick.get("lastPrice")) if isinstance(tick, Mapping) else None
        pre_close = (
            _finite(tick.get("lastClose")) if isinstance(tick, Mapping) else None
        )
        day_chg = (
            price / pre_close - 1
            if price is not None and pre_close not in (None, 0)
            else None
        )
        is_st = is_st_of(symbol)
        threshold: float | None = None
        if is_st is not None:
            try:
                threshold = float(
                    threshold_of(symbol, is_st=is_st, trade_date=trade_date)
                )
            except Exception as exc:  # noqa: BLE001 阈值取不到就不判跌停（核心留痕）
                logger.warning("[LeverageTrim] 涨跌停阈值不可用（%s）：%s", symbol, exc)
        legs.append(
            LegInput(
                symbol=symbol,
                volume=volume,
                available=_finite(pos.get("can_use_volume")),
                price=price,
                market_value=_finite(pos.get("market_value")),
                day_chg_ratio=day_chg,
                limit_threshold_ratio=threshold,
                inflight=(symbol, "sell") in set(inflight_keys or ()),
                name=str(pos.get("instrument_name") or ""),
            )
        )
    return legs


def equity_from_asset(asset: Mapping[str, Any]) -> float | None:
    """账户权益（总杠杆的**分母**）= 现金 + 持仓市值（闸门 ``total_assets`` 同口径）。

    优先柜台自报的 ``total_asset``；缺失时用 ``cash + market_value`` 重建。市值列缺失
    而现金有值 → ``None``（**不拿现金冒充净资产**：那会把杠杆算小、把该减的仓放过去）。
    """
    total = _finite((asset or {}).get("total_asset"))
    if total is not None and total > 0:
        return total
    cash = _finite((asset or {}).get("cash"))
    mv = _finite((asset or {}).get("market_value"))
    if cash is None or mv is None:
        return None
    rebuilt = cash + mv
    return rebuilt if rebuilt > 0 else None


@dataclass
class AccountRead:
    """一次账户快照读取的结果。``errors`` 非空 = **账不可信**（本轮不动手）。"""

    equity: float | None = None
    reported_value: float | None = None
    legs: tuple[LegInput, ...] = ()
    errors: tuple[str, ...] = ()
    positions_count: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors


async def read_account(deps: TrimDeps) -> AccountRead:
    """柜台账户三数 + 持仓 + 行情 → 腿输入（任一处读不出 = 账不可信）。"""
    try:
        asset = await deps.client.get_asset()
    except Exception as exc:  # noqa: BLE001
        return AccountRead(errors=(f"账户资产读取失败：{type(exc).__name__}: {exc}",))
    try:
        positions = await deps.client.get_positions()
    except Exception as exc:  # noqa: BLE001
        return AccountRead(errors=(f"柜台持仓读取失败：{type(exc).__name__}: {exc}",))
    if not isinstance(asset, Mapping):
        return AccountRead(errors=("账户资产返回形态异常（非映射）：账不可信",))

    equity = equity_from_asset(asset)
    codes = [
        _norm_code((pos or {}).get("stock_code") or (pos or {}).get("symbol"))
        for pos in positions or ()
    ]
    codes = [c for c in codes if c]
    try:
        ticks = await deps.client.get_full_tick(codes) if codes else {}
    except Exception as exc:  # noqa: BLE001 无行情不下手（同隔壁 degraded = 只告警）
        return AccountRead(
            equity=equity,
            errors=(f"实时行情拉取失败：{type(exc).__name__}: {exc}",),
            positions_count=len(codes),
        )

    try:
        inflight = await deps.read_inflight()
    except Exception as exc:  # noqa: BLE001
        # 其余四路取数都各自把异常转成 ``AccountRead(errors=…)``，这一路原来裸调用：
        # 生产实现走 ``get_session(read_only=True)``，DB 抖动时异常会**穿出整轮**
        # （编排层的轮次函数对此无 try）——worker 只按「相同文本去重」打一行 ERROR、
        # 心跳照打，运维面板停上一轮的摘要、当日计数不动、**没有任何告警**
        # （评审 M3）。读不出在途 = 账不可信，按 fail-closed 走 blocked + 每日一次告警。
        return AccountRead(
            equity=equity,
            errors=(f"在途委托读取失败：{type(exc).__name__}: {exc}",),
            positions_count=len(codes),
        )
    if not getattr(inflight, "ok", False):
        return AccountRead(
            equity=equity,
            errors=tuple(
                f"在途委托账不可信：{e}" for e in getattr(inflight, "errors", ())
            ),
            positions_count=len(codes),
        )

    legs = build_legs(
        positions,
        ticks or {},
        inflight_keys=frozenset(getattr(inflight, "keys", frozenset()) or ()),
        trade_date=deps.now().date(),
    )
    reported = _finite(asset.get("market_value"))
    if reported is None and not legs:
        # 自报为 None（不是 0）且逐腿也空：两个口径都没数到东西。空仓时柜台自报的是
        # 0.0，所以这个组合是**读取异常**而不是「没有持仓」——不许读成「未超限」。
        return AccountRead(
            equity=equity,
            errors=("账户自报市值缺失且逐腿重建为空（疑似读取异常）：敞口不可得",),
            positions_count=len(codes),
        )
    return AccountRead(
        equity=equity,
        reported_value=reported,
        legs=tuple(legs),
        positions_count=len(codes),
    )


def write_status(redis: Any, summary: Mapping[str, Any]) -> None:
    """状态键：``last`` + ``log``（LPUSH 截断）。失败只告警（不拖垮循环）。"""
    try:
        payload = json.dumps(dict(summary), ensure_ascii=False, default=str)
        r = _client(redis)  # ``lpush``/``ltrim`` 同样只有原生客户端有
        r.set(LAST_KEY, payload, ex=LAST_TTL_S)
        r.lpush(LOG_KEY, payload)
        r.ltrim(LOG_KEY, 0, LOG_KEEP - 1)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[LeverageTrim] 状态键写入失败: %s", exc)


def read_status(redis: Any) -> dict[str, Any]:
    """运维端点用：最近的减仓摘要（读不到 → 空摘要，不抛）。"""
    try:
        raw = _client(redis).get(LAST_KEY)
        if raw:
            doc = json.loads(raw)
            return doc if isinstance(doc, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[LeverageTrim] 状态读取失败: %s", exc)
    return {}


# ── 生产接线 ────────────────────────────────────────────────────────
def default_trim_deps(redis: Any) -> TrimDeps:
    """生产接线：每一项都指向既有唯一实现（本模块不复制任何取数口径）。"""
    from backend.services.live_trading.services.internal_strategy_dispatcher import (
        dispatch_internal_strategy_order,
    )
    from backend.services.live_trading.services.qmt_exec_client import (
        get_qmt_exec_client,
    )
    from backend.services.live_trading.services.trading_session import is_trading_time
    from backend.services.trade.services.decision_executor import read_inflight
    from backend.shared.database_manager_v2 import get_session
    from backend.shared.decision_context_source import now_cn
    from backend.shared.live_trading_gate import is_real_trading_enabled
    from backend.shared.notification_publisher import publish_notification_async
    from backend.shared.real_positions import active_broker_type
    from backend.shared.risk.tiers import load_tier
    from backend.shared.simulation_account_keys import resolve_db_account_user

    from backend.services.trade_shared.deps import get_redis as _get_redis

    user_id = resolve_db_account_user(ENV_ACCOUNT_USER)
    client = get_qmt_exec_client()

    async def dispatch(order_data: dict[str, Any]) -> dict[str, Any]:
        async with get_session() as db:
            return await dispatch_internal_strategy_order(
                order_data=order_data,
                user_id=user_id,
                tenant_id=TENANT_ID,
                redis=redis,
                db=db,
            )

    async def notify(
        user_id_: str,
        title: str,
        content: str,
        level: str = "info",
        tenant_id: str = "default",
    ) -> Any:
        return await publish_notification_async(
            user_id=str(user_id_),
            tenant_id=str(tenant_id or TENANT_ID),
            title=title,
            content=content,
            type="trading",
            level=level,
            action_url="/trading",
        )

    async def read_inflight_fn() -> Any:
        async with get_session(read_only=True) as db:
            return await read_inflight(db, tenant_id=TENANT_ID, user_id=user_id)

    def load_risk_config_fn() -> Any:
        from backend.services.trade.services.risk_gate_service import load_config

        return load_config(_get_redis())

    return TrimDeps(
        client=client,
        redis=redis,
        dispatch=dispatch,
        notify=notify,
        load_tier=lambda: load_tier(_get_redis()),
        load_risk_config=load_risk_config_fn,
        read_inflight=read_inflight_fn,
        selected_broker=lambda: str(active_broker_type(strict=True) or ""),
        real_enabled=is_real_trading_enabled,
        is_trading_time=is_trading_time,
        now=now_cn,
        user_id=user_id,
        tenant_id=TENANT_ID,
    )
