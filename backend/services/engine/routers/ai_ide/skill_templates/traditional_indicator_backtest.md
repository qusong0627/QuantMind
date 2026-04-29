用途：传统技术指标（MACD/KDJ/RSI/BOLL）脚本，必须”直接运行并输出收益指标”。

强制约束：
1) 数据读取必须使用 qlib：
   - qlib.init(provider_uri=”/app/db/qlib_data”, region=”cn”)
   - D.features(...)
2) 禁止 CSV 占位路径和 AAPL 默认代码。
3) 必须包含 main() 入口。
4) 必须输出：累计收益、年化收益、最大回撤、夏普比率、交易次数。
5) 回测计算必须使用 position=signal.shift(1) 防未来函数。
6) 默认参数：
   - initial_capital=1000000
   - commission=0.0003
   - slippage=0.0005
7) 默认股票池 top100（可配置），禁止无边界遍历全市场。
8) 优先一次性向量化计算，避免在 on_bar 内重复全窗口指标计算。

建议骨架：
- init_qlib()
- get_data()
- calculate_indicator()
- generate_signals()
- backtest()
- main()

默认最小导入集（建议按此生成）：
```python
import os
import numpy as np
import pandas as pd
import qlib
from qlib.data import D
from qlib.constant import REG_CN
```

禁止导入（常见误导）：
- from qlib.contrib.evaluate import backtest
- from qlib.backtest import executor, analyzer
- from qlib.workflow import R
- 任何未使用导入

输出指标格式（必须包含）：
```python
print(f”累计收益: {total_return:.2%}”)
print(f”年化收益: {annual_return:.2%}”)
print(f”最大回撤: {max_drawdown:.2%}”)
print(f”夏普比率: {sharpe_ratio:.2f}”)
print(f”交易次数: {trade_count}”)
```
