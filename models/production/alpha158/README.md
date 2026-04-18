# 19_T3_Alpha158_Base

简介
- 本模型目录为“19号”模型（已重命名为 T+3）：基础 Alpha158 特征模型（Alpha158 基线实现）。
- 目标：提供一个可复现的轻量级基线，用于快速生成因子预测与后续实验对比（T+3 回报预测）。

目录结构（计划）
- `config.yaml`：模型与数据配置。
- `prepare_data.py`：数据准备与特征加载脚本。
- `train.py`：训练脚本。
- `eval.py`：评估脚本（可复用仓库现有评估脚本）。
- `metadata.json`：模型元数据。
- `model_checkpoint.bin`：训练好的模型权重（占位）。

数据来源
- Qlib 格式数据目录：/root/qlib-main/qlib_data/Alpha158_2026

使用示例
1. 编辑 `config.yaml`，确认 `qlib_init.provider_uri` 指向上面路径。
2. 运行 `python prepare_data.py`（如果需要预处理）。
3. 运行 `python train.py` 训练模型。

推理运行时
- `inference.py` 现对线上同步推理主动收敛并行度，避免单次请求在 Qlib 特征构建阶段 fork 大量子进程导致 API 超时或容器被 OOM/Kill。
- 默认推理运行参数：
  - `ALPHA158_INFERENCE_THREADS=4`
  - `ALPHA158_INFERENCE_KERNELS=1`
  - `ALPHA158_INFERENCE_JOBLIB_BACKEND=threading`
- 推理阶段会覆盖训练配置中的高并行参数，尤其不会继续沿用 `config.yaml` 内偏向离线训练的 `num_threads=32`。

创建时间：2026-03-22
作者：自动化脚手架
