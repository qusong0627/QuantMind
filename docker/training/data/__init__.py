"""训练数据包（P2 由 train.py 拆出）。

- splits: T+1 执行口径常量、数据集切分、数组化（含截面预处理入口）
- loading: 本地 parquet / QuantDB 直读、标签构建
- factor_selection: IC/ICIR 筛选 + 自适应回填

与 diagnostics/model_trainers 同级，训练容器三通道同步。
"""
