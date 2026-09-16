from pathlib import Path

from backend.shared import qlib_paths


def _make_ready_provider(root: Path) -> None:
    (root / "calendars").mkdir(parents=True)
    (root / "instruments").mkdir()
    (root / "features" / "sh600000").mkdir(parents=True)
    (root / "calendars" / "day.txt").write_text("2024-01-02\n")
    (root / "instruments" / "all.txt").write_text(
        "SH600000\t2024-01-02\t2024-01-02\n"
    )


def test_is_qlib_provider_ready_requires_day_layout(tmp_path: Path):
    provider = tmp_path / "cn_data"
    provider.mkdir()

    assert not qlib_paths.is_qlib_provider_ready(provider)

    _make_ready_provider(provider)

    assert qlib_paths.is_qlib_provider_ready(provider)


def test_resolve_cn_skips_incomplete_quantdb_cache(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("QLIB_PROVIDER_URI", raising=False)
    monkeypatch.setattr(qlib_paths, "_PROJECT_ROOT", tmp_path)

    incomplete_cache = tmp_path / "data" / "quantdb" / ".qlib_cache" / "cn_data"
    incomplete_cache.mkdir(parents=True)
    fallback = tmp_path / "db" / "qlib_data"
    _make_ready_provider(fallback)

    assert qlib_paths.resolve_qlib_provider_uri("CN") == str(fallback)


def test_fallback_redirects_missing_canonical_to_ready_legacy(
    tmp_path: Path, monkeypatch
):
    """新部署常见故障：前端钉死 /data/qlib/cn_data 但数据在旧 db/qlib_data。"""
    monkeypatch.delenv("QLIB_PROVIDER_URI", raising=False)
    monkeypatch.setattr(qlib_paths, "_PROJECT_ROOT", tmp_path)

    legacy = tmp_path / "db" / "qlib_data"
    _make_ready_provider(legacy)

    missing_canonical = str(tmp_path / "data" / "qlib" / "cn_data")
    assert qlib_paths.fallback_to_ready_provider_uri(
        missing_canonical, market="CN"
    ) == str(legacy)


def test_fallback_keeps_ready_provider_and_unknown_paths(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("QLIB_PROVIDER_URI", raising=False)
    monkeypatch.setattr(qlib_paths, "_PROJECT_ROOT", tmp_path)

    ready = tmp_path / "data" / "qlib" / "cn_data"
    _make_ready_provider(ready)
    assert qlib_paths.fallback_to_ready_provider_uri(str(ready)) == str(ready)

    # 全部未就绪时原样返回，让调用方报出真实缺失路径
    empty_root = tmp_path / "nothing"
    empty_root.mkdir()
    monkeypatch.setattr(qlib_paths, "_PROJECT_ROOT", empty_root)
    missing = str(empty_root / "nowhere")
    result = qlib_paths.fallback_to_ready_provider_uri(missing)
    assert "nowhere" in result


def test_guess_market_from_provider_uri():
    assert qlib_paths.guess_market_from_provider_uri("/data/qlib/cn_data") == "CN"
    assert qlib_paths.guess_market_from_provider_uri("/data/qlib/hk_data") == "HK"
    assert qlib_paths.guess_market_from_provider_uri("/x/us_data/y") == "US"
    assert qlib_paths.guess_market_from_provider_uri("/data/qlib/bc_data") == "CRYPTO"
    assert qlib_paths.guess_market_from_provider_uri(None) == "CN"
