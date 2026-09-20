"""信号准确率回看（T-N 分数/排名 → 至今涨跌）纯函数测试。

覆盖：信号日阶梯、收益口径与空值安全、除权校正、口径一致性判定、分档统计、符号归一。
不打 DB / 不读 parquet——取数层由 test_stock_lookback_wiring.py 的源码断言守。
"""

import math
from datetime import date

import pytest

from backend.services.api.stock_lookback import (
    apply_scale,
    bucket_stats,
    canonical_symbol,
    compute_return,
    parse_asof,
    pick_lookback_dates,
    scale_comparable,
)

# 实测的信号日阶梯（2026-09-21 为锚点，降序）
LADDER = [
    "2026-09-21",
    "2026-09-18",
    "2026-09-17",
    "2026-09-16",
    "2026-09-15",
    "2026-09-14",
    "2026-09-11",
    "2026-09-10",
    "2026-09-09",
    "2026-09-08",
    "2026-09-07",
]


# ---------------------------------------------------------------------------
# 信号日阶梯
# ---------------------------------------------------------------------------


def test_pick_lookback_dates_real_ladder():
    """实测阶梯：T-3=09-16 / T-5=09-14 / T-10=09-07。"""
    assert pick_lookback_dates(LADDER, [3, 5, 10]) == {
        3: "2026-09-16",
        5: "2026-09-14",
        10: "2026-09-07",
    }


def test_pick_lookback_dates_short_ladder_omits_missing_not_shift():
    """阶梯不够长时，缺的档**整个不出现**——绝不顺延到别的日期充数。"""
    short = LADDER[:4]  # 只有 09-21 / 09-18 / 09-17 / 09-16
    assert pick_lookback_dates(short, [3, 5, 10]) == {3: "2026-09-16"}


def test_pick_lookback_dates_order_independent():
    """乱序传入 lookbacks 结果一致（前端可能按任意顺序给）。"""
    assert pick_lookback_dates(LADDER, [10, 3, 5]) == pick_lookback_dates(
        LADDER, [3, 5, 10]
    )


def test_pick_lookback_dates_empty_ladder():
    assert pick_lookback_dates([], [3, 5, 10]) == {}


def test_pick_lookback_dates_offset_zero_is_anchor():
    """N=0 就是锚点自身（防御性：参数校验应拦掉，但函数本身也别崩）。"""
    assert pick_lookback_dates(LADDER, [0]) == {0: "2026-09-21"}


# ---------------------------------------------------------------------------
# 收益
# ---------------------------------------------------------------------------


def test_compute_return_basic():
    assert compute_return(100.0, 110.0) == pytest.approx(0.10)
    assert compute_return(100.0, 90.0) == pytest.approx(-0.10)


def test_compute_return_none_safe():
    """任一端缺失 → None。绝不能兜底成 0，那是在宣称「没涨没跌」。"""
    assert compute_return(None, 110.0) is None
    assert compute_return(100.0, None) is None
    assert compute_return(None, None) is None


def test_compute_return_rejects_nonpositive_base():
    """`daily_forward` 早年有负价/趋零的损坏记录（前复权幻觉），相除会出 inf/天文数字。"""
    assert compute_return(0.0, 110.0) is None
    assert compute_return(-1.0, 110.0) is None
    assert compute_return(1e-12, 110.0) is None  # 趋零同样拒绝


def test_compute_return_rejects_nan():
    assert compute_return(math.nan, 110.0) is None
    assert compute_return(100.0, math.nan) is None


def test_compute_return_zero_now_price_is_legit():
    """现价为 0 → -100%，这是有效信息（虽然极端），不该当缺失吞掉。"""
    assert compute_return(100.0, 0.0) == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# 除权校正系数
# ---------------------------------------------------------------------------


def test_apply_scale_no_ex_dividend_is_identity():
    """无除权：实时价与前复权最新收盘同基准，k = 1，不改变数值。"""
    price, scaled = apply_scale(now=40.59, pre_close=40.59, qfq_latest=40.59)
    assert price == pytest.approx(40.59)
    assert scaled is False


def test_apply_scale_with_ex_dividend_shrinks_now_price():
    """除权日：PreClose 是未复权昨收 100，前复权最新收盘 95 → k = 0.95。

    实时价 100 必须缩成 95 才能与历史前复权基准相除，否则凭空多出 5% 涨幅。
    """
    price, scaled = apply_scale(now=100.0, pre_close=100.0, qfq_latest=95.0)
    assert price == pytest.approx(95.0)
    assert scaled is True

    # 端到端体现：不校正会算成 +5%，校正后为 0%
    base = 95.0
    assert compute_return(base, 100.0) == pytest.approx(0.0526, abs=1e-4)
    assert compute_return(base, price) == pytest.approx(0.0)


def test_apply_scale_falls_back_when_preclose_missing():
    """PreClose 缺失/非正 → 不缩放（k=1），仍然给出一个可用的价格。"""
    for bad in (None, 0, -1.0, math.nan):
        price, scaled = apply_scale(now=100.0, pre_close=bad, qfq_latest=95.0)
        assert price == pytest.approx(100.0)
        assert scaled is False


def test_apply_scale_none_now_price():
    price, scaled = apply_scale(now=None, pre_close=100.0, qfq_latest=95.0)
    assert price is None
    assert scaled is False


# ---------------------------------------------------------------------------
# 口径一致性（逐回看点）
# ---------------------------------------------------------------------------


def test_scale_comparable_same_family():
    """实测：T-3 该 run 的 sd 0.1466 vs 锚点 0.2478 = 0.59 倍 → 同口径。"""
    assert scale_comparable(0.1466, 0.2478) is True
    assert scale_comparable(0.2052, 0.2478) is True  # T-5，0.83 倍


def test_scale_comparable_flags_t10_real_case():
    """实测：T-10（09-07）该日只有窄口径 run，sd 0.0104 vs 锚点 0.2478 = 0.042 倍。"""
    assert scale_comparable(0.0104, 0.2478) is False


def test_scale_comparable_boundaries():
    assert scale_comparable(0.3, 0.1) is True  # 恰好 3 倍
    assert scale_comparable(0.31, 0.1) is False  # 超 3 倍
    assert scale_comparable(0.1, 0.3) is True  # 反向 1/3
    assert scale_comparable(0.09, 0.3) is False


def test_scale_comparable_zero_sd_is_not_comparable():
    """分数全同 → 没有区分度，不可比（不是「完美一致」）。"""
    assert scale_comparable(0.0, 0.25) is False


def test_scale_comparable_unknown_does_not_cry_wolf():
    """任一侧缺失 → 判不了，按可比处理，别把表涂满黄条。"""
    assert scale_comparable(None, 0.25) is True
    assert scale_comparable(0.2, None) is True


# ---------------------------------------------------------------------------
# 分档统计
# ---------------------------------------------------------------------------


def _rows(spec):
    """spec: [(rank_pct, score, ret)] → 统一行结构。"""
    return [{"pct": pct, "score": score, "ret": ret} for pct, score, ret in spec]


def test_bucket_stats_hand_computed():
    """100 只：前 20% 各涨 10%，后 20% 各跌 5%，中间不涨不跌。

    高分档均涨 0.10、低分档均涨 -0.05、价差 0.15、两个命中率都是 100%。
    """
    spec = []
    for i in range(100):
        pct = i / 99  # 0 → 1
        if pct >= 0.8:
            spec.append((pct, 0.6, 0.10))
        elif pct <= 0.2:
            spec.append((pct, -0.6, -0.05))
        else:
            spec.append((pct, 0.0, 0.0))
    out = bucket_stats(_rows(spec))

    assert out["sample"] == 100
    assert out["hi_avg"] == pytest.approx(0.10)
    assert out["lo_avg"] == pytest.approx(-0.05)
    assert out["spread"] == pytest.approx(0.15)
    assert out["hi_hit"] == pytest.approx(1.0)
    assert out["lo_hit"] == pytest.approx(1.0)
    assert out["missing_price"] == 0


def test_bucket_stats_lo_hit_counts_declines_not_rises():
    """回归：低分命中 = 低分档里**下跌**的占比。写反了整张表的结论就翻个。

    构造低分档 20 只：10 只跌、10 只涨 → 命中率必须是 0.5，而不是 0.5 的巧合——
    用 1 跌 19 涨再验一次，写反会得到 0.95。
    """
    spec = [(0.9, 0.5, 0.01)] + [(0.5, 0.0, 0.0)]
    spec += [(0.05, -0.5, -0.01)] + [(0.05, -0.5, 0.01)] * 19
    out = bucket_stats(_rows(spec))
    assert out["lo_hit"] == pytest.approx(0.05)  # 1/20 下跌


def test_bucket_stats_all_returns_missing_yields_none_not_zero():
    """无有效收益 → 统计一律 None。**尤其是 spread 不能是 0.0**：
    0 价差是在宣称「模型无区分度」，那是事实主张；缺数据不是事实主张。
    """
    out = bucket_stats(_rows([(0.9, 0.5, None), (0.1, -0.5, None)]))
    assert out["hi_avg"] is None
    assert out["lo_avg"] is None
    assert out["spread"] is None
    assert out["hi_hit"] is None
    assert out["lo_hit"] is None
    assert out["missing_price"] == 2


def test_bucket_stats_empty_rows():
    out = bucket_stats([])
    assert out["sample"] == 0
    assert out["hi_avg"] is None
    assert out["spread"] is None


def test_bucket_stats_all_negative_scores_still_buckets():
    """全负分日（用户原话「分数﹣的」场景）：分档按分位而非符号，两档都非空。"""
    spec = [(i / 99, -0.9 + i * 0.001, 0.01 if i >= 80 else -0.01) for i in range(100)]
    out = bucket_stats(_rows(spec))
    assert out["n_hi"] > 0, "全负分也要有高分档，否则按符号阈值分档就废了"
    assert out["n_lo"] > 0
    assert out["n_neg"] == 100
    assert out["avg_score_hi"] < 0  # 高分档也是负分——正是要展示给用户看的


def test_bucket_stats_negative_bucket_is_separate_from_low_bucket():
    """负分档与低分档口径不同：负分可能落在中段。"""
    spec = [(0.5, -0.2, -0.03)] * 10 + [(0.5, 0.9, 0.03)] * 10
    out = bucket_stats(_rows(spec))
    assert out["n_neg"] == 10
    assert out["neg_avg"] == pytest.approx(-0.03)
    assert out["n_lo"] == 0  # 没有 pct<=0.2 的
    assert out["lo_avg"] is None


def test_bucket_stats_missing_pct_excluded_from_buckets():
    """rank_pct 缺失的票进不了任何分档，但计入 sample。"""
    spec = [(0.95, 0.9, 0.05), (None, 0.5, 0.02), (0.05, -0.9, -0.05)]
    out = bucket_stats(_rows(spec))
    assert out["sample"] == 3
    assert out["n_hi"] == 1
    assert out["n_lo"] == 1


def test_bucket_stats_bucket_width_configurable():
    spec = [(i / 99, float(i), 0.01) for i in range(100)]
    wide = bucket_stats(_rows(spec), bucket_pct=0.5)
    narrow = bucket_stats(_rows(spec), bucket_pct=0.1)
    assert wide["n_hi"] > narrow["n_hi"]
    assert wide["n_lo"] > narrow["n_lo"]


# ---------------------------------------------------------------------------
# 符号归一（engine_signal_scores 纯数字 → QuantDB 后缀式）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("600519", "600519.SH"),
        ("600519.SH", "600519.SH"),
        ("sh600519", "600519.SH"),
        ("SH600519", "600519.SH"),
        ("000001", "000001.SZ"),
        ("300750", "300750.SZ"),
        ("688981", "688981.SH"),
        ("830001", "830001.BJ"),
        (" 600519 ", "600519.SH"),  # 脏空白
    ],
)
def test_canonical_symbol_cn(raw, expected):
    assert canonical_symbol(raw) == expected


@pytest.mark.parametrize("raw", ["00700", "0700.HK", "AAPL", "BRK.B", "", None, "abc"])
def test_canonical_symbol_rejects_non_cn(raw):
    """港股/美股/垃圾值一律 None —— 本表只做 CN；转不出来就宁可缺，不能瞎猜交易所。"""
    assert canonical_symbol(raw) is None


# ---------------------------------------------------------------------------
# asof 解析
# ---------------------------------------------------------------------------


def test_parse_asof_returns_date_object_not_str():
    """回归：asyncpg 绑 DATE 只认 `date` 对象。

    实测传 `'2026-09-14'` 会在执行期炸
    `DataError: 'str' object has no attribute 'toordinal'`——是 **500**，
    不是查空。所以这一条必须盯住类型，不能只盯值。
    """
    out = parse_asof("2026-09-14")
    assert out == date(2026, 9, 14)
    assert not isinstance(out, str)


def test_parse_asof_blank_is_none():
    """空 → None（= 不设锚点），不能当成非法值报错。"""
    assert parse_asof(None) is None
    assert parse_asof("") is None
    assert parse_asof("   ") is None


def test_parse_asof_accepts_datetime_string_and_date():
    """带时间戳的 ISO 串取日期段；已经是 date 则幂等。"""
    assert parse_asof("2026-09-14T10:00:00") == date(2026, 9, 14)
    assert parse_asof(date(2026, 9, 14)) == date(2026, 9, 14)


@pytest.mark.parametrize("bad", ["2026/09/14", "yesterday", "2026-13-01", "2026091"])
def test_parse_asof_rejects_garbage(bad):
    """非法格式抛 ValueError（端点转 400），绝不静默当成 None 悄悄换锚点。"""
    with pytest.raises(ValueError):
        parse_asof(bad)


def test_parse_asof_accepts_compact_form_version_independently():
    """紧凑式 `20260914` 必须显式解析，不能靠 `fromisoformat`。

    回归：`date.fromisoformat` 对紧凑式的接受度是 **Python 版本相关的**
    （3.11+ 收、3.10 拒）。当初就是靠它，本机 50 条全过、容器（3.10）会 400——
    同一次请求两个结果。所以这里既断言「收」，也断言不会退化成靠版本。
    """
    assert parse_asof("20260914") == date(2026, 9, 14)
    assert parse_asof(" 20260914 ") == date(2026, 9, 14)
    with pytest.raises(ValueError):
        parse_asof("20261301")  # 紧凑式里的非法月也要拦住
