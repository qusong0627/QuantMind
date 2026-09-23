"""对外数据面的数据集注册表（唯一出处）与路径解析。

对外数据面只认**注册表里有的名字**。`{dataset}` 是用户输入、会被拼进文件路径
或 SQL 表名，所以它做的第一件事永远是「查表」，不是「拼接」。

三类数据集，对应三种取数形态
----------------------------
============  ============================  ======================
kind          落盘/存储形态                   传输
============  ============================  ======================
`partition`   一个交易日一个 parquet 文件     按分区取文件（Range/ETag）
`blob`        整个数据集一个文件              整文件取（Range/ETag）
`row`         数据库表，行会被追加/原地更新    行级增量游标
============  ============================  ======================

判断依据是**能不能整体重写**：能（分区、单文件），就按文件给；只能改其中几行，
就得靠游标，并且必须接受「可能重复」。

为什么不在这里再抄一份 QuantDB 数据集清单
------------------------------------------
`backend/shared/quantdb_datasets.py` 已经是数据集名 → 相对路径的既有事实源
（27 条，含 `layout`）。本模块**只登记对外暴露的子集**，`rel_dir` 一律从那里取，
并在测试里钉住「登记的名字在上游都存在且 `layout` 相符」。本仓在这件事上吃过的
亏是「一份清单手抄八遍、八遍都漏了一项」，所以路径字段绝不在这里出现第二次。

被刻意排除的数据集（都在下面各自的注释里写了理由，别当成遗漏）
--------------------------------------------------------------
* `engine_signal_scores` —— 全仓最有价值的表之一，但**没有 `updated_at`**：
  `created_at` 覆盖全部行却不随 upsert 变化，`signal_ts` 只有 0.6% 的行有值。
  任何时间戳游标都会**漏掉更新**且不报错。要么先补列（14M 行），要么不做。
* `tick_data` —— 上游声明 `layout="partition"`，盘面实际是平的
  `{SYMBOL}_{YYYYMMDD}.parquet`（逐笔，一天几千个文件）。声明与盘面不一致，
  按声明取会**静默查空**，所以这一版不收。
* `min1_kline` / `min5_kline` / 财务三表 / `index_weights` —— `symbol` 形态，
  按标的寻址，需要另一套寻址方式，本批不做。
* 旧代年度快照 `db/feature_snapshots/model_features_{YYYY}.parquet` ——
  单文件 0.6–3.3 GB 且被 `update_feature_parquet.main()` **整文件重写**。
  消费者应该走 `features_daily` 的按日分区，而不是每年下一份 3GB 的文件。
* Huntly 新闻原文 —— 在另一个容器的 SQLite 里，时间列是**字符串本地墙钟**
  且有 `0001-12-30` 这类脏值，需要单独的适配器；本批只出富化结果。
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Literal

from backend.shared.freshness import FreshnessPolicy
from backend.shared.quantdb_datasets import DATASETS as _QUANTDB_DATASETS
from backend.shared.quantdb_paths import resolve_quantdb_dir
from backend.shared.utc_datetime import UTC

Kind = Literal["partition", "blob", "row"]

#: 分区名的对外格式。**只接受这一种写法**：`2026-9-2` 这类变体一律拒——
#: 我们不做日期解析的容错，容错就意味着「猜」，而猜错就是读错分区。
_PARTITION_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

#: `dt=YYYYMMDD` 目录名。
_PARTITION_DIR_RE = re.compile(r"dt=(\d{8})")

#: 允许进 SQL 的标识符形状（表名、列名）。见 `RowDataset.__post_init__`。
_IDENT_RE = re.compile(r"[a-z_][a-z0-9_]*")


def _as_aware_datetime(raw: str) -> datetime:
    """兜底键里的时间戳列（极少数场景）。与游标时间戳同一口径：无时区即拒。

    这里**不**复用 `cursor._parse_timestamp`——那个模块是游标格式的守卫，
    注册表不该依赖它；两条路径都收在同一条约定下即可（`utc_datetime`）。
    """
    normalized = raw.replace("Z", "+00:00").replace("z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("兜底键时间戳必须带时区")
    return parsed.astimezone(UTC)


#: 兜底键列的类型 → 「把游标里的字符串还原成可绑定值」的函数。
#:
#: **为什么需要还原**：游标在线上是纯字符串（无损、跨语言确定），但 asyncpg
#: 从预备语句拿到参数 OID 后**拒绝绑定 str**——`CAST(:t AS timestamptz)` 救不了，
#: 那个 cast 在 asyncpg 的类型编解码之后才生效（实测报错：
#: `invalid input for query argument $1: '1970-01-01T00:00:00Z'
#: (expected a datetime.date or datetime.datetime instance, got 'str')`）。
#: 所以字符串只活在线上，绑定前必须还原成原生 Python 类型。
#:
#: 这也让 SQL 里不再需要任何 `CAST`：参数类型由 Python 值决定，与列类型自然对齐。
_KEY_CASTS: dict[str, Callable[[str], object]] = {
    "text": str,
    "bigint": int,
    "integer": int,
    "date": date.fromisoformat,
    "timestamptz": _as_aware_datetime,
}

#: 建议消费者多久做一次全量兜底（天）。游标类接口的固有性质：**以早于已发出
#: 水位的时间戳写进来的行（补数据、时钟回拨、批量重算）永远看不到**。这不是
#: 缺陷，是这类游标换来的东西（服务端无状态）；对策是消费者定期全量重来一次。
FULL_SYNC_RECOMMENDED_AFTER_DAYS = 30.0


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PartitionDataset:
    """按交易日分区的数据集（QuantDB `dt=YYYYMMDD/data.parquet`）。"""

    name: str
    market: str
    grain: str
    description: str
    rel_dir: str
    #: 新鲜度窗（天）。日频数据的正常间隔是 1 个交易日，跨周末/长假会到 3–4 天，
    #: 所以「新鲜」不能按小时算——按小时的阈值套在日频上会永远显示 stale，
    #: 那不是「数据不新鲜」，是**口径错误**（见设计文档 §5）。
    fresh_within_days: float = 4.0
    stale_within_days: float = 14.0

    def policy(self) -> FreshnessPolicy:
        return FreshnessPolicy(
            fresh_within_s=self.fresh_within_days * 86400.0,
            stale_within_s=self.stale_within_days * 86400.0,
        )


@dataclass(frozen=True)
class BlobDataset:
    """整个数据集就是一个文件（会**原地重写**，所以只有 etag 可比对）。"""

    name: str
    market: str
    description: str
    rel_path: str
    fresh_within_days: float = 30.0
    stale_within_days: float = 120.0

    def policy(self) -> FreshnessPolicy:
        return FreshnessPolicy(
            fresh_within_s=self.fresh_within_days * 86400.0,
            stale_within_s=self.stale_within_days * 86400.0,
        )


@dataclass(frozen=True)
class RowDataset:
    """数据库表，按 `(cursor_column, key_columns…)` 元组做行级增量。"""

    name: str
    description: str
    table: str
    #: 「变更时间」列。**必须是每次写入都会刷新的列**，否则更新对游标不可见。
    cursor_column: str
    #: 兜底键：同一时间戳下用来定序、保证不漏行的列（通常是主键）。
    #: 必须是**每行固定不变**的列——它会被写进游标，值一变位置就没了意义。
    key_columns: tuple[str, ...]
    #: 与 `key_columns` 一一对应的列类型（取值必须是 `_KEY_CASTS` 的键）。
    #: 用途是**把游标里的字符串还原成可绑定的 Python 值**（见 `_KEY_CASTS`），
    #: 所以这里的类型必须与列的真实类型一致：声明错了就是一个类型错误，
    #: 不是一个静默的错位置——asyncpg 会当场拒。
    key_casts: tuple[str, ...]
    #: 有 `tenant_id` / `user_id` 列的表必须按凭据过滤——对外凭据属于某个人，
    #: 不加过滤就是把别人的运行记录发给它。
    tenant_scoped: bool
    fresh_within_days: float = 4.0
    stale_within_days: float = 30.0

    def __post_init__(self) -> None:
        """表名/列名是**拼进 SQL** 的，所以在构造时就把它们钉成纯标识符。

        这些值全部来自本模块的常量，本来就不可注入；但 SQL 拼接不该建立在
        「调用方记得别传用户输入」这种约定上。`__post_init__` 让它变成
        **机械可验证**的事实：注册表里放不进一个带引号或分号的名字。
        """
        for ident in (self.table, self.cursor_column, *self.key_columns):
            if not _IDENT_RE.fullmatch(ident):
                raise ValueError(f"数据集 {self.name} 的标识符不合法：{ident!r}")
        if len(self.key_columns) != len(self.key_casts):
            raise ValueError(f"数据集 {self.name} 的 key_columns 与 key_casts 不对齐")
        if not self.key_columns:
            # 允许空兜底键（`updated_at` 本身就唯一），但那样得有人**确认过**
            # 这个前提；默认空元组更可能是漏填。要空就显式写 `()`。
            raise ValueError(f"数据集 {self.name} 没有兜底键：同一时刻多行会漏")
        for cast in self.key_casts:
            if cast not in _KEY_CASTS:
                raise ValueError(f"数据集 {self.name} 的 cast 不在允许集合内：{cast!r}")

    def coerce_key(self, raw: Sequence[str]) -> tuple[object, ...]:
        """游标里的兜底键字符串 → 可绑定的原生值。

        数量对不上时抛 `ValueError`（端点翻成 `400 invalid_cursor`，让消费者
        丢弃游标全量重来）。**猜是不行的**：猜错就是读错位置，而且看起来一切正常。
        """
        if len(raw) != len(self.key_casts):
            raise ValueError(
                f"数据集 {self.name} 的兜底键组件数不对："
                f"期望 {len(self.key_casts)}，收到 {len(raw)}"
            )
        return tuple(
            _KEY_CASTS[cast](item)
            for cast, item in zip(self.key_casts, raw, strict=True)
        )

    def policy(self) -> FreshnessPolicy:
        return FreshnessPolicy(
            fresh_within_s=self.fresh_within_days * 86400.0,
            stale_within_s=self.stale_within_days * 86400.0,
        )


#: 对外暴露的 QuantDB 分区数据集。名字必须在上游 `quantdb_datasets` 里存在
#: 且 `layout == "partition"`（`test_external_api_datasets.py` 钉住）。
#: 这里只放**已验证盘面确为 `dt=` 分区**的那些——`tick_data` 声明是分区、
#: 盘面是平铺，所以在模块 docstring 的排除清单里，不在这里。
_PARTITION_NAMES: tuple[str, ...] = (
    # 1 行情
    "daily_forward",
    "daily_backward",
    "daily_unadjusted",
    "index_daily",
    # 2 基础板块
    "margin_trading",
    # 5 技术衍生
    "valuation",
    "technical_indicators",
    "market_sentiment",
    # 6 ML 数据集
    "features_daily",
    "l1_factors",
    "l2_factors",
    "l1_l2_factors",
)


def _build_partition_datasets() -> tuple[PartitionDataset, ...]:
    """从上游注册表取路径，只在本模块补对外需要的市场/粒度/描述。"""
    upstream = {spec.dataset: spec for spec in _QUANTDB_DATASETS}
    built: list[PartitionDataset] = []
    for name in _PARTITION_NAMES:
        spec = upstream[name]
        built.append(
            PartitionDataset(
                name=spec.dataset,
                market="CN",
                grain="1d",
                description=f"{spec.name}：{spec.note}" if spec.note else spec.name,
                rel_dir=spec.rel_dir,
            )
        )
    return tuple(built)


PARTITION_DATASETS: tuple[PartitionDataset, ...] = _build_partition_datasets()

#: 单文件数据集。文件名在盘面上核对过（`ls` 确认），不是猜的。
#: 目录部分仍取自上游注册表，避免出现第二份路径。
BLOB_DATASETS: tuple[BlobDataset, ...] = (
    BlobDataset(
        name="trading_calendar",
        market="CN",
        description="A股交易日历（外部节点解释任何日期都要先有它）",
        rel_path="2_base_sector/trading_calendar/trading_days.parquet",
    ),
    BlobDataset(
        name="instrument_detail",
        market="CN",
        description="个股详情快照（名称/行业等基本面列）",
        rel_path="2_base_sector/instrument_detail/instrument_detail.parquet",
    ),
    BlobDataset(
        name="sector_concept",
        market="CN",
        description="行业/概念板块成分",
        rel_path="2_base_sector/sector_concept/sector_members.parquet",
    ),
)

#: 表型数据集。**每一列的选择都有实测依据**（见模块 docstring 与设计文档 §4）。
ROW_DATASETS: tuple[RowDataset, ...] = (
    RowDataset(
        name="model_inference_runs",
        description="模型推理运行记录（每次推理一行，含状态与产物路径）",
        table="qm_model_inference_runs",
        cursor_column="updated_at",
        key_columns=("run_id",),
        key_casts=("text",),
        tenant_scoped=True,
    ),
    RowDataset(
        name="model_inference_batches",
        description="模型推理批次",
        table="qm_model_inference_batches",
        cursor_column="updated_at",
        key_columns=("batch_id",),
        key_casts=("text",),
        tenant_scoped=True,
    ),
    RowDataset(
        name="feature_runs",
        description="特征就绪登记（盘中实时链路写的那张表）",
        table="engine_feature_runs",
        cursor_column="updated_at",
        key_columns=("run_id",),
        key_casts=("text",),
        tenant_scoped=True,
    ),
    RowDataset(
        name="news_enrichment",
        description="新闻情感/标签富化结果（按 Huntly 文章 id 对齐原文）",
        table="news_article_enrichment",
        cursor_column="enriched_at",
        key_columns=("huntly_page_id",),
        key_casts=("bigint",),
        # 新闻是平台级共享数据，表里没有 tenant/user 列——不按凭据过滤是有意的，
        # 不是漏了。其余三张表都有，必须过滤。
        tenant_scoped=False,
    ),
)

_PARTITION_BY_NAME = {ds.name: ds for ds in PARTITION_DATASETS}
_BLOB_BY_NAME = {ds.name: ds for ds in BLOB_DATASETS}
_ROW_BY_NAME = {ds.name: ds for ds in ROW_DATASETS}


def get_partition_dataset(name: str) -> PartitionDataset | None:
    return _PARTITION_BY_NAME.get(name)


def get_blob_dataset(name: str) -> BlobDataset | None:
    return _BLOB_BY_NAME.get(name)


def get_row_dataset(name: str) -> RowDataset | None:
    """按名字取表型数据集。**表名只能从这里来**——绝不直接拼进 SQL。"""
    return _ROW_BY_NAME.get(name)


def dataset_kind(name: str) -> Kind | None:
    if name in _PARTITION_BY_NAME:
        return "partition"
    if name in _BLOB_BY_NAME:
        return "blob"
    if name in _ROW_BY_NAME:
        return "row"
    return None


# ---------------------------------------------------------------------------
# 路径解析与安全
# ---------------------------------------------------------------------------


def _real(path: Path) -> Path | None:
    try:
        return Path(os.path.realpath(path))
    except OSError:
        return None


def resolve_under_root(rel_path: str) -> Path | None:
    """把注册表里的相对路径解析成绝对路径，并保证它**仍在 QuantDB 根之下**。

    越界判定用 `os.path.commonpath`，**不是 `startswith`**：后者在
    `root=/a/b` 对 `/a/bc/...` 时会误判为「在根下」（前缀相同但不是子路径）。

    根目录每次现算、不缓存：`QM_QUANTDB_DATA_DIR` 可能在进程存活期间被改，
    而且是**挂载/便携包两种部署**下解析结果不同的东西（`quantdb_paths` 的
    「非空才采纳」回退逻辑正是为此）。缓存它等于把启动时刻的挂载状态钉死。
    """
    if not rel_path:
        return None
    root = resolve_quantdb_dir()
    real_root = _real(root)
    candidate = _real(root / rel_path)
    if real_root is None or candidate is None:
        return None
    try:
        if os.path.commonpath([str(real_root), str(candidate)]) != str(real_root):
            return None
    except ValueError:
        # 不同盘符（Windows）/ 绝对相对混用 —— commonpath 直接抛，判为越界
        return None
    return candidate


def normalize_partition(value: str) -> str | None:
    """`YYYY-MM-DD` → `YYYYMMDD`；任何别的写法返回 None。

    正则挡住形状，`date.fromisoformat` 挡住数值（`2026-13-45` 形状对、值越界）。
    两层都要：只靠正则会把 13 月放进去，只靠 fromisoformat 会接受
    `2026-1-5` 这种我们没打算支持的写法。
    """
    if not isinstance(value, str) or not _PARTITION_RE.fullmatch(value):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed.strftime("%Y%m%d")


def partition_file(ds: PartitionDataset, partition: str) -> Path | None:
    """某个分区对应的 parquet 绝对路径；分区名非法或越界时 None。"""
    compact = normalize_partition(partition)
    if compact is None:
        return None
    return resolve_under_root(f"{ds.rel_dir}/dt={compact}/data.parquet")


def blob_file(ds: BlobDataset) -> Path | None:
    return resolve_under_root(ds.rel_path)


def dataset_dir(rel_dir: str) -> Path | None:
    return resolve_under_root(rel_dir)


# ---------------------------------------------------------------------------
# 分区枚举（只读目录名，不 stat、不读文件内容）
# ---------------------------------------------------------------------------


def list_partitions(
    ds: PartitionDataset,
    *,
    since: str | None = None,
    until: str | None = None,
) -> list[str]:
    """数据集的分区日期（`YYYY-MM-DD`，升序）。

    **只读目录名**：一次 `scandir` 就够，不 stat、更不读 parquet。
    `l2_factors` 这类数据集的历史分区有两千多个，逐分区 `stat` 会让一个
    JSON 端点变成「几万个系统调用」。逐分区 `stat` 只发生在**分页之后**的
    那一页上（见 `data._stat_partitions`），不在这里。

    上游 `l1_factors` / `l2_factors` 目录下还有非分区的 `report/` 子目录，
    所以按 `dt=` 前缀过滤，不是「把所有子目录当分区」。
    """
    directory = dataset_dir(ds.rel_dir)
    if directory is None or not directory.is_dir():
        return []

    out: list[str] = []
    try:
        with os.scandir(directory) as it:
            for entry in it:
                match = _PARTITION_DIR_RE.fullmatch(entry.name)
                if match is None:
                    continue
                # is_dir() 走 getdents 里的 d_type，不额外 stat
                if not entry.is_dir():
                    continue
                raw = match.group(1)
                try:
                    day = date(int(raw[:4]), int(raw[4:6]), int(raw[6:8])).isoformat()
                except ValueError:
                    # `dt=20261345` 这种脏目录：跳过，不让它变成一个假分区
                    continue
                out.append(day)
    except OSError:
        return []
    out.sort()
    if since is not None:
        out = [d for d in out if d >= since]
    if until is not None:
        out = [d for d in out if d <= until]
    return out


def partition_date_ts(partition: str) -> float:
    """分区日期 → epoch 秒（该日 00:00 UTC），用于新鲜度分级。

    用 UTC 零点而不是「当天某个收盘时刻」：这里要回答的是「我的镜像新不新」，
    不是「收盘数据到了吗」。多算进去的时区差最多几小时，而新鲜度窗是按**天**
    设的（4 天），压不到边界上。
    """
    day = date.fromisoformat(partition)
    return datetime.combine(day, time.min, tzinfo=UTC).timestamp()


# ---------------------------------------------------------------------------
# ETag（必须与 FileResponse 逐字一致）
# ---------------------------------------------------------------------------


def etag_of(stat_result: os.stat_result) -> str:
    """`stat` → ETag，**逐字复刻 starlette `FileResponse.set_stat_headers`**。

    为什么要复刻而不是自己定一套：清单端点给出的 etag 和文件端点返回的 ETag
    必须是同一个字符串。不一致的话，消费者「先问清单、再带 `If-None-Match`
    去取文件」永远命中不了，每次都得整份重下——而且没有任何报错，只是慢。

    所以这里连**引号**都保留（starlette 产出的 ETag 是带引号的强校验器），
    连 `str(float)` 的写法都照抄。
    """
    base = f"{stat_result.st_mtime}-{stat_result.st_size}"
    digest = hashlib.md5(base.encode(), usedforsecurity=False).hexdigest()
    return f'"{digest}"'


def stat_or_none(path: Path) -> os.stat_result | None:
    """`stat` 一个普通文件；不存在 / 是目录 / 无权限一律 None。

    只 `stat` 一次就把「是不是普通文件」判掉：`os.path.isfile` 会再走一次
    系统调用，而这个函数在清单端点是**按分区逐个调用**的（两千多个分区时，
    多一次 stat 就是多两千次系统调用）。
    """
    try:
        stat_result = path.stat()
    except OSError:
        return None
    if not stat.S_ISREG(stat_result.st_mode):
        return None
    return stat_result


__all__ = [
    "BLOB_DATASETS",
    "BlobDataset",
    "Kind",
    "PARTITION_DATASETS",
    "PartitionDataset",
    "ROW_DATASETS",
    "RowDataset",
    "blob_file",
    "dataset_dir",
    "dataset_kind",
    "etag_of",
    "get_blob_dataset",
    "get_partition_dataset",
    "get_row_dataset",
    "list_partitions",
    "normalize_partition",
    "partition_date_ts",
    "partition_file",
    "resolve_under_root",
    "stat_or_none",
]
