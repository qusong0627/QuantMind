"""仓位信号分（position_score）：基于截面基准 + 凯利公式的建仓比例建议。

设计文档见 scripts/build_position_calibration.py。本模块在推理落库后调用，
按当日截面批量计算每只股票的 position_score，写入 engine_signal_scores.quality JSONB。

position_score ∈ {0} ∪ [0.1, 0.99]：
  0    = 不入场（低于行业头部 / 大盘空仓 / 卖出信号）
  0.1~0.99 = 建议投入仓位百分比（半凯利 + 非线性映射）

依赖（离线校准产物，缺失时回退仓库内置默认 → 经验默认值）：
  <QuantDB数据目录>/position_signal_calibration/ic_weights.json    —— p 的融合权重
  <QuantDB数据目录>/position_signal_calibration/payoff_table.json —— 赔率查找表 b
数据目录经 backend/shared/quantdb_paths.resolve_quantdb_dir() 解析（便携包/Docker 通用），
内置默认在 backend/services/engine/inference/calibration_defaults/。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import text

from backend.shared.quantdb_paths import resolve_quantdb_subdir

logger = logging.getLogger(__name__)

_CALIBRATION_DEFAULTS_DIR = Path(__file__).resolve().parent / "calibration_defaults"
_calibration_missing_logged = False

# 经验默认权重（校准产物缺失时用）：与 L2 模型校准结果相近
_DEFAULT_WEIGHTS = {"market": 0.16, "industry": 0.30, "board": 0.27, "cap": 0.27}
_DEFAULT_B = 1.15  # 全局默认赔率
_N_BUCKETS = 10

# 行业/板块/市值档前 N 均分基准
_TOP_N_AVG = 10
# 铁律（双门槛）：个股必须在行业前 X% 且全市场前 Y% 才入场。
# 行业前 20%：行业内的强度门槛；全市场前 40%：防止小行业龙头全市场垫底仍入场。
_INDUSTRY_PCT_FLOOR = 0.80
_MARKET_PCT_FLOOR = 0.60


def _calibration_dir() -> Path:
    """校准产物目录。惰性解析——便携包数据目录与 Docker 不同，勿在 import 时求值。"""
    return resolve_quantdb_subdir("position_signal_calibration")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("position_signal: 读取校准产物 %s 失败: %s", path, exc)
        return None


def _load_calibration() -> tuple[dict[str, float], dict[str, Any]]:
    """加载 IC 权重 + 赔率查找表。

    优先级：数据目录校准产物 → 仓库内置默认 → 经验默认值。
    便携包/新装环境的 QuantDB 数据目录里通常没有校准目录，内置默认保证
    仍用真实校准值（而非粗糙经验值）；客户重校准后以数据目录为准。
    """
    global _calibration_missing_logged
    weights = dict(_DEFAULT_WEIGHTS)
    payoff: dict[str, Any] = {"fallback_by_bucket": {}, "overall_b": _DEFAULT_B, "table": {}}

    cal_dir = _calibration_dir()
    ic_path = cal_dir / "ic_weights.json"
    if not ic_path.is_file():
        ic_path = _CALIBRATION_DEFAULTS_DIR / "ic_weights.json"
    payoff_path = cal_dir / "payoff_table.json"
    if not payoff_path.is_file():
        payoff_path = _CALIBRATION_DEFAULTS_DIR / "payoff_table.json"

    ic_data = _read_json(ic_path) if ic_path.is_file() else None
    if ic_data is None:
        if not _calibration_missing_logged:
            _calibration_missing_logged = True
            logger.warning(
                "position_signal: 校准产物缺失（%s 与内置默认 %s 均无），"
                "用经验默认权重/赔率",
                cal_dir,
                _CALIBRATION_DEFAULTS_DIR,
            )
    elif isinstance(ic_data.get("weights"), dict):
        w = ic_data["weights"]
        weights = {k: float(w.get(k, _DEFAULT_WEIGHTS[k])) for k in _DEFAULT_WEIGHTS}

    payoff_data = _read_json(payoff_path) if payoff_path.is_file() else None
    if payoff_data is not None:
        payoff = payoff_data
    return weights, payoff


def _classify_board(code: str) -> str:
    if code.startswith("688"):
        return "科创板"
    if code.startswith("30"):
        return "创业板"
    if code.startswith(("002", "003")):
        return "中小板"
    if code.startswith(("000", "001")):
        return "深主板"
    if code.startswith("60"):
        return "沪主板"
    if code.startswith(("4", "8", "9")):
        return "北交所"
    return "其他"


def _cap_tier(ltsz_yi: float | None) -> str:
    if ltsz_yi is None:
        return ""
    if ltsz_yi < 30:
        return "微盘"
    if ltsz_yi < 100:
        return "小盘"
    if ltsz_yi < 300:
        return "中盘"
    if ltsz_yi < 1000:
        return "大盘"
    return "超大盘"


def _top_n_avg(scores: pd.Series, n: int = _TOP_N_AVG) -> float | None:
    s = scores.dropna()
    if s.empty:
        return None
    if len(s) <= n:
        return round(float(s.mean()), 4)
    return round(float(s.nlargest(n).mean()), 4)


def _pct_within(scores: pd.Series) -> float:
    """单只股票在所在组的百分位（0~1）。组内<2返回0.5。"""
    s = scores.dropna()
    n = len(s)
    if n < 2:
        return 0.5
    return float((s.rank(method="average") - 1).iloc[-1] / (n - 1))


def _lookup_b(payoff: dict[str, Any], industry: str, score_pct: float) -> float:
    """查赔率 b(industry, bucket)。缺失时按 bucket 兜底 → 全局兜底。"""
    bucket = int(max(0, min(_N_BUCKETS - 1, score_pct * _N_BUCKETS)))
    ind_table = payoff.get("table", {}).get(industry)
    if ind_table and str(bucket) in ind_table:
        return float(ind_table[str(bucket)])
    fb = payoff.get("fallback_by_bucket", {})
    if str(bucket) in fb:
        return float(fb[str(bucket)])
    return float(payoff.get("overall_b", _DEFAULT_B))


def _compute_position_score(row: pd.Series, weights: dict[str, float],
                            payoff: dict[str, Any], market_empty: bool) -> float:
    """单只股票的 position_score。

    口径统一用百分位（0~1），不比绝对分数——不同模型分数量纲差 100 倍，
    绝对值无意义。双铁律：行业前 20% 且全市场前 40% 才入场。

    映射分两层，确保既有「入场/不入场」门槛（凯利）又有「投多少」区分度（强度）：
      1. 强度因子 s：把加权百分位 p_blend 用幂映射拉伸尾部（p^3），让顶尖 vs 合格拉开
         差距。入场股 p_blend∈[0.6,1.0]，幂映射后 s∈[0.22,1.0]，跨度大。
      2. 凯利上限 k：半凯利仓位，钳到 [0, 0.7]，控总仓位防单票满仓。
      position_score = clamp(0.1 + 0.89 * s_weighted, 0.1, 0.99)
    其中 s_weighted = s * (0.5 + 0.5 * k_norm)：强度为主、凯利为辅（极端乐观上调）。
    """
    side = str(row.get("signal_side") or "HOLD")
    if side == "SELL":
        return 0.0
    if market_empty:
        return 0.0
    # 双铁律：行业前 20% + 全市场前 40%
    if float(row["pct_industry"]) < _INDUSTRY_PCT_FLOOR:
        return 0.0
    if float(row["pct_market"]) < _MARKET_PCT_FLOOR:
        return 0.0

    p_blend = (
        weights["market"] * row["pct_market"]
        + weights["industry"] * row["pct_industry"]
        + weights["board"] * row["pct_board"]
        + weights["cap"] * row["pct_cap"]
    )
    p_blend = max(0.0, min(1.0, float(p_blend)))

    # ── 强度因子 s：幂映射 p^3 拉伸尾部（p=0.6→0.22, p=0.8→0.51, p=0.95→0.86, p=1→1）──
    s = p_blend ** 3

    # ── 凯利上限 k：半凯利，钳到 [0, 0.7] ──
    b = _lookup_b(payoff, str(row.get("industry") or ""), row["pct_market"])
    k = min(0.70, max(0.0, 0.5 * (p_blend - (1 - p_blend) / b)))
    k_norm = k / 0.70

    # 强度为主、凯利为辅
    position = 0.1 + 0.89 * s * (0.5 + 0.5 * k_norm)
    return round(max(0.1, min(0.99, position)), 3)


def compute_position_scores(
    symbols: list[str],
    scores: list[float],
    sides: list[str],
) -> list[dict[str, Any]]:
    """对当日截面计算每只股票的基准 + 百分位 + position_score。

    返回 [{symbol, position_score, industry_top10_avg, board_top10_avg,
           cap_top10_avg, pct_market, pct_industry, pct_board, pct_cap, market_empty}, ...]，
    供调用方批量 UPDATE 回 quality JSONB。

    元数据（行业/市值/板块）从 instrument_detail parquet 查；缺失的股票不报错，
    按「其他」行业处理（百分位退化）。
    """
    if not symbols:
        return []
    weights, payoff = _load_calibration()

    df = pd.DataFrame({
        "symbol": symbols,
        "fusion_score": scores,
        "signal_side": sides,
    })
    df = df[df["fusion_score"].notna()].copy()
    if df.empty:
        return []
    df["code"] = df["symbol"].map(lambda s: s.split(".")[0] if "." in s else s)

    def _to_suffix(code: str) -> str:
        """纯数字代码 → 后缀格式（与 instrument_detail.Symbol 对齐）。已带后缀则原样。"""
        if "." in code:
            return code
        if code.startswith(("4", "8", "9")) and not code.startswith("9"):
            return f"{code}.BJ"
        if code.startswith("6") or code.startswith("9"):
            return f"{code}.SH"
        return f"{code}.SZ"

    df["symbol_sfx"] = df["code"].map(_to_suffix)

    # 元数据：行业 + 流通市值(亿) + 板块
    detail_dir = resolve_quantdb_subdir("2_base_sector", "instrument_detail")
    try:
        import duckdb

        if not any(detail_dir.rglob("*.parquet")):
            raise FileNotFoundError(f"instrument_detail 缺失: {detail_dir}")
        # glob 交给 DuckDB（其文件顺序决定下面 keep="last" 的胜者，勿在 Python 侧重排）
        pattern = (detail_dir / "**" / "*.parquet").as_posix()
        con = duckdb.connect()
        try:
            meta = con.execute(
                "SELECT Symbol AS symbol, rs_hyname AS industry, Ltsz AS ltsz "
                "FROM read_parquet(?, union_by_name=true) "
                "WHERE Symbol LIKE '%.SH' OR Symbol LIKE '%.SZ' OR Symbol LIKE '%.BJ'",
                [pattern],
            ).fetchdf()
        finally:
            con.close()
        # Ltsz 字段已是亿元单位（如 2124.91 = 平安银行流通市值2124亿），勿再除1e8
        meta["ltsz_yi"] = pd.to_numeric(meta["ltsz"], errors="coerce")
        meta["board"] = meta["symbol"].map(lambda s: _classify_board(s.split(".")[0]))
        meta["cap_tier"] = meta["ltsz_yi"].map(_cap_tier)
        # instrument_detail 可能有同 symbol 多行（不同快照），取最新一条
        meta = meta.drop_duplicates(subset=["symbol"], keep="last")
        df = df.merge(meta[["symbol", "industry", "board", "cap_tier"]], left_on="symbol_sfx", right_on="symbol", how="left", suffixes=("", "_meta"))
    except Exception as exc:
        logger.warning(
            "position_signal: 元数据加载失败（目录 %s），按「其他」行业降级: %s",
            detail_dir,
            exc,
        )
        df["industry"] = "其他"
        df["board"] = "其他"
        df["cap_tier"] = "中盘"

    df["industry"] = df["industry"].fillna("其他")
    df["board"] = df["board"].fillna("其他")
    df["cap_tier"] = df["cap_tier"].fillna("中盘")

    # ── 维度基准：前10均分（行业股数<10退化全均分，见 _top_n_avg）──
    df["industry_top10_avg"] = df.groupby("industry")["fusion_score"].transform(_top_n_avg)
    df["board_top10_avg"] = df.groupby("board")["fusion_score"].transform(_top_n_avg)
    df["cap_top10_avg"] = df.groupby("cap_tier")["fusion_score"].transform(_top_n_avg)

    # ── 百分位（组内排名 0~1）──
    def _grp_rank(s: pd.Series) -> pd.Series:
        n = len(s)
        if n < 2:
            return pd.Series([0.5] * n, index=s.index)
        return s.rank(method="average") / (n + 1)  # +1 避免满 1.0
    df["pct_market"] = df["fusion_score"].rank(method="average") / (len(df) + 1)
    df["pct_industry"] = df.groupby("industry", group_keys=False)["fusion_score"].transform(_grp_rank)
    df["pct_board"] = df.groupby("board", group_keys=False)["fusion_score"].transform(_grp_rank)
    df["pct_cap"] = df.groupby("cap_tier", group_keys=False)["fusion_score"].transform(_grp_rank)

    # ── 大盘空仓信号（沿用 compute_market_signals 口径的退化版）──
    # 信号日全市场正分占比 < 30% 视为弱市空仓
    pos_pct = (df["fusion_score"] > 0).mean() if len(df) else 0.0
    market_empty = pos_pct < 0.30

    # ── position_score ──
    df["position_score"] = df.apply(
        lambda r: _compute_position_score(r, weights, payoff, market_empty), axis=1
    )

    out = []
    for _, r in df.iterrows():
        out.append({
            "symbol": str(r["symbol"]),
            "position_score": float(r["position_score"]),
            "industry_top10_avg": float(r["industry_top10_avg"]) if pd.notna(r["industry_top10_avg"]) else None,
            "board_top10_avg": float(r["board_top10_avg"]) if pd.notna(r["board_top10_avg"]) else None,
            "cap_top10_avg": float(r["cap_top10_avg"]) if pd.notna(r["cap_top10_avg"]) else None,
            "pct_market": round(float(r["pct_market"]), 4),
            "pct_industry": round(float(r["pct_industry"]), 4),
            "market_empty": bool(market_empty),
        })
    return out


def batch_update_quality(db, run_id: str, tenant_id: str, user_id: str,
                        predictions: list[dict[str, Any]]) -> None:
    """把 position_score 及基准写入 engine_signal_scores.quality JSONB（合并到已有 quality）。"""
    if not predictions:
        return
    # 取该 run 的 (symbol -> row_id + 旧 quality)
    rows = db.execute(
        text("""
            SELECT id, symbol, quality FROM engine_signal_scores
            WHERE run_id = :rid AND tenant_id = :tid AND user_id = :uid
        """),
        {"rid": run_id, "tid": tenant_id, "uid": user_id},
    ).fetchall()
    if not rows:
        return
    # symbol 归一（信号表 symbol 存纯数字或 suffix 不一致，都按纯数字匹配）
    by_code: dict[str, Any] = {}
    for rid, sym, q in rows:
        code = sym.split(".")[0] if sym and "." in sym else sym
        by_code[str(code)] = (rid, q)

    update_sql = text("""
        UPDATE engine_signal_scores SET quality = CAST(:q AS jsonb) WHERE id = :id
    """)
    updated = 0
    for p in predictions:
        sym = p["symbol"]
        code = sym.split(".")[0] if "." in sym else sym
        hit = by_code.get(str(code))
        if not hit:
            continue
        rid, old_q = hit
        merged = dict(old_q) if isinstance(old_q, dict) else {}
        merged["position"] = {
            "position_score": p["position_score"],
            "industry_top10_avg": p["industry_top10_avg"],
            "board_top10_avg": p["board_top10_avg"],
            "cap_top10_avg": p["cap_top10_avg"],
            "pct_market": p["pct_market"],
            "pct_industry": p["pct_industry"],
            "market_empty": p["market_empty"],
        }
        db.execute(update_sql, {"id": rid, "q": json.dumps(merged, ensure_ascii=False)})
        updated += 1
    db.commit()
    logger.info(f"[position_signal] 已更新 {updated}/{len(predictions)} 条 position_score, run_id={run_id}")
