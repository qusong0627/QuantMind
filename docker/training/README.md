# docker/training

用途：训练容器内的模型训练入口脚本与运行时辅助文件。

## 说明
- 统一运行镜像为 `quantmind-ml-runtime:latest`。
- `train.py` 会在用户提交特征的基础上自动补齐 6 个基础特征：`mom_ret_1d`、`mom_ret_5d`、`mom_ret_20d`、`liq_volume`、`liq_amount`、`liq_turnover_os`。
- `metadata.json` 现在会同时记录三层口径：
  - `requested_feature_count/requested_features`：前端提交的特征
  - `auto_appended_feature_count/auto_appended_features`：训练脚本自动补齐的基础特征
  - `feature_count/features/feature_columns`：最终实际入模特征
- 训练结束后默认生成 `shap_summary.csv`，使用 LightGBM 原生 `pred_contrib=True` 计算 SHAP 汇总贡献度；默认读取验证集、采样 30000 行，并在 `metadata.json.shap` 中记录状态、样本数、耗时和错误信息。SHAP 失败不会阻断训练完成。
- 前端训练页会据此展示“提交特征数 / 自动补充特征 / 实际入模特征数”，便于排查维度不一致问题。
