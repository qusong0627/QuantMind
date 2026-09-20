"""**已废弃的转发空壳** —— 请直接 import 权威实现。

权威路径是 ``backend/services/simulation/services/local_market_data.py``
（``compute_limits`` / ``limit_pct`` / ``LIMIT_TOLERANCE`` 的唯一事实源）。

本文件属于 ``backend/services/trade/simulation/`` 这棵**并行旧树**，整个目录只剩
``import *`` 空壳（同目录的 ``corporate_action_service.py`` 一样）。2026-09-20
最后 5 个消费者（``scripts/backtest_l2_{year,top20,optimized}.py``、
``backtest_news_{sentiment,optimized}.py``）已改为直接引用权威路径，本文件目前
**无任何导入方**。

保留而非删除的原因：它是 git 跟踪的历史路径，可能有仓库外的脚本或旧分支仍在
引用；``import *`` 转发的是同一批对象，不存在口径分叉。但**新代码不要走这里** ——
它连 ``LIMIT_TOLERANCE`` 这类新增常量都要靠 ``import *`` 顺带带出，一旦权威模块
日后定义 ``__all__`` 就会静默少名字。
"""

from backend.services.simulation.services.local_market_data import *  # noqa: F401,F403,E402
