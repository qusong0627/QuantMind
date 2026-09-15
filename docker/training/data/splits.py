"""数据集切分与数组化（P2 由 train.py 拆出，逐行搬运）。

训练信号在 T 日收盘后生成，最早在下一个交易日执行。训练、回测和线上
forward label 都应使用相同的 T+1 执行口径（_EXECUTION_LAG_DAYS）。
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger("quantmind.train")

_EXECUTION_LAG_DAYS = 1


def _split_data(df: pd.DataFrame, cfg: dict) -> tuple:
    """数据切分：显式 split 优先于 val_ratio。返回 (train_df, val_df, test_df)。

    时间序列切分必须保证 train < val < test，严禁 test=val（经典数据泄漏）。
    """
    model_cfg = cfg.get("model", {})

    def _frame_range_text(frame: pd.DataFrame) -> str:
        if frame.empty:
            return "EMPTY"
        return f"{frame['trade_date'].min().date()}~{frame['trade_date'].max().date()}"

    split_cfg = cfg.get("split", {})
    if split_cfg.get("valid"):
        valid_start_str, valid_end_str = split_cfg["valid"]
        train_start_str, train_end_str = split_cfg["train"]
        requested_train = f"{train_start_str}~{train_end_str}"
        requested_val = f"{valid_start_str}~{valid_end_str}"
        # train 必须有下界，否则会吃进 train_start 之前的数据；
        # 且 train_end 必须早于 valid_start，否则三段重叠造成泄漏
        if pd.Timestamp(train_end_str) >= pd.Timestamp(valid_start_str):
            raise RuntimeError(
                f"split.train end ({train_end_str}) must be strictly before "
                f"split.valid start ({valid_start_str}); overlapping segments "
                "leak validation data into training."
            )
        train_df = df[
            (df["trade_date"] >= pd.Timestamp(train_start_str)) &
            (df["trade_date"] <= pd.Timestamp(train_end_str))
        ].copy()
        val_df   = df[
            (df["trade_date"] >= pd.Timestamp(valid_start_str)) &
            (df["trade_date"] <= pd.Timestamp(valid_end_str))
        ].copy()
        if split_cfg.get("test"):
            test_start_str, test_end_str = split_cfg["test"]
            if pd.Timestamp(test_start_str) <= pd.Timestamp(valid_end_str):
                raise RuntimeError(
                    f"split.test start ({test_start_str}) must be strictly after "
                    f"split.valid end ({valid_end_str}); overlapping segments "
                    "make early stopping and final evaluation share data."
                )
            requested_test = f"{test_start_str}~{test_end_str}"
            test_df = df[
                (df["trade_date"] >= pd.Timestamp(test_start_str)) &
                (df["trade_date"] <= pd.Timestamp(test_end_str))
            ].copy()
        else:
            raise RuntimeError(
                "split.test is required when split.valid is configured. "
                "test=val is a classic data leakage pattern — early stopping "
                "and model selection would both happen on test data, "
                "inflating all reported metrics. "
                "Please add a 'test' section to the split config, e.g.:\n"
                "  split:\n"
                "    train: ['2020-01-01', '2023-12-31']\n"
                "    valid: ['2024-01-01', '2024-06-30']\n"
                "    test:  ['2024-07-01', '2024-12-31']"
            )
        logger.info(f"Split mode: train~{split_cfg['train'][1]}  val {valid_start_str}~{valid_end_str}")
    else:
        val_ratio = float(model_cfg.get("val_ratio") or 0.15)
        dates = sorted(df["trade_date"].unique())
        if not dates:
            raise RuntimeError("No rows available for split after preprocessing. 请检查训练时间窗口与特征快照覆盖范围。")
        # 三段式切分：train | val | test，避免 test=val 的数据泄漏
        test_ratio = val_ratio / 2.0
        val_start_idx = int(len(dates) * (1 - val_ratio))
        test_start_idx = int(len(dates) * (1 - test_ratio))
        val_start = dates[val_start_idx]
        test_start = dates[test_start_idx]
        train_df = df[df["trade_date"] < val_start].copy()
        val_df   = df[(df["trade_date"] >= val_start) & (df["trade_date"] < test_start)].copy()
        test_df  = df[df["trade_date"] >= test_start].copy()
        train_start = pd.Timestamp(df["trade_date"].min()).date()
        train_end = (pd.Timestamp(val_start) - pd.Timedelta(days=1)).date()
        requested_train = f"{train_start}~{train_end}"
        requested_val = f"{pd.Timestamp(val_start).date()}~{pd.Timestamp(test_start).date() - pd.Timedelta(days=1)}"
        requested_test = f"{pd.Timestamp(test_start).date()}~{pd.Timestamp(df['trade_date'].max()).date()}"
        logger.info(
            f"val_ratio mode (3-way split): train[{len(train_df)}]~{pd.Timestamp(val_start).date() - pd.Timedelta(days=1)}"
            f"  val[{len(val_df)}] {pd.Timestamp(val_start).date()}~{pd.Timestamp(test_start).date() - pd.Timedelta(days=1)}"
            f"  test[{len(test_df)}] {pd.Timestamp(test_start).date()}~"
        )

    # ── Embargo：标签是未来 horizon 日收益，train 末尾样本的标签落在 val 区间内 ──
    # 不隔离会让 val/test 的价格信息经标签渗回 train。裁掉每段尾部 horizon 个交易日。
    _horizon = max(1, int((cfg.get("label", {}) or {}).get("target_horizon_days") or 1))
    _embargo_days = _horizon + _EXECUTION_LAG_DAYS
    if _embargo_days > 0:
        def _embargo(frame: pd.DataFrame, name: str) -> pd.DataFrame:
            if frame.empty:
                return frame
            days = sorted(frame["trade_date"].unique())
            if len(days) <= _embargo_days:
                logger.warning(
                    "Embargo skipped for %s: only %d trading days <= label span %d",
                    name, len(days), _embargo_days,
                )
                return frame
            cutoff = days[-_embargo_days]
            trimmed = frame[frame["trade_date"] < cutoff].copy()
            logger.info(
                "Embargo %s: dropped last %d trading days (%d -> %d rows)",
                name, _embargo_days, len(frame), len(trimmed),
            )
            return trimmed

        train_df = _embargo(train_df, "train")
        val_df = _embargo(val_df, "val")

    train_df = train_df.reset_index(drop=True)
    val_df = val_df.reset_index(drop=True)
    test_df = test_df.reset_index(drop=True)
    if train_df.empty or val_df.empty or test_df.empty:
        available_range = "EMPTY"
        if not df.empty:
            available_range = f"{df['trade_date'].min().date()}~{df['trade_date'].max().date()}"
        raise RuntimeError(
            "Dataset split contains empty segment. "
            f"available={available_range}; "
            f"train={len(train_df)}({_frame_range_text(train_df)}) requested={requested_train}; "
            f"val={len(val_df)}({_frame_range_text(val_df)}) requested={requested_val}; "
            f"test={len(test_df)}({_frame_range_text(test_df)}) requested={requested_test}. "
            "请调整 train/valid/test 时间窗口，确保三段均与可用数据重叠。"
        )
    return train_df, val_df, test_df


def _prepare_arrays(
    train_df: pd.DataFrame,
    val_df: pd.DataFrame,
    features: list[str],
    prep_cfg: dict | None = None,
    extra_frames: list[pd.DataFrame] | None = None,
) -> tuple:
    """计算 fill_values 并转换为 numpy 数组。

    prep_cfg 启用时（`preprocessing.enabled=true`），对特征做截面预处理：
    per (trade_date, feature) 中位数填充 + 分位缩尾 + 截面 Z-score。
    类别特征（ind_code_l1/l2）不参与变换（保持原始编码）。
    extra_frames：一并**原地**预处理的帧（test 帧与全窗口帧）——2026-09-15 修正：
    此前只处理 train/val，测试期与全窗口预测（pred.pkl）在原始特征上进行，与训练/
    推理口径不一致。截面统计按日独立（切分按日期，单日横截面完整落在同一段内），
    故各帧分别处理与其自身口径一致。
    返回 (fill_values, X_train, y_train, X_val, y_val, _fill_fn)。
    """
    import math
    from preprocessing import cross_sectional_preprocess

    prep_cfg = prep_cfg or {}
    prep_enabled = bool(prep_cfg.get("enabled", False))
    _exclude = {"ind_code_l1", "ind_code_l2"}
    _prep_feats = [f for f in features if f not in _exclude]

    if prep_enabled and _prep_feats:
        _winsor = bool(prep_cfg.get("winsor", True))
        # 训练页精细开关（均为可选，缺省与历史口径逐位一致）：
        #   winsor_quantiles [lo, hi]（如 [0.025, 0.975]）、fill=median|zero、
        #   standardize=zscore|rank。非法值一律回退默认，不阻断训练。
        _quantiles = None
        _raw_q = prep_cfg.get("winsor_quantiles")
        if isinstance(_raw_q, (list, tuple)) and len(_raw_q) == 2:
            try:
                _lo, _hi = float(_raw_q[0]), float(_raw_q[1])
                if 0.0 <= _lo < _hi <= 1.0:
                    _quantiles = (_lo, _hi)
            except (TypeError, ValueError):
                _quantiles = None
        _fill_mode = str(prep_cfg.get("fill") or "median").strip().lower()
        _std_mode = str(prep_cfg.get("standardize") or "zscore").strip().lower()
        _prep_kw = {
            "enabled": True,
            "winsor": _winsor,
            "fill_value": _fill_mode,
            "standardize": _std_mode,
        }
        if _quantiles is not None:
            _prep_kw["quantiles"] = _quantiles
        # 关键修正：test 帧与全窗口帧此前未预处理，测试指标/全窗口预测失真。
        # preprocess 已就地化，对传入帧原地处理（不影响返回值签名）。
        # 多模型循环（train_multi_models）复用同一批帧：用 attrs 配置指纹去重，
        # 同配置不重复处理（否则第二次调用会把已处理帧再 zscore 一遍）。
        _prep_sig = repr(sorted(_prep_kw.items())) + "|" + repr(sorted(_prep_feats))

        def _apply_prep(frame: pd.DataFrame) -> pd.DataFrame:
            if frame.attrs.get("qm_prep_sig") == _prep_sig:
                return frame
            out = cross_sectional_preprocess(frame, _prep_feats, **_prep_kw)
            out.attrs["qm_prep_sig"] = _prep_sig
            return out

        train_df = _apply_prep(train_df)
        val_df = _apply_prep(val_df)
        for _extra in extra_frames or []:
            _apply_prep(_extra)
        logger.info(
            "Cross-sectional preprocessing enabled: %d features "
            "(exclude %s, fill=%s, standardize=%s, winsor=%s%s)",
            len(_prep_feats),
            sorted(_exclude & set(features)),
            _fill_mode,
            _std_mode,
            _winsor,
            f", quantiles={_quantiles}" if _quantiles else "",
        )

    # 逐列取中位数：train_df[features] 整块取值会临时复制 ~8GB（7.4M×273 float32）
    fill_values_raw = {c: train_df[c].median() for c in features}
    fill_values = {k: (0.0 if (isinstance(v, float) and math.isnan(v)) else v) for k, v in fill_values_raw.items()}
    _fill_vec = np.array([fill_values[c] for c in features], dtype=np.float32)

    def _fill(frame: pd.DataFrame) -> np.ndarray:
        # 预分配一块 float32 矩阵逐列搬入并原地填 NaN。原实现
        # 「frame[features].copy() → 逐列 astype/fillna → to_numpy(float32)」
        # 同时存在 2~3 份整块副本（7.7M×273 时每份 ≈8GB），是预处理段峰值主因。
        arr = np.empty((len(frame), len(features)), dtype=np.float32)
        for i, c in enumerate(features):
            col = frame[c].to_numpy(dtype=np.float32, copy=True)
            mask = np.isnan(col)
            if mask.any():
                col[mask] = _fill_vec[i]
            arr[:, i] = col
        return arr

    X_train = _fill(train_df)
    y_train = train_df["label"].astype("float32").to_numpy()
    X_val = _fill(val_df)
    y_val = val_df["label"].astype("float32").to_numpy()
    return fill_values, X_train, y_train, X_val, y_val, _fill
