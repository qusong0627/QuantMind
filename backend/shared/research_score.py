"""研究评分（0–100 截面分位）——**后端唯一的分数刻度**。

与前端 ``electron/src/features/shared/researchScore.ts`` 是同一公式的两份实现，
金样共用 ``electron/src/features/shared/__tests__/fixtures/researchScoreGolden.json``：
改这边必须改那边，``backend/tests/test_research_score.py`` 会当场变红。

口径：``研究评分 = rank_pct × 100``，保留一位小数；含义是「该标的在**同交易日、
同市场**截面内的百分位」，跨市场/跨模型不可直接比较，不含方向。

用途：推送文案、API 响应、导出文件里凡是要显示分数的地方都走这里，
避免各处自己 ``* 100`` 一次、各自决定保留几位、各自决定缺失显示什么。
"""

from __future__ import annotations

import math

__all__ = [
    "RESEARCH_SCORE_BANDS",
    "RESEARCH_SCORE_MISSING",
    "RESEARCH_SCORE_HINT",
    "SIGNAL_POSITION_LABELS",
    "signal_position_label",
    "research_score",
    "format_research_score",
    "research_score_band",
    "format_research_score_with_band",
    "format_rank_pct_as_research_score",
]

RESEARCH_SCORE_MISSING = "—"

#: 档位刻度。上三档切线（85/70/60）刻意与 ``eval_scoring.py`` 的评级刻度一致，
#: 避免同一个分数在两处得到不同结论；40 只是把「居后」与「尾部」分开。
RESEARCH_SCORE_BANDS: tuple[tuple[float, str], ...] = (
    (85.0, "头部"),
    (70.0, "居前"),
    (60.0, "居中"),
    (40.0, "居后"),
    (0.0, "尾部"),
)

#: 口径声明（唯一一份）。与前端 ``RESEARCH_SCORE_HINT`` **字面量必须一致**。
#:
#: 「同一模型」四个字不能省：``rank_pct`` 的分母是**同一次推理**（``PARTITION BY run_id``，
#: 见 ``signal_contract.py`` 顶部口径说明），不是全市场跨模型截面。含糊成「全市场分位」
#: 是拔高，与后端实际算的东西不符。
RESEARCH_SCORE_HINT = (
    "该标的在同一模型、同一交易日的截面内所处百分位（0–100，越高越靠前）。"
    "跨模型/跨市场不可直接比较，仅反映当日截面相对位置。"
)

#: 内部信号枚举 → 中性位置词。与前端 ``electron/src/features/shared/signalVocabulary.ts``
#: 的 ``SIGNAL_POSITION_LABELS`` **字面量必须一致**——同一套展示口径的两份实现，改一边必须改另一边。
#:
#: 为什么要译：``signal_side`` 是推理链路的内部判定（筛选、撮合、推送都在消费它），
#: 直接渲染成「买入/卖出」就是在向用户输出**投资建议**的形态；展示面一律只说位置。
#:
#: **不写「前 20% / 后 20%」**：该判定有两条来源——推理链路按百分位
#: （``_resolve_signal_sides`` 的 ``buy_pct=0.20``）加共识/置信闸门，而 pred.parquet 回退线
#: 按**绝对阈值**（``fusion > 0.2``）。写死百分比会在其中一条来源上变成假话，所以只给定性位置。
#:
#: **边界**：执行面（下单表单、委托台账、成交回报、手动任务）必须继续说买入/卖出，
#: 含糊化会让人下错单——故这张表只用于展示面文案。
SIGNAL_POSITION_LABELS: dict[str, str] = {
    "BUY": "靠前",
    "SELL": "靠后",
    "HOLD": "居中",
}


def signal_position_label(side: object) -> str:
    """``signal_side`` → 中性位置词。未知值/缺失一律 ``—``。

    **不猜**：把不认识的枚举硬套成「靠前」是在替一个我们没定义的判定编含义。
    """
    if side is None:
        return RESEARCH_SCORE_MISSING
    return SIGNAL_POSITION_LABELS.get(str(side).strip().upper(), RESEARCH_SCORE_MISSING)


def _as_finite_number(value: object) -> float | None:
    """严格取数：只认真正的数值。

    **不做 ``float(str)`` 那样的宽松转换**——前端 ``typeof x !== 'number'`` 会把
    ``"0.5"`` 判为缺失，后端若照单全收，同一次调用的两端会给出不同结论，
    金样也就失去了意义。``bool`` 是 ``int`` 的子类，显式挡掉。
    """
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def research_score(rank_pct: float | None) -> float | None:
    """``rank_pct``（0–1）→ 研究评分（0.0–100.0，一位小数）。

    缺失或**越界**一律返回 ``None``：越界通常意味着调用方把百分数当分位传进来了，
    静默截断会把口径错误伪装成正常分数，返回 ``None`` 让错误当场可见。

    取整用 ``floor(x * 1000 + 0.5)``（四舍五入到一位小数）而不是内置 ``round``：
    ``round`` 走银行家舍入（``round(0.5) == 0``），与前端 JS 的
    ``Math.floor(x * 1000 + 0.5)`` 会在 ``rank_pct=0.0005`` 这类边界上分叉。
    两边同序浮点运算 → 位级别一致，金样才真的锁得住。
    """
    value = _as_finite_number(rank_pct)
    if value is None:
        return None
    if value < 0.0 or value > 1.0:
        return None
    return math.floor(value * 1000 + 0.5) / 10


def format_research_score(score: float | None) -> str:
    """格式化：一位小数；缺失 → ``—``。"""
    value = _as_finite_number(score)
    if value is None:
        return RESEARCH_SCORE_MISSING
    return f"{value:.1f}"


def research_score_band(score: float | None) -> str:
    """档位名：纯描述截面位置。缺失 → ``—``（**不是「尾部」**）。"""
    value = _as_finite_number(score)
    if value is None:
        return RESEARCH_SCORE_MISSING
    for minimum, label in RESEARCH_SCORE_BANDS:
        if value >= minimum:
            return label
    return RESEARCH_SCORE_MISSING


def format_research_score_with_band(score: float | None) -> str:
    """组合展示：``87.2（头部）``；缺失 → ``—``。"""
    value = _as_finite_number(score)
    if value is None:
        return RESEARCH_SCORE_MISSING
    return f"{format_research_score(value)}（{research_score_band(value)}）"


def format_rank_pct_as_research_score(rank_pct: float | None) -> str:
    """由 ``rank_pct`` 一步得到展示串（调用方最常见的用法）。"""
    return format_research_score_with_band(research_score(rank_pct))
