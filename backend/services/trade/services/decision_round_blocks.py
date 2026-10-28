"""决策轮**补块装配**（Stage 2 第一批/第二批）：快照/分数/名单/台账 → 提示词增量。

``decision_round.py`` 与 ``decision_round_io.py`` 都已顶到 800 行预算顶（分层守卫
``test_modules_stay_within_the_file_budget_and_layering``），两批「模型分数 +
市场状态 + 丁1–丁9 块」的装配（要价清单、取数、降级留痕、补块文本）独立成
这一块，**与 io 同层**：只许 import core 与 shared（要调 ``RoundDeps`` 上的取数
闭包，所以进不了纯函数 core），不许碰编排/调度/驱动/io。

两条纪律（细节在各函数 docstring，渲染在 ``shared/decision_blocks`` 与
``shared/decision_risk_blocks``）：

1. **降级不拦轮**：所有块都是增益面——读失败 → 「本轮不可得」如实渲染（丁8
   置顶汇总）并留痕，绝不静默消失、绝不编 0。
2. **未接线 = 历史字节**：``deps`` 上对应闭包为 ``None``（旧替身）→ 对应块整段
   空串，提示词与升级前逐字节一致（上游金样不破）。第二批以
   ``deps.read_series_summary`` 为**总接线标记**（生产 io 同批全量注入，见
   :func:`enrich_quotes_with_blocks`）——没它整批不渲染。丁2 L2 是**自有接线**
   （``deps.load_l2_snapshot``）：数据源独立（通达信 PG 快照），可单独开关。

``RoundBlocks`` 是装配结果的信封：编排层把 ``build_context`` 的历史参数原样
转发给 :func:`build_round_context`，其余各块由 ``replace`` 注入——
**决策轮签名零改动**即完成接线（``decision_round.py`` 已顶 799/800 行）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from backend.services.trade.services.decision_round_core import TENANT_ID, RoundDeps
from backend.shared.decision.context import PoolQuote, RebalanceContext
from backend.shared.decision_blocks import (
    INDEX_CODES,
    attach_signal_scores,
    build_cross_agent_block,
    build_freshness_block,
    build_indices_block,
    build_l2_block,
    build_market_state_block,
    build_minute_block,
    build_mirror_feedback_block,
    build_mirror_gates_block,
    build_news_block,
    build_orderbook_block,
    build_prev_round_block,
    build_score_leaders_block,
    build_signal_meta_block,
    build_tier_block,
)
from backend.shared.decision_context_source import build_context
from backend.shared.decision_risk_blocks import (
    build_industry_caution_block,
    build_risk_block,
)
from backend.shared.env_flags import env_flag
from backend.shared.freshness import FRESH, quote_policy
from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)

#: 毫秒戳归一阈值（快照 ``timestamp`` 契约是秒；>1e12 一律按毫秒折算）。
_MS_TS_CUTOFF = 1e12


def _snap_ts(snap: Mapping[str, Any] | None) -> float | None:
    """快照成员 ``timestamp`` → epoch 秒；缺失/坏值 → ``None``（不猜）。"""
    if not isinstance(snap, Mapping):
        return None
    try:
        ts = float(snap.get("timestamp"))
    except (TypeError, ValueError):
        return None
    if ts > _MS_TS_CUTOFF:
        ts /= 1000.0
    return ts if ts > 0 else None


async def _refresh_quotes_before_prompt(codes: Sequence[str], round_id: str) -> None:
    """提示词前按需直取 TDX 快照（best-effort，**绝不抛出**）。

    **为什么存在**（2026-10-19 用户口径「行情都喂给 agent」）：热集轮转
    ~0.18s/只 × 500+ 只 ≈ 100s 才刷新同一只票一次，而轮首读快照是一瞬间——
    池票在提示词里的期望年龄 ≈50s，模型看到的是「上一轮的市场」。这里对
    持仓+池+指数现查现写 ``market:snapshot/series``，让**提示词与随后的风控
    读同一份新鲜行情**（提交前的逐腿刷新仍保留，见 ``decision_executor``）。

    边界：``QM_DECISION_ROUND_PROMPT_REFRESH=false`` 整体关闭（桥维护时不必
    每轮多等 ≤8s）；桥未配置/限流/单只失败都静默放行——行情是增益面，
    刷不到就按既有快照继续，绝不因刷新失败中止本轮。
    """
    if not env_flag("QM_DECISION_ROUND_PROMPT_REFRESH", default=True):
        return
    try:
        from backend.services.live_trading.services.tdx_quote_feed import (
            refresh_symbols_now,
        )

        result = await refresh_symbols_now(codes) or {}
    except Exception as exc:  # noqa: BLE001 刷新失败按既有快照继续
        logger.warning("[DecisionRound] %s 提示词前行情刷新异常: %s", round_id, exc)
        return
    if result:
        ok = sum(1 for hit in result.values() if hit)
        logger.info(
            "[DecisionRound] %s 提示词前行情刷新 %d/%d 只成功",
            round_id,
            ok,
            len(result),
        )


def risky_symbols(codes: Sequence[str]) -> frozenset[str]:
    """代码 → 权威名称表判定为 ST/退市的集合（后缀式）。

    用本地名称索引（``stocks_index.json``）而不是模型自报的 ``name``：池外买入
    时模型可能给空名或错名，名称判据必须由系统一侧锚定。索引缺失 / 名称查不到
    → 空集（``symbol_policy`` 的 fail-open 口径：宁可漏拦一只，也不能因为名称表
    拉不到把当天所有买入停掉；代码黑名单不依赖名称）。
    """
    from backend.shared.stock_name_mapper import get_stock_name, normalize_symbol
    from backend.shared.symbol_policy import is_risky_name

    out: set[str] = set()
    for raw in codes or ():
        code = str(raw or "").strip()
        if not code:
            continue
        name = get_stock_name(normalize_symbol(code) or code)
        if is_risky_name(name):
            out.add(StockCodeUtil.to_suffix(code))
    return frozenset(out)


async def enrich_decision_quotes(
    *,
    deps: RoundDeps,
    quote_client: Any,
    round_id: str,
    decisions: Sequence[Any],
    snapped: Mapping[str, Mapping[str, Any]],
    gate: Any,
) -> tuple[
    list[str], Mapping[str, Mapping[str, Any]], Any, list[str]
]:
    """池外买入（2026-10-28「股票不限」）：权威名称判定 + 行情现刷。

    返回 ``(点名的买入代码, 合并后的快照, 更新后的闸门, 留痕)``。三条纪律：

    * 名称判据由**系统**锚定（模型自报 ``name`` 可能为空/写错，ST 判定不能信它）；
    * 行情按点名代码**现刷一次**再执行——否则 ``plan_orders`` 按「无价」否决，
      「放开了池」会变成「池外一只都买不进」；
    * 补取失败**不拦轮**：腿到执行段按无价否决并留痕（与行情面其余降级同向）。
    """
    buy_symbols = sorted(
        {
            StockCodeUtil.to_suffix(str(getattr(d, "code", "") or "").strip())
            for d in decisions
            if str(getattr(d, "action", "") or "").strip().lower() == "buy"
            and str(getattr(d, "code", "") or "").strip()
        }
    )
    notes: list[str] = []
    if not buy_symbols:
        return buy_symbols, snapped, gate, notes
    gate = replace(
        gate, risky_symbols=gate.risky_symbols | risky_symbols(buy_symbols)
    )
    missing = [s for s in buy_symbols if s not in snapped]
    if not missing:
        return buy_symbols, snapped, gate, notes
    extra: Mapping[str, Mapping[str, Any]] = {}
    if quote_client is not None:
        await _refresh_quotes_before_prompt(missing, round_id)
        try:
            extra = deps.read_snaps(quote_client, missing) or {}
        except Exception as exc:  # noqa: BLE001 刷不到按无价否决，腿不提交
            logger.warning(
                "[DecisionRound] %s 池外标的行情补取失败 %s: %s",
                round_id,
                missing,
                exc,
            )
    if extra:
        notes.append(f"池外买入 {len(missing)} 只：已现刷行情 {len(extra)} 只")
        return buy_symbols, {**snapped, **extra}, gate, notes
    notes.append(f"池外买入 {len(missing)} 只：行情补取为空（执行段将按无价否决）")
    return buy_symbols, snapped, gate, notes


async def load_round_snapshots(
    deps: RoundDeps,
    round_id: str,
    quote_client: Any,
    positions_list: Sequence[Any],
    pool_rows: Sequence[Any],
    quotes_failure: str,
) -> tuple[
    dict[str, Any] | None, Mapping[str, Mapping[str, Any]], Mapping[str, Any], str
]:
    """③b+④ 盘中 regime + 行情快照 + 持仓行情序列（要价清单装配在编排层之外）。

    要价清单（suffix 形）= 持仓（原始 payload 直接取 symbol，不必先建持仓行）+
    池 + regime 指数 + **丁5 四只展示指数**（``decision_blocks.INDEX_CODES``：
    深市两只是本批新加的常驻源，见 ``hot_set.REGIME_INDEXES``）——指数是
    【盘中市场状态】【大盘指数】块的原料，缺了它对应行如实渲染「未取到」。
    regime（db0 ``qm:regime:intraday``）是**增益块**：未接线/读失败 → ``None``
    （不拦轮、指数不进清单），降级渲染与留痕在
    :func:`enrich_quotes_with_blocks` 收口。

    **读之前先刷**（2026-10-19）：要价清单装配好后先
    :func:`_refresh_quotes_before_prompt` 现查现写（best-effort），再 ``read_snaps``
    ——顺序反了等于没刷。

    **持仓行情序列**（``market:series`` 摘要，2026-10-15 审计修）在快照之外
    单独取一份给持仓行现价用：快照面当日每轮存活性抖动（flash 12:00 全空），
    序列面全程可用——持仓现价 = 快照 ``Now`` → 序列 ``last_price`` → 桥兜底。
    读失败按空表降级（持仓行退回桥价并标 ``‡``，见 ``positions_to_holding_rows``）。

    快照读取失败同样**不在这里拦轮**（守护轮与已有持仓仍要靠行情；「池子整段
    没有价」能否继续由 ⑦ 的行情闸门判）：原因追加进 ``quotes_failure`` 原样返回。
    """
    regime: dict[str, Any] | None = None
    if deps.load_regime is not None:
        try:
            regime = deps.load_regime()
        except Exception as exc:  # noqa: BLE001 增益块异常按不可得降级
            logger.warning("[DecisionRound] %s 盘中 regime 读取异常: %s", round_id, exc)
    held = sorted(
        {
            StockCodeUtil.to_suffix(str(p.get("symbol") or "").strip())
            for p in positions_list
            if isinstance(p, Mapping) and p.get("symbol")
        }
    )
    index = str((regime or {}).get("index") or "").strip()
    pool_set = {
        StockCodeUtil.to_suffix(str(r.code))
        for r in pool_rows
        if str(getattr(r, "code", "") or "").strip()
    }
    codes = sorted(
        set(held)
        | pool_set
        | ({StockCodeUtil.to_suffix(index)} if index else set())
        | set(INDEX_CODES)
    )
    snapped: Mapping[str, Mapping[str, Any]] = {}
    if quote_client is not None and codes:
        await _refresh_quotes_before_prompt(codes, round_id)
        try:
            snapped = deps.read_snaps(quote_client, codes) or {}
        except Exception as exc:  # noqa: BLE001 读失败先记因，是否中止见 ⑦ 的行情闸门
            quotes_failure = f"行情快照读取失败：{type(exc).__name__}: {exc}"
            logger.warning("[DecisionRound] %s（按无行情继续）", quotes_failure)
    series: Mapping[str, Any] = {}
    if deps.read_series_summary is not None and held:
        try:
            series = deps.read_series_summary(held) or {}
        except Exception as exc:  # noqa: BLE001 现价面读失败按桥兜底降级（标 ‡）
            logger.warning("[DecisionRound] %s 持仓行情序列读取异常: %s", round_id, exc)
    return regime, snapped, series, quotes_failure


@dataclass(frozen=True, slots=True)
class RoundBlocks:
    """补块信封：进 ``build_context`` 的两块 + Stage 2 各新块（同名同义字段）。

    前两个字段由编排层原样转进 ``build_context``（历史参数位）；其余交给
    :func:`build_round_context` 用 ``replace`` 落到 ``RebalanceContext`` 的对应
    字段（含既有风险两槽与 ``extra_context`` 唤醒位）。默认空串 = 未接线，
    渲染端逐字节冻结。
    """

    market_state_block: str = ""
    signal_meta_block: str = ""
    alert_block: str = ""
    indices_block: str = ""
    stage2_block: str = ""
    tier_block: str = ""
    extra_context: str = ""
    risk_block: str = ""
    industry_caution_block: str = ""
    #: 【研究总控·交易日前瞻】（前一日夜间综合 + 当日各档复盘）。空串 = 未接线/未产出。
    research_block: str = ""


def build_round_context(blocks: RoundBlocks, **legacy: Any) -> RebalanceContext:
    """装配信封 → ``RebalanceContext``：``legacy`` = ``build_context`` 的原始参数。

    **编排层的签名零改动**：它把 ``blocks`` 与历史 kwargs 一起递进来，其余各块在
    这里注入。``replace`` 要求字段存在（Stage 2 第二批已加）——字段名写错会
    即刻 TypeError，不会静默丢块。
    """
    return replace(
        build_context(**legacy),
        alert_block=blocks.alert_block,
        indices_block=blocks.indices_block,
        stage2_block=blocks.stage2_block,
        tier_block=blocks.tier_block,
        extra_context=blocks.extra_context,
        risk_block=blocks.risk_block,
        industry_caution_block=blocks.industry_caution_block,
        research_block=blocks.research_block,
    )


def _snap_freshness_alert(
    snapped: Mapping[str, Mapping[str, Any]], now: datetime
) -> str:
    """行情快照整体新鲜度一句话（``freshness.quote_policy`` 唯一分级）；fresh → ""。"""
    age_s: float | None = None
    for snap in snapped.values():
        ts = _snap_ts(snap)
        if ts is None:
            continue
        age = now.timestamp() - ts
        age_s = age if age_s is None else min(age_s, age)
    if age_s is None:
        return "行情快照无时间戳：新鲜度无法验证" if snapped else ""
    if age_s < -60:
        return "行情快照时间戳异常（未来戳）：新鲜度无法验证"
    level = quote_policy().classify(age_s)
    if level == FRESH:
        return ""
    return f"行情快照整体不新鲜（最新 {age_s / 60:.0f} 分钟前，分级 {level}）"


async def enrich_quotes_with_blocks(
    deps: RoundDeps,
    round_id: str,
    agent: str,
    user: str,
    regime: Mapping[str, Any] | None,
    snapped: Mapping[str, Mapping[str, Any]],
    quotes: Sequence[PoolQuote],
    holdings: Sequence[Any],
    now: datetime,
    notes: list[str],
    tier: Any | None = None,
    include_score_leaders: bool = False,
) -> tuple[tuple[PoolQuote, ...], RoundBlocks]:
    """⑦b+⑦c 行情行填「信号分」+ 全部提示词补块（两批合并收口）。

    返回 ``(填分后的行情, 信封)``。口径：

    1. 分数取数只有 ``load_score_snapshot`` 一个入口（``deps.load_scores``）；
       抛异常或 ``ok=False`` → **降级不拦轮**：行内无后缀、信号块如实说
       「不可得」，原因进 ``notes``（审计）与丁8。绝不编 0。
    2. **总接线标记 = ``deps.read_series_summary``**（生产 ``default_round_deps``
       第二批闭包同批全量注入）：为 ``None``（旧替身）→ 丁1/丁3/丁2/丁5/丁7
       整批空串，提示词逐字节等于历史。丁4/丁6/丁9/风险两槽/唤醒块/镜像两块
       各自以自己的闭包（``load_news_files``/``load_decision_rows``/
       ``load_risk_files``/``read_wake_ctx``/``read_mirror_ctx``）为门——
       未接线即空串。
    3. 降级姿态：每块「本轮不可得」如实渲染；**汇总一句进丁8置顶**
       （``build_freshness_block``），读不到与没有是两件事。
    4. 丁7 档位经 ``deps.load_tier`` **再读一次**（编排 ⑤ 已读）：同源同值，
       重复读只是为免改编排签名；失败按未接线降级。
    """
    # ── 分数（Stage 2 第一批，原样）─────────────────────────────────
    score_map: dict[str, dict[str, Any]] = {}
    score_meta: dict[str, Any] = {"ok": False, "reason": "分数快照未接线"}
    scores_wired = deps.load_scores is not None
    if scores_wired:
        try:
            score_map, score_meta = await deps.load_scores(TENANT_ID, user)
        except Exception as exc:  # noqa: BLE001 取数异常按不可得降级（不拦轮）
            logger.warning("[DecisionRound] %s 分数快照读取异常: %s", round_id, exc)
            score_meta = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
        if not score_meta.get("ok"):
            note = f"模型分数不可得（{score_meta.get('reason')}）：行情行内无「信号分」"
            notes.append(note)
            logger.warning("[DecisionRound] %s %s", round_id, note)
    scored_quotes, scored_n = attach_signal_scores(quotes, score_map)
    scored_seq = tuple(scored_quotes)

    market_state_block = ""
    if deps.load_regime is not None:
        index = str((regime or {}).get("index") or "").strip()
        index_snap = snapped.get(StockCodeUtil.to_suffix(index)) if index else None
        market_state_block = build_market_state_block(regime, index_snap, now)
        if regime is None:
            note = "盘中 regime 不可得：市场状态块本轮为「不可得」降级"
            notes.append(note)
            logger.warning("[DecisionRound] %s %s", round_id, note)
    signal_meta_block = (
        build_signal_meta_block(score_meta, filled=scored_n, total=len(scored_seq))
        if scores_wired
        else ""
    )
    # 2026-10-28「股票不限」：候选池只有 20 只，池外高分票需要榜面才看得见。
    if include_score_leaders and score_map:
        leaders = build_score_leaders_block(score_map)
        if leaders:
            signal_meta_block = (
                f"{signal_meta_block}\n\n{leaders}" if signal_meta_block else leaders
            )

    # ── Stage 2 第二批：先收集降级留痕（丁8 置顶汇总）───────────────
    alerts: list[str] = []
    if scores_wired and not score_meta.get("ok"):
        alerts.append(f"模型分数不可得（{score_meta.get('reason')}）")
    if deps.load_regime is not None and regime is None:
        alerts.append("盘中 regime 不可得：市场状态块降级")
    enabled = deps.read_series_summary is not None  # 本批总接线标记
    held_codes = [str(h.code) for h in holdings]
    pool_codes = [str(q.code) for q in scored_seq]

    # 丁5 指数行（快照取数面）
    indices_block = ""
    if enabled:
        indices_block = build_indices_block(snapped, now)
        if not snapped:
            alerts.append("行情快照整体未取到（快照源不可用）")
        else:
            have = sum(1 for c in INDEX_CODES if snapped.get(c))
            if have < len(INDEX_CODES):
                alerts.append(f"指数快照未取全（{have}/{len(INDEX_CODES)}）")
        alert = _snap_freshness_alert(snapped, now)
        if alert:
            alerts.append(alert)

    # 【研究总控·交易日前瞻】：前一日夜间综合 + 当日各档复盘 → 决策模型可见。
    # 未接线/尚未产出是常态（首次上线、当日未跑）：不占位、不告警，绝不臆造。
    research_block = ""
    if deps.load_research_brief is not None:
        try:
            research_block = str(deps.load_research_brief(now.strftime("%Y%m%d")) or "")
        except Exception as exc:  # noqa: BLE001 研究面读失败不挡决策
            logger.warning(
                "[DecisionRound] %s 研究总控 brief 读取异常: %s", round_id, exc
            )

    # 丁1 盘口 / 丁3 分钟（微观结构面，同属总接线标记）
    orderbook_block = minute_block = l2_block = ""
    if enabled:
        series: Mapping[str, Any] = {}
        # 序列一次读持仓+池（2026-10-19：池票的五分钟涨速与持仓同口径；
        # 30 个键对一条管线是小数，见 feeds.series_summaries 的容量注释）。
        series_codes = list(dict.fromkeys(held_codes + pool_codes))
        if series_codes:
            try:
                series = deps.read_series_summary(series_codes) or {}
            except Exception as exc:  # noqa: BLE001 序列读不到按不可得降级
                logger.warning("[DecisionRound] %s 行情序列读取异常: %s", round_id, exc)
                series = {}
        orderbook_block = build_orderbook_block(
            holdings, snapped, series, now, pool=scored_seq
        )
        minute_block = build_minute_block(holdings, series, now)
        if holdings and not series:
            alerts.append("行情序列 market:series 本轮无成员：盘口涨速/分钟特征缺位")

    # 丁2 通达信付费 L2（自有接线：采集任务写 PG，这里读当日最新行）。
    # 与盘口/分钟不同源：总接线标记为 None 时仍可单独接线；未接线 → 历史门控字节。
    if deps.load_l2_snapshot is not None and (held_codes or pool_codes):
        l2_codes = list(dict.fromkeys(held_codes + pool_codes))
        l2_rows: Mapping[str, Any] = {}
        l2_note = ""
        try:
            l2_rows = await deps.load_l2_snapshot(l2_codes, now.date()) or {}
        except Exception as exc:  # noqa: BLE001 L2 读不到按不可得降级（不拦轮）
            logger.warning("[DecisionRound] %s L2 快照读取异常: %s", round_id, exc)
            l2_note = (
                f"通达信 L2 快照读取失败（{type(exc).__name__}）：L2 微观结构块降级"
            )
        else:
            if not l2_rows:
                l2_note = "通达信 L2 快照本轮无数据：L2 微观结构块降级"
        if l2_note:
            alerts.append(l2_note)
        l2_block = build_l2_block(l2_rows, holdings, scored_seq, now)
    elif enabled:
        # 第二批总接线但 L2 未单独接线：历史门控字节（旧替身/存量部署不改提示词）。
        l2_block = build_l2_block()

    # 丁4 新闻（自有接线）
    news_block = ""
    if deps.load_news_files is not None:
        brief: Mapping[str, Any] | None = None
        watch: Mapping[str, Any] | None = None
        try:
            brief, watch = deps.load_news_files()
        except Exception as exc:  # noqa: BLE001 简报读不到按不可得降级
            logger.warning("[DecisionRound] %s 新闻简报读取异常: %s", round_id, exc)
        news_block = build_news_block(brief, watch, held_codes, now)
        if brief is None:
            alerts.append("新闻简报文件缺失：盘中消息面缺位")

    # 丁6 跨 agent / 丁9 上一轮（台账，自有接线）
    cross_block = prev_block = ""
    if deps.load_decision_rows is not None:
        try:
            rows: Sequence[Any] = await deps.load_decision_rows(now.date(), user)
        except Exception as exc:  # noqa: BLE001 读失败两块整段不出（防谎报「首轮」）
            logger.warning("[DecisionRound] %s 决策台账读取异常: %s", round_id, exc)
            alerts.append(
                f"决策台账读取失败（{type(exc).__name__}）：互看/上一轮块降级"
            )
        else:
            rows = rows or ()
            cross_block = build_cross_agent_block(rows, agent)
            prev_block = build_prev_round_block(rows, agent, now)

    # 镜像闸门/回注（2026-10-15 审计 P0-B/P0-C）：镜像启用时把生效硬约束与当日
    # 被跳过的腿送进提示词——模型看不见闸门就会产出必被跳的腿（全天零反馈）。
    mirror_gates_block = mirror_feedback_block = ""
    if deps.read_mirror_ctx is not None:
        try:
            mirror_ctx: Mapping[str, Any] = deps.read_mirror_ctx() or {}
        except Exception as exc:  # noqa: BLE001 读不到按未接线降级（不往提示词涂鸦）
            logger.warning("[DecisionRound] %s 镜像上下文读取异常: %s", round_id, exc)
        else:
            if mirror_ctx.get("active"):
                mirror_gates_block = build_mirror_gates_block(
                    mirror_ctx.get("limits") or {}
                )
                mirror_feedback_block = build_mirror_feedback_block(
                    mirror_ctx.get("skips")
                )
                if mirror_ctx.get("skips") is None:
                    alerts.append("镜像跳过台账读取失败：执行受阻回注降级")

    stage2_block = "\n\n".join(
        part
        for part in (
            mirror_gates_block,
            mirror_feedback_block,
            orderbook_block,
            minute_block,
            l2_block,
            news_block,
            cross_block,
            prev_block,
        )
        if part
    )

    # 风险两槽（risk_block / industry_caution，上游既有槽，第二批补取数）
    risk_block = ""
    industry_caution = ""
    if deps.load_risk_files is not None:
        risk_doc: Mapping[str, Any] | None = None
        industry_doc: Mapping[str, Any] | None = None
        fund_doc: Mapping[str, Any] | None = None
        try:
            risk_doc, industry_doc, fund_doc = deps.load_risk_files()
        except Exception as exc:  # noqa: BLE001 名单读不到按缺位降级
            logger.warning("[DecisionRound] %s 风险清单读取异常: %s", round_id, exc)
        names = {str(h.code): str(h.name) for h in holdings}
        names.update({str(q.code): str(q.name) for q in scored_seq})
        risk_block = build_risk_block(
            risk_doc, fund_doc, held_codes, pool_codes, names, now
        )
        industry_caution = build_industry_caution_block(
            industry_doc, held_codes, pool_codes
        )
        if risk_doc is None:
            alerts.append("事件风险清单未产出：风险警示段缺位")
        if industry_doc is None:
            alerts.append("行业风险清单未产出：行业提示段缺位")

    # 丁7 档位（总接线标记门控；读失败按未接线降级）。``tier`` 由编排层传入
    # **生效档位**（资金策略覆盖后的两个旋钮），保证提示词里的上限与闸门同值。
    tier_block = ""
    if enabled:
        try:
            tier_block = build_tier_block(
                tier if tier is not None else deps.load_tier()
            )
        except Exception as exc:  # noqa: BLE001 档位读不到按未接线降级
            logger.warning("[DecisionRound] %s 档位读取异常: %s", round_id, exc)

    # 丙1 唤醒上下文（合成槽轮的触发说明；非唤醒轮读回空串）
    extra_context = ""
    if deps.read_wake_ctx is not None:
        try:
            extra_context = str(deps.read_wake_ctx(round_id) or "")
        except Exception as exc:  # noqa: BLE001 读失败按非唤醒轮降级
            logger.warning("[DecisionRound] %s 唤醒上下文读取异常: %s", round_id, exc)

    blocks = RoundBlocks(
        market_state_block=market_state_block,
        signal_meta_block=signal_meta_block,
        alert_block=build_freshness_block(alerts),
        indices_block=indices_block,
        stage2_block=stage2_block,
        tier_block=tier_block,
        extra_context=extra_context,
        risk_block=risk_block,
        industry_caution_block=industry_caution,
        research_block=research_block,
    )
    return scored_seq, blocks
