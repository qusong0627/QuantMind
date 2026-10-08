"""挖掘因子毕业器（quantcustom → quantdb 镜像）的不变量。

这里守的都是**静默损坏**——不报错、只让数据悄悄变样：

1. **镜像 ≠ 合并**。只允许写 ``6_ml_datasets/rd_mined`` 自己的目录；一旦碰到
   ``l1_factors`` / ``l2_factors``，就是把用户产出混进官方行情库，而 CN 训练
   会把它当行情读 —— 出事时没有任何一层会报错。用「污染哨兵」目录断言。
2. **幂等**。第二次跑必须零写入（`is_noop`）。判据是 (size, mtime_ns)：物化器
   新增因子会重写**全部**分区对齐列集，所以列漂移必然改时间戳，无需额外 schema 比对。
3. **默认不删**。源里没有的分区要留在目标里 —— 误删会让训练静默少一段历史
   （少一天数据不会报错，只会让回测结果悄悄变）。只有显式 `--prune` 才收敛。
4. **原子**。写 ``.tmp`` 再 ``os.replace``；跑完不留 ``.tmp`` 残骸（残留会让
   下次扫描把它当成未知文件）。
5. **单分区失败不中断整批**，但要记进 ``failed`` 让调用方拿到非零退出码 ——
   毕业一半而日志说「完成」是最坏的结果。

纯文件系统测试（tmp_path），不碰 DB、不碰真实数据根。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.scripts import promote_rd_mined as prm


# ---------------------------------------------------------------------------
# 夹具：造一个假的「源根 / 目标根」
# ---------------------------------------------------------------------------
def _write_partition(root: Path, dataset: str, dt: str, payload: bytes) -> Path:
    d = root / "6_ml_datasets" / dataset / f"dt={dt}"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "data.parquet"
    f.write_bytes(payload)
    return f


def _split(dataset_dir: Path) -> tuple[Path, str]:
    """把 ``.../6_ml_datasets/rd_mined`` 还原成 (数据根, 库名)，配合 _write_partition。"""
    return dataset_dir.parents[1], dataset_dir.name


@pytest.fixture()
def roots(tmp_path: Path) -> tuple[Path, Path]:
    """(源数据集目录, 目标数据集目录)，均为 6_ml_datasets/rd_mined。"""
    src_root = tmp_path / "quantcustom"
    dst_root = tmp_path / "quantdb"
    return prm.resolve_roots(src_root, dst_root)


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------
def test_scan_only_picks_dt_partitions(roots):
    """只认 dt=*/data.parquet；清单、别的目录一律不算分区。"""
    src, _ = roots
    prm.scan_partitions(src)  # 目录不存在 → 空
    assert prm.scan_partitions(src) == {}

    _write_partition(*_split(src), "20200102", b"a")
    (src / "_promote_manifest.json").write_text("{}", encoding="utf-8")
    (src / "not_a_partition").mkdir(parents=True, exist_ok=True)
    (src / "not_a_partition" / "data.parquet").write_bytes(b"x")

    found = prm.scan_partitions(src)
    assert set(found) == {"dt=20200102"}


# ---------------------------------------------------------------------------
# 计划：幂等 / 增量
# ---------------------------------------------------------------------------
def test_first_promote_copies_everything(roots):
    src, dst = roots
    _write_partition(*_split(src), "20200102", b"a")
    _write_partition(*_split(src), "20200103", b"bb")

    plan = prm.plan_sync(prm.scan_partitions(src), prm.scan_partitions(dst))

    assert plan.to_copy == ("dt=20200102", "dt=20200103")
    assert plan.unchanged == ()
    assert not plan.is_noop


def test_second_promote_is_a_noop(roots):
    """幂等：镜像一次后再计划，零拷贝零删除。"""
    src, dst = roots
    for dt, payload in (("20200102", b"a"), ("20200103", b"bb")):
        _write_partition(*_split(src), dt, payload)
    plan = prm.plan_sync(prm.scan_partitions(src), prm.scan_partitions(dst))
    prm.apply_sync(src, dst, plan)

    again = prm.plan_sync(prm.scan_partitions(src), prm.scan_partitions(dst))

    assert again.is_noop, f"重复毕业产生了写入：{again.summary()}"
    assert len(again.unchanged) == 2


def test_changed_mtime_triggers_recopy(roots):
    """内容变了（列集漂移会重写分区）→ 必须重拷，不能因为路径相同就跳过。"""
    src, dst = roots
    f = _write_partition(*_split(src), "20200102", b"a")
    prm.apply_sync(
        src, dst, prm.plan_sync(prm.scan_partitions(src), prm.scan_partitions(dst))
    )

    f.write_bytes(b"changed-content")
    os.utime(f, ns=(f.stat().st_atime_ns, f.stat().st_mtime_ns + 10**9))

    plan = prm.plan_sync(prm.scan_partitions(src), prm.scan_partitions(dst))
    assert plan.to_copy == ("dt=20200102",)


def test_changed_size_triggers_recopy(roots):
    """同样大小但内容不同、且时间戳恰好没变 —— 按 size 兜住。"""
    src, dst = roots
    f = _write_partition(*_split(src), "20200102", b"aaaa")
    st = f.stat()
    prm.apply_sync(
        src, dst, prm.plan_sync(prm.scan_partitions(src), prm.scan_partitions(dst))
    )

    f.write_bytes(b"bbbbbbbb")  # 长度不同
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))  # 时间戳还原

    plan = prm.plan_sync(prm.scan_partitions(src), prm.scan_partitions(dst))
    assert plan.to_copy == ("dt=20200102",)


def test_force_recopies_identical_partitions(roots):
    src, dst = roots
    _write_partition(*_split(src), "20200102", b"a")
    prm.apply_sync(
        src, dst, prm.plan_sync(prm.scan_partitions(src), prm.scan_partitions(dst))
    )

    forced = prm.plan_sync(
        prm.scan_partitions(src), prm.scan_partitions(dst), force=True
    )
    assert forced.to_copy == ("dt=20200102",)


# ---------------------------------------------------------------------------
# 删除语义：默认保守
# ---------------------------------------------------------------------------
def test_target_only_partitions_are_kept_by_default(roots):
    """源里没有的分区默认保留 —— 误删会让训练静默少一段历史。"""
    src, dst = roots
    _write_partition(*_split(src), "20200102", b"a")
    _write_partition(*_split(dst), "20200103", b"old")

    plan = prm.plan_sync(prm.scan_partitions(src), prm.scan_partitions(dst))

    assert plan.to_prune == ()
    prm.apply_sync(src, dst, plan)
    assert (dst / "dt=20200103" / "data.parquet").is_file(), "默认不该删目标独有分区"


def test_prune_removes_target_only_partitions(roots):
    src, dst = roots
    _write_partition(*_split(src), "20200102", b"a")
    _write_partition(*_split(dst), "20200103", b"old")

    plan = prm.plan_sync(prm.scan_partitions(src), prm.scan_partitions(dst), prune=True)

    assert plan.to_prune == ("dt=20200103",)
    prm.apply_sync(src, dst, plan)
    assert not (dst / "dt=20200103").exists()


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------
def test_copied_bytes_match_source(roots):
    src, dst = roots
    payload = b"\x00\x01parquet-ish\xff"
    _write_partition(*_split(src), "20200102", payload)

    prm.apply_sync(
        src, dst, prm.plan_sync(prm.scan_partitions(src), prm.scan_partitions(dst))
    )

    assert (dst / "dt=20200102" / "data.parquet").read_bytes() == payload


def test_no_tmp_files_left_behind(roots):
    """原子写留下的 .tmp 必须清干净，否则下次扫描会把它当未知文件。"""
    src, dst = roots
    _write_partition(*_split(src), "20200102", b"a")

    prm.apply_sync(
        src, dst, prm.plan_sync(prm.scan_partitions(src), prm.scan_partitions(dst))
    )

    assert list(dst.rglob("*.tmp")) == []


def test_dry_run_writes_nothing(roots):
    src, dst = roots
    _write_partition(*_split(src), "20200102", b"a")
    plan = prm.plan_sync(prm.scan_partitions(src), prm.scan_partitions(dst))

    result = prm.apply_sync(src, dst, plan, dry_run=True)

    assert result.copied == ["dt=20200102"]
    assert not dst.exists() or list(dst.rglob("data.parquet")) == []


# ---------------------------------------------------------------------------
# 边界：只动自己那一个库
# ---------------------------------------------------------------------------
def test_promote_never_touches_sibling_datasets(roots):
    """**镜像不是合并**：绝不允许写 l1_factors / l2_factors。

    CN 训练把 l1_factors 当行情源读；把它改了不会报错，只会让所有 CN 模型
    静默换了一套行情。这里用哨兵文件断言兄弟库字节不变。
    """
    src, dst = roots
    _write_partition(*_split(src), "20200102", b"a")
    sentinels = {}
    for sibling in ("l1_factors", "l2_factors"):
        for root in (src, dst):
            p = _write_partition(
                root, sibling, "20200102", f"{sibling}-sentinel".encode()
            )
            sentinels[p] = p.read_bytes()

    plan = prm.plan_sync(prm.scan_partitions(src), prm.scan_partitions(dst), prune=True)
    prm.apply_sync(src, dst, plan)

    for path, before in sentinels.items():
        assert path.exists(), f"兄弟库被删：{path}"
        assert path.read_bytes() == before, f"兄弟库被改写：{path}"
    # 且源/目标兄弟库都没被卷进拷贝计划
    assert all("l1_factors" not in n and "l2_factors" not in n for n in plan.to_copy)


def test_apply_reports_failures_without_aborting_the_batch(roots):
    """单个分区失败要记进 failed（调用方据此非零退出），不能整批中断。"""
    src, dst = roots
    _write_partition(*_split(src), "20200102", b"a")
    _write_partition(*_split(src), "20200103", b"b")

    # 源文件在计划之后消失 —— 模拟并发写盘/被清理
    plan = prm.plan_sync(prm.scan_partitions(src), prm.scan_partitions(dst))
    (src / "dt=20200102" / "data.parquet").unlink()

    result = prm.apply_sync(src, dst, plan)

    assert [f["partition"] for f in result.failed] == ["dt=20200102"]
    assert result.copied == ["dt=20200103"], "一个失败不该拖垮其余分区"


# ---------------------------------------------------------------------------
# 根目录解析
# ---------------------------------------------------------------------------
def test_resolve_roots_prefers_explicit_args(tmp_path):
    src, dst = prm.resolve_roots(tmp_path / "s", tmp_path / "d")

    assert src == tmp_path / "s" / "6_ml_datasets" / "rd_mined"
    assert dst == tmp_path / "d" / "6_ml_datasets" / "rd_mined"


def test_resolve_roots_falls_back_to_env(tmp_path, monkeypatch):
    monkeypatch.setenv(prm._SOURCE_ENV, str(tmp_path / "envsrc"))
    monkeypatch.setenv(prm._TARGET_ENV, str(tmp_path / "envdst"))

    src, dst = prm.resolve_roots()

    assert src == tmp_path / "envsrc" / "6_ml_datasets" / "rd_mined"
    assert dst == tmp_path / "envdst" / "6_ml_datasets" / "rd_mined"
