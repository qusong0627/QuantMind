# 导入所需的技术指标和工具函数
# from pandas_ta.core import atr, stdev, true_range, npNaN  # 导入ATR、标准差、真实波动范围等指标
import numpy as np  # 数值计算
from ..indicators import Stop, Literal  # 导入基础止损类


class CAC40(Stop):
    """CAC40止损策略类，继承自基础Stop类"""

    def __init__(self, trailstart: float = 3., basepercent: float = 0.094, stepsize: float = 3.,
                 percentinc: float = 0.102, roundto: float = -0.5, pricedistance: float = 5.) -> None:
        # 初始化止损策略参数
        self.trailstart = trailstart  # 开始跟踪利润的起点（例如：3个点）
        self.basepercent = basepercent  # 基础利润保留百分比（例如：0.094表示9.4%）
        self.stepsize = stepsize  # 每增加一定百分比的步长（例如：3个点为一个单位）
        self.percentinc = percentinc  # 每步长对应的百分比增量（例如：0.102表示10.2%）
        self.roundto = roundto  # 四舍五入调整值（-0.5向下取整，+0.4向上取整）
        self.pricedistance = pricedistance  # 与当前价格的最小距离
        self.y1 = 0.  # 多单跟踪变量
        self.y2 = 0.  # 空单跟踪变量
        self.ProfitPerCent1 = basepercent  # 多单利润百分比
        self.ProfitPerCent2 = basepercent  # 空单利润百分比

    def long(self) -> None:
        """多单止损计算逻辑"""
        low = self.low[-1]  # 获取最新低点
        per_target_price = self.target_price[-2]
        # 若当前低点高于交易价格加上y1倍价格跳动
        if low > (self.kline.open_price + self.y1 * self.price_tick):
            # 计算价格变动点数（以价格跳动为单位）
            x1 = (low - self.kline.open_price) / self.price_tick
            # 当变动点数超过跟踪起点时
            if x1 >= self.trailstart:
                Diff1 = x1 - self.trailstart  # 超出起点的点数
                # 计算步数（取最大值0，避免负数）
                Chunks1 = max(0., Diff1 / self.stepsize + self.roundto)
                # 计算当前利润百分比（基础百分比*（1+步数*增量百分比））
                _ProfitPerCent = self.basepercent * \
                    (1 + Chunks1 * self.percentinc)
                # 更新多单利润百分比（不超过100%，不低于当前值）
                self.ProfitPerCent1 = max(
                    self.ProfitPerCent1, min(100, _ProfitPerCent))
                # 更新y1（取当前计算值与历史值的最大值）
                self.y1 = max(x1 * self.ProfitPerCent1, self.y1)
                # 当y1有效时，计算目标价格
                if self.y1 > 0.:
                    self.target_price.new = self.kline.open_price + \
                        self.y1 * self.price_tick + self.pricedistance
                    return
        self.target_price.new = per_target_price

    def short(self):
        """空单止损计算逻辑"""
        high = self.high[-1]  # 获取最新高点
        per_target_price = self.target_price[-2]
        # 若当前高点低于交易价格减去y2倍价格跳动
        if high < (self.kline.open_price - self.y2 * self.price_tick):
            # 计算价格变动点数（以价格跳动为单位）
            x2 = (self.kline.open_price - high) / self.price_tick
            # 当变动点数超过跟踪起点时
            if x2 >= self.trailstart:
                Diff2 = x2 - self.trailstart  # 超出起点的点数
                # 计算步数（取最大值0，避免负数）
                Chunks2 = max(0., Diff2 / self.stepsize + self.roundto)
                # 计算当前利润百分比
                _ProfitPerCent = self.basepercent * \
                    (1 + Chunks2 * self.percentinc)
                # 更新空单利润百分比
                self.ProfitPerCent2 = max(
                    self.ProfitPerCent2, min(100, _ProfitPerCent))
                # 更新y2
                self.y2 = max(x2 * self.ProfitPerCent2, self.y2)
                # 当y2有效时，计算目标价格
                if self.y2 > 0.:
                    self.target_price.new = self.kline.open_price - \
                        self.y2 * self.price_tick - self.pricedistance
                    return
        self.target_price.new = per_target_price


class SegmentationTracking(Stop):
    """## 分段跟踪停止策略
    根据价格波动幅度分段调整止损位置，结合ATR、标准差等指标

    ## Args:
        length (int, optional): atr指标长度. Defaults to 14.
        mult (float, optional): atr指标乘数. Defaults to 1..
        method (str, optional): 初始价选用指标,可选["atr","std","smoothrng"]. Defaults to "atr".
        acceleration (list[float], optional): 分段乘数. Defaults to [0.382, 0.5, 0.618, 1.0].
        min_distance (float, optional): 最小距离. Defaults to 0..
    """

    def __init__(self, length: int = 14, mult: float = 1., method: Literal["atr", "std", "smoothrng"] = "atr",
                 acceleration: list[float] = [0.382, 0.5, 0.618, 1.0], min_distance: float = 0.) -> None:
        self.length = length  # 指标计算周期长度
        self.mult = mult  # 指标乘数
        self.method = method  # 选用的指标方法
        self.acceleration = acceleration  # 分段加速度（调整系数）
        # 计算最小距离（基于加速度的第一个值）
        self.min_distance = max(min(1., min_distance), 0.) * acceleration[0]
        # 根据方法选择对应的指标函数
        if self.method == 'atr':
            self._isatr = True
            self._atr = self.kline.atr(
                self.length)
        elif self.method == 'std':
            self._atr = self.kline.close.stdev(
                self.length)
        else:
            self._atr = self.kline.btind.smoothrng(
                self.length)

    def long(self):
        """多单分段跟踪止损计算"""
        _atr = self._atr[-1]
        if not np.isnan(_atr):
            pre_stop_price = self.stop_price[-2]
            if np.isnan(pre_stop_price):
                stop_price = self.low[-1] - self.mult * _atr
            else:
                preprice = self.close[-2]  # 前收盘价
                lastprice = self.close[-1]  # 最新收盘价
                stop_price = pre_stop_price  # 上次止损价
                range_ = abs(preprice - lastprice)  # 价格波动范围
                
                # 若最新收盘价高于前收盘价（价格上涨）
                if lastprice > preprice:
                    diff_price = lastprice - self.kline.open_price  # 与开仓价的差价
                    _atr *= self.mult  # 应用乘数

                    # 根据不同条件调整止损价
                    if stop_price < self.kline.open_price:
                        # 止损价低于开仓价时，用最小加速度调整
                        stop_price += self.acceleration[0] * range_
                    else:
                        # 根据差价与ATR的关系，使用不同加速度
                        if diff_price < _atr:
                            stop_price += self.acceleration[1] * range_
                        elif diff_price < 2. * _atr:
                            stop_price += self.acceleration[2] * range_
                        else:
                            stop_price += self.acceleration[3] * range_
                else:
                    # 价格未上涨时，应用最小距离调整
                    if self.min_distance:
                        stop_price += self.min_distance * range_
            self.stop_price.new = stop_price  # 更新止损价

    def short(self):
        """空单分段跟踪止损计算"""
        _atr = self._atr[-1]
        if not np.isnan(_atr):
            pre_stop_price = self.stop_price[-2]
            # 若为初始止损设置
            if np.isnan(pre_stop_price):
                stop_price = self.high[-1] + self.mult * _atr
            else:
                preprice = self.close[-2]  # 前收盘价
                lastprice = self.close[-1]  # 最新收盘价
                stop_price = pre_stop_price  # 上次止损价
                range_ = abs(preprice - lastprice)  # 价格波动范围

                # 若前收盘价高于最新收盘价（价格下跌）
                if preprice > lastprice:
                    diff_price = self.kline.open_price - lastprice  # 与开仓价的差价
                    _atr *= self.mult  # 应用乘数
                    

                    # 根据不同条件调整止损价
                    if stop_price > self.kline.open_price:
                        # 止损价高于开仓价时，用最小加速度调整
                        stop_price -= self.acceleration[0] * range_
                    else:
                        # 根据差价与ATR的关系，使用不同加速度
                        if diff_price < _atr:
                            stop_price -= self.acceleration[1] * range_
                        elif diff_price < 2. * _atr:
                            stop_price -= self.acceleration[2] * range_
                        else:
                            stop_price -= self.acceleration[3] * range_
                else:
                    # 价格未下跌时，应用最小距离调整
                    if self.min_distance:
                        stop_price += self.min_distance * range_

            self.stop_price.new = stop_price  # 更新止损价


class TimeSegmentationTracking(Stop):
    """## 时间分段跟踪停止策略
    根据持仓天数分段调整止损位置，结合时间因素动态调整

    ## Args:
        length (int, optional): atr指标长度. Defaults to 14.
        mult (float, optional): atr指标乘数. Defaults to 1..
        method (str, optional): 初始价选用指标,可选["atr","std","smoothrng"]. Defaults to "atr".
        days (list[int], optional): 持仓天数列表. Defaults to [3, 8, 13].
        acceleration (list[float], optional): 分段乘数. Defaults to [0.382, 0.5, 0.618, 1.0].
        min_distance (float, optional): 最小距离. Defaults to 1..
    """

    def __init__(self, length: int = 14, mult: float = 1., method: Literal["atr", "std", "smoothrng"] = "atr",
                 days: list[int] = [3, 8, 13], acceleration: list[float] = [0.382, 0.5, 0.618, 1.0],
                 min_distance: float = 1.) -> None:
        self.count = 0  # 持仓天数计数器
        self.length = length  # 指标周期长度
        self.mult = mult  # 指标乘数
        self.days = days  # 分段天数节点
        self.acceleration = acceleration  # 分段加速度
        # 计算最小距离
        self.min_distance = max(min(1., min_distance), 0.) * acceleration[0]
        # 根据方法选择对应的指标函数
        if method == 'atr':
            self._isatr = True
            self._atr = self.kline.atr(
                self.length)
        elif method == 'std':
            self._atr = self.kline.close.stdev(
                self.length)
        else:
            self._atr = self.kline.close.btind.smoothrng(
                self.length)

    def long(self):
        """多单时间分段跟踪止损计算"""
        _atr = self._atr[-1]
        if not np.isnan(_atr):
            pre_stop_price = self.stop_price[-2]
            # 若为初始止损设置
            if np.isnan(pre_stop_price):
                stop_price = self.low[-1] - self.mult * _atr
                self.count = 0  # 重置持仓天数
            else:
                self.count += 1  # 持仓天数+1
                stop_price = pre_stop_price  # 上次止损价
                range_ = self.close[-1] - self.close[-2]  # 价格波动范围

                # 若价格上涨
                if range_ > 0.:
                    # 根据持仓天数选择不同加速度调整止损价
                    if self.count <= self.days[0]:
                        stop_price += self.acceleration[0] * range_
                    else:
                        if self.count <= self.days[1]:
                            stop_price += self.acceleration[1] * range_
                        elif self.count <= self.days[2]:
                            stop_price += self.acceleration[2] * range_
                        else:
                            stop_price += self.acceleration[3] * range_
                else:
                    # 价格未上涨，按最小距离调整
                    stop_price += self.min_distance * abs(range_)
            self.stop_price.new = stop_price

    def short(self):
        """空单时间分段跟踪止损计算"""
        _atr = self._atr[-1]
        if not np.isnan(_atr):
            pre_stop_price = self.stop_price[-2]
            # 若为初始止损设置
            if np.isnan(pre_stop_price):
                stop_price = self.high[-1] + self.mult * _atr
                self.count = 0  # 重置持仓天数
            else:
                self.count += 1  # 持仓天数+1
                stop_price = pre_stop_price  # 上次止损价
                range_ = self.close[-2] - self.close[-1]  # 价格波动范围

                # 若价格下跌
                if range_ > 0.:
                    # 根据持仓天数选择不同加速度调整止损价
                    if self.count <= self.days[0]:
                        stop_price -= self.acceleration[0] * range_
                    else:
                        if self.count <= self.days[1]:
                            stop_price -= self.acceleration[1] * range_
                        elif self.count <= self.days[2]:
                            stop_price -= self.acceleration[2] * range_
                        else:
                            stop_price -= self.acceleration[3] * range_
                else:
                    # 价格未下跌，按最小距离调整
                    stop_price -= self.min_distance * abs(range_)
            self.stop_price.new = stop_price


class TrailingStopLoss(Stop):
    """跟踪目标值策略，当价格向有利方向移动一定幅度后，调整目标值"""

    def __init__(self, trailingstart=3., trailingstep=3.) -> None:
        self.trailingstart = trailingstart  # 开始跟踪的初始幅度
        self.trailingstep = trailingstep  # 每次调整的步长

    def long(self):
        """多单跟踪止损计算"""
        per_target_price = self.target_price[-2]
        if np.isnan(per_target_price):
            # 初始化目标价
            if self.close[-1] - self.kline.open_price >= self.trailingstart * self.price_tick:
                self.target_price.new = self.close[-1] + \
                    self.trailingstep * self.price_tick
        else:
            # 若已有目标价，当价格超过目标价一定步长时，上调目标价
            new_target_price = self.close[-1] + \
                self.trailingstep * self.price_tick
            self.target_price.new = min(new_target_price, per_target_price)

    def short(self):
        """空单跟踪止损计算"""
        per_target_price = self.target_price[-2]
        if np.isnan(per_target_price):
            # 初始化目标价
            if self.kline.open_price-self.close[-1] >= self.trailingstart * self.price_tick:
                self.target_price.new = self.close[-1] - \
                    self.trailingstep * self.price_tick
        else:
            # 若已有目标价，当价格超过目标价一定步长时，上调目标价
            new_target_price = self.close[-1] - \
                self.trailingstep * self.price_tick
            self.target_price.new = max(new_target_price, per_target_price)


class AnotherATRTrailingStop(Stop):
    """另一种ATR跟踪止损策略
    来源: https://www.prorealcode.com/prorealtime-indicators/another-atr-trailing-stop/
    （注：原代码未实现，此处省略）
    """
    ...


class FixedStop(Stop):
    """## 固定点数停止策略
    同时设置固定止损价和固定目标价

    ## Args:
        stop_distance (float, optional): 固定止损价距离（最小变动单位）. Defaults to 5..
        target_distance (float, optional): 固定目标价距离（最小变动单位）. Defaults to 5..
    """

    def __init__(self, stop_distance: float = 5., target_distance: float = 5.) -> None:
        self.stop_distance = stop_distance  # 止损距离（最小变动单位）
        self.target_distance = target_distance  # 目标距离（最小变动单位）

    def long(self):
        """多单固定止损和目标价计算"""
        per_target_price = self.target_price[-2]
        per_stop_price = self.stop_price[-2]
        if np.isnan(per_stop_price):
            # 初始止损价 = 最新低点 - 止损距离*价格跳动
            self.stop_price.new = self.low[-1] - \
                self.stop_distance * self.price_tick
        else:
            self.stop_price.new = per_stop_price
        if np.isnan(per_target_price):
            # 初始目标价 = 最新高点 + 目标距离*价格跳动
            self.target_price.new = self.high[-1] + \
                self.target_distance * self.price_tick
        else:
            self.target_price.new = per_target_price

    def short(self):
        """空单固定止损和目标价计算"""
        per_target_price = self.target_price[-2]
        per_stop_price = self.stop_price[-2]
        if np.isnan(per_stop_price):
            # 初始止损价 = 最新低点 - 止损距离*价格跳动
            self.stop_price.new = self.high[-1] + \
                self.stop_distance * self.price_tick
        else:
            self.stop_price.new = per_stop_price
        if np.isnan(per_target_price):
            # 初始目标价 = 最新高点 + 目标距离*价格跳动
            self.target_price.new = self.low[-1] - \
                self.target_distance * self.price_tick
        else:
            self.target_price.new = per_target_price


class FSStop(Stop):
    """Fixed Stop Stop
    ## 固定点数止损停止策略
    仅设置固定止损价，不设置目标价

    ## Args:
        stop_distance (float, optional): 固定止损价距离（点数）. Defaults to 5..
    """

    def __init__(self, stop_distance: float = 5.) -> None:
        self.stop_distance = stop_distance  # 止损距离（点数）

    def long(self):
        """多单固定止损计算"""
        per_stop_price = self.stop_price[-2]
        if np.isnan(per_stop_price):
            # 初始止损价 = 最新低点 - 止损距离*价格跳动
            self.stop_price.new = self.low[-1] - \
                self.stop_distance * self.price_tick
        else:
            self.stop_price.new = per_stop_price

    def short(self):
        """空单固定止损计算"""
        per_stop_price = self.stop_price[-2]
        if np.isnan(per_stop_price):
            # 初始止损价 = 最新低点 - 止损距离*价格跳动
            self.stop_price.new = self.high[-1] + \
                self.stop_distance * self.price_tick
        else:
            self.stop_price.new = per_stop_price


class FTStop(Stop):
    """Fixed Target Stop
    ## 固定点数目标停止策略
    仅设置固定目标价，不设置止损价

    ## Args:
        target_distance (float, optional): 固定目标价距离（点数）. Defaults to 5..
    """

    def __init__(self, target_distance: float = 5.) -> None:
        self.target_distance = target_distance  # 目标距离（点数）

    def long(self):
        """多单固定目标价计算"""
        per_target_price = self.target_price[-2]
        if np.isnan(per_target_price):
            # 初始目标价 = 最新高点 + 目标距离*价格跳动
            self.target_price.new = self.high[-1] + \
                self.target_distance * self.price_tick
        else:
            self.target_price.new = per_target_price

    def short(self):
        """空单固定目标价计算"""
        per_target_price = self.target_price[-2]
        if np.isnan(per_target_price):
            # 初始目标价 = 最新高点 + 目标距离*价格跳动
            self.target_price.new = self.low[-1] - \
                self.target_distance * self.price_tick
        else:
            self.target_price.new = per_target_price


class ThreeCommasStop(Stop):
    """## 3Commas Bot 止损止盈（摆动点 + ATR 跟踪止损 + 盈亏比止盈）

    复刻 TradingView 脚本 *3Commas Bot* (Bjorgum) 的风控部分:
    https://cn.tradingview.com/script/MvlwAzSg-3Commas-Bot/

    本停止类只负责**开仓之后**的 ``stop_price`` / ``target_price`` 计算,
    不包含原脚本的均线交叉入场信号(入场信号请在自己的策略里生成)。

    ### 计算规则(与原脚本一致)
    开仓首根 bar(用 ``self.kline.open_price`` 作为入场价):

        # 多单
        stop   = lowestLow(swing_lookback)   - ATR(atr_len) * risk_m
        risk   = entry - stop
        target = entry + rnr * risk              # use_limit=False 时不设止盈
        trigger = entry + rnr * risk * rr_exit

        # 空单(方向相反)
        stop   = highestHigh(swing_lookback) + ATR(atr_len) * risk_m
        risk   = stop - entry
        target = entry - rnr * risk
        trigger = entry - rnr * risk * rr_exit

    持仓期间:

        - ``trail_stop=True`` 时按 ATR 跟踪止损: 多单只上移、空单只下移,
          即"跟踪止盈/锁住利润", 止损线不是水平线;
        - ``trail_source`` 决定跟踪基准:
          ``"High/Low"``=摆动高/低点(默认), ``"Close"``/``"Open"``=前一根收盘/开盘;
        - ``rr_exit>0`` 时价格须先触及 ``trigger`` 才开始跟踪; ``rr_exit=0`` 则开仓即跟踪;
        - ``target`` 开仓后固定不变(水平线)。

    ## Args:
        swing_lookback (int): 摆动高低点回看长度. Defaults to 5.
        atr_len (int): ATR 周期. Defaults to 14.
        risk_m (float): 初始止损的 ATR 乘数(Risk Adjustment). Defaults to 1.0.
        rnr (float): 盈亏比 Reward:Risk. Defaults to 1.0.
        use_limit (bool): 是否设置止盈目标. Defaults to True.
        trail_stop (bool): 是否启用 ATR 跟踪止损. Defaults to True.
        trail_stop_size (float): 跟踪止损的 ATR 乘数. Defaults to 1.0.
        trail_source (str): 跟踪基准, 可选 ["High/Low", "Close", "Open"]. Defaults to "High/Low".
        rr_exit (float): 触发跟踪所需的盈利比例(0~1, 相对目标距离). Defaults to 0.0.

    ### Examples
    ```python
    self.data.buy(stop=BtStop.ThreeCommasStop)
    self.data.sell(stop=BtStop.ThreeCommasStop(
        swing_lookback=10, atr_len=14, risk_m=1.5,
        rnr=2.0, trail_stop_size=1.5, rr_exit=0.5))
    ```
    """

    def __init__(self, swing_lookback: int = 5, atr_len: int = 14,
                 risk_m: float = 1., rnr: float = 1., use_limit: bool = True,
                 trail_stop: bool = True, trail_stop_size: float = 1.,
                 trail_source: Literal["High/Low", "Close", "Open"] = "High/Low",
                 rr_exit: float = 0.) -> None:
        self.swing_lookback = max(1, int(swing_lookback))  # 摆动点回看长度
        self.atr_len = max(1, int(atr_len))  # ATR 周期
        self.risk_m = float(risk_m)  # 初始止损 ATR 乘数
        self.rnr = float(rnr)  # 盈亏比
        self.use_limit = bool(use_limit)  # 是否设止盈
        self.trail_stop = bool(trail_stop)  # 是否启用跟踪止损
        self.trail_stop_size = float(trail_stop_size)  # 跟踪止损 ATR 乘数
        self.trail_source = trail_source  # 跟踪基准
        self.rr_exit = float(rr_exit)  # 触发跟踪的盈利比例
        # 与持仓无关的指标, 可全量预计算(框架会随数据滚动更新)
        self._atr = self.kline.atr(self.atr_len)
        self._lowest_low = self.low.rolling(self.swing_lookback).min()
        self._highest_high = self.high.rolling(self.swing_lookback).max()
        # 持仓状态(每次开仓首根 bar 重置)
        self._look_for_exit = False  # 是否已启动跟踪
        self._trigger = np.nan  # 跟踪触发线

    def _reset(self) -> None:
        """开仓首根 bar 重置持仓状态"""
        self._look_for_exit = self.rr_exit == 0.
        self._trigger = np.nan

    def long(self) -> None:
        """多单止损/止盈计算"""
        atr = self._atr[-1]
        lowest_low = self._lowest_low[-1]
        if np.isnan(self.stop_price[-2]):  # 开仓首根 bar
            self._reset()
            if np.isnan(atr) or np.isnan(lowest_low):
                self.stop_price.new = np.nan
                self.target_price.new = np.nan
                return
            stop = lowest_low - atr * self.risk_m
            entry = self.kline.open_price
            risk = entry - stop
            if self.use_limit:
                target = entry + self.rnr * risk
                self._trigger = entry + self.rnr * risk * self.rr_exit
            else:
                target = np.nan
        else:
            stop = self.stop_price[-2]
            target = self.target_price[-2]

        # rr_exit: 价格走到目标的 rr_exit 比例后才启动跟踪
        if self.trail_stop and not self._look_for_exit and not np.isnan(self._trigger):
            if self.high[-1] >= self._trigger:
                self._look_for_exit = True

        # ATR 跟踪止损(只上移, 锁定利润)
        if self.trail_stop and self._look_for_exit and not np.isnan(atr):
            if self.trail_source == "Close":
                src = self.close[-2]
            elif self.trail_source == "Open":
                src = self.open[-2]
            else:  # "High/Low": 摆动低点
                src = lowest_low
            if not np.isnan(src):
                trail = src - atr * self.trail_stop_size
                if np.isnan(stop) or trail > stop:
                    stop = trail

        self.stop_price.new = stop
        self.target_price.new = target

    def short(self) -> None:
        """空单止损/止盈计算"""
        atr = self._atr[-1]
        highest_high = self._highest_high[-1]
        if np.isnan(self.stop_price[-2]):  # 开仓首根 bar
            self._reset()
            if np.isnan(atr) or np.isnan(highest_high):
                self.stop_price.new = np.nan
                self.target_price.new = np.nan
                return
            stop = highest_high + atr * self.risk_m
            entry = self.kline.open_price
            risk = stop - entry
            if self.use_limit:
                target = entry - self.rnr * risk
                self._trigger = entry - self.rnr * risk * self.rr_exit
            else:
                target = np.nan
        else:
            stop = self.stop_price[-2]
            target = self.target_price[-2]

        # rr_exit: 价格走到目标的 rr_exit 比例后才启动跟踪
        if self.trail_stop and not self._look_for_exit and not np.isnan(self._trigger):
            if self.low[-1] <= self._trigger:
                self._look_for_exit = True

        # ATR 跟踪止损(只下移, 锁定利润)
        if self.trail_stop and self._look_for_exit and not np.isnan(atr):
            if self.trail_source == "Close":
                src = self.close[-2]
            elif self.trail_source == "Open":
                src = self.open[-2]
            else:  # "High/Low": 摆动高点
                src = highest_high
            if not np.isnan(src):
                trail = src + atr * self.trail_stop_size
                if np.isnan(stop) or trail < stop:
                    stop = trail

        self.stop_price.new = stop
        self.target_price.new = target
