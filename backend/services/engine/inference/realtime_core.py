"""实时推理共享核心（T-P6-08/09）：周期装配 + 摘要 + 账本条目——服务与回放器唯一实现。

**为什么独立成模块（结构优化）**：实时服务（在线）与回放验收器（离线）必须走**同一份**
周期装配与同一份摘要定义，否则"diff=0"的验收本身失去意义（两处各写=自证循环）。
服务逐周期把「输入锚（每标的快照水印 cuts）+ 矩阵摘要 + 分数摘要」落账本；
回放器按 cuts 从 L0.5 归档重建同样的引擎状态与矩阵，摘要逐周期比对。

**精确复现语义（2026-09-17 定稿）**：实时服务每周期对每标的只喂**当前快照键值一帧**
（不是喂帧流）——回放器同法：每周期取「最后一条 ts ≤ cut 的归档帧」喂入。
引擎状态逐周期累积路径两侧完全一致 → 摘要可逐周期严格相等（非近似）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from backend.shared.feature_incremental import TIER_COLUMNS, is_missing


def digits(symbol: str) -> str:
    """后缀式/前缀式/纯数字 → 纯数字（**只用于连因子源/快照 parquet 的行键**）。

    ⚠️ **不得当身份键**（审计 M2）：``000001.SH``（上证指数，热集常驻）与
    ``000001.SZ``（平安银行）会折叠成同一字符串——指数吃到股票的基线行、账本
    两行无法区分、engine_signal_scores 冲突键把两行并成一行。身份一律走
    :func:`identity`；本函数只允许出现在「连到天然以纯数字为键的源行」这一步，
    且映射回身份空间必须经 :func:`identity`。
    """
    s = str(symbol or "").strip().upper()
    for suf in (".SH", ".SZ", ".BJ"):
        if s.endswith(suf):
            return s[: -len(suf)]
    if s[:2] in ("SH", "SZ", "BJ") and s[2:].isdigit():
        return s[2:]
    return s


def identity(symbol: str) -> str:
    """任意形态 → **后缀身份**（``600036.SH``；市场段是身份的一部分，审计 M2）。

    热集/矩阵行序/账本/回放指针/engine_signal_scores 冲突键统一用本形态。后缀式、
    前缀式、纯数字都收敛到后缀式（纯数字按代码前缀推断市场：6/9→SH、0/3/2→SZ、
    4/8/92→BJ）——纯数字推断对**股票**正确，指数（000001.SH）在股票因子源里因此
    正确地查不到行，而不是串吃平安银行。识别不了的形态原样返回（实时链仅 CN，
    热集不出现其他形态）。

    **场内基金**（热集实测含 159518.SZ、501018.SH——ETF/LOF 不属股票前缀规则、
    ``to_suffix`` 对纯数字基金码原样返回）：补一条基金码推断——``15/16/18`` 开头
    → SZ（深市基金），``5`` 开头 → SH（沪市基金 50/51/52/56/58）。范围刻意收窄
    （不含 ``1`` 全段：``11/12`` 是沪/深可转债号段，不猜市场，走到下面原样返回）。
    """
    s = str(symbol or "").strip().upper()
    if not s:
        return ""
    try:
        from backend.shared.stock_utils import StockCodeUtil

        out = StockCodeUtil.to_suffix(s)
        if out != s:
            return str(out)
        # 基金码（股票规则未覆盖）：深市 15x/16x/18x、沪市 5xxxxx
        if len(s) == 6 and s.isdigit() and (s[:2] in ("15", "16", "18") or s[0] == "5"):
            return f"{s}.{'SZ' if s[0] == '1' else 'SH'}"
        return str(out) if out else s
    except Exception:  # noqa: BLE001 - 识别失败不丢符号（原样当身份用）
        return s


def snapshot_key(symbol: str) -> str | None:
    """任意形态 → ``market:snapshot:{prefix.lower()}``（与 collector 写入面同构）。"""
    s = str(symbol or "").strip().upper()
    if "." in s:
        code, _, mk = s.partition(".")
        if mk in ("SH", "SZ", "BJ") and code.isdigit():
            return f"market:snapshot:{mk.lower()}{code}"
        return None
    if s[:2] in ("SH", "SZ", "BJ") and s[2:].isdigit():
        return f"market:snapshot:{s[:2].lower()}{s[2:]}"
    if s.isdigit() and len(s) == 6:
        mk = "SH" if s[0] in "69" else ("BJ" if s[0] in "48" else "SZ")
        return f"market:snapshot:{mk.lower()}{s}"
    return None


def snapshot_watermark(snap: dict[str, Any] | None) -> float | None:
    """快照消费水印：快照键内的 timestamp/ts（回放锚）。"""
    if not snap:
        return None
    for key in ("timestamp", "ts"):
        raw = snap.get(key)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


@dataclass
class CycleResult:
    """单周期装配结果（含回放对账所需的全部摘要物质）。"""

    x: np.ndarray
    symbols: list[str]  # 后缀身份（identity()，000001.SH≠000001.SZ），行序=矩阵行序（账本据此重放）
    cols: list[str]
    scores: np.ndarray
    ranks: np.ndarray
    ready: int
    missing: int
    overridden: int
    cuts: list[float | None] = field(default_factory=list)
    seq_len: int = 1  # 1 = 平铺单帧；>1 = 时序窗口帧数（矩阵为 [n, seq, d]）
    window_missing: int = 0  # 时序模式下前帧有缺（整帧缺席）的标的数——仅时序模式可非零


def sequence_len_of(meta: dict[str, Any]) -> int:
    """模型窗口长度（唯一实现；在线服务与回放器共用）。

    非时序模型 → 1。时序模型依次认 ``dl_params.dl_step_len`` → ``dl_params.step_len``
    → ``input_spec.tensor_shape[1]``（批量模板只认 ``dl_step_len`` 且默认 20——训练实际
    写的是 ``step_len``，本函数把它也认上）。全部来源缺失**显式报错**，不静默拿默认值：
    窗口长度猜错 = 拿错形状的矩阵喂模型，分数全是噪声。
    """
    if not meta.get("is_sequence_model"):
        return 1
    dl = meta.get("dl_params") or {}
    shape = (meta.get("input_spec") or {}).get("tensor_shape")
    candidates: list[Any] = [dl.get("dl_step_len"), dl.get("step_len")]
    if isinstance(shape, (list, tuple)) and len(shape) > 2:
        candidates.append(shape[1])
    for raw in candidates:
        try:
            if raw is not None and int(raw) > 0:
                return int(raw)
        except (TypeError, ValueError):
            continue
    raise ValueError(
        "时序模型（is_sequence_model=true）metadata 缺 step_len："
        "dl_params.dl_step_len / dl_params.step_len / input_spec.tensor_shape[1] 均不可用"
    )


def apply_feat_norm(x: np.ndarray, feat_norm: dict[str, Any] | None) -> np.ndarray:
    """批量 DL 标准化口径的镜像（``templates/inference_parquet.py::_apply_feat_norm``）。

    训练集 mean/std 标准化（std=0 视作 1）后 NaN/Inf 归零——批量时序推理就是这么把
    缺值喂进模型的。与批量模板的唯一有意差异：无 feat_norm 时批量原样返回（NaN 留在
    分数里由下游 skip），实时链归零——链上消费方（排名/落库/JSON 状态镜像）都吃不消
    NaN 分数。镜像等价由 ``test_realtime_sequence_window.py`` 与模板函数逐值对拍钉住。
    """
    fn = feat_norm if isinstance(feat_norm, dict) else {}
    if not (fn.get("mean") and fn.get("std")):
        return np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    mean = np.asarray(fn["mean"], dtype=np.float32)
    std = np.asarray(fn["std"], dtype=np.float32)
    std = np.where(std == 0, 1.0, std)
    x = (x - mean) / std
    return np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)


def matrix_digest(x: np.ndarray, cols: list[str], model_version: str) -> str:
    """输入矩阵摘要：模型版本 + 列序 + float32 原始字节（行序由 symbols 锚定）。"""
    h = hashlib.sha256()
    h.update(str(model_version).encode("utf-8"))
    h.update(b"\x1f")
    h.update(",".join(cols).encode("utf-8"))
    h.update(b"\x1f")
    h.update(np.ascontiguousarray(x, dtype="<f4").tobytes())
    return h.hexdigest()


def scores_digest(scores: np.ndarray) -> str:
    """分数摘要：round(6) 后 float64 字节（消除无关浮点尾差噪声的伪差异）。"""
    arr = np.round(np.asarray(scores, dtype=float), 6)
    return hashlib.sha256(arr.astype("<f8").tobytes()).hexdigest()


def ledger_entry(
    result: CycleResult,
    *,
    ts: float,
    run_id: str,
    model_version: str,
    override: tuple[str, ...] | list[str] | set[str] = (),
) -> dict[str, Any]:
    """账本条目：输入锚（symbols 顺序 + 每标的 cuts）+ 双摘要 + 诊断量（回放对账唯一依据）。

    ``override`` 必须随账落盘——回放要用**当时**的覆盖白名单，而非当前配置（配置会变）。
    """
    return {
        "ts": round(float(ts), 3),
        "run_id": run_id,
        "model_version": model_version,
        "n": len(result.symbols),
        "ready": result.ready,
        "missing": result.missing,
        "overridden": result.overridden,
        "seq_len": int(result.seq_len),
        "window_missing": int(result.window_missing),
        "symbols": list(result.symbols),
        "cuts": result.cuts,
        "override": sorted(str(c) for c in override),
        "x_mean": round(float(np.mean(result.x)), 8),
        "scores_mean": round(float(np.mean(result.scores)), 8),
        "x_digest": matrix_digest(result.x, result.cols, model_version),
        "scores_digest": scores_digest(result.scores),
    }


def live_coverage(
    hot: list[str], snapshots: dict[str, dict[str, Any]], *, now: float
) -> float:
    """实时快照覆盖率（0~1）：**可用**快照的占比——ts 缺失或超 STALE 线一律不算。

    口径与消费方新鲜度同源（``shared/freshness.classify_age``；阈值仅在该模块读取）。
    用于发布闸门：覆盖率不足时**不发布伪实时信号**（基线 T-1 打分不等于实时）。
    """
    if not hot:
        return 0.0
    from backend.shared.freshness import UNAVAILABLE, classify_age, quote_policy

    policy = quote_policy()
    usable = 0
    for sym in hot:
        snap = snapshots.get(sym)
        if not snap:
            continue
        try:
            ts = float(snap.get("timestamp") or snap.get("ts") or 0.0)
        except (TypeError, ValueError):
            continue
        if ts > 1e12:
            ts /= 1000.0
        if ts <= 0:
            continue
        if (
            classify_age(
                now - ts,  # 不夹取负值：未来偏斜由 classify_age 的容差守卫裁定

                fresh_within_s=policy.fresh_within_s,
                stale_within_s=policy.stale_within_s,
            )
            != UNAVAILABLE
        ):
            usable += 1
    return usable / len(hot)


def compute_cycle(
    *,
    session: Any,
    input_name: str,
    cols: list[str],
    fill: dict[str, Any],
    model_version: str,
    hot: list[str],
    snapshots: dict[str, dict[str, Any]],
    baseline: dict[str, dict[str, Any]],
    histories: dict[str, Any],
    override: set[str],
    engine: Any,
    bootstrapped: set[str],
    window: dict[str, list[dict[str, Any] | None]] | None = None,
    seq_len: int = 1,
    feat_norm: dict[str, Any] | None = None,
) -> CycleResult:
    """单周期装配：引导(每标的一次) → 喂当前快照一帧 → live 覆盖 → 矩阵 → 推理 → 排名。

    在线服务与离线回放共用本函数——任何装配逻辑改动两侧同时生效（结构纪律）。

    **身份口径（审计 M2）**：``baseline/histories/window/snapshots`` 的键与
    ``CycleResult.symbols`` 一律**后缀身份**（:func:`identity`）——``000001.SH``
    （指数）与 ``000001.SZ``（平安银行）绝不折叠成同一键；``digits()`` 只用于
    源行键转换。引擎句柄仍按入参 ``sym`` 原样（同一账本代次两侧一致即可）。

    时序模型（``seq_len > 1``，如 NativeTFT；``metadata`` 经 :func:`sequence_len_of` 裁定）：
    矩阵升为三维 ``[n, seq_len, d]``——前 seq_len-1 帧来自 ``window``（:func:`load_window_for_model`
    按因子日对齐，整帧缺席为 None），最后一帧 = 基线行 + live 覆盖（覆盖只作用于最后一帧：
    历史帧是既成事实）。装配口径**镜像批量 DL 路径**（``templates/inference_parquet.py``）：
    **不填 fill_values**，统一 :func:`apply_feat_norm`（训练集 mean/std）后 NaN/Inf 归零。
    批量对整窗不足的标的是弃打分（NaN），实时不弃——热集池固定、分数行必须对齐；缺帧只
    影响该标的自身分数并记入 ``window_missing``（如实计数，不装没发生）。
    """
    seq = max(int(seq_len or 1), 1)
    n_cols = len(cols)
    if seq > 1:
        x = np.empty((len(hot), seq, n_cols), dtype=np.float32)
    else:
        x = np.empty((len(hot), n_cols), dtype=np.float32)
    ready = missing = overridden = window_missing = 0
    symbols_norm: list[str] = []
    cuts: list[float | None] = []
    for i, sym in enumerate(hot):
        ident = identity(sym)
        symbols_norm.append(ident)
        row = dict(baseline.get(ident) or {})
        if sym not in bootstrapped:
            hist = histories.get(ident)
            if hist is not None and len(hist):
                try:
                    engine.bootstrap(sym, hist)
                except Exception:  # noqa: BLE001 - 单标的引导失败不拖垮周期
                    pass
            bootstrapped.add(sym)
        snap = snapshots.get(ident)
        cuts.append(snapshot_watermark(snap))
        if snap:
            engine.on_snapshot(sym, snap)
        live = None
        if override:
            try:
                live = engine.compute(sym)
            except Exception:  # noqa: BLE001 - 单标的失败不拖垮周期
                live = None
        vals: list[float | None] = []
        for col in cols:
            val = row.get(col)
            if live is not None and col in override:
                lv = live.get(col)
                if lv is not None and np.isfinite(lv):
                    val = lv
                    overridden += 1
            vals.append(float(val) if not is_missing(val) else None)
        if seq > 1:
            frames = list((window or {}).get(ident) or [])
            gap = False
            for j in range(seq - 1):
                frame = frames[j] if j < len(frames) else None
                if frame is None:
                    gap = True
                    frame = {}
                for k, col in enumerate(cols):
                    v = frame.get(col)
                    x[i, j, k] = float(v) if not is_missing(v) else np.nan
            if gap:
                window_missing += 1
            for k, val in enumerate(vals):
                if val is None:
                    missing += 1
                    x[i, seq - 1, k] = np.nan
                else:
                    x[i, seq - 1, k] = val
        else:
            for k, col in enumerate(cols):
                val = vals[k]
                if val is None:
                    val = fill.get(col, 0.0)
                    missing += 1
                x[i, k] = float(val)
        if row:
            ready += 1
    if seq > 1:
        x = apply_feat_norm(x, feat_norm)
    out = session.run(None, {input_name: x})[0]
    scores = np.asarray(out, dtype=float).reshape(-1)
    order = np.argsort(-scores)
    ranks = np.empty(len(scores), dtype=int)
    ranks[order] = np.arange(1, len(scores) + 1)
    return CycleResult(
        x=x, symbols=symbols_norm, cols=list(cols), scores=scores, ranks=ranks,
        ready=ready, missing=missing, overridden=overridden, cuts=cuts,
        seq_len=seq, window_missing=window_missing,
    )


# ── 基线加载（服务与回放共用；路径可注入）────────────────────────────


def load_baseline_bundle(
    symbols: list[str],
    day: date,
    *,
    parquet_path: str | Path,
    cols: list[str],
    history_len: int = 45,
) -> dict[str, Any]:
    """T-1 行 + 价格历史（单次 parquet 读取）。返回 {rows, history}；缺文件返回空。

    rows/history 的键 = **后缀身份**（``identity(源行纯数字)``——源 parquet 行键是
    纯数字，指数代码在股票源里忠实缺席而非串到同数字股票行；审计 M2）。
    """
    import pandas as pd

    path = Path(parquet_path)
    if not path.is_file() or not cols:
        return {"rows": {}, "history": {}}
    raw = ["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount"]
    # 列裁剪必须与 parquet **实际 schema 取交集**：模型 feature_columns 里的 parquet 未收录列
    # （如 JQ110_* ——2026-09-17 实测硬报 ArrowInvalid）不得进 read_parquet，缺失值统一走
    # compute_cycle 的 fill_values 兜底（与快照覆盖白名单同纪律：口径不符不硬来）。
    requested = raw + [c for c in cols if c not in raw]
    try:
        import pyarrow.parquet as _pq

        available = set(_pq.ParquetFile(path).schema_arrow.names)
    except Exception:  # noqa: BLE001 - schema 读取失败回落全量请求（由 read_parquet 报真错）
        available = set()
    if available:
        ordered = list(dict.fromkeys(requested))
        selected = [c for c in ordered if c in available]
    else:
        selected = list(dict.fromkeys(requested))
    df = pd.read_parquet(path, columns=selected)
    df = df[df["symbol"].isin([digits(s) for s in symbols])]
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    past = df[df["trade_date"].dt.date < day]
    if past.empty:
        return {"rows": {}, "history": {}}
    latest = past["trade_date"].max()
    rows: dict[str, dict[str, Any]] = {}
    history: dict[str, Any] = {}
    for sym, g in past.groupby("symbol"):
        g = g.sort_values("trade_date")
        ident = identity(str(sym))
        history[ident] = g[raw].tail(history_len).reset_index(drop=True)
        last = g[g["trade_date"] == latest]
        if not last.empty:
            row = last.iloc[-1]
            rows[ident] = {c: row.get(c) for c in cols}
    return {"rows": rows, "history": history}


QUANTDB_DATA_SOURCE = "quantdb_factors"


def _load_qfq_history(
    symbols: list[str],
    start: str,
    end_upper: str,
    *,
    history_len: int = 45,
    hub: Any = None,
) -> dict[str, Any]:
    """前复权日线历史（增量引擎引导专用，唯一口径）。

    为什么不用因子源自带 OHLCV：l1_l2 的行情补给是 daily_backward（后复权），而实时
    快照价=原始价≈前复权尾段——混用会让 mom_* 全族系统性偏负（2026-09-17 实测
    600036：40.60/57.08-1=-0.289 假收益）。批量侧特征基于 daily_forward（前复权），
    实时必须同口径（daily_forward 直读，配额与量纲纪律见 quantdb_hub）。

    ``end_upper`` = 日线可取的上界（**day-1**，严格防盘中未收盘行泄漏）；实际结束日
    放宽到「≤ end_upper 的最新 K 线可用日」——因子源比 K 线晚 1–2 个交易日，若锚在
    因子日上，引擎重启后首日的 mom 短窗基准会偏旧（历史环补不到最近完整交易日）。
    """
    if hub is None:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        hub = QuantDBDataHub.get_instance()
    day0 = date.fromisoformat(str(start)[:10])
    day1 = date.fromisoformat(str(end_upper)[:10])
    avail = hub._partition_dates("1_kline_data/daily_forward", None, day1)
    if avail:
        last = avail[-1]
        day1 = date(int(last[:4]), int(last[4:6]), int(last[6:]))
    df = hub.fetch_daily_kline_batch(symbols, day0, day1, adjust="qfq")
    history: dict[str, Any] = {}
    if df is None or not len(df):
        return history
    raw = ["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount"]
    keep = [c for c in raw if c in df.columns]
    wanted = {identity(s) for s in symbols}
    work = df.assign(_norm=df["symbol"].map(identity))
    work = work[work["_norm"].isin(wanted)]
    for ident, g in work.groupby("_norm"):
        history[str(ident)] = (
            g.sort_values("trade_date")[keep].tail(history_len).reset_index(drop=True)
        )
    return history


def _quantdb_reader_for_meta(meta: dict[str, Any]) -> Any:
    from backend.services.engine.data_platform.quantdb_factor_reader import (
        QuantDBFactorReader,
    )
    from backend.shared.quantdb_paths import resolve_pinned_data_dir

    # 死 pin（训练节点路径，服务端不存在）→ None，由读取器回落本机根；
    # 直接把死 pin 交进去会静默返回 0 列，实时打分全列走 fill。
    pinned = resolve_pinned_data_dir(meta.get("quantdb_dir"))
    market = str((meta.get("context") or {}).get("market") or "").strip().upper() or None
    return QuantDBFactorReader(str(pinned) if pinned else None, market=market)


def load_baseline_quantdb(
    symbols: list[str],
    day: date,
    *,
    meta: dict[str, Any],
    cols: list[str],
    reader: Any | None = None,
    history_hub: Any | None = None,
    history_len: int = 45,
) -> dict[str, Any]:
    """QuantDB 直读基线（2026-09-17 迁移）：取数面与批量推理 ``load_date_data`` 同源。

    背景：模型 metadata 绑定 ``data_source=quantdb_factors`` 时，批量链已走
    ``QuantDBFactorReader`` 直读原始因子源；实时基线此前仍读遗留 ``model_features_{year}``
    快照（该文件 2026-08 起停更/格式污染 → 「T-1」实际停在 08-24，属另一种伪实时）。

    - rows：**最近可用因子日**（``available_dates`` ≤ day-1）的模型特征行；
      仅请求该源真实存在的列——缺列统一缺席（``compute_cycle`` 交 fill 兜底，
      与 parquet 路径同纪律：口径不符不硬来）；
    - history：近 ``history_len`` 个可用交易日的**前复权**日线（daily_forward 直读；
      增量引擎引导用，引导仅发生一次）——与快照价/批量特征同口径（后复权混用会致
      mom_* 系统性偏负，2026-09-17 实测）。
    直读失败**显式抛出**（由调用方记 last_error），绝不静默回落 parquet 防混源。
    """
    from backend.services.engine.data_platform.quantdb_factor_reader import (
        split_features_by_availability,
    )

    if reader is None:
        reader = _quantdb_reader_for_meta(meta)
    source = str(meta.get("factor_source") or "l1_l2_factors")
    dates = reader.available_dates(source, end=(day - timedelta(days=1)).isoformat())
    if not dates:
        return {"rows": {}, "history": {}}
    latest = dates[-1]

    mapping = {
        str(k): str(v) for k, v in (meta.get("factor_field_sources") or {}).items()
    }
    # 跨库组合（库:列）逐库判可用：拿锚库列集过滤会把副库特征整体静默丢掉，
    # 实时推理写出全 NaN 行而不报错。缺失特征交 fill 兜底（与 parquet 路径同纪律）。
    requested, _missing = split_features_by_availability(
        reader,
        dict.fromkeys(cols),
        mapping,
        anchor=source,
    )
    wanted = {identity(s) for s in symbols}

    rows: dict[str, dict[str, Any]] = {}
    if requested:
        day_df = reader.read_day(
            source,
            features=requested,
            trade_date=latest,
            feature_sources=mapping or None,
        )
        day_df = day_df.assign(_norm=day_df["symbol"].map(identity))
        day_df = day_df[day_df["_norm"].isin(wanted)]
        for ident, g in day_df.groupby("_norm"):
            last = g.iloc[-1]
            rows[str(ident)] = {c: last.get(c) for c in cols}

    history: dict[str, Any] = {}
    hist_dates = dates[-max(1, int(history_len)):]
    history = _load_qfq_history(
        symbols,
        hist_dates[0],
        (day - timedelta(days=1)).isoformat(),  # 上界=day-1（K 线可补到比因子日新）
        history_len=history_len,
        hub=history_hub,
    )
    return {"rows": rows, "history": history}


def load_baseline_for_model(
    symbols: list[str],
    day: date,
    *,
    meta: dict[str, Any],
    cols: list[str],
    parquet_path: str | Path,
    history_len: int = 45,
    reader: Any | None = None,
    history_hub: Any | None = None,
) -> dict[str, Any]:
    """基线加载唯一分派（在线服务/回放验收共用）：取数面由模型 ``data_source`` 绑定裁定。

    - ``quantdb_factors`` → QuantDB 直读（与批量链同源）；
    - 其余（遗留快照模型）→ ``model_features_{year}.parquet``（不可变快照语义，保持不动）。
    """
    if str(meta.get("data_source") or "").strip() == QUANTDB_DATA_SOURCE:
        return load_baseline_quantdb(
            symbols,
            day,
            meta=meta,
            cols=cols,
            reader=reader,
            history_hub=history_hub,
            history_len=history_len,
        )
    return load_baseline_bundle(
        symbols, day, parquet_path=parquet_path, cols=cols, history_len=history_len
    )


def load_window_quantdb(
    symbols: list[str],
    day: date,
    *,
    meta: dict[str, Any],
    cols: list[str],
    step_len: int,
    reader: Any | None = None,
) -> dict[str, Any]:
    """时序模型窗口（批量 ``load_window_data`` 的同源镜像）：D 之前 step_len-1 个因子日 × 请求标的。

    D = 最近可用因子日（≤ day-1，与 :func:`load_baseline_quantdb` 同一锚点）——服务把
    基线的「当前帧」当窗口最后一帧，本函数只取**前 step_len-1 帧**。帧序 = 因子日序
    （旧→新），与批量 ``tail(step_len)`` 的窗口切片对齐；某标的某日缺行 → 该帧为 None
    （整帧缺席，交 :func:`compute_cycle` 记 ``window_missing``）。因子日不足 step_len-1 天
    （新库/次新上市）时**前部补 None**，绝不把缺失帧挤到新端——错位一帧整窗就移了位。

    取数源/列过滤/副库映射与基线完全同规（``split_features_by_availability`` + 库:列映射）。
    返回 ``{"dates": [因子日或 None × (step_len-1)], "frames": {后缀身份: [帧或 None]}}``
    （键经 :func:`identity`——指数与同数字股票绝不串帧；审计 M2）。
    """
    from backend.services.engine.data_platform.quantdb_factor_reader import (
        split_features_by_availability,
    )

    frames_n = max(int(step_len) - 1, 0)
    if frames_n == 0:
        return {"dates": [], "frames": {}}
    if reader is None:
        reader = _quantdb_reader_for_meta(meta)
    source = str(meta.get("factor_source") or "l1_l2_factors")
    dates = reader.available_dates(source, end=(day - timedelta(days=1)).isoformat())
    if not dates:
        return {"dates": [None] * frames_n, "frames": {}}
    prior = [str(d) for d in dates[:-1][-frames_n:]]
    pad = frames_n - len(prior)
    dates_out: list[str | None] = [None] * pad + prior

    mapping = {
        str(k): str(v) for k, v in (meta.get("factor_field_sources") or {}).items()
    }
    requested, _missing = split_features_by_availability(
        reader,
        dict.fromkeys(cols),
        mapping,
        anchor=source,
    )
    wanted = {identity(s) for s in symbols}
    by_date: dict[str, dict[str, dict[str, Any]]] = {d: {} for d in prior}
    if requested and prior:
        try:
            df = reader.read_range(
                source,
                features=requested,
                feature_sources=mapping or None,
                start=prior[0],
                end=prior[-1],
                include_ohlcv=False,
            )
        except Exception as exc:  # noqa: BLE001 - 与基线同纪律：直读失败显式抛出记 last_error
            raise RuntimeError(f"QuantDB 时序窗口读取失败: {exc}") from exc
        df = df.assign(
            _norm=df["symbol"].map(identity),
            _date=df["trade_date"].astype(str).str[:10],
        )
        df = df[df["_norm"].isin(wanted)]
        for (ident, dt), g in df.groupby(["_norm", "_date"]):
            slot = by_date.get(str(dt))
            if slot is None:
                continue
            last = g.iloc[-1]
            slot[str(ident)] = {c: last.get(c) for c in cols}
    return {
        "dates": dates_out,
        "frames": {
            s: [None] * pad + [by_date[d].get(s) for d in prior] for s in wanted
        },
    }


def load_window_for_model(
    symbols: list[str],
    day: date,
    *,
    meta: dict[str, Any],
    cols: list[str],
    step_len: int,
    reader: Any | None = None,
) -> dict[str, Any]:
    """窗口加载唯一分派（在线服务/回放验收共用）：时序窗口目前只支持 quantdb 直读源。

    遗留 ``model_features_{year}.parquet`` 是「单文件=最新日」快照，没有按日整表语义，
    拼不出逐日对齐的窗口 → **显式报错**，绝不静默拿错帧喂模型。
    """
    if str(meta.get("data_source") or "").strip() == QUANTDB_DATA_SOURCE:
        return load_window_quantdb(
            symbols, day, meta=meta, cols=cols, step_len=step_len, reader=reader
        )
    raise RuntimeError(
        f"时序模型（step_len={step_len}）需要 quantdb_factors 基线，"
        f"当前 data_source={meta.get('data_source')!r} 不支持窗口拼装"
    )


def effective_override(whitelist: tuple[str, ...] | list[str], cols: list[str]) -> set[str]:
    """白名单 ∩ TIER ∩ 模型列（唯一裁定入口）。"""
    return set(whitelist) & set(TIER_COLUMNS) & set(cols)


def ledger_json(entry: dict[str, Any]) -> str:
    return json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
