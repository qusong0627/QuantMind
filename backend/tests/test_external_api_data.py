"""对外数据面端点（`/api/ext/v1/data/*`）的行为。

这个文件测的不是「能返回 200」，而是三条**错了不会报错**的性质：

1. **清单里的 etag 与文件端点的 ETag 是同一个字符串**。不一致的话，
   消费者「先问清单、再带 `If-None-Match` 取文件」永远命中不了——没有报错，
   只是每轮整份重下。
2. **分页不漏不重**。`next_since` 给错一天，边界那天的分区会被跳过或重来；
   两种都不会抛异常，只是镜像少一天/多下一遍。
3. **游标参数以原生 Python 类型绑定**。第一版用字符串 + `CAST(:t AS timestamptz)`，
   实测 asyncpg 直接拒（`invalid input for query argument $1: '1970-01-01T00:00:00Z'`），
   整条链路跑不起来。崩掉是好事——坏的是「不崩但走错位置」的那类，
   所以下面还钉了「游标属于别的数据集时 400 而不是照用」。

不连库、不连盘面真实数据：QuantDB 根换成 tmp 目录，`get_session` 换成假的。
端点逻辑（校验顺序、状态码、绑定参数）全部真跑。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.services.api.routers.external import datasets as registry
from backend.services.api.routers.external import router as router_module
from backend.services.api.routers.external.auth import (
    ExternalPrincipal,
    require_external_principal,
)
from backend.services.api.routers.external.cursor import encode_cursor
from backend.shared import live_trading_gate as gate

PRINCIPAL = ExternalPrincipal(
    access_key="qm_test_dataplane",
    user_id="10000001",
    tenant_id="default",
    permissions=("data.read",),
    session_expires_at=1_800_000_000,
)

PARTITION_DS = registry.PARTITION_DATASETS[0]
BLOB_DS = registry.BLOB_DATASETS[0]
ROW_DS = registry.get_row_dataset("news_enrichment")
RUNS_DS = registry.get_row_dataset("model_inference_runs")
assert ROW_DS is not None and RUNS_DS is not None

T_NEW = datetime(2026, 9, 23, 10, 0, 0, 123456, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 盘面替身
# ---------------------------------------------------------------------------


@pytest.fixture()
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一个只有 `PARTITION_DS` 与 `BLOB_DS` 的 QuantDB 根。

    别的数据集**故意不建目录**：`available=false` 那条路径必须有真实输入。
    """
    (tmp_path / "placeholder.txt").write_text("x")
    base = tmp_path / PARTITION_DS.rel_dir
    for day, payload in (
        ("20260920", b"A" * 300),
        ("20260921", b"B" * 400),
        ("20260922", b"C" * 500),
        ("20260923", b"D" * 600),
    ):
        (base / f"dt={day}").mkdir(parents=True)
        (base / f"dt={day}" / "data.parquet").write_bytes(b"PAR1" + payload)
    blob = tmp_path / BLOB_DS.rel_path
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(b"PAR1" + b"Z" * 1000)
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# 库替身
# ---------------------------------------------------------------------------


class _Row:
    """最小行替身：端点只用到 `._mapping`。"""

    def __init__(self, **kw: Any) -> None:
        self._mapping = dict(kw)


class _FakeResult:
    def __init__(self, rows: list[_Row] | None = None, scalar: Any = None) -> None:
        self._rows = rows or []
        self._scalar = scalar

    def fetchall(self) -> list[_Row]:
        return list(self._rows)

    def scalar(self) -> Any:
        return self._scalar


class _FakeSession:
    """把 `SELECT MAX(...)` 与翻页查询分开应答，并**记下绑定参数**。"""

    def __init__(self, rows: list[_Row], newest: Any) -> None:
        self.rows = rows
        self.newest = newest
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, sql: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        text = str(sql)
        self.calls.append((text, dict(params or {})))
        if "SELECT MAX" in text:
            return _FakeResult(scalar=self.newest)
        return _FakeResult(rows=self.rows)


class _FakeCtx:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session

    async def __aenter__(self) -> _FakeSession:
        return self.session

    async def __aexit__(self, *_exc: object) -> bool:
        return False


@pytest.fixture()
def db(monkeypatch: pytest.MonkeyPatch) -> _FakeSession:
    """默认：一行新鲜数据。单个测试可以换掉 `rows` / `newest`。"""
    session = _FakeSession(
        rows=[_Row(enriched_at=T_NEW, huntly_page_id=42, sentiment_label="neutral")],
        newest=T_NEW,
    )
    import backend.shared.database_manager_v2 as db_module

    monkeypatch.setattr(db_module, "get_session", lambda **_kw: _FakeCtx(session))
    return session


@pytest.fixture()
def client(root: Path, db: _FakeSession, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """完整形态：闸门 + 路由（与真部署一致）。"""
    monkeypatch.setenv(gate.ENV_KEY, "false")
    app = FastAPI()
    # 走**真实的** router 对象（含它自己 include 数据面这一步），
    # 前缀用闸门的常量——免得测试里出现第二个命名空间出处。
    app.include_router(router_module.router, prefix=gate.EXT_API)
    gate.install_live_trading_gate_middleware(app, "test")
    app.dependency_overrides[require_external_principal] = lambda: PRINCIPAL
    return TestClient(app)


@pytest.fixture()
def raw(root: Path, db: _FakeSession) -> TestClient:
    """**不挂闸门**：单独测 handler 自己那一层校验。

    数据面的路径安全是叠着的，闸门（形状白名单）在最外层，handler 的
    注册表/正则/`realpath` 在里层。用带闸门的 client 测里层是测不到的——
    闸门会先把请求拦掉（403），断言就变成了「闸门拦住了」。
    两层各测各的，见 `test_gate_rejects_near_miss_shapes`。
    """
    app = FastAPI()
    app.include_router(router_module.router, prefix=gate.EXT_API)
    app.dependency_overrides[require_external_principal] = lambda: PRINCIPAL
    return TestClient(app)


def _url(path: str) -> str:
    return f"{gate.EXT_API}{path}"


# ---------------------------------------------------------------------------
# 可用性索引
# ---------------------------------------------------------------------------


def test_datasets_index_reports_mounted_and_unmounted(client: TestClient) -> None:
    body = client.get(_url("/data/datasets")).json()
    assert body["server_time"] > 0
    by_name = {d["name"]: d for d in body["datasets"]}

    mounted = by_name[PARTITION_DS.name]
    assert mounted["available"] is True
    assert mounted["kind"] == "partition"
    assert mounted["partition_count"] == 4
    assert mounted["first_partition"] == "2026-09-20"
    assert mounted["last_partition"] == "2026-09-23"
    assert mounted["as_of"] == "2026-09-23T00:00:00Z", (
        "as_of 是**数据自己**的时间（最新分区日），不是 server_time"
    )

    # 注册表里有、但这台机器上没挂盘：如实报不可用，而不是从清单里消失
    assert by_name["l2_factors"]["available"] is False
    assert by_name["l2_factors"]["kind"] == "partition"

    assert by_name[BLOB_DS.name]["available"] is True
    assert by_name[BLOB_DS.name]["kind"] == "blob"
    assert by_name[BLOB_DS.name]["bytes"] == 1004

    # 表型数据集在库替身下也是 available（newest 有值）
    assert by_name["news_enrichment"]["available"] is True
    assert by_name["news_enrichment"]["kind"] == "row"
    assert by_name["news_enrichment"]["as_of"] == "2026-09-23T10:00:00.123456Z"


def test_unmounted_dataset_reports_no_as_of_not_zero(client: TestClient) -> None:
    """**「没有数据」绝不能显示成「数据是现在的」。**"""
    for entry in client.get(_url("/data/datasets")).json()["datasets"]:
        if not entry["available"]:
            assert entry["as_of"] is None
            assert entry["freshness"] == "unavailable"
            assert entry["partition_count"] in (None, 0)


# ---------------------------------------------------------------------------
# 分区清单
# ---------------------------------------------------------------------------


def test_partition_listing_carries_bytes_and_etag(client: TestClient) -> None:
    body = client.get(_url(f"/data/datasets/{PARTITION_DS.name}/partitions")).json()
    assert body["dataset"] == PARTITION_DS.name
    assert body["as_of"] == "2026-09-23T00:00:00Z"
    assert [p["partition"] for p in body["partitions"]] == [
        "2026-09-20",
        "2026-09-21",
        "2026-09-22",
        "2026-09-23",
    ]
    first = body["partitions"][0]
    assert first["bytes"] == 304, "字节数是**文件**大小，不是分区目录"
    assert first["etag"].startswith('"') and first["etag"].endswith('"')
    assert first["mtime"].endswith("Z") and "T" in first["mtime"]
    assert body["truncated"] is False and body["next_since"] is None


def test_partitions_listing_and_file_endpoint_share_one_etag(client: TestClient) -> None:
    """**本文件最承重的一条。**

    清单说「这个分区是 etag X」，消费者带着 `If-None-Match: X` 去取文件，
    必须命中 304。命中不了的话，每次同步都要重下全量——而且没有任何报错。
    """
    listing = client.get(_url(f"/data/datasets/{PARTITION_DS.name}/partitions")).json()
    entry = next(p for p in listing["partitions"] if p["partition"] == "2026-09-22")
    file_url = _url(
        f"/data/datasets/{PARTITION_DS.name}/partitions/2026-09-22/file"
    )

    fresh = client.get(file_url)
    assert fresh.status_code == 200
    assert fresh.headers["etag"] == entry["etag"], (
        "清单里的 etag 与文件端点返回的 ETag 不是同一个字符串——"
        "消费者的「先问再下」永远命中不了"
    )
    assert fresh.headers["content-type"] == "application/vnd.apache.parquet"
    assert fresh.headers["accept-ranges"] == "bytes"
    assert len(fresh.content) == entry["bytes"]

    revalidate = client.get(file_url, headers={"If-None-Match": entry["etag"]})
    assert revalidate.status_code == 304
    assert revalidate.headers["etag"] == entry["etag"]
    assert revalidate.content == b""


@pytest.mark.parametrize(
    "header",
    [
        "W/{etag}",  # 弱校验器：RFC 7232 允许，只做字符串相等会漏判
        'W/{etag}, "other"',
        '"other", {etag}',  # 逗号列表
        "{etag}, \"other\"",
        " {etag} ",  # 空白：列表分隔后允许
        "*",
    ],
)
def test_if_none_match_accepts_standard_forms(client: TestClient, header: str) -> None:
    """`{etag}` 自带引号（强校验器），模板里**不能再加一层**——这是最容易写错的一处。"""
    listing = client.get(_url(f"/data/datasets/{PARTITION_DS.name}/partitions")).json()
    etag = listing["partitions"][-1]["etag"]
    assert etag.startswith('"'), "前提：etag 自带引号"
    url = _url(f"/data/datasets/{PARTITION_DS.name}/partitions/2026-09-23/file")
    got = client.get(url, headers={"If-None-Match": header.format(etag=etag)})
    assert got.status_code == 304, (
        f"If-None-Match: {header.format(etag=etag)} 没被认出——"
        "标准客户端会因此每次整份重下"
    )


def test_stale_etag_gets_the_body(client: TestClient) -> None:
    """反面：etag 变了必须给 200 + 内容，不能永远 304。"""
    url = _url(f"/data/datasets/{PARTITION_DS.name}/partitions/2026-09-23/file")
    got = client.get(url, headers={"If-None-Match": '"definitely-not-it"'})
    assert got.status_code == 200
    assert len(got.content) == 604


def test_range_request_resumes(client: TestClient) -> None:
    """断点续传是白送的（`FileResponse`），但「白送」这件事值得钉一次——
    哪天换掉它，这条会红。"""
    url = _url(f"/data/datasets/{PARTITION_DS.name}/partitions/2026-09-22/file")
    got = client.get(url, headers={"Range": "bytes=8-15"})
    assert got.status_code == 206
    assert got.content == b"C" * 8
    assert got.headers["content-range"].startswith("bytes 8-15/")


def test_pagination_covers_every_partition_exactly_once(client: TestClient) -> None:
    """**不漏不重**。`next_since` 给成开区间的话，边界那天会被跳过——
    而跳过的表现是「镜像少一天」，一路绿灯。"""
    seen: list[str] = []
    since: str | None = None
    for _ in range(10):
        params = {"limit": 2, **({"since": since} if since else {})}
        body = client.get(
            _url(f"/data/datasets/{PARTITION_DS.name}/partitions"), params=params
        ).json()
        seen.extend(p["partition"] for p in body["partitions"])
        if not body["truncated"]:
            break
        since = body["next_since"]
    else:  # pragma: no cover - 分页没有终止
        pytest.fail("分页没有终止")
    assert seen == ["2026-09-20", "2026-09-21", "2026-09-22", "2026-09-23"]
    assert len(seen) == len(set(seen)), f"分页出现重复：{seen}"


def test_partition_listing_filters(client: TestClient) -> None:
    body = client.get(
        _url(f"/data/datasets/{PARTITION_DS.name}/partitions"),
        params={"since": "2026-09-21", "until": "2026-09-22"},
    ).json()
    assert [p["partition"] for p in body["partitions"]] == ["2026-09-21", "2026-09-22"]
    assert body["as_of"] == "2026-09-22T00:00:00Z", "as_of 是**筛选后**最新的那个"


@pytest.mark.parametrize(
    ("param", "value"),
    [("since", "2026-9-1"), ("since", "2026-13-45"), ("until", "yesterday")],
)
def test_bad_date_filters_are_400(client: TestClient, param: str, value: str) -> None:
    got = client.get(
        _url(f"/data/datasets/{PARTITION_DS.name}/partitions"), params={param: value}
    )
    assert got.status_code == 400
    assert got.json()["detail"] == f"invalid_{param}"


# ---------------------------------------------------------------------------
# 错误分类与路径安全
# ---------------------------------------------------------------------------


def test_unknown_dataset_is_404(client: TestClient) -> None:
    for url in (
        "/data/datasets/nope/partitions",
        "/data/datasets/nope/blob",
        "/data/nope/changes",
    ):
        got = client.get(_url(url))
        assert got.status_code == 404, url
        assert got.json()["detail"] == "dataset_not_found"


@pytest.mark.parametrize("name", ["....", "nope", "daily_forward_x", "tick_data"])
def test_dataset_name_is_never_used_as_a_path(raw: TestClient, name: str) -> None:
    """名字先查表、查不到即 404 ——**绝不拼接到路径上**。

    `....` 是「像但非法」的名字；`tick_data` 是**上游存在、本注册表刻意不收**
    的（见 `datasets` 模块 docstring：声明是分区、盘面是平铺，按声明取会静默
    查空）。两者都必须 404，不能因为「上游有」就漏出去。

    注：`..` / `%2e%2e` 这类点段在 HTTP 客户端与 ASGI 层就被规范化掉了，
    根本到不了 handler；覆盖它们的是闸门的形状白名单（见
    `test_gate_rejects_near_miss_shapes`）。
    """
    got = raw.get(_url(f"/data/datasets/{name}/blob"))
    assert got.status_code == 404, f"{name} → {got.status_code}"
    assert got.json()["detail"] == "dataset_not_found"


def test_gate_rejects_near_miss_shapes(client: TestClient) -> None:
    """闸门那一层：形状白名单是**逐段写死**的，`[a-z0-9_]+` 之外的名字一律 403。

    这一层比 handler 更早生效，覆盖的是「注册表将来加了什么」管不到的那部分
    ——比如 `/data/datasets/../../x/blob` 这种连段数都不对的路径。
    """
    for path in (
        "/data/datasets/x/partitions/2026-09-22",  # 少了 file 一段
        "/data/datasets/x/blob/extra",  # blob 后面多一段
        "/data/x/changes/extra",  # changes 后面多一段 ← fullmatch 而非 match 的那一条
        "/data/x/partitions",  # 不是 datasets/{name}/partitions 的形状
        "/data/X_UPPER/changes",  # 字符类只有 [a-z0-9_]：注册表里的名字都是小写
    ):
        got = client.get(_url(path))
        assert got.status_code == 403, f"{path} → {got.status_code}"


def test_invalid_partition_is_400_but_unknown_partition_is_404(
    client: TestClient,
) -> None:
    """两种错分开是刻意的：格式错是**规则问题**（400，规则公开、无状态），
    分区不存在是**数据问题**（404）。混在一起会让对接方去查错方向。"""
    bad_format = client.get(
        _url(f"/data/datasets/{PARTITION_DS.name}/partitions/2026-13-45/file")
    )
    assert bad_format.status_code == 400
    assert bad_format.json()["detail"] == "invalid_partition"

    missing = client.get(
        _url(f"/data/datasets/{PARTITION_DS.name}/partitions/2019-01-02/file")
    )
    assert missing.status_code == 404
    assert missing.json()["detail"] == "partition_not_found"


def test_unmounted_dataset_is_503_not_404(client: TestClient) -> None:
    """「没有这个数据集」与「有这个数据集但这台机器上没有数据」是两件事。
    混在一起会让运维去查错方向。"""
    unmounted = next(d for d in registry.PARTITION_DATASETS if d.name != PARTITION_DS.name)
    got = client.get(
        _url(f"/data/datasets/{unmounted.name}/partitions/2026-09-22/file")
    )
    assert got.status_code == 503
    assert got.json()["detail"] == "dataset_unavailable"


@pytest.mark.parametrize("evil", ["....", "20260922", "2026-09-22 ", "2026-9-2"])
def test_traversal_partition_is_rejected_by_the_handler(raw: TestClient, evil: str) -> None:
    """单段但非法的分区名 → 400，**在碰到磁盘之前**。

    带斜杠的写法（`../../etc/passwd`）连路由都匹配不上，点段写法
    （`..` / `%2e%2e`）在客户端就被规范化掉了——它们到不了 handler，
    覆盖它们的是闸门。这里只取**能走到 handler 里**的那些写法。
    """
    got = raw.get(_url(f"/data/datasets/{PARTITION_DS.name}/partitions/{evil}/file"))
    assert got.status_code == 400, f"{evil!r} → {got.status_code}"
    assert got.json()["detail"] == "invalid_partition"


# ---------------------------------------------------------------------------
# 单文件数据集
# ---------------------------------------------------------------------------


def test_blob_is_served_and_revalidates(client: TestClient) -> None:
    url = _url(f"/data/datasets/{BLOB_DS.name}/blob")
    first = client.get(url)
    assert first.status_code == 200
    assert len(first.content) == 1004
    again = client.get(url, headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304


def test_missing_blob_file_is_503(client: TestClient, root: Path) -> None:
    """目录在、文件没了（正被重写）→ 503（服务端状态），不是 404。"""
    (root / BLOB_DS.rel_path).unlink()
    got = client.get(_url(f"/data/datasets/{BLOB_DS.name}/blob"))
    assert got.status_code == 503


# ---------------------------------------------------------------------------
# 行级增量
# ---------------------------------------------------------------------------


def test_changes_returns_rows_and_a_cursor(client: TestClient) -> None:
    body = client.get(_url(f"/data/{ROW_DS.name}/changes")).json()
    assert body["dataset"] == ROW_DS.name
    assert body["items"] == [
        {
            "enriched_at": "2026-09-23T10:00:00.123456Z",
            "huntly_page_id": 42,
            "sentiment_label": "neutral",
        }
    ]
    assert body["has_more"] is False
    assert body["next_cursor"].startswith("qmc1.")
    assert body["as_of"] == "2026-09-23T10:00:00.123456Z"
    assert body["full_sync_recommended_after"].endswith("Z")


def test_changes_binds_native_types_and_has_no_cast(client: TestClient, db: _FakeSession) -> None:
    """**这一条对应一个真实故障。**

    游标在线上是字符串，但第一版把它原样绑给 asyncpg，还指望
    `CAST(:t AS timestamptz)` 兜住——CAST 在 asyncpg 的类型编解码**之后**
    才生效，于是整个端点报：

        invalid input for query argument $1: '1970-01-01T00:00:00Z'
        (expected a datetime.date or datetime.datetime instance, got 'str')

    所以这里同时钉两件事：绑定值必须是原生类型；SQL 里不许再出现 CAST
    （留着它就会让人以为「反正数据库会转」）。
    """
    import re

    cursor = encode_cursor(ROW_DS.name, updated_at=T_NEW, key=("42",))
    client.get(_url(f"/data/{ROW_DS.name}/changes"), params={"cursor": cursor})

    page_sql, params = next(
        (sql, p) for sql, p in db.calls if "SELECT MAX" not in sql
    )
    assert "CAST(" not in page_sql.upper(), (
        "翻页 SQL 里还留着 CAST：参数类型该由 Python 值决定，"
        "cast 在 asyncpg 编解码之后才生效，兜不住"
    )
    assert isinstance(params["t"], datetime), f"t 是 {type(params['t'])}，不是 datetime"
    assert params["t"] == T_NEW, "微秒必须逐位无损（见 cursor 模块的模块 docstring）"
    assert isinstance(params["k0"], int), "bigint 兜底键必须以 int 绑定"
    assert params["k0"] == 42
    assert params["limit"] == 501, "多取一行用来判 has_more，不能真发给消费者"
    assert "enriched_at IS NOT NULL" in page_sql, (
        "游标列为 NULL 的行不可能被元组比较选中——这个前提要写在 SQL 里，"
        "而不是靠三值逻辑的副产品"
    )
    assert re.search(r"ORDER BY enriched_at, huntly_page_id", page_sql)


def test_has_more_when_the_page_is_full(client: TestClient, db: _FakeSession) -> None:
    """`LIMIT n+1` 多取的那一行不能出现在 items 里，但必须让 has_more=true。"""
    db.rows = [
        _Row(enriched_at=T_NEW, huntly_page_id=i, sentiment_label="x") for i in range(3)
    ]
    body = client.get(_url(f"/data/{ROW_DS.name}/changes"), params={"limit": 2}).json()
    assert len(body["items"]) == 2
    assert body["has_more"] is True
    assert [i["huntly_page_id"] for i in body["items"]] == [0, 1]


def test_cursor_round_trip_is_used_verbatim(client: TestClient, db: _FakeSession) -> None:
    body = client.get(_url(f"/data/{ROW_DS.name}/changes")).json()
    db.calls.clear()
    client.get(
        _url(f"/data/{ROW_DS.name}/changes"), params={"cursor": body["next_cursor"]}
    )
    page_sql, params = next((s, p) for s, p in db.calls if "SELECT MAX" not in s)
    assert params["t"] == T_NEW
    assert params["k0"] == 42
    assert "> (:t, :k0)" in page_sql.replace("\n", " "), (
        "边界必须是严格大于的**元组**比较——只比时间戳会在同一时刻多行时漏行"
    )


def test_cursor_from_another_dataset_is_rejected(client: TestClient) -> None:
    """静默接受等于让消费者拿 A 的水位读 B：少了几天数据，没有任何报错。"""
    foreign = encode_cursor(RUNS_DS.name, updated_at=T_NEW, key=("run-1",))
    got = client.get(_url(f"/data/{ROW_DS.name}/changes"), params={"cursor": foreign})
    assert got.status_code == 400
    assert got.json()["detail"] == "invalid_cursor"


@pytest.mark.parametrize(
    "bad",
    [
        "qmc1.zzz",  # 前缀对、载荷坏
        "qmx1.AAAA.BBBB",  # **另一套前缀**（会话令牌）——不能当成游标认下来
        "not-a-cursor",
        "qmc1.",
        "qmc1." + "A" * 4096,  # 超长：不能变成一次无界的 base64 解码
    ],
)
def test_garbage_cursor_is_400_not_500(client: TestClient, bad: str) -> None:
    got = client.get(_url(f"/data/{ROW_DS.name}/changes"), params={"cursor": bad})
    assert got.status_code == 400, f"{bad[:32]!r} → {got.status_code}"
    assert got.json()["detail"] == "invalid_cursor"


def test_empty_cursor_is_treated_as_absent(client: TestClient) -> None:
    """`?cursor=` 与不带 cursor 同义（返回第一页），不是 400。

    这是有意的：查询串由拼装产生时经常留下一个空参数，把它报成「游标坏了」
    会让人去查游标格式，方向反了。
    """
    got = client.get(_url(f"/data/{ROW_DS.name}/changes"), params={"cursor": ""})
    assert got.status_code == 200
    assert len(got.json()["items"]) == 1


def test_empty_page_echoes_the_cursor_back(client: TestClient, db: _FakeSession) -> None:
    """没有新行时返回**传入的**游标，不是 null。

    返回 null 会让消费者要么丢掉进度（下轮全量重来），要么自己记住——
    两种都不该由对接方承担。
    """
    db.rows = []
    cursor = encode_cursor(ROW_DS.name, updated_at=T_NEW, key=("42",))
    body = client.get(
        _url(f"/data/{ROW_DS.name}/changes"), params={"cursor": cursor}
    ).json()
    assert body["items"] == []
    assert body["next_cursor"] == cursor
    assert body["has_more"] is False


def test_tenant_scoped_datasets_filter_by_principal(client: TestClient, db: _FakeSession) -> None:
    """对外凭据属于某个人。不加过滤就是把**别人的**运行记录发给它。"""
    db.rows = []  # 只关心 SQL 与绑定参数，不要走「本页最后一行」那段
    client.get(_url(f"/data/{RUNS_DS.name}/changes"))
    page_sql, params = next((s, p) for s, p in db.calls if "SELECT MAX" not in s)
    assert "tenant_id = :tenant" in page_sql and "user_id = :user_id" in page_sql
    assert params["tenant"] == PRINCIPAL.tenant_id
    assert params["user_id"] == PRINCIPAL.user_id


def test_unscoped_dataset_does_not_filter_by_principal(
    client: TestClient, db: _FakeSession
) -> None:
    """反面：`news_article_enrichment` 表里根本没有租户列，硬加过滤会把它查空
    （而且不会报错，只是永远 0 行）。"""
    client.get(_url(f"/data/{ROW_DS.name}/changes"))
    page_sql, params = next((s, p) for s, p in db.calls if "SELECT MAX" not in s)
    assert "tenant_id" not in page_sql
    assert "tenant" not in params


def test_sensitive_looking_columns_are_dropped(
    client: TestClient, db: _FakeSession, caplog: pytest.LogCaptureFixture
) -> None:
    """`SELECT *` 是镜像语义要的，代价是**将来新增的列会自动外发**。
    这层网兜住最坏的那种：哪天有人往表里加 `api_token`，它不会被静默发出去。"""
    db.rows = [
        _Row(
            enriched_at=T_NEW,
            huntly_page_id=7,
            sentiment_label="x",
            api_token="sk-should-not-leak",
            user_password_hash="$2b$12$whatever",
        )
    ]
    with caplog.at_level("WARNING"):
        items = client.get(_url(f"/data/{ROW_DS.name}/changes")).json()["items"]
    assert set(items[0]) == {"enriched_at", "huntly_page_id", "sentiment_label"}
    assert "sk-should-not-leak" not in str(items)
    assert any("凭据特征" in r.message for r in caplog.records), (
        "剔除了就得留下痕迹——这是一行需要人来看的代码错误，不能静默"
    )


@pytest.mark.parametrize(
    ("value", "want"),
    [
        (b"\x00\x01\xff", "AAH/"),  # bytes → base64（jsonb 里可能存在）
        (1.5, 1.5),
    ],
)
def test_row_values_are_json_safe(client: TestClient, db: _FakeSession, value: Any, want: Any) -> None:
    db.rows = [_Row(enriched_at=T_NEW, huntly_page_id=1, blob_col=value)]
    items = client.get(_url(f"/data/{ROW_DS.name}/changes")).json()["items"]
    assert items[0]["blob_col"] == want


def test_non_finite_floats_become_null_not_invalid_json(
    client: TestClient, db: _FakeSession
) -> None:
    """`real` 列里可以存 NaN，而 JSON 根本没有 NaN 的表达。
    原样写出去就是一份**非法 JSON**，客户端解析直接失败。"""
    db.rows = [_Row(enriched_at=T_NEW, huntly_page_id=1, sentiment_score=float("nan"))]
    resp = client.get(_url(f"/data/{ROW_DS.name}/changes"))
    assert resp.status_code == 200
    assert resp.json()["items"][0]["sentiment_score"] is None
    assert "NaN" not in resp.text


def test_read_failure_is_503_not_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """读库失败是**服务端**的问题。报成 400/404 会把对接方引去查自己的请求。"""
    import backend.shared.database_manager_v2 as db_module

    class _Boom:
        async def __aenter__(self) -> Any:
            raise RuntimeError("connection refused")

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    monkeypatch.setattr(db_module, "get_session", lambda **_kw: _Boom())
    got = client.get(_url(f"/data/{ROW_DS.name}/changes"))
    assert got.status_code == 503
    assert got.json()["detail"] == "dataset_unavailable"


def test_index_survives_a_broken_database(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """`/datasets` 是节点接入时问的第一件事。库暂时读不到不该让它整个接不上——
    表型那几项如实报不可用，文件型的信息照给。"""
    import backend.shared.database_manager_v2 as db_module

    def _boom(**_kw: Any) -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr(db_module, "get_session", _boom)
    got = client.get(_url("/data/datasets"))
    assert got.status_code == 200
    by_name = {d["name"]: d for d in got.json()["datasets"]}
    assert by_name[PARTITION_DS.name]["available"] is True
    assert by_name[ROW_DS.name]["available"] is False
    assert by_name[ROW_DS.name]["freshness"] == "unavailable"


# ---------------------------------------------------------------------------
# 闸门与命名空间
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/data/datasets",
        f"/data/datasets/{PARTITION_DS.name}/partitions",
        f"/data/datasets/{PARTITION_DS.name}/partitions/2026-09-22/file",
        f"/data/datasets/{BLOB_DS.name}/blob",
        f"/data/{ROW_DS.name}/changes",
    ],
)
def test_data_plane_is_not_blocked_by_the_real_trading_gate(
    path: str, client: TestClient
) -> None:
    """数据面**不碰交易**，所以实盘开关关着也必须可用。

    注意断言的是「不是 403」而不是「200」：具体状态码由上面各条负责，
    这条只问闸门放不放行。放行表登记漏一条，这里就是 403。
    """
    got = client.get(_url(path))
    assert got.status_code != 403, (
        f"{path} 被实盘闸门挡了——数据面不因实盘关闭而不可用，"
        "请检查 live_trading_gate 的对外登记表"
    )


def test_data_endpoints_require_authentication() -> None:
    """反面：去掉依赖覆盖后必须 401。

    防止「依赖注入把鉴权短路了」这种情况悄悄发生——那会让整个数据面免认证。
    """
    app = FastAPI()
    app.include_router(router_module.router, prefix=gate.EXT_API)
    unauthenticated = TestClient(app)
    got = unauthenticated.get(_url("/data/datasets"))
    assert got.status_code == 401
