"""QuantMind 训练器包（B4 由 docker/training/train.py 拆出）。

- registry：类型集合 + Trainer/MODEL_REGISTRY + 分派器 + DLAdapter。
- metrics：IC/RankIC/metrics 纯函数。
- trainers_gbdt：GBDT/sklearn 组（import 即注册）。
- trainers_dl：DL/TFT 组实现（注册由 registry 循环完成）。
"""
