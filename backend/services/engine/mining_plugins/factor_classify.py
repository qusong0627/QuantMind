"""因子分类（池总览「因子分类」口径的单源实现）。

分类两段（优先级从高到低）
--------------------------
1. ``rd_agent_factors.metadata_json.description`` 的 ``[类别]`` 前缀——挖掘
   LLM 生成因子时自带的细类标签（如 ``[隔夜跳空反转因子-中期种子]``），
   经有序规则表归一到大类；
2. 无前缀/无描述 → 因子名关键词兜底（英文命名风格：``atr_14``/``mfi_14``…）；
3. 都不命中 → ``other``（「其他」如实呈现——**不猜、不塞进某个大类凑数**）。

规则是**有序 substring 匹配，先命中先赢**，优先级即业务判断，逐条注释：
* 「量价背离反转」整体是反转族（背离只是形成机制）→ 显式规则先行；
* 「X 调整 Y」按头部名词归类：波动率调整动量 → 动量、流动性调整波动 → 波动；
* 「交互」因子（动量×量能…）先于其构成词；
* 隔夜/跳空反转（隔夜跳空反转因子族）归**反转**——经济含义是反转，
  纯隔夜信息/跳空因子才归隔夜族。

金样 ``backend/tests/fixtures/factorCategoryGolden.json`` 冻结现有全量
词表（148 个描述前缀 + 20 个无描述因子名）的分类结果：改规则必须显式
同步金样（同 ``decomposeCardsGolden`` 纪律）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Mapping

logger = logging.getLogger(__name__)

#: 规范大类（id → 中文名）。展示端按计数排序；``other`` 永远显式呈现。
CANONICAL_CLASSES: Mapping[str, str] = {
    "momentum": "动量与趋势",
    "reversal": "反转与均值回归",
    "volatility": "波动与风险",
    "volume": "成交量与量能",
    "price_volume": "量价关系",
    "liquidity": "流动性与规模",
    "overnight": "隔夜与跳空",
    "position": "日内与价格位置",
    "moneyflow": "资金流",
    "sentiment": "概念与情绪",
    "interaction": "交互",
    "return_dist": "收益与分布",
    "valuation": "估值",
    "other": "其他",
}

OTHER_CLASS = "other"

_BRACKET_RE = re.compile(r"^\[([^\]]+)\]")


def _rules(*pairs: tuple[str, str]) -> tuple[tuple[re.Pattern[str], str], ...]:
    return tuple((re.compile(p, re.IGNORECASE), cls) for p, cls in pairs)


#: 描述前缀 → 大类（有序；先命中先赢。优先级的业务理由见模块 docstring）。
_LABEL_RULES: tuple[tuple[re.Pattern[str], str], ...] = _rules(
    (r"交互|interaction", "interaction"),
    # 量价背离反转族：名称含「量价」但它本体是反转因子 → 先于下方的量价/反转规则
    (r"量价背离反转", "reversal"),
    # 量价/价量/相关：价格与成交量的关系类（含「量价因子」——动量加权也按量价族）。
    # 注意：裸「相关」是子串规则——将来出现「市场相关性/beta 因子」会误入此族，
    # 届时必须把本规则收窄为量价相关语境（如 量价相关|价量相关|价格.*相关）。
    (r"量价|价量|相关|price.?volume|correlation", "price_volume"),
    # 资金流族：净流入-换手率背离、吸筹、MFI —— 先于「动量」（背离动量等）
    (r"背离|吸筹|资金流|净流入|money.?flow", "moneyflow"),
    # 概念热度/情绪整族先于动量与位置（「概念热度趋势」含趋势、「概念热度相对
    # 位置」含位置，都属概念族）
    (r"概念|情绪|concept|sentiment", "sentiment"),
    # 反转族：含「完全体反转」与「X 调整反转」（头部名词=反转）、均值回归
    (r"反转|回归|reversal|reversion", "reversal"),
    # 动量族：动量/趋势/乖离/均线/筹码动量/超买超卖振荡器/技术指标
    # （「波动率调整动量」「流动性调整动量」在此命中——按头部名词归类）
    (r"动量|momentum|趋势|trend|乖离|均线|筹码|超买超卖|相对强弱|技术指标", "momentum"),
    # 隔夜族：纯隔夜信息/跳空（反转/动量变体已被上方规则收走）
    (r"隔夜|跳空|overnight|gap", "overnight"),
    # 波动族：波动/振幅/区间宽度/回撤/风险（「流动性调整波动」按头部名词=波动）
    (r"波动|振幅|宽度|回撤|风险|volatility|risk", "volatility"),
    # 流动性族：流动性/非流动性/规模
    (r"流动|liquidity|规模", "liquidity"),
    # 量能族：量能/量比/成交量/成交额（「流动性因子-成交额加速」已被上方流动收走）
    (r"量能|量比|成交量|成交额|volume", "volume"),
    # 日内与位置：日内收益/多空、价格/收盘/区间位置
    (r"日内|位置|position|intraday", "position"),
    (r"估值|valuation", "valuation"),
    # 收益与分布：基础收益、偏度（隔夜/日内收益已被上方规则收走）
    (r"收益|偏度|return|skew", "return_dist"),
)

#: 因子名 → 大类（无描述时的兜底；同样先命中先赢）。
#: 注意 ``vol_?\d`` 不吞 ``volume_*``（vol 后必须紧跟数字/下划线数字）。
_NAME_RULES: tuple[tuple[re.Pattern[str], str], ...] = _rules(
    (r"交互|interaction", "interaction"),
    (r"reversal|rev_?\d|_rev\b|reversion", "reversal"),
    (r"gap|overnight", "overnight"),
    # 波动率先于日内/流动性：amplitude/atr 是波动族（「intraday amplitude」亦然）
    (r"amplitude|atr|volatility|vol_?\d|std|sigma|range|cv_?\d", "volatility"),
    # \bturn 防误吞 "return"（"re-turn" 里含 "turn"，无边界词首匹配会命中）
    (r"amihud|illiq|liq_|liquidity|turnover|\bturn|vwap|capacity|float_mv", "liquidity"),
    (r"netflow|mfi|flow", "moneyflow"),
    (r"corr|sync|pv_", "price_volume"),
    (r"volume|volratio|vol_ratio", "volume"),
    (r"rsi|momentum|mom_?\d|trend|ma_?\d|bias|cci|kdj|macd", "momentum"),
    (r"intraday|position|close_pos", "position"),
    (r"return|skew|upday", "return_dist"),
    (r"chip|concept|hot", "sentiment"),
)


@dataclass(frozen=True)
class FactorClass:
    """一次分类的结果：大类 id + 中文名 + 原始细类标签（无 = None）。"""

    category_id: str
    category_label: str
    raw_label: str | None = None


def _match(rules: tuple[tuple[re.Pattern[str], str], ...], text: str) -> str | None:
    for pattern, cls in rules:
        if pattern.search(text):
            return cls
    return None


def classify_raw_label(label: str) -> str:
    """描述前缀（已剥 ``[]``）→ 大类 id；不命中 → ``other``。"""
    found = _match(_LABEL_RULES, (label or "").strip())
    if found is None:
        logger.debug("[FactorClassify] 未命中描述前缀规则: %r", label)
        return OTHER_CLASS
    return found


def classify_by_name(name: str) -> str:
    """因子名 → 大类 id；不命中 → ``other``。"""
    found = _match(_NAME_RULES, (name or "").strip())
    if found is None:
        logger.debug("[FactorClassify] 未命中因子名规则: %r", name)
        return OTHER_CLASS
    return found


def extract_raw_label(description: str | None) -> str | None:
    """从描述里剥出 ``[类别]`` 前缀；无前缀/无描述 → None。"""
    m = _BRACKET_RE.match((description or "").strip())
    return m.group(1).strip() if m else None


def classify_factor(
    *, factor_name: str, description: str | None = None
) -> FactorClass:
    """入口：描述前缀优先，其次因子名兜底，都无 → other。

    不读 fomulation/code（描述前缀已覆盖 93%，名字兜底覆盖其余——统计见
    金样）；找不到证据就不猜类别。
    """
    raw = extract_raw_label(description)
    if raw:
        cls = classify_raw_label(raw)
        return FactorClass(cls, CANONICAL_CLASSES[cls], raw)
    cls = classify_by_name(factor_name)
    return FactorClass(cls, CANONICAL_CLASSES[cls], None)


def category_label(category_id: str | None) -> str:
    """大类 id → 中文名（未知 id 显式回「其他」，不回空串）。"""
    return CANONICAL_CLASSES.get(category_id or "", CANONICAL_CLASSES[OTHER_CLASS])


__all__ = [
    "CANONICAL_CLASSES",
    "OTHER_CLASS",
    "FactorClass",
    "category_label",
    "classify_by_name",
    "classify_factor",
    "classify_raw_label",
    "extract_raw_label",
]
