"""决策轮调度（P2.8）：到点 → 取数 → 提示词 → LLM → 闸门 → 执行 → 守护 → 审计。

本模块是 **P2 决策链的唯一生产者**：在它之前，``gates.filter_pool``（P2.1b）、
``watch_writer.write_watch_plan``（P2.1d）、``decision_ledger_store``（P2.1e）、
``decision_executor.run_round``（P2.3b）四件都已交付但**零生产调用方**——规则在、
单测在、生产者不在（登记表里判为 ``kind=unseen`` 的那些条目就是这么来的）。
本模块把它们串成一轮，并按下表到点触发。

槽位表 = 隔壁 quant-Trader 的 crontab（北京时间），**不用 cron**
------------------------------------------------------------
===========  =========  ==================================================
北京时刻       schema     职责
===========  =========  ==================================================
08:30        intraday   盘前决策（守护规则为主；未开盘不提交腿）
09:00        intraday   开盘前再决策（集合竞价 09:15 之前同样不提交）
09:35        rebalance  **建仓轮**：hold / sell / buy，提交腿
10:00        intraday   整点守护轮
10:05        rebalance  补跑（当日未出过 rebalance 决策才跑）
11:00        intraday   整点守护轮
11:05        rebalance  补跑（同上）
12:00        intraday   午间再决策（非交易时段不提交）
13:00/14:00  intraday   整点守护轮
14:45        intraday   尾盘决策
===========  =========  ==================================================

隔壁 crontab 的三条来源：``live_llm_trade.py``（09:35 主决策 + 10:05/11:05
``--catch-up``）与 ``live_hourly_analysis.py``（08:30 / 09:00–14:00 整点 / 14:45，
见 ``docs/local/quant-trader-migration-plan.md`` §P4）。它那侧靠系统 cron 拉起
一次性脚本，本仓**不迁 crontab**：轮次是 trade 服务里的常驻循环，理由三条——
① 隔壁那种「crontab 拉起 → 脚本自己读 .env → 打完就退」的形态在容器里等于把
`.env` 和密钥又抄一份；② 每轮要读 PG/Redis/远端行情三处连接，进程常驻才有连接
复用；③ 漏跑与重跑都在 Redis 键上（见下），cron 的「错过就永远错过」在这里被
45 分钟宽限窗 + 补跑槽取代。

Redis 键（全部在**交易库** DB2）与幂等
------------------------------------
* ``trade:decision-round:slot:{day}:{HHMM}:{schema}``——槽位认领（``SET NX EX 2d``，
  值 ``auto``/``manual``）。多实例/重启/自动轮共用一把锁：谁先 ``set`` 成功谁跑这一槽。
  ``--force``（手动重跑）走**覆盖写**：已在册的槽位也放行，否则「重跑」对任何跑过
  一次的槽位都是空转——认领键值即「谁认的」，排障时先看它。
* ``trade:decision-round:done:{day}:{schema}``——当日**该 schema 已出过决策**
  （``EX 7d``）。补跑槽查它，主槽不查。
* ``trade:decision-round:last`` / ``:log``——最近一轮状态（前端/体检读）。
* 腿的幂等键 = ``build_llm_decision_client_order_id(round_id, symbol, side, agent)``，
  ``round_id = rnd-{YYYYMMDD}-{HHMM}``。同槽重试 → 同键 → 被去重；**补跑是另一槽**
  （10:05 的 round_id 不是 09:35 的）→ 不同键，所以补跑的重复提交防线是**在途账**
  （``run_round`` 读不到在途即整轮不下单，见 decision_executor 模块 docstring）。

为什么 claim/done/status 走**原生** redis-py 客户端
------------------------------------------------
（这三把键的读写都在调度层 ``decision_round_tick``；这条口径是两层的共同前提，
写在这里免得读调度层的人回头找。）
``trade_shared.redis_client.RedisClient`` 的 ``get``/``set`` 会把任何异常吞成
``None``/``None``（只记一条日志）。用 ``set(key, nx=True)`` 认领槽位时，一次写失败
返回 ``None`` 会被读成「别人已经跑过了」——这一轮**永不执行且无人报错**，正是
``sltp_executor`` 里那套 ``read_key_strict`` 修的病。故调度层持有原生客户端
（异常可见），而规则表/档位仍走包装客户端（它们内部自带 ``_raw_client`` 解包，
不再添第 9 份拷贝）。

fail-closed 分层（每一层的姿态都是**选的**，不是顺手写的）
--------------------------------------------------------
=========================  ==========================================
读不到的东西                 姿态
=========================  ==========================================
账户身份（user_id）           abort：不问模型也不下单
持仓（real_account_snapshots）abort：模型看不到持仓就不该动它
资金面（cash/market_value）   abort：真实账户现金/市值仍是内部风控与持仓一致性
                             判据；提示词额度只认分账授予额度（见 ⑦）
分账账本段（P2.7）            abort：读不到就不知道哪些票是自己的
                             （当成空账本 ⇒ 该卖的卖不掉；回退全账户
                             持仓 ⇒ 2026-09-08 跨 agent 卖仓）
券商选择                       abort（仅决策轮）：``broker:selected:CN``
                             读不到就不知道读哪座账（两座差 ~25 倍），
                             不许回退 env 默认掷硬币
行情客户端/快照               **看池面**：池里一只价都取不到时——
                             交易时段 abort（模型只能看到空池，出的不是
                             决策）；非交易时段与「只有部分无价」降级，
                             行情块整段不出现、执行段按「无价」否决
候选池文件                    跑，响亮留痕（守护轮不依赖池）
排除名单                      跑，``blocked_symbols=()`` + 留痕
                             （对齐档位层「absent 不是故障」）
档位 ``per_stock_pct`` 非法    abort：把 15 当 15% 会让单票夹取成空操作
执行段 aborted 且零提交        abort：且**不许覆盖守护规则表**（整组替换
                             会把既有止损清掉、给未买入的标的 arm 规则）
=========================  ==========================================

**调度层**（``decision_round_tick``）另有一张表：槽位认领、补跑去重、状态键，
它的失败姿态（认领不了就不跑、补跑读不到 done 键就不跑且放掉认领……）见那个
模块的 docstring——这一层与「一轮里发生了什么」是两件事，故与本文分家。

非交易时段（含午休）：**照常出决策与守护规则，腿一条不提交**——守护规则就是
给开盘用的，止盈止损规则不因当前是午休而失效。拒发发生在提交器上（每条腿留一行
回执），与隔壁把「连续竞价时段闸」放在下单检查链里、拒单即留痕同形。

实盘/模拟
--------
``real`` 现读 ``shared.live_trading_gate.is_real_trading_enabled()``（一轮内不变），
关着时腿走模拟台账（影子期口径，与 ``push_orders`` 同形），并把 ``mode`` 写进
审计 ``context_meta`` 与状态键——**模式永远可见**，不做静默切换。

编号说明
--------
本批是 **P2.8**：计划里 P2.5 已被「``live_price_watch.py`` 不搬」（其职责由
P1.3 桥自带止损 + 本模块的守护规则吸收）占用，批次号**不复用**。

CLI / 常驻 worker：**不在这里**，见 ``decision_round_runner.py``（驱动层）。本模块只回答
「一轮里发生了什么」；``python -m backend.services.trade.services.decision_round_runner
[--slot HHMM] [--dry-run]`` 才是操作员入口。

分层（单向依赖，测试有源码守卫）::

    runner（常驻循环/CLI） → tick（到点/认领/去重/状态） → round（本模块：一轮）
                          → io（生产接线） → core（纯逻辑）
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Any

from backend.shared.decision.gates import BuyGate
from backend.shared.order_contract import normalize_agent
from backend.services.trade.services.decision_round_core import (
    ACCOUNT_AGE_WARN_MIN,
    STATUS_ABORTED,
    STATUS_ERROR,
    STATUS_LLM_FAILED,
    STATUS_OK,
    TENANT_ID,
    RoundDeps,
    RoundResult,
    RoundSlot,
    abort_result,
    account_meta,
    build_pool_stamps,
    clip_holdings_to_ledger,
    context_meta,
    merge_outcomes,
    position_source_meta,
    positions_consistency_issue,
    refusing_submitter,
    round_id_for,
    snap_price,
)
from backend.services.trade.services.decision_round_blocks import (
    build_round_context,
    enrich_decision_quotes,
    enrich_quotes_with_blocks,
    load_round_snapshots,
    risky_symbols,
)
from backend.services.trade.services.decision_round_io import (
    maybe_write_watch,
    record_round_dialogue,
    record_round_position,
)
from backend.services.trade.services.decision_round_notes import (
    exec_summary,
    round_highlights,
)
from backend.services.trade.services.decision_round_policy import (
    capital_policy,
    filter_affordable_pool,
    tier_with_policy,
)

logger = logging.getLogger(__name__)


async def run_once(
    slot: RoundSlot,
    *,
    deps: RoundDeps,
    now: datetime | None = None,
    day: date | None = None,
) -> RoundResult:
    """跑一轮（**前提：槽位已认领**；取数→提示词→LLM→执行→守护→审计）。

    不含 Redis 认领/去重（那是 ``decision_round_tick.round_tick`` 的事）：本函数可被
    CLI、测试、补跑器直接调用，语义是「现在，就按这个槽位跑一轮」。
    """

    now = now or deps.now()
    day = day or now.date()
    round_id = round_id_for(day, slot)
    mode = "real" if deps.real_enabled() else "sim"
    started = now

    try:
        return await _run_once_inner(
            slot,
            deps=deps,
            now=now,
            day=day,
            round_id=round_id,
            mode=mode,
            started=started,
        )
    except Exception as exc:  # noqa: BLE001 一轮炸掉不许带走整个 worker
        logger.error("[DecisionRound] %s 编排异常: %s", round_id, exc, exc_info=True)
        return RoundResult(
            status=STATUS_ERROR,
            day=day,
            slot=slot,
            round_id=round_id,
            note=f"{type(exc).__name__}: {exc}",
            errors=(f"{type(exc).__name__}: {exc}",),
            mode=mode,
        )


async def _run_once_inner(
    slot: RoundSlot,
    *,
    deps: RoundDeps,
    now: datetime,
    day: date,
    round_id: str,
    mode: str,
    started: datetime,
) -> RoundResult:
    from backend.shared.decision.context import (
        DirectionBlock,
        render_prompt_for,
    )
    from backend.shared.decision.watch_map import plan_watch
    from backend.shared.decision_context_source import (
        positions_to_holding_rows,
        quotes_from_snapshots,
    )
    from backend.shared.decision_ledger_store import build_records
    from backend.services.trade.services.decision_executor import (
        holdings_from_rows,
        quotes_for,
    )

    # ① 账户身份 → 持仓 → 资金面（三者任一不可得 = 本轮不做，见模块 docstring）
    user = str(deps.account_user() or "").strip()
    if not user:
        return abort_result(day, slot, "账户身份为空：本轮不做（fail-closed）")

    try:
        positions, pos_meta = await deps.load_positions(TENANT_ID, user)
    except Exception as exc:  # noqa: BLE001
        return abort_result(
            day, slot, f"持仓读取失败：{type(exc).__name__}: {exc}（本轮不问模型）"
        )
    try:
        account = await deps.load_account(TENANT_ID, user)
    except Exception as exc:  # noqa: BLE001
        return abort_result(
            day, slot, f"资金面读取异常：{type(exc).__name__}: {exc}（本轮不问模型）"
        )
    if not account.ok:
        return abort_result(
            day,
            slot,
            "资金面不可信：" + "；".join(account.errors),
            account=account_meta(account),
        )
    if account.age_min is not None and account.age_min > ACCOUNT_AGE_WARN_MIN:
        logger.warning(
            "[DecisionRound] 账户快照偏旧：%.1f 分钟（source=%s snapshot_at=%s）——"
            "按此资金面决策，年龄已记入审计 context_meta",
            account.age_min,
            account.source,
            account.snapshot_at,
        )
    # 「空仓」与「持仓链路断了」长得一样，但一个照跑、一个必须停：持仓为空而市值明显
    # 非零时按前者决策，结果是该卖的卖不掉、不该买的照买。判据只用同一行快照的两个数。
    pos_issue = positions_consistency_issue(
        holdings=len(positions or {}),
        market_value=account.market_value,
        total_asset=account.total_asset,
        sources=position_source_meta(pos_meta),
    )
    if pos_issue:
        logger.error("[DecisionRound] %s %s", round_id, pos_issue)
        return abort_result(
            day,
            slot,
            pos_issue,
            account=account_meta(account),
            positions=pos_meta or {},
        )

    # ② LLM 绑定（只读 env，先解析：未配置就没有必要再取行情/跑滤池）
    try:
        binding = deps.load_llm()
    except Exception as exc:  # noqa: BLE001 未配置/解析失败都在这
        return RoundResult(
            status=STATUS_LLM_FAILED,
            day=day,
            slot=slot,
            round_id=round_id,
            note=f"LLM 未就绪：{type(exc).__name__}: {exc}",
            errors=(f"{type(exc).__name__}: {exc}",),
            mode=mode,
        )
    # agent 名先归一（去空白 + 按列宽截断）：它是幂等键段/账本段/两张订单表的**同一个
    # 键**（口径见 normalize_agent）。归一只做在身份上，API 调用仍用 binding.model 原名。
    agent = normalize_agent(binding.model)

    # ②b 分账账本（P2.7）：本 agent 名下的持仓 + 子账户虚拟现金。**在 ② 之后**是因为
    # 账本按 agent 切段，而 agent 名来自 LLM 绑定（纯 env 解析，不花钱不触网）。
    # 读失败 = 中止本轮，**不是**「当成空账本继续」：看不到自己的账就不知道哪些票是
    # 自己的——把「读不到」当「名下没有货」会让该卖的卖不掉且账面全绿；反过来回退到
    # 全账户持仓就是 2026-09-08 那次跨 agent 卖仓（pro 卖了 flash 的生益电子）。
    try:
        ledger = await deps.load_agent_ledger(TENANT_ID, user, agent)
    except Exception as exc:  # noqa: BLE001
        return abort_result(
            day,
            slot,
            f"分账账本读取异常：{type(exc).__name__}: {exc}（本轮不问模型）",
        )
    if not ledger.ok:
        return abort_result(
            day,
            slot,
            "分账账本不可读："
            + "；".join(ledger.errors or ("未报告原因",))
            + "（本轮不问模型）",
        )
    virtual_cash = float(ledger.virtual_cash)
    #: 必须可见的说明（没判/口径回退…）：先于裁剪持仓建表（不能等到 ⑥）——它是「模型为什么对这几只票视而不见」的唯一答案。
    notes: list[str] = []

    # ③ 候选池（日格式 **%Y%m%d**——传 ISO 日期会静默拿到 None）
    pool_doc = deps.load_pool(day.strftime("%Y%m%d"))
    pool_rows: tuple[Any, ...] = tuple(getattr(pool_doc, "rows", ()) or ())
    direction = getattr(pool_doc, "direction", None) or DirectionBlock()
    pool_file = str(getattr(pool_doc, "source", "") or "")
    if pool_doc is None:
        logger.warning(
            "[DecisionRound] %s 无候选池文件（%s/）：本轮不筛候选，池外票被 l2.pool_not_member 拦下",
            round_id,
            day.strftime("%Y%m%d"),
        )

    # ③b+④ 行情。客户端不可用/读失败本身**不在这里中止**：守护轮与已有持仓仍要靠它
    # （「池子整段没有价」能否继续取决于时段，由 ⑦ 行情闸门判）。regime 与要价清单
    # （持仓+池+指数）的装配口径收在 io 助手。
    quote_client = None
    #: 行情不可用的**原因**（客户端构造失败/读取异常），进 abort 理由与日志。
    quotes_failure = ""
    holding_meta = pos_meta or {}
    positions_list = list((positions or {}).values())
    try:
        quote_client = deps.quote_client()
    except Exception as exc:  # noqa: BLE001
        quotes_failure = f"行情客户端不可用：{type(exc).__name__}: {exc}"
        logger.warning("[DecisionRound] %s", quotes_failure)
    regime, snapped, series, quotes_failure = await load_round_snapshots(
        deps, round_id, quote_client, positions_list, pool_rows, quotes_failure
    )
    holding_rows = positions_to_holding_rows(
        positions_list, snaps=snapped, series=series
    )

    # ④b 分账裁剪（P2.7）：只看本 agent 名下的持仓（归属/可卖两条口径见
    # `clip_holdings_to_ledger`：账本管归属、桥管 T+1 可卖，绝不互相回退）。
    all_rows = holding_rows
    holding_rows, hidden = clip_holdings_to_ledger(all_rows, ledger.positions)
    if hidden:
        # 一定要说出来：迁移未完成（历史仓位尚未导入分账账本）时，这一行的存在就是
        # 「模型为什么对这几只票视而不见」的答案。
        note = (
            f"分账裁剪：桥账户 {len(all_rows)} 只持仓中 {hidden} 只不在本 agent"
            f"（{agent}）名下，已从提示词与执行段一并隐藏"
        )
        notes.append(note)
        logger.info("[DecisionRound] %s %s", round_id, note)

        if not holding_rows:
            logger.warning(
                "[DecisionRound] %s 本 agent（%s）名下**一只都没有**，而桥账户持有 %d 只："
                "本轮模型看不到任何持仓（只可能买、不可能卖）。历史仓位若尚未导入"
                "分账账本，这是预期行为",
                round_id,
                agent,
                len(all_rows),
            )

    # ⑤ 档位（读失败由 tiers 自己回退收紧，不抛）；显式资金策略覆盖档位
    #    （2026-10-28 用户口径：3×¥20 万、单笔上限 ¥20 万 → 单票比例 100%）。
    from backend.shared.decision_llm_client import effective_env

    tier = deps.load_tier()
    policy = capital_policy(tier, env=effective_env())
    per_stock_pct, max_new_buys = policy.per_stock_pct, policy.max_new_buys
    notes.extend(policy.notes)
    # 提示词里的档位块与闸门同值：覆盖后的两个旋钮写回生效档位（source 留痕）。
    effective_tier = tier_with_policy(tier, policy)

    # ⑥ 排除名单（缺失 ≠ 空名单）
    excluded = deps.load_excluded()
    if not excluded.present:
        notes.append(excluded.note)

    # ⑦ 闸门的候选侧：先按「买得起一手」筛池，再交给模型
    # 额度口径（2026-10-19 用户拍板）：**只认用户授予的分账额度**（¥10 万/agent），
    # 不把真实账户的 cash/市值/总资产喂给模型——账户里有多少钱是账户主人的隐私，
    # 模型只需要知道「这条线还能动用多少」。真实账户三数仍进内部风控（执行段的
    # `l1.available_cash`、桥可卖量）与审计 `context_meta.account`，但不进提示词。
    quota_total = ledger.quota_total
    quota_used = ledger.quota_used
    # 单票预算取**两个口径中更紧的那个**（与隔壁 `min(remaining*PER_STOCK_PCT,
    # agent_virtual_cash)` 同形）：授予额度口径决定「这条线还能动用多少」，分账虚拟
    # 现金决定「账上还有多少现钱」。只取前者，会在现金已不足时给出买不起的池面；
    # 只取后者，盈利卖出会让可动用额度超过用户授予的上限。
    per_stock_budget = round((quota_total - quota_used) * per_stock_pct, 2)
    if virtual_cash < per_stock_budget:
        per_stock_budget = round(virtual_cash, 2)
    try:
        gate = BuyGate(
            pool_codes=frozenset(str(r.code) for r in pool_rows),
            blocked_symbols=excluded.symbols,
            risky_symbols=risky_symbols(str(r.code) for r in pool_rows),
            per_stock_pct=per_stock_pct,
            allow_st=False,
            allow_out_of_pool=policy.allow_out_of_pool,
            max_new_buys_round=max_new_buys,
            # 子账户虚拟现金（本 agent 的名义额度余额）：买入腿的硬上限，
            # 与真实账户现金**各自独立**成立（隔壁 `o["cost"] > vcash` 那条闸）。
            virtual_cash=virtual_cash,
        )
    except ValueError as exc:
        # per_stock_pct 不是比例（例如档位文档里写了 15）：夹取会变成空操作，
        # 一笔打到满额——这种配置下**不能**继续（见 gates.BuyGate docstring）。
        return abort_result(day, slot, f"闸门参数非法：{exc}（本轮不问模型）")

    pool_prices = {str(r.code): snap_price(snapped.get(str(r.code))) for r in pool_rows}
    # 行情闸门（**在滤池之前**、在问模型之前）：池里一只价都取不到 ⇒ 模型看到的是
    # 空池，只能回报「全部持有」。那不是决策，是把「行情断了」写成「今天不建仓」——
    # 而 ``decision_round_tick.round_tick`` 按 ``result.ok`` 置 done 键，当天 10:05/11:05 两个补跑槽会全部
    # 「当日已出过 rebalance 决策」跳过：一次行情抖动吃掉当天全部建仓轮，状态键、日志、
    # CLI 退出码三面却全绿。交易时段内 fail-closed（abort 且不置 done，补跑槽会再来）；
    # 非交易时段（08:30/09:00 的盘前规划轮）保持降级：那种轮本来就不发腿，只出计划与
    # 守护规则，而盘前拿不到价是常态（下同 :data:`in_session` 分支的口径）。
    in_session = bool(deps.is_trading_time(now))
    n_no_price = sum(1 for r in pool_rows if pool_prices.get(str(r.code)) is None)
    if pool_rows and in_session and n_no_price == len(pool_rows):
        why = quotes_failure or (
            "行情客户端未配置（REMOTE_QUOTE_REDIS_HOST 为空）"
            if quote_client is None
            else "行情源未返回这些代码"
        )
        reason = (
            f"候选池 {len(pool_rows)} 只在交易时段内全部无可用现价（{why}）："
            "模型只能看到空池，本轮不做（补跑槽会再来）"
        )
        logger.error("[DecisionRound] %s %s", round_id, reason)
        return abort_result(
            day,
            slot,
            reason,
            quotes={"client": quote_client is not None, "rows": len(snapped)},
            pool={"file": pool_file, "rows": len(pool_rows)},
        )
    if 0 < n_no_price < len(pool_rows):
        # 部分无价：照跑，但要说出来——「池里 N 只没进模型视野」与「模型没选它们」
        # 在审计里长得一样，归因会指向模型。
        logger.warning(
            "[DecisionRound] %s 候选池 %d 只中有 %d 只无可用现价（不进模型视野）",
            round_id,
            len(pool_rows),
            n_no_price,
        )
        notes.append(
            f"候选池 {n_no_price}/{len(pool_rows)} 只无可用现价（不进模型视野）"
        )
    kept, kept_rows, dropped_rows = filter_affordable_pool(
        pool_rows=pool_rows,
        pool_prices=pool_prices,
        gate=gate,
        budget=per_stock_budget,
        round_id=round_id,
    )

    quotes, stale = quotes_from_snapshots(snapped, kept, now)
    # ⑦b/⑦c 分数填进行情行（「信号分」后缀）+ 两个补块：数据门控——未接线整段
    # 不渲染、取数失败如实降级并留痕（口径收在 io 助手，渲染在 decision_blocks）。
    quotes, blocks = await enrich_quotes_with_blocks(
        deps, round_id, agent, user, regime, snapped, quotes, holding_rows, now, notes,
        tier=effective_tier, include_score_leaders=policy.allow_out_of_pool,
    )

    context = build_round_context(
        blocks,
        agent=agent,
        holdings=holding_rows,
        pool=kept,
        direction=direction,
        now=now,
        quotes=quotes,
        quotes_stale_count=stale,
        # 成本/浮盈按**本 agent 的分账账本**渲染（账本无该票时回退桥口径并打 `*`）：
        # 共用账户里，桥的持仓成本是混合口径，拿它算「我这笔赚没赚」是错的基准。
        ledger_positions=ledger.positions,
        quota_total=quota_total,
        quota_used=quota_used,
        per_stock_pct=per_stock_pct,
        max_new_buys=max_new_buys,
        allow_out_of_pool=policy.allow_out_of_pool,
        profit_first=policy.profit_first,
        market_state_block=blocks.market_state_block,
        signal_meta_block=blocks.signal_meta_block,
    )
    # ⑧ LLM 调用（模型名与调用器同源，见 LLMBinding）：decide 同步阻塞 httpx（最长 ~120s），必须 to_thread
    prompt = render_prompt_for(context, slot.schema)
    attempt = await asyncio.to_thread(binding.decide, prompt, slot.schema)
    if not getattr(attempt, "ok", False):
        # 没出决策：**不写审计行、不置 done 键**（补跑槽还能再来一次），只留状态。
        reason = str(getattr(attempt, "error_text", lambda: "")() or "")
        note = f"LLM 未给出决策（{getattr(attempt, 'status', '')}）：{reason or '未报告原因'}"
        if getattr(attempt, "truncated", False):  # 顶格截断≠格式烂（reason=length）
            usage = dict(getattr(attempt, "usage", None) or {})
            got = usage.get("completion_tokens")
            detail = f"，completion_tokens={got}" if got is not None else ""
            note += f"（供应商输出顶格截断{detail}，非解析问题：调大该家 max_tokens）"
        return RoundResult(
            status=STATUS_LLM_FAILED,
            day=day,
            slot=slot,
            round_id=round_id,
            agent=agent,
            note=note,
            errors=(reason,) if reason else (),
            mode=mode,
            meta={"calls": int(getattr(attempt, "calls", 0) or 0)},
        )

    # ⑧b 对话落盘（arena「模型对话」写侧）：**在闸门之前**写，纪律见该函数
    record_round_dialogue(
        agent=agent, prompt=prompt, attempt=attempt, now=now, notes=notes
    )

    decisions = tuple(getattr(attempt, "decisions", ()) or ())
    stamps = build_pool_stamps(
        kept=kept_rows,
        dropped=dropped_rows,
        wanted=[str(getattr(d, "code", "") or "") for d in decisions],
    )

    # ⑧c 池外买入（2026-10-28「股票不限」）：名称由系统锚定 + 点名代码现刷行情。
    decision_buy_symbols: list[str] = []
    if policy.allow_out_of_pool:
        decision_buy_symbols, snapped, gate, more_notes = await enrich_decision_quotes(
            deps=deps, quote_client=quote_client, round_id=round_id,
            decisions=decisions, snapped=snapped, gate=gate,
        )
        notes.extend(more_notes)

    # ⑨ 执行段与守护段（同一份快照、同一把闸门、同一个 round_id）。``in_session``
    # 已在 ⑦ 的行情闸门处取（那处也要用；一轮内不变）。
    exec_holdings = holdings_from_rows(holding_rows)
    exec_symbols = sorted(
        set(exec_holdings) | {str(r.code) for r in kept} | set(decision_buy_symbols)
    )
    exec_quotes = quotes_for(exec_symbols, snapped, trade_date=day)

    inflight: frozenset[tuple[str, str]] | None = None
    submitter: Any = None
    #: 执行段缺 ``outcomes`` 属性时的留痕（见 try 块内的赋值点）；``try`` 正常走完才会
    #: 到达下面的返回，这里先绑定是为了「有没有取到」这件事在任何路径上都有定义。
    outcomes_gap = ""
    watch_result: Any = None
    if not in_session:
        # 非交易时段：计划照算（留痕），但一条腿都不发。
        inflight = frozenset()
        submitter = refusing_submitter("非交易时段：本轮只出决策与守护规则，不提交腿")
    try:
        async with deps.open_db() as db:
            outcome = await deps.run_exec(
                db=db,
                batch=attempt.batch,
                round_id=round_id,
                holdings=exec_holdings,
                quotes=exec_quotes,
                gate=gate,
                # 腿的**尺度口径**取两个额度里更紧的那个（``per_stock_budget`` 同式）：
                # 执行段按 ``quota × pct`` 算股数（``execution._Plan.buy``）。授予额度
                # 的剩余量是用户允许这条线动用的上限；``virtual_cash`` 是账上现钱。
                # 取更紧的那个之后，vcash 闸退回它真正的职责：``per_stock_pct``
                # 未配置（=不夹取）时的最后一道兜底。真实账户现金不参与这个数——
                # 它由执行段的 ``l1.available_cash`` 内部把关。
                quota=min(ledger.quota_remaining, virtual_cash),
                new_buys_round=0,  # 本轮已产生数：单轮一次规划＝0；别传档位上限（会全否）
                inflight=inflight,
                agent=agent,
                real=mode == "real",
                submitter=submitter,
                tenant_id=TENANT_ID,
                user_id=user,
            )
            # 执行段取数要在**写守护规则之前**：``aborted`` 且零提交的轮次不许动守护
            # 规则表——``write_watch_plan`` 是整组替换，用它去覆盖等于把 09:00 挂上的
            # 止损全清掉，还给一批**从未买入**的标的 arm 上规则（本条见 P2.8 评审 H3：
            # 原先守护段写在 aborted 判定之前，一次瞬时在读途账失败就会清空守护层）。
            aborted, summary = exec_summary(outcome)
            submitted = int(summary.get("submitted", 0) or 0)
            if aborted and not submitted:
                logger.warning(
                    "[DecisionRound] %s 执行段 aborted 且零提交：守护规则表本轮整组"
                    "保留（不覆盖既有止损，也不 arm 未买入的标的）",
                    round_id,
                )
                notes.append("执行段 aborted 且零提交：守护规则表整组保留")
            else:
                # 守护规则成本价只认分账账本（桥成本是共享账户混合口径；601000.SH 实测）
                watch_result = maybe_write_watch(
                    deps=deps,
                    slot=slot,
                    decisions=decisions,
                    agent=agent,
                    notes=notes,
                    # 分账账本：成本回填 + watch 只保护本 agent 现有持仓（执行器把它
                    # 落成卖出规则，未持仓标的挂了也不会成交，只会每天告警缺成本价）
                    ledger=ledger,
                )
            # 「取不到」与「真的是空」必须分开（同 ``summary`` 那条纪律）：没有
            # ``outcomes`` 属性时合并结果只剩守护段，审计行会显示「这一轮什么也没发生」
            # ——而腿可能已经真出去了。不改状态（此刻单已在下，报 error 会让补跑槽重来
            # 一轮），但把它作为一条 ``errors`` 带出去，状态键不留全绿。
            if not hasattr(outcome, "outcomes"):
                outcomes_gap = (
                    "执行段返回值缺 outcomes 属性：本轮逐决策结果无法回收，"
                    "审计行的结果列为空（真单是否出去请看 orders）"
                )
            merged = merge_outcomes(
                getattr(outcome, "outcomes", None),
                watch_result.outcomes() if watch_result is not None else None,
            )
            # ⑩b 持仓落盘（甲4）：arena「持仓/净值」快照（aborted 轮不写，见该函数）
            if not aborted:
                record_round_position(agent=agent, ledger=ledger, now=now, notes=notes)
            # 真单镜像就绪探测（P2-2，H2/M9）：与镜像链路同一实现（``RoundDeps`` 接线到
            # ``real_mirror_service.real_trading_ready``）。探测失败记 None——审计里
            # 「探不到」与「不就绪」是两件事；探测本身绝不阻断本轮落账。
            mirror_probe: tuple[bool, str] | None = None
            probe = getattr(deps, "mirror_ready", None)
            if probe is not None:
                try:
                    mirror_probe = probe()
                except Exception as exc:  # noqa: BLE001 探不到就如实记 None
                    logger.warning(
                        "[DecisionRound] %s 镜像就绪探测失败: %s", round_id, exc
                    )
            ctx_meta = context_meta(
                slot=slot,
                round_id=round_id,
                day=day,
                model=agent,
                mode=mode,
                attempt=attempt,
                account=account,
                ledger=ledger,
                pos_meta=holding_meta,
                pool_file=pool_file,
                kept=kept_rows,
                dropped=dropped_rows,
                quotes=quotes,
                stale=stale,
                quote_client=quote_client is not None,
                tier=tier,
                per_stock_pct=per_stock_pct,
                max_new_buys=max_new_buys,
                quota_total=quota_total,
                quota_used=quota_used,
                excluded=excluded,
                in_session=in_session,
                mirror=mirror_probe,
                notes=notes,
            )
            records = build_records(
                decisions,
                round_id=round_id,
                agent=agent,
                trade_date=day,
                decided_at=started,
                market="CN",
                tenant_id=TENANT_ID,
                user_id=user,
                held=exec_holdings,
                outcomes=merged,
                pool_ctx=stamps,
                context_meta=ctx_meta,
            )
            audit_rows = int(await deps.write_ledger(db, records) or 0)
    except Exception as exc:  # noqa: BLE001 执行/落账失败：状态如实记，done 键不置
        logger.error("[DecisionRound] %s 执行段失败: %s", round_id, exc, exc_info=True)
        return RoundResult(
            status=STATUS_ERROR,
            day=day,
            slot=slot,
            round_id=round_id,
            note=f"执行段失败：{type(exc).__name__}: {exc}",
            errors=(f"{type(exc).__name__}: {exc}",),
            agent=agent,
            mode=mode,
            decisions=len(decisions),
        )

    # 执行段的结果取数（``aborted``/``summary``）已在 try 块内完成——守护段的写与不写
    # 要由它决定（见那里的注释），所以这里不再重取。
    if aborted and not submitted:
        # 执行段的 ``aborted`` 语义是**一张单都没发**（``decision_executor.run_round``
        # 只在在途账读不到时置它）。此前它只进 ``note``，状态仍是 ``ok`` —— 后果不是
        # 「日志难看」：``decision_round_tick.round_tick`` 按 ``result.ok`` 写 done 键，于是当天 10:05/11:05
        # 两个补跑槽全部「当日已出过 rebalance 决策」跳过，**一次瞬时读失败吃掉当天
        # 全部建仓轮**，而状态键、日志、CLI 退出码三面全绿。core 的账户闸门走
        # ``abort_result`` → ``STATUS_ABORTED`` → 不置 done 键，这里对齐同一条口径。
        # 守护规则表本轮未动，也要写进 note：那张表是**外部状态**，审计表里看不出它。
        return RoundResult(
            status=STATUS_ABORTED,
            day=day,
            slot=slot,
            round_id=round_id,
            note=f"{aborted}（守护规则表本轮整组保留）",
            errors=(aborted, outcomes_gap) if outcomes_gap else (aborted,),
            agent=agent,
            mode=mode,
            decisions=len(decisions),
            legs=int(summary.get("legs", 0) or 0),
            audit_rows=audit_rows,
            watch_armed=len(getattr(watch_result, "armed", ()) or ()),
            # 中止轮的要点行同样要给（腿都标「未发出」）：告警旁一句「本来要做什么」，
            # 值班不必翻审计表就知道这轮缺的是什么。
            meta={"highlights": round_highlights(decisions, outcome)},
        )
    # 轮终注记（2026-10-10 审计 H3/M10）：「跑成了但零产出」必须在读侧与「一切正常」
    # 区分开——空池要能看出是**设计内的看空清空**还是供给链断了；交易时段内一条腿
    # 都没提交时，note 不再含糊地说「提交完成」。判据与告警（``decision_round_alerts``）
    # 共用同一对 meta 字段（``pool.rows`` / ``in_session``），那边据此决定推不推。
    direction_text = str(getattr(direction, "direction", "") or "")
    bearish_clear = len(pool_rows) == 0 and "看空" in direction_text
    if aborted:
        note = aborted
    elif bearish_clear:
        note = f"空池（看空清空）：方向「{direction_text}」→ 本轮不建仓"
    elif not in_session:
        note = "非交易时段：未提交腿"
    elif submitted > 0:
        note = "提交完成"
    else:
        note = "提交完成（0 腿）"
    return RoundResult(
        status=STATUS_OK,
        day=day,
        slot=slot,
        round_id=round_id,
        note=note,
        errors=(outcomes_gap,) if outcomes_gap else (),
        agent=agent,
        mode=mode,
        decisions=len(decisions),
        legs=int(summary.get("legs", 0) or 0),
        submitted=submitted,
        failed=int(summary.get("failed", 0) or 0),
        watch_armed=len(getattr(watch_result, "armed", ()) or ()),
        audit_rows=audit_rows,
        meta={
            "vetoes": int(summary.get("vetoes", 0) or 0),
            "noops": int(summary.get("noops", 0) or 0),
            "watches": int(summary.get("watches", 0) or 0),
            "duplicates": int(summary.get("duplicates", 0) or 0),
            # 本轮要点（▸ 段的数据源）：摘要在计数之外要说人话——方向/标的/股数/
            # 理由（2026-10-15 用户点名「通知了、不给建议吗」）。提炼纪律见
            # ``decision_round_notes.round_highlights``；空列表=没可说的事，不是取不到。
            "highlights": round_highlights(decisions, outcome),
            # 执行段自己的注记（如「虚拟现金未建模，未判」）——是说明不是错误，
            # 与 ``errors`` 分开：把 note 混进 errors 会让状态键红得没有意义。
            "plan_notes": list(summary.get("notes") or [])[:5],
            "pool": {
                "file": pool_file,
                # 原始行数（供给面）。**必须显式带出来**：告警侧靠「键在不在」区分
                # 「真的是 0 行」与「取不到」（同 ``exec_summary`` 纪律）。
                "rows": len(pool_rows),
                "shown": len(kept_rows),
                "dropped": len(dropped_rows),
                # 大盘方向文本（看空清空判据；"—"=缺失，绝不编一个方向出来）。
                "direction": direction_text,
                "total_score": getattr(direction, "total_score", None),
            },
            # 告警判据：非交易时段的零腿是**设计内**（盘前规划轮不发腿）。
            "in_session": bool(in_session),
            # 对外状态面（前端/告警）也只暴露授予额度口径：真实账户三数只留在
            # 审计行 ``context_meta.account`` 里，供运维复盘，不进任何模型可见面。
            "quota": {
                "total": quota_total,
                "used": quota_used,
                "remaining": ledger.quota_remaining,
                "cash": virtual_cash,
            },
        },
    )
