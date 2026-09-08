# 导入所需的技术指标和工具函数
# from pandas_ta.core import atr, stdev, true_range, npNaN  # 导入ATR、标准差、真实波动范围等指标
from ..core import smoothrng, np  # 导入平滑随机数函数和部分应用函数
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
