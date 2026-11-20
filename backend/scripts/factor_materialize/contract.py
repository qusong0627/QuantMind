"""模型字段契约：每天物化后「现役模型真正要读」的列。

来源是模型 metadata 的 ``factor_field_sources``（与读侧
``QuantDBFactorReader`` 同语法）：

- 值形如 ``"库:列"`` → 该列来自副库；
- 值是不带前缀的裸列名 → 该列来自锚库（metadata 的 ``factor_source``）。

本模块只关心**自己物化**的库（``MATERIALIZED_LIBRARIES``）。``l1_factors`` /
``l1_l2_factors`` / ``l2_factors`` 等下载类库与 ``alpha360`` / ``jq110`` /
``alpha_library`` / ``tdxgs`` 记入 ``external``，只报告不设闸；没有任何来源的
特征列（如 qlib Alpha158 的 KMID 系）记入 ``unmapped``。

为什么不能按「列名出现在哪个库」猜：同一列名可能同时存在于 ``factor_defs``
与 ``cand_factors``，而模型实际读的是锚库。猜错库会让探针在错误的库上判定
覆盖，掩盖真正要读的库的空列。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

LOG = logging.getLogger("factor_materialize.contract")

#: 自有物化库（每日编排要保覆盖的库）
MATERIALIZED_LIBRARIES = (
    "factor_defs",
    "cand_factors",
    "factor1300",
    "gap_mined",
    "l1_batch1_factors",
    "rd_mined",
)

#: 结构性全空列：现役模型引用、但官方历史分区本就整列为空。
#: 闸门必须放行，否则每天都会误报（新增列先在此登记再放宽）。
KNOWN_EMPTY_COLUMNS = frozenset(
    {
        "feat_idio_to_vol",
        "feat_idio_to_vol_r2",
    }
)

#: 已知 T+1 滞后列（两融类）：最新交易日当晚两融分区尚未落盘，整列空属预期，
#: 次日回补窗口（repair_dates）会自动重算。覆盖闸门把它们按「延后」上报，
#: 不判失败——否则每个交易日都会误报一次。
KNOWN_LAG_COLUMNS: dict[str, frozenset[str]] = {
    # 两融三件套（2026-11-20）：mg_net_ratio_5 / sl_bal_ratio 与 mg_bal_chg_20
    # 同源同滞后，11/19 当晚实测三列全空、前一日 67% 覆盖。
    "l1_batch1_factors": frozenset({"mg_bal_chg_20", "mg_net_ratio_5", "sl_bal_ratio"}),
    "gap_mined": frozenset({"MG2_BALCHG_3M"}),
    # 两融类：最新交易日当晚两融分区尚未落盘（f13_0582 融资比率 / 0583 融券比率 /
    # 0584 融资净买入占比）。2026-11-10 实测 20261109 当日晚跑时三列全空，
    # 而 20261102~20261106 均有值——属 T+1 滞后，非数据断供。
    "factor1300": frozenset({"f13_0582", "f13_0583", "f13_0584"}),
}

#: 上游数据断供导致的临时空列：官方历史有值，但新日期算不出来。
#: 与 KNOWN_EMPTY_COLUMNS 的区别：这些列**必须**在数据恢复后删除本登记。
#: 探针闸门放行以免整库停更，覆盖闸门按「已知滞后」上报（状态可见但不判失败）。
#: 2026-11-10：2026-11-06 收入表刷新丢失 total_operating_cost/费用/减值字段，
#: f13_1128（去杠杆经营性资产盈利收益率）分子的 rev_ttm−cogs_ttm 整列为空。
DATA_BLOCKED_COLUMNS: dict[str, frozenset[str]] = {
    "factor1300": frozenset({"f13_1128"}),
}


@dataclass(frozen=True)
class Contract:
    """模型字段契约快照。"""

    required: dict[str, tuple[str, ...]] = field(default_factory=dict)
    external: dict[str, tuple[str, ...]] = field(default_factory=dict)
    unmapped: tuple[str, ...] = ()
    models: int = 0

    def all_required(self) -> dict[str, tuple[str, ...]]:
        return dict(self.required)


def _metadata_files(repo: Path, *, include_users: bool) -> list[Path]:
    files = sorted((repo / "models" / "production").glob("*/metadata.json"))
    if include_users:
        files += sorted((repo / "models" / "users").glob("*/*/*/metadata.json"))
    return [p for p in files if p.is_file()]


def _split_source(feature: str, source: object, anchor: str) -> tuple[str, str]:
    """``factor_field_sources`` 值 → (库, 列)。裸名归锚库。"""
    if isinstance(source, str) and source.strip():
        text = source.strip()
        if ":" in text:
            library, column = text.split(":", 1)
            return library.strip(), column.strip()
        return anchor, text
    return anchor, str(feature)


def load_contract(
    repo: Path,
    *,
    include_users: bool = True,
) -> Contract:
    """扫描模型元数据，汇总各库必需列。坏 metadata 逐条隔离，不阻断。"""
    required: dict[str, set[str]] = {}
    external: dict[str, set[str]] = {}
    unmapped: set[str] = set()
    models = 0
    for path in _metadata_files(Path(repo), include_users=include_users):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:  # 坏文件不阻断整轮物化
            LOG.warning("跳过不可读元数据 %s：%s", path, exc)
            continue
        sources = data.get("factor_field_sources") or {}
        features = data.get("feature_columns") or data.get("features") or []
        if not features and sources:
            features = list(sources)
        if not features:
            continue
        anchor = str(data.get("factor_source") or "").strip()
        models += 1
        for feature in features:
            library, column = _split_source(feature, sources.get(feature), anchor)
            if not library or not column:
                unmapped.add(str(feature))
            elif library in MATERIALIZED_LIBRARIES:
                required.setdefault(library, set()).add(column)
            else:
                external.setdefault(library, set()).add(column)
    contract = Contract(
        required={lib: tuple(sorted(cols)) for lib, cols in required.items()},
        external={lib: tuple(sorted(cols)) for lib, cols in external.items()},
        unmapped=tuple(sorted(unmapped)),
        models=models,
    )
    LOG.info(
        "契约：%d 个模型；自有库 %s；外部库 %s；无来源 %d",
        models,
        {lib: len(cols) for lib, cols in contract.required.items()},
        {lib: len(cols) for lib, cols in contract.external.items()},
        len(contract.unmapped),
    )
    if DATA_BLOCKED_COLUMNS:
        LOG.warning(
            "上游断供临时放行的列（数据恢复后必须删除登记）：%s",
            {lib: sorted(cols) for lib, cols in DATA_BLOCKED_COLUMNS.items()},
        )
    return contract


def gate_columns(library: str, contract: Contract) -> tuple[str, ...]:
    """某库参与闸门的必需列（去掉结构性全空白名单与上游断供列）。

    注意：**包含**已知 T+1 滞后列——回补检测要用它们（次日重算）。
    覆盖闸门请改用 :func:`coverage_columns`。
    """
    blocked = DATA_BLOCKED_COLUMNS.get(library, frozenset())
    return tuple(
        column
        for column in contract.required.get(library, ())
        if column not in KNOWN_EMPTY_COLUMNS and column not in blocked
    )


def coverage_columns(
    library: str,
    contract: Contract,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """覆盖闸门用列：(严格列, 已知滞后列)。滞后列只上报、不判失败。"""
    lagged = KNOWN_LAG_COLUMNS.get(library, frozenset()) | DATA_BLOCKED_COLUMNS.get(
        library, frozenset()
    )
    strict: list[str] = []
    deferred: list[str] = []
    for column in gate_columns(library, contract):
        (deferred if column in lagged else strict).append(column)
    return tuple(strict), tuple(deferred)
