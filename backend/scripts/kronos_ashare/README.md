# Kronos A股零样本测试

> **最终判定（2026-11-08）：A 股现阶段无可用增量，不集成、不做特征、不建议微调。**
>
> - 503 个交易日（csi300，2024-06~2026-06）零样本原始 RankIC = **−0.0007**；与 20 日动量相关 **−0.63**，本质是反转指标的另一种写法；
> - 对动量正交后残差 IC +0.012（ICIR 1.68），低于 0.02 门槛；
> - L5 的 LGB 对照在种子 42 下 ΔRankIC +0.0143（t=5.17），但 **同日打乱特征后增益 +0.0164（更大）**，换种子甚至 −0.0186 → 增益是模型随机性，不是信息；
> - 路径数 1/2/3/5/10/20 无单调差异，**1 条路径就够**。
>
> 评估方法与噪声对照的教训见 `docs/Kronos_A股可用性评估_20261108.md`；本目录代码与 GPU 环境保留，可复用于评估其他序列模型。

> 研究模块，**不接生产、不进前端、不写 `engine_signal_scores`**。用于回答一个问题：Kronos 在 A 股截面上到底有没有预测能力。

## 它做什么

1. 从本地 Qlib 数据读取 OHLCVA；
2. 在每个信号日 T，用过去 `lookback` 根日线调用 Kronos，预测未来 `pred_len` 根；
3. 把预测压成截面分数：`预测期均价 / T 收盘 - 1`；
4. 对 T+pred_len 收益算 IC / RankIC / ICIR / TopK 超额；
5. 自动对比朴素基线（20 日动量、20 日反转）；
6. 输出 `per_date.csv`、`summary.json`、`report.md`。

不训练、不微调、不改模型结构。

## 前置条件

- 容器内或远端训练节点（需要 `qlib`、`torch`）；
- Kronos 官方仓库已 clone：

```bash
git clone https://github.com/shiyu-coder/Kronos.git /data/kronos/Kronos
pip install -r /data/kronos/Kronos/requirements.txt
```

- Qlib 数据：默认 `/data/qlib/cn_data`（宿主 `data/qlib/cn_data`）。

## 先跑 dry-run（不加载模型）

```bash
docker exec quantmind python3 -m backend.scripts.kronos_ashare.run_zero_shot \
  --qlib-dir /data/qlib/cn_data \
  --universe csi300 \
  --start 2025-01-01 --end 2026-11-06 \
  --signal-every 20 --max-dates 6 \
  --lookback 90 --pred-len 20 \
  --dry-run
```

确认：能读到 300 只、每个信号日有足够窗口、输出目录生成 `dry_run.json`。

## 跑 Kronos-small（中等模型）

```bash
docker exec quantmind python3 -m backend.scripts.kronos_ashare.run_zero_shot \
  --qlib-dir /data/qlib/cn_data \
  --universe csi300 \
  --start 2025-01-01 --end 2026-11-06 \
  --signal-every 20 --max-dates 6 \
  --lookback 90 --pred-len 20 \
  --model NeoQuasar/Kronos-small \
  --tokenizer NeoQuasar/Kronos-Tokenizer-base \
  --device cpu --sample-count 2 \
  --kronos-home /data/kronos/Kronos
```

CPU 上先用 `--limit-symbols 50 --max-dates 2 --sample-count 1` 冒烟，再放大。
GPU 节点把 `--device cuda:0 --sample-count 10` 打开。

## 模型与参数实测（2026-11-08，csi300 前 100 只 × 12 个截面）

| 组 | 模型 | lookback | pred_len | 路径数 | RankIC | ICIR | TopK 超额 | TopK 胜率 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| A | base | 100 | 5 | 1 | +0.035 | 0.63 | +0.06% | 33% |
| B | base | 90 | 10 | 1 | +0.075 | 1.50 | +0.40% | 50% |
| C | base | 100 | 10 | 1 | +0.097 | 1.64 | +0.57% | 67% |
| D | base | 90 | 5 | 1 | +0.097 | 2.03 | +0.62% | 83% |

同一批日期里 20 日动量的 RankIC 是 −0.011。A 与 D 只差 lookback 10 根就翻倍，说明
**12 个截面不足以分辨参数优劣**（标准误约 ±0.055）；能分辨的是整体水位。要下参数结论，
截面数至少要 100 个。

## 特征化评估链路（L4/L5）

零样本 IC 只是第一道门。真正决定「能不能用」的是**把它当特征喂给现有模型有没有增量**：

```bash
# 1) 导出日频特征（每个交易日 × 每只股票一行）
python3 -m backend.scripts.kronos_ashare.feature_export \
  --start 2024-06-03 --end 2026-06-30 --step-days 1 \
  --lookback 100 --pred-len 10 --limit-symbols 300 --device cuda:0 \
  --output /data/kronos_ashare/features/base_csi300_l100_p10.parquet --resume

# 2) 残差 IC：对 20 日动量正交后还剩多少
python3 -m backend.scripts.kronos_ashare.feature_probe \
  --features /data/kronos_ashare/features/base_csi300_l100_p10.parquet \
  --horizon 10 --output /data/kronos_ashare/probe_l100_p10.json

# 3) LGB 对照：同配方 ± Kronos 特征，purged 分窗比 OOS RankIC
python3 -m backend.scripts.kronos_ashare.feature_ablation \
  --features /data/kronos_ashare/features/base_csi300_l100_p10.parquet \
  --horizon 10 --output /data/kronos_ashare/ablation_l100_p10.json
```

**判据**

- 残差 RankIC < 0.02 或 ICIR < 0.3 → 停，不做训练；
- `base_plus_kronos` 相对 `base` 的 OOS RankIC 提升 < 0.01 → 不进库，标「待验证」；
- 提升 ≥ 0.01 且训练窗内外同向 → 才考虑写进 `l1_l2_factors` 供给生产模型。

代码符号口径：Qlib instruments 是 `sh600000`，QuantDB 因子面板（`l1_l2_factors`）是
`600000.SH`，合并前必须过 `StockCodeUtil.to_suffix`。

## GPU 环境（研究用，不碰生产容器）

生产容器 `quantmind` 是 CPU 版 torch 且没有 `--gpus`。研究侧用**独立容器 + 独立 venv**：

```bash
# venv（torch 2.8.0+cu128，与宿主 5060 Ti / sm120 匹配）
python3 -m venv --system-site-packages /data/kronos/gpuvenv
pip install --find-links https://mirrors.aliyun.com/pytorch-wheels/cu128/ "torch==2.8.0+cu128" --no-deps
# CUDA 运行库复用本地 paddle GPU 镜像里的 nvidia 包（4.1G，拷到 /data/kronos/nvidia-libs）
docker run --rm --gpus all --volumes-from quantmind \
  -e PYTHONPATH=/data/kronos/nvidia-libs:/data/kronos/pydeps:/app \
  --entrypoint bash quantmind-oss:latest -lc \
  '/data/kronos/gpuvenv/bin/python -m backend.scripts.kronos_ashare.feature_export ...'
```

实测吞吐（RTX 5060 Ti）：模型加载 ~39s（固定成本），推理 **0.035s/样本**（batch 64），
建窗口 0.004s/样本。15 万样本约 1.5~2 小时。

## 输出怎么读

| 字段 | 含义 |
|---|---|
| `ic_mean` | Pearson IC 均值 |
| `rank_ic_mean` | Spearman RankIC 均值 |
| `rank_ic_icir` | RankIC 均值 / 标准差 × √截面数 |
| `topk_excess_mean` | TopK 相对全池的平均超额（可执行收益口径） |
| `topk_hit_mean` | TopK 超额 > 0 的截面占比 |

**判据（建议先定死）**

- RankIC 不高于 `momentum_20` / `reversal_20` → 零样本没有增量，停；
- RankIC 有但 TopK 扣费前超额 ≤ 0 → 只当特征（B 方案），不做策略；
- 过线才进入微调（A 方案）。

## 口径与边界

- 信号日 T 收盘出分，T+`entry_lag` 收盘成交；默认 T+1。
- IC/RankIC 对 `close[T+pred_len]/close[T]-1`（模型目标）。
- TopK 超额对 `close[T+pred_len]/close[T+entry_lag]-1`，并剔除入场日涨停。
- 涨停判定复用 `backend/scripts/stock_classifier/panel.py` 的板块近似（**ST 按主板处理**），正式结论要用 `backend/services/simulation/services/local_market_data.py` 的 `compute_limits` 复核。
- 没有扣佣金/印花税/滑点；没有做行业/风格中性化；没有做 PIT 成分。
- 模型预测价没有涨跌停约束；本脚本只用于**截面排序**，不把预测价当成交价。

## 文件

| 文件 | 作用 |
|---|---|
| `windows.py` | 信号日、窗口、前视收益、分数（纯函数） |
| `metrics.py` | IC/RankIC/TopK/汇总/基线（纯函数） |
| `data.py` | Qlib 读取（延迟导入 qlib） |
| `kronos_runtime.py` | Kronos 延迟导入与批量预测 |
| `run_zero_shot.py` | 零样本截面测试 CLI |
| `feature_export.py` | 日频特征导出（断点续写，供训练使用） |
| `feature_probe.py` | 残差 RankIC（对动量正交后的增量） |
| `feature_ablation.py` | LGB 对照训练（± Kronos 特征的 OOS 比较） |

测试：`backend/tests/test_kronos_ashare_windows.py`、`backend/tests/test_kronos_ashare_metrics.py`。
