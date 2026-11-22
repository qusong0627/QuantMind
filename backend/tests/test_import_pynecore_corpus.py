"""pynecore-strategies 语料导入器：分类 / ID 稳定 / 落盘契约。

运行（容器）：python3 -m pytest backend/tests/test_import_pynecore_corpus.py -q
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import import_pynecore_corpus as imp  # noqa: E402

LONG_SRC = """@script.strategy('Long Only', overlay=True, initial_capital=1000,
                 default_qty_type=strategy.fixed, default_qty_value=1)
def main():
    pass
"""

SHORT_SRC = """@script.strategy('Short Side', overlay=True, currency=currency.USD,
                 initial_capital=50000, commission_value=0.2)
def main():
    strategy.entry('S', strategy.short)
"""

SEC_SRC = """@script.strategy('Multi TF', overlay=True)
def main():
    request.security(syminfo.tickerid, '1D', close)
"""

DYN_SEC_SRC = """@script.strategy('Dyn TF', overlay=True)
def main():
    request.security(syminfo.tickerid, dynamic_tf, close)
"""

LIB_SRC = """@script.strategy('With Lib', overlay=True)
import lib.publisher.utils.v1 as utils
def main():
    pass
"""

PINE = """//@version=6
// © Test Author
strategy('x', overlay=true)
if close > open
    strategy.entry('L', strategy.long)
"""


def _meta(sid: str, body: str) -> str:
    return (
        '"""\n@pyne\n\ncompiled\n"""\n'
        "from pynecore.lib import script, strategy, syminfo, close, currency\n"
        f"{body}"
    )


def _corpus(tmp: Path) -> Path:
    root = tmp / "corpus"
    (root / "scripts").mkdir(parents=True)
    (root / "assets/strategy/bit-exact").mkdir(parents=True)
    (root / "assets/strategy/verified").mkdir(parents=True)
    cases = [
        ("strategy_long_Aaaa1111.py", LONG_SRC, "bit-exact"),
        ("strategy_short_Bbbb2222.py", SHORT_SRC, "verified"),
        ("strategy_sec_Cccc3333.py", SEC_SRC, "bit-exact"),
        ("strategy_lib_Dddd4444.py", LIB_SRC, "bit-exact"),
        ("strategy_dyn_Eeee5555.py", DYN_SEC_SRC, "verified"),
    ]
    for name, body, tier in cases:
        (root / "scripts" / name).write_text(_meta(name, body), encoding="utf-8")
        sid = name.rsplit("_", 1)[1].removesuffix(".py")
        (root / "assets/strategy" / tier / f"{sid}.pine").write_text(
            PINE, encoding="utf-8"
        )
    return root


def test_discover_classifies(tmp_path):
    records = imp.discover(_corpus(tmp_path))
    by_sid = {r["source_id"]: r for r in records}
    assert len(records) == 5

    long = by_sid["Aaaa1111"]
    assert long["has_short"] is False
    assert long["import_status"] == "ready"
    assert long["market_hint"] == "cn"
    assert long["category"] == imp.CATEGORY_CN
    assert long["author"] == "Test Author"

    short = by_sid["Bbbb2222"]
    assert short["has_short"] is True
    assert short["market_hint"] == "us"
    assert short["category"] == imp.CATEGORY_US

    sec = by_sid["Cccc3333"]
    assert sec["has_security"] is True
    assert sec["import_status"] == "ready_security"
    assert sec["security_timeframes"] == ["1D"]
    assert "cn" in sec["security_markets"]
    assert sec["category"] == imp.CATEGORY_CN

    dyn = by_sid["Eeee5555"]
    assert dyn["import_status"] == "deferred_security"
    assert dyn["category"] == imp.CATEGORY_DEFERRED

    lib = by_sid["Dddd4444"]
    assert lib["has_lib"] is True
    assert lib["import_status"] == "ready"


def test_summarize_counts():
    records = [
        {"import_status": "ready", "has_short": False, "has_lib": False},
        {"import_status": "ready", "has_short": True, "has_lib": True},
        {"import_status": "deferred_security", "has_short": False, "has_lib": False},
        {"import_status": "ready_security", "has_short": False, "has_lib": False},
    ]
    assert imp.summarize(records) == {
        "total": 4,
        "cn": 2,
        "us": 1,
        "deferred": 1,
        "lib": 1,
        "security": 1,
    }


def test_assign_ids_reuses_existing_and_continues():
    records = [{"source_id": "Aaaa1111"}, {"source_id": "Bbbb2222"}]
    existing = [{"id": "9005", "source_kind": "imported", "source_id": "Aaaa1111"}]
    out = imp.assign_ids(records, existing)
    assert out[0]["id"] == "9005"
    assert out[1]["id"] == "9006"


def test_assign_ids_ignores_non_imported():
    out = imp.assign_ids(
        [{"source_id": "Xxxx1111"}], [{"id": "u001", "source_kind": "custom"}]
    )
    assert out[0]["id"] == "9001"


def test_write_records_contract(tmp_path, monkeypatch):
    library_root = tmp_path / "pine_library"
    monkeypatch.setenv("PINE_LIBRARY_DIR", str(library_root))
    records = imp.assign_ids(imp.discover(_corpus(tmp_path)), [])
    counts = imp.write_records(records, library_root=library_root)
    assert counts["added"] == 5

    item = library_root.parent / "pine_transpile" / "9001"
    assert (item / "candidate.py").is_file()
    meta = json.loads((item / "meta.json").read_text(encoding="utf-8"))
    assert meta["imported"] is True
    assert meta["source_repo"] == imp.SOURCE_REPO
    assert meta["problems"] == []
    assert meta["original_strategy_decorator"].startswith("@script.strategy(")

    index = json.loads((library_root / "custom/index.json").read_text(encoding="utf-8"))
    imported = [it for it in index["items"] if it["source_kind"] == "imported"]
    assert len(imported) == 5
    assert {it["category"] for it in imported} == {
        imp.CATEGORY_CN,
        imp.CATEGORY_US,
        imp.CATEGORY_DEFERRED,
    }
    # 非 custom ID（9xxx）会从 source/ 解析源码
    assert (library_root / "source/9001.pine").is_file()


def test_write_records_is_idempotent(tmp_path, monkeypatch):
    library_root = tmp_path / "pine_library"
    monkeypatch.setenv("PINE_LIBRARY_DIR", str(library_root))
    records = imp.assign_ids(imp.discover(_corpus(tmp_path)), [])
    imp.write_records(records, library_root=library_root)
    counts = imp.write_records(records, library_root=library_root)
    assert counts["skipped"] == 5
    index = json.loads((library_root / "custom/index.json").read_text(encoding="utf-8"))
    assert len(index["items"]) == 5


def test_unsupported_request_feature_is_deferred(tmp_path):
    corpus = _corpus(tmp_path)
    feat = """@script.strategy('Earnings', overlay=True)
def main():
    request.earnings(syminfo.tickerid, earnings.actual)
"""
    (corpus / "scripts/strategy_feat_Ffff6666.py").write_text(
        _meta("feat", feat), encoding="utf-8"
    )
    (corpus / "assets/strategy/bit-exact/Ffff6666.pine").write_text(
        PINE, encoding="utf-8"
    )
    rec = {r["source_id"]: r for r in imp.discover(corpus)}["Ffff6666"]
    assert rec["import_status"] == "deferred_feature"
    assert rec["feature_unresolved"] == ["request.earnings"]
    assert rec["category"] == imp.CATEGORY_DEFERRED


def test_deferred_manifest(tmp_path):
    library_root = tmp_path / "pine_library"
    library_root.mkdir()
    path = imp.write_deferred_manifest(imp.discover(_corpus(tmp_path)), library_root)
    assert path is not None and path.is_file()
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["total"] == 1
    assert doc["items"][0]["source_id"] == "Eeee5555"
    assert "dynamic_tf" in doc["items"][0]["unresolved"]


def test_copy_lib(tmp_path):
    corpus = _corpus(tmp_path)
    lib = corpus / "scripts/lib/Publisher"
    lib.mkdir(parents=True)
    (lib / "utils.py").write_text("VALUE = 1\n", encoding="utf-8")
    out = imp.copy_lib(corpus, tmp_path / "pine_library")
    assert out["copied"] is True
    assert (tmp_path / "pine_library/lib/Publisher/utils.py").is_file()


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    library_root = tmp_path / "pine_library"
    monkeypatch.setenv("PINE_LIBRARY_DIR", str(library_root))
    rc = imp.main(
        [
            "--corpus",
            str(_corpus(tmp_path)),
            "--library-root",
            str(library_root),
        ]
    )
    assert rc == 0
    assert not library_root.exists()
