"""机构级跨市场因子回测引擎（T-FB-01…21，「因子挖掘 → 回测中心」）。

模块划分：
- ``compat.py``  静态兼容性分类（列 token ⊆ 市场列集）
- ``profiles.py`` 市场档案注册表（provider/universe/基准/费率/窗口，单源）
- ``ic.py``      日度 IC 序列与组合曲线（与挖掘/现行回测逐字同口径）
- ``engine.py``  求值核心（因子 × 市场 → 指标 + 序列 + 终态）
- ``store.py``   台账/序列持久化（沿 run_id 精确收口纪律）
- ``router.py``  ``/api/v1/factor-backtest`` 端点组
"""
