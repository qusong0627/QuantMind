"""因子研究「扫描」接口 —— 拼装、只读保证、以及报错指路。

上游：扫描本身的差异口径由 `test_factor_research_scan.py` 锁死（新增/消失/去重/
阈值），本文件只锁接口层三件事：

1. **拼装**：把「盘上扫到的」与「快照目录记的」合起来，新增/消失各自带上来源库；
   消失项在盘上已经找不到了，来源库只能从目录条目反查（`wind_source` 前缀）；
2. **只读**：扫描不得写 `factors.json`、不得留下任何重算痕迹 —— 重算一次私人库
   是 5~15 分钟 + 重写 5.5GB 宽表，值不值得付要用户看完差异自己定；
3. **报错指路要指对**：数据目录缺失（`6_ml_datasets` 没挂上/没生成）与快照缺失
   是两回事。前者让人去点「一键计算」会白等一次几分钟的重建，且真正的原因
   （数据没挂上）一个字都没提 —— 这正是本次把 `SnapshotMissing` 单独成类型的原因。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.engine.factor_research import discovery, router as fr_router  # noqa: E402
from backend.services.engine.factor_research import service, store  # noqa: E402

PREFIX = discovery.SOURCE_PREFIX
SNAPSHOT = "factor_research_private"


def _write_ds(root: Path, name: str, cols: list[str], day: str = "20240102") -> None:
    d = root / "6_ml_datasets" / name / f"dt={day}"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {"symbol": ["000001.SZ", "600000.SH"], **{c: [1.0, 2.0] for c in cols}}
        ),
        d / "data.parquet",
    )


def _factor(code: str, *, source_lib: str = "alpha_library") -> dict:
    """快照目录条目（只保留本测试关心的字段）。"""
    return {
        "code": code,
        "name_cn": code,
        "display_name": code,
        "l1": discovery.lib_label(source_lib),
        "l2": "",
        "wind_source": f"{PREFIX}{source_lib}",
    }


@pytest.fixture()
def qroot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """真造一个 quantdb 临时目录，并让 `resolve_quantdb_dir()` 指过去。

    不 monkeypatch `store.factors_meta`：走真读路径才测得到「只读」这条
    （假函数天生不写盘，测了等于没测）。`QM_QUANTDB_DATA_DIR` 要求目录非空，
    故先把数据集写进去。
    """
    root = tmp_path / "quantdb"
    _write_ds(root, "alpha_library", ["a1", "a2", "a3", "a4", "new1"])
    snap = root / SNAPSHOT
    snap.mkdir(parents=True)
    (snap / "factors.json").write_text(
        json.dumps(
            {
                "factors": [
                    _factor("a1"),
                    _factor("a2"),
                    _factor("a3"),
                    _factor("a4"),
                    _factor("gone1", source_lib="l2_factors"),
                    # 经典复刻因子：wind_source 不是 6_ml_datasets 来源 → 取不到库名
                    {**_factor("classic_x"), "wind_source": ""},
                ],
                "l1_order": [],
                "l2_order": {},
                "meta": {"built_at": "2026-09-19T11:22:33", "source": "auto"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))
    return root


@pytest.fixture()
def client() -> TestClient:
    """只挂本 router 的最小应用（与真部署的路由对象同一个）。"""
    app = FastAPI()
    app.include_router(fr_router.router)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# 拼装：盘上扫描 × 快照目录
# ---------------------------------------------------------------------------
def test_new_factor_carries_its_source_library(qroot: Path):
    """新挖到、写进 quantdb 但快照里没有的因子 → 出现在 new，带库名与中文标签。"""
    out = service.scan_sources("private")

    assert [x["code"] for x in out["new"]] == ["new1"]
    assert out["new"][0]["library"] == "alpha_library"
    assert out["new"][0]["library_label"] == "Alpha 因子库"
    assert out["new_by_library"] == {"alpha_library": 1}


def test_missing_factor_finds_its_library_through_wind_source(qroot: Path):
    """消失项要标出它当初来自哪个库 —— 盘上已无它，只能反查 wind_source 前缀。

    这条同时锁「构建侧拼前缀」与「扫描侧解前缀」两个字符串必须一致：改了一边，
    这里就红，而不是界面上悄悄少一列来源。
    """
    out = service.scan_sources("private")

    assert [x["code"] for x in out["missing"]] == ["classic_x", "gone1"]
    got = {x["code"]: (x["library"], x["library_label"]) for x in out["missing"]}
    assert got["gone1"] == ("l2_factors", "L2 因子")
    # 非 6_ml_datasets 来源（经典复刻）取不到库名：留空，不猜
    assert got["classic_x"] == ("", "")


def test_counts_and_snapshot_meta_are_reported(qroot: Path):
    """两侧计数与快照元信息原样带出 —— 前端据此显示「已是最新」/快照建于何时。"""
    out = service.scan_sources("private")

    assert out["unchanged_count"] == 4
    assert out["discovered_count"] == 5  # a1..a4 + new1
    assert out["catalog_count"] == 6  # a1..a4 + gone1 + classic_x
    assert out["dataset"] == "private"
    assert out["snapshot_at"] == "2026-09-19T11:22:33"
    assert out["snapshot_source"] == "auto"


def test_scan_is_read_only(qroot: Path):
    """扫描不得写目录、不得留下任何重算痕迹（这是个 GET）。

    重算一次私人库要 5~15 分钟并重写 5.5GB 宽表；「先看看有什么新的」之所以
    值得单独做一个入口，前提就是它**不付**这个代价。若哪天有人顺手在这里
    触发一次重建，功能还在、只是每次都白等几分钟 —— 没有任何一层会报错。
    """
    snap = qroot / SNAPSHOT
    before = (snap / "factors.json").read_bytes()
    before_mtime = (snap / "factors.json").stat().st_mtime_ns

    service.scan_sources("private")

    assert (snap / "factors.json").read_bytes() == before, "扫描改写了因子目录"
    assert (snap / "factors.json").stat().st_mtime_ns == before_mtime
    left = sorted(p.name for p in snap.iterdir())
    assert left == ["factors.json"], f"扫描在产物目录留下了东西：{left}"


def test_scan_source_of_roundtrip():
    """目录前缀的拼/解必须成对：`source_of(SOURCE_PREFIX + lib) == lib`。"""
    assert discovery.source_of(f"{PREFIX}alpha360") == "alpha360"
    assert discovery.source_of("") == ""
    assert discovery.source_of("Wind 聚宽") == ""


# ---------------------------------------------------------------------------
# 拒绝：classic 没有「盘上有什么」可对
# ---------------------------------------------------------------------------
def test_service_rejects_classic(qroot: Path):
    """classic 的目录来自静态 catalog.py，扫它只能给出一份看起来合理的空差异。"""
    with pytest.raises(ValueError, match="private"):
        service.scan_sources("classic")


def test_route_rejects_classic_with_400(client: TestClient, qroot: Path):
    got = client.get("/api/v1/factor-research/scan", params={"dataset": "classic"})

    assert got.status_code == 400
    assert "private" in got.json()["detail"]


def test_route_returns_diff_for_private(client: TestClient, qroot: Path):
    got = client.get("/api/v1/factor-research/scan", params={"dataset": "private"})

    assert got.status_code == 200, got.text
    body = got.json()
    assert [x["code"] for x in body["new"]] == ["new1"]
    assert body["snapshot_source"] == "auto"


def test_dataset_is_validated(client: TestClient, qroot: Path):
    """非法取值由 Literal 收口在路由层（422），不会流到文件系统路径上。"""
    got = client.get("/api/v1/factor-research/scan", params={"dataset": "../../etc"})

    assert got.status_code == 422


# ---------------------------------------------------------------------------
# 报错指路：数据目录缺失 ≠ 快照缺失
# ---------------------------------------------------------------------------
def test_snapshot_missing_is_503_pointing_at_one_click_build(
    client: TestClient, qroot: Path
):
    """快照没建过 → 503 且提示去点「一键计算」（这条路点下去确实有用）。"""
    (qroot / SNAPSHOT / "factors.json").unlink()

    got = client.get("/api/v1/factor-research/scan", params={"dataset": "private"})

    assert got.status_code == 503
    assert "一键计算" in got.json()["detail"]


def test_missing_data_dir_is_not_reported_as_snapshot_gap(
    client: TestClient, qroot: Path
):
    """数据目录缺失 → 必须指向「数据」，不得翻译成「请点一键计算」。

    回归背景：路由旧守卫按文案里的「缺失」二字判，而 `6_ml_datasets 目录缺失`
    也含这两个字，于是被一并翻成「因子快照不完整，请点一键计算」—— 用户照着点，
    白等一次 5~15 分钟的重建，而真正的原因（数据盘没挂上）一个字都没提。
    """
    import shutil

    shutil.rmtree(qroot / "6_ml_datasets")

    got = client.get("/api/v1/factor-research/scan", params={"dataset": "private"})

    assert got.status_code == 503, got.text
    detail = got.json()["detail"]
    assert "6_ml_datasets" in detail, f"报错没点出缺的是哪个目录：{detail}"
    assert "一键计算" not in detail, "指错路了：数据缺失被说成快照缺失"


def test_snapshot_missing_is_a_filenotfound_error():
    """兼容既有调用方：`SnapshotMissing` 仍是 `FileNotFoundError` 子类。"""
    assert issubclass(store.SnapshotMissing, FileNotFoundError)
