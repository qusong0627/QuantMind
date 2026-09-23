"""对外数据面的数据集注册表与路径解析。

这个文件保护的是数据面**唯一**能被用户输入碰到磁盘/SQL 的地方。
`{name}` 与 `{partition}` 都来自 URL，都会变成路径片段或表名，所以这里的
每一条断言都在问同一个问题：**这个输入能不能让它走到注册表外面去。**

为什么要单独测注册表而不是只测端点
----------------------------------
端点是「一次请求」，注册表是**不变量**。上面那三个数据集清单将来会加条目，
加的人多半只跑端点测试——那时漏掉的恰恰是「新加的名字在上游存在吗」
「layout 对得上吗」这类只在整表上才看得出来的问题。

上游唯一事实源
--------------
`rel_dir` 一律取自 `backend/shared/quantdb_datasets.py`。本文件钉住这一点，
是因为本仓在这件事上吃过的亏是「一份清单手抄八遍、八遍都漏了一项」。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.services.api.routers.external import datasets as registry
from backend.shared.quantdb_datasets import get_dataset_spec


@pytest.fixture()
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """空的 QuantDB 根。`resolve_quantdb_dir` 要求目录**非空**才采纳。"""
    (tmp_path / "placeholder.txt").write_text("x")
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# 注册表的不变量
# ---------------------------------------------------------------------------


def test_partition_names_exist_upstream_with_partition_layout() -> None:
    """登记的名字必须在上游存在，且上游声明的布局确实是 `partition`。

    这是防「凭印象登记」的那一条：`tick_data` 就是这么露馅的——上游写着
    `layout="partition"`，盘面却是平铺的 `{SYMBOL}_{YYYYMMDD}.parquet`。
    上游声明本身也可能错，所以本测试只保证**声明层面对齐**，
    盘面核对靠下面的 `test_registered_dirs_are_the_upstream_dirs`。
    """
    for ds in registry.PARTITION_DATASETS:
        spec = get_dataset_spec(ds.name)
        assert spec is not None, f"{ds.name} 不在上游 quantdb_datasets 里"
        assert spec.layout == "partition", (
            f"{ds.name} 上游声明是 {spec.layout!r}，不是 partition——"
            "按分区取会静默查空"
        )
        assert ds.rel_dir == spec.rel_dir, (
            f"{ds.name} 的 rel_dir 与上游不一致：路径出现了第二份"
        )


def test_registered_names_are_unique_across_kinds() -> None:
    """三类注册表之间不能重名：`dataset_kind` 是顺序判定的，重名会让
    后一类永远取不到（而且没有任何报错）。"""
    names = [
        *(d.name for d in registry.PARTITION_DATASETS),
        *(d.name for d in registry.BLOB_DATASETS),
        *(d.name for d in registry.ROW_DATASETS),
    ]
    assert len(names) == len(set(names)), f"注册表里有重名：{names}"


def test_every_registered_name_resolves_to_its_kind() -> None:
    """正反两面：登记的名字能查到，随便一个名字查不到（不是「找不到就返回第一个」）。"""
    for ds in registry.PARTITION_DATASETS:
        assert registry.dataset_kind(ds.name) == "partition"
    for ds in registry.BLOB_DATASETS:
        assert registry.dataset_kind(ds.name) == "blob"
    for ds in registry.ROW_DATASETS:
        assert registry.dataset_kind(ds.name) == "row"
    assert registry.dataset_kind("definitely_not_a_dataset") is None
    assert registry.dataset_kind("engine_signal_scores") is None, (
        "engine_signal_scores 是**刻意排除**的（没有可用的游标列，见模块 docstring）"
    )


def test_freshness_windows_are_day_scale_not_quote_scale() -> None:
    """**日频数据不能套行情阈值。**

    `freshness.quote_policy()` 是 60s/300s 的**行情**口径，直接拿来做
    「这个数据集新不新」的判定，结果是：最新分区是今天，但只要现在不是
    UTC 零点后的头一分钟，它就一直显示 `stale`。那不是「数据不新鲜」，
    是**口径错误**——而且它会训练所有人忽略这个字段，比没有这个字段更糟。

    所以对外数据面**自建**策略（按天），并且不新增任何阈值 env
    （`test_freshness_latency.py` 有源守卫，框架的阈值读取点只有 `quote_policy`）。
    """
    from backend.shared.freshness import quote_policy

    quote = quote_policy()
    for ds in (
        *registry.PARTITION_DATASETS,
        *registry.BLOB_DATASETS,
        *registry.ROW_DATASETS,
    ):
        policy = ds.policy()
        assert policy.fresh_within_s >= 86400.0, (
            f"{ds.name} 的「新鲜」窗小于一天（{policy.fresh_within_s}s）——"
            "日频数据会长期显示 stale"
        )
        assert policy.stale_within_s > policy.fresh_within_s, (
            f"{ds.name} 的两条阈值线反了：stale 窗必须比 fresh 窗宽"
        )
        assert (policy.fresh_within_s, policy.stale_within_s) != (
            quote.fresh_within_s,
            quote.stale_within_s,
        ), f"{ds.name} 直接复用了行情阈值——见本测试 docstring"


def test_row_datasets_declare_identifiers_only() -> None:
    """表名/列名是拼进 SQL 的。构造期就把非标识符挡掉。"""
    for ds in registry.ROW_DATASETS:
        assert ds.key_columns, f"{ds.name} 没有兜底键：同一时刻多行会漏"
        assert len(ds.key_columns) == len(ds.key_casts)

    from backend.services.api.routers.external.datasets import RowDataset

    def _make(**kw: object) -> RowDataset:
        base: dict[str, object] = {
            "name": "x",
            "description": "",
            "table": "t",
            "cursor_column": "updated_at",
            "key_columns": ("id",),
            "key_casts": ("text",),
            "tenant_scoped": False,
        }
        base.update(kw)
        return RowDataset(**base)  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        _make(table="t; DROP TABLE users")
    with pytest.raises(ValueError):
        _make(key_columns=("id = '1' OR '1'='1",), key_casts=("text",))
    with pytest.raises(ValueError):
        _make(key_casts=("text; --",))
    with pytest.raises(ValueError):
        _make(key_casts=("int4",))  # 不在允许集合内
    with pytest.raises(ValueError):
        _make(key_columns=("a", "b"), key_casts=("text",))  # 不对齐
    with pytest.raises(ValueError):
        _make(key_columns=(), key_casts=())  # 没有兜底键


def test_coerce_key_returns_native_values_not_strings() -> None:
    """**绑定前必须还原成原生类型。**

    游标在线上是纯字符串，但 asyncpg 从预备语句拿到参数 OID 后拒绝绑定
    `str`（`CAST(:t AS timestamptz)` 救不了，那个 cast 在类型编解码之后）。
    第一版就是这么写的，实测报错：

        invalid input for query argument $1: '1970-01-01T00:00:00Z'
        (expected a datetime.date or datetime.datetime instance, got 'str')

    所以这一条不是风格偏好，是**上一版跑不起来的原因**。
    """
    ds = registry.get_row_dataset("news_enrichment")
    assert ds is not None
    (value,) = ds.coerce_key(["689"])
    assert isinstance(value, int) and value == 689

    runs = registry.get_row_dataset("model_inference_runs")
    assert runs is not None
    (value,) = runs.coerce_key(["run-abc"])
    assert isinstance(value, str)

    with pytest.raises(ValueError):
        runs.coerce_key(["a", "b"])  # 组件数不对：宁可报错也不猜

    from backend.services.api.routers.external.datasets import RowDataset

    ts = RowDataset(
        name="x",
        description="",
        table="t",
        cursor_column="updated_at",
        key_columns=("d",),
        key_casts=("timestamptz",),
        tenant_scoped=False,
    )
    (value,) = ts.coerce_key(["2026-09-23T12:34:56.123456Z"])
    assert isinstance(value, datetime)
    assert value.microsecond == 123456
    with pytest.raises(ValueError):
        # naive 一律拒：本仓「无时区视为 UTC」的约定在这里是「猜位置」
        ts.coerce_key(["2026-09-23T12:34:56.123456"])


# ---------------------------------------------------------------------------
# 分区名规范化
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "want"),
    [("2026-09-22", "20260922"), ("1990-01-01", "19900101"), ("2024-02-29", "20240229")],
)
def test_normalize_partition_accepts_canonical(raw: str, want: str) -> None:
    assert registry.normalize_partition(raw) == want


@pytest.mark.parametrize(
    "raw",
    [
        "2026-9-2",  # 变体写法：不做日期容错，容错就是猜
        "20260922",  # 紧凑写法（对外只认带横杠的）
        "2026-13-45",  # 形状对、值越界
        "2023-02-29",  # 非闰年
        "2026-09-22/../../etc",  # 想拼路径
        "2026-09-22 ",  # 尾空格
        "2026-09-22\n",
        "",
        "dt=20260922",
        "../../../etc/passwd",
    ],
)
def test_normalize_partition_rejects_anything_else(raw: str) -> None:
    assert registry.normalize_partition(raw) is None


# ---------------------------------------------------------------------------
# 路径安全
# ---------------------------------------------------------------------------


def test_resolve_under_root_allows_normal_paths(root: Path) -> None:
    (root / "1_market").mkdir()
    got = registry.resolve_under_root("1_market")
    assert got is not None
    assert Path(os.path.realpath(root)) == Path(os.path.realpath(got.parent))


@pytest.mark.parametrize(
    "rel",
    [
        "../outside",
        "1_market/../../outside",
        "/etc/passwd",  # 绝对路径：`root / "/etc/passwd"` 在 pathlib 里会**丢掉 root**
        "",
    ],
)
def test_resolve_under_root_blocks_escape(root: Path, rel: str) -> None:
    (root / "1_market").mkdir()
    assert registry.resolve_under_root(rel) is None


def test_commonpath_catches_the_sibling_prefix_case(tmp_path: Path) -> None:
    """**`commonpath` 而不是 `startswith` 的那一条。**

    `root = /a/b`、目标 `/a/bc/x`：字符串前缀判定会认为「在根下」，
    而它其实是**另一个目录**。这里把两个目录并排造出来（真实的越界写法），
    确认解析结果是 None 而不是 `/a/bc/x`。
    """
    base = tmp_path / "base"
    inside = base / "quantdb"
    sibling = base / "quantdb_backup"
    inside.mkdir(parents=True)
    sibling.mkdir()
    (inside / "keep.txt").write_text("x")

    monkeyed = tmp_path / "base" / "quantdb"
    assert monkeyed.exists()
    assert sibling.exists()
    assert str(sibling).startswith(str(inside)), "前提：sibling 确实以 inside 为字符串前缀"

    monkeyed_env = os.environ.get("QM_QUANTDB_DATA_DIR")
    os.environ["QM_QUANTDB_DATA_DIR"] = str(inside)
    try:
        assert registry.resolve_under_root("../quantdb_backup") is None, (
            "越界到字符串前缀相同的兄弟目录没被挡住——判定退化成 startswith 了"
        )
    finally:
        if monkeyed_env is None:
            os.environ.pop("QM_QUANTDB_DATA_DIR", None)
        else:
            os.environ["QM_QUANTDB_DATA_DIR"] = monkeyed_env


def test_partition_file_stays_inside_the_dataset_dir(root: Path) -> None:
    ds = registry.PARTITION_DATASETS[0]
    target = root / ds.rel_dir / "dt=20260922"
    target.mkdir(parents=True)
    (target / "data.parquet").write_bytes(b"PAR1")

    got = registry.partition_file(ds, "2026-09-22")
    assert got is not None and got.exists()
    assert registry.partition_file(ds, "../../etc/passwd") is None
    assert registry.partition_file(ds, "2026-13-45") is None


# ---------------------------------------------------------------------------
# 分区枚举
# ---------------------------------------------------------------------------


def test_list_partitions_reads_dir_names_only(root: Path) -> None:
    """只读目录名：不 stat、不读文件内容（单 worker 的事件循环容不下一次大读）。"""
    ds = registry.PARTITION_DATASETS[0]
    base = root / ds.rel_dir
    for name in ("dt=20260922", "dt=20260923", "dt=20260101"):
        (base / name).mkdir(parents=True)
    # 上面几个目录里**故意不放 data.parquet**：枚举结果不该依赖文件在不在
    assert registry.list_partitions(ds) == ["2026-01-01", "2026-09-22", "2026-09-23"]


def test_list_partitions_skips_dirty_and_non_partition_dirs(root: Path) -> None:
    """`dt=20261345`（脏日期）与 `report/`（上游 l1/l2 目录下真实存在）
    都不能变成假分区——前者会让 `partition_date_ts` 抛，后者会变成一个空分区。"""
    ds = registry.PARTITION_DATASETS[0]
    base = root / ds.rel_dir
    (base / "dt=20260922").mkdir(parents=True)
    (base / "dt=20261345").mkdir()
    (base / "report").mkdir()
    (base / "dt=2026092").mkdir()  # 位数不足
    (base / "dt=20260922.bak").mkdir()
    (base / "dt=20260922").joinpath("data.parquet").write_bytes(b"PAR1")
    assert registry.list_partitions(ds) == ["2026-09-22"]


def test_list_partitions_filters_and_sorts(root: Path) -> None:
    ds = registry.PARTITION_DATASETS[0]
    base = root / ds.rel_dir
    for name in ("dt=20260901", "dt=20260910", "dt=20260920"):
        (base / name).mkdir(parents=True)
    assert registry.list_partitions(ds, since="2026-09-10") == [
        "2026-09-10",
        "2026-09-20",
    ]
    assert registry.list_partitions(ds, until="2026-09-10") == [
        "2026-09-01",
        "2026-09-10",
    ]
    # since/until 都是**闭区间**：分页时下一页从最后一个已返回分区继续，
    # 开区间会让「恰好落在边界上的那天」永远取不到。
    assert registry.list_partitions(ds, since="2026-09-10", until="2026-09-10") == [
        "2026-09-10"
    ]


def test_list_partitions_on_missing_dir_is_empty_not_an_error(root: Path) -> None:
    """没挂盘的数据集：`available=false` 要靠这个空列表得出，不能抛。"""
    assert registry.list_partitions(registry.PARTITION_DATASETS[-1]) == []


# ---------------------------------------------------------------------------
# etag：必须与 starlette 逐字一致
# ---------------------------------------------------------------------------


def test_etag_matches_starlette_file_response_exactly(tmp_path: Path) -> None:
    """**这一条是「先问再下」能不能成立的关键。**

    清单端点给的 etag 与文件端点返回的 `ETag` 必须是**同一个字符串**。
    不一致的话，消费者带 `If-None-Match` 永远命中不了，每轮都得整份重下
    ——没有报错，只是慢，而慢到没人会去查。

    starlette 的算法（1.6.0 `FileResponse.set_stat_headers`）是
    `md5(f"{st_mtime}-{st_size}")`，**带引号**。这里直接用它的实现比对，
    而不是复述一遍公式（复述一遍就等于把两处实现都写在测试里）。
    """
    from fastapi import FastAPI
    from fastapi.responses import FileResponse
    from fastapi.testclient import TestClient

    path = tmp_path / "x.parquet"
    path.write_bytes(b"PAR1" + b"\x00" * 500)
    stat_result = path.stat()

    # 走一次真实请求，比对**线路上实际发出的头**——而不是复述一遍公式：
    # 直接读 `response.headers` 拿不到（starlette 是在 `__call__` 里才
    # `set_stat_headers` 的，构造完那一刻还没有 etag）。
    app = FastAPI()

    @app.get("/f")
    async def _f() -> FileResponse:  # pragma: no cover - 桩
        return FileResponse(path, media_type="application/octet-stream")

    served = TestClient(app).get("/f").headers["etag"]
    assert served == registry.etag_of(stat_result), (
        f"自算的 etag（{registry.etag_of(stat_result)}）与 FileResponse "
        f"实际发出的 ETag（{served}）不一致——盘面清单与文件端点会对不上，"
        "消费者每轮全量重下且没有任何报错"
    )
    assert registry.etag_of(stat_result).startswith('"')  # 强校验器带引号


def test_etag_changes_when_the_file_is_rewritten(tmp_path: Path) -> None:
    """派生产物会**整段重算**（本仓有过真实反例）。承诺只有一条：
    **重写后 etag 一定变**。"""
    path = tmp_path / "x.parquet"
    path.write_bytes(b"PAR1" + b"\x00" * 500)
    before = registry.etag_of(path.stat())
    path.write_bytes(b"PAR1" + b"\x01" * 500)
    os.utime(path, (1e9, 1e9))
    assert registry.etag_of(path.stat()) != before


def test_stat_or_none_rejects_directories_and_missing(tmp_path: Path) -> None:
    """目录不是文件：`stat_or_none` 必须是 None，否则 `FileResponse` 会在
    流式阶段抛 `IsADirectoryError`（那时响应头已经发出去了）。"""
    directory = tmp_path / "d"
    directory.mkdir()
    assert registry.stat_or_none(directory) is None
    assert registry.stat_or_none(tmp_path / "missing") is None
    file = tmp_path / "f"
    file.write_bytes(b"x")
    assert registry.stat_or_none(file) is not None


def test_partition_date_ts_is_utc_midnight() -> None:
    assert registry.partition_date_ts("2026-09-22") == datetime(
        2026, 9, 22, tzinfo=timezone.utc
    ).timestamp()
