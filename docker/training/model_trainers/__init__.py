"""QuantMind 训练器包（B4 由 docker/training/train.py 拆出）。

- registry：类型集合 + Trainer/MODEL_REGISTRY + 分派器 + DLAdapter。
- metrics：IC/RankIC/metrics 纯函数。
- trainers_gbdt：GBDT/sklearn 组（import 即完成 6 条注册）。
- trainers_dl：DL/TFT 组实现（注册由 registry 循环完成）。

聚合导入保证：任何 `import model_trainers[.x]` 都触发完整注册
（GBDT 装饰器 + DL 循环），MODEL_REGISTRY 恒为全集。
"""

from model_trainers import metrics, registry, trainers_dl, trainers_gbdt

__all__ = ["metrics", "registry", "trainers_dl", "trainers_gbdt"]
