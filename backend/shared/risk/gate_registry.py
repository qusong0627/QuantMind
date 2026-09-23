"""闸门登记表 —— 每条风控规则的**依据 / 失效条件 / 复核日**（P1.6 影子代价账配套）。

三层分工（与 `registry.py` 的区别要点名）
----------------------------------------
* `backend/shared/risk/registry.py` —— **有哪些规则**（代码级：`RuleSpec` + `@rule`
  装饰器，判定函数的注册表）。改规则才会改它。
* 本模块 `gate_registry.py` —— 每条规则的**依据与失效条件**（给人读的元数据）。
  加规则时**先**在 `builtin_rules.py` 注册判定、**再**在此加条目；两边双向覆盖由
  `backend/tests/test_risk_gate_registry.py` 强制（缺一边即红）。
* `backend/scripts/risk_ghost_*.py` —— 规则被触发时**记录代价**与**读数**。

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

`unseen` / `unknown` 两种 kind（候选进模型视野前被剔除 / 历史回填原因不可考）
属决策层与迁移层，等 P2/P3 落地时在本表增补；本版只覆盖执行面。
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

#: 记录口径全集（`kind` 只能取这里面的值；测试强制）
KINDS: tuple[str, ...] = (KIND_VETO, KIND_STRUCTURAL)


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
