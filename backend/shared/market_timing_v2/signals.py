"""机构择时 v2：把「TFT 战术层 + MLP 战略层」算成可展示的快照。

口径（研究结论见 ``data/tmp/market_timing_20261124/REPORT.md``）：
- TFT（深度序列）只取聚合分 ``pred_mean``/``pred_bottom20``，做战术层；
- MLP（市场级特征）做战略确认；
- 树模型（LightGBM rank）不参与大盘方向，只用于截面选股。

规则：TFT_z>0 → 50% 基准暴露；TFT_z>0 且 MLP_z>0 → 100%；TFT_z<=0 → 0%。
不加杠杆、不做空、T+1 开盘执行、5bp/次。仅影子盘/研究，不构成投资建议。
"""

from __future__ import annotations

import json
import os
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import statsmodels.api as sm
from sklearn.exceptions import ConvergenceWarning
from sklearn.impute import SimpleImputer
from sklearn.neural_network import MLPRegressor
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .history import FEATURES, STATE_DIR, TARGET, build_market_history

warnings.filterwarnings("ignore", category=ConvergenceWarning)
warnings.filterwarnings(
    "ignore", message="Skipping features without any observed values"
)

SNAPSHOT_PATH = STATE_DIR / "latest.json"
MLP_MODEL_PATH = STATE_DIR / "mlp_model.joblib"
MLP_HISTORY_PATH = STATE_DIR / "mlp_history.parquet"
MLP_META_PATH = STATE_DIR / "mlp_meta.json"

TFT_MODEL_ID = os.environ.get(
    "MARKET_TIMING_TFT_MODEL_ID",
    "mdl_cn_train_20261123074451_caca88d1_fb1bd4c0",
)
TFT_MODEL_DIR = Path(
    os.environ.get(
        "MARKET_TIMING_TFT_MODEL_DIR",
        f"/app/models/users/default/10000001/{TFT_MODEL_ID}",
    )
)
OOS_START = pd.Timestamp(os.environ.get("MARKET_TIMING_V2_OOS_START", "2025-09-29"))
PLACEBO_REPS = int(os.environ.get("MARKET_TIMING_V2_PLACEBO_REPS", "300"))
MODEL_VERSION = "market-timing-v2-shadow-1"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _write_snapshot(snapshot: dict[str, Any]) -> None:
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SNAPSHOT_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(_clean(snapshot), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(SNAPSHOT_PATH)


def _clean(value: Any) -> Any:
    if value is pd.NaT:
        return None
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, float):
        return None if not np.isfinite(value) else value
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.strftime("%Y-%m-%d")
    return value


def _expanding_z(series: pd.Series, min_periods: int = 60) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce")
    mu = s.expanding(min_periods=min_periods).mean().shift(1)
    sd = s.expanding(min_periods=min_periods).std().shift(1)
    return (s - mu) / sd.replace(0, np.nan)


def _metrics(
    ret: pd.Series | np.ndarray, pos: pd.Series | np.ndarray, cost_bps: float = 5.0
) -> dict[str, Any]:
    r = np.asarray(ret, dtype=float)
    x = np.asarray(pos, dtype=float)
    n = min(len(r), len(x))
    r, x = r[:n], x[:n]
    xl = np.r_[0.0, x[:-1]]
    s = x * r - np.abs(x - xl) * cost_bps / 1e4
    mask = np.isfinite(s)
    s = s[mask]
    if len(s) < 20:
        return {}
    eq = np.cumprod(1 + s)
    peak = np.maximum.accumulate(eq)
    ann = float(s.mean() * 252)
    vol = float(s.std(ddof=0) * np.sqrt(252))
    t = float(
        sm.OLS(s, np.ones(len(s)))
        .fit(cov_type="HAC", cov_kwds={"maxlags": 5})
        .tvalues[0]
    )
    return {
        "n": int(len(s)),
        "ann": ann,
        "vol": vol,
        "sharpe": ann / vol if vol > 0 else None,
        "mdd": float((eq / peak - 1).min()),
        "hit": float((s > 0).mean()),
        "turnover": float(np.abs(x - xl)[mask].mean()),
        "exposure": float(x[mask].mean()),
        "t": t,
        "total": float(eq[-1] - 1),
    }


def _rule(tft_z: pd.Series | np.ndarray, mlp_z: pd.Series | np.ndarray) -> np.ndarray:
    t = np.asarray(tft_z, dtype=float)
    m = np.asarray(mlp_z, dtype=float)
    return np.where((t > 0) & (m > 0), 1.0, np.where(t > 0, 0.5, 0.0))


def load_tft_history() -> pd.DataFrame:
    """TFT 聚合分日序列：训练产物 test + pred_daily 增量。"""
    rows: list[pd.DataFrame] = []
    pred_path = TFT_MODEL_DIR / "pred.parquet"
    if pred_path.exists():
        df = pd.read_parquet(pred_path, columns=["trade_date", "pred", "split"])
        df = df[df["split"] == "test"].copy()
        df["date"] = pd.to_datetime(df["trade_date"]).dt.normalize()
        g = df.groupby("date")["pred"]
        rows.append(
            pd.DataFrame(
                {
                    "tft_pred_mean": g.mean(),
                    "tft_pred_bottom20": g.apply(
                        lambda s: float(s[s <= s.quantile(0.2)].mean())
                    ),
                    "tft_pred_std": g.std(),
                    "tft_n": g.size(),
                }
            ).reset_index()
        )
    daily_root = TFT_MODEL_DIR / "pred_daily"
    if daily_root.exists():
        for name in sorted(os.listdir(daily_root)):
            if not name.startswith("dt="):
                continue
            ds = name[3:]
            p = daily_root / name / "data.parquet"
            if len(ds) != 8 or not ds.isdigit() or not p.exists():
                continue
            d = pd.read_parquet(p, columns=["score"])
            s = pd.to_numeric(d["score"], errors="coerce").dropna()
            if s.empty:
                continue
            rows.append(
                pd.DataFrame(
                    [
                        {
                            "date": pd.Timestamp(ds),
                            "tft_pred_mean": float(s.mean()),
                            "tft_pred_bottom20": float(s[s <= s.quantile(0.2)].mean()),
                            "tft_pred_std": float(s.std()),
                            "tft_n": int(s.size),
                        }
                    ]
                )
            )
    if not rows:
        return pd.DataFrame(
            columns=[
                "date",
                "tft_pred_mean",
                "tft_pred_bottom20",
                "tft_pred_std",
                "tft_n",
            ]
        )
    out = pd.concat(rows, ignore_index=True).sort_values("date")
    out["date"] = pd.to_datetime(out["date"])
    return out.drop_duplicates("date", keep="last").reset_index(drop=True)


def _fit_mlps(X: np.ndarray, y: np.ndarray) -> list[Any]:
    models = []
    for seed in (11, 23, 47):
        pipe = make_pipeline(
            SimpleImputer(strategy="median"),
            StandardScaler(),
            MLPRegressor(
                hidden_layer_sizes=(32, 16),
                alpha=0.05,
                learning_rate_init=1e-3,
                max_iter=400,
                early_stopping=True,
                n_iter_no_change=20,
                validation_fraction=0.15,
                random_state=seed,
            ),
        )
        pipe.fit(X, y)
        models.append(pipe)
    return models


def _predict_mlps(models: list[Any], X: np.ndarray) -> np.ndarray:
    return np.mean([m.predict(X) for m in models], axis=0)


def train_mlp_artifacts(
    history: pd.DataFrame, *, force: bool = False
) -> tuple[pd.DataFrame, list[Any]]:
    """训练/加载 MLP：walk-forward 历史（2019+）+ 最终模型（预测最新）。"""
    if (
        not force
        and MLP_MODEL_PATH.exists()
        and MLP_HISTORY_PATH.exists()
        and MLP_META_PATH.exists()
    ):
        meta = json.loads(MLP_META_PATH.read_text())
        hist = pd.read_parquet(MLP_HISTORY_PATH)
        hist["date"] = pd.to_datetime(hist["date"])
        trained_at = pd.Timestamp(meta.get("trained_at", "1970-01-01"))
        if trained_at.tzinfo is not None:
            trained_at = trained_at.tz_convert(None)
        if (
            meta.get("features") == FEATURES
            and (pd.Timestamp.now() - trained_at).days <= 45
            and not hist.empty
            and hist["date"].max() >= history["date"].max() - pd.Timedelta(days=10)
        ):
            return hist, joblib.load(MLP_MODEL_PATH)

    data = history.dropna(subset=[TARGET]).sort_values("date").reset_index(drop=True)
    full = history.sort_values("date").reset_index(drop=True)
    rows: list[pd.DataFrame] = []
    for year in range(2019, int(full["date"].max().year) + 1):
        test_start = pd.Timestamp(f"{year}-01-01")
        test_end = pd.Timestamp(f"{year}-12-31")
        train = data[data["date"] < test_start - pd.Timedelta(days=10)]
        # 预测覆盖测试年全部交易日（含尚未兑现标签的尾部），与离线研究口径一致
        test = full[(full["date"] >= test_start) & (full["date"] <= test_end)]
        if len(train) < 300 or len(test) < 20:
            continue
        # 与离线研究口径一致：只用验证年之前的数据训练，留出最近 12 个月做 early-stop 缓冲
        va_start = test_start - pd.Timedelta(days=380)
        va = train[train["date"] >= va_start]
        tr = train[train["date"] < va_start]
        if len(va) < 60:
            tr = train.iloc[:-60]
        models = _fit_mlps(tr[FEATURES].to_numpy(float), tr[TARGET].to_numpy(float))
        pred = _predict_mlps(models, test[FEATURES].to_numpy(float))
        rows.append(pd.DataFrame({"date": test["date"].to_numpy(), "mlp_pred": pred}))
    hist = (
        pd.concat(rows, ignore_index=True)
        .sort_values("date")
        .drop_duplicates("date", keep="last")
        if rows
        else pd.DataFrame(columns=["date", "mlp_pred"])
    )
    hist.to_parquet(MLP_HISTORY_PATH, index=False)

    # 最终模型：只用标签已知的行（最新 10 天不参与训练）
    known = data[data["date"] <= data["date"].max() - pd.Timedelta(days=10)]
    models = _fit_mlps(known[FEATURES].to_numpy(float), known[TARGET].to_numpy(float))
    joblib.dump(models, MLP_MODEL_PATH)
    meta = {
        "version": MODEL_VERSION,
        "trained_at": _now(),
        "train_start": known["date"].min().strftime("%Y-%m-%d"),
        "train_end": known["date"].max().strftime("%Y-%m-%d"),
        "features": FEATURES,
        "n_train": int(len(known)),
    }
    MLP_META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    return hist, models


def build_snapshot(*, force: bool = False) -> dict[str, Any]:
    history = build_market_history(force=force)
    history["date"] = pd.to_datetime(history["date"])
    history = history.sort_values("date").reset_index(drop=True)

    tft = load_tft_history()
    mlp_hist, mlp_models = train_mlp_artifacts(history, force=force)
    mlp_hist["date"] = pd.to_datetime(mlp_hist["date"])

    # 最新日期用最终模型预测（历史 z 用 walk-forward，保证 OOS）；缺失特征交给 pipeline 填补
    latest_rows = history.tail(60)[["date"] + FEATURES]
    if not latest_rows.empty:
        latest_pred = pd.DataFrame(
            {
                "date": latest_rows["date"].to_numpy(),
                "mlp_pred_latest": _predict_mlps(
                    mlp_models, latest_rows[FEATURES].to_numpy(float)
                ),
            }
        )
    else:
        latest_pred = pd.DataFrame(columns=["date", "mlp_pred_latest"])

    df = (
        history.merge(tft, on="date", how="left")
        .merge(mlp_hist, on="date", how="left")
        .merge(latest_pred, on="date", how="left")
    )
    df["mlp_pred"] = df["mlp_pred"].fillna(df["mlp_pred_latest"])
    df["tft_z"] = _expanding_z(df["tft_pred_mean"])
    # MLP z 与离线研究同口径：只在 TFT/MLP 同时可用的联合窗口内做 expanding z，
    # 不用 2019-2024 的 MLP 预测去污染当前 z（两段模型输出尺度不同）。
    joint_mask = df["tft_pred_mean"].notna() & df["mlp_pred"].notna()
    df["mlp_z"] = np.nan
    df.loc[joint_mask, "mlp_z"] = _expanding_z(df.loc[joint_mask, "mlp_pred"])
    df["exposure"] = _rule(df["tft_z"], df["mlp_z"])

    joint = df.dropna(subset=["tft_z", "mlp_z"]).copy()
    if joint.empty:
        snapshot = {
            "status": "missing",
            "model_version": MODEL_VERSION,
            "generated_at": _now(),
            "message": "TFT 或 MLP 信号不足 60 个交易日，暂时无法生成择时快照",
        }
        _write_snapshot(snapshot)
        return snapshot

    latest = joint.iloc[-1]
    tft_as_of = tft["date"].max() if not tft.empty else None
    mlp_as_of = df.loc[df["mlp_pred"].notna(), "date"].max()
    data_as_of = history["date"].max()
    stale_days = (
        int((history["date"] > latest["date"]).sum()) if pd.notna(latest["date"]) else 0
    )

    oos = joint[joint["date"] >= OOS_START].copy()
    combined = _metrics(oos["fwd1"], oos["exposure"])
    tft_only = _metrics(oos["fwd1"], (oos["tft_z"] > 0).astype(float))
    mlp_only = _metrics(oos["fwd1"], (oos["mlp_z"] > 0).astype(float))
    bh = _metrics(oos["fwd1"], np.ones(len(oos)), cost_bps=0.0)

    # MLP 长历史（2019+）单独表现
    mlp_long = df.dropna(subset=["mlp_pred", "fwd1"])
    mlp_long = mlp_long[mlp_long["date"] >= "2019-01-01"]
    mlp_long_m = _metrics(mlp_long["fwd1"], (mlp_long["mlp_pred"] > 0).astype(float))
    mlp_long_bh = _metrics(mlp_long["fwd1"], np.ones(len(mlp_long)), cost_bps=0.0)

    # 组合规则 placebo：20 日分块置换 TFT/MLP 信号
    placebo_p = None
    if len(oos) >= 60:
        rng = np.random.default_rng(20261124)
        obs = combined.get("sharpe")
        if obs is not None:
            vals = []
            tz = oos["tft_z"].to_numpy(float)
            mz = oos["mlp_z"].to_numpy(float)
            ret = oos["fwd1"].to_numpy(float)

            def _block(x: np.ndarray) -> np.ndarray:
                blocks = [x[i : i + 20] for i in range(0, len(x), 20)]
                return np.concatenate(
                    [blocks[i] for i in rng.permutation(len(blocks))]
                )[: len(x)]

            for _ in range(max(50, PLACEBO_REPS)):
                pos = _rule(_block(tz), _block(mz))
                vals.append(_metrics(ret, pos).get("sharpe"))
            vals = np.asarray([v for v in vals if v is not None], dtype=float)
            if len(vals):
                placebo_p = float((1 + (vals >= obs).sum()) / (1 + len(vals)))

    history_series = [
        {
            "date": row["date"].strftime("%Y-%m-%d"),
            "tft_z": row["tft_z"],
            "mlp_z": row["mlp_z"],
            "exposure": row["exposure"],
            "tft_pred_mean": row["tft_pred_mean"],
            "mlp_pred": row["mlp_pred"],
        }
        for _, row in joint.tail(120).iterrows()
    ]

    label = (
        "进攻（满额）"
        if latest["exposure"] >= 1
        else ("战术进攻（半额）" if latest["exposure"] > 0 else "防守（空仓）")
    )
    status = "ok" if stale_days <= 5 else "stale"
    if stale_days > 10:
        status = "missing"

    snapshot = {
        "status": status,
        "model_version": MODEL_VERSION,
        "generated_at": _now(),
        "data_as_of": data_as_of,
        "tft_as_of": tft_as_of,
        "mlp_as_of": mlp_as_of,
        "as_of": latest["date"],
        "stale_trading_days": stale_days,
        "market": "CN",
        "tft_model": {
            "model_id": TFT_MODEL_ID,
            "role": "战术层",
            "pred_mean": latest.get("tft_pred_mean"),
            "pred_bottom20": latest.get("tft_pred_bottom20"),
            "z": latest["tft_z"],
        },
        "mlp_model": {
            "model": "market_mlp_v2",
            "role": "战略确认层",
            "pred": latest["mlp_pred"],
            "z": latest["mlp_z"],
        },
        "signal": {
            "tft_z": latest["tft_z"],
            "mlp_z": latest["mlp_z"],
            "exposure": latest["exposure"],
            "label": label,
            "rule": "TFT_z>0 → 基准 50%；TFT_z>0 且 MLP_z>0 → 100%；TFT_z≤0 → 0%",
            "execution": "HS300 ETF/期货，T+1 开盘执行，5bp/次；不加杠杆、不做空",
            "confidence": "shadow",
        },
        "structure": {
            "breadth": latest.get("breadth"),
            "gap_ie_20": latest.get("gap_ie_20"),
            "gap_hz_20": latest.get("gap_hz_20"),
            "vol20": latest.get("vol20"),
            "vol20_ann": (latest.get("vol20") * np.sqrt(252))
            if pd.notna(latest.get("vol20"))
            else None,
            "bp_capw_pct": latest.get("bp_capw_pct"),
            "ep_capw_pct": latest.get("ep_capw_pct"),
            "med_pe_pct": latest.get("med_pe_pct"),
            "amount_ratio20": latest.get("amt_ratio20"),
        },
        "performance": {
            "window": {
                "start": OOS_START.strftime("%Y-%m-%d"),
                "end": latest["date"].strftime("%Y-%m-%d"),
                "n": int(len(oos)),
            },
            "combined": combined,
            "tft_only": tft_only,
            "mlp_only": mlp_only,
            "buy_hold": bh,
            "placebo_p": placebo_p,
            "mlp_long_history": {
                "window": {
                    "start": "2019-01-01",
                    "end": mlp_long["date"].max().strftime("%Y-%m-%d")
                    if not mlp_long.empty
                    else None,
                },
                "strategy": mlp_long_m,
                "buy_hold": mlp_long_bh,
            },
        },
        "history": history_series,
        "roles": [
            {
                "model": "TFT 深度序列",
                "role": "战术层",
                "use": "pred_mean / pred_bottom20 聚合分",
                "status": "参与",
            },
            {
                "model": "MLP 市场级",
                "role": "战略确认层",
                "use": "趋势/广度/估值/裂口/两融特征",
                "status": "参与",
            },
            {
                "model": "LightGBM 树模型",
                "role": "截面选股",
                "use": "只做个股排序，不参与大盘方向",
                "status": "不参与",
            },
        ],
        "notes": [
            "影子盘/研究口径，不构成投资建议；仅用于模型体检与仓位风险预算。",
            "TFT 最新推理日可能晚于行情日；stale_trading_days 为 TFT 信号落后行情的天数。",
            "MLP 长历史 2019-2026 为 walk-forward 样本外；2025-09 起的组合规则为 OOS。",
            "树模型在本口径下大盘方向判负，不进入仓位；请勿用其聚合均值预测指数。",
        ],
    }
    _write_snapshot(snapshot)
    return snapshot


def load_snapshot() -> dict[str, Any] | None:
    if not SNAPSHOT_PATH.exists():
        return None
    try:
        return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


if __name__ == "__main__":
    snap = build_snapshot(force=True)
    print(
        json.dumps(
            _clean(
                {
                    k: snap.get(k)
                    for k in (
                        "status",
                        "as_of",
                        "tft_as_of",
                        "mlp_as_of",
                        "stale_trading_days",
                    )
                }
            ),
            ensure_ascii=False,
        )
    )
    print("signal", json.dumps(_clean(snap.get("signal", {})), ensure_ascii=False))
    print("snapshot", SNAPSHOT_PATH)
