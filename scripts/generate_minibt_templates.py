# -*- coding: utf-8 -*-
"""一次性生成 minibt A股策略模板到 strategy_templates/(.py + .json 成对)。"""
import json
import os
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "strategy_templates"

HEADER = '''# -*- coding: utf-8 -*-
"""【minibt·{folder}】{name}

策略思想: {idea}
参考: {source}
数据: QuantDB 前复权日线(代码可传 '600036' / 'SH600036' / '600036.SH')

A 股口径提示:
- 信号当根 K 线收盘价成交, 引擎无 T+1/涨跌停/整手约束, 回测与实盘存在口径差
- 反手逻辑含做空(A 股现货无裸卖空, 实盘请忽略 short_signal 只做多)
- 手续费为双边佣金比例, 未含印花税与滑点
- 想加自动止损可把 buy/sell 换成: self.kline.buy(size=SIZE, stop=BtStop.SegmentationTracking)
"""
import numpy as np

from minibt import Bt, BtIndicator, BtStop, Strategy
from backend.shared.minibt_qdb import load_daily
from backend.shared.minibt_result import run_and_report

SYMBOL = '600036'      # 标的代码
START = '20220101'     # 起始日(前几个月为指标预热, 不产生信号)
END = '20250630'       # 结束日
SIZE = 1000            # 每次交易股数(100 的整数倍)
COMMISSION = 0.00025   # 双边佣金比例(万 2.5)

DF = load_daily(SYMBOL, START, END)

'''

TAIL = '''

if __name__ == "__main__":
    bt = Bt(auto=False)
    bt.addstrategy({cls})
    run_and_report(bt, DF)
'''

T = {}

T["minibt_dual_ma"] = dict(
    folder="趋势跟踪", name="双均线趋势跟踪", cls="DualMAStrategy",
    idea="短期均线上穿长期均线(金叉)做多,下穿(死叉)做空;经典趋势跟随入门策略。",
    source="minibt 快速上手示例",
    category="basic", difficulty="beginner",
    code='''class DualMAStrategy(Strategy):
    params = dict(fast=10, slow=20)

    def __init__(self):
        self.kline = self.get_kline(DF, duration_seconds=86400)
        self.percent_commission = COMMISSION  # ⚠️ 必须放在 get_kline 之后
        self.ma_fast = self.kline.close.sma(self.params.fast)
        self.ma_slow = self.kline.close.sma(self.params.slow)
        self.long_signal = self.ma_fast.cross_up(self.ma_slow)
        self.short_signal = self.ma_fast.cross_down(self.ma_slow)

    def next(self):
        if not self.kline.position:
            if self.long_signal.new:
                self.kline.buy(size=SIZE)
            elif self.short_signal.new:
                self.kline.sell(size=SIZE)
        elif self.kline.position > 0 and self.short_signal.new:
            self.kline.sell(size=SIZE)
        elif self.kline.position < 0 and self.long_signal.new:
            self.kline.buy(size=SIZE)
''',
)

T["minibt_trix"] = dict(
    folder="趋势跟踪", name="TRIX 三重平滑顺势", cls="TRIXStrategy",
    idea="三重 EMA 平滑的变动率(TRIX)上穿其信号线做多,下穿做空;过滤短线噪音。",
    source="shinnytech TRIX 策略教程",
    category="advanced", difficulty="intermediate",
    code='''class TRIX(BtIndicator):
    params = dict(TRIX_PERIOD=12, SIGNAL_PERIOD=9)
    isplot = dict(long_signal=False, short_signal=False)

    def next(self):
        ema1 = self.close.ema(self.params.TRIX_PERIOD)
        ema2 = ema1.ema(self.params.TRIX_PERIOD)
        ema3 = ema2.ema(self.params.TRIX_PERIOD)
        trix = ema3.diff().ZeroDivision(ema3.shift()) * 100.
        signal = trix.sma(self.params.SIGNAL_PERIOD)
        long_signal = trix.cross_up(signal)
        short_signal = trix.cross_down(signal)
        return trix, signal, long_signal, short_signal


class TRIXStrategy(Strategy):
    def __init__(self):
        self.kline = self.get_kline(DF, duration_seconds=86400)
        self.percent_commission = COMMISSION  # ⚠️ 必须放在 get_kline 之后
        self.trix = TRIX(self.kline)

    def next(self):
        if not self.kline.position:
            if self.trix.long_signal.new:
                self.kline.buy(size=SIZE)
            elif self.trix.short_signal.new:
                self.kline.sell(size=SIZE)
        elif self.kline.position > 0 and self.trix.short_signal.new:
            self.kline.sell(size=SIZE)
        elif self.kline.position < 0 and self.trix.long_signal.new:
            self.kline.buy(size=SIZE)
''',
)

T["minibt_hull"] = dict(
    folder="趋势跟踪", name="HullMA 双周期", cls="HullStrategy",
    idea="短周期 Hull 均线上穿/下穿长周期 Hull 均线,且价格在长周期均线同侧才开仓。",
    source="shinnytech Hull 策略教程",
    category="advanced", difficulty="intermediate",
    code='''class Hull(BtIndicator):
    params = dict(LONG_HMA_PERIOD=30, SHORT_HMA_PERIOD=5)
    isplot = dict(long_signal=False, short_signal=False)
    overlap = True

    def _hma(self, period):
        half_period = period // 2
        sqrt_period = int(np.sqrt(period))
        wma1 = self.close.wma(half_period)
        wma2 = self.close.wma(period)
        raw_hma = 2 * wma1 - wma2
        return raw_hma.wma(sqrt_period)

    def next(self):
        short_hma = self._hma(self.params.SHORT_HMA_PERIOD)
        long_hma = self._hma(self.params.LONG_HMA_PERIOD)
        long_signal = short_hma.cross_up(long_hma)
        long_signal &= self.close > long_hma
        short_signal = short_hma.cross_down(long_hma)
        short_signal &= self.close < long_hma
        return short_hma, long_hma, long_signal, short_signal


class HullStrategy(Strategy):
    def __init__(self):
        self.kline = self.get_kline(DF, duration_seconds=86400)
        self.percent_commission = COMMISSION  # ⚠️ 必须放在 get_kline 之后
        self.hull = Hull(self.kline)

    def next(self):
        if not self.kline.position:
            if self.hull.long_signal.new:
                self.kline.buy(size=SIZE)
            elif self.hull.short_signal.new:
                self.kline.sell(size=SIZE)
        elif self.kline.position > 0 and self.hull.short_signal.new:
            self.kline.sell(size=SIZE)
        elif self.kline.position < 0 and self.hull.long_signal.new:
            self.kline.buy(size=SIZE)
''',
)

T["minibt_aroon"] = dict(
    folder="趋势跟踪", name="阿隆指标趋势强度", cls="AroonStrategy",
    idea="Aroon-Up 高位且 Aroon-Dn 低位时做多(创新高能力强),反之做空。",
    source="shinnytech Aroon 策略教程",
    category="advanced", difficulty="intermediate",
    code='''class AroonStrategy(Strategy):
    params = dict(AROON_PERIOD=10, AROON_UPPER=75, AROON_LOWER=25)

    def __init__(self):
        self.kline = self.get_kline(DF, duration_seconds=86400)
        self.percent_commission = COMMISSION  # ⚠️ 必须放在 get_kline 之后
        self.aroon = self.kline.aroon(self.params.AROON_PERIOD)
        self.long_signal = self.aroon.aroon_up > self.params.AROON_UPPER
        self.long_signal &= self.aroon.aroon_down < self.params.AROON_LOWER
        self.short_signal = self.aroon.aroon_down > self.params.AROON_UPPER
        self.short_signal &= self.aroon.aroon_up < self.params.AROON_LOWER
        self.long_signal.isplot = False
        self.short_signal.isplot = False

    def next(self):
        if not self.kline.position:
            if self.long_signal.new:
                self.kline.buy(size=SIZE)
            elif self.short_signal.new:
                self.kline.sell(size=SIZE)
        elif self.kline.position > 0 and self.short_signal.new:
            self.kline.sell(size=SIZE)
        elif self.kline.position < 0 and self.long_signal.new:
            self.kline.buy(size=SIZE)
''',
)

T["minibt_vortex"] = dict(
    folder="趋势跟踪", name="涡旋指标 Vortex", cls="VortexStrategy",
    idea="正向/负向涡旋运动比较:+VI 上穿 -VI 且大于阈值做多,反之做空。",
    source="shinnytech Vortex 策略教程",
    category="advanced", difficulty="intermediate",
    code='''class Vortex(BtIndicator):
    params = dict(VI_PERIOD=14, VI_THRESHOLD=1.0)
    isplot = dict(long_signal=False, short_signal=False)

    def next(self):
        tr = self.true_range()
        plus_vm = (self.high - self.low.shift()).abs()
        minus_vm = (self.low - self.high.shift()).abs()
        tr_sum = tr.rolling(self.params.VI_PERIOD).sum()
        plus_vi = plus_vm.rolling(self.params.VI_PERIOD).sum() / tr_sum
        minus_vi = minus_vm.rolling(self.params.VI_PERIOD).sum() / tr_sum
        long_signal = plus_vi.cross_up(minus_vi)
        long_signal &= plus_vi > self.params.VI_THRESHOLD
        short_signal = minus_vi.cross_up(plus_vi)
        short_signal &= minus_vi > self.params.VI_THRESHOLD
        return plus_vi, minus_vi, long_signal, short_signal


class VortexStrategy(Strategy):
    def __init__(self):
        self.kline = self.get_kline(DF, duration_seconds=86400)
        self.percent_commission = COMMISSION  # ⚠️ 必须放在 get_kline 之后
        self.vortex = Vortex(self.kline)

    def next(self):
        if not self.kline.position:
            if self.vortex.long_signal.new:
                self.kline.buy(size=SIZE)
            elif self.vortex.short_signal.new:
                self.kline.sell(size=SIZE)
        elif self.kline.position > 0 and self.vortex.short_signal.new:
            self.kline.sell(size=SIZE)
        elif self.kline.position < 0 and self.vortex.long_signal.new:
            self.kline.buy(size=SIZE)
''',
)

T["minibt_rsi_reversal"] = dict(
    folder="均值回归", name="RSI 超买超卖反转", cls="RSIStrategy",
    idea="RSI 从超卖区上穿阈值做多,从超买区下穿做空;震荡市反转思路。",
    source="shinnytech RSI 策略教程",
    category="basic", difficulty="beginner",
    code='''class RSI(BtIndicator):
    params = dict(RSI_PERIOD=6, OVERBOUGHT=65, OVERSOLD=35)
    isplot = dict(long_signal=False, short_signal=False)

    def next(self):
        rsi = self.close.rsi(self.params.RSI_PERIOD)
        long_signal = rsi.cross_up(self.params.OVERSOLD)
        short_signal = rsi.cross_down(self.params.OVERBOUGHT)
        return rsi, long_signal, short_signal


class RSIStrategy(Strategy):
    def __init__(self):
        self.kline = self.get_kline(DF, duration_seconds=86400)
        self.percent_commission = COMMISSION  # ⚠️ 必须放在 get_kline 之后
        self.rsi = RSI(self.kline)

    def next(self):
        if not self.kline.position:
            if self.rsi.long_signal.new:
                self.kline.buy(size=SIZE)
            elif self.rsi.short_signal.new:
                self.kline.sell(size=SIZE)
        elif self.kline.position > 0 and self.rsi.short_signal.new:
            self.kline.sell(size=SIZE)
        elif self.kline.position < 0 and self.rsi.long_signal.new:
            self.kline.buy(size=SIZE)
''',
)

T["minibt_cci_reversal"] = dict(
    folder="均值回归", name="CCI 极值回归", cls="CCIStrategy",
    idea="CCI 上穿 +100(强势启动)做多,下穿 -100(弱势破位)做空;极值突破式回归。",
    source="shinnytech CCI 策略教程(A股日线简化版)",
    category="basic", difficulty="beginner",
    code='''class CCI(BtIndicator):
    params = dict(CCI_PERIOD=10, CCI_UPPER=100, CCI_LOWER=-100)
    isplot = dict(long_signal=False, short_signal=False)

    def next(self):
        cci = self.close.cci(self.params.CCI_PERIOD)
        long_signal = cci.cross_up(self.params.CCI_UPPER)
        short_signal = cci.cross_down(self.params.CCI_LOWER)
        return cci, long_signal, short_signal


class CCIStrategy(Strategy):
    def __init__(self):
        self.kline = self.get_kline(DF, duration_seconds=86400)
        self.percent_commission = COMMISSION  # ⚠️ 必须放在 get_kline 之后
        self.cci = CCI(self.kline)

    def next(self):
        if not self.kline.position:
            if self.cci.long_signal.new:
                self.kline.buy(size=SIZE)
            elif self.cci.short_signal.new:
                self.kline.sell(size=SIZE)
        elif self.kline.position > 0 and self.cci.short_signal.new:
            self.kline.sell(size=SIZE)
        elif self.kline.position < 0 and self.cci.long_signal.new:
            self.kline.buy(size=SIZE)
''',
)

T["minibt_zscore"] = dict(
    folder="均值回归", name="Z-Score 统计套利式回归", cls="ZscoreStrategy",
    idea="价格 Z-Score 低于 -1.8 做多、高于 +1.8 做空,回到 ±0.4 区间或击穿 2.5 止损离场。",
    source="shinnytech Z-Score 策略教程",
    category="advanced", difficulty="advanced",
    code='''class Zscore(BtIndicator):
    params = dict(WINDOW=14, ENTRY=1.8, EXIT=0.4, STOP=2.5)
    isplot = dict(long_signal=False, short_signal=False,
                  exitlong_signal=False, exitshort_signal=False)

    def next(self):
        z = self.close.zscore(self.params.WINDOW)
        long_signal = z.cross_down(-self.params.ENTRY)
        short_signal = z.cross_up(self.params.ENTRY)
        back_in_band = (-self.params.EXIT <= z) & (z <= self.params.EXIT)
        stop_hit = (z >= self.params.STOP) | (z <= -self.params.STOP)
        exitlong_signal = back_in_band | (z >= self.params.STOP)
        exitshort_signal = back_in_band | (z <= -self.params.STOP)
        return z, long_signal, short_signal, exitlong_signal, exitshort_signal


class ZscoreStrategy(Strategy):
    def __init__(self):
        self.kline = self.get_kline(DF, duration_seconds=86400)
        self.percent_commission = COMMISSION  # ⚠️ 必须放在 get_kline 之后
        self.zscore = Zscore(self.kline)

    def next(self):
        if not self.kline.position:
            if self.zscore.long_signal.new:
                self.kline.buy(size=SIZE)
            elif self.zscore.short_signal.new:
                self.kline.sell(size=SIZE)
        elif self.kline.position > 0 and self.zscore.exitlong_signal.new:
            self.kline.sell(size=SIZE)
        elif self.kline.position < 0 and self.zscore.exitshort_signal.new:
            self.kline.buy(size=SIZE)
''',
)

T["minibt_keltner"] = dict(
    folder="通道突破", name="肯特纳通道突破", cls="KeltnerChannelStrategy",
    idea="EMA±动态 ATR 通道:价格突破上轨且短期 EMA 在上做多,跌破下轨做空;通道宽度随趋势强度自适应。",
    source="shinnytech Keltner Channel 策略教程",
    category="advanced", difficulty="advanced",
    code='''class KeltnerChannel(BtIndicator):
    params = dict(EMA_PERIOD=8, ATR_PERIOD=7, ATR_MULT=1.5,
                  SHORT_EMA_PERIOD=5, TREND=0.5)
    overlap = True

    def next(self):
        ema = self.close.ema(self.params.EMA_PERIOD)
        ema_short = self.close.ema(self.params.SHORT_EMA_PERIOD)
        trend_direction = self.zeros().ifs(ema_short > ema, 1., ema_short < ema, -1.)
        trend_strength = (ema_short - ema).abs() / self.close * 100.
        tr = self.true_range()
        atr = tr.sma(self.params.ATR_PERIOD)
        # 强趋势时收窄通道(乘数 ×0.8),弱趋势时用标准乘数
        dynamic_mult = self.full(self.params.ATR_MULT).mask(
            trend_strength > self.params.TREND, self.params.ATR_MULT * 0.8)
        upper_band = ema + dynamic_mult * atr
        lower_band = ema - dynamic_mult * atr
        long_signal = self.close > upper_band
        long_signal &= trend_direction > 0.
        short_signal = self.close < lower_band
        short_signal &= trend_direction < 0.
        return upper_band, lower_band, long_signal, short_signal


class KeltnerChannelStrategy(Strategy):
    def __init__(self):
        self.kline = self.get_kline(DF, duration_seconds=86400)
        self.percent_commission = COMMISSION  # ⚠️ 必须放在 get_kline 之后
        self.kc = KeltnerChannel(self.kline)

    def next(self):
        if not self.kline.position:
            if self.kc.long_signal.new:
                self.kline.buy(size=SIZE)
            elif self.kc.short_signal.new:
                self.kline.sell(size=SIZE)
        elif self.kline.position > 0 and self.kc.short_signal.new:
            self.kline.sell(size=SIZE)
        elif self.kline.position < 0 and self.kc.long_signal.new:
            self.kline.buy(size=SIZE)
''',
)

T["minibt_dual_thrust"] = dict(
    folder="通道突破", name="Dual Thrust 区间突破", cls="DualThrustStrategy",
    idea="近 N 日最高价差构成动态上下轨:收盘上穿买入轨做多,下穿卖出轨做空;经典日内策略的日线版。",
    source="shinnytech Dual Thrust 教程(A股日线化,纯 pandas 实现,不依赖 tqsdk)",
    category="advanced", difficulty="advanced",
    code='''class DualThrust(BtIndicator):
    params = dict(NDAY=14, K1=0.6, K2=0.6)
    isplot = dict(long_signal=False, short_signal=False)
    overlap = True

    def next(self):
        HH = self.high.rolling(self.params.NDAY).max().shift()
        HC = self.close.rolling(self.params.NDAY).max().shift()
        LC = self.close.rolling(self.params.NDAY).min().shift()
        LL = self.low.rolling(self.params.NDAY).min().shift()
        # range = max(HH-LC, HC-LL)
        rng = (HH - LC).clip(lower=HC - LL)
        buy_line = self.open + rng * self.params.K1   # 上轨
        sell_line = self.open - rng * self.params.K2  # 下轨
        long_signal = self.close.cross_up(buy_line)
        short_signal = self.close.cross_down(sell_line)
        return buy_line, sell_line, long_signal, short_signal


class DualThrustStrategy(Strategy):
    def __init__(self):
        self.kline = self.get_kline(DF, duration_seconds=86400)
        self.percent_commission = COMMISSION  # ⚠️ 必须放在 get_kline 之后
        self.dt = DualThrust(self.kline)

    def next(self):
        if not self.kline.position:
            if self.dt.long_signal.new:
                self.kline.buy(size=SIZE)
            elif self.dt.short_signal.new:
                self.kline.sell(size=SIZE)
        elif self.kline.position > 0 and self.dt.short_signal.new:
            self.kline.sell(size=SIZE)
        elif self.kline.position < 0 and self.dt.long_signal.new:
            self.kline.buy(size=SIZE)
''',
)

T["minibt_vpt"] = dict(
    folder="量价动量", name="VPT 量价趋势", cls="VPTStrategy",
    idea="量价趋势累计线(VPT)在均线上方且放量上涨做多,放量下跌做空。",
    source="shinnytech VPT 策略教程",
    category="basic", difficulty="intermediate",
    code='''class VPT(BtIndicator):
    params = dict(MA_PERIOD=14, VOLUME_MULT=1.5)
    isplot = dict(long_signal=False, short_signal=False)

    def next(self):
        size = len(self.volume)
        vpt = self.volume.copy(True)
        volume = self.volume.values
        close = self.close.values
        diff = self.close.diff().values
        for i in range(1, size):
            vpt[i] = vpt[i - 1] + volume[i] * diff[i] / close[i - 1]
        vpt_ma = vpt.sma(self.params.MA_PERIOD)
        avg_volume = self.volume.sma(self.params.MA_PERIOD)

        long_signal = vpt > vpt_ma
        long_signal &= self.close.diff() > 0.
        long_signal &= self.volume > (self.params.VOLUME_MULT * avg_volume)
        short_signal = vpt < vpt_ma
        short_signal &= self.close.diff() < 0.
        short_signal &= self.volume > (self.params.VOLUME_MULT * avg_volume)
        return vpt, vpt_ma, long_signal, short_signal


class VPTStrategy(Strategy):
    def __init__(self):
        self.kline = self.get_kline(DF, duration_seconds=86400)
        self.percent_commission = COMMISSION  # ⚠️ 必须放在 get_kline 之后
        self.vpt = VPT(self.kline)

    def next(self):
        if not self.kline.position:
            if self.vpt.long_signal.new:
                self.kline.buy(size=SIZE)
            elif self.vpt.short_signal.new:
                self.kline.sell(size=SIZE)
        elif self.kline.position > 0 and self.vpt.short_signal.new:
            self.kline.sell(size=SIZE)
        elif self.kline.position < 0 and self.vpt.long_signal.new:
            self.kline.buy(size=SIZE)
''',
)

for tid, spec in T.items():
    code = HEADER.format(folder=spec["folder"], name=spec["name"], idea=spec["idea"], source=spec["source"]) + spec["code"] + TAIL.format(cls=spec["cls"])
    (OUT / f"{tid}.py").write_text(code, encoding="utf-8")
    meta = {
        "id": tid,
        "name": f"minibt·{spec['name']}",
        "description": spec["idea"],
        "category": spec["category"],
        "difficulty": spec["difficulty"],
        "params": [],
        "execution_defaults": {},
        "live_defaults": {},
        "live_config_tips": [],
        "markets": ["a_share"],
        "dir": f"minibt策略/{spec['folder']}",
    }
    (OUT / f"{tid}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print("written", tid)
print("done:", len(T), "templates")
