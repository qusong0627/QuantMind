"""CCASS 参与者分类（唯一分类口径，2026-11-14 v1）。

分类表：``config/ccass_participant_categories.yaml``（可用
``QM_CCASS_PARTICIPANT_CATEGORIES`` 覆盖）。优先级：
1. explicit participant id（同一 id 只属于一个类别）；
2. token 匹配（按 YAML 中 categories 的顺序，先命中先得）；
3. 前缀兜底：A→other，B→local_small_broker，C→local_custodian。

注意：HKEX 实际前缀是 A=中国结算、B=券商、C=银行/托管行。分类表是研究资产，
改分类必须升 version 并在因子结果里标注，避免同名因子跨版本不可比。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = PROJECT_ROOT / "config" / "ccass_participant_categories.yaml"

#: 大类 → 研究聚合组（因子层只认这些组名）
GROUPS: dict[str, tuple[str, ...]] = {
    "south": ("south",),
    "csdc_hk": ("csdc_hk",),
    "foreign_custodian": ("foreign_custodian",),
    "local_custodian": ("local_custodian",),
    "foreign_ib": ("foreign_ib",),
    "chinese_broker": ("chinese_broker",),
    "internet_retail": ("internet_retail",),
    "traditional_retail": ("traditional_retail",),
    "local_small_broker": ("local_small_broker",),
    "shell_related": ("shell_related",),
    "finance_company": ("finance_company",),
    # 研究口径的合并组
    "institution": ("south", "foreign_custodian", "foreign_ib", "chinese_broker"),
    "retail": ("internet_retail", "traditional_retail"),
    "minor": ("local_small_broker", "shell_related", "finance_company"),
    "all": (),
}

_DEFAULT_PREFIX = {"A": "other", "B": "local_small_broker", "C": "local_custodian"}


def _path() -> Path:
    env = os.getenv("QM_CCASS_PARTICIPANT_CATEGORIES", "").strip()
    return Path(env) if env else DEFAULT_PATH


@lru_cache(maxsize=4)
def _load_raw(path_str: str, mtime_ns: int) -> dict[str, Any]:
    import yaml

    with open(path_str, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    # 展开 explicit id → category（检测重复 id）
    id_to_cat: dict[str, str] = {}
    token_rules: list[tuple[str, str]] = []
    order: list[str] = []
    for cat, spec in (cfg.get("categories") or {}).items():
        order.append(cat)
        for pid in spec.get("ids") or []:
            pid = str(pid).strip()
            if pid in id_to_cat and id_to_cat[pid] != cat:
                raise ValueError(f"CCASS 分类表重复 id: {pid} ({id_to_cat[pid]} / {cat})")
            id_to_cat[pid] = cat
        for tok in spec.get("tokens") or []:
            token_rules.append((str(tok).strip(), cat))
    return {
        "version": str(cfg.get("version") or "unknown"),
        "id_to_cat": id_to_cat,
        "token_rules": token_rules,
        "order": order,
    }


def _config() -> dict[str, Any]:
    p = _path()
    st = p.stat()
    return _load_raw(str(p), st.st_mtime_ns)


def version() -> str:
    return _config()["version"]


def categories() -> list[str]:
    return list(_config()["order"])


def classify(participant_id: object, participant_name: object) -> str:
    """单个参与者 → 类别名（见 YAML）。"""
    cfg = _config()
    pid = str(participant_id or "").strip()
    name = str(participant_name or "").strip()
    cat = cfg["id_to_cat"].get(pid)
    if cat:
        return cat
    upper = name.upper()
    for token, cat in cfg["token_rules"]:
        if token and token.upper() in upper:
            return cat
    return _DEFAULT_PREFIX.get(pid[:1].upper(), "other")


def classify_frame(
    df: pd.DataFrame,
    *,
    id_col: str = "participant_id",
    name_col: str = "participant_name",
    out_col: str = "participant_category",
) -> pd.DataFrame:
    """给 DataFrame 加分类列（按唯一参与者映射，~500 次调用）。"""
    out = df.copy()
    uniq = out[[id_col, name_col]].drop_duplicates()
    mapping = {
        (str(pid), str(name) if pd.notna(name) else ""): classify(pid, name)
        for pid, name in zip(uniq[id_col], uniq[name_col], strict=False)
    }
    out[out_col] = [
        mapping.get((str(pid), str(name) if pd.notna(name) else ""), "other")
        for pid, name in zip(out[id_col], out[name_col], strict=False)
    ]
    return out


def is_south(cat: str) -> bool:
    return cat in GROUPS["south"]


def group_of(cat: str, group: str) -> bool:
    members = GROUPS.get(group)
    if members is None:
        raise KeyError(f"未知 CCASS 分组: {group}")
    return True if not members else cat in members
