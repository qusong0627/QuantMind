"""因子池面板缓存：逐日截面 rank_pct + zscore 双列 float32 parquet。

面板是池级分析的**价值级底座**——novelty、值级相关边、多样性熵、组合
优化全部从这里算，不再重跑因子代码；无面板的因子只参与公式级边（UI
标注「无面板」，不静默）。

落盘约定：

- 路径 ``{QM_FACTOR_POOL_PANEL_DIR:/data/rd_agent_pool/panels}/{market}/{factor_id}.parquet``
- 列：``trade_date``（YYYY-MM-DD 字符串）/ ``symbol``（**前缀式**）/
  ``rank_pct``（逐日截面 pct rank ∈ (0,1]，float32）/ ``zscore``（逐日截面
  z 分，float32）——两列都在写侧算好：novelty 用 rank_pct（见下），
  多样性矩阵用 zscore，读侧各取所需、不再变换。写侧给了
  ``forward_return`` 时附带 ``fret``（前瞻收益，float32，组合实验室的
  rank-IC 目标）；**列缺失 = 收益不可用**（旧面板），组合侧据此提示重算。
- 原子写（临时文件 + ``os.replace``）；读取失败一律 None + 告警，调用方
  按「无面板」降级。
- **两个写侧，覆盖面不同、后写覆盖先写**（2026-10-08 组合复现事故的根因）：
  ``pool_service.upsert_state``（回测完成钩子）写的是该次回测的对齐数据
  ——universe（如 csi300）× 回测窗，约 0.6~1MB/因子；``mining_pool_rebuild
  --panels`` 写因子代码全量值（全市场全历史）+ 窗口对齐的 fret（``--window-days``
  默认 750 个交易日），可达 ~80MB/因子。组合侧 ``build_dataset`` 会剔
  fret 为 NaN 的行，故读数实际收敛到「最近一次写入的覆盖面 ∩ 750 日窗」；
  同一个种子在面板被重写前后重跑，权重会变——复现前先锁数据（比对
  parquet 哈希）。

novelty 相关口径（与 ``rd_mined_materialize._max_abs_corr`` 同源）：逐日
截面 rank 后按日求皮尔逊相关、再对日均值——面板已存 rank_pct，对
rank_pct 求皮尔逊 ≡ 逐日 Spearman。每天 ≥``MIN_PAIRS_PER_DAY`` 对、
≥``MIN_SAMPLE_DAYS`` 个有效日才算数；无重叠/样本不足返回 None
（宁可漏报，不可误报）。样本日采样沿 materializer 的 linspace 约定。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_ENV_PANEL_DIR = "QM_FACTOR_POOL_PANEL_DIR"
_DEFAULT_PANEL_ROOT = Path("/data/rd_agent_pool/panels")

#: 采样日上限 / 每日最少配对数 / 最少有效日（与物化器同值，改口径两边一起改）
SAMPLE_DAYS = 60
MIN_PAIRS_PER_DAY = 20
MIN_SAMPLE_DAYS = 5

_PANEL_COLUMNS = ("trade_date", "symbol", "rank_pct", "zscore")


def panel_root() -> Path:
    """面板根目录（env 覆盖，便于测试与迁移）。"""
    env = os.environ.get(_ENV_PANEL_DIR)
    return Path(env) if env else _DEFAULT_PANEL_ROOT


def panel_path(market: str, factor_id: str) -> Path:
    return panel_root() / str(market) / f"{factor_id}.parquet"


def panel_ref(market: str, factor_id: str) -> str:
    """落库的相对标识（绝对根迁移不失效）。"""
    return f"{market}/{factor_id}.parquet"


def canonicalize_values(values: pd.Series) -> pd.DataFrame | None:
    """(datetime, instrument) 两层索引的因子值 → 长表 ``trade_date/symbol/value``。

    symbol 统一前缀式（面板与 pool 表口径一致）；NaN/inf 剔；同 (日, 股)
    重复取最后。索引不是两层 MultiIndex 时**响亮报错**（静默返回空会把
    「层序错位」伪装成「没数据」，2026-10-07 那次对齐事故的教训）。
    """
    if values is None or len(values) == 0:
        return None
    from backend.shared.stock_utils import StockCodeUtil

    if not isinstance(values.index, pd.MultiIndex) or values.index.nlevels != 2:
        raise ValueError("因子值索引不是 (datetime, instrument) 两层 MultiIndex")
    days = pd.to_datetime(values.index.get_level_values(0), errors="coerce")
    symbols = [
        StockCodeUtil.to_prefix(str(v)) for v in values.index.get_level_values(1)
    ]
    frame = pd.DataFrame(
        {
            "trade_date": days.strftime("%Y-%m-%d"),
            "symbol": symbols,
            "value": pd.to_numeric(
                pd.Series(np.asarray(values)), errors="coerce"
            ).to_numpy(),
        }
    )
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna(
        subset=["trade_date", "value"]
    )
    frame = frame.drop_duplicates(subset=["trade_date", "symbol"], keep="last")
    if frame.empty:
        return None
    return frame.reset_index(drop=True)


def write_panel(
    market: str,
    factor_id: str,
    values: pd.Series,
    forward_return: pd.Series | None = None,
) -> str | None:
    """因子值 → 面板 parquet（逐日 rank_pct + zscore，float32），返回 panel_ref。

    ``forward_return`` 给定时（回测时进程内的 r_clean）追加 ``fret`` 列
    （float32，同一 canonicalize：后缀代码/字符串日期都能对上）——组合
    实验室的 rank-IC 目标从这里取。**不传则不写该列**：列在不在就是
    「收益可不可用」的读侧判据（旧面板缺列 → 提示重算面板，绝不硬算）。

    无有效值返回 None（调用方记「无面板」）；写盘失败向上抛（调用方决定
    降级），临时文件 finally 清理。
    """
    frame = canonicalize_values(values)
    if frame is None:
        return None
    grouped = frame.groupby("trade_date")["value"]
    rank_pct = grouped.rank(pct=True)
    zscore = grouped.transform(lambda s: (s - s.mean()) / (s.std(ddof=0) + 1e-12))
    out = pd.DataFrame(
        {
            "trade_date": frame["trade_date"].to_numpy(),
            "symbol": frame["symbol"].to_numpy(),
            "rank_pct": rank_pct.to_numpy(dtype=np.float32),
            "zscore": zscore.to_numpy(dtype=np.float32),
        }
    )
    if forward_return is not None:
        ret_frame = canonicalize_values(forward_return)
        if ret_frame is not None:
            out = out.merge(
                ret_frame[["trade_date", "symbol", "value"]].rename(
                    columns={"value": "fret"}
                ),
                on=["trade_date", "symbol"],
                how="left",
            )
            out["fret"] = out["fret"].astype(np.float32)
    path = panel_path(market, factor_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        out.to_parquet(tmp, index=False)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return panel_ref(market, factor_id)


def read_panel(market: str, factor_id: str) -> pd.DataFrame | None:
    """读面板；文件缺失/损坏/缺列一律 None + 告警（按「无面板」降级）。"""
    path = panel_path(market, factor_id)
    if not path.is_file():
        return None
    try:
        frame = pd.read_parquet(path)
        missing = [c for c in _PANEL_COLUMNS if c not in frame.columns]
        if missing:
            logger.warning("面板缺列 %s: %s", missing, path)
            return None
        return frame
    except Exception as exc:  # noqa: BLE001 — 单因子面板坏不拖垮池刷新
        logger.warning("面板读取失败 %s: %s", path, exc)
        return None


def sample_days(days: Sequence[str] | set[str], n: int = SAMPLE_DAYS) -> list[str]:
    """linspace 均匀采样（与 ``rd_mined_materialize._pick_sample_days`` 同款）。"""
    seq = sorted({str(d) for d in days})
    if n <= 0 or len(seq) <= n:
        return seq
    idx = np.linspace(0, len(seq) - 1, n).round().astype(int)
    return [seq[i] for i in sorted(set(idx.tolist()))]


def pair_corr(a: pd.DataFrame, b: pd.DataFrame) -> tuple[float, int] | None:
    """两面板逐日截面秩相关 → (日均 rho, 有效日数)；样本不足返回 None。"""
    merged = a[["trade_date", "symbol", "rank_pct"]].merge(
        b[["trade_date", "symbol", "rank_pct"]],
        on=["trade_date", "symbol"],
        suffixes=("_a", "_b"),
    )
    if merged.empty:
        return None
    day = merged["trade_date"]
    xa, xb = merged["rank_pct_a"], merged["rank_pct_b"]
    xa_c = xa - xa.groupby(day).transform("mean")
    xb_c = xb - xb.groupby(day).transform("mean")
    cov = (xa_c * xb_c).groupby(day).sum()
    v_a = (xa_c**2).groupby(day).sum()
    v_b = (xb_c**2).groupby(day).sum()
    n_pairs = merged.groupby("trade_date").size()
    denom = (v_a * v_b) ** 0.5
    rho_day = (cov / denom.where(denom > 0)).where(n_pairs >= MIN_PAIRS_PER_DAY)
    n_valid = int(rho_day.notna().sum())
    if n_valid < MIN_SAMPLE_DAYS:
        return None
    return float(rho_day.mean()), n_valid


def pairwise_corr(
    frames: Mapping[str, pd.DataFrame], *, days: set[str] | None = None
) -> dict[tuple[str, str], float]:
    """两两秩相关：``{(a, b): rho}``（a < b 字典序），样本不足的对不出现。

    ``days`` 给定时先过滤面板（池刷新的 60 日采样，控制两两成本）。
    """
    ids = sorted(frames)
    if days is not None:
        frames = {k: v[v["trade_date"].isin(days)] for k, v in frames.items()}
    out: dict[tuple[str, str], float] = {}
    for i, a_id in enumerate(ids):
        for b_id in ids[i + 1 :]:
            res = pair_corr(frames[a_id], frames[b_id])
            if res is not None:
                out[(a_id, b_id)] = res[0]
    return out


def corr_matrix(frames: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """zscore 宽表（样本 × 因子）相关矩阵，缺测按成对完整观测（pandas 默认）。

    供多样性熵（``factor_quality.diversity_entropy``）与有效因子数使用；
    单因子/空输入返回空 DataFrame（调用方按不可得降级）。
    """
    if len(frames) < 2:
        return pd.DataFrame()
    merged: pd.DataFrame | None = None
    for fid in sorted(frames):
        col = frames[fid][["trade_date", "symbol", "zscore"]].rename(
            columns={"zscore": fid}
        )
        merged = (
            col
            if merged is None
            else merged.merge(col, on=["trade_date", "symbol"], how="outer")
        )
    assert merged is not None
    return merged.drop(columns=["trade_date", "symbol"]).corr()
