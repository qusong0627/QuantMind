"""统计派 · 候选因子族（移植 P2 / Tier 2）。

32 个只用 OHLCV+成交额的价量候选（含 原实现 7 个：5 日反转 / 20 日
动量 / 放量 / 额突增 / 5 日非流动性 / 5 日振幅 / 5 日跳空；T-FM-05 增自适应族
4 个：波动状态切换 / R² 门控 / 偏度门控 / 波动×峰度双门控），每个带：数学式、
文献出处、pandas 实现与 **qlib 表达式**（factor_code 落库/回测形态）。

双表示的纪律（比原实现的「pandas + qd.panel 代码体」更严）：

- pandas 实现严格对齐 **qlib 算子语义**（`qlib/data/ops.py` 实测：滚动窗口
  ``min_periods=1``、`Std` 即 pandas ``std()``（ddof=1）、逐元素 max 是
  ``Greater`` 而非 ``Max``（`Max` 只有滚动重载！）、条件用 `Gt`/`Lt` 比较算子
  （NaN 经比较为假；`Greater` 是 max 不是比较）、`If(cond,a,b)` 为 `np.where`
  语义；`Skew`/`Kurt` 输出是 float64 计算后投 float32 的两段式——口径详见
  自适应族注释）；
- 等价性由集成测试钉住：同一份 qlib 取出的 OHLCV 面板上，qlib 表达式求值 与
  ``compute`` 输出逐点对比（``test_statistical_candidates.py``）。表达式改了
  而实现没改（或反之）会直接红。

方向（正/负 IC 预期）不在此固定——由数据说话，IC/RankIC 符号进结果。
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable

import numpy as np
import pandas as pd

#: 候选计算所需的行情列（QuantDB 日线面板）
REQUIRED_COLUMNS: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
)

FAMILY_LABELS: dict[str, str] = {
    "reversal": "反转",
    "momentum": "动量",
    "lottery": "彩票/偏度",
    "volatility": "波动率",
    "liquidity": "流动性/量能",
    "price_volume": "量价关系",
    "intraday": "日内结构",
    "adaptive": "自适应/状态切换",
}


@dataclass(frozen=True)
class Candidate:
    key: str
    name: str
    family: str
    formulation: str
    source: str
    expression: str
    min_history: int  # 表达式所需最少 bar 数（引擎据此裁每个 symbol 的头部）
    compute: Callable[[pd.DataFrame], pd.Series]


def _ret(frame: pd.DataFrame) -> pd.Series:
    return frame["close"] / frame["close"].shift(1) - 1


def _mean(series: pd.Series, window: int) -> pd.Series:
    # min_periods=1 与 qlib Rolling 一致（qlib 不裁头部——暖机由引擎按
    # min_history 显式裁，算得对不对与用不用得到分开管）
    return series.rolling(window, min_periods=1).mean()


def _std(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=1).std()


# ── 反转族 ───────────────────────────────────────────────────────────


def _reversal(frame: pd.DataFrame, window: int) -> pd.Series:
    return frame["close"].shift(window) / frame["close"] - 1


# ── 动量族 ───────────────────────────────────────────────────────────


def _momentum(frame: pd.DataFrame, window: int) -> pd.Series:
    return frame["close"] / frame["close"].shift(window) - 1


# ── 自适应族辅助（T-FM-05）── qlib 回归/矩算子的逐位复刻 ─────────────
#
# 论文附录 D 的「回归/条件算子」模板（036-046 波动状态、080-083 R² 门控、
# 094-105 高阶矩切换）的 A 股适配。门控量是计算出来的状态（短长窗波动比、
# 拟合优度、偏度/峰度），不是又一个固定窗——新逻辑而非窗口变体，且候选池
# 只 +4（控制 FDR 膨胀）。
#
# 容器内对拍实测钉住的口径（test_statistical_candidates.py 逐点对拍兜底）：
# - 条件一律 `Gt`/`Lt`（比较算子，NaN 经比较为假）；`Greater` 是逐元素 max，
#   不能当比较用；组合条件走 `Mul`（对 0/1 值即布尔与），pandas 侧直接 ``&``。
# - `Slope`/`Rsquare`/`Resi` 是 cython 滚动回归（窗口 y 对 x=0..n−1 最小二乘）：
#   rsq = corr²，std≈0（atol=2e-05, min_periods=1）的窗口后处理成 NaN；
#   resi 取**窗口末点**残差。下用 float64 闭式解复刻（实测 maxrel ~6e-8）。
#   **暖机头部不对齐**（实测）：cython 首根 NaN、第 2 根起即出部分窗值
#   （min_periods=1 风格），闭式解不足窗统一 NaN——两边都从 min_history
#   起消费（引擎裁剪 + 对拍区域），头部差异不触达任何判定面。
# - `Skew`/`Kurt` 节点输出是 **float64 计算后投 float32** 的两段式（本仓库
#   qlib 逐位实测；直接对 float32 输入调 pandas rolling 会差 ~1e-7，在
#   ``<3``/``>1`` 这类字面量闸上是潜在翻转点，故照两段式复刻；比较时
#   numpy 弱标量按 float32 生效）。
#
# 与 T-FE-05 选择分的交互（如实记录，不设豁免）：本族表达式结构是全目录
# 最复杂段（如 vol_kurt 一条 op=24 / 字面量=16），简单性因子 0.015–0.082
# （对照：旧目录最复杂的 atr_14 ≈0.06、最简族 0.23/0.59）——同 |RankIC|·
# 一致性下选择分只有最简族的约 1/3–1/40，拿席位需显著更高 IC 是简单性
# 先验的预期折扣；要豁免应改 T-FE-05 口径本身（独立决策）。4 条候选照常
# 全量进 IC/FDR/审计与论文对照。


def _slope64(series: pd.Series, window: int) -> pd.Series:
    """qlib `Slope` 复刻：窗口最小二乘斜率（尾点对齐当前 bar；不足窗处
    NaN——与 cython 头部口径的差异见上方族注释，不触达消费面）。"""
    y = series.astype(np.float64)
    x = np.arange(window, dtype=np.float64)
    xc = x - x.mean()
    sxx = float((xc**2).sum())
    sxy = sum(xc[window - 1 - d] * y.shift(d) for d in range(window))
    return sxy / sxx


def _rsq64(series: pd.Series, window: int) -> pd.Series:
    """qlib `Rsquare` 复刻：corr(x, y)²；std≈0 窗口后处理成 NaN（qlib 算子
    源码同款掩码，atol=2e-05）。"""
    y = series.astype(np.float64)
    x = np.arange(window, dtype=np.float64)
    xc = x - x.mean()
    sxx = float((xc**2).sum())
    sxy = sum(xc[window - 1 - d] * y.shift(d) for d in range(window))
    ysum = y.rolling(window, min_periods=window).sum()
    syy = (y**2).rolling(window, min_periods=window).sum() - ysum**2 / window
    rsq = sxy**2 / (sxx * syy)
    return rsq.mask(np.isclose(y.rolling(window, min_periods=1).std(), 0, atol=2e-05))


def _resi64(
    series: pd.Series, window: int, *, slope: pd.Series | None = None
) -> pd.Series:
    """qlib `Resi` 复刻：窗口回归在末点的残差 y_t − ŷ_t（x 居中后末点
    x=(n−1)/2）。``slope`` 传已算好的斜率可省一次闭式解（值恒同）。"""
    y = series.astype(np.float64)
    if slope is None:
        slope = _slope64(series, window)
    mean = y.rolling(window, min_periods=window).mean()
    return y - mean - slope * (window - 1) / 2.0


def _skew(series: pd.Series, window: int) -> pd.Series:
    """qlib `Skew` 节点输出的两段式复刻（float64 计算 → float32）。

    float32 对齐的是 **qlib 输出列**口径；qlib 表达式树内部节点仍是
    float64（转换只发生在数据边界），闸门阈值（``|skew|>1``）在 qlib 里
    比较未舍入值、在本实现比较已舍入值——贴边样本差 ≤ ~1e-7，由等价性
    测试容差兜底（实测四候选 A 臂命中率 18.7%~42.9%，无空转盲区）。
    """
    return (
        series.astype(np.float64)
        .rolling(window, min_periods=1)
        .skew()
        .astype(np.float32)
    )


def _kurt(series: pd.Series, window: int) -> pd.Series:
    """qlib `Kurt` 节点输出的两段式复刻（float64 计算 → float32）。

    float32 对齐的是 **qlib 输出列**口径；闸门阈值（``kurt<3``）在 qlib
    内部树上是 float64 比较、本实现是已舍入值比较——口径差 ≤ ~1e-7，
    由等价性测试容差兜底（同上，见 :func:`_skew`）。
    """
    return (
        series.astype(np.float64)
        .rolling(window, min_periods=1)
        .kurt()
        .astype(np.float32)
    )


def _volreg_reversal_5d(frame: pd.DataFrame) -> pd.Series:
    vol_short = _std(_ret(frame), 20)
    return _reversal(frame, 5).where(
        vol_short > _mean(vol_short, 60), _momentum(frame, 5)
    )


def _trend_r2_gate_20d(frame: pd.DataFrame) -> pd.Series:
    close = frame["close"]
    slope = _slope64(close, 20)  # 两臂共用：resi 臂只要这一条斜率，不再重算
    return (slope / close).where(
        _rsq64(close, 20) > 0.5, _resi64(close, 20, slope=slope) / close
    )


def _skew_gate_momentum_20d(frame: pd.DataFrame) -> pd.Series:
    return _reversal(frame, 20).where(
        _skew(_ret(frame), 20).abs() > 1, _momentum(frame, 20)
    )


def _vol_kurt_gate_10d(frame: pd.DataFrame) -> pd.Series:
    ret = _ret(frame)
    vol_short = _std(ret, 20)
    gate = (vol_short > _mean(vol_short, 60)) & (_kurt(ret, 24) < 3)
    return _momentum(frame, 10).where(gate, _reversal(frame, 10))


CANDIDATES: tuple[Candidate, ...] = (
    Candidate(
        key="reversal_5d",
        name="5 日反转",
        family="reversal",
        formulation="close[t-5]/close[t] - 1",
        source="Jegadeesh(1990)；A 股短期反转（原实现候选同口径）",
        expression="Ref($close,5)/$close-1",
        min_history=5,
        compute=lambda frame: _reversal(frame, 5),
    ),
    Candidate(
        key="reversal_10d",
        name="10 日反转",
        family="reversal",
        formulation="close[t-10]/close[t] - 1",
        source="Lehmann(1990)",
        expression="Ref($close,10)/$close-1",
        min_history=10,
        compute=lambda frame: _reversal(frame, 10),
    ),
    Candidate(
        key="reversal_20d",
        name="20 日反转",
        family="reversal",
        formulation="close[t-20]/close[t] - 1",
        source="Jegadeesh(1990)",
        expression="Ref($close,20)/$close-1",
        min_history=20,
        compute=lambda frame: _reversal(frame, 20),
    ),
    Candidate(
        key="reversal_60d",
        name="60 日反转",
        family="reversal",
        formulation="close[t-60]/close[t] - 1",
        source="De Bondt & Thaler(1985) 长期反转",
        expression="Ref($close,60)/$close-1",
        min_history=60,
        compute=lambda frame: _reversal(frame, 60),
    ),
    Candidate(
        key="momentum_20d",
        name="20 日动量",
        family="momentum",
        formulation="close[t]/close[t-20] - 1",
        source="Jegadeesh & Titman(1993)（原实现候选同口径）",
        expression="$close/Ref($close,20)-1",
        min_history=20,
        compute=lambda frame: _momentum(frame, 20),
    ),
    Candidate(
        key="momentum_60d",
        name="60 日动量",
        family="momentum",
        formulation="close[t]/close[t-60] - 1",
        source="Jegadeesh & Titman(1993)",
        expression="$close/Ref($close,60)-1",
        min_history=60,
        compute=lambda frame: _momentum(frame, 60),
    ),
    Candidate(
        key="momentum_120d",
        name="120 日动量",
        family="momentum",
        formulation="close[t]/close[t-120] - 1",
        source="Carhart(1997) 动量因子",
        expression="$close/Ref($close,120)-1",
        min_history=120,
        compute=lambda frame: _momentum(frame, 120),
    ),
    Candidate(
        key="high_prox_250d",
        name="52 周高点接近度",
        family="momentum",
        formulation="close[t] / max(close, 250)",
        source="George & Hwang(2004) 52-week high",
        expression="$close/Max($close,250)",
        min_history=250,
        compute=lambda frame: (
            frame["close"] / frame["close"].rolling(250, min_periods=1).max()
        ),
    ),
    Candidate(
        key="max_ret_20d",
        name="20 日最大单日收益",
        family="lottery",
        formulation="max(ret, 20)",
        source="Bali, Cakici & Whitelaw(2011) MAX 效应",
        expression="Max($close/Ref($close,1)-1,20)",
        min_history=21,
        compute=lambda frame: _ret(frame).rolling(20, min_periods=1).max(),
    ),
    Candidate(
        key="skew_20d",
        name="20 日收益偏度",
        family="lottery",
        formulation="skew(ret, 20)",
        source="Boyer, Mitton & Vorkink(2010)；Barberis & Huang(2008)",
        expression="Skew($close/Ref($close,1)-1,20)",
        min_history=21,
        compute=lambda frame: _ret(frame).rolling(20, min_periods=1).skew(),
    ),
    Candidate(
        key="vol_20d",
        name="20 日波动率",
        family="volatility",
        formulation="std(ret, 20)",
        source="Ang, Hodrick, Xing & Zhang(2006)",
        expression="Std($close/Ref($close,1)-1,20)",
        min_history=21,
        compute=lambda frame: _std(_ret(frame), 20),
    ),
    Candidate(
        key="vol_60d",
        name="60 日波动率",
        family="volatility",
        formulation="std(ret, 60)",
        source="Ang et al.(2006)",
        expression="Std($close/Ref($close,1)-1,60)",
        min_history=61,
        compute=lambda frame: _std(_ret(frame), 60),
    ),
    Candidate(
        key="downside_vol_20d",
        name="20 日下行波动",
        family="volatility",
        formulation="std(min(ret, 0), 20)",
        source="Ang & Chen(2002) 下行风险",
        expression="Std(If($close<Ref($close,1),$close/Ref($close,1)-1,0),20)",
        min_history=21,
        compute=lambda frame: (
            _ret(frame).where(_ret(frame) < 0, 0.0).rolling(20, min_periods=1).std()
        ),
    ),
    Candidate(
        key="parkinson_20d",
        name="20 日 Parkinson 波动",
        family="volatility",
        formulation="sqrt(mean(ln(high/low)^2, 20) / (4 ln 2))",
        source="Parkinson(1980) 极值波动率",
        # 4·ln2 用字面量：qlib 0.9.7 的 Log 只接受表达式（Log(2) 装载即炸），
        # 且 repr(4·ln2)=2.772588722239781 与该 double 精确往返（pandas 侧同式）
        expression="Power(Mean(Power(Log($high/$low),2),20)/2.772588722239781,0.5)",
        min_history=20,
        compute=lambda frame: np.sqrt(
            _mean(np.log(frame["high"] / frame["low"]) ** 2, 20) / (4 * np.log(2))
        ),
    ),
    Candidate(
        key="atr_14",
        name="14 日真实波幅",
        family="volatility",
        formulation="mean(max(high-low, |high-prev_close|, |low-prev_close|), 14) / close",
        source="Wilder(1978) ATR",
        expression=(
            "Mean(Greater(Greater($high-$low,Abs($high-Ref($close,1))),"
            "Abs($low-Ref($close,1))),14)/$close"
        ),
        min_history=15,
        compute=lambda frame: (
            _mean(
                np.maximum(
                    np.maximum(
                        frame["high"] - frame["low"],
                        (frame["high"] - frame["close"].shift(1)).abs(),
                    ),
                    (frame["low"] - frame["close"].shift(1)).abs(),
                ),
                14,
            )
            / frame["close"]
        ),
    ),
    Candidate(
        key="amihud_5d",
        name="5 日非流动性",
        family="liquidity",
        formulation="mean(|ret| / (amount + 1e-8), 5)",
        source="Amihud(2002)（原实现 illiq_5d 同口径）",
        expression="Mean(Abs($close/Ref($close,1)-1)/($amount+1e-8),5)",
        min_history=6,
        compute=lambda frame: _mean(_ret(frame).abs() / (frame["amount"] + 1e-8), 5),
    ),
    Candidate(
        key="amihud_20d",
        name="20 日非流动性",
        family="liquidity",
        formulation="mean(|ret| / (amount + 1e-8), 20)",
        source="Amihud(2002)",
        expression="Mean(Abs($close/Ref($close,1)-1)/($amount+1e-8),20)",
        min_history=21,
        compute=lambda frame: _mean(_ret(frame).abs() / (frame["amount"] + 1e-8), 20),
    ),
    Candidate(
        key="volume_spike_20d",
        name="20 日放量倍数",
        family="liquidity",
        formulation="volume / mean(volume, 20)",
        source="原实现候选；Chordia et al.(2001) 交易量",
        expression="$volume/Mean($volume,20)",
        min_history=20,
        compute=lambda frame: frame["volume"] / _mean(frame["volume"], 20),
    ),
    Candidate(
        key="amount_burst_20d",
        name="20 日额突增倍数",
        family="liquidity",
        formulation="amount / mean(amount, 20)",
        source="原实现候选；Chordia et al.(2001)",
        expression="$amount/Mean($amount,20)",
        min_history=20,
        compute=lambda frame: frame["amount"] / _mean(frame["amount"], 20),
    ),
    Candidate(
        key="volume_cv_20d",
        name="20 日成交变异系数",
        family="liquidity",
        formulation="std(volume, 20) / mean(volume, 20)",
        source="Chordia, Roll & Subrahmanyam(2001) 交易量波动",
        expression="Std($volume,20)/Mean($volume,20)",
        min_history=20,
        compute=lambda frame: _std(frame["volume"], 20) / _mean(frame["volume"], 20),
    ),
    Candidate(
        key="pv_corr_20d",
        name="20 日量价相关",
        family="price_volume",
        formulation="corr(close, volume; 20)",
        source="Karpoff(1987) 量价关系综述",
        expression="Corr($close,$volume,20)",
        min_history=20,
        compute=lambda frame: (
            frame["close"].rolling(20, min_periods=1).corr(frame["volume"])
        ),
    ),
    Candidate(
        key="pv_corr_60d",
        name="60 日量价相关",
        family="price_volume",
        formulation="corr(close, volume; 60)",
        source="Karpoff(1987)",
        expression="Corr($close,$volume,60)",
        min_history=60,
        compute=lambda frame: (
            frame["close"].rolling(60, min_periods=1).corr(frame["volume"])
        ),
    ),
    Candidate(
        key="range_5d",
        name="5 日振幅",
        family="intraday",
        formulation="mean((high-low)/close, 5)",
        source="原实现候选 range_5d",
        expression="Mean(($high-$low)/$close,5)",
        min_history=5,
        compute=lambda frame: _mean((frame["high"] - frame["low"]) / frame["close"], 5),
    ),
    Candidate(
        key="range_20d",
        name="20 日振幅",
        family="intraday",
        formulation="mean((high-low)/close, 20)",
        source="日内振幅（波动率近亲）",
        expression="Mean(($high-$low)/$close,20)",
        min_history=20,
        compute=lambda frame: _mean(
            (frame["high"] - frame["low"]) / frame["close"], 20
        ),
    ),
    Candidate(
        key="gap_drift_5d",
        name="5 日跳空漂移",
        family="intraday",
        formulation="mean(open/prev_close - 1, 5)",
        source="原实现候选 gap_drift_5d",
        expression="Mean($open/Ref($close,1)-1,5)",
        min_history=6,
        compute=lambda frame: _mean(frame["open"] / frame["close"].shift(1) - 1, 5),
    ),
    Candidate(
        key="overnight_ret_20d",
        name="20 日隔夜收益",
        family="intraday",
        formulation="mean(open/prev_close - 1, 20)",
        source="Lou, Polk & Skouras(2019) 隔夜收益",
        expression="Mean($open/Ref($close,1)-1,20)",
        min_history=21,
        compute=lambda frame: _mean(frame["open"] / frame["close"].shift(1) - 1, 20),
    ),
    Candidate(
        key="intraday_ret_20d",
        name="20 日日内收益",
        family="intraday",
        formulation="mean(close/open - 1, 20)",
        source="Lou, Polk & Skouras(2019) 日内/隔夜分解",
        expression="Mean($close/$open-1,20)",
        min_history=20,
        compute=lambda frame: _mean(frame["close"] / frame["open"] - 1, 20),
    ),
    Candidate(
        key="close_position_20d",
        name="20 日收盘位置",
        family="intraday",
        formulation="mean((close-low)/(high-low+1e-12), 20)",
        source="RSV/KDJ 技术面；收盘位置效应",
        expression="Mean(($close-$low)/($high-$low+1e-12),20)",
        min_history=20,
        compute=lambda frame: _mean(
            (frame["close"] - frame["low"]) / (frame["high"] - frame["low"] + 1e-12),
            20,
        ),
    ),
    # ── 自适应/状态切换族（T-FM-05；口径见族内辅助函数区注释）────────
    Candidate(
        key="volreg_reversal_5d",
        name="波动状态门控 5 日反转/动量",
        family="adaptive",
        formulation="std(ret,20) > mean(std(ret,20),60) ? rev5 : mom5",
        source="论文附录 D 036/046 波动状态切换模板（T-FM-05 A 股适配）",
        expression="If(Gt(Std($close/Ref($close,1)-1,20),Mean(Std($close/Ref($close,1)-1,20),60)),Ref($close,5)/$close-1,$close/Ref($close,5)-1)",
        min_history=81,
        compute=_volreg_reversal_5d,
    ),
    Candidate(
        key="trend_r2_gate_20d",
        name="趋势拟合度门控动量/回归",
        family="adaptive",
        formulation="rsquare(close,20) > 0.5 ? slope/close : resi/close",
        source="论文附录 D 080/081 R² 门控趋势模板（T-FM-05 A 股适配）",
        expression="If(Gt(Rsquare($close,20),0.5),Slope($close,20)/$close,Resi($close,20)/$close)",
        min_history=20,
        compute=_trend_r2_gate_20d,
    ),
    Candidate(
        key="skew_gate_momentum_20d",
        name="偏度门控 20 日反转/动量",
        family="adaptive",
        formulation="|skew(ret,20)| > 1 ? rev20 : mom20",
        source="论文附录 D 095/101 偏度门控模板（T-FM-05 A 股适配）",
        expression="If(Gt(Abs(Skew($close/Ref($close,1)-1,20)),1),Ref($close,20)/$close-1,$close/Ref($close,20)-1)",
        min_history=21,
        compute=_skew_gate_momentum_20d,
    ),
    Candidate(
        key="vol_kurt_gate_10d",
        name="波动×峰度双门控 10 日动量",
        family="adaptive",
        formulation="(std 高 且 kurt(ret,24) < 3) ? mom10 : rev10",
        source="论文附录 D 094 波动×峰度双门控模板（T-FM-05 A 股适配）",
        expression="If(Gt(Mul(Gt(Std($close/Ref($close,1)-1,20),Mean(Std($close/Ref($close,1)-1,20),60)),Lt(Kurt($close/Ref($close,1)-1,24),3)),0),$close/Ref($close,10)-1,Ref($close,10)/$close-1)",
        min_history=81,
        compute=_vol_kurt_gate_10d,
    ),
)

CANDIDATE_BY_KEY: dict[str, Candidate] = {c.key: c for c in CANDIDATES}

# Fixed candidate genealogy is catalog metadata. Per-user cooling observations
# belong in the owner-scoped persistence layer and must never live here.
BASELINE_BY_KEY: dict[str, str] = {
    "reversal_10d": "reversal_5d",
    "reversal_20d": "reversal_10d",
    "reversal_60d": "reversal_20d",
    "momentum_60d": "momentum_20d",
    "momentum_120d": "momentum_60d",
    "high_prox_250d": "momentum_120d",
    "skew_20d": "max_ret_20d",
    "vol_60d": "vol_20d",
    "downside_vol_20d": "vol_20d",
    "parkinson_20d": "vol_20d",
    "atr_14": "vol_20d",
    "amihud_20d": "amihud_5d",
    "volume_spike_20d": "amihud_20d",
    "amount_burst_20d": "volume_spike_20d",
    "volume_cv_20d": "volume_spike_20d",
    "pv_corr_60d": "pv_corr_20d",
    "range_20d": "range_5d",
    "overnight_ret_20d": "gap_drift_5d",
    "close_position_20d": "intraday_ret_20d",
    "volreg_reversal_5d": "reversal_5d",
    "trend_r2_gate_20d": "momentum_20d",
    "skew_gate_momentum_20d": "momentum_20d",
}

#: 暖机 bar 数 = 全体候选的最大回看（引擎据此外扩取数区间）
MAX_MIN_HISTORY: int = max(c.min_history for c in CANDIDATES)


def compute_panel(panel: pd.DataFrame, candidate: Candidate) -> pd.Series:
    """在整块面板上按 symbol 分组计算候选（输入/输出都是 MultiIndex 面板）。

    面板索引 ``(trade_date, symbol)``，按日期升序；组内赋值即逐 symbol 计算。
    输出按 **输入原序** 回排——组结果按 symbol 拼接，顺序与面板不一致，因子序列
    靠索引对齐，顺序走样就是静默错位，这里显式复位。

    逐组 ``concat`` 而不是 ``groupby.apply``：单组面板上 apply 会把「一组一条
    Series」包装成 DataFrame（实测 1 行 × 1 列），``rename(key)`` 随即炸在
    ``Index('key')``；``transform`` 对帧函数是按列传 Series（KeyError）。逐组
    拼装没有这类边界行为，顺便把「compute 必须返回 Series」查在明面上。
    """
    missing = [c for c in REQUIRED_COLUMNS if c not in panel.columns]
    if missing:
        raise ValueError(
            f"面板缺列: {missing}（候选 {candidate.key} 需要 OHLCV+amount）"
        )
    pieces: list[pd.Series] = []
    for _symbol, group in panel.groupby(level=1, sort=False):
        piece = candidate.compute(group)
        if not isinstance(piece, pd.Series):
            raise TypeError(
                f"候选 {candidate.key} 的 compute 必须返回 Series"
                f"（收到 {type(piece).__name__}）"
            )
        pieces.append(piece)
    if not pieces:
        return pd.Series(dtype=float, index=panel.index, name=candidate.key)
    return pd.concat(pieces).reindex(panel.index).rename(candidate.key)
