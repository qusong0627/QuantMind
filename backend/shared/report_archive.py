"""报告档案根解析的**唯一实现**（第五份副本不许再抄）。

同一段「env → 新目录 → 旧目录」的逻辑此前被抄了四份：
``services/engine/routers/trading_agents.py:_resolve_results_dir``（权威版）与
``scripts/{factor_dedup_report,factor_deep_dive,factor_regime_analysis}.py``。
三处脚本副本还各自带一份 **降级到不同顺序** 的 except 分支 —— 同一环境里
写侧与读侧可能解析出不同目录，档案会"写进去但看不见"。

⚠️ 历史事故（照抄在 ``factor_dedup_report.py`` 的 docstring 里，此处再记一次）：
脚本最初写死 ``/data/reports/trading_agents``，而当时该目录不存在、档案实际
回退在旧目录 ``db/trading_agents_results``。脚本一跑就把新目录建了出来，
解析随即改指新目录 → 老用户的股票研报历史在 UI 里"消失"（数据仍在旧目录）。
**建目录这个副作用会改变解析结果**，故本模块只解析、不创建；创建由调用方
显式决定，且必须用本模块解析出的根。

顺序（与权威版逐字一致，勿改）：
  1. ``$TRADING_AGENTS_RESULTS_DIR`` —— **存在且是目录**才采用（配了空路径等同没配）
  2. ``/data/reports/trading_agents``
  3. ``/app/db/trading_agents_results``（旧目录）
  4. 都没有 → 返回 2，由调用方在创建时决定（不在此处创建）
"""

from __future__ import annotations

import os
from pathlib import Path

RESULTS_ENV = "TRADING_AGENTS_RESULTS_DIR"
"""覆盖报告档案根的环境变量名（沿用既有名，勿改名：部署脚本已在用）。"""

RESULTS_DIR = Path("/data/reports/trading_agents")
"""当前报告档案根。"""

LEGACY_RESULTS_DIR = Path("/app/db/trading_agents_results")
"""旧报告档案根（历史档案所在地，只读回退）。"""


def resolve_results_dir() -> Path:
    """按固定顺序解析报告档案根；解析不出来时返回默认根（不创建目录）。"""
    env_val = os.getenv(RESULTS_ENV, "").strip()
    if env_val:
        p = Path(env_val)
        if p.is_dir():
            return p
    if RESULTS_DIR.is_dir():
        return RESULTS_DIR
    if LEGACY_RESULTS_DIR.is_dir():
        return LEGACY_RESULTS_DIR
    return RESULTS_DIR


def archive_root() -> Path:
    """``resolve_results_dir`` 的别名，供报告生成脚本沿用既有调用名。"""
    return resolve_results_dir()


def ensure_report_dir(name: str) -> Path:
    """在档案根下取（必要时创建）子目录，如 ``"因子研究"``。

    单段名称校验：拒绝路径分隔与 ``..``，防止报告名把文件写到档案根之外。
    """
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError(f"非法报告子目录名: {name!r}")
    p = archive_root() / name
    p.mkdir(parents=True, exist_ok=True)
    return p
