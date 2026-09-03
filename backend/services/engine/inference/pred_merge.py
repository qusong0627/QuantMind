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
    return len(new_df)
