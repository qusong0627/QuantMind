"""推理分数回写 pred.parquet 的共享工具。

两套推理数据的一致性维护：
- A 套：engine_signal_scores（单日批次，symbol 纯数字，trade_date=T+1 信号生效日）
- B 套：模型目录 pred.parquet（历史分数序列，symbol SH/SZ 前缀式，trade_date=T 数据日）

coverage 缺口判定与个股分数曲线均读 B 套；每日自动推理与一键补全成功后
须把真实分数合并回 B 套，否则 B 套停在训练日，缺口与曲线永不更新。
"""
from __future__ import annotations

import re
from pathlib import Path


def merge_signals_into_pred(
    parquet_file: Path,
    signals_by_date: list[tuple[str, list[dict]]],
    *,
    create_if_missing: bool = False,
) -> int:
    """把 runner 真实推理分数合并进 pred.parquet。

    - symbol 归一为 SH/SZ 前缀式（pred.parquet 约定）
    - 按 (symbol, trade_date) 去重，新分数覆盖旧值
    - 临时文件 + 原子替换，避免并发读到半写文件
    - 默认不凭单日数据创建残缺历史（create_if_missing=False 时文件
      不存在直接返回 0）
    - 合并的同时刷新「按日物化分片」（pred_daily/dt=YYYYMMDD/data.parquet），
      供投研平台按日直读单日截面，避免重复全文件扫描

    返回本次写入的行数。
    """
    import os
    import tempfile

    import duckdb
    import pandas as pd

    from backend.shared.stock_utils import StockCodeUtil

    if not parquet_file.is_file() and not create_if_missing:
        return 0

    rows = []
    for d, signals in signals_by_date:
        for s in signals or []:
            raw = str(s.get("symbol", "")).strip()
            sym = StockCodeUtil.to_prefix(raw)
            if re.match(r"^(SH|SZ|BJ)\d{6}$", sym):
                pass  # A 股路径不变
            elif raw.endswith((".HK", ".hk")) or raw.isdigit():
                # 港股：pred.parquet symbol 为 4位+.HK（0700.HK），非 A 股前缀式
                sym = StockCodeUtil.to_hk_suffix(
                    raw[:-3] if raw.endswith((".HK", ".hk")) else raw
                )
                if not re.match(r"^\d{4,5}\.HK$", sym, re.IGNORECASE):
                    continue
            else:
                continue
            try:
                score = float(s.get("score"))
            except (TypeError, ValueError):
                continue
            rows.append(
                {
                    "symbol": sym,
                    "trade_date": pd.Timestamp(d),
                    # 推理日无真实标签；用 NaN 保持 label 列 float64 类型不变
                    "label": float("nan"),
                    "pred": score,
                    "split": "test",
                }
            )
    if not rows:
        return 0
    new_df = pd.DataFrame(rows)
    con = duckdb.connect()
    try:
        if parquet_file.is_file():
            existing = con.execute(
                f"SELECT * FROM read_parquet('{str(parquet_file)}')"
            ).df()
            combined = pd.concat([existing, new_df], ignore_index=True)
        else:
            combined = new_df
        combined = combined.drop_duplicates(subset=["symbol", "trade_date"], keep="last")
        combined = combined.sort_values(["trade_date", "symbol"]).reset_index(drop=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(parquet_file.parent), suffix=".parquet.tmp"
        )
        os.close(tmp_fd)
        combined.to_parquet(tmp_path, index=False)
        os.replace(tmp_path, str(parquet_file))
    finally:
        try:
            con.close()
        except Exception:
            pass

    # 刷新按日物化分片：合并后的当日截面直接落盘，投研平台无需再从全量重提。
    _refresh_pred_daily(parquet_file, new_df)
    return len(new_df)


def _refresh_pred_daily(parquet_file: Path, new_df) -> None:
    """把本次写入的每日截面刷新到 pred_daily/dt=YYYYMMDD/data.parquet。

    物化分片是投研平台的读取加速层（惰性物化 + 推理合并后主动刷新），
    仅保存 symbol/pred 两列，已剔除 B 股/北交所/指数（与投研口径一致）。
    """
    import os
    import tempfile

    import pandas as pd

    if new_df.empty or "trade_date" not in new_df.columns:
        return
    daily_dir = parquet_file.parent / "pred_daily"
    for d, grp in new_df.groupby(new_df["trade_date"].dt.normalize()):
        date_str = d.strftime("%Y-%m-%d")
        rows = []
        for _, r in grp.iterrows():
            sym = str(r.get("symbol", ""))
            if not re.match(r"^(SH|SZ|BJ)\d{6}$", sym):
                continue
            if sym.startswith("SH000") or sym.startswith("SZ399"):
                continue
            if sym.startswith("SH900") or sym.startswith("SZ200"):
                continue
            if sym.startswith("BJ"):
                continue
            pred_val = r.get("pred")
            if pred_val is None or (isinstance(pred_val, float) and pd.isna(pred_val)):
                continue
            rows.append({"symbol": sym, "score": float(pred_val)})
        if not rows:
            continue
        part = daily_dir / f"dt={date_str.replace('-', '')}" / "data.parquet"
        part.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(part.parent), suffix=".parquet.tmp")
        os.close(tmp_fd)
        pd.DataFrame(rows).to_parquet(tmp_path, index=False)
        os.replace(tmp_path, str(part))
