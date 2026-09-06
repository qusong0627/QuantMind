"""漂移诊断（P1 由 train.py 拆出，逐行搬运）。

双通道检测，消除"牛市量能膨胀"类伪警：水平 PSI（量纲敏感）+
截面 rank 位移（身份级结构漂移，对整体水平平移免疫）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _psi_single(a: np.ndarray, b: np.ndarray, n_bins: int = 10) -> float:
    """单个特征的 PSI（Population Stability Index）。

    PSI = Σ (actual% - expected%) * ln(actual% / expected%)
    以 a 为基准分布（expected），b 为待检分布（actual）。
    <0.05 无显著漂移；0.05~0.2 中等漂移；>0.2 显著漂移（compute_psi_drift 判级用）。
    """
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    a = a[np.isfinite(a)]
    b = b[np.isfinite(b)]
    if len(a) < 20 or len(b) < 20:
        return float("nan")
    # 分位数分箱（基准分布）
    edges = np.quantile(a, np.linspace(0, 1, n_bins + 1))
    edges[0], edges[-1] = -np.inf, np.inf
    # 去重相邻边界
    unique_edges = []
    for e in edges:
        if not unique_edges or e != unique_edges[-1]:
            unique_edges.append(e)
    if len(unique_edges) < 3:
        return 0.0
    bin_a = np.histogram(a, bins=unique_edges)[0].astype(np.float64)
    bin_b = np.histogram(b, bins=unique_edges)[0].astype(np.float64)
    # 防止除零/对数零
    pct_a = bin_a / max(len(a), 1)
    pct_b = bin_b / max(len(b), 1)
    pct_a = np.clip(pct_a, 1e-6, None)
    pct_b = np.clip(pct_b, 1e-6, None)
    return float(np.sum((pct_b - pct_a) * np.log(pct_b / pct_a)))


def _rank_displacement(
    train_df: pd.DataFrame,
    recent_df: pd.DataFrame,
    feat: str,
) -> float:
    """每只股票在两个阶段的截面 rank 位移均值（身份级稳定性）。

    对每只股票：训练段内按日截面 rank(pct) 后取均值 → rank_tr[s]；
    recent 段同理 → rank_rc[s]。位移 = |rank_rc[s] - rank_tr[s]|（0~1）。
    取全市场均值为该特征的截面结构漂移强度：
    - ≈0：个股相对位置稳定 → 即使水平值大幅漂移（量能膨胀）也属良性；
    - 大：大量个股截面位置重排（风格切换/板块轮动）→ 真实结构漂移。
    比"rank 直方图 PSI"更强的原因：直方图只看宏观 rank 分布（涨跌家数结构），
    身份重排时分布不变测不出；位移跟踪每只股票的个体位置，身份重排必显形。

    只统计在两端都有足够观测的股票，避免次新/停复牌（仅 1-2 天）的噪声污染均值。
    若交集为空或任一端的股票数过少 → 返回 nan（不可估计），由调用方保守处理。
    """
    min_obs = 5  # 一只股票至少要在该段出现 5 个交易日，均值才有意义
    tr_rank = train_df.groupby("trade_date")[feat].rank(pct=True)
    tr_count = train_df.groupby("symbol")[feat].transform("size")
    tr_keep = tr_count >= min_obs
    tr_mean = tr_rank[tr_keep].groupby(train_df.loc[tr_keep, "symbol"].to_numpy()).mean()

    rc_rank = recent_df.groupby("trade_date")[feat].rank(pct=True)
    rc_count = recent_df.groupby("symbol")[feat].transform("size")
    rc_keep = rc_count >= min_obs
    rc_mean = rc_rank[rc_keep].groupby(recent_df.loc[rc_keep, "symbol"].to_numpy()).mean()

    common = tr_mean.index.intersection(rc_mean.index)
    if len(common) < 50:  # 交集过小 → 不可靠，保守返回 nan
        return float("nan")
    disp = (rc_mean.loc[common] - tr_mean.loc[common]).abs()
    return float(disp.mean())


def _compute_rank_disp_all(
    train_df: pd.DataFrame,
    recent_df: pd.DataFrame,
    features: list[str],
) -> dict[str, dict]:
    """批量计算全部特征的 rank_disp，供 compute_psi_drift 使用。

    向量化：一次 groupby.rank 算所有特征的日截面 rank，再按 symbol 聚合均值，
    避免逐特征重复扫描。与单特征版 `_rank_displacement` 保持一致：过滤掉观测
    < min_obs 天的股票，避免次新/停复牌（仅 1-2 天）的噪声污染均值。

    返回 {feature: {"mean": 位移均值, "std": 个股位移横截面标准差, "n": 交集股票数}}；
    不可估计时为 {"mean": nan, "std": nan, "n": 0}。std/n 供显著性检验用
    （位移均值的标准误 ≈ std/√n，相干性结构漂移在大面板上极显著）。
    """
    min_obs = 5
    nan_entry = {"mean": float("nan"), "std": float("nan"), "n": 0}
    if not features:
        return {}
    avail = [f for f in features if f in train_df.columns and f in recent_df.columns]
    missing = {f: dict(nan_entry) for f in features if f not in avail}

    def _per_symbol_mean_rank(df: pd.DataFrame) -> pd.DataFrame:
        keep = df.groupby("symbol")[avail[0]].transform("size") >= min_obs
        sub = df.loc[keep]
        if sub.empty:
            return pd.DataFrame(index=df["symbol"].unique())
        rank_df = sub.groupby("trade_date")[avail].rank(pct=True)
        rank_df["symbol"] = sub["symbol"].to_numpy()
        return rank_df.groupby("symbol")[avail].mean()

    tr_mean = _per_symbol_mean_rank(train_df)
    rc_mean = _per_symbol_mean_rank(recent_df)

    common = tr_mean.index.intersection(rc_mean.index)
    out = dict(missing)
    if len(common) < 50:
        out.update({f: dict(nan_entry) for f in avail})
        return out
    disp = (rc_mean.loc[common] - tr_mean.loc[common]).abs()
    out.update({
        f: {
            "mean": float(disp[f].mean()),
            "std": float(disp[f].std(ddof=1)) if len(common) > 1 else float("nan"),
            "n": int(len(common)),
        }
        for f in avail
    })
    return out


def compute_psi_drift(
    df: pd.DataFrame,
    features: list[str],
    train_start: str,
    train_end: str,
    n_recent_days: int = 30,
    top_n: int = 20,
) -> dict:
    """数据漂移检测：对比训练区间 vs 最近 n 个交易日的特征分布（PSI）。

    双通道检测，消除"牛市量能膨胀"类伪警：
    - `level_psi`（原 psi 字段）：原始水平值分箱 PSI（量纲敏感）。成交额/换手等
      水平特征在牛市中整体抬升时必然重度，但树模型只走 ≤/> 分叉、标签又是截面
      rank，单纯水平平移对预测力几乎无影响——这类是"良性量纲膨胀"。
    - `rank_disp`（新增）：每只股票在训练段与 recent 段的截面 rank 位移均值。
      只反映"个股相对位置是否重排"，对整体水平平移免疫，能抓住真实的风格切换/
      板块轮动等结构漂移。
    判级以 `rank_disp` 为主：rank_disp 高 = 真实结构漂移（severe）；rank_disp 低
    但 level_psi 高 = 良性量纲膨胀（降级到 stable/medium，附 `benign_scale=True`）。
    rank_disp 不可估计（股票交集过小/观测不足）时按 level_psi 保守判级并标
    `rank_reliable=False`，绝不静默归 0（否则会掩蔽真实漂移）。

    方法学要点（2026-08 修复误报）：
    - 基准窗与 recent 窗等长且相邻（紧邻其前的 n 个交易日）。旧实现拿整个
      训练窗（数千日）均值对 30 日窗均值，30 日侧采样噪声（每股均值 rank
      标准误 ~0.09）会被误读成截面重排，优秀模型也报"严重漂移"。
    - 判级 = 显著性检验 + 历史波动包络：在全部可用日期内取多组相邻等长窗
      位移，中位数为噪声本底、最大值为包络。z = (rank_disp − 本底) /
      (个股位移 std/√n)；severe 需 z≥8、幅度≥0.10 且超包络 25%，medium 需
      z≥4、幅度≥0.05 且超包络——仅高于单点本底但仍在历史波动区间内的
      风格轮动不再误报。本底不可估计时退回固定阈值 0.10/0.25。

    返回:
        {
          "enabled": True,
          "train_start": ..., "train_end": ...,
          "recent_start": ..., "recent_end": ...,
          "drift": {"stable": N, "medium": N, "severe": N},
          "top_drift_features": [ {feature, psi(=level_psi), rank_disp, level, benign_scale, rank_reliable}, ... ],
          "max_psi": float (最大结构漂移 = max rank_disp),
          "overall": "stable" | "warning" | "severe"
        }
    """
    if df is None or df.empty or not features:
        return {"enabled": False, "reason": "no data"}

    train_mask = (df["trade_date"] >= pd.Timestamp(train_start)) & (df["trade_date"] <= pd.Timestamp(train_end))
    train_df = df[train_mask]
    # 最近 n 个交易日
    all_dates = sorted(df["trade_date"].unique())
    recent_dates = all_dates[-n_recent_days:]
    if not recent_dates:
        return {"enabled": False, "reason": "no recent dates"}
    recent_df = df[df["trade_date"].isin(recent_dates)]
    if train_df.empty or recent_df.empty:
        return {"enabled": False, "reason": "empty train or recent frame"}

    # rank_disp 基准窗：与 recent 等长、紧邻其前的交易日窗口。
    # 不能用整个训练窗做基准——训练窗均值（数千日）极稳，而 recent 窗
    # （30 日）的每股 rank 均值标准误 ~0.09，两者相减会把纯采样噪声
    # 误判为截面重排（历史误报"严重漂移"的根因）。等长相邻窗对比时
    # 两侧噪声量级相当、相互抵消，剩下的才是真实结构漂移。
    prior_dates = all_dates[:-n_recent_days]
    if len(prior_dates) >= n_recent_days:
        baseline_dates = prior_dates[-n_recent_days:]
        baseline_source = "prior_window"
    else:
        # 历史不足（如新上市数据）：退回训练窗并降低置信
        baseline_dates = [d for d in all_dates if d not in set(recent_dates)]
        baseline_source = "train_window"
    baseline_df = df[df["trade_date"].isin(baseline_dates)]
    if baseline_df.empty:
        return {"enabled": False, "reason": "empty baseline frame"}

    # 只取可用特征
    usable = [f for f in features if f in df.columns]
    # 采样控制计算量：每边最多 5 万行
    train_sample = train_df[usable].dropna(how="all").sample(min(50000, len(train_df)), random_state=42)
    recent_sample = recent_df[usable].dropna(how="all").sample(min(50000, len(recent_df)), random_state=42)
    if train_sample.empty or recent_sample.empty:
        return {"enabled": False, "reason": "empty sample"}

    # 批量算全部特征的截面结构漂移（身份级 rank 位移，等长相邻窗对比）
    rank_disp_map = _compute_rank_disp_all(baseline_df, recent_df, usable)

    # 噪声本底校准：截面 rank 有强时间自相关（动量/估值类特征尤甚），
    # 短窗均值 rank 的采样噪声很大，任何固定阈值都会随市场状态误报。
    # 在全部可用日期内（模型验证段也包含——"正常波动"应以模型见过的
    # 全部市场状态为准）取最多 4 组相邻等长窗位移：中位数作噪声本底、
    # 最大值作"历史波动包络"。近期位移超出包络才算真实漂移，仅高于
    # 单点本底属于市场正常波动区间（牛市量能/风格轮动）。
    noise_floor_map: dict[str, dict] = {}
    pool_dates = all_dates[:-n_recent_days]
    ntd = len(pool_dates)
    if ntd >= 4 * n_recent_days:
        n_samples = min(8, max(1, (ntd - 2 * n_recent_days) // n_recent_days))
        step = (ntd - 2 * n_recent_days) // max(n_samples, 1) if n_samples > 1 else 0
        sample_specs = []
        for i in range(n_samples):
            start = min(i * step, ntd - 2 * n_recent_days)
            end = start + 2 * n_recent_days
            if (start, end) not in sample_specs:
                sample_specs.append((start, end))
        per_sample_maps = []
        for start, end in sample_specs:
            win_a = pool_dates[start: start + n_recent_days]
            win_b = pool_dates[start + n_recent_days: end]
            per_sample_maps.append(_compute_rank_disp_all(
                df[df["trade_date"].isin(win_a)],
                df[df["trade_date"].isin(win_b)],
                usable,
            ))
        for f in usable:
            means = [
                m[f]["mean"] for m in per_sample_maps
                if m.get(f, {}).get("mean") is not None and np.isfinite(m[f]["mean"])
            ]
            if means:
                noise_floor_map[f] = {
                    "mean": float(np.median(means)),      # 噪声本底
                    "envelope": float(np.max(means)),     # 历史波动包络
                }
    floor_values = [
        v["mean"] for v in noise_floor_map.values()
        if v.get("mean") is not None and np.isfinite(v["mean"])
    ]
    noise_floor = float(np.median(floor_values)) if floor_values else float("nan")
    adaptive = bool(floor_values)

    results = []
    for f in usable:
        a = train_sample[f].to_numpy()
        b = recent_sample[f].to_numpy()
        level_psi = _psi_single(a, b)
        if not np.isfinite(level_psi):
            continue
        disp_stat = rank_disp_map.get(f) or {}
        rank_disp = disp_stat.get("mean")
        # rank_disp 不可估计（交集过小/数据不足）→ 保守处理：
        # 不能当良性置 0（会掩蔽真实漂移），按水平 PSI 判级并标记 unreliable
        rank_reliable = bool(rank_disp is not None and np.isfinite(rank_disp))
        z = None
        feat_floor = None
        feat_envelope = None
        if not rank_reliable:
            rank_disp = level_psi  # 用水平 PSI 兜底判级（不静默归 0）
            if rank_disp >= 0.25:
                level = "severe"
            elif rank_disp >= 0.10:
                level = "medium"
            else:
                level = "stable"
        else:
            rank_disp = float(rank_disp)
            # 判级 = 统计显著性检验，而非固定阈值/本底倍数：
            # 位移均值的标准误 ≈ 个股位移横截面 std / √n，真实的结构漂移
            # 是个股层面的相干位移，几百只股票的面板上即使幅度不大
            # （如半壁板块 +3σ 重排，均值位移仅 ~0.24）也极显著；
            # 纯采样噪声则只贡献本底量级的非相干位移，z≈0。
            # 倍数法失效的原因：结构漂移的位移有几何上界（"半升半降"
            # 重排均值位移上界 ~0.25），短窗高噪时达不到 4×本底。
            feat_stat = noise_floor_map.get(f) or {}
            feat_floor = feat_stat.get("mean")
            feat_envelope = feat_stat.get("envelope", feat_floor)
            feat_std = disp_stat.get("std")
            feat_n = disp_stat.get("n", 0)
            floor_ok = (
                adaptive
                and feat_floor is not None and np.isfinite(feat_floor) and feat_floor > 1e-4
                and feat_std is not None and np.isfinite(feat_std) and feat_std > 1e-4
                and feat_n >= 50
            )
            if floor_ok:
                # 本底自身也是单次抽样估计 → 标准误放宽 √2
                se = feat_std / (feat_n ** 0.5) * (2.0 ** 0.5)
                z = (rank_disp - feat_floor) / se
                # 双重门槛：z 显著（排除短窗采样噪声）+ 超出历史波动包络
                # （排除"高于单点本底但仍在市场正常波动区间内"的风格轮动）。
                # 幅度下限防"统计显著但经济无意义"（超大面板微小相干位移）。
                if z >= 8.0 and rank_disp >= 0.10 and rank_disp >= 1.25 * feat_envelope:
                    level = "severe"
                elif z >= 4.0 and rank_disp >= 0.05 and rank_disp >= feat_envelope:
                    level = "medium"
                else:
                    level = "stable"
            else:
                # 本底不可估计：退回固定阈值（中等 ≥0.10 / 严重 ≥0.25）
                feat_floor = None
                feat_envelope = None
                if rank_disp >= 0.25:
                    level = "severe"
                elif rank_disp >= 0.10:
                    level = "medium"
                else:
                    level = "stable"
        # 水平高但截面结构未显著漂移 = 良性量纲膨胀
        benign_scale = rank_reliable and level_psi >= 0.05 and level == "stable"
        results.append({
            "feature": f,
            "psi": round(level_psi, 4),      # 兼容原字段（水平 PSI）
            "rank_disp": round(rank_disp, 4), # 截面结构漂移（身份级 rank 位移）
            "z": round(z, 2) if z is not None and np.isfinite(z) else None,
            "noise_floor": round(feat_floor, 4) if feat_floor is not None and np.isfinite(feat_floor) else None,
            "envelope": round(feat_envelope, 4) if feat_envelope is not None and np.isfinite(feat_envelope) else None,
            "level": level,
            "benign_scale": benign_scale,     # 良性量纲膨胀标记
            "rank_reliable": rank_reliable,   # rank 位移是否可估计
        })

    if not results:
        return {"enabled": False, "reason": "no computable features"}

    results.sort(key=lambda r: (r["rank_disp"], r["psi"]), reverse=True)
    drift_counts = {"stable": 0, "medium": 0, "severe": 0}
    for r in results:
        drift_counts[r["level"]] += 1

    # overall 判定基于 rank_disp（真实结构漂移），而非水平量纲
    severe_count = drift_counts["severe"]
    medium_count = drift_counts["medium"]
    # 2026-08 下调阈值以提高灵敏度：更少的 severe/medium 数即可触发告警
    severe_ratio = severe_count / max(1, len(results))
    if severe_count >= 3 or severe_ratio >= 0.3 or (severe_count + medium_count) >= max(5, len(results) * 0.3):
        overall = "severe"
    elif severe_count >= 1 or medium_count >= 3:
        overall = "warning"
    else:
        overall = "stable"

    return {
        "enabled": True,
        "train_start": train_start,
        "train_end": train_end,
        "baseline_start": str(baseline_dates[0].date()),
        "baseline_end": str(baseline_dates[-1].date()),
        "baseline_source": baseline_source,
        "recent_start": str(recent_dates[0].date()),
        "recent_end": str(recent_dates[-1].date()),
        "noise_floor": round(noise_floor, 4) if np.isfinite(noise_floor) else None,
        "adaptive_thresholds": adaptive,
        "drift": drift_counts,
        "top_drift_features": results[:top_n],
        "max_psi": round(max(r["rank_disp"] for r in results), 4),  # 最大结构漂移（rank_disp）
        "overall": overall,
    }
