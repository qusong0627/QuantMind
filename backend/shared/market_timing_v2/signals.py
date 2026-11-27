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
import logging
import os
import time
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

logger = logging.getLogger(__name__)

warnings.filterwarnings("ignore", category=ConvergenceWarning)
warnings.filterwarnings(
    "ignore", message="Skipping features without any observed values"
)

SNAPSHOT_PATH = STATE_DIR / "latest.json"
MLP_MODEL_PATH = STATE_DIR / "mlp_model.joblib"
MLP_HISTORY_PATH = STATE_DIR / "mlp_history.parquet"
MLP_META_PATH = STATE_DIR / "mlp_meta.json"

DEFAULT_TFT_MODEL_ID = "mdl_cn_train_20261123074451_caca88d1_fb1bd4c0"
TFT_MODEL_ID = os.environ.get("MARKET_TIMING_TFT_MODEL_ID", DEFAULT_TFT_MODEL_ID)
TFT_MODEL_DIR = Path(
    os.environ.get("MARKET_TIMING_TFT_MODEL_DIR", f"/app/models/users/default/10000001/{TFT_MODEL_ID}")
)
#: 没配 env 时按这个根目录扫模型库（多租户/多用户都扫），新用户训练出自己的模型后自动接上。
MODEL_ROOT = Path(os.environ.get("QM_MODEL_ROOT", "/app/models/users"))
#: 序列/深度模型优先当战术层；树模型按研究结论不参与大盘方向。
_DL_TYPES = {"nativetft", "transformer", "lstm", "gru", "alstm", "tcn", "tabnet", "mlp"}
#: 按「租户/用户」分别缓存解析结果（键 = "tenant/user"，无作用域时用 "*"）
_TFT_CACHE: dict[str, dict[str, Any]] = {}
_TFT_CACHE_TTL_S = 300.0


def _scope_pair(tenant_id: str | None, user_id: str | None) -> tuple[str, str] | None:
    """当前作用域：显式传入优先，其次环境变量，最后落到 default/10000001。

    默认就限定在**当前用户**，不跨租户/跨用户扫模型；
    需要全库扫描（仅调试）时设 ``MARKET_TIMING_SCAN_ALL=true``。
    """
    if tenant_id and user_id:
        return str(tenant_id), str(user_id)
    if os.environ.get("MARKET_TIMING_SCAN_ALL", "").strip().lower() == "true":
        return None
    t = os.environ.get("MARKET_TIMING_TENANT_ID", "default").strip() or "default"
    u = os.environ.get("MARKET_TIMING_USER_ID", "10000001").strip() or "10000001"
    return t, u


def _scope_key(scope: tuple[str, str] | None) -> str:
    return f"{scope[0]}/{scope[1]}" if scope else "*"
OOS_START = pd.Timestamp(os.environ.get("MARKET_TIMING_V2_OOS_START", "2025-09-29"))
PLACEBO_REPS = int(os.environ.get("MARKET_TIMING_V2_PLACEBO_REPS", "300"))
MODEL_VERSION = "market-timing-v2-shadow-1"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _write_snapshot(snapshot: dict[str, Any], path: Path | None = None) -> None:
    target = path or SNAPSHOT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(_clean(snapshot), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(target)


def _scoped_snapshot_path(scope: tuple[str, str] | None) -> Path:
    """全局快照写 latest.json；带作用域时另写一份 latest_{tenant}_{user}.json。"""
    if not scope:
        return SNAPSHOT_PATH
    return SNAPSHOT_PATH.with_name(f"latest_{scope[0]}_{scope[1]}.json")


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


def _equity(dates: pd.Series, ret: np.ndarray, pos: np.ndarray,
            cost_bps: float = 5.0) -> list[dict[str, Any]]:
    """净值曲线（起始 = 1）。前端「一眼看出效果」靠这条线。"""
    r = np.asarray(ret, dtype=float)
    x = np.asarray(pos, dtype=float)
    ok = np.isfinite(r)
    if not ok.any():
        return []
    xl = np.r_[0.0, x[:-1]]
    s = x * r - np.abs(x - xl) * cost_bps / 1e4
    eq = np.cumprod(1 + np.nan_to_num(s, nan=0.0))
    out: list[dict[str, Any]] = []
    for d, v in zip(dates, eq, strict=False):
        if not np.isfinite(v):
            continue
        out.append({"date": pd.Timestamp(d).strftime("%Y-%m-%d"),
                    "nav": round(float(v), 6)})
    return out


def _monthly(dates: pd.Series, ret: np.ndarray, pos: np.ndarray,
             cost_bps: float = 5.0) -> list[dict[str, Any]]:
    """按月复利，给前端柱状图。"""
    r = np.asarray(ret, dtype=float)
    x = np.asarray(pos, dtype=float)
    xl = np.r_[0.0, x[:-1]]
    s = x * r - np.abs(x - xl) * cost_bps / 1e4
    df = pd.DataFrame({"date": pd.to_datetime(dates).values,
                       "s": np.nan_to_num(s, nan=0.0)})
    df["m"] = df["date"].dt.strftime("%Y-%m")
    g = df.groupby("m", sort=True)["s"].apply(lambda v: float(np.prod(1 + v) - 1))
    return [{"month": k, "ret": round(v, 6)} for k, v in g.items()]


def _model_meta(d: Path) -> dict[str, Any]:
    try:
        data = json.loads((d / "metadata.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _latest_pred_date(d: Path) -> str:
    """模型最近一次日更推理的日期（pred_daily/dt=YYYYMMDD），没有就空串。"""
    root = d / "pred_daily"
    best = ""
    if root.exists():
        try:
            for name in os.listdir(root):
                if name.startswith("dt=") and len(name) == 11 and name[3:].isdigit():
                    best = max(best, name[3:])
        except OSError:
            return ""
    return best


def _scan_tft_candidates(scope: tuple[str, str] | None) -> list[dict[str, Any]]:
    """扫模型库挑战术层候选：给了作用域就只看 ``{tenant}/{user}`` 下的模型。"""
    out: list[dict[str, Any]] = []
    if not MODEL_ROOT.exists():
        return out

    model_dirs: list[Path] = []
    if scope:
        base = MODEL_ROOT / scope[0] / scope[1]
        if not base.is_dir():
            return out
        try:
            model_dirs = [p for p in base.iterdir() if p.is_dir() and p.name.startswith("mdl_")]
        except OSError:
            return out
    else:
        try:
            tenants = list(MODEL_ROOT.iterdir())
        except OSError:
            return out
        for tenant in tenants:
            if not tenant.is_dir():
                continue
            try:
                users = list(tenant.iterdir())
            except OSError:
                continue
            for user in users:
                if not user.is_dir():
                    continue
                try:
                    model_dirs.extend(
                        p for p in user.iterdir() if p.is_dir() and p.name.startswith("mdl_")
                    )
                except OSError:
                    continue

    for m in model_dirs:
        if not ((m / "pred.parquet").exists() or (m / "pred_daily").exists()):
            continue
        meta = _model_meta(m)
        market = str(meta.get("market") or "").upper()
        mtype = str(meta.get("model_type") or "").lower()
        # 树模型/线性模型按研究结论不参与大盘方向，不当战术层候选；
        # 类型缺失的（旧模型）放行，交给排序里的 DL 优先级处理。
        if mtype and mtype not in _DL_TYPES:
            continue
        try:
            mtime = m.stat().st_mtime
        except OSError:
            mtime = 0.0
        out.append(
            {
                "id": m.name,
                "dir": m,
                "market": market,
                "type": mtype,
                "latest": _latest_pred_date(m),
                "mtime": mtime,
            }
        )
    return out


def resolve_tft_model(
    tenant_id: str | None = None, user_id: str | None = None
) -> tuple[str | None, Path | None, str]:
    """决定战术层用哪个模型：env 指定 → 默认模型 → 扫**当前用户**模型库 → 无（降级 MLP 单层）。

    作用域：显式 tenant/user 优先，其次 ``MARKET_TIMING_TENANT_ID``/``MARKET_TIMING_USER_ID``。
    给了作用域就只在 ``models/users/{tenant}/{user}/`` 下扫，不会串到别的用户/租户。
    返回 ``(model_id, model_dir, source)``，source ∈ env_dir/env_id/scan/none。
    """
    scope = _scope_pair(tenant_id, user_id)
    key = _scope_key(scope)
    now = time.time()
    cached = _TFT_CACHE.get(key)
    if cached and now - float(cached["at"]) < _TFT_CACHE_TTL_S:
        return cached["id"], cached["dir"], cached["source"]

    def remember(mid: str | None, mdir: Path | None, src: str):
        _TFT_CACHE[key] = {"at": now, "id": mid, "dir": mdir, "source": src}
        return mid, mdir, src

    # 1) 显式指定目录
    env_dir = os.environ.get("MARKET_TIMING_TFT_MODEL_DIR", "").strip()
    if env_dir:
        p = Path(env_dir)
        if (p / "pred.parquet").exists() or (p / "pred_daily").exists():
            return remember(p.name, p, "env_dir")

    # 2) 显式指定 model_id（默认模型走这条），且必须在作用域内
    env_id = os.environ.get("MARKET_TIMING_TFT_MODEL_ID", "").strip() or DEFAULT_TFT_MODEL_ID
    if scope:
        hits = [MODEL_ROOT / scope[0] / scope[1] / env_id]
    else:
        hits = list(MODEL_ROOT.glob(f"*/*/{env_id}")) if MODEL_ROOT.exists() else []
    for hit in hits:
        if (hit / "pred.parquet").exists() or (hit / "pred_daily").exists():
            return remember(env_id, hit, "env_id")

    # 3) 扫当前用户的模型库：CN 优先、深度/序列类型优先、推理日期最新者胜
    cands = _scan_tft_candidates(scope)
    if cands:
        cands.sort(
            key=lambda c: (
                c["market"] == "CN",
                c["type"] in _DL_TYPES,
                c["latest"],
                c["mtime"],
            ),
            reverse=True,
        )
        best = cands[0]
        logger.info(
            "[timing-v2] 战术层自动选中模型 %s（scope=%s / %s / %s / 最新推理 %s）",
            best["id"], key, best["market"] or "?", best["type"] or "?",
            best["latest"] or "—",
        )
        return remember(best["id"], best["dir"], "scan")

    # 4) 当前用户一个都没有：降级为 MLP 单层
    return remember(None, None, "none")


def load_tft_history(
    tenant_id: str | None = None, user_id: str | None = None
) -> pd.DataFrame:
    """TFT 聚合分日序列：训练产物 test + pred_daily 增量。"""
    _, model_dir, _ = resolve_tft_model(tenant_id, user_id)
    if model_dir is None:
        return pd.DataFrame(
            columns=["date", "tft_pred_mean", "tft_pred_bottom20", "tft_pred_std", "tft_n"]
        )
    rows: list[pd.DataFrame] = []
    pred_path = model_dir / "pred.parquet"
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
    daily_root = model_dir / "pred_daily"
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


def build_snapshot(
    *,
    force: bool = False,
    history: pd.DataFrame | None = None,
    inference_info: dict[str, Any] | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    # 作用域：显式传入（API 刷新）优先；定时任务走 env（MARKET_TIMING_TENANT_ID/USER_ID）。
    # 给了作用域就只在该用户的模型库里挑战术层，快照另写一份 latest_{tenant}_{user}.json。
    scope = _scope_pair(tenant_id, user_id)
    explicit_scope = bool(tenant_id and user_id)
    snapshot_path = _scoped_snapshot_path((tenant_id, user_id)) if explicit_scope else SNAPSHOT_PATH
    if history is None:
        history = build_market_history(force=force)
    history["date"] = pd.to_datetime(history["date"])
    history = history.sort_values("date").reset_index(drop=True)

    tft = load_tft_history(tenant_id, user_id)
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
    # 找不到任何可用的战术层模型（新用户没训练过 / 没跑过日更推理）时，退化为 MLP 单层，
    # 页面照常给信号，而不是整块「未就绪」。
    mode = "tft_mlp" if df["tft_pred_mean"].notna().any() else "mlp_only"
    # MLP z 与离线研究同口径：只在 TFT/MLP 同时可用的联合窗口内做 expanding z，
    # 不用 2019-2024 的 MLP 预测去污染当前 z（两段模型输出尺度不同）。
    joint_mask = (
        df["tft_pred_mean"].notna() & df["mlp_pred"].notna()
        if mode == "tft_mlp"
        else df["mlp_pred"].notna()
    )
    df["mlp_z"] = np.nan
    df.loc[joint_mask, "mlp_z"] = _expanding_z(df.loc[joint_mask, "mlp_pred"])
    if mode == "tft_mlp":
        df["exposure"] = _rule(df["tft_z"], df["mlp_z"])
        # 影子变体（只记录，不参与生产）：MLP 用原始预测符号，不做 expanding z。
        # 依据：MLP walk-forward 长历史 2019-2026，raw pred>0 年化 +9.6%/t1.84，
        # z>0 仅 +1.1%/t0.30 —— z 把预测的绝对水准（本身带信息）减掉了。
        df["exposure_alt"] = np.where(
            (df["tft_z"] > 0) & (df["mlp_pred"] > 0),
            1.0,
            np.where(df["tft_z"] > 0, 0.5, 0.0),
        )
    else:
        df["tft_z"] = np.nan
        df["exposure"] = np.where(df["mlp_z"] > 0, 1.0, 0.0)
        df["exposure_alt"] = np.where(df["mlp_pred"] > 0, 1.0, 0.0)

    joint = (
        df.dropna(subset=["mlp_z"])
        if mode == "mlp_only"
        else df.dropna(subset=["tft_z", "mlp_z"])
    ).copy()
    if joint.empty:
        snapshot = {
            "status": "missing",
            "model_version": MODEL_VERSION,
            "generated_at": _now(),
            "message": "MLP 市场级信号不足 60 个交易日，暂时无法生成择时快照",
            "inference": inference_info or {},
        }
        _write_snapshot(snapshot, snapshot_path)
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
    tft_only = (
        _metrics(oos["fwd1"], (oos["tft_z"] > 0).astype(float))
        if mode == "tft_mlp"
        else {}
    )
    mlp_only = _metrics(oos["fwd1"], (oos["mlp_z"] > 0).astype(float))
    bh = _metrics(oos["fwd1"], np.ones(len(oos)), cost_bps=0.0)
    alt = _metrics(oos["fwd1"], oos["exposure_alt"])

    # 净值曲线 + 月度收益（前端图表口径，与 _metrics 完全一致：5bp/次）
    _d = pd.to_datetime(oos["date"]).dt.strftime("%Y-%m-%d").tolist()
    _r = oos["fwd1"].values
    equity = {
        "dates": _d,
        "combined": [p["nav"] for p in _equity(oos["date"], _r, oos["exposure"].values)],
        "tft_only": [
            p["nav"]
            for p in _equity(oos["date"], _r, (oos["tft_z"] > 0).astype(float).values)
        ],
        "buy_hold": [
            p["nav"]
            for p in _equity(oos["date"], _r, np.ones(len(oos)), cost_bps=0.0)
        ],
        "shadow_mlp_raw": [
            p["nav"] for p in _equity(oos["date"], _r, oos["exposure_alt"].values)
        ],
    }
    monthly = _monthly(oos["date"], _r, oos["exposure"].values)

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
            "exposure_alt": row["exposure_alt"],
            "tft_pred_mean": row["tft_pred_mean"],
            "mlp_pred": row["mlp_pred"],
        }
        for _, row in joint.tail(120).iterrows()
    ]

    resolved_id, _, tft_source = resolve_tft_model(tenant_id, user_id)
    if mode == "mlp_only":
        label = "进攻（MLP 单层）" if latest["exposure"] >= 1 else "防守（MLP 单层）"
    else:
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
        "mode": mode,
        "scope": _scope_key(scope),
        "tft_model": {
            "model_id": resolved_id if mode == "tft_mlp" else None,
            "source": tft_source,
            "role": "战术层" if mode == "tft_mlp" else "战术层（未启用）",
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
            "exposure_alt": float(latest["exposure_alt"]),
            "rule_alt": "影子变体：TFT_z>0 且 MLP 原始预测>0 → 100%；TFT_z>0 → 50%；其余 0%。不参与生产，仅并行观察",
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
            "shadow_mlp_raw": alt,
            "equity": equity,
            "monthly": monthly,
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
        "inference": inference_info or {"status": "not_checked"},
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
            (
                "未找到可用的战术层模型：已降级为 MLP 单层（只用 45 个市场级特征）。"
                "训练一个 CN 深度/序列模型并跑过日更推理后会自动接上，无需改配置。"
                if mode == "mlp_only"
                else (
                    "战术层模型自动选取：CN 优先、深度/序列类型优先、推理日期最新者胜；"
                    "需要固定时设置 MARKET_TIMING_TFT_MODEL_ID。"
                )
            ),
            "TFT 最新推理日可能晚于行情日；stale_trading_days 为 TFT 信号落后行情的天数；盘后任务会先尝试自动补推理。",
            "MLP 长历史 2019-2026 为 walk-forward 样本外；2025-09 起的组合规则为 OOS。",
            "树模型在本口径下大盘方向判负，不进入仓位；请勿用其聚合均值预测指数。",
        ],
    }
    _write_snapshot(snapshot, snapshot_path)
    return snapshot


def load_snapshot(
    tenant_id: str | None = None, user_id: str | None = None
) -> dict[str, Any] | None:
    """优先读调用者作用域的快照（latest_{tenant}_{user}.json），没有再回落到全局。"""
    paths: list[Path] = []
    if tenant_id and user_id:
        paths.append(_scoped_snapshot_path((str(tenant_id), str(user_id))))
    paths.append(SNAPSHOT_PATH)
    for p in paths:
        if not p.exists():
            continue
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
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
