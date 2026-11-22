"""合并因子库的列级来源解析。

背景：CUSTOM 市场的 `l1_factors` 是 281 列合并面板，物理列复制自
alpha_library / alpha360 / factor_dictionary / l1_l2_factors 等原始库。
`factor_field_sources` 只记裸列名时，来源信息会丢；本模块按
`qm_quantdb_factor_field` 的目录记录把每个列解析回原始库，供重训与审计展示。

只读目录，不读 parquet：目录命中即返回候选（多候选按原始库优先级排序），
调用方若要严格校验数值，可再按候选读一天 parquet 对比。
"""

from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import text

# 原始库优先级：越靠前越可能是合并库的真实来源
_SOURCE_PRIORITY = (
    "alpha_library",
    "alpha360",
    "factor_dictionary",
    "l1_l2_factors",
    "l1_factors",
    "tianfeng_daily",
    "tdx_ind",
    "factor1300",
    "rd_mined",
    "cand_factors",
    "gap_mined",
    "jkp_ashare_monthly",
)
_MARKET_PRIORITY = {"CN": 0, "HK": 1, "US": 2, "FUTURES": 3, "CRYPTO": 4, "CUSTOM": 9}


def _source_rank(dataset_id: str) -> int:
    try:
        return _SOURCE_PRIORITY.index(dataset_id)
    except ValueError:
        return len(_SOURCE_PRIORITY)


def _candidate_sort_key(
    candidate: dict[str, Any], *, market: str, dataset_id: str
) -> tuple[int, int, int, str]:
    """排序键：原始库优先级 → 同市场优先 → 市场顺序 → 库名。"""
    src_dataset = str(candidate.get("dataset_id") or "")
    src_market = str(candidate.get("market") or "").upper()
    same_market_rank = 0 if src_market == market else 1
    return (
        _source_rank(src_dataset),
        same_market_rank,
        _MARKET_PRIORITY.get(src_market, 8),
        src_dataset,
    )


def build_feature_provenance(
    features: Iterable[str],
    rows: Iterable[dict[str, Any]],
    *,
    market: str,
    dataset_id: str,
) -> dict[str, dict[str, Any]]:
    """纯函数：目录行 → {feature: {source_market, source_dataset, source_column, candidates}}。"""
    market_key = str(market or "").upper()
    dataset_key = str(dataset_id or "").strip()
    wanted = {str(feature).strip() for feature in features if str(feature).strip()}
    by_feature: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        column = str(row.get("column_name") or "").strip()
        if not column or column not in wanted:
            continue
        src_market = str(row.get("market") or "").upper()
        src_dataset = str(row.get("dataset_id") or "").strip()
        if not src_dataset:
            continue
        # 合并库自身不算来源
        if dataset_key and src_market == market_key and src_dataset == dataset_key:
            continue
        by_feature.setdefault(column, []).append(
            {"market": src_market, "dataset_id": src_dataset}
        )
    out: dict[str, dict[str, Any]] = {}
    for feature, candidates in by_feature.items():
        candidates.sort(
            key=lambda item: _candidate_sort_key(
                item, market=market_key, dataset_id=dataset_key
            )
        )
        best = candidates[0]
        out[feature] = {
            "source_market": best["market"],
            "source_dataset": best["dataset_id"],
            "source_column": feature,
            "candidates": candidates[:5],
        }
    return out


async def resolve_feature_provenance(
    session,
    *,
    features: Iterable[str],
    market: str,
    dataset_id: str,
) -> dict[str, dict[str, Any]]:
    """走 `qm_quantdb_factor_field` 目录解析列级来源。"""
    wanted = sorted({str(feature).strip() for feature in features if str(feature).strip()})
    if not wanted:
        return {}
    result = await session.execute(
        text(
            """
            SELECT column_name, market, dataset_id
            FROM qm_quantdb_factor_field
            WHERE column_name = ANY(:features)
            """
        ),
        {"features": wanted},
    )
    rows = [dict(row) for row in result.mappings().all()]
    return build_feature_provenance(
        wanted, rows, market=market, dataset_id=dataset_id
    )
