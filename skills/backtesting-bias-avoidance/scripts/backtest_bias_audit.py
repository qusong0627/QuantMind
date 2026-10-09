#!/usr/bin/env python3
"""回测偏差审计 — PBO(CSCV)/DSR/夏普显著性(HAC)/成本冲击/前视量化，自包含实现。

来源：quantskills/skill-backtesting-bias-avoidance。该仓库 GitHub license 元数据为
NOASSERTION（LICENSE 文件为截断的 GPL-3.0 声明文本）。本文件是按方法论重写的独立
实现，**未复制源仓库代码**；统计口径（CSCV/PBO 与 Deflated Sharpe，Bailey &
López de Prado；HAC 夏普显著性，Lo 2002；线性+平方根律成本）沿用公开文献口径。

模式：
  --sanity            结构校准（合成已知真值）：纯噪声试验矩阵 → PBO≈0.5；强信号矩阵 → PBO 低
  --demo              合成 AR(1) 价格 + MA(快,慢) 参数网格全流程（含前视偏差量化）
  --input trials.csv  试验矩阵：每列一条策略的每期收益（列名=策略名；首列可为 YYYY-MM-DD 日期）
  --quantdb           本地真实指数 MA 网格全流程（CN/HK/US，读 QuantDB index_daily）

依赖：numpy（数学）；--quantdb 另需 pyarrow（读 parquet），在 quantmind 容器内运行。
CSV 与 sanity 模式不再扣费（输入视为已含各自成本口径）；demo/quantdb 模式自带成本建模。

用法：
  python3 backtest_bias_audit.py --sanity
  python3 backtest_bias_audit.py --demo --out /tmp/demo.json
  python3 backtest_bias_audit.py --input trials.csv --out report.json
  python3 backtest_bias_audit.py --quantdb --market CN --symbol 000300.SH \
      --start 2016-01-01 --end 2026-09-30 --fast 5:40:5 --slow 20:120:10 --cost-bps 3 \
      --out /data/reports/backtest-bias-audit/cn_000300_2016_2026.json
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
from datetime import datetime, timedelta
from itertools import combinations
from pathlib import Path

ANNUAL = 252
EULER_GAMMA = 0.5772156649015329
DEFAULT_FAST = "5:40:5"
DEFAULT_SLOW = "20:120:10"
DEFAULT_COST_BPS = 3.0        # A股撮合口径：万3 单边（3bps），双边各自计
DEFAULT_IMPACT = 0.0005       # 平方根律系数（无 ADV 的风格化近似），可 --impact 0 关闭
COST_SWEEP_BPS = [0.0, 1.0, 2.0, 3.0, 5.0, 10.0]
PBO_HIGH = 0.50
PBO_MEDIUM = 0.25
DSR_SIGNIFICANT = 0.95
LOOKAHEAD_HIGH = 0.50         # 泄漏-干净的毛夏普差 > 0.5 触发高
MIN_BETS_OOS = 35
MIN_NEFF = 60
DISCLAIMER = "本报告基于公开数据与规则化分析生成，仅供研究参考，不构成任何投资建议。"

try:
    import numpy as np
except ImportError:  # pragma: no cover
    raise SystemExit(
        "需要 numpy。在 quantmind 容器内运行：docker exec -w /app quantmind python3 <本脚本> ...") from None


# ------------------------------------------------------------------ 小数学

def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def norm_ppf(p: float) -> float:
    """标准正态分位数（Acklam 近似 + Halley 精修，精度 ~1e-15）。"""
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0,1)")
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
    plow, phigh = 0.02425, 1.0 - 0.02425
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        x = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    elif p > phigh:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        x = -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    else:
        q = p - 0.5
        r = q * q
        x = (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
            (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
    e = norm_cdf(x) - p
    u = e * math.sqrt(2.0 * math.pi) * math.exp(x * x / 2.0)
    return x - u / (1.0 + x * u / 2.0)


def nw_auto_lag(T: int) -> int:
    """Newey-West(1994) 自动带宽 L = floor(4*(T/100)^(2/9))，CSV 模式默认。"""
    return max(1, int(math.floor(4.0 * (T / 100.0) ** (2.0 / 9.0))))


def _skew(x: np.ndarray) -> float:
    m, s = float(x.mean()), float(x.std())
    return 0.0 if s == 0 else float(((x - m) ** 3).mean() / s ** 3)


def _kurtosis(x: np.ndarray) -> float:
    m, s = float(x.mean()), float(x.std())
    return 3.0 if s == 0 else float(((x - m) ** 4).mean() / s ** 4)


def fin(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _jsonable(obj):
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, (np.floating, np.integer)):
        return _jsonable(obj.item())
    return obj


# ------------------------------------------------------------- 统计层

def sharpe_per_period(r: np.ndarray) -> float:
    s = float(r.std())
    return float(r.mean()) / s if s > 0 else 0.0


def sharpe_ann(r: np.ndarray) -> float:
    return sharpe_per_period(r) * math.sqrt(ANNUAL)


def hac_sharpe_stats(r: np.ndarray, lag: int) -> dict:
    """夏普 HAC 显著性（Newey-West/Bartlett 核，Lo 2002 口径）：t、95%CI、方差膨胀因子、N_eff。"""
    r = np.asarray(r, float)
    r = r[np.isfinite(r)]
    T = len(r)
    out = {"sr_ann": None, "t": None, "ci95": [None, None], "T": T,
           "neff": None, "vif": None, "lag": None}
    if T < 5 or float(r.std()) == 0.0:
        return out
    mu = float(r.mean())
    L = max(1, min(int(lag), T // 3))
    d = r - mu
    g0 = float(d @ d) / T
    s = g0
    for k in range(1, L + 1):
        s += 2.0 * (1.0 - k / (L + 1.0)) * float(d[k:] @ d[:-k]) / T
    s, g0 = max(s, 1e-18), max(g0, 1e-18)
    sd = math.sqrt(g0)
    vif = s / g0
    se_mu = math.sqrt(s / T)
    sr, se_sr = mu / sd * math.sqrt(ANNUAL), se_mu / sd * math.sqrt(ANNUAL)
    out.update(sr_ann=sr, t=mu / se_mu, ci95=[sr - 1.96 * se_sr, sr + 1.96 * se_sr],
               neff=T / vif, vif=vif, lag=L)
    return out


def segment_stats(net: np.ndarray, pos: np.ndarray) -> dict:
    """按持仓段（连续同仓位为一笔）聚合 → 独立下注口径。"""
    segs = []
    cur_pos, cur_sum = None, 0.0
    for p, x in zip(pos, net, strict=True):
        if cur_pos is None or p != cur_pos:
            if cur_pos not in (None, 0.0):
                segs.append((cur_pos, cur_sum))
            cur_pos, cur_sum = p, 0.0
        cur_sum += float(x)
    if cur_pos not in (None, 0.0):
        segs.append((cur_pos, cur_sum))
    out = {"n": len(segs), "sr_ann": None, "t": None, "ci95": [None, None]}
    if len(segs) < 3:
        return out
    v = np.array([x for _, x in segs])
    mu, sd = float(v.mean()), float(v.std(ddof=1))
    if sd == 0.0:
        return out
    t = mu / (sd / math.sqrt(len(segs)))
    bpy = len(segs) / (len(net) / ANNUAL)          # 每年下注次数
    sr = mu / sd * math.sqrt(bpy)
    se = math.sqrt(bpy) / math.sqrt(len(segs))
    out.update(sr_ann=sr, t=t, ci95=[sr - 1.96 * se, sr + 1.96 * se])
    return out


def metrics(r: np.ndarray) -> dict:
    r = np.asarray(r, float)
    r = r[np.isfinite(r)]
    out = {"ann_return": None, "sharpe": None, "sortino": None, "mdd": None,
           "calmar": None, "hit_rate": None}
    if len(r) == 0 or float(r.std()) == 0.0:
        return out
    ann = float(r.mean()) * ANNUAL
    eq = np.cumprod(1.0 + r)
    pk = np.maximum.accumulate(eq)
    mdd = float(((eq - pk) / pk).min())
    dn = r[r < 0]
    out.update(ann_return=ann, sharpe=sharpe_ann(r),
               sortino=(float(r.mean()) / float(dn.std()) * math.sqrt(ANNUAL)) if len(dn) and dn.std() > 0 else None,
               mdd=mdd, calmar=(ann / abs(mdd)) if mdd < 0 else None,
               hit_rate=float((r > 0).mean()))
    return out


def _sr_pp(R: np.ndarray) -> np.ndarray:
    mu, sd = R.mean(axis=0), R.std(axis=0)
    out = np.zeros(R.shape[1])
    nz = sd > 0
    out[nz] = mu[nz] / sd[nz]
    return out


def cscv_pbo(R: np.ndarray, s_blocks: int, seed: int, boot: int = 2000) -> dict:
    """CSCV → PBO（Bailey & López de Prado）：IS 最优策略的 OOS 排名落在后半的概率。"""
    T, N = R.shape
    m = (T // s_blocks) * s_blocks
    if N < 2 or m < s_blocks * 2:
        return {"pbo": None, "ci95": [None, None], "n_splits": 0,
                "note": "矩阵过小，CSCV 不可计算"}
    Rb = R[:m]
    blocks = np.array_split(np.arange(m), s_blocks)
    lams = np.empty(math.comb(s_blocks, s_blocks // 2))
    for i, combo in enumerate(combinations(range(s_blocks), s_blocks // 2)):
        in_blocks = set(combo)
        ir = np.concatenate([blocks[b] for b in combo])
        orr = np.concatenate([blocks[b] for b in range(s_blocks) if b not in in_blocks])
        is_sr, oos_sr = _sr_pp(Rb[ir]), _sr_pp(Rb[orr])
        ns = int(np.argmax(is_sr))
        rank = int(np.sum(oos_sr <= oos_sr[ns]))          # 1..N，含并列
        w = min(max(rank / (N + 1), 1e-6), 1 - 1e-6)
        lams[i] = math.log(w / (1.0 - w))
    pbo = float(np.mean(lams <= 0.0))
    rng = np.random.default_rng(seed)
    ps = (lams[rng.integers(0, len(lams), size=(boot, len(lams)))] <= 0.0).mean(axis=1)
    return {"pbo": pbo, "ci95": [float(np.percentile(ps, 2.5)), float(np.percentile(ps, 97.5))],
            "n_splits": len(lams), "s_blocks": s_blocks, "rows_used": m,
            "note": "自助 CI 因划分相互重叠而偏窄，仅作参考"}


def deflated_sharpe(sel_returns: np.ndarray, all_sr_pp: np.ndarray, n_trials: int) -> dict:
    """DSR（Bailey & López de Prado 2014）：对 N 次试验与偏度/峰度去通胀后的夏普显著性。"""
    r = np.asarray(sel_returns, float)
    r = r[np.isfinite(r)]
    T = len(r)
    out = {"dsr": None, "sr0_pp": None, "sr_pp": None, "skew": None, "kurt": None,
           "n_trials": n_trials}
    if T < 3 or n_trials < 2 or float(r.std(ddof=1)) == 0.0:
        return out
    sr = sharpe_per_period(r)
    sk, ku = _skew(r), _kurtosis(r)
    v = float(np.var(all_sr_pp, ddof=1))
    sr0 = math.sqrt(v) * ((1 - EULER_GAMMA) * norm_ppf(1 - 1.0 / n_trials)
                          + EULER_GAMMA * norm_ppf(1 - 1.0 / (n_trials * math.e)))
    den = math.sqrt(max(1 - sk * sr + (ku - 1) / 4 * sr ** 2, 1e-9))
    out.update(dsr=norm_cdf((sr - sr0) * math.sqrt(T - 1) / den), sr0_pp=sr0, sr_pp=sr,
               skew=sk, kurt=ku)
    return out


def dedupe_columns(R: np.ndarray, names: list[str]) -> tuple[np.ndarray, list[str], int, list[int]]:
    """剔除逐位相同的重复列（参数格点常见），返回去重后矩阵/列名/剔除数/原列索引。"""
    seen, keep = {}, []
    for j in range(R.shape[1]):
        key = R[:, j].tobytes()
        if key in seen:
            continue
        seen[key] = j
        keep.append(j)
    return R[:, keep], [names[j] for j in keep], R.shape[1] - len(keep), keep


# ------------------------------------------------------------- 价格/引擎层

def ma_positions(closes: np.ndarray, fast: int, slow: int) -> np.ndarray:
    """MA(快)>MA(慢) → 1，否则 0；pos[t] 只用 ≤t 的收盘价（决策在 t 收盘），执行在 t+1。"""
    n = len(closes)
    pos = np.zeros(n)
    pref = np.concatenate([[0.0], np.cumsum(closes)])
    for t in range(slow - 1, n):
        maf = (pref[t + 1] - pref[t + 1 - fast]) / fast
        mas = (pref[t + 1] - pref[t + 1 - slow]) / slow
        pos[t] = 1.0 if maf > mas else 0.0
    return pos


def apply_positions(rets: np.ndarray, pos: np.ndarray, cost_lin: float, impact: float) -> tuple:
    """无前视引擎：gross[t]=pos[t-1]*ret[t]；成本=线性×|Δw| + 冲击×|Δw|^1.5。"""
    exec_pos = np.concatenate([[0.0], pos[:-1]])
    gross = exec_pos * rets
    turn = np.abs(np.diff(np.concatenate([[0.0], pos])))
    cost = cost_lin * turn + impact * np.power(turn, 1.5)
    return gross, gross - cost, turn


def parse_grid(spec: str) -> list[int]:
    parts = spec.split(":")
    if len(parts) == 1:
        return [int(parts[0])]
    if len(parts) != 3:
        raise SystemExit(f"网格格式应为 start:stop:step，收到 {spec!r}")
    a, b, st = (int(x) for x in parts)
    if st <= 0 or b < a:
        raise SystemExit(f"网格参数非法：{spec!r}")
    return list(range(a, b + 1, st))


def build_price_matrix(closes: np.ndarray, fasts: list[int], slows: list[int],
                       cost_lin: float, impact: float) -> dict:
    rets = np.zeros(len(closes))
    rets[1:] = closes[1:] / closes[:-1] - 1.0
    cols, pos_cols, params = [], [], []
    for f in fasts:
        for s in slows:
            if f >= s:
                continue
            pos = ma_positions(closes, f, s)
            gross, net, _ = apply_positions(rets, pos, cost_lin, impact)
            cols.append(net)
            pos_cols.append(pos)
            params.append((f, s, gross, pos))
    if not cols:
        raise SystemExit("参数网格为空（需 fast < slow）")
    names = [f"fast={f},slow={s}" for f, s, _, _ in params]
    return {"R_net": np.column_stack(cols), "names": names, "params": params,
            "rets": rets, "closes": closes}


def resolve_data_root() -> str:
    env = os.environ.get("QM_DATA_ROOT", "").strip()
    candidates = ([env] if env else []) + [
        "/data",
        "/quantmind/data",
        str(Path.home() / "projects" / "quantmind" / "data"),
    ]
    for cand in candidates:
        if os.path.isdir(os.path.join(cand, "quantdb", "1_kline_data")):
            return cand
    raise SystemExit("未找到 QuantDB 数据根（含 quantdb/1_kline_data）：设置 QM_DATA_ROOT / 确认 /data 挂载")


def load_index_closes(market: str, symbol: str, start: str, end: str) -> tuple:
    """读 QuantDB index_daily 单指数收盘序列（CN/HK/US，symbol 一律后缀式）。"""
    try:
        import pyarrow.dataset as ds
    except ImportError:  # pragma: no cover
        raise SystemExit(
            "--quantdb 需要 pyarrow。在 quantmind 容器内运行：\n"
            "  docker cp <本脚本> quantmind:/tmp/ && docker exec -w /app quantmind "
            "python3 /tmp/backtest_bias_audit.py --quantdb ...") from None
    prefix = {"CN": "quantdb", "HK": "quanthk", "US": "quantus"}.get(market.upper())
    if prefix is None:
        raise SystemExit(f"不支持的市场: {market}（可选 CN/HK/US）")
    base = f"{resolve_data_root()}/{prefix}/1_kline_data/index_daily"
    t0 = datetime.strptime(re.sub(r"\D", "", start), "%Y%m%d")
    t1 = datetime.strptime(re.sub(r"\D", "", end), "%Y%m%d") + timedelta(days=1)
    dataset = ds.dataset(base, format="parquet", partitioning="hive")
    filt = ((ds.field("symbol") == symbol) & (ds.field("time") >= t0)
            & (ds.field("time") < t1))
    tab = dataset.to_table(filter=filt, columns=["time", "close"])
    rows = sorted(zip(tab.column("time").to_pylist(), tab.column("close").to_pylist(),
                      strict=True))
    dedup = {}
    for ts, c in rows:                       # 同日重复保留最后一行并计数
        dedup[ts] = c
    dups = len(rows) - len(dedup)
    items = sorted(dedup.items())
    if len(items) < 100:
        raise SystemExit(
            f"{market} {symbol} 在 {start}~{end} 仅 {len(items)} 行（<100）："
            f"确认代码为后缀式（CN=000300.SH / HK=HSI.HK / US=SPX.US）与同步状态")
    dates = [ts.strftime("%Y-%m-%d") for ts, _ in items]
    closes = np.array([float(c) for _, c in items])
    if not np.all(np.isfinite(closes)) or np.any(closes <= 0):
        raise SystemExit("收盘序列存在非有限值或非正值")
    return closes, dates, {"symbol": symbol, "market": market.upper(),
                           "rows": len(items), "dups_dropped": dups,
                           "base": base}


def demo_prices(phi: float, n: int = 1500, seed: int = 7) -> tuple:
    """合成 AR(1) 价格（已知真实过程，φ 小=近似无趋势 → 检验器应给出“无显著净边际”）。"""
    rng = np.random.default_rng(seed)
    eps = rng.normal(0.0, 0.012, n)
    r = np.zeros(n)
    for t in range(1, n):
        r[t] = phi * r[t - 1] + eps[t]
    closes = 100.0 * np.exp(np.cumsum(r))
    d0 = datetime(2018, 1, 1)
    dates, acc = [], d0
    while len(dates) < n:
        if acc.weekday() < 5:
            dates.append(acc.strftime("%Y-%m-%d"))
        acc += timedelta(days=1)
    return closes, dates


def load_csv_matrix(path: str) -> tuple:
    """--input 契约：首列可为 YYYY-MM-DD 日期；其余每列一条策略的每期收益（已含各自成本口径）。"""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = [row for row in csv.reader(fh) if row and any(str(c).strip() for c in row)]
    if len(rows) < 60:
        raise SystemExit("CSV 行数不足（<60）")
    header, body = rows[0], rows[1:]
    if len(set(header)) != len(header):
        raise SystemExit("CSV 列名重复")
    widths = {len(r) for r in body}
    if len(widths) != 1:
        raise SystemExit(f"CSV 行列数不一致：{sorted(widths)}")
    date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    first_col = [r[0].strip() for r in body]
    has_dates = all(date_re.match(v) for v in first_col)
    dates = first_col if has_dates else None
    if dates and dates != sorted(dates):
        raise SystemExit("CSV 需按时间升序排列")
    names = header[1:] if has_dates else header[:]
    cols = []
    for j in range(len(names)):
        ci = j + 1 if has_dates else j
        try:
            cols.append(np.array([float(r[ci]) for r in body]))
        except (TypeError, ValueError) as exc:
            raise SystemExit(f"列 {names[j]!r} 含非数值：{exc}") from exc
    R = np.column_stack(cols)
    if not np.all(np.isfinite(R)):
        raise SystemExit("CSV 含 NaN/Inf；本工具要求完整收益序列")
    return R, names, dates


# ------------------------------------------------------------- 分析主流程

def audit_matrix(R: np.ndarray, names: list[str], dates: list, args, price_ctx: dict | None) -> dict:
    R, names, n_dup, keep = dedupe_columns(R, names)
    T, N = R.shape
    findings: list[dict] = []

    def add(id_, sev, typ, evidence, impact, fix):
        findings.append({"id": id_, "severity": sev, "type": typ,
                         "evidence": evidence, "impact": impact, "recommended_fix": fix})

    if n_dup:
        add("duplicate-trials-dropped", "info", "confirmed-issue",
            {"dropped": n_dup, "kept": N},
            "参数格点存在逐位相同的重复试验列；重复列会制造并列并虚增试验数。已剔除后分析。",
            "确认参数空间无退化格点；如需保留请在 --input 侧自行去重。")
    if N < 2:
        add("pbo-dsr-unavailable", "insufficient-evidence", "missing-evidence",
            {"n_trials_effective": N},
            "有效策略列数 < 2，PBO(过拟合概率)与 DSR(多重检验校正)不可计算。",
            "提供 ≥2 条策略收益列（或更大的参数网格）后重跑。")
    if T // args.cscv_s < 20:
        add("cscv-blocks-small", "insufficient-evidence", "missing-evidence",
            {"T": T, "S": args.cscv_s, "block_size": T // args.cscv_s},
            "CSCV 分块样本过小，PBO 估计不稳定。",
            "增大样本窗口或减小 --cscv-s。")

    split = int(T * (1.0 - args.oos_frac))
    is_sr = np.array([sharpe_ann(R[:split, j]) for j in range(N)])
    oos_sr = np.array([sharpe_ann(R[split:, j]) for j in range(N)])
    sel = int(np.argmax(is_sr))
    sel_orig = keep[sel]                 # 去重前原列号（price_ctx 按原始列存储）
    sel_name = names[sel]
    is_best = float(is_sr[sel])

    lag = args.hac_lag or (price_ctx["params"][sel_orig][1] if price_ctx else nw_auto_lag(T))
    oos_hac = hac_sharpe_stats(R[split:, sel], lag)
    full_hac = hac_sharpe_stats(R[:, sel], lag)
    m_full = metrics(R[:, sel])
    scan = [{"name": names[j], "is_sharpe": fin(is_sr[j]),
             "oos_sharpe": fin(oos_sr[j]), "selected": j == sel}
            for j in range(N)]

    pbo = cscv_pbo(R, args.cscv_s, args.seed) if N >= 2 else {"pbo": None, "ci95": [None, None], "n_splits": 0}
    n_trials = args.dsr_trials or N
    dsr = deflated_sharpe(R[:, sel], np.array([sharpe_per_period(R[:, j]) for j in range(N)]), n_trials)

    # ---- 价格模式特有：前视量化 / 成本敏感性 / 下注 / 走查 ----
    look_ahead = {"available": False, "reason": "input 模式无持仓序列，前视无法重建；"
                                               "请在策略侧自查滞后（见 references/methodology.md 检查表）"}
    cost_sens = {"available": False, "rows": [], "breakeven_bps": None}
    segment = {"available": False}
    turnover_per_day = None
    gross_sharpe = None
    if price_ctx is not None:
        f_sel, s_sel, gross_sel, pos_sel = price_ctx["params"][sel_orig]
        leaky = pos_sel * price_ctx["rets"]
        clean_hac = hac_sharpe_stats(gross_sel, lag)
        leaky_hac = hac_sharpe_stats(leaky, lag)
        look_ahead = {"available": True, "config": sel_name,
                      "clean_gross_sharpe": fin(clean_hac["sr_ann"]),
                      "clean_t": fin(clean_hac["t"]),
                      "leaky_gross_sharpe": fin(leaky_hac["sr_ann"]),
                      "leaky_t": fin(leaky_hac["t"]),
                      "inflation": fin(leaky_hac["sr_ann"] - clean_hac["sr_ann"]),
                      "note": "毛口径（隔离前视）；干净=滞后一bar，泄漏=当bar收益"}
        turnover_per_day = float(np.abs(np.diff(np.concatenate([[0.0], pos_sel]))).mean())
        gross_sharpe = sharpe_ann(gross_sel)
        rows = []
        for cb in COST_SWEEP_BPS:
            _, net_c, _ = apply_positions(price_ctx["rets"], pos_sel, cb / 1e4, args.impact)
            rows.append({"cost_bps": cb, "net_sharpe": fin(sharpe_ann(net_c))})
        breakeven = None
        prev = None
        for cb10 in range(0, 301):                     # 0..30bp，步长 0.1bp
            cb = cb10 / 10.0
            _, net_c, _ = apply_positions(price_ctx["rets"], pos_sel, cb / 1e4, args.impact)
            sh = sharpe_ann(net_c)
            if prev is not None and prev > 0 >= sh:
                breakeven = cb
                break
            prev = sh
        cost_sens = {"available": True, "rows": rows, "breakeven_bps": fin(breakeven),
                     "note": f"每行均含冲击 {args.impact}×|Δw|^1.5；breakeven 为全样本净夏普穿零的线性成本（0.1bp 步长）"}
        in_mkt = pos_sel > 0
        segment = {"available": True, "oos": segment_stats(R[split:, sel], pos_sel[split:]),
                   "full": segment_stats(R[:, sel], pos_sel),
                   "turnover_per_day": fin(turnover_per_day),
                   "bars_in_market_pct": fin(float(pos_sel.mean()) * 100.0),
                   "hit_rate_in_market": fin(float((R[in_mkt, sel] > 0).mean()))}

    # ---- walk-forward（有日期则做：锚定式，年为单位）----
    wf = {"available": False, "reason": "无日期列，无法按时间走查"}
    if dates:
        years = sorted({int(d[:4]) for d in dates})
        if len(years) >= 4:
            picks, wf_net, wf_dates = [], [], []
            for y in years[3:]:
                tr = np.array([int(d[:4]) < y for d in dates])
                te = np.array([int(d[:4]) == y for d in dates])
                if tr.sum() < 60 or te.sum() < 20:
                    continue
                j = int(np.argmax([sharpe_ann(R[tr, k]) for k in range(N)]))
                picks.append({"year": y, "config": names[j],
                              "train_best_is_sharpe": fin(sharpe_ann(R[tr, j])),
                              "year_oos_sharpe": fin(sharpe_ann(R[te, j]))})
                wf_net.append(R[te, j])
                wf_dates.extend([d for d, m in zip(dates, te, strict=True) if m])
            if wf_net:
                wf_cat = np.concatenate(wf_net)
                wf_hac = hac_sharpe_stats(wf_cat, args.hac_lag or nw_auto_lag(len(wf_cat)))
                wf = {"available": True, "years": len(picks), "picks": picks,
                      "wf_net_sharpe": fin(wf_hac["sr_ann"]), "wf_t": fin(wf_hac["t"]),
                      "wf_ci95": [fin(wf_hac["ci95"][0]), fin(wf_hac["ci95"][1])],
                      "note": "锚定式：每年用此前全部数据选参数，仅当年样本外拼接"}
            else:
                wf = {"available": False, "reason": "前 3 年后无满足最小样本的年度"}
        else:
            wf = {"available": False, "reason": f"样本仅 {len(years)} 个自然年（<4），走查不可用"}

    # ---- 审计规则 ----
    if pbo["pbo"] is not None and pbo["pbo"] > PBO_HIGH:
        add("pbo-overfit", "high", "confirmed-issue",
            {"pbo": fin(pbo["pbo"]), "ci95": [fin(pbo["ci95"][0]), fin(pbo["ci95"][1])],
             "threshold": f">{PBO_HIGH:.0%}"},
            "样本内最优策略在样本外落入后半的概率过半：选择更像过拟合而非真优势。",
            "扩大样本/减少试验次数；用样本外或走查结果作为头条，不采用全样本最优参数。")
    elif pbo["pbo"] is not None and pbo["pbo"] > PBO_MEDIUM:
        add("pbo-moderate", "medium", "confirmed-issue",
            {"pbo": fin(pbo["pbo"]), "threshold": f"{PBO_MEDIUM:.0%}~{PBO_HIGH:.0%}"},
            "过拟合风险中等。", "补充独立样本/走查确认后再采信选参。")
    if dsr["dsr"] is not None and dsr["dsr"] < DSR_SIGNIFICANT:
        add("dsr-not-significant", "high", "confirmed-issue",
            {"dsr": fin(dsr["dsr"]), "n_trials": n_trials, "sr0_pp": fin(dsr["sr0_pp"]),
             "threshold": f"<{DSR_SIGNIFICANT:.0%}"},
            "按试验次数去通胀后，选中的夏普不再显著为正（DSR 为乐观上界，真实自由度更高）。",
            "如实披露全部研究自由度并重估；必要时放弃该选参。")
    if gross_sharpe is not None and gross_sharpe > 0 and (m_full["sharpe"] or 0) <= 0:
        add("edge-eaten-by-cost", "high", "confirmed-issue",
            {"gross_sharpe": fin(gross_sharpe), "net_sharpe": fin(m_full["sharpe"])},
            "毛边际为正但扣费后归零：优势被成本与冲击吃光。",
            "降低换手；核对成本假设；按净口径重估策略。")
    if look_ahead["available"] and look_ahead["inflation"] is not None \
            and look_ahead["inflation"] > LOOKAHEAD_HIGH:
        add("look-ahead-inflation", "high", "confirmed-issue",
            {"clean": look_ahead["clean_gross_sharpe"], "leaky": look_ahead["leaky_gross_sharpe"],
             "inflation": look_ahead["inflation"], "threshold": f">{LOOKAHEAD_HIGH}"},
            "忘记执行滞后使夏普显著虚高：结论依赖前视。", "按 references/methodology.md 检查表逐项排查滞后与滚动窗口。")
    if oos_hac["ci95"][0] is not None and oos_hac["ci95"][0] <= 0 <= oos_hac["ci95"][1]:
        add("no-significant-net-edge", "medium", "confirmed-issue",
            {"oos_sharpe": fin(oos_hac["sr_ann"]), "t": fin(oos_hac["t"]),
             "ci95": [fin(oos_hac["ci95"][0]), fin(oos_hac["ci95"][1])], "lag": oos_hac["lag"]},
            "样本外扣费后无显著净边际（HAC 95%CI 含 0）。此为“未发现正向优势”，不等于“策略无效”。",
            "如需继续：扩大独立样本、降低换手、核对数据时点。")
    bets_oos = segment["oos"]["n"] if segment.get("available") else None
    if (bets_oos is not None and bets_oos < MIN_BETS_OOS) or \
            (oos_hac["neff"] is not None and oos_hac["neff"] < MIN_NEFF):
        add("low-effective-sample", "medium", "confirmed-issue",
            {"bets_oos": bets_oos, "neff_oos": fin(oos_hac["neff"]),
             "vif": fin(oos_hac["vif"]) if "vif" in oos_hac else None,
             "threshold": f"下注<{MIN_BETS_OOS} 或 N_eff<{MIN_NEFF}"},
            "有效样本/独立下注偏少：日频样本量高估了真实信息量。", "延长样本或降低信号频率。")
    if cost_sens["available"] and cost_sens["breakeven_bps"] is not None \
            and args.cost_bps is not None and cost_sens["breakeven_bps"] < 2 * args.cost_bps:
        add("cost-sensitive", "medium", "confirmed-issue",
            {"breakeven_bps": cost_sens["breakeven_bps"], "headline_cost_bps": args.cost_bps,
             "turnover_per_day": turnover_per_day},
            "盈亏平衡成本低于头条成本假设的 2 倍：净表现对成本口径高度敏感。",
            "核对真实费率/冲击；降低换手后重算。")
    if price_ctx is not None:
        add("survivorship-not-checked", "insufficient-evidence", "missing-evidence",
            {"universe": "单一指数（价格指数，点位为当时真实发布值）", "purpose": "本演示不涉及选股池"},
            "幸存者/时点偏差在「交易指数本身」的场景下不适用；但把结论外推到成分股/选股策略时不成立。",
            "若改为选股回测：必须纳入退市标的并核对成分/财务的公告日时点。")
    else:
        add("survivorship-not-checked", "insufficient-evidence", "missing-evidence",
            {"universe": "由 --input 提供，未附带成分与退市信息"},
            "试验矩阵为最终收益序列，标的池是否含退市标的、是否时点对齐不可由本工具判定。",
            "在数据装配侧复核：含退市/破产标的、成分与财务为当时可见值。")
    if not look_ahead["available"]:
        add("look-ahead-not-checkable", "insufficient-evidence", "missing-evidence",
            {"reason": look_ahead["reason"]},
            "本模式无法量化前视偏差（缺持仓序列），前视风险未被排除。",
            "用 --demo/--quantdb 跑同一信号量化泄漏虚高，或按检查表人工核对。")
    if not wf["available"]:
        add("walk-forward-unavailable", "insufficient-evidence", "missing-evidence",
            {"reason": wf["reason"]},
            "未执行滚动走查：单一 70/30 切分不构成稳健性证据。",
            "提供带日期的更长样本，或接受本限制并在结论中声明。")

    severities = {f["severity"] for f in findings}
    if N < 2 or T < 100:
        status = "insufficient-evidence"
    elif "high" in severities:
        status = "fail"
    elif "medium" in severities:
        status = "warning"
    else:
        status = "pass"

    limitations = [
        "单窗口/单标的/单市场不能证明稳健；样本外不显著仅表示“未发现正向优势”，不表示“策略无效”。",
        "成本为线性佣金+平方根律冲击的风格化近似（无 ADV/盘口数据）；真实冲击随规模与流动性变化。",
        "未建模：涨跌停/停牌不可成交、T+1 于本策略为日频自然满足、做空与借券成本（本演示为 long/flat）。",
        "指数为价格指数（不含分红再投），收益口径偏低；DSR 的试验数只含显式网格，成本/带宽/规则等研究自由度未计入 → DSR 为显著性上界、PBO 为过拟合下界。",
        "HAC 带宽默认取选中信号的慢线窗口（input 模式取 Newey-West 自动带宽）；带宽选择本身是研究自由度。",
    ]
    next_actions = [
        "人工复核全部 high/medium 发现，逐条对照 severity 触发规则与证据数值。",
        "对 severity=insufficient-evidence 的缺口补齐证据后再下结论，不得当通过。",
        "任何对外结论一律引用「样本外·扣费后·无前视」口径；不显著时使用“无显著净边际”。",
    ]
    return {
        "status": status,
        "input_summary": {
            "mode": args.mode, "n_periods": T, "n_trials_declared": N + n_dup,
            "n_trials_effective": N, "n_trials_dropped_duplicate": n_dup,
            "oos_split_index": split, "oos_periods": T - split,
            "start": dates[0] if dates else None, "end": dates[-1] if dates else None,
            "selected": sel_name, "selected_is_sharpe": fin(is_best),
            "selected_oos_sharpe": fin(oos_sr[sel]),
            "symbol": (price_ctx or {}).get("meta", {}).get("symbol") if price_ctx else None,
        },
        "assumptions": {
            "execution_lag_bars": 1, "annualization": ANNUAL,
            "cost_bps_linear_per_side": args.cost_bps, "impact_coef": args.impact,
            "oos_frac": args.oos_frac, "hac_lag": lag, "cscv_s_blocks": args.cscv_s,
            "dsr_trials": n_trials, "bootstrap": {"B": 2000, "seed": args.seed},
        },
        "headline": {
            "oos_net_sharpe": fin(oos_hac["sr_ann"]), "t_hac": fin(oos_hac["t"]),
            "ci95": [fin(oos_hac["ci95"][0]), fin(oos_hac["ci95"][1])],
            "significant": bool(oos_hac["ci95"][0] is not None and oos_hac["ci95"][0] > 0),
            "wording": ("存在显著净边际（样本外·扣费后·HAC）"
                        if (oos_hac["ci95"][0] is not None and oos_hac["ci95"][0] > 0)
                        else "无显著净边际（样本外·扣费后·HAC）"),
        },
        "pbo": pbo, "dsr": dsr, "look_ahead": look_ahead,
        "walk_forward": wf, "cost_sensitivity": cost_sens, "segment": segment,
        "performance_full_sample": m_full,
        "performance_full_hac": {k: fin(v) if not isinstance(v, list) else [fin(x) for x in v]
                                 for k, v in full_hac.items()},
        "scan": scan,
        "findings": findings, "limitations": limitations, "next_actions": next_actions,
        "metrics": {"oos_net_sharpe": fin(oos_hac["sr_ann"]), "pbo": fin(pbo["pbo"]),
                    "dsr": fin(dsr["dsr"]), "full_net_sharpe": fin(m_full["sharpe"]),
                    "gross_sharpe": fin(gross_sharpe)},
        "disclaimer": DISCLAIMER,
    }


# ------------------------------------------------------------- 结构校准

def run_sanity() -> dict:
    """结构校准：已知真值的合成矩阵（确定性种子）。"""
    T, N, S = 2000, 64, 10
    rng = np.random.default_rng(20261007)
    noise = rng.normal(0.0, 0.01, (T, N))
    noise_res = cscv_pbo(noise, S, seed=1)
    signal = rng.normal(0.0, 0.01, (T, N))
    signal[:, 0] += 0.0015                      # 单列日漂移 15bp ≈ 年化 SR ~2.4，强信号
    signal_res = cscv_pbo(signal, S, seed=1)
    split = int(T * 0.7)
    sig_sel = int(np.argmax([sharpe_ann(signal[:split, j]) for j in range(N)]))
    sig_dsr = deflated_sharpe(signal[:, 0], np.array([sharpe_per_period(signal[:, j]) for j in range(N)]), N)
    noise_sel = int(np.argmax([sharpe_ann(noise[:split, j]) for j in range(N)]))
    noise_dsr = deflated_sharpe(noise[:, noise_sel],
                                np.array([sharpe_per_period(noise[:, j]) for j in range(N)]), N)
    checks = [
        {"name": "noise_matrix_pbo", "expect": "≈0.5（无真值可挑，选中即掷硬币）",
         "observed": noise_res["pbo"], "tolerance": [0.35, 0.65],
         "passed": noise_res["pbo"] is not None and 0.35 <= noise_res["pbo"] <= 0.65},
        {"name": "signal_matrix_pbo", "expect": "≤0.25（有真信号列，选中可复现）",
         "observed": signal_res["pbo"], "tolerance": [0.0, 0.25],
         "passed": signal_res["pbo"] is not None and signal_res["pbo"] <= 0.25},
    ]

    def f3(x):
        return f"{x:.3f}" if x is not None else "n/a"

    notes = [
        f"信号列为第 0 列；样本内(70%)选中列 idx={sig_sel}"
        f"（{'正确命中信号列' if sig_sel == 0 else '未命中，检查种子/强度'}）",
        f"参考：噪声矩阵选中的 DSR={f3(noise_dsr['dsr'])}（噪声里挑最优不应过 0.95）；"
        f"信号列 DSR={f3(sig_dsr['dsr'])}",
    ]
    return {"status": "pass" if all(c["passed"] for c in checks) else "fail",
            "checks": checks, "notes": notes,
            "config": {"T": T, "N": N, "S": S, "seed": 20261007, "drift_signal": 0.0015},
            "limitations": ["本校准验证的是统计机械（CSCV/PBO、DSR）在已知真值下的行为，"
                            "不构成对任何真实策略的结论。"],
            "disclaimer": DISCLAIMER}


# ------------------------------------------------------------- CLI

def emit(payload: dict, out: str | None) -> None:
    text = json.dumps(_jsonable(payload), ensure_ascii=False, indent=2, allow_nan=False)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text + "\n", encoding="utf-8")
        n = len(payload.get("findings", payload.get("checks", [])))
        print(f"[backtest_bias_audit] status={payload['status']} findings={n} -> {out}")
    else:
        print(text)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest bias audit: PBO/DSR/HAC/costs/look-ahead.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--sanity", action="store_true")
    source.add_argument("--demo", action="store_true")
    source.add_argument("--input")
    source.add_argument("--quantdb", action="store_true")
    parser.add_argument("--market", help="CN / HK / US（--quantdb 时必填）")
    parser.add_argument("--symbol", help="后缀式指数代码：000300.SH / HSI.HK / SPX.US（--quantdb 时必填）")
    parser.add_argument("--start", help="窗口起点 YYYY-MM-DD（--quantdb 时必填）")
    parser.add_argument("--end", help="窗口终点 YYYY-MM-DD（--quantdb 时必填）")
    parser.add_argument("--fast", default=DEFAULT_FAST, help=f"快线网格 start:stop:step（默认 {DEFAULT_FAST}）")
    parser.add_argument("--slow", default=DEFAULT_SLOW, help=f"慢线网格 start:stop:step（默认 {DEFAULT_SLOW}）")
    parser.add_argument("--phi", type=float, default=0.05, help="--demo 合成 AR(1) 系数")
    parser.add_argument("--cost-bps", type=float, default=None, help=f"线性成本 单边bp（价格模式默认 {DEFAULT_COST_BPS}）")
    parser.add_argument("--impact", type=float, default=None, help=f"平方根律冲击系数（默认 {DEFAULT_IMPACT}）")
    parser.add_argument("--oos-frac", type=float, default=0.30, help="样本外占比（默认 0.30，末尾留出）")
    parser.add_argument("--cscv-s", type=int, default=10, help="CSCV 分块数 S（默认 10 → 252 种划分）")
    parser.add_argument("--hac-lag", type=int, default=0, help="HAC 带宽（0=自动：价格模式取选中慢线）")
    parser.add_argument("--dsr-trials", type=int, default=0, help="DSR 试验数（0=去重后的网格列数）")
    parser.add_argument("--seed", type=int, default=1, help="自助法种子")
    parser.add_argument("--out")
    args = parser.parse_args()

    if not 0.0 < args.oos_frac < 1.0:
        parser.error("--oos-frac 需在 (0,1)")
    if args.cscv_s < 2 or args.cscv_s % 2:
        parser.error("--cscv-s 需为 ≥2 的偶数（S/2 与 S-S/2 对称）")

    if args.sanity:
        args.mode = "sanity"
        report = run_sanity()
        emit(report, args.out)
        raise SystemExit(0 if report["status"] == "pass" else 1)

    if args.input is not None:
        if args.cost_bps is not None or args.impact is not None:
            parser.error("--cost-bps/--impact 仅在 --demo/--quantdb 模式有效（--input 的收益视为已含各自成本口径）")
        args.mode, args.cost_bps, args.impact = "input", None, None
        R, names, dates = load_csv_matrix(args.input)
        price_ctx = None
    else:
        args.cost_bps = DEFAULT_COST_BPS if args.cost_bps is None else args.cost_bps
        args.impact = DEFAULT_IMPACT if args.impact is None else args.impact
        if args.cost_bps < 0 or args.impact < 0:
            parser.error("--cost-bps/--impact 不能为负")
        if args.demo:
            args.mode = "demo"
            closes, dates = demo_prices(args.phi)
            meta = {"symbol": f"SYN(phi={args.phi})", "market": "SYN", "rows": len(closes)}
        else:
            if not (args.market and args.symbol and args.start and args.end):
                parser.error("--quantdb 需要 --market --symbol --start --end")
            args.mode = "quantdb"
            closes, dates, meta = load_index_closes(args.market, args.symbol, args.start, args.end)
        price_ctx = build_price_matrix(closes, parse_grid(args.fast), parse_grid(args.slow),
                                       args.cost_bps / 1e4, args.impact)
        price_ctx["meta"] = meta
        R, names = price_ctx["R_net"], price_ctx["names"]

    report = audit_matrix(R, names, dates, args, price_ctx)
    emit(report, args.out)


if __name__ == "__main__":
    main()
