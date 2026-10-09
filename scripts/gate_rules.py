#!/usr/bin/env python3
"""闸门规则的**稳定标识词表**（纯常量，无 I/O；2026-09-19 影子代价账配套）。

为什么要有这一层：否决原因此前只以中文句子存在（`BuyDecision.reason`、
各类 print 文案）。拿句子当键有三个必然结局——改一个字就换一条规则、
同名规则在两个文件里写成两种说法、报告只能靠正则猜。本模块把"规则是谁"
固定成机器可读的 id；中文只留在 `reason` 里给人看。

分工（三层，各司其职）：
  * `gate_rules.py`（本模块）——**有哪些规则**（词表，代码 import 的出处）
  * `configs/gate_registry.json`  ——每条规则的**依据/失效条件/复核日**（给人读，
    可改不需发版；测试强制两边双向覆盖，见 tests/test_ghost_ledger.py）
  * `scripts/ghost_ledger.py`      ——规则被触发时**记录事件**（唯一写入层）

命名口径 `<域>.<规则>`：域 = 这条规则在哪里生效（halt/symbol/pct/pool/cap/
cash/lev/inflight/limit/budget）。新增规则时**先**在这里加常量、**再**在登记表
加条目、最后才允许在调用点写死字符串——顺序反了测试会红。
"""
from __future__ import annotations

# ---- 买入放行闸（buy_gate.check_buy）----
HALT_DAILY = "halt.daily"                  # 当日熔断：亏到阈值，当天禁买（卖出照常）
SYMBOL_BOUNDARY = "symbol.boundary"        # 标的边界：ST/退市/黑名单/解禁/负面清单
PCT_INVALID = "pct.invalid"                # pct 非数字（模型吐脏值）
PCT_ZERO = "pct.zero"                      # pct=0 或未表达比例：意图不可执行
POOL_NOT_MEMBER = "pool.not_member"        # 非持仓且不在候选池（不允许凭空开仓）
CAP_ROUND_NEW_BUYS = "cap.round_new_buys"  # 本轮新开仓数上限
CAP_DAILY_NEW_BUYS = "cap.daily_new_buys"  # 当日累计新开仓数上限

# ---- 下单前置闸（执行段，两条路径各自实现）----
CAP_POSITION = "cap.position"              # 单票集中度：同票敞口 > MAX_POS_PCT×权益
CASH_INSUFFICIENT = "cash.insufficient"    # 账户可用现金不足
CASH_VCASH = "cash.vcash"                  # 分账虚拟现金不足（子账户不透支）
LEV_OVER = "lev.over"                      # 加仓后杠杆 > LEVERAGE_MAX×权益
BUDGET_BELOW_MIN_LOT = "budget.below_min_lot"  # 单票预算 < 最小一手（模型看见也想要，买不起）

# ---- 决策段去重/不可成交 ----
INFLIGHT_DUP_BUY = "inflight.dup_buy"      # 同标的已有在途买单未确认（防重复建仓）
LIMIT_UP = "limit.up"                      # 涨停不追
LIMIT_DOWN = "limit.down"                  # 跌停不接（买入侧）
SYMBOL_HALTED = "symbol.halted"            # 停牌/无成交（买不到）

# ---- 数据质量问题（不是风控选择，但同样在吃掉机会）----
DATA_NO_QUOTE = "data.no_quote"            # K 线不足 / 价格解析失败 / 无有效价格
DATA_DIRTY_QUOTE = "data.dirty_quote"      # K 线价与实时价偏差>40%（脏数据防超量）

# ---- 执行失败（桥/柜台）----
EXEC_SUBMIT_FAILED = "exec.submit_failed"  # 下单被桥或柜台拒绝/断链（真金白银的机会损失）

# ---- 候选池视野（kind=unseen：模型根本没看见，不是"被否决的决策"）----
BUDGET_UNAFFORDABLE = "budget.unaffordable"  # 按资金整行剔除（高分票常因贵先出局）

# ---- 历史回填（kind=unknown：原因已不可考，先只做"落地率"统计）----
UNKNOWN_NO_EXEC = "unknown.no_execution"   # 历史意图在成交流水里找不到任何下发记录

#: 词表全量（登记表覆盖性检查、报告分组都用它；顺序即报告里的展示序）
ALL: tuple[str, ...] = (
    HALT_DAILY, SYMBOL_BOUNDARY, SYMBOL_HALTED, PCT_INVALID, PCT_ZERO,
    POOL_NOT_MEMBER,
    CAP_ROUND_NEW_BUYS, CAP_DAILY_NEW_BUYS, CAP_POSITION,
    CASH_INSUFFICIENT, CASH_VCASH, LEV_OVER, BUDGET_BELOW_MIN_LOT,
    INFLIGHT_DUP_BUY, LIMIT_UP, LIMIT_DOWN,
    DATA_NO_QUOTE, DATA_DIRTY_QUOTE, EXEC_SUBMIT_FAILED,
    BUDGET_UNAFFORDABLE,
    UNKNOWN_NO_EXEC,
)


def domain_of(rule: str) -> str:
    """规则 → 域（id 里点号前的部分）。报告按域聚合：同一域的花费才是可比的。"""
    return str(rule or "").strip().split(".")[0] or "other"

#: 事件类别（写进影子账的 kind 字段，报告按它分开读）：
#:   veto   模型明确想买、被规则拦下  → 正超额 = 这条规则的成本
#:   unseen 候选在进入模型视野前被剔除 → "假设模型会选它"的弱反事实
#:   unknown 历史回填，原因不可考
KIND_VETO = "veto"
KIND_UNSEEN = "unseen"
KIND_UNKNOWN = "unknown"

_KIND = {
    BUDGET_UNAFFORDABLE: KIND_UNSEEN,
    UNKNOWN_NO_EXEC: KIND_UNKNOWN,
}


def kind_of(rule: str) -> str:
    """规则 → 事件类别；未登记规则按 veto 记（宁可按最强口径读，不静默丢类别）。"""
    return _KIND.get(str(rule).strip(), KIND_VETO)
