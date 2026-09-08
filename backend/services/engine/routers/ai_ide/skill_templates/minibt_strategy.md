用途：minibt 风格策略（Strategy/next 事件式写法 + 指标即插即用 + 双引擎回测），运行在专用 minibt 运行时镜像（Python 3.12）。用户说"用 minibt 写策略 / 策略实验室 / minibt 回测"时启用本模板。

强制约束：
1) 数据读取必须使用 QuantDB 适配器，禁止 qlib/CSV/akshare：
   - from backend.shared.minibt_qdb import load_daily
   - df = load_daily('600036', '2023-01-01', '2025-06-30')  # 代码可传 600036 / SH600036 / 600036.SH
   - 数据为 A 股日线前复权（列 datetime/open/high/low/close/volume/amount）
2) 必须有 `if __name__ == "__main__":` 入口，运行时入口固定为：
   - bt = Bt(auto=False)
   - bt.addstrategy(MyStrategy)
   - result = run_and_report(bt, df)
   - 禁止 Bt(auto=True)、禁止 Bt().run() 空参形式
3) 回测执行必须是 `run_and_report(bt, df)`（内部 isplot=False, isreport=False），禁止手动 print 指标、禁止 isplot=True、禁止任何 PyQt/light_chart/bokeh 调用（服务端无 GUI）。
4) 手续费必须显式配置（默认为 0，会虚高收益），A 股口径：
   - self.percent_commission = 0.00025   # 佣金（双边）
   - self.stamp_tax = 0.0005             # 印花税（卖出），如引擎支持则设置
5) size 单位是"股"，A 股整手为 100 股/手；买入建议 size=100 的整数倍。
6) 已知撮合口径（写策略与解读结果时必须告知用户）：
   - 信号在当根 K 线收盘价成交（防未来函数安全）
   - 信号 .new 单次消费：持多时 sell() 只平多不开空，反手需先平后开两步
   - 引擎无 T+1、无涨跌停、无整手约束 → 结果与实盘存在系统性口径差
   - 持仓按保证金式记账，报告收益以 run_and_report 输出为准
7) 指标即插即用（KLine/IndSeries 属性链），可用库与示例：
   - 内置核心：self.kline.close.sma(20)、self.kline.atr(14)、self.kline.macd()、ma1.cross_up(ma2)
   - Pandas-Ta：self.kline.close.rsi(14)
   - TA-Lib：self.kline.close.MACD()（大写方法名）
   - BtInd：self.kline.close.btind.pmax3()
   - TradingView 移植：self.kline.tradingview.UT_Bot_Alerts()
   - FinTa：self.kline.finta.RSI()
   - 因子/配对：self.kline.factors、Pair(两列价格DataFrame).z_score()
   - 不可用：tulip、tqta、tqfunc（运行时镜像未装对应依赖）
8) 只输出完整可运行代码，不要 markdown 围栏、不要解释性输出。

默认最小骨架（以此为准改参数/指标/信号）：
```python
from minibt import Bt, Strategy
from backend.shared.minibt_qdb import load_daily
from backend.shared.minibt_result import run_and_report


class MACross(Strategy):
    params = dict(l1=10, l2=20)

    def __init__(self):
        self.kline = self.get_kline(load_daily('600036', '2023-01-01', '2025-06-30'), duration_seconds=86400)
        self.percent_commission = 0.00025   # ⚠️ 必须放在 get_kline 之后(setter 只作用于已注册合约)
        self.ma1 = self.kline.close.sma(self.params.l1)
        self.ma2 = self.kline.close.sma(self.params.l2)
        self.long_signal = self.ma1.cross_up(self.ma2)
        self.short_signal = self.ma1.cross_down(self.ma2)

    def next(self):
        if not self.kline.position:
            if self.long_signal.new:
                self.kline.buy(size=1000)
            elif self.short_signal.new:
                self.kline.sell(size=1000)
        elif self.kline.position > 0 and self.short_signal.new:
            self.kline.sell(size=1000)
        elif self.kline.position < 0 and self.long_signal.new:
            self.kline.buy(size=1000)


if __name__ == "__main__":
    bt = Bt(auto=False)
    bt.addstrategy(MACross)
    run_and_report(bt, load_daily('600036', '2023-01-01', '2025-06-30'))
```

注意：df 加载两次仅为骨架清晰，实际生成时用变量复用；多股票可多次 get_kline。
