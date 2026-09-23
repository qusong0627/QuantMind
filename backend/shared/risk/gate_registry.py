"""闸门登记表 —— 每条风控规则的**依据 / 失效条件 / 复核日**（P1.6 影子代价账配套）。

三层分工（与 `registry.py` 的区别要点名）
----------------------------------------
* `backend/shared/risk/registry.py` —— **有哪些规则**（代码级：`RuleSpec` + `@rule`
  装饰器，判定函数的注册表）。改规则才会改它。
* 本模块 `gate_registry.py` —— 每条规则的**依据与失效条件**（给人读的元数据）。
  加规则时**先**在 `builtin_rules.py` 注册判定、**再**在此加条目；两边双向覆盖由
  `backend/tests/test_risk_gate_registry.py` 强制（缺一边即红）。
* `backend/scripts/risk_ghost_*.py` —— 规则被触发时**记录代价**与**读数**。

两族规则（同一张表，两套代码）
------------------------------
执行族的判定住在 `builtin_rules.py`（签名 ``fn(ctx, params)``，跑在下单前的
规则引擎里）；**决策族**的判定住在 `backend/shared/decision/gates.py`（纯函数，
入参由决策轮自己给），覆盖「LLM 说买」到「下单」之间那几步。后者**不能**并进
执行族：`RiskContext` 里没有 `day_chg`/候选池/名称这些字段，硬挂上去就是
「判定与单测都在、ctx 字段没人填 → 规则恒不触发」。双向覆盖按两族的**并集**查。

为什么要「失效条件」和「复核日」
--------------------------------
风控规则天然是**单向棘轮**：每条都有"当初为什么加"的理由，没有任何一条会因为
**代价大于收益**而自动下线；而"当初的理由"在几个月后没人记得请。于是规则只会
越来越多，每一条都在悄悄吃掉机会。故本条表的硬性要求是：**每条规则必须写明
"什么观测会证明它错了"（`falsify`）与"最迟何时复核"（`review_by`）**——
到期或影子样本够（先到者，见 `REVIEW_SAMPLE_MIN`）就复核一次，结论三选一：
保留（写明理由）/ 放宽（改参数）/ 删除。

`kind`：记录口径，不是规则的重要性分级
--------------------------------------
`kind` 决定这条规则被触发时**能不能记代价**（供 `ghost` 侧分账）：

* ``veto`` —— **放行即成交**。被拦的单本可以真的买到/卖掉，故"放行后是涨是跌"
  就是这条闸的成本/收益，逐条可测。
* ``structural`` —— **放行也不会成交**：交易所规则（整手、T+1）、监管红线
  （自成交、急停）、系统健康（时钟、契约）、纯监控（撤单率 WARN）。
  它们的代价**恒为 0**（放行=废单或违规），把它们的触发数当"代价"读会得出
  「整手校验该删」这种结论。处置口径也不同：**structural 规则的触发数居高不下，
  是上游该修修**（例如非整手=下单前没对齐手数），不是"这条闸该删"。
* ``unseen`` —— **候选在进模型视野之前就被剔除**（P2.1b 起用于
  `l1.unaffordable`）。它记的不是「被否决的决策」而是「假设模型会选它」的
  **弱反事实**：放行不等于成交（模型未必选它），故不进代价账；把它与 veto
  混记，会把「视野被资金规模截断」读成「规则的成本」。

``unknown``（历史回填原因不可考）属迁移层，等 P3 落地时在本表增补。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

#: 复核触发条件之二：影子样本达到该条数即可提前复核（与 `review_by` 先到者）。
#: 30 这个数与隔壁 `gate_registry.json` 的 `_review_note` 同口径，理由也一样——
#: 少于 30 条时"均值"本身没有意义，拿它下结论等于掷硬币。
REVIEW_SAMPLE_MIN = 30

#: 复核结论三选一（报告里按这个口径给建议，避免每个读报表的人自创标准）
REVIEW_ACTIONS: tuple[str, ...] = ("保留（写明理由）", "放宽（改参数）", "删除")

KIND_VETO = "veto"
KIND_STRUCTURAL = "structural"
KIND_UNSEEN = "unseen"

#: 记录口径全集（`kind` 只能取这里面的值；测试强制）
KINDS: tuple[str, ...] = (KIND_VETO, KIND_STRUCTURAL, KIND_UNSEEN)


@dataclass(frozen=True)
class GateSpec:
    """一条闸门的审阅元数据（纯数据，无 I/O）。"""

    rule_id: str
    title: str
    where: str
    kind: str
    evidence: str
    falsify: str
    review_by: str
    note: str = ""


def _spec(
    rule_id: str,
    title: str,
    where: str,
    kind: str,
    evidence: str,
    falsify: str,
    review_by: str,
    note: str = "",
) -> GateSpec:
    return GateSpec(
        rule_id=rule_id,
        title=title,
        where=where,
        kind=kind,
        evidence=evidence,
        falsify=falsify,
        review_by=review_by,
        note=note,
    )


#: 首轮复核日：影子期（2026-09-23 起）后 30 天。逐条写出而不是共用常量——
#: 复核日本来就允许逐条不同（样本积累速度不同），共用常量会让人以为"必须一起改"。
_ROUND1 = "2026-10-23"

#: 登记表（rule_id → GateSpec）。顺序即报告里的展示序：先按层，层内按经济含义分组。
_REGISTRY: tuple[GateSpec, ...] = (
    # ── L0 系统级（红线条，放行也没有意义）────────────────────────────
    _spec(
        "l0.kill_switch",
        "急停开关",
        "builtin_rules.l0_kill_switch",
        KIND_STRUCTURAL,
        "人工应急停机：置位后拒新单并触发全撤。它的价值不在省钱，在于「出事时能停手」。",
        "本规则**不可由代价证伪**——急停是人为红线。可证伪的是它的**可靠性**："
        "一次真实停机演练中若有新单漏过（`verdict != halt`）即视为失效，与影子账无关。",
        _ROUND1,
        "无样本（从未置位）是常态，不作为删除理由；复核时只确认「置位后确实全停」。",
    ),
    _spec(
        "l0.session",
        "申报时段校验",
        "builtin_rules.l0_session",
        KIND_STRUCTURAL,
        "非交易日/非申报时段的委托到交易所就是废单（或排到下一时段），"
        "拦下它不损失成交机会，只省一次废单往返。",
        "若影子账中出现**盘中被拒**的样本（`hm` 落在 windows 内却被拒），"
        "说明时段表配错，属配置缺陷而非规则该删。盘后入队（WARN）本就不拦单。",
        _ROUND1,
        "盘后入队的 queued_intent 走 WARN 不拦单，故本规则**不应有** veto 类代价样本。",
    ),
    _spec(
        "l0.clock_drift",
        "时钟漂移校验",
        "builtin_rules.l0_clock_drift",
        KIND_STRUCTURAL,
        "本机时钟与交易所偏差过大时，时段/行情时效判定全部失真——先拒绝成交，再修时钟。",
        "若被拒样本的 `skew_ms` 事后证明为**测量侧**误差（NTP 源自身漂移），"
        "则应改测量而非删规则；`skew_ms` 缺失时不拦（未测量≠漂移）。",
        _ROUND1,
        "未测量时不判（`clock_skew_ms=None` → 放行），故「零触发」既可能是"
        "「时钟一直准」也可能是「一直没测」——复核时先看测量是否在跑。",
    ),
    # ── L1 账户级 ────────────────────────────────────────────────────
    _spec(
        "l1.available_cash",
        "买入可用资金校验",
        "builtin_rules.l1_available_cash",
        KIND_VETO,
        "买入金额超可用资金。含 fail-closed 分支：快照/金额不可得时拒（"
        "缺数据放行会在最需要时把闸关掉）。",
        "被拦买单在其后 T+1/T+5 的均超额**显著为正且大于成本**"
        "（`REVIEW_SAMPLE_MIN` 条以上）→ 说明门槛过紧，应放宽"
        "（例如改用「可用资金 + T+1 预计到账」）。",
        _ROUND1,
        "fail-closed 分支与真实越限分支不计代价差异：两者都拦掉了本可成交的单，"
        "成本口径相同；区分二者是「规则该不该留」之外的问题（见 P1.6 后续）。",
    ),
    _spec(
        "l1.t1_sellable",
        "T+1 可卖量校验",
        "builtin_rules.l1_t1_sellable",
        KIND_STRUCTURAL,
        "卖出量超可卖持仓：A 股 T+1，当日买入当日不可卖。放行也是柜台废单。",
        "触发数居高不下 = **上游持仓账不平**（例如卖出没归因回去，见台账漂移 "
        "P0.4），该修台账而不是删规则。",
        _ROUND1,
        "`sellable_volume` 不可得时 fail-closed 拒卖——账户会被这条卡死，故"
        "**不可得**的拒单要单独看：那是快照故障，与「真的卖了不可卖的量」是两回事。",
    ),
    _spec(
        "l1.position_cap",
        "单票市值上限",
        "builtin_rules.l1_position_cap",
        KIND_VETO,
        "单票占比（持仓+本单）超上限，防单一标的风险集中。",
        "被拦标的其后 T+5 均超额为正且样本 ≥ `REVIEW_SAMPLE_MIN` → 上限过紧；"
        "若中位数为正而均值不为正，说明是少数大赢家被误拦，应看分布而非只看均值。",
        _ROUND1,
    ),
    _spec(
        "l1.per_order_pct",
        "单笔买入金额上限",
        "builtin_rules.l1_per_order_pct",
        KIND_VETO,
        "防守档下单笔体量上限（分母=账户总资产）。与 position_cap 的分工："
        "后者管累计占比，本规则管「持仓 3% 的票一笔打进 12%」这种单笔过重。",
        "默认 0.15 与 position_cap 同值即「未接档位时不额外收紧」——若影子账出现"
        "**独立于 position_cap** 的拒单（同单未被 position_cap 拦而本规则拦），"
        "说明档位层正在压它，其代价必须单独记账（这正是它存在的理由）。",
        _ROUND1,
    ),
    _spec(
        "l1.new_buys_per_day",
        "当日新开仓数上限",
        "builtin_rules.l1_new_buys_per_day",
        KIND_VETO,
        "当天往几个**新**标的开仓的上限（加仓不占额度），防「每个都小仓位」的"
        "无预算分散下注。",
        "被拦标的其后 T+5 均超额为正 → 额度太紧；恒为负 → 额度在拦住差标的，"
        "可考虑放宽。注意按**标的**去重，同一标的重复买入不占额度。",
        _ROUND1,
    ),
    _spec(
        "l1.leverage_cap",
        "总杠杆上限",
        "builtin_rules.l1_leverage_cap",
        KIND_VETO,
        "全账户总敞口唯一总额闸（既有规则全是逐维度上限，十只各 15% 就是 150%）。"
        "只拦买入；卖出降杠杆恒放行。",
        "被拦买单其后 T+1/T+5 均超额持续为正（拦住的是机会而非损失）→ 放宽 "
        "`max_leverage`；或出现「本单金额」无法计算导致的 fail-closed 拒单"
        "高频发生 → 修上下文而不是动阈值。",
        _ROUND1,
        "隔壁主判据其实是**分账**杠杆（每个 agent 一条线，virtual_cash 分母）；"
        "本闸是全账户口径，等 P2.7 显式建模虚拟子账户后才谈得上分账杠杆。",
    ),
    _spec(
        "l1.industry_cap",
        "行业集中度上限",
        "builtin_rules.l1_industry_cap",
        KIND_VETO,
        "行业占比（行业已知时）超上限，防行业级集中。行业归属不可得时只 WARN"
        "（数据可得性问题可见化，不阻断交易）。",
        "被拦样本按行业分组后其后 T+5 均超额为正 → 上限过紧；若为负，说明"
        "行业闸确实在躲开弱行业。**注意分母**：行业占比来自上游，占比本身错了"
        "则本规则的代价不可解释。",
        _ROUND1,
        "行业占比未知只 WARN 不拒单，故本规则**不应有**因「占比未知」产生的代价样本。",
    ),
    _spec(
        "l1.daily_loss_limit",
        "日内亏损限额",
        "builtin_rules.l1_daily_loss_limit",
        KIND_VETO,
        "当日亏损达阈值后停止**开仓**（卖出照常），防连续亏损日越亏越加。"
        "与 l0.kill_switch 的区别：这是当日、自动、只禁开仓。",
        "被拦当日之后 T+5 的均超额 ≥ 0（拦掉的是机会）→ 应改为**降档**"
        "（减半额度）而不是全禁；均超额显著为负 → 全禁是对的。",
        _ROUND1,
        "`daily_pnl_pct` 不可得时**不判**（fail-open：没有依据就不拦）——"
        "这条已在 P1.9 接线，取数口径与账户页 today_pnl 同源。",
    ),
    # ── L3 订单级 ────────────────────────────────────────────────────
    _spec(
        "l3.max_order_value",
        "单笔金额上限",
        "builtin_rules.l3_max_order_value",
        KIND_VETO,
        "单笔金额绝对上限（元）：防「价格看错 / 手数多打一个零」类事故的最后一道"
        "绝对闸。与占比类规则不同，它不随账户规模变化。",
        "被拦单其后 T+5 均超额为正 → 上限过紧；恒为负 → 它在拦住误单。"
        "若被拦单的 `amount` 与正常单分布**同一个数量级**，说明上限本身设错。",
        _ROUND1,
    ),
    _spec(
        "l3.price_deviation",
        "价格偏离闸门",
        "builtin_rules.l3_price_deviation",
        KIND_VETO,
        "限价与最新价偏离超限即拒（强平单只保 sanity 上界 20%）。防「限价写错」"
        "与「追高/杀跌」。",
        "被拦单其后 T+1 的**可实现价格**证明当时报价是对的（例如涨停板买单被拦、"
        "次日更高）→ 阈值过紧；强平单被 sanity 上界拦下则是**功能问题**"
        "（强平必须能出去），不属阈值调整。",
        _ROUND1,
    ),
    _spec(
        "l3.stale_quote",
        "陈旧价拒单",
        "builtin_rules.l3_stale_quote",
        KIND_VETO,
        "行情时间戳早于阈值（默认 5s）即拒——用陈旧价算出来的金额/占比全部失真。"
        "不可得也拒（fail-closed）。",
        "若被拦样本的 `price_source` 集中在**同一类源**（如某路行情长期滞后），"
        "该修数据源而不是放宽阈值；盘后入队走 WARN 不拦单。",
        _ROUND1,
        "本规则的拒单常与「本单金额不可得」连锁——代价记账时必须与"
        "l1.available_cash 的同类拒单**去重**（同一单被多条规则拦只记一次成本）。",
    ),
    _spec(
        "l3.order_frequency",
        "下单频率上限",
        "builtin_rules.l3_order_frequency",
        KIND_VETO,
        "每分钟下单数上限。既是防程序失控，也是**法规申报义务**的近邻"
        "（高频阈值 300 笔/秒、20000 笔/日见 "
        "`shared/programmatic_trading_disclosure.py`）。",
        "撞线时**只告警不拦单**（`l3.order_frequency` 撞线口径见 "
        "CLAUDE.md）；若影子账显示被拦单集中在高频时段且其后超额为正，"
        "说明是策略节奏与阈值不匹配，应调 `max_per_minute` 而不是删规则。",
        _ROUND1,
    ),
    _spec(
        "l3.cancel_ratio",
        "撤单率监控",
        "builtin_rules.l3_cancel_ratio",
        KIND_STRUCTURAL,
        "撤单率超监管参考线时**只 WARN**（供限频决策），从不拒单——故它没有成交代价。",
        "无代价可证伪。它的观测价值在**趋势**：撤单率长期贴线说明策略在"
        "频繁改价，属策略问题。",
        _ROUND1,
        "本规则恒不拒单（WARN 型），影子账里**不应有**它的 veto 行；"
        "出现即说明判定被改坏。",
    ),
    _spec(
        "l3.self_trade",
        "自成交防范",
        "builtin_rules.l3_self_trade",
        KIND_STRUCTURAL,
        "同标的窗口内存在反向委托即拒：自成交在 A 股属**违规**（可被认定为"
        "异常交易），放行不是收益而是风险。",
        "触发数居高不下 = 上游**同标的双向下**（例如信号抖动），该修信号"
        "而不是删规则。检测窗口过短导致的漏报才是本规则的风险面。",
        _ROUND1,
        "窗口由 `recent_symbol_sides` 提供：**空窗口 = 不判**，故零触发不代表"
        "「没有自成交风险」，只代表上游没喂窗口。复核时先确认窗口在喂。",
    ),
    _spec(
        "l3.lot_size",
        "整手校验",
        "builtin_rules.l3_lot_size",
        KIND_STRUCTURAL,
        "买入必须整手（主板 100 / 科创 200）；卖出允许零股清仓。"
        "非整手委托到交易所就是废单。",
        "**触发数居高不下 = 下单前没对齐手数**，属上游缺陷（见 P1.3 的手数对齐"
        "口径），不是本规则该删。若出现「对齐后仍被拦」的样本，才是校验本身有错。",
        _ROUND1,
        "本规则是**最容易**被误读成「该删的闸」的一条：影子账里它的触发数会很高，"
        "但代价恒为 0（放行=废单）。",
    ),
    _spec(
        "l3.duplicate_fingerprint",
        "重复单防范",
        "builtin_rules.l3_duplicate_fingerprint",
        KIND_STRUCTURAL,
        "窗口内存在同参数委托即拒：重复单放行会**多买一份**，那不是收益而是错误"
        "（下单路径重试/双触发时的兜底）。",
        "若出现「参数确实不同却被判重复」的样本，说明指纹算法过粗"
        "（例如把合法的分批单当重复）——那才是要修的地方。",
        _ROUND1,
        "指纹为空时**不判**（`fp` 空串直接放行），故零触发可能是「没喂指纹」；"
        "复核时查下游何时写入 `fingerprint`，而不是先怀疑窗口设置。",
    ),
    # ── L6 数据/模型级 ───────────────────────────────────────────────
    _spec(
        "l6.book_invalid",
        "盘口异常拒单",
        "builtin_rules.l6_book_invalid",
        KIND_STRUCTURAL,
        "盘口倒挂/空盘口即拒：这种行情下的成交价不可信，放行等于蒙眼下单。",
        "若被拒样本的盘口事后证明是**采集侧**问题（某路行情源常态倒挂），"
        "修数据源而不是删规则。",
        _ROUND1,
        "盘口字段缺失（`book_crossed`/`book_empty` 均为 None）时**不判**——"
        "「没盘口数据」与「盘口坏了」必须分开，前者是数据可得性问题。",
    ),
    _spec(
        "l6.contract_mismatch",
        "模型/特征契约不符",
        "builtin_rules.l6_contract_mismatch",
        KIND_STRUCTURAL,
        "模型与特征契约不符（由上游置位）即拒：契约不符意味着这个信号本身"
        "不可信，放行是拿错模型下注。",
        "触发即**上游**契约漂移（`contract_ok=False` 的置位点），"
        "处置是修契约不是放宽规则。",
        _ROUND1,
        "`contract_ok` 默认 True（未置位即视为相符），故本规则的漏报面在**置位点**"
        "而不在本规则——复核时查的是「谁在什么时候把它置成 False」。",
    ),
    # ══ 决策层：买入闸（P2.1b）═══════════════════════════════════════
    # 与上面执行族的区别：这一族判的是**「LLM 说买」到「下单」之间**的那几步，
    # 入参由决策轮自己给（行情/候选池/额度都在它手上），不读任何配置存储。
    # 为什么不并进执行族：`RiskContext` 里没有 day_chg/候选池/名称这些字段，
    # 硬挂上去就是「判定与单测都在、ctx 字段没人填 → 规则恒不触发」。
    # 实现见 `backend/shared/decision/gates.py`。
    _spec(
        "l1.unaffordable",
        "候选买不起一手（不进模型视野）",
        "gates.filter_pool",
        KIND_UNSEEN,
        "按「额度 ≥ 一手最小股数（科创 200、其余 100）× 现价」筛候选：买不起的票"
        "**不进模型视野**。既有事故口径：把买不起的票留在池里，模型选它之后被"
        "执行段拒掉，账面上成了「模型老给不可执行的决策」——真相是额度买不起。",
        "若被剔标的其后 T+5 均超额**显著为正**且样本 ≥ `REVIEW_SAMPLE_MIN`，"
        "说明池子被资金规模截断得太狠——那时要查的是额度分配（单票预算相对"
        "股价太小），不是删这条判据。",
        _ROUND1,
        "kind=unseen：触发数**不进**代价账（`priced_kinds()` 只含 veto）。"
        "触发比例长期偏高 = 资金分配问题，属上游。",
    ),
    _spec(
        "l1.vcash",
        "子账户虚拟现金校验",
        "gates.check_buy",
        KIND_VETO,
        "多模型竞争（P2.7）下的**分账**口径：每个决策 agent 一条虚拟资金线，"
        "本单金额超该线即拒。与 `l1.available_cash`（全账户真钱）不是一回事——"
        "真钱闸管「买不买得起」，本闸管「这条线还能不能下注」。",
        "若被拦决策其后 T+1/T+5 均超额显著为正（样本 ≥ `REVIEW_SAMPLE_MIN`）→ "
        "分账额度配得过紧，应放宽分配比例；若长期只留 note（未建模），说明那是"
        "**还没通电**而不是「这条闸没用」，不作为删除理由。",
        _ROUND1,
        "`virtual_cash=None`（未建模）时**不判**并留 note；真钱闸在执行段"
        "（`l1.available_cash`，fail-closed），本层不复制。",
    ),
    _spec(
        "l2.pool_not_member",
        "新开仓须在候选池内",
        "gates.check_buy",
        KIND_VETO,
        "新开仓只能在**本轮候选池**内选（池子由信号/因子链路产出）。LLM 凭空点"
        "一只池外票 = 绕开整条研究链路下单。**加仓不受限**：池子约束的是"
        "「新开什么」，不是「能不能继续持有」。",
        "若被拦标的其后 T+5 均超额为正 → 说明模型确实看到了池外机会（池子太窄），"
        "该修的是候选池构造；均超额为负 → 闸在拦住跑偏。两者都不指向删闸。",
        _ROUND1,
    ),
    _spec(
        "l2.pool_row_invalid",
        "候选行不可用（缺码/缺价）",
        "gates.filter_pool",
        KIND_STRUCTURAL,
        "候选行不是对象、缺 `code`、或在有额度时缺现价——这样的行连「买不买得起」"
        "都判不了，留着只会白占一次推理。",
        "触发数持续 > 0 = 池子**构建侧**在产残行（上游缺陷），该修构造侧；"
        "若残行比例升高而候选池同时变空，先查数据源，而不是放宽本判据。",
        _ROUND1,
        "与 `l4.symbol_boundary` 分开记：那是「这只票被禁买」，这是「行本身"
        "没构造好」。混在一起会让人去查一只票，而问题在数据管道。",
    ),
    _spec(
        "l2.round_new_buys",
        "单轮新开仓数上限",
        "gates.check_buy",
        KIND_VETO,
        "单**轮**决策允许新开仓的标的数上限，防一次推理把额度铺满 N 只。"
        "与 `l1.new_buys_per_day` 分工：后者是当日累计（跨轮），本闸只管本轮。",
        "若被拦标的其后 T+5 均超额为正 → 单轮上限太紧（好机会被摊薄）。注意它"
        "与 `l1.new_buys_per_day` 天然连锁（同一批标的可能被两条闸先后拦），"
        "影子账**按单去重**，否则同一笔机会会被记两次代价。",
        _ROUND1,
        "未配置（0）时不判并留 note：配置缺失要可见，不能默默变成「不限量」。",
    ),
    _spec(
        "l2.pct_invalid",
        "买入比例读不出（停手留痕）",
        "gates.check_buy",
        KIND_STRUCTURAL,
        '`pct` 给了值但解析不出（`"0.3股"` / `"三成"` / 布尔 / `NaN`）→ 拒买'
        "并留原文。这类值**不能**按「没给」处理：卖出侧「没给」走清仓，方向正好反。",
        "触发即模型输出格式跑偏（提示词/schema 问题），处置是修提示词；若脏值"
        "集中在某一个模型，该禁的是那个模型的输出，而不是放宽本判据。",
        _ROUND1,
        "structural 的理由是**反事实无法统一定价**：不知道模型想买多少，就算不出"
        "「放行会赚多少」。记成 veto 会让影子账拿一个编出来的仓位去算收益。",
    ),
    _spec(
        "l2.pct_zero",
        "买入未给可执行比例",
        "gates.check_buy",
        KIND_STRUCTURAL,
        "模型没给比例、给了 0 或负值 → 买入不可执行。买入是「用多少额度」的声明："
        "不说是没说，**不是**用满额度（那是凭空放大）；与卖出侧「没给即清仓」的"
        "方向刻意相反。",
        "触发比例长期偏高 = 提示词没让模型给出幅度（schema 里 pct 是必填却没人填）"
        "→ 修提示词/契约。",
        _ROUND1,
        "同 `l2.pct_invalid`：没有幅度就没有可定价的反事实。触发数高是**症状**，"
        "病灶在提示词。",
    ),
    _spec(
        "l3.below_min_lot",
        "额度买不起一手（执行兜底）",
        "gates.check_buy",
        KIND_STRUCTURAL,
        "可用额度 < 最小申报量 × 现价（科创 200 股、其余 100 股）→ 拒。放行也是"
        "废单（非最小申报量被柜台拒），故代价恒为 0。",
        "触发数居高不下 = 预算与股价不匹配（上游该按股价分配额度），不是删闸。",
        _ROUND1,
        "与 `l1.unaffordable` 同判据但**位置不同**：那个在进模型视野前剔除"
        "（unseen，由 `filter_pool` 出），这个是决策已产出后的兜底——池子构建时"
        "的额度与下单时的额度可能不是同一个数。",
    ),
    _spec(
        "l4.symbol_boundary",
        "标的边界（黑名单/ST/退市）",
        "gates.check_symbol_boundary",
        KIND_VETO,
        "买入黑名单（按 6 位号精确匹配）+ ST/退市（按名称，见 "
        "`shared/symbol_policy.py`）。名单是人工判决，名称是推断，两者互补："
        "名单不依赖名称，名称判据覆盖名单没来得及收进去的新 ST。",
        "若被拦标的其后 T+5 均超额**持续为正**且集中在黑名单 → 名单过期了"
        "（人工表会腐烂），复核名单本身；若集中在 ST/退市类 → `symbol_policy` "
        "的判据过宽（例如把带「退」字的正常简称误判），修判据而不是删闸。",
        _ROUND1,
        "名称取不到时**放行**（fail-open）：不能因为名称表拉不到就停掉当天所有"
        "买入。故零触发也可能是「名称没拉到」——复核时先确认名称在喂。",
    ),
    _spec(
        "l4.limit_up",
        "涨停板不追高",
        "gates.check_limit_reach",
        KIND_VETO,
        "当日涨幅 ≥ 涨跌停阈值（比例，含等号）即拒买：涨停板上的买入价就是当日"
        "最高价，次日能否高开全靠运气。阈值由调用点**注入**（唯一事实源 "
        "`local_market_data.limit_threshold`，按板别/ST/制度日期解析）。",
        "若被拦标的其后 T+1/T+5 均超额显著为正（封板后继续涨）→ 拦掉的是连板"
        "机会，应改为「只拦已开板的」或按封单量放宽。这是本层最该被反复检验的"
        "一条：它拦的正是最热的那批票。",
        _ROUND1,
        "阈值单位是**比例**（0.1 = 一成）；传百分点（10.0）直接 ValueError——"
        "静默按错单位比较会让本闸一条都不拦。",
    ),
    _spec(
        "l4.limit_down",
        "跌停板不接刀",
        "gates.check_limit_reach",
        KIND_VETO,
        "当日跌幅 ≥ 阈值即拒买：跌停板上买到的「便宜」通常是重大利空或流动性"
        "枯竭的定价，不是折价。阈值同上由调用点注入。",
        "若被拦标的其后 T+5 均超额为正（跌停次日常有反抽）→ 说明该场景下接刀"
        "有利，应放宽或改为按基本面/事件分层判断，而不是一律拦。",
        _ROUND1,
    ),
    _spec(
        "l4.halted",
        "停牌不挂单",
        "gates.check_halted",
        KIND_STRUCTURAL,
        "标的停牌即拒买：放行也是废单（交易所不接），代价恒为 0。",
        "触发数居高不下 = 停牌状态源**滞后或缺失**（上游该修），不是删闸。"
        "`halted=None` 时不判，故零触发也可能是「没喂状态」——复核时先确认状态"
        "源在喂。",
        _ROUND1,
        "停牌状态未知（`None`）**不判**（不知道 ≠ 没停牌）：把「没查到」当"
        "「没停牌」是用一次查询失败换一笔必然废掉的买单。",
    ),
    # ── 决策层：执行段（`execution.py`，P2.3b 落地）──────────────────
    # 上一条是「买入闸」（LLM 说买 → 该不该买）；这一段是「从决策到一张单」的
    # 最后几步，买卖两侧都判，且**判的是能不能下出去**、不是该不该。
    _spec(
        "l2.sell_not_held",
        "卖出无对应持仓",
        "execution.sell_not_held",
        KIND_STRUCTURAL,
        "决策让卖的标的在本 agent 名下没有持仓（或压根没给代码）：A 股没有卖空，"
        "放行也是废单；多模型分账下这条同时是**跨 agent 卖仓**的防线。",
        "触发数居高不下 = 持仓快照与决策所见的那本账不是同一本（分账口径没对齐），"
        "该修取数侧；若出现「确实持有却被判非持仓」的样本，才是判据形状有错"
        "（例如代码归一没覆盖某个市场）。",
        _ROUND1,
        "**与 `l4.symbol_boundary` 分列**：那条判「这只票该不该碰」（改的是名单），"
        "这条判「这本账里有没有它」（查的是账）。原因与处置都不同。",
    ),
    _spec(
        "l3.no_quote",
        "无有效参考价",
        "execution.no_quote",
        KIND_STRUCTURAL,
        "现价缺失/为 0/非有限：算不出股数，也报不出限价。放行也下不出一张合法的单。",
        "触发集中在一只票 = 该票行情源断了（修数据源）；集中在某段时间 = 快照整体"
        "陈旧（查新鲜度分级）。与 `l3.stale_quote` 不是一回事：那条是**有价但旧**，"
        "这条压根没有价。",
        _ROUND1,
        "**无价 ⇒ 这条决策作废，不是延后**：本轮不发明价格、也不挂市价单兜底"
        "（`_limit` 返回 `None` 时绝不臆造一个价）。行情恢复后**下一轮**模型会重新"
        "决策——把这一轮没价当作「继续执行」是用一次取数失败换一笔无价委托。",
    ),
    _spec(
        "l3.inflight_dup",
        "同向在途重复",
        "execution.inflight_dup",
        KIND_STRUCTURAL,
        "同标的同方向已有**未确认**的委托（或本轮已产出同向腿）：LLM 决策耗时以分钟"
        "计，期间哨兵可能刚卖出/买入同一代码，模型据此再下一次就是第二笔真委托。"
        "放行的结果是多买一份/多卖一份——那不是收益而是错误。",
        "同 `l3.duplicate_fingerprint`：若出现「在途其实早已成交/已撤却仍被拦」的"
        "样本，说明在途账**没回执**（对账滞后），该修回执链路而不是删本条。",
        _ROUND1,
        "与 `l3.duplicate_fingerprint` 是**同一意图的两个机制**：那条按指纹窗口在规则"
        "引擎里判（任何下单路径都过），本条按实际在途委托在决策执行段判（只有 LLM "
        "链路有跨轮重复的问题）。合并会让某一条路径拿到不适用的判据。",
    ),
    _spec(
        "l4.sell_limit_down",
        "跌停不卖（执行段）",
        "execution.at_limit_down",
        KIND_VETO,
        "当日跌幅 ≥ 阈值即拒卖：跌停价的成交是**当日最差价**，而 LLM 的卖出是调仓"
        "意图（不是止损）——下一轮再卖通常更好。与买入侧 `l4.limit_down` 同谓词、"
        "**不同经济含义**（那边是别接飞刀），故分列。",
        "被拦标的其后 T+5 均超额为负（继续跌）→ 这一档确实该等；若为正且样本 ≥ "
        "`REVIEW_SAMPLE_MIN`，说明跌停后的反抽足够多，应放宽为「只拦封死跌停」。",
        _ROUND1,
        "**只管 LLM 的调仓腿**：止损（`sltp_executor`）在同一工况下**必须照卖**"
        "（报跌停价是它刻意选的口径，见 `lot_rules.aggressive_sell_price`）——把这条"
        "闸挂到止损路径上会让止损在跌停日集体失效。",
    ),
)

#: rule_id → GateSpec（对外只读视图）
_BY_ID: dict[str, GateSpec] = {s.rule_id: s for s in _REGISTRY}


def all_gates() -> tuple[GateSpec, ...]:
    """登记表全量（顺序即报告展示序）。"""
    return _REGISTRY


def gate_spec(rule_id: str) -> GateSpec | None:
    """按 id 取条目；未登记返回 None（**不猜**——未登记必须可见，见报告侧）。"""
    return _BY_ID.get(str(rule_id or "").strip())


def kind_of(rule_id: str) -> str:
    """规则 → 记录口径。未登记按 ``veto`` 记（宁可按最强口径读，不静默丢类别）。"""
    spec = gate_spec(rule_id)
    return spec.kind if spec is not None else KIND_VETO


def priced_kinds() -> tuple[str, ...]:
    """**可计入代价**的 kind（报告侧的分账依据，单源）。"""
    return (KIND_VETO,)


def review_overdue(
    as_of: date | str, gates: Iterable[GateSpec] | None = None
) -> list[GateSpec]:
    """复核日已到/已过（``review_by <= as_of``）的条目，按日期升序。

    ``as_of`` 由调用方注入（报告用当天；测试用固定日）——**不在本模块读时钟**，
    否则测试会随日历腐烂。
    """
    if isinstance(as_of, str):
        as_of = date.fromisoformat(as_of)
    due = [
        g
        for g in (gates if gates is not None else _REGISTRY)
        if date.fromisoformat(g.review_by) <= as_of
    ]
    return sorted(due, key=lambda g: (g.review_by, g.rule_id))
