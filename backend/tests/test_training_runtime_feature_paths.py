"""特征快照目录解析的可移植性回归（容器 / 便携包 / 本机）。

背景：推理预检与推理脚本历史上把特征目录硬编码成 /app/db/feature_snapshots。
便携包（免 Docker，解压目录运行）里模型 metadata 常写相对路径 "db/feature_snapshots"，
旧实现把它当 /app/... 解析（Windows 上甚至是盘符相对的 C:\\app\\...），parquet 源模型
预检永远报「parquet 文件不存在」，推理被阻断。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.services.engine.inference import script_runner as sr
from backend.shared import training_runtime as tr


@pytest.fixture
def fake_root(tmp_path, monkeypatch):
    """把「仓库根」指向临时目录，避免依赖真实目录结构。"""
    root = tmp_path / "pack"
    (root / "db" / "feature_snapshots").mkdir(parents=True)
    monkeypatch.setattr(tr, "repo_root_dir", lambda: root)
    monkeypatch.setattr(sr, "repo_root_dir", lambda: root)
    monkeypatch.setattr(sr, "rebase_container_path", tr.rebase_container_path)
    monkeypatch.delenv("MODEL_TRAINING_DATA_DIR", raising=False)
    return root


class TestFeatureSnapshotDir:
    def test_defaults_to_repo_root(self, fake_root):
        assert tr.feature_snapshot_dir() == fake_root / "db" / "feature_snapshots"

    def test_env_override_wins(self, fake_root, monkeypatch, tmp_path):
        override = tmp_path / "custom_features"
        monkeypatch.setenv("MODEL_TRAINING_DATA_DIR", str(override))
        assert tr.feature_snapshot_dir() == override

    def test_relative_metadata_dir_resolves_against_repo_root(self, fake_root):
        assert (
            tr.resolve_feature_snapshot_dir("db/feature_snapshots")
            == fake_root / "db" / "feature_snapshots"
        )

    def test_empty_raw_falls_back_to_default(self, fake_root):
        assert (
            tr.resolve_feature_snapshot_dir("")
            == fake_root / "db" / "feature_snapshots"
        )

    def test_container_absolute_path_rebases_to_repo_root(self, fake_root):
        # 便携包内 /app/... 不存在 → 按仓库根重定位（rebase 只看前缀映射）
        assert tr.rebase_container_path("/app/db/feature_snapshots") == (
            fake_root / "db" / "feature_snapshots"
        )
        assert tr.rebase_container_path("/data/db/other_probe") is None
        assert tr.rebase_container_path("/opt/whatever") is None

    def test_unknown_absolute_path_returned_as_is(self, fake_root):
        assert tr.resolve_feature_snapshot_dir("/nope/features") == Path(
            "/nope/features"
        )

    def test_missing_relative_path_stays_under_repo_root(self, fake_root):
        # 到处都不存在时仍返回仓库根下的绝对路径，报错信息才不会误导成 /app
        assert (
            tr.resolve_feature_snapshot_dir("some/missing")
            == fake_root / "some" / "missing"
        )


class TestProviderUriNormalization:
    def test_relative_uri_resolves_against_repo_root(self, fake_root):
        target = fake_root / "db" / "qlib_data_probe"
        target.mkdir(parents=True)
        assert sr.InferenceScriptRunner._normalize_provider_uri(
            "db/qlib_data_probe"
        ) == str(target)

    def test_container_absolute_uri_rebases_when_missing(self, fake_root):
        target = fake_root / "db" / "qlib_data_probe"
        target.mkdir(parents=True)
        assert sr.InferenceScriptRunner._normalize_provider_uri(
            "/app/db/qlib_data_probe"
        ) == str(target)

    def test_unresolvable_relative_uri_stays_under_repo_root(self, fake_root):
        assert sr.InferenceScriptRunner._normalize_provider_uri(
            "db/definitely_missing_probe"
        ) == str(fake_root / "db" / "definitely_missing_probe")


class TestParquetDataDirResolution:
    @staticmethod
    def _runner(tmp_path: Path, meta: dict) -> sr.InferenceScriptRunner:
        model_dir = tmp_path / "model_probe"
        model_dir.mkdir(parents=True, exist_ok=True)
        (model_dir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
        return sr.InferenceScriptRunner(primary_model_dir=str(model_dir))

    def test_active_data_source_uses_repo_root_for_relative_meta(
        self, tmp_path, fake_root
    ):
        runner = self._runner(
            tmp_path, {"data_source": "parquet", "data_dir": "db/feature_snapshots"}
        )
        assert runner._resolve_primary_active_data_source(
            {"data_source": "parquet", "data_dir": "db/feature_snapshots"}
        ) == str(fake_root / "db" / "feature_snapshots")

    def test_parquet_readiness_reads_relative_meta_dir(self, tmp_path, fake_root):
        pd = pytest.importorskip("pandas")
        pd.DataFrame({"trade_date": ["2026-09-08"], "symbol": ["600519"]}).to_parquet(
            fake_root / "db" / "feature_snapshots" / "model_features_2026.parquet"
        )

        runner = self._runner(
            tmp_path,
            {
                "data_source": "parquet",
                "data_dir": "db/feature_snapshots",
                "context": {"market": "CN"},
            },
        )

        result = runner._query_parquet_readiness("2026-09-08")

        assert result["ready"] is True, result
