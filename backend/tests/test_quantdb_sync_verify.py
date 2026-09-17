"""QuantDB 同步内容校验测试（2026-09-17 停更事故回归）。

事故：云侧 manifest schema 变更（sha256 整列消失，仅剩 etag=内容 MD5）→ 旧校验
``actual == expected("")`` 恒假 → 4 个全量重写数据集每轮把 2600 个分区全判"待重下"
→ 下载风暴（HTTP 410/超时）+ celery 硬超时被杀 → features_daily/l1/l2/valuation
连续多晚停更。本测试锁死校验优先级（sha256 → etag-MD5）与保守回退（都缺=不一致）。
"""

from __future__ import annotations

import hashlib

import pytest


def _write(tmp_path, payload: bytes = b"parquet-bytes" * 512):
    f = tmp_path / "data.parquet"
    f.write_bytes(payload)
    return f, hashlib.sha256(payload).hexdigest(), hashlib.md5(payload).hexdigest()


@pytest.mark.unit
def test_verify_content_sha256_priority(tmp_path):
    """sha256 存在时权威；etag 再对也不影响（sha 不一致=不一致）。"""
    from backend.scripts.quantdb_daily_sync import verify_content

    f, sha, md5 = _write(tmp_path)
    ok, actual = verify_content({"sha256": sha, "etag": "0" * 32}, f)
    assert ok and actual == sha
    ok, actual = verify_content({"sha256": "0" * 64, "etag": f'"{md5}"'}, f)
    assert not ok and actual == sha


@pytest.mark.unit
def test_verify_content_etag_md5_fallback(tmp_path):
    """sha256 缺失（云侧新 schema）→ etag(MD5) 回退；etag 带引号/大写均可。"""
    from backend.scripts.quantdb_daily_sync import verify_content

    f, sha, md5 = _write(tmp_path)
    ok, actual = verify_content({"etag": f'"{md5.upper()}"', "size": 123}, f)
    assert ok and actual == sha  # 登记仍用本地 sha256
    ok, _ = verify_content({"etag": "0" * 32}, f)
    assert not ok


@pytest.mark.unit
def test_verify_content_conservative_when_hash_missing(tmp_path):
    """两个哈希字段都不可用（缺失/空串/非 MD5 形态）→ 一律判不一致（保守重下）。"""
    from backend.scripts.quantdb_daily_sync import verify_content

    f, _sha, _md5 = _write(tmp_path)
    for obj in ({}, {"sha256": ""}, {"sha256": None, "etag": None}, {"etag": "etag-abc"}, {"etag": "a" * 31}):
        ok, _ = verify_content(obj, f)
        assert not ok, obj
