"""融合模型权重周期刷新（P2-4 / 设计《机构级模型融合》§7）回归测试。

锁五条纪律：
1. 防抖：最大权重变动 < 阈值不落盘（快照内容不变、无历史行）；
2. 写入面不对称：证据不可得/成员解析失败 → 跳过留原因，**绝不**降级等权
   重算写盘（那是静默改生产权重）；
3. manual/equal/未知策略与旧版缺成员记录 → 跳过（manual 是用户意图）；
4. 单模型失败隔离：一个模型炸掉不拖垮其余；
5. 落盘形态与创建期一致（v2 快照 + history event=refreshed），v1 平铺快照可读。

引擎（compute_fusion_weights）用真身跑合成 IC——契约是「快照 == 引擎输出」，
不是硬编码数字。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from backend.services.engine.inference import fusion_refresh as fr
from backend.services.engine.inference.fusion_orchestrator import FusionMember
from backend.services.engine.inference.fusion_quality import (
    FusionEvidence,
    MemberEvidence,
)
from backend.shared.fusion_weights import FusionWeightConfig, compute_fusion_weights

# 合成 IC：A 强 B 弱（都是 30 天、有波动，ICIR 可算）。
_A_ICS = [0.16, 0.04] * 15
_B_ICS = [0.03, 0.01] * 15


def _member(mid: str, ics: list[float]) -> MemberEvidence:
    return MemberEvidence(
        model_id=mid,
        horizon_days=5,
        market="CN",
        ic_by_date={f"2026-09-{d:02d}": v for d, v in enumerate(ics, start=1)},
        score_days=len(ics),
    )


def _evidence() -> FusionEvidence:
    return FusionEvidence(
        members=(_member("mdl_a", _A_ICS), _member("mdl_b", _B_ICS)), corr={}
    )


def _resolved_members() -> list[FusionMember]:
    return [
        FusionMember(
            model_id="mdl_a",
            display_name="A",
            model_type="native_tft",
            market="CN",
            horizon_days=5,
            model_dir="/tmp/fusion_test/a",
        ),
        FusionMember(
            model_id="mdl_b",
            display_name="B",
            model_type="native_tft",
            market="CN",
            horizon_days=5,
            model_dir="/tmp/fusion_test/b",
        ),
    ]


def _row(
    model_id: str,
    model_dir: Path,
    strategy: str = "icir_shrunk",
    source_ids: tuple[str, ...] = ("mdl_a", "mdl_b"),
) -> dict[str, Any]:
    return {
        "tenant_id": "default",
        "user_id": "10000001",
        "model_id": model_id,
        "status": "ready",
        "storage_path": str(model_dir),
        "metadata_json": {
            "is_ensemble": True,
            "weight_strategy": strategy,
            "source_model_ids": list(source_ids),
        },
    }


def _seed_snapshot(model_dir: Path, weights: dict[str, float]) -> None:
    (model_dir / "weight_snapshot.json").write_text(
        json.dumps(
            {
                "version": 2,
                "as_of": "2026-09-01",
                "strategy": "icir_shrunk",
                "weights": weights,
                "diagnostics": [],
                "updated_at": "2026-09-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (model_dir / "weight_history.jsonl").write_text(
        json.dumps(
            {
                "as_of": "2026-09-01",
                "strategy": "icir_shrunk",
                "weights": weights,
                "event": "created",
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _expected_weights(strategy: str = "icir_shrunk") -> dict[str, float]:
    fw = compute_fusion_weights(
        {"mdl_a": _A_ICS, "mdl_b": _B_ICS},
        {},
        FusionWeightConfig(strategy=strategy),
    )
    return {k: round(float(v), 6) for k, v in fw.weights.items()}


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """扫描/成员解析/证据三层接缝替身 + 可控模型目录。"""
    state: dict[str, Any] = {
        "rows": [],
        "resolved": _resolved_members(),
        "evidence": _evidence(),
        "gather_calls": 0,
        "resolve_fail_ids": set(),
    }

    async def fake_scan() -> list[dict[str, Any]]:
        return state["rows"]

    async def fake_resolve(model_ids, tenant_id, user_id):
        if model_ids and model_ids[0] in state["resolve_fail_ids"]:
            raise ValueError(f"成员不可用: {model_ids[0]}")
        return state["resolved"]

    async def fake_gather(specs, tenant_id, user_id):
        state["gather_calls"] += 1
        evidence = state["evidence"]
        if isinstance(evidence, Exception):
            raise evidence
        return evidence

    def make_dir(model_id: str) -> Path:
        d = tmp_path / model_id
        d.mkdir()
        return d

    monkeypatch.setattr(fr, "_scan_ensembles", fake_scan)
    monkeypatch.setattr(fr, "_resolve_members", fake_resolve)
    monkeypatch.setattr(fr, "_gather_evidence", fake_gather)
    state["make_dir"] = make_dir
    return state


# ---------------------------------------------------------------------------
# 主路径：超阈值落盘（v2 形态 + history event=refreshed）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_writes_snapshot_and_history_when_delta_exceeds_threshold(
    env: dict[str, Any],
) -> None:
    model_dir = env["make_dir"]("mdl_cn_ensemble_1")
    _seed_snapshot(model_dir, {"mdl_a": 1.0, "mdl_b": 0.0})  # 与引擎输出必差 > 0.02
    env["rows"].append(_row("mdl_cn_ensemble_1", model_dir))

    summary = await fr.refresh_fusion_weights()

    assert summary["scanned"] == 1
    assert [e["status"] for e in summary["updated"]] == ["updated"]
    assert summary["debounced"] == [] and summary["skipped"] == []
    snap = json.loads((model_dir / "weight_snapshot.json").read_text(encoding="utf-8"))
    assert snap["version"] == 2
    assert snap["strategy"] == "icir_shrunk"
    assert snap["weights"] == _expected_weights(), "快照必须等于引擎输出（同一实现）"
    assert len(snap["diagnostics"]) == 2
    lines = (
        (model_dir / "weight_history.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()
    )
    assert len(lines) == 2, "刷新必须追加一行历史（不覆盖 created 行）"
    last = json.loads(lines[-1])
    assert last["event"] == "refreshed"
    assert last["weights"] == snap["weights"]
    assert last["max_delta"] == summary["updated"][0]["max_delta"] > fr.MIN_WEIGHT_DELTA


@pytest.mark.asyncio
async def test_second_run_is_debounced_and_leaves_files_untouched(
    env: dict[str, Any],
) -> None:
    """防抖核心：首跑落盘后权重已 == 引擎输出，二跑必须零写盘。"""
    model_dir = env["make_dir"]("mdl_cn_ensemble_2")
    _seed_snapshot(model_dir, {"mdl_a": 1.0, "mdl_b": 0.0})
    env["rows"].append(_row("mdl_cn_ensemble_2", model_dir))

    await fr.refresh_fusion_weights()
    snap_before = (model_dir / "weight_snapshot.json").read_text(encoding="utf-8")
    history_before = (model_dir / "weight_history.jsonl").read_text(encoding="utf-8")

    second = await fr.refresh_fusion_weights()

    assert second["updated"] == []
    assert [e["status"] for e in second["debounced"]] == ["debounced"]
    assert (model_dir / "weight_snapshot.json").read_text(
        encoding="utf-8"
    ) == snap_before
    assert (model_dir / "weight_history.jsonl").read_text(
        encoding="utf-8"
    ) == history_before


@pytest.mark.asyncio
async def test_missing_snapshot_is_treated_as_full_change(
    env: dict[str, Any],
) -> None:
    model_dir = env["make_dir"]("mdl_cn_ensemble_3")  # 无快照文件
    env["rows"].append(_row("mdl_cn_ensemble_3", model_dir))

    summary = await fr.refresh_fusion_weights()

    assert [e["status"] for e in summary["updated"]] == ["updated"]
    assert summary["updated"][0]["max_delta"] is None  # 无穷 → None（不可比较）
    assert (model_dir / "weight_snapshot.json").exists()


@pytest.mark.asyncio
async def test_dry_run_reports_without_writing(env: dict[str, Any]) -> None:
    model_dir = env["make_dir"]("mdl_cn_ensemble_4")
    _seed_snapshot(model_dir, {"mdl_a": 1.0, "mdl_b": 0.0})
    env["rows"].append(_row("mdl_cn_ensemble_4", model_dir))
    snap_before = (model_dir / "weight_snapshot.json").read_text(encoding="utf-8")

    summary = await fr.refresh_fusion_weights(dry_run=True)

    assert [e["status"] for e in summary["updated"]] == ["would_update"]
    assert (model_dir / "weight_snapshot.json").read_text(
        encoding="utf-8"
    ) == snap_before
    history = (model_dir / "weight_history.jsonl").read_text(encoding="utf-8")
    assert history.count("\n") == 1, "dry_run 不得追加历史行"


@pytest.mark.asyncio
async def test_recent_ic_alias_is_refreshable_and_snapshots_canonical_strategy(
    env: dict[str, Any],
) -> None:
    model_dir = env["make_dir"]("mdl_cn_ensemble_5")
    _seed_snapshot(model_dir, {"mdl_a": 1.0, "mdl_b": 0.0})
    env["rows"].append(_row("mdl_cn_ensemble_5", model_dir, strategy="recent_ic"))

    summary = await fr.refresh_fusion_weights()

    assert [e["status"] for e in summary["updated"]] == ["updated"]
    snap = json.loads((model_dir / "weight_snapshot.json").read_text(encoding="utf-8"))
    assert snap["strategy"] == "icir_shrunk", "别名归一到引擎口径后再落盘"


# ---------------------------------------------------------------------------
# 跳过语义：manual/equal/未知策略/旧记录/目录缺失
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_equal_and_unknown_strategy_skip_before_any_io(
    env: dict[str, Any],
) -> None:
    rows = [
        _row("mdl_manual", env["make_dir"]("mdl_manual"), strategy="manual"),
        _row("mdl_equal", env["make_dir"]("mdl_equal"), strategy="equal"),
        _row(
            "mdl_icir_legacy_init",
            env["make_dir"]("mdl_icir_legacy_init"),
            strategy="icir",
        ),
    ]
    env["rows"].extend(rows)

    summary = await fr.refresh_fusion_weights()

    assert summary["updated"] == [] and summary["debounced"] == []
    statuses = {e["model_id"]: e["status"] for e in summary["skipped"]}
    assert statuses == {
        "mdl_manual": "skipped_manual",
        "mdl_equal": "skipped_equal",
        "mdl_icir_legacy_init": "skipped_strategy",
    }
    assert env["gather_calls"] == 0, "跳过必须发生在任何证据/引擎调用之前"


@pytest.mark.asyncio
async def test_legacy_row_without_source_ids_is_skipped(env: dict[str, Any]) -> None:
    row = _row("mdl_legacy", env["make_dir"]("mdl_legacy"))
    row["metadata_json"]["source_model_ids"] = []
    env["rows"].append(row)

    summary = await fr.refresh_fusion_weights()

    assert [e["status"] for e in summary["skipped"]] == ["skipped_legacy"]
    assert env["gather_calls"] == 0


@pytest.mark.asyncio
async def test_missing_strategy_is_reported_as_missing_not_equal(
    env: dict[str, Any],
) -> None:
    """元数据缺 weight_strategy（实测 stacking 模型即此形态）→ 如实报缺，
    不默认成 equal 再按 equal 报原因。"""
    row = _row("mdl_no_strategy", env["make_dir"]("mdl_no_strategy"))
    row["metadata_json"]["weight_strategy"] = None
    env["rows"].append(row)

    summary = await fr.refresh_fusion_weights()

    entry = summary["skipped"][0]
    assert entry["status"] == "skipped_no_strategy"
    assert "weight_strategy" in entry["reason"]


@pytest.mark.asyncio
async def test_missing_model_dir_is_skipped(
    env: dict[str, Any], tmp_path: Path
) -> None:
    env["rows"].append(_row("mdl_gone", tmp_path / "nonexistent"))

    summary = await fr.refresh_fusion_weights()

    assert [e["status"] for e in summary["skipped"]] == ["skipped_dir"]


# ---------------------------------------------------------------------------
# 写入面不对称：证据/成员不可得 → 跳过留原因，绝不降级写盘
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evidence_failure_skips_and_never_degrades_to_equal(
    env: dict[str, Any],
) -> None:
    """核心纪律：拉不到证据宁可不动（下 tick 重试），绝不等权重算写盘。"""
    model_dir = env["make_dir"]("mdl_cn_ensemble_6")
    _seed_snapshot(model_dir, {"mdl_a": 0.7, "mdl_b": 0.3})
    env["rows"].append(_row("mdl_cn_ensemble_6", model_dir))
    env["evidence"] = RuntimeError("pred parquet 缺失")
    snap_before = (model_dir / "weight_snapshot.json").read_text(encoding="utf-8")
    history_before = (model_dir / "weight_history.jsonl").read_text(encoding="utf-8")

    summary = await fr.refresh_fusion_weights()

    assert summary["updated"] == [] and summary["errors"] == []
    entry = summary["skipped"][0]
    assert entry["status"] == "skipped_evidence"
    assert "pred parquet 缺失" in entry["reason"]
    assert (model_dir / "weight_snapshot.json").read_text(
        encoding="utf-8"
    ) == snap_before
    assert (model_dir / "weight_history.jsonl").read_text(
        encoding="utf-8"
    ) == history_before


@pytest.mark.asyncio
async def test_member_resolution_failure_is_isolated_to_that_model(
    env: dict[str, Any],
) -> None:
    """一个模型成员解析失败（ValueError）不拖垮同批其余模型。"""
    bad_dir = env["make_dir"]("mdl_bad")
    good_dir = env["make_dir"]("mdl_good")
    _seed_snapshot(good_dir, {"mdl_a": 1.0, "mdl_b": 0.0})
    env["rows"].append(_row("mdl_bad", bad_dir, source_ids=("mdl_missing", "mdl_b")))
    env["rows"].append(_row("mdl_good", good_dir))
    env["resolve_fail_ids"] = {"mdl_missing"}

    summary = await fr.refresh_fusion_weights()

    assert [e["model_id"] for e in summary["updated"]] == ["mdl_good"]
    skipped = {e["model_id"]: e["status"] for e in summary["skipped"]}
    assert skipped == {"mdl_bad": "skipped_members"}


@pytest.mark.asyncio
async def test_unexpected_model_error_lands_in_errors_and_others_continue(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    err_dir = env["make_dir"]("mdl_err")
    ok_dir = env["make_dir"]("mdl_ok")
    _seed_snapshot(ok_dir, {"mdl_a": 1.0, "mdl_b": 0.0})
    env["rows"].append(_row("mdl_err", err_dir))
    env["rows"].append(_row("mdl_ok", ok_dir))

    real_write = fr._write_snapshot

    def flaky_write(model_dir: Path, **kw):
        if model_dir.name == "mdl_err":
            raise OSError("disk full")
        return real_write(model_dir, **kw)

    monkeypatch.setattr(fr, "_write_snapshot", flaky_write)

    summary = await fr.refresh_fusion_weights()

    assert [e["model_id"] for e in summary["updated"]] == ["mdl_ok"]
    assert summary["errors"] == [{"model_id": "mdl_err", "error": "disk full"}]


# ---------------------------------------------------------------------------
# 纯函数：v1 快照兼容与最大变动口径
# ---------------------------------------------------------------------------


def test_read_snapshot_weights_accepts_v1_flat_and_v2_nested(tmp_path: Path) -> None:
    v2 = tmp_path / "v2.json"
    v2.write_text(json.dumps({"weights": {"a": 0.6, "b": 0.4}}), encoding="utf-8")
    assert fr._read_snapshot_weights(v2) == {"a": 0.6, "b": 0.4}

    v1 = tmp_path / "v1.json"
    v1.write_text(json.dumps({"a": 0.6, "b": 0.4}), encoding="utf-8")
    assert fr._read_snapshot_weights(v1) == {"a": 0.6, "b": 0.4}

    missing = tmp_path / "none.json"
    assert fr._read_snapshot_weights(missing) == {}


def test_max_abs_delta_counts_key_only_on_one_side_as_full_move() -> None:
    assert fr._max_abs_delta({}, {"a": 0.5}) == float("inf")
    assert fr._max_abs_delta({"a": 0.5}, {"a": 0.5}) == 0.0
    assert fr._max_abs_delta({"a": 0.5, "b": 0.5}, {"a": 0.5}) == 0.5
    assert fr._max_abs_delta({"a": 0.5}, {"a": 0.5, "b": 0.5}) == 0.5
