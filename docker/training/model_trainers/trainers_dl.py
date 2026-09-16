"""DL/TFT 组训练器与时序 DataLoader（B4 由 train.py 拆出）。

注册说明：DL 条目由 registry.py 的注册循环统一登记（按 _DL_MODEL_TYPES），
本模块只提供训练/预测实现，不直接操作注册表。
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import torch

from model_trainers.metrics import _compute_metrics

logger = logging.getLogger("quantmind.train")

_QLIB_TS_MODEL_MAP: dict[str, tuple[str, str]] = {
    "gru":         ("qlib.contrib.model.pytorch_gru_ts",         "GRU"),
    "lstm":        ("qlib.contrib.model.pytorch_lstm_ts",        "LSTM"),
    "alstm":       ("qlib.contrib.model.pytorch_alstm_ts",       "ALSTM"),
    "transformer": ("qlib.contrib.model.pytorch_transformer_ts", "TransformerModel"),
    "tcn":         ("qlib.contrib.model.pytorch_tcn_ts",         "TCN"),
}

_QLIB_FLAT_MODEL_MAP: dict[str, tuple[str, str]] = {
    "tabnet":      ("qlib.contrib.model.pytorch_tabnet",         "TabnetModel"),
}

_QLIB_MODEL_MAP = {**_QLIB_TS_MODEL_MAP, **_QLIB_FLAT_MODEL_MAP}

# 预测分块粒度（内存模型，配套 2026-09-16 v4 预测段 OOM 修复）：
# 全量单拷贝预测数组在 768 万行×273 feats 下达 8.4GB，与训练底仓叠加顶穿主机
# 内存上限（v4 在 41.3GB 被内核全局 OOM 实证）。TS 滑窗永不跨 symbol → 按符号
# 数分块；flat 模型逐行独立 → 按行数分块。窗口/归一化/权重均与块无关，分块
# 与全量逐值等价（tools/equiv 测试脚本验证）。
_PRED_SYMBOL_CHUNK = 400
_PRED_ROW_CHUNK = 1_000_000

class _TSLazyDataset(torch.utils.data.Dataset):
    """Lazy TS dataset: 按需生成滚动窗口，避免一次性加载全部窗口到内存。

    存储原始数据 (per-instrument contiguous arrays)，__getitem__ 时动态切片。
    内存占用: O(total_rows * d_feat) 而非 O(N_windows * step_len * d_feat)。
    """

    def __init__(self, X: np.ndarray, y: np.ndarray, instrument_offsets: list[int], step_len: int):
        self.X = X              # [total_rows, d_feat] float32 contiguous
        self.y = y              # [total_rows] float32
        self.step_len = step_len
        # 每个 instrument 的有效窗口起始行号 (全局索引)。
        # 向量化构造：逐 instrument 的 Python 循环在千万行规模下会退化为分钟级开销，
        # 且 list 存数百万 int 对象内存放大数倍，这里直接生成 ndarray。
        bounds = np.asarray(instrument_offsets, dtype=np.int64)
        starts = bounds[:-1]
        lengths = bounds[1:] - starts
        n_windows = np.maximum(lengths - step_len + 1, 0)
        keep = n_windows > 0
        if not keep.any():
            raise ValueError(f"No valid TS samples (step_len={step_len}, rows={len(X)})")
        starts, n_windows = starts[keep], n_windows[keep]
        # 每个 instrument 生成 [start, start+1, ..., start+n_windows-1]
        base = np.repeat(starts, n_windows)
        within = np.arange(n_windows.sum(), dtype=np.int64) - np.repeat(
            np.concatenate([[0], np.cumsum(n_windows)[:-1]]), n_windows
        )
        self.indices = base + within

    def __len__(self) -> int:
        return len(self.indices)

    def __getitem__(self, idx: int) -> tuple["torch.Tensor", "torch.Tensor"]:
        import torch
        start = self.indices[idx]
        window = self.X[start : start + self.step_len].copy()  # [step_len, d_feat]
        label = self.y[start + self.step_len - 1]
        # Qlib TS 模型期望: data[:, 0:-1] = features, data[-1, -1] = label
        label_col = np.full((self.step_len, 1), np.float32(0.0))
        label_col[-1, 0] = label
        row = np.concatenate([window, label_col], axis=1)  # [step_len, d_feat+1]
        # Qlib train_epoch 期望 (data, weight) 元组
        weight = torch.tensor(1.0, dtype=torch.float32)
        return torch.from_numpy(row), weight

def _build_ts_dataloader(
    df_X: pd.DataFrame,
    df_y: pd.Series,
    step_len: int,
    batch_size: int,
    shuffle: bool = True,
    feat_norm: tuple[np.ndarray, np.ndarray] | None = None,
    drop_last: bool = True,
) -> "tuple[torch.utils.data.DataLoader, tuple[np.ndarray, np.ndarray]]":
    """将扁平 DataFrame (MultiIndex: instrument x datetime) 转为 3D DataLoader。

    每个样本是 [step_len, d_feat+1]，最后一列为 label (取自最后一个时间步)。
    使用 LazyDataset 按需生成窗口，内存占用 O(rows * d_feat)。

    参数:
        feat_norm: (mean, std) 元组。如果为 None，从数据中计算统计量并返回。
                   验证集应传入训练集的统计量，避免 look-ahead。

    返回:
        (DataLoader, (mean, std)) — mean/std 始终返回，方便下游记录。
    """
    import torch
    from torch.utils.data import DataLoader

    # copy=True：显式自有缓冲，后续全部原地操作（避免污染调用方视图）
    X_values = np.array(df_X.values, dtype=np.float32, copy=True)
    y_values = np.ascontiguousarray(df_y.values, dtype=np.float32)

    # 填充特征中的 NaN/inf（GRU 只 mask label 中的 NaN，不处理 feature 中的 NaN）
    nan_count = np.isnan(X_values).sum()
    inf_count = np.isinf(X_values).sum()
    if nan_count > 0 or inf_count > 0:
        logger.info("Cleaning features: %d NaN, %d inf -> 0.0", nan_count, inf_count)
        X_values = np.nan_to_num(X_values, nan=0.0, posinf=0.0, neginf=0.0)

    # 清理标签中的 NaN/inf，否则 _TSLazyDataset 会让 label_col 含 NaN，
    # 导致 Transformer/TCN 的 loss_fn 和 metric_fn 输出 NaN
    y_nan = np.isnan(y_values).sum()
    y_inf = np.isinf(y_values).sum()
    if y_nan > 0 or y_inf > 0:
        logger.info("Cleaning labels: %d NaN, %d inf -> 0.0", y_nan, y_inf)
        y_values = np.nan_to_num(y_values, nan=0.0, posinf=0.0, neginf=0.0)

    # Z-score 标准化：Qlib DataHandlerLP 默认做全局 z-score，我们绕过数据管道
    # 直接用 parquet 数据，需手动标准化。不标准化的话，特征值范围在 ±10^10
    # 级别的数据进入 Transformer self-attention softmax 会产生 NaN。
    # 每列分别计算 mean/std，验证集复用训练集的统计量（避免 look-ahead）。
    if feat_norm is not None:
        _feat_mean, _feat_std = feat_norm
    else:
        _feat_mean = X_values.mean(axis=0)
        _feat_std = X_values.std(axis=0)
        _feat_std = np.where(_feat_std == 0, 1.0, _feat_std)  # 避免除零
    X_values -= _feat_mean
    X_values /= _feat_std
    # 标准化后再次确保无 NaN（mean/std 计算过程中可能引入）
    _post_nan = np.isnan(X_values).sum()
    if _post_nan:
        X_values = np.nan_to_num(X_values, nan=0.0)

    if isinstance(df_X.index, pd.MultiIndex):
        # 单次取出 instrument 层，避免在循环内反复 get_level_values + 全量 == 比较
        # （旧实现对每只股票各扫一遍全表，5400股 x 640万行 ≈ 350亿次比较，单核跑数小时）
        inst_codes, inst_uniques = pd.factorize(df_X.index.get_level_values(0), sort=False)
        # 按 instrument 稳定排序，使同股票行连续（stable 保持各股票内部的时间顺序）
        order = np.argsort(inst_codes, kind="stable")
        X_values = X_values[order]
        y_values = y_values[order]
        counts = np.bincount(inst_codes, minlength=len(inst_uniques))
        offsets = np.concatenate([[0], np.cumsum(counts)]).tolist()
    else:
        offsets = [0, len(X_values)]

    dataset = _TSLazyDataset(X_values, y_values, offsets, step_len)
    # 记录排序索引（相对 df_X 的行），供推理 scatter 回原始行号
    if isinstance(df_X.index, pd.MultiIndex):
        dataset.original_rows = order
    logger.info("TS DataLoader: %d samples from %d rows (step_len=%d)", len(dataset), len(X_values), step_len)
    # drop_last=True 与 Qlib 官方 fit() 一致：末批若只剩 1 个样本，
    # collate 会把 weight 压成 0 维标量，Qlib loss_fn 内的 weight[mask] 会抛
    # IndexError: too many indices for tensor of dimension 0。
    loader = DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=shuffle,
        drop_last=drop_last,
        num_workers=0,
    )
    return loader, (_feat_mean, _feat_std)


def _build_ts_dataloader_from_frame(
    frame: pd.DataFrame,
    features: list[str],
    label_col: "str | None" = "label",
    *,
    step_len: int,
    batch_size: int,
    shuffle: bool = True,
    feat_norm: "tuple[np.ndarray, np.ndarray] | None" = None,
    drop_last: bool = True,
) -> "tuple[torch.utils.data.DataLoader, tuple[np.ndarray, np.ndarray]]":
    """单拷贝 TS 数据通路（机构级内存模型，2026-09-16）。

    背景：旧链路（_ts_indexed + _build_ts_dataloader）在训练集上串行产生
    ``sort_values → frame[features] → .values → (x-mean) → /std → fancy-reorder``
    共 6 份整表副本（640 万×273 float32 每份 ≈7GB，峰值 3~4 份并存 ≈25-30GB），
    叠加加载后底仓后必然击穿容器限额——这是 DL 大表"模型构建即 OOM"的根因。

    分配模型：**全流程只有 1 个 n×d 主数组**（≈7GB），其余全部原地：
    1) 符号连续性校验（加载器输出已按 (symbol, trade_date) 排序，通常零拷贝直通；
       乱序时做唯一一次 lexsort，记录置换供 scatter 映射）；
    2) 逐列搬入预分配数组（每列 ≈32MB，与 splits._fill 同款做法）；
    3) 原地清 NaN/Inf + 原地 z-score（X 为自有缓冲，唯一副本，安全）；
    4) 分组偏移由 factorize 直接给出（无 MultiIndex 对象开销）。
    """
    n = len(frame)
    if n == 0:
        raise ValueError("empty frame for TS dataloader")

    from torch.utils.data import DataLoader

    sym = frame["symbol"].to_numpy()
    if n > 1 and not bool((sym[1:] >= sym[:-1]).all()):
        # 异常路径：乱序输入。lexsort 主键取 (symbol, trade_date) 稳定序
        order = np.lexsort(
            (frame["trade_date"].to_numpy(), sym)
        )
        frame = frame.iloc[order]
        sym = sym[order]
    else:
        order = None  # 恒等：行序即原始行序

    X = np.empty((n, len(features)), dtype=np.float32)
    for i, c in enumerate(features):
        X[:, i] = frame[c].to_numpy(dtype=np.float32, copy=False)
    if label_col and label_col in frame.columns:
        y = frame[label_col].to_numpy(dtype=np.float32, copy=False)
    else:
        y = np.zeros(n, dtype=np.float32)  # 纯推理：无标签

    np.nan_to_num(X, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
    if feat_norm is None:
        mean = X.mean(axis=0)
        std = np.where((s := X.std(axis=0)) == 0, 1.0, s)
        feat_norm = (mean, std)
    else:
        mean, std = feat_norm
    X -= mean
    X /= std
    np.nan_to_num(X, copy=False, nan=0.0)

    inst_codes, inst_uniques = pd.factorize(sym, sort=False)
    counts = np.bincount(inst_codes, minlength=len(inst_uniques))
    offsets = np.concatenate([[0], np.cumsum(counts)]).tolist()

    dataset = _TSLazyDataset(X, y, offsets, step_len)
    # 接口对齐旧链路：scatter 消费者按 original_rows[ds.indices] 映射回原始行
    dataset.original_rows = order if order is not None else np.arange(n)
    loader = DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=shuffle,
        drop_last=drop_last,
        num_workers=0,
    )
    logger.info(
        "TS single-copy loader: %d samples from %d rows × %d feats (X=%.2fGB, order=%s)",
        len(dataset), n, len(features), X.nbytes / 1e9,
        "identity" if order is None else "lexsorted",
    )
    return loader, feat_norm

def _train_dl(
    model_type: str,
    train_df: pd.DataFrame,
    val_df: pd.DataFrame,
    features: list[str],
    dl_params: dict[str, Any],
    output_dir: Path,
    hardware: dict[str, Any] | None = None,
) -> tuple:
    """Qlib 深度学习模型训练。

    返回 (model_obj, train_metrics, val_metrics, dl_metadata)
    """
    import importlib
    import copy
    import torch

    mod_path, cls_name = _QLIB_MODEL_MAP[model_type]
    mod = importlib.import_module(mod_path)
    ModelCls = getattr(mod, cls_name)

    d_feat = len(features)
    is_ts = model_type in _QLIB_TS_MODEL_MAP

    # 构建模型参数（按 Qlib 实际 API 签名映射 + 量化最佳实践调优）
    # 前端 dl_params 传 key 不带 dl_ 前缀（如 dropout, batch_size, hidden_size），
    # train.py 之前用 dl_ 前缀（如 dl_dropout, dl_batch_size, dl_hidden_size），
    # 兼容两种写法：优先取 dl_ 前缀 key，回退到无前缀 key。
    def _dl(key: str, default: Any) -> Any:
        return dl_params.get(f"dl_{key}", dl_params.get(key, default))

    model_params: dict[str, Any] = {}
    if model_type == "transformer":
        # Transformer: d_model 需被 nhead 整除；lr 宜小（1e-4），dropout 0.2 防过拟合
        d_model = int(_dl("hidden_size", 64))
        nhead = max(1, d_model // 16)  # 64/16=4 heads, 128/16=8 heads
        d_model = nhead * (d_model // nhead)
        if d_model < nhead:
            d_model = nhead * 2
        model_params.update({
            "d_feat": d_feat,
            "d_model": d_model,
            "nhead": nhead,
            "num_layers": int(_dl("num_layers", 2)),
            "dropout": float(_dl("dropout", 0.2)),
            "lr": float(_dl("lr", 0.0001)),  # Transformer 需较小学习率
        })
    elif model_type == "tabnet":
        # TabNet: out_dim=1 绕过 Qlib decoder bug；n_steps 控制决策步数
        model_params.update({
            "d_feat": d_feat,
            "out_dim": 1,
            "final_out_dim": 1,
            "n_d": int(_dl("hidden_size", 64)),
            "n_a": int(_dl("hidden_size", 64)),
            "n_steps": max(1, int(_dl("num_layers", 5))),
            "lr": float(_dl("lr", 0.005)),  # TabNet lr 宜适中
        })
    elif model_type == "tcn":
        # TCN: n_chans 建议 64~128，dropout 0.2~0.3 防过拟合，lr 1e-4
        model_params.update({
            "d_feat": d_feat,
            "n_chans": int(_dl("hidden_size", 128)),
            "kernel_size": int(_dl("kernel_size", 5)),
            "num_layers": int(_dl("num_layers", 2)),
            "dropout": float(_dl("dropout", 0.2)),
            "lr": float(_dl("lr", 0.0001)),
        })
    else:
        # GRU/LSTM/ALSTM: hidden_size 64~128，dropout 0.2 防过拟合，lr 1e-3
        model_params.update({
            "d_feat": d_feat,
            "hidden_size": int(_dl("hidden_size", 64)),
            "num_layers": int(_dl("num_layers", 2)),
            "dropout": float(_dl("dropout", 0.2)),
        })

    n_epochs    = int(_dl("n_epochs", 200))
    batch_size  = int(_dl("batch_size", 4000))
    # lr：rnn/tcn/alstm/nativetft 默认 0.001→3e-4。大表（640万行×273）下
    # 1e-3 一轮即"冲过头"（2026-09-16 GRU 第 1 轮后停滞、被耐心 3 早停实证）；
    # transformer/tabnet 各自分支有专属默认（1e-4 / 5e-3），仅未显式传 lr 时生效。
    lr          = float(_dl("lr", 0.0003))
    step_len    = int(dl_params.get("dl_step_len", 20))
    early_stop  = int(dl_params.get("early_stopping_rounds", 20))
    metric_name = str(dl_params.get("metric", "")).lower()

    # 确定 GPU 和训练参数
    # 所有 Qlib DL 模型（GRU/LSTM/ALSTM/TransformerModel/TCN/TabnetModel）都接受 GPU/n_epochs/lr/batch_size/early_stop/metric
    gpu_id = 0
    if hardware and not hardware.get("gpu_available"):
        gpu_id = -1
    model_params["GPU"] = gpu_id
    model_params["n_epochs"] = n_epochs
    model_params.setdefault("lr", lr)  # 不覆盖模型特定 lr（Transformer/TCN/TabNet 已在上方设置）
    model_params["batch_size"] = batch_size
    model_params["early_stop"] = early_stop
    model_params["metric"] = metric_name

    logger.info("DL model: %s, params=%s, is_ts=%s", model_type, model_params, is_ts)

    # 实例化模型
    model_obj = ModelCls(**model_params)

    # TabNet: Qlib 的 tabnet_model(feature, priors) 返回 (vec, sparse_loss) 元组，
    # 但 train_epoch 内部 self.loss_fn(pred, label) 未解包 pred。
    # 修复：覆盖 loss_fn 自动解包元组。out_dim=1 已在 model_params 中设置。
    if model_type == "tabnet":
        _orig_loss_fn = model_obj.loss_fn.__func__ if hasattr(model_obj.loss_fn, '__func__') else model_obj.loss_fn
        def _safe_loss_fn(self, pred, label):
            if isinstance(pred, tuple):
                pred = pred[0]
            if hasattr(pred, 'dim') and pred.dim() == 2 and pred.shape[1] == 1:
                pred = pred.squeeze(-1)
            return _orig_loss_fn(self, pred, label)
        import types
        model_obj.loss_fn = types.MethodType(_safe_loss_fn, model_obj)
        _orig_metric_fn = model_obj.metric_fn.__func__ if hasattr(model_obj.metric_fn, '__func__') else model_obj.metric_fn
        def _safe_metric_fn(self, pred, label):
            return -self.loss_fn(pred, label)
        model_obj.metric_fn = types.MethodType(_safe_metric_fn, model_obj)

    # 准备训练/验证数据：仅 flat 模型需要 X 矩阵（train_args 直接喂 (X, y)）。
    # TS 通路走 loader，不用这两个切片——pandas 2.x 的 df[cols] 是深拷贝
    # （实测 shares_memory=False），旧实现无条件切 train+val ≈+8GB 常驻，
    # 把 loader 构建期峰值推到主机红线（v6 在 ALSTM 处 40GB 顶穿实证）。
    if is_ts:
        X_train = y_train = X_val = y_val = None
    else:
        X_train = train_df[features]
        y_train = train_df["label"]
        X_val = val_df[features]
        y_val = val_df["label"]

    # Qlib TS 模型（pytorch_*_ts）的 train_epoch/test_epoch 只接受 DataLoader，
    # 且要求样本形如 [step_len, d_feat+1]（最后一列末行为 label）；
    # flat 模型（pytorch_tabnet 等）才接受 (x_df, y_df)。
    is_ts = model_type in _QLIB_TS_MODEL_MAP

    if is_ts:
        # 单拷贝 TS 数据通路（sort/特征提取/.values/归一化/重排的 6 份整表副本
        # 全部消除，见 _build_ts_dataloader_from_frame 文档——旧链路是 DL 大表 OOM 根因）
        # 建 loader（+6.97GB 主数组）前先归还上文预处理阶段滞留的 arena
        from diagnostics.utils import trim_memory as _trim_pre_loader

        _trim_pre_loader("before TS loader build")
        _train_loader, _feat_norm = _build_ts_dataloader_from_frame(
            train_df, features, "label",
            step_len=step_len, batch_size=batch_size, shuffle=True,
        )
        _val_loader, _ = _build_ts_dataloader_from_frame(
            val_df, features, "label",
            step_len=step_len, batch_size=batch_size, shuffle=False,
            feat_norm=_feat_norm,  # 验证集复用训练集统计量
        )
        train_args: tuple = (_train_loader,)
        val_args: tuple = (_val_loader,)

        # Qlib Transformer/TCN 的 train_epoch/test_epoch 用 `for data in loader`
        # （不解包 weight），而 GRU/LSTM/ALSTM 用 `for data, weight in loader`。
        # 包装 DataLoader，让 Transformer/TCN 只返回 data tensor。
        _needs_weight_unpack = model_type not in ("transformer", "tcn")
        if not _needs_weight_unpack:
            _orig_train_loader = train_args[0]
            _orig_val_loader = val_args[0]

            class _WeightlessLoader:
                """Strip weight from (data, weight) tuples for Qlib models that don't expect it."""
                def __init__(self, loader):
                    self._loader = loader
                def __iter__(self):
                    for batch in self._loader:
                        data = batch[0] if isinstance(batch, (list, tuple)) else batch
                        yield data
                def __len__(self):
                    return len(self._loader)

            train_args = (_WeightlessLoader(_orig_train_loader),)
            val_args = (_WeightlessLoader(_orig_val_loader),)
    else:
        train_args = (X_train, y_train)
        val_args = (X_val, y_val)

    # 训练循环 (直接调用 Qlib 模型的 train_epoch/test_epoch)
    best_score = -np.inf
    best_epoch = 0
    stop_steps = 0
    best_state = None
    evals: dict[str, list[float]] = {"train": [], "valid": []}

    logger.info("DL training: %d epochs, batch_size=%d, lr=%s", n_epochs, batch_size, lr)

    for epoch in range(n_epochs):
        model_obj.train_epoch(*train_args)

        # Evaluate — TS 模型 test_epoch 只返回 score，flat 模型返回 (loss, score)
        _val_out = model_obj.test_epoch(*val_args)
        if isinstance(_val_out, tuple):
            val_loss, val_score = _val_out
        else:
            val_loss, val_score = float("nan"), float(_val_out)

        train_score = float("nan")  # placeholder, not computed each epoch
        evals["train"].append(train_score)
        evals["valid"].append(val_score)

        # 每轮都打：早期停滞诊断依赖完整曲线（旧实现 (epoch+1)%10 过滤，
        # 8 轮配置只打第 1 轮，停滞只能靠早停终值倒推）
        logger.info("Epoch %d/%d: valid=%.6f", epoch + 1, n_epochs, val_score)

        if val_score > best_score:
            best_score = val_score
            best_epoch = epoch
            stop_steps = 0
            # 保存最佳状态
            inner_model = None
            for attr_name in ("model", "GRU_model", "gru_model", "LSTM_model", "lstm_model",
                              "ALSTM_model", "alstm_model", "TCN_model", "tcn_model",
                              "tabnet_model", "transformer_model"):
                inner_model = getattr(model_obj, attr_name, None)
                if inner_model is not None:
                    break
            if inner_model is not None:
                best_state = copy.deepcopy(inner_model.state_dict())
        else:
            stop_steps += 1
            if stop_steps >= early_stop:
                logger.info("Early stop at epoch %d (best=%d, score=%.6f)", epoch, best_epoch, best_score)
                break

    # 恢复最佳模型
    if best_state is not None:
        inner_model = None
        for attr_name in ("model", "GRU_model", "gru_model", "LSTM_model", "lstm_model",
                          "ALSTM_model", "alstm_model", "TCN_model", "tcn_model",
                          "tabnet_model", "transformer_model"):
            inner_model = getattr(model_obj, attr_name, None)
            if inner_model is not None:
                break
        if inner_model is not None:
            inner_model.load_state_dict(best_state)

    # 保存模型
    torch.save(best_state, str(output_dir / "model.pth"))
    logger.info("DL model saved: model.pth (best_epoch=%d, best_score=%.6f)", best_epoch, best_score)

    # 预测/指标前释放训练态大数据：TS loader 的 X（训练 ~6.97GB + 验证 ~0.81GB）
    # 与训练帧切片在保存后仍被局部变量持有，而随后 _dl_metrics 会对 train_df/val_df
    # 逐块预测——不释放则全量预测数组与训练底仓叠加顶穿主机内存上限
    # （2026-09-16 v4 在 41.3GB 被内核全局 OOM 实证；容器限额 53G 之上还有
    #  主机 60G 全局天花板，实际可用仅 ~41GB）。
    if is_ts:
        del _train_loader, _val_loader
        if model_type in ("transformer", "tcn"):
            # _WeightlessLoader 包装时保留了原始 loader 引用，一并断开
            del _orig_train_loader, _orig_val_loader
    train_args = val_args = None  # 断开 _WeightlessLoader / (X_train, y_train) 引用链
    del X_train, X_val, y_train, y_val
    from diagnostics.utils import trim_memory as _trim_after_train

    _trim_after_train("after train, before metrics")

    # DL 元数据 (供推理重建模型)
    dl_metadata = {
        "model_class_name": cls_name,
        # 记录模型类型：多模型训练时权重文件按类型命名（model_{type}.pth），
        # 推理侧需要它来定位正确的权重（缺省回退 model.pth 兼容旧产物）
        "model_type": model_type,
        "model_params": {k: v for k, v in model_params.items() if k not in ("GPU", "n_epochs", "lr", "batch_size", "early_stop", "metric")},
        "is_sequence_model": is_ts,
        "input_spec": {
            "tensor_shape": [None, step_len, d_feat] if is_ts else [None, d_feat],
            "feature_columns": features,
        },
        "dl_params": {k: v for k, v in dl_params.items()},
    }
    # feat_norm: TS 模型训练时用训练集 mean/std 标准化（验证集复用），
    # 推理必须用同一统计量，否则输入分布漂移导致预测不可信。
    if is_ts and _feat_norm is not None:
        _fm, _fs = _feat_norm
        dl_metadata["feat_norm"] = {
            "mean": [float(v) for v in _fm],
            "std": [float(v) for v in _fs],
        }

    # 计算指标：train/val 用模型预测算真实 rmse/auc（不再硬编码 NaN）
    # test 指标由 train_model 基于 test_df 计算。
    def _dl_metrics(frame: pd.DataFrame) -> dict:
        try:
            pred_df = _predict_dl(output_dir, frame, features, dl_metadata)
            # 轻量指标帧（内存模型，2026-09-16 v6）：旧实现 frame.copy() + 全列
            # merge 对 640 万行 train 帧瞬时再分配 ~14GB（7.2GB 拷贝 + 7.2GB
            # merge 输出），与常驻帧/模型叠加在 42.8GB 顶穿主机上限（v5 实证）。
            # _predict_dl 输出与输入行序严格一致（scatter 按行号回填），且
            # _compute_metrics 仅依赖 trade_date 做每日分组 → 位置对齐后只构造
            # 单列小帧，逐值与旧 merge 通路一致。
            _has_keys = "symbol" in frame.columns and "trade_date" in frame.columns
            _aligned = False
            if _has_keys and len(pred_df) == len(frame):
                _aligned = bool(
                    (frame["symbol"].to_numpy() == pred_df["symbol"].to_numpy()).all()
                    and (frame["trade_date"].to_numpy() == pred_df["trade_date"].to_numpy()).all()
                )
            if _aligned:
                pred_np = pred_df["pred"].to_numpy()
                if int((~np.isnan(pred_np)).sum()) < 10:
                    logger.warning("DL metrics: 预测不足 10 行有效，rmse/auc 置 0")
                    return {"ic": float("nan"), "rank_ic": float("nan"), "rank_icir": float("nan"), "rmse": 0.0, "auc": 0.0}
                small = pd.DataFrame({"trade_date": frame["trade_date"].to_numpy()})
                y_true = frame["label"].astype("float32").to_numpy()
                y_pred = np.where(np.isnan(pred_np), np.float32(0.0), pred_np).astype("float32")
                return _compute_metrics(small, y_true, y_pred)
            # 回退：遗留无键帧 / 键序不匹配的防御路径，保持旧 merge 通路
            frame_m = frame.copy()
            if "symbol" not in frame_m.columns or "trade_date" not in frame_m.columns:
                frame_m = frame.reset_index()
                if "instrument" in frame_m.columns:
                    frame_m["symbol"] = frame_m["instrument"]
                    frame_m["trade_date"] = frame_m["datetime"] if "datetime" in frame_m.columns else 0
            merged = frame_m.merge(pred_df[["symbol", "trade_date", "pred"]], on=["symbol", "trade_date"], how="left")
            if merged["pred"].notna().sum() < 10:
                logger.warning("DL metrics: 预测不足 10 行有效，rmse/auc 置 0")
                return {"ic": float("nan"), "rank_ic": float("nan"), "rank_icir": float("nan"), "rmse": 0.0, "auc": 0.0}
            y_true = merged["label"].astype("float32").to_numpy()
            y_pred = merged["pred"].fillna(0).astype("float32").to_numpy()
            return _compute_metrics(merged, y_true, y_pred)
        except Exception as exc:
            logger.warning("DL metrics 计算失败: %s", exc)
            return {"ic": float("nan"), "rank_ic": float("nan"), "rank_icir": float("nan"), "rmse": 0.0, "auc": 0.0}

    train_m = _dl_metrics(train_df)
    val_m = _dl_metrics(val_df)
    train_m.setdefault("rmse", 0.0); train_m.setdefault("auc", 0.0)
    val_m.setdefault("rmse", 0.0); val_m.setdefault("auc", 0.0)

    return model_obj, train_m, val_m, dl_metadata

def _train_nativetft(
    model_type: str,
    train_df: pd.DataFrame,
    val_df: pd.DataFrame,
    features: list[str],
    dl_params: dict[str, Any],
    output_dir: Path,
    hardware: dict[str, Any] | None = None,
) -> tuple:
    """NativeTFT (轻量 TFT 变体) 训练。

    架构: input_proj → GRU → MultiheadAttention → GRN → output
    非 Qlib 模型，使用自定义训练循环 + _build_ts_dataloader 构建 3D 窗口。
    """
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    import copy

    d_feat = len(features)
    _dl_h = dl_params.get("dl_hidden_size", dl_params.get("hidden_size", 64))
    hidden_dim = int(_dl_h)
    _dl_nh = dl_params.get("dl_num_heads", dl_params.get("num_heads", 4))
    num_heads = max(1, int(_dl_nh))
    # 确保 hidden_dim 被 num_heads 整除
    hidden_dim = num_heads * (hidden_dim // num_heads)
    if hidden_dim < num_heads:
        hidden_dim = num_heads * 2
    dropout = float(dl_params.get("dl_dropout", dl_params.get("dropout", 0.2)))
    step_len = int(dl_params.get("dl_step_len", dl_params.get("step_len", 20)))
    n_epochs = int(dl_params.get("dl_n_epochs", dl_params.get("n_epochs", 200)))
    batch_size = int(dl_params.get("dl_batch_size", dl_params.get("batch_size", 4000)))
    lr = float(dl_params.get("dl_lr", dl_params.get("lr", 0.0005)))
    early_stop = int(dl_params.get("early_stopping_rounds", dl_params.get("early_stop", 20)))

    device = torch.device("cpu")
    if hardware and hardware.get("gpu_available"):
        device = torch.device("cuda:0")

    # ── 构建模型 ──
    class _GRN(nn.Module):
        def __init__(self, input_size, hidden_size, output_size, p_dropout):
            super().__init__()
            self.lin1 = nn.Linear(input_size, hidden_size)
            self.lin2 = nn.Linear(hidden_size, hidden_size)
            self.gate = nn.Linear(hidden_size, output_size)
            self.drop = nn.Dropout(p_dropout)
            self.norm = nn.LayerNorm(output_size)
            self.skip = nn.Linear(input_size, output_size) if input_size != output_size else nn.Identity()

        def forward(self, x):
            h = F.elu(self.lin1(x))
            h = self.lin2(h)
            h = self.drop(h)
            g = self.gate(h).sigmoid()
            return self.norm(self.skip(x) + g * h)

    class _NativeTFTNet(nn.Module):
        def __init__(self):
            super().__init__()
            self.input_proj = nn.Linear(d_feat, hidden_dim)
            self.gru = nn.GRU(hidden_dim, hidden_dim, batch_first=True)
            self.attn = nn.MultiheadAttention(embed_dim=hidden_dim, num_heads=num_heads, batch_first=True)
            self.grn = _GRN(hidden_dim, hidden_dim, hidden_dim, dropout)
            self.output_layer = nn.Linear(hidden_dim, 1)

        def forward(self, x):
            h = self.input_proj(x)
            h_gru, _ = self.gru(h)
            attn_out, _ = self.attn(h_gru, h_gru, h_gru)
            h = h_gru + attn_out
            h = self.grn(h[:, -1, :])
            return self.output_layer(h).squeeze(-1)

    model = _NativeTFTNet().to(device)
    logger.info("NativeTFT: d_feat=%d, hidden=%d, heads=%d, step_len=%d, device=%s",
                d_feat, hidden_dim, num_heads, step_len, device)

    # ── 构建 DataLoader（单拷贝通路，同 _train_dl）──
    # 训练集计算 mean/std 统计量，验证集复用（避免 look-ahead）；
    # feat_norm 持久化进 dl_metadata 供推理复现标准化。
    _train_loader, _feat_norm = _build_ts_dataloader_from_frame(
        train_df, features, "label",
        step_len=step_len, batch_size=batch_size, shuffle=True,
    )
    _val_loader, _ = _build_ts_dataloader_from_frame(
        val_df, features, "label",
        step_len=step_len, batch_size=batch_size, shuffle=False,
        feat_norm=_feat_norm,
    )

    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    loss_fn = nn.MSELoss()

    # ── 训练循环 ──
    best_score = -np.inf
    best_epoch = 0
    stop_steps = 0
    best_state = None
    evals: dict[str, list[float]] = {"train": [], "valid": []}

    logger.info("NativeTFT training: %d epochs, batch_size=%d, lr=%s", n_epochs, batch_size, lr)

    for epoch in range(n_epochs):
        # Train
        model.train()
        epoch_loss = 0.0
        n_batches = 0
        for batch in _train_loader:
            data = batch[0] if isinstance(batch, (list, tuple)) else batch
            feature = data[:, :, 0:-1].float().to(device)
            # _TSLazyDataset 返回 (row, weight)，weight 恒为 1.0，必须从
            # row 的最后一列末行取真标签，否则模型学预测常数 1.0 (loss 极低、IC=NaN)
            label = data[:, -1, -1].float().to(device)

            optimizer.zero_grad()
            pred = model(feature)
            loss = loss_fn(pred, label)
            loss.backward()
            optimizer.step()
            epoch_loss += loss.item()
            n_batches += 1

        # Evaluate
        model.eval()
        val_preds = []
        val_labels = []
        with torch.no_grad():
            for batch in _val_loader:
                data = batch[0] if isinstance(batch, (list, tuple)) else batch
                feature = data[:, :, 0:-1].float().to(device)
                # 同训练循环：真标签在 data[:, -1, -1]，batch[1] 只是 weight=1.0
                label = data[:, -1, -1].float().to(device)
                pred = model(feature)
                val_preds.append(pred.cpu().numpy())
                val_labels.append(label.cpu().numpy())

        val_pred_arr = np.concatenate(val_preds)
        val_label_arr = np.concatenate(val_labels)
        # IC (Pearson correlation) as validation metric; 常数预测/常数标签时
        # corrcoef 为 NaN，置 0 避免 best_state 全 NaN 导致权重丢失
        if len(val_pred_arr) > 1:
            with np.errstate(invalid="ignore"):
                val_score = float(np.corrcoef(val_pred_arr, val_label_arr)[0, 1])
            if not np.isfinite(val_score):
                val_score = 0.0
        else:
            val_score = 0.0

        evals["train"].append(float("nan"))
        evals["valid"].append(val_score)

        if (epoch + 1) % 10 == 0 or epoch == 0:
            logger.info("Epoch %d/%d: valid_ic=%.6f, loss=%.6f", epoch + 1, n_epochs, val_score,
                        epoch_loss / max(1, n_batches))

        if val_score > best_score:
            best_score = val_score
            best_epoch = epoch
            stop_steps = 0
            best_state = copy.deepcopy(model.state_dict())
        else:
            stop_steps += 1
            if stop_steps >= early_stop:
                logger.info("Early stopping at epoch %d (best=%d, score=%.6f)", epoch + 1, best_epoch, best_score)
                break

    if best_state is not None:
        model.load_state_dict(best_state)
    else:
        logger.warning("NativeTFT best_state 为 None（可能 val_score 全 NaN），使用最终权重")
        best_state = copy.deepcopy(model.state_dict())

    # 保存模型
    torch.save(best_state, str(output_dir / "model.pth"))
    logger.info("NativeTFT saved: model.pth (best_epoch=%d, best_ic=%.6f)", best_epoch, best_score)

    dl_metadata = {
        "model_type": "NativeTFT",
        "model_arch": {
            "input_dim": d_feat,
            "hidden_dim": hidden_dim,
            "num_heads": num_heads,
            "dropout": dropout,
        },
        "is_sequence_model": True,
        "input_spec": {
            "tensor_shape": [None, step_len, d_feat],
            "feature_columns": features,
        },
        "dl_params": {k: v for k, v in dl_params.items()},
    }
    # feat_norm: 训练集 mean/std（验证集已复用），推理必须用同一统计量
    if _feat_norm is not None:
        _fm, _fs = _feat_norm
        dl_metadata["feat_norm"] = {
            "mean": [float(v) for v in _fm],
            "std": [float(v) for v in _fs],
        }

    # 计算真实 train/val 指标（不再硬编码 NaN rmse/auc）
    def _tft_metrics(frame: pd.DataFrame) -> dict:
        try:
            pred_df = _predict_nativetft(output_dir, frame, features, dl_metadata)
            frame_m = frame.copy()
            if "symbol" not in frame_m.columns or "trade_date" not in frame_m.columns:
                frame_m = frame.reset_index()
                if "instrument" in frame_m.columns:
                    frame_m["symbol"] = frame_m["instrument"]
                    frame_m["trade_date"] = frame_m["datetime"] if "datetime" in frame_m.columns else 0
            merged = frame_m.merge(pred_df[["symbol", "trade_date", "pred"]], on=["symbol", "trade_date"], how="left")
            if merged["pred"].notna().sum() < 10:
                return {"ic": float("nan"), "rank_ic": float("nan"), "rank_icir": float("nan"), "rmse": 0.0, "auc": 0.0}
            y_true = merged["label"].astype("float32").to_numpy()
            y_pred = merged["pred"].fillna(0).astype("float32").to_numpy()
            return _compute_metrics(merged, y_true, y_pred)
        except Exception as exc:
            logger.warning("NativeTFT metrics 计算失败: %s", exc)
            return {"ic": float("nan"), "rank_ic": float("nan"), "rank_icir": float("nan"), "rmse": 0.0, "auc": 0.0}

    train_m = _tft_metrics(train_df)
    val_m = _tft_metrics(val_df)
    train_m.setdefault("rmse", 0.0); train_m.setdefault("auc", 0.0)
    val_m.setdefault("rmse", 0.0); val_m.setdefault("auc", 0.0)

    return model, train_m, val_m, dl_metadata

def _predict_dl(
    model_dir: Path,
    df_X: pd.DataFrame,
    features: list[str],
    dl_metadata: dict[str, Any],
    batch_size: int = 8000,
) -> np.ndarray:
    """加载训练好的 DL 模型并预测。"""
    import importlib
    import torch

    # 训练阶段（20+ 分钟批次张量 churn）滞留的 glibc arena 先归还 OS：
    # 否则 训练底仓(含滞留 ~10GB) + 预测主数组(全窗口 ≈8.4GB) 会顶穿容器限额
    # （2026-09-16 GRU 保存后 36 秒 OOMKilled 实证）。
    from diagnostics.utils import trim_memory as _trim

    _trim("before dl predict")

    cls_name = dl_metadata.get("model_class_name", "")
    model_params = dl_metadata.get("model_params", {})
    is_ts = dl_metadata.get("is_sequence_model", False)

    # 找到对应的 Qlib 模型类
    model_cls = None
    for _map in (_QLIB_TS_MODEL_MAP, _QLIB_FLAT_MODEL_MAP):
        for _mt, (_mod_path, _cls_name) in _map.items():
            if _cls_name == cls_name:
                mod = importlib.import_module(_mod_path)
                model_cls = getattr(mod, _cls_name)
                break
        if model_cls is not None:
            break

    if model_cls is None:
        raise ValueError(f"Cannot find Qlib model class: {cls_name}")

    infer_device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    logger.info("DL inference device: %s", infer_device)
    # 推理设备跟随可用算力：有 GPU 就用 cuda（本路径不调用 qlib 高层 API，
    # GPU 参数仅保持语义一致，实际前向由下方手写循环驱动）
    model_params["GPU"] = 0 if torch.cuda.is_available() else -1
    model_obj = model_cls(**model_params)

    # 加载权重：多模型训练下权重按类型命名（model_{type}.pth），
    # 单模型 / 旧产物回退固定名 model.pth
    _mt = str(dl_metadata.get("model_type") or "")
    model_path = model_dir / f"model_{_mt}.pth" if _mt else model_dir / "model.pth"
    if not model_path.exists():
        model_path = model_dir / "model.pth"
    if not model_path.exists():
        raise FileNotFoundError(f"DL 权重不存在: 已尝试 model_{_mt}.pth 与 model.pth @ {model_dir}")

    state_dict = torch.load(str(model_path), map_location="cpu")
    inner_model = None
    for attr_name in ("model", "GRU_model", "gru_model", "LSTM_model", "lstm_model",
                      "ALSTM_model", "alstm_model", "TCN_model", "tcn_model",
                      "tabnet_model", "transformer_model"):
        inner_model = getattr(model_obj, attr_name, None)
        if inner_model is not None:
            break
    if inner_model is not None:
        inner_model.load_state_dict(state_dict)
    model_obj.fitted = True

    # 预测
    if is_ts:
        step_len = dl_metadata.get("dl_params", {}).get("dl_step_len", 20)
        # 复用训练集标准化统计量（推理输入必须与训练同分布）
        feat_norm = None
        _fn = dl_metadata.get("feat_norm")
        if isinstance(_fn, dict) and _fn.get("mean") and _fn.get("std"):
            feat_norm = (np.asarray(_fn["mean"], dtype=np.float32),
                         np.asarray(_fn["std"], dtype=np.float32))
        # TS 滑窗必须按 symbol 分组：df_X 是扁平 RangeIndex，
        # 单拷贝通路内部按需 lexsort 并记录 original_rows（相对块帧行），
        # 输出按 (symbol, trade_date) 键控。
        # 分块预测（内存模型，2026-09-16）：TS 滑窗永不跨 symbol，按符号块切分
        # 与全量逐值等价；全量单拷贝数组在 768 万行×273 下达 8.4GB，与底仓
        # 叠加顶穿主机上限（v4 在 41.3GB 被内核全局 OOM 实证），分块后峰值≈
        # 最大块（400 符号 ≈0.5GB）。
        _pred_df = df_X
        n_total = len(_pred_df)
        out = np.full(n_total, np.nan, dtype=np.float32)

        # 找到内部模型（跨块复用，eval/to(device) 只做一次）
        inner_model = None
        for attr_name in ("model", "GRU_model", "gru_model", "LSTM_model", "lstm_model",
                          "ALSTM_model", "alstm_model", "TCN_model", "tcn_model",
                          "tabnet_model", "transformer_model"):
            inner_model = getattr(model_obj, attr_name, None)
            if inner_model is not None:
                break
        if inner_model is not None:
            inner_model.eval()
            inner_model = inner_model.to(infer_device)
        is_tcn = "TCN" in cls_name

        _has_keys = "symbol" in _pred_df.columns and "trade_date" in _pred_df.columns
        if _has_keys and n_total:
            _sym_codes = pd.factorize(_pred_df["symbol"].to_numpy(), sort=False)[0]
            _n_codes = int(_sym_codes.max()) + 1
            _blocks = [
                np.flatnonzero((_sym_codes >= lo) & (_sym_codes < lo + _PRED_SYMBOL_CHUNK))
                for lo in range(0, _n_codes, _PRED_SYMBOL_CHUNK)
            ]
        else:
            _blocks = [np.arange(n_total)]  # 遗留无键调用：单块（与旧实现同路径）

        for _pos in _blocks:
            if _pos.size == 0:
                continue
            # 单块覆盖全帧时不复制（小帧与旧实现内存同路径）
            _chunk = _pred_df if _pos.size == n_total else _pred_df.iloc[_pos]
            # 乱序块修正：scatter 公式（窗口末端 = 起始行 + step_len-1）仅在块内
            # 符号连续（行步长 1）时成立。非符号主序的块先做 (symbol, trade_date)
            # 稳定排序，并同步置换行号映射，保证窗口末端落回真实行。
            # 生产数据为符号主序（loader 日志 order=identity），此分支零成本跳过。
            if _has_keys and len(_chunk) > 1:
                _csym = _chunk["symbol"].to_numpy()
                if not bool((_csym[1:] >= _csym[:-1]).all()):
                    _ord = np.lexsort((_chunk["trade_date"].to_numpy(), _csym))
                    _chunk = _chunk.iloc[_ord]
                    _pos = _pos[_ord]
            try:
                if _has_keys:
                    loader, _ = _build_ts_dataloader_from_frame(
                        _chunk, features, None,
                        step_len=step_len, batch_size=batch_size,
                        shuffle=False, feat_norm=feat_norm, drop_last=False,
                    )
                else:
                    # 遗留调用方（无键帧）：保持旧构造兼容
                    _X = _chunk[features]
                    _y = pd.Series(0.0, index=_chunk.index)
                    loader, _ = _build_ts_dataloader(
                        _X, _y, step_len, batch_size, shuffle=False,
                        feat_norm=feat_norm, drop_last=False,
                    )
            except ValueError as exc:
                # 退化块（块内所有 symbol 行数 < step_len，无完整窗口）：
                # 该块记 NaN，与全量路径中这些 symbol 无窗口的语义一致
                logger.warning("DL predict 跳过退化块（%d 行）: %s", _pos.size, exc)
                if _chunk is not _pred_df:
                    del _chunk
                _trim()
                continue
            preds = []
            for batch in loader:
                data = batch[0] if isinstance(batch, (list, tuple)) else batch
                feature = data[:, :, 0:-1].to(infer_device)
                # TCN 期望 channels-first [batch, d_feat, seq]（训练时 qlib 内部 transpose，
                # 推理需手动补）；GRU/LSTM/ALSTM batch_first，Transformer 内部自处理。
                if is_tcn:
                    feature = feature.transpose(1, 2)
                with torch.no_grad():
                    pred = inner_model(feature.float())
                    if isinstance(pred, tuple):
                        pred = pred[0]
                    if hasattr(pred, 'dim') and pred.dim() == 2 and pred.shape[1] == 1:
                        pred = pred.squeeze(-1)
                    preds.append(pred.detach().cpu().numpy())
            raw_pred = np.concatenate(preds) if preds else np.empty(0, dtype=np.float32)

            # 时序滑窗预测天然比全量行少（每只股票前 step_len-1 行无完整窗口）。
            # 用 dataset.original_rows（块内排序索引→块帧行）把预测 scatter 回
            # 块帧原始行，再经 _pos 映射回全局行号，其余填 NaN。
            if raw_pred.shape[0] > 0 and hasattr(loader.dataset, "original_rows"):
                ds = loader.dataset
                orig = ds.original_rows[ds.indices] + step_len - 1
                if orig.max() < len(_chunk) and len(orig) == raw_pred.shape[0]:
                    out[_pos[orig]] = raw_pred
                else:
                    logger.warning(
                        "DL predict scatter 不匹配(块): pred=%d orig=%d chunk=%d -> 块内全 NaN",
                        raw_pred.shape[0], len(orig), len(_chunk),
                    )
            else:
                logger.warning("DL predict 无 original_rows 或无预测(块): pred=%d", raw_pred.shape[0])
            del loader, preds, raw_pred
            if _chunk is not _pred_df:
                del _chunk
            # 块间归还 glibc arena：每块工作集 ~1.7GB，不 trim 则滞留累积
            # ~1.4GB/块（v5 实测 9 块涨 ~16GB），会顶穿后续阶段
            _trim()
        return pd.DataFrame({
            "symbol": _pred_df["symbol"].to_numpy(),
            "trade_date": _pred_df["trade_date"].to_numpy(),
            "pred": out,
        })
    else:
        # 分块预测（行块）：flat 模型逐行独立，行块切分逐值等价；全量 X（768 万
        # 行）≈8.4GB 与底仓叠加顶穿主机上限（2026-09-16 v4 实证），峰值降为单块。
        n_total = len(df_X)
        _flat_out = np.empty(n_total, dtype=np.float32)
        if inner_model is not None:
            inner_model.eval()
            inner_model = inner_model.to(infer_device)
        is_tabnet = "Tabnet" in cls_name
        _nan_warned = False
        for _st in range(0, n_total, _PRED_ROW_CHUNK):
            _blk = df_X.iloc[_st:_st + _PRED_ROW_CHUNK]
            X_values = np.empty((len(_blk), len(features)), dtype=np.float32)
            for _i, _c in enumerate(features):
                X_values[:, _i] = _blk[_c].to_numpy(dtype=np.float32, copy=False)
            # 填充 NaN/Inf：tabnet 内部断言禁止 NaN 输入（原地）
            if np.isnan(X_values).any() or np.isinf(X_values).any():
                if not _nan_warned:
                    logger.warning("DL flat predict: 特征含 NaN/Inf，填 0（分块处理）")
                    _nan_warned = True
                np.nan_to_num(X_values, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
            preds = []
            for i in range(0, len(X_values), batch_size):
                # 逐批上卡：旧实现把整表 .to(device)（≈7GB 显存 + 同量主机侧
                # 中转），改为批级传输（批 8000 ≈ 9MB）
                batch = torch.from_numpy(X_values[i:i+batch_size]).to(infer_device)
                with torch.no_grad():
                    if is_tabnet:
                        # qlib TabNet.forward(x, priors) 需要 priors 参数（训练时 qlib 内部构造）
                        priors = torch.ones(batch.shape[0], batch.shape[1], dtype=batch.dtype, device=infer_device)
                        pred = inner_model(batch.float(), priors)
                    else:
                        pred = inner_model(batch.float())
                    if isinstance(pred, tuple):
                        pred = pred[0]
                    if hasattr(pred, 'dim') and pred.dim() == 2 and pred.shape[1] == 1:
                        pred = pred.squeeze(-1)
                    preds.append(pred.detach().cpu().numpy())
            _flat_out[_st:_st + len(_blk)] = (
                np.concatenate(preds) if preds else np.float32(0.0)
            )
            del _blk, X_values, preds
            # 块间归还 glibc arena（同 TS 通路，防滞留累积）
            _trim()
        return pd.DataFrame({
            "symbol": df_X["symbol"].to_numpy(),
            "trade_date": df_X["trade_date"].to_numpy(),
            "pred": _flat_out,
        })

def _predict_nativetft(
    model_dir: Path,
    df_X: pd.DataFrame,
    features: list[str],
    dl_metadata: dict[str, Any],
    batch_size: int = 8000,
) -> np.ndarray:
    """加载训练好的 NativeTFT 模型并预测。"""
    import torch

    from diagnostics.utils import trim_memory as _trim

    model_arch = dl_metadata.get("model_arch", {})
    input_dim = int(model_arch.get("input_dim", len(features)))
    hidden_dim = int(model_arch.get("hidden_dim", 64))
    num_heads = int(model_arch.get("num_heads", 4))
    dropout = float(model_arch.get("dropout", 0.1))
    step_len = int(dl_metadata.get("dl_params", {}).get("dl_step_len", 20))

    # 重建模型架构 (与 _train_nativetft 中一致)
    import torch.nn as nn
    import torch.nn.functional as F

    class _GRN(nn.Module):
        def __init__(self, input_size, hidden_size, output_size, p_dropout):
            super().__init__()
            self.lin1 = nn.Linear(input_size, hidden_size)
            self.lin2 = nn.Linear(hidden_size, hidden_size)
            self.gate = nn.Linear(hidden_size, output_size)
            self.drop = nn.Dropout(p_dropout)
            self.norm = nn.LayerNorm(output_size)
            self.skip = nn.Linear(input_size, output_size) if input_size != output_size else nn.Identity()

        def forward(self, x):
            h = F.elu(self.lin1(x))
            h = self.lin2(h)
            h = self.drop(h)
            g = self.gate(h).sigmoid()
            return self.norm(self.skip(x) + g * h)

    class _NativeTFTNet(nn.Module):
        def __init__(self):
            super().__init__()
            self.input_proj = nn.Linear(input_dim, hidden_dim)
            self.gru = nn.GRU(hidden_dim, hidden_dim, batch_first=True)
            self.attn = nn.MultiheadAttention(embed_dim=hidden_dim, num_heads=num_heads, batch_first=True)
            self.grn = _GRN(hidden_dim, hidden_dim, hidden_dim, dropout)
            self.output_layer = nn.Linear(hidden_dim, 1)

        def forward(self, x):
            h = self.input_proj(x)
            h_gru, _ = self.gru(h)
            attn_out, _ = self.attn(h_gru, h_gru, h_gru)
            h = h_gru + attn_out
            h = self.grn(h[:, -1, :])
            return self.output_layer(h).squeeze(-1)

    infer_device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    logger.info("NativeTFT inference device: %s", infer_device)
    model = _NativeTFTNet().to(infer_device)
    # 权重定位：多模型训练下按类型命名（model_nativetft.pth）。注意此处
    # dl_metadata["model_type"] 存的是类名 "NativeTFT"，与注册键大小写不同，
    # 故依次尝试 原值 / 小写 / 固定名三种候选。
    _cands = []
    _mt = str(dl_metadata.get("model_type") or "")
    if _mt:
        _cands += [model_dir / f"model_{_mt}.pth", model_dir / f"model_{_mt.lower()}.pth"]
    _cands.append(model_dir / "model.pth")
    model_path = next((c for c in _cands if c.exists()), None)
    if model_path is None:
        raise FileNotFoundError(
            f"NativeTFT 权重不存在: 已尝试 {[c.name for c in _cands]} @ {model_dir}"
        )
    state_dict = torch.load(str(model_path), map_location=infer_device)
    model.load_state_dict(state_dict)
    model.eval()

    # 复用训练集标准化统计量（推理输入必须与训练同分布）
    feat_norm = None
    _fn = dl_metadata.get("feat_norm")
    if isinstance(_fn, dict) and _fn.get("mean") and _fn.get("std"):
        feat_norm = (np.asarray(_fn["mean"], dtype=np.float32),
                     np.asarray(_fn["std"], dtype=np.float32))
    # 分块预测（符号块，同 _predict_dl）：全量单拷贝数组（768 万行 ≈8.4GB）与
    # 帧副本叠加会顶穿主机上限（2026-09-16 v4 实证）；窗口永不跨 symbol，
    # 分块逐值等价。返回顺序为各块内 (symbol, trade_date) 排序后拼接。
    _has_keys = "symbol" in df_X.columns and "trade_date" in df_X.columns
    n_total = len(df_X)
    if _has_keys and n_total:
        _sym_codes = pd.factorize(df_X["symbol"].to_numpy(), sort=False)[0]
        _n_codes = int(_sym_codes.max()) + 1
        _blocks = [
            np.flatnonzero((_sym_codes >= lo) & (_sym_codes < lo + _PRED_SYMBOL_CHUNK))
            for lo in range(0, _n_codes, _PRED_SYMBOL_CHUNK)
        ]
    else:
        _blocks = [np.arange(n_total)]

    _parts = []
    for _pos in _blocks:
        if _pos.size == 0:
            continue
        _chunk = df_X if _pos.size == n_total else df_X.iloc[_pos]
        # TS 滑窗必须按 symbol 分组，否则窗口跨股票边界产生污染样本
        _pred_df = _chunk.copy()
        if _has_keys:
            _pred_df = _pred_df.sort_values(["symbol", "trade_date"])
            _idx = pd.MultiIndex.from_arrays(
                [_pred_df["symbol"].to_numpy(), _pred_df["trade_date"].to_numpy()],
                names=["instrument", "datetime"],
            )
            _X = _pred_df[features].set_axis(_idx)
            _y = pd.Series(0.0, index=_idx)
        else:
            _X = _pred_df[features]
            _y = pd.Series(0.0, index=_pred_df.index)

        # 构建 TS DataLoader 并预测
        try:
            loader, _ = _build_ts_dataloader(
                _X, _y, step_len, batch_size, shuffle=False, feat_norm=feat_norm, drop_last=False)
        except ValueError as exc:
            # 退化块（块内所有 symbol 行数 < step_len）：与全量路径这些 symbol
            # 无窗口的语义一致，记 NaN 跳过
            logger.warning("NativeTFT predict 跳过退化块（%d 行）: %s", _pos.size, exc)
            if _chunk is not df_X:
                del _chunk
            _trim()
            continue

        preds = []
        with torch.no_grad():
            for batch in loader:
                data = batch[0] if isinstance(batch, (list, tuple)) else batch
                feature = data[:, :, 0:-1].float().to(infer_device)
                pred = model(feature)
                preds.append(pred.cpu().numpy())
        raw_pred = np.concatenate(preds) if preds else np.empty(0, dtype=np.float32)

        # 与 _predict_dl 相同的 scatter 逻辑：用 original_rows 映射回块帧行
        _n_chunk = len(_pred_df)
        out = np.full(_n_chunk, np.nan, dtype=np.float32)
        if raw_pred.shape[0] > 0 and hasattr(loader.dataset, "original_rows"):
            ds = loader.dataset
            orig = ds.original_rows[ds.indices] + step_len - 1
            if orig.max() < _n_chunk and len(orig) == raw_pred.shape[0]:
                out[orig] = raw_pred
        _parts.append(pd.DataFrame({
            "symbol": _pred_df["symbol"].to_numpy(),
            "trade_date": _pred_df["trade_date"].to_numpy(),
            "pred": out,
        }))
        del loader, _X, _y, _pred_df, preds, raw_pred, out
        if _chunk is not df_X:
            del _chunk
        # 块间归还 glibc arena（同 _predict_dl，防滞留累积）
        _trim()
    if not _parts:
        return pd.DataFrame({"symbol": [], "trade_date": [], "pred": []})
    return pd.concat(_parts, ignore_index=True)
