# Alpha158 训练方案 (统一复权模式)

本项目采用 **“原始价 + 复权因子”** 的标准 Qlib 模式进行 Alpha158 因子训练与推理，确保回测与实盘的一致性。

## 1. 基础数据维度 (Raw Data)

训练与推理统一使用以下 6 个核心字段：

| 字段名 | 说明 | 备注 |
| :--- | :--- | :--- |
| `$open` | 原始开盘价 | 不复权 |
| `$high` | 原始最高价 | 不复权 |
| `$low` | 原始最低价 | 不复权 |
| `$close` | 原始收盘价 | 不复权 |
| `$volume` | 成交股数 | 原始量 |
| `$factor` | 复权因子 | 用于计算连续价格序列 |

> [!TIP]
> 推荐同时准备 `$vwap` (成交均价) 以支持更完整的 Alpha158 算子计算。

## 2. 特征工程配置 (Alpha158)

在 Qlib 配置文件或 Python 脚本中，`DataHandler` 的配置如下：

```yaml
data_handler_config: &data_handler_config
    start_time: 2016-01-01
    end_time: 2025-12-31
    fit_start_time: 2016-01-01
    fit_end_time: 2023-12-31
    instruments: *instruments_config
    infer_processors:
        - class: FilterCol
          kwargs:
              fields_group: feature
              col_list: ["REB30", "REB60", "WVMA5"] # 可选：过滤极值或高相关性因子
        - class: RobustZScoreNorm
          kwargs:
              fields_group: feature
              clip_outlier: true
        - class: Fillna
          kwargs:
              fields_group: feature
    learn_processors:
        - class: DropnaLabel
        - class: CSRLabelNormalize
          kwargs:
              fields_group: label
    label: ["Ref($close, -2) / Ref($close, -1) - 1"] # 预测次日收益率
```

## 3. 训练与验证逻辑

*   **模型选择**：LightGBM (GBDT)
*   **数据集划分**：
    *   **训练集**：2016-01-01 至 2023-12-31 (8年数据量)
    *   **验证集**：2024-01-01 至 2024-12-31 (用于 Early Stopping)
    *   **测试集**：2025-01-01 至今 (用于 OOS 验证)

## 3.1 标签口径：T+N 预测目标

训练页与模型管理页统一使用 `T+N` 表示标签 horizon，也就是“以交易日 T 为基准，预测未来 N 个交易日后的目标值”。

- 默认值：`T+1`
- 默认目标：回归型未来收益率
- 可扩展目标：分类型涨跌方向
- 推荐写入元数据的字段：
  - `target_horizon_days`
  - `target_mode`
  - `label_formula`
  - `training_window`

示例标签口径：

```yaml
label: ["Ref($close, -2) / Ref($close, -1) - 1"] # T+1
```

如果 horizon 改为 `N`，则标签与训练摘要应同步改写为对应的 `T+N` 口径，避免训练页、模型管理页和回测中心出现不同版本的定义。

## 4. 回测与实盘一致性保障

1.  **特征计算**：提取特征时，Qlib 内部会自动计算 `$close * $factor`。
2.  **信号输出**：模型输出分值（Score/Rank），不直接输出交易价格。
3.  **预测存储**：保存为 `pred.pkl`，包含 `datetime`, `instrument`, `score`。
4.  **撮合执行**：回测引擎读取 `pred.pkl`，根据 `score` 排序选股，下单时查询对应的原始 `$close` (或 `$open`) 进行撮合，并记录手续费。

## 5. 快速启动命令

```bash
# 激活环境
source .venv/bin/activate

# 运行训练脚本 (示例)
python scripts/training/train_custom_lgbm_duckdb.py --feature-set 64
```
