"""因子库 parquet 分区的读取契约（唯一实现）。

背景：QuantDB 因子库 parquet 的**物理列序随日期漂移** —— 列名集合逐日相同，
但列在文件里的物理位置会变。实测（2026-09-20）：

| 库 | 分区总数 | 物理序种数 | 漂移分区数 | 漂移日期范围 |
|---|---|---|---|---|
| ``l1_factors`` | 2604 | 4 | 174 | 20260105 – 20260918 |
| ``l1_l2_factors`` | 2116 | 2 | 170 | 20260105 – 20260914 |

漂移的形态是「某次上游写入把新列**追加到末尾**」而不是放回历史位置
（``fun_pe``/``fun_ep`` 在旧分区位于中段，20260105 起跑到末尾）。

任何「跨多个交易日累积、且按列**位置**索引」的算法都会**静默串列** ——
第 i 列在不同日子是不同因子。受害写法：``select *`` / ``select_dtypes`` 拿自然序
当特征清单、跨日累积 ``X'X`` 求 PCA 基、跨日堆叠回归、``X[:, i]`` 切片、
把特征名存盘后再按位置用。

本模块把「列序」从**写入者的偶然**变成**读取者的约定**：

1. 特征列一律**按名字排序**。名字在库内唯一 ⇒ 排序即规范序，且与日期无关。
2. 元数据列（代码/日期/OHLCV/发布列）固定前置，不参与特征轴。
3. 列集合跨日不一致 ⇒ **抛错**，绝不静默跳过 —— 静默少累一天无人察觉
   （见 ``verification-vacuous-pass-guard`` 同族毛病）。

用法::

    from backend.shared.factor_partition import read_partition, ColumnContract

    contract = ColumnContract("l1_factors")
    for path in paths:
        df = read_partition(path)
        feats = contract.check(df.columns, context=path)   # 不一致直接抛
        X = df[feats].to_numpy()

不要再用 ``select *`` 的自然序当特征轴。
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

# 元数据列：不参与特征轴。按名字识别，顺序即规范前置序。
META_COLUMNS: tuple[str, ...] = (
    "symbol",
    "instrument",
    "code",
    "date",
    "dt",
    "time",
    "release_id",
    "published_at",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
)
_META_SET = frozenset(META_COLUMNS)

#: 兼容别名（研究脚本里的 NON_FEATURE 同义）
NON_FEATURE = _META_SET


def is_meta(column: str) -> bool:
    """该列是否为元数据列（大小写不敏感）。"""
    return column.lower() in _META_SET


def canonical_columns(all_columns: Iterable[str]) -> list[str]:
    """把任意物理列序规范化为**与日期无关**的确定序。

    元数据列按 :data:`META_COLUMNS` 的固定顺序前置，其余列按名字排序。

    >>> canonical_columns(["b", "close", "a", "symbol"])
    ['symbol', 'close', 'a', 'b']
    """
    cols = list(all_columns)
    meta = [c for c in META_COLUMNS if c in cols]
    feats = sorted(c for c in cols if c not in _META_SET)
    return meta + feats


def feature_columns(all_columns: Iterable[str]) -> list[str]:
    """规范序下的特征列（已排序，不含元数据列）。"""
    return sorted(c for c in all_columns if c not in _META_SET)


def partition_signature(path: str | Path) -> tuple[str, ...]:
    """只读 parquet footer 返回物理列序（不读数据，快）。"""
    import pyarrow.parquet as pq  # 局部导入：本模块被 CLI 冷启动调用

    return tuple(pq.read_schema(str(path)).names)


def partition_order_signature(path: str | Path) -> tuple[str, ...]:
    """:func:`partition_signature` 的别名，语义更明确。"""
    return partition_signature(path)


def read_partition(
    path: str | Path,
    *,
    columns: Iterable[str] | None = None,
    numeric_only: bool = False,
) -> pd.DataFrame:  # noqa: F821 - pandas 为延迟导入，注解靠 __future__ 惰性求值
    """读单个因子分区，返回**规范列序**的 DataFrame。

    Args:
        path: ``dt=YYYYMMDD/data.parquet``。
        columns: 显式列清单（走投影下推）。给定时仍按规范序重排。
        numeric_only: 只保留数值特征列（元数据列始终保留）。

    Raises:
        FileNotFoundError: 路径不存在。
        ValueError: 分区为空或列名为空。
    """
    import pandas as pd

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"因子分区不存在: {p}")
    df = pd.read_parquet(p, columns=list(columns) if columns else None)
    if df.shape[1] == 0:
        raise ValueError(f"因子分区无列: {p}")

    ordered = canonical_columns(df.columns)
    df = df[ordered]
    if numeric_only:
        keep = [c for c in ordered if is_meta(c)] + [
            c
            for c in ordered[sum(1 for c in ordered if is_meta(c)) :]
            if pd.api.types.is_numeric_dtype(df[c])
        ]
        df = df[keep]
    return df


def iter_partitions(root: str | Path) -> Iterator[Path]:
    """遍历 ``root`` 下所有 ``dt=*/**.parquet`` 分区文件（按日期排序）。"""
    base = Path(root)
    for dt_dir in sorted(base.glob("dt=*")):
        yield from sorted(dt_dir.glob("*.parquet"))


@dataclass(frozen=True)
class OrderGroup:
    """一种物理列序及其覆盖的分区。"""

    order: tuple[str, ...]
    dates: tuple[str, ...]

    @property
    def n_cols(self) -> int:
        return len(self.order)


def scan_order_groups(root: str | Path) -> list[OrderGroup]:
    """按物理列序给一个库的全部分区分组，返回按分区数降序的组列表。"""
    buckets: dict[tuple[str, ...], list[str]] = {}
    for f in iter_partitions(root):
        dt = f.parent.name.split("=", 1)[-1]
        buckets.setdefault(partition_signature(f), []).append(dt)
    groups = [
        OrderGroup(order=order, dates=tuple(sorted(dates)))
        for order, dates in buckets.items()
    ]
    groups.sort(key=lambda g: (-len(g.dates), g.dates[0]))
    return groups


def drift_report(root: str | Path) -> dict[str, object]:
    """一个库的列序漂移体检。``drifted`` = 物理序与主序不同的分区数。"""
    groups = scan_order_groups(root)
    if not groups:
        return {"partitions": 0, "orders": 0, "drifted": 0, "groups": groups}
    ref = set(groups[0].order)
    drifted = sum(len(g.dates) for g in groups[1:] if set(g.order) == ref)
    return {
        "partitions": sum(len(g.dates) for g in groups),
        "orders": len(groups),
        "drifted": drifted,
        # 列集合与主序不同的组：不是「重排」能修的，是上游 schema 变更
        "schema_variants": [
            {"n_cols": g.n_cols, "n": len(g.dates), "span": (g.dates[0], g.dates[-1])}
            for g in groups[1:]
            if set(g.order) != ref
        ],
        "groups": groups,
    }


class ColumnContract:
    """跨日列集合守卫：第一次调用定基，之后逐日比对，不一致**立即抛错**。

    静默跳过（``continue``）是陷阱：跨日累积的 ``mom += Z.T@Z`` 会少累一天而
    无人察觉。宁可炸。
    """

    def __init__(self, library: str) -> None:
        self.library = library
        self._ref: tuple[str, ...] | None = None

    @property
    def reference(self) -> tuple[str, ...] | None:
        return self._ref

    def check(self, columns: Iterable[str], *, context: str = "") -> list[str]:
        """校验并返回该日的**规范特征列序**。

        Raises:
            RuntimeError: 列集合与首次调用不一致（顺序差异不算）。
        """
        feats = tuple(feature_columns(columns))
        if self._ref is None:
            if not feats:
                raise RuntimeError(f"{self.library}: 首次调用无特征列 ({context})")
            self._ref = feats
            return list(feats)
        if feats != self._ref:
            only_now = sorted(set(feats) - set(self._ref))
            only_ref = sorted(set(self._ref) - set(feats))
            raise RuntimeError(
                f"{self.library} 列集合与首日不一致（顺序已排序，故非顺序问题）"
                f" @{context or '?'}: 只在今日 {only_now[:5]}；只在首日 {only_ref[:5]}"
            )
        return list(feats)


@dataclass(frozen=True)
class ResolvedFeatures:
    """一次性定死的特征清单（训练与回测必须共用同一份）。"""

    library: str
    columns: tuple[str, ...]  # 特征轴：喂给模型的那几列
    key_columns: tuple[str, ...]  # 键/元列，用于对齐标签与股票池
    dropped: tuple[tuple[str, str], ...]  # (列名, 缺失说明)
    n_partitions: int
    span: tuple[str, str] | None  # (首日, 末日)

    @property
    def read_columns(self) -> tuple[str, ...]:
        """传给 ``read_partition(columns=...)`` 的完整清单（键 + 特征）。

        ``read_partition`` 只返回你点名要的列，所以**不能只传特征** ——
        否则拿不到 ``symbol``/``date``，也就没法对齐标签。
        """
        return self.key_columns + self.columns

    def as_dict(self) -> dict[str, object]:
        return {
            "library": self.library,
            "columns": list(self.columns),
            "key_columns": list(self.key_columns),
            "n_features": len(self.columns),
            "dropped": [{"column": c, "reason": r} for c, r in self.dropped],
            "n_partitions": self.n_partitions,
            "span": list(self.span) if self.span else None,
        }

    def summary(self) -> str:
        span = f"{self.span[0]}~{self.span[1]}" if self.span else "—"
        lines = [
            f"{self.library or '?'}: {len(self.columns)} 维特征"
            f"（{self.n_partitions} 个分区 {span}）",
            f"  键列: {', '.join(self.key_columns) or '—'}",
        ]
        if self.dropped:
            lines.append(f"  已剔除 {len(self.dropped)} 列（各分区不完全存在）：")
            lines += [f"    - {c}: {r}" for c, r in self.dropped]
        return "\n".join(lines)


def resolve_features(
    paths: Iterable[str | Path],
    wanted: Iterable[str],
    *,
    on_missing: str = "intersect",
    library: str = "",
) -> ResolvedFeatures:
    """训练/回测开工前**一次性**定死特征清单，之后按名读取永不报错。

    为什么需要它：列集合会跨 schema 断点变化（实测 ``features_daily`` 20260914 起
    50→78 列、``l1_factors`` 20260826 起 121→119 列）。若逐分区调用 :class:`ColumnContract`，
    训练会在断点当天中断。正确做法不是在读取期容忍，而是**建模前把清单定死**：

    - ``intersect``（默认）：只保留在**所有**分区都存在的列，缺的进 ``dropped``。
      确定、可复现、不注入 NULL。**训练与回测必须共用返回的** ``columns``。
    - ``raise``：缺任何一列即抛，等价于严格模式。

    ⚠ 不要用「缺列填 NaN」代替本函数：老分区全 NaN、新分区全非 NULL 会构成完美的
    年代指示器，树模型必然学它 —— 那是泄露，不是容错。

    只读 parquet footer，不读数据。空 ``paths`` 或无列时抛 ``ValueError``。
    """
    if on_missing not in ("intersect", "raise"):
        raise ValueError(f"on_missing 只支持 intersect/raise，收到 {on_missing!r}")

    plist = [Path(p) for p in paths]
    if not plist:
        raise ValueError("resolve_features 需要至少一个分区")
    wanted_list = list(dict.fromkeys(wanted))  # 去重且保序
    if not wanted_list:
        raise ValueError("resolve_features 需要非空 wanted 特征清单")

    per_file = {p: set(partition_signature(p)) for p in plist}
    present_in_all = set.intersection(*per_file.values())
    missing_map: dict[str, list[Path]] = {c: [] for c in wanted_list}
    for p, cols in per_file.items():
        for c in wanted_list:
            if c not in cols:
                missing_map[c].append(p)

    dropped = tuple(
        (c, _missing_reason(missing_map[c], len(plist)))
        for c in wanted_list
        if missing_map[c]
    )
    if dropped and on_missing == "raise":
        raise RuntimeError(
            f"{library or '?'} 特征清单有 {len(dropped)} 列并非全程存在："
            + "；".join(f"{c}（{r}）" for c, r in dropped[:5])
        )

    keep = tuple(sorted(c for c in wanted_list if not missing_map[c]))
    if not keep:
        raise ValueError(
            f"{library or '?'} 特征清单在全部 {len(plist)} 个分区里都不完整，交集为空"
        )
    dates = sorted(p.parent.name.split("=", 1)[-1] for p in plist)
    return ResolvedFeatures(
        library=library,
        columns=keep,
        # 键列同样只取「全程都在」的：l1_factors 20260826 起就丢了 published_at
        key_columns=tuple(c for c in META_COLUMNS if c in present_in_all),
        dropped=dropped,
        n_partitions=len(plist),
        span=(dates[0], dates[-1]),
    )


def _missing_reason(missing: list[Path], total: int) -> str:
    """把「哪些分区缺这一列」压成一句可读的说明（含缺失区间）。"""
    dts = sorted(p.parent.name.split("=", 1)[-1] for p in missing)
    if len(dts) == 1:
        return f"仅 {dts[0]} 缺"
    return f"{len(missing)}/{total} 个分区缺（{dts[0]} ~ {dts[-1]}）"


def default_market_roots() -> dict[str, Path]:
    """各市场 QuantDB 根目录（env 优先，与 CLAUDE.md 的数据目录约定一致）。"""
    project = Path(__file__).resolve().parents[2]
    spec = {
        "CN": ("QM_QUANTDB_DATA_DIR", "data/quantdb"),
        "US": ("QM_QUANTUS_DATA_DIR", "data/quantus"),
        "HK": ("QM_QUANTHK_DATA_DIR", "data/quanthk"),
        "BC": ("QM_QUANTBC_DATA_DIR", "data/quantbc"),
        "FUTURES": ("QM_QUANTFUTURES_DATA_DIR", "data/quantfutures"),
        "CUSTOM": ("QM_QUANTCUSTOM_DATA_DIR", "data/quantcustom"),
    }
    return {
        market: Path(os.getenv(env, str(project / rel)))
        for market, (env, rel) in spec.items()
    }
