"""backend/shared/version.py 单元测试。

两条「安静」契约（2026-10-09 定口径）：
1. 版本明细：version.json（update.sh 部署形态）优先 → version.txt describe（遗留）→ dev；
2. 更新检查只服务**走过 update.sh 的部署**（version.json 在场）：开发机 bind mount
   恒最新，但遗留 version.txt 的 commit 冻结在写入时刻，拿它对比上游会把
   「本地其实最新」误报成落后几百提交 —— 宁可没有提示，不许假提示。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import backend.shared.version as vmod


def _isolate(monkeypatch, tmp_path: Path, *, txt: str | None, js: dict | None):
    """把版本文件与缓存全部指到 tmp_path，避免读到仓库/磁盘上的真文件。"""
    vtxt = tmp_path / "version.txt"
    if txt is not None:
        vtxt.write_text(txt, encoding="utf-8")
    monkeypatch.setattr(vmod, "_VERSION_TXT", vtxt)

    vjson = tmp_path / "version.json"
    if js is not None:
        import json

        vjson.write_text(json.dumps(js, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(vmod, "_VERSION_JSON", vjson)

    monkeypatch.setattr(vmod, "_CACHE_FILE", tmp_path / "version_check.json")


# ---------------------------------------------------------------------------
# 版本明细
# ---------------------------------------------------------------------------


def test_version_info_falls_back_to_dev(tmp_path: Path, monkeypatch):
    """两个版本文件都不在（纯源码开发环境）→ dev / 空 commit。"""
    _isolate(monkeypatch, tmp_path, txt=None, js=None)
    info = vmod.get_version_info()
    assert info["version"] == "dev"
    assert info["commit"] == ""


def test_version_info_parses_legacy_txt(tmp_path: Path, monkeypatch):
    """遗留 version.txt：describe 原文做 version，短 SHA 解析出来做 commit。"""
    _isolate(monkeypatch, tmp_path, txt="v1.9.0-beta-150-g3d32379f\n", js=None)
    info = vmod.get_version_info()
    assert info["version"] == "v1.9.0-beta-150-g3d32379f"
    assert info["commit"] == "3d32379f"


def test_version_info_prefers_json(tmp_path: Path, monkeypatch):
    """version.json（update.sh 部署）优先于遗留 txt，且 branch 取部署实际分支。"""
    _isolate(
        monkeypatch,
        tmp_path,
        txt="v1.9.0-beta-150-g3d32379f\n",
        js={"version": "v1.9.0-beta-700-gabcdef12", "commit": "abcdef12" * 5, "branch": "next"},
    )
    info = vmod.get_version_info()
    assert info["version"] == "v1.9.0-beta-700-gabcdef12"
    assert info["commit"] == "abcdef12" * 5
    assert info["branch"] == "next"


def test_version_info_json_branch_falls_back_to_upstream(tmp_path: Path, monkeypatch):
    """version.json 缺 branch 字段（旧 update.sh 写的）→ 回退上游对比分支。"""
    _isolate(monkeypatch, tmp_path, txt=None, js={"version": "v1", "commit": "abc1234"})
    info = vmod.get_version_info()
    assert info["branch"] == vmod._UPSTREAM_BRANCH


def test_default_upstream_branch_is_next():
    """默认对比分支必须是 next：main 只读、推送走 next，绑 master 会把正常推进误报成落后。"""
    import os

    assert os.getenv("QUANTMIND_UPSTREAM_BRANCH") is None, "环境变量在场时本断言不代表代码默认值"
    assert vmod._UPSTREAM_BRANCH == "next"


# ---------------------------------------------------------------------------
# 更新检查
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_updates_skips_without_version_json(tmp_path: Path, monkeypatch):
    """回归（本地误报落后）：只有遗留 version.txt 的开发机，不许发更新检查。

    这个用例守的是真实事故：本地 bind mount 恒最新，但 version.txt 停在 8 月，
    后台管理页常驻「落后 100+ 个提交」；点它给的指引（去服务器跑 update.sh）
    在本地是无意义的。判据 = version.json 是否在场，而不是 commit 能否解析。
    """
    _isolate(monkeypatch, tmp_path, txt="v1.9.0-beta-150-g3d32379f\n", js=None)
    assert await vmod.check_updates(force=True) is None


@pytest.mark.asyncio
async def test_check_updates_compares_against_next_when_deployed(tmp_path: Path, monkeypatch):
    """version.json 在场（update.sh 部署）→ 带上游 compare，落后数按分支 next 算。"""
    _isolate(
        monkeypatch,
        tmp_path,
        txt=None,
        js={"version": "v9", "commit": "abcdef1234567890", "branch": "next"},
    )
    fake = SimpleNamespace(
        calls=[],
        get=lambda url: fake.calls.append(url)
        or SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"commits": [{"id": 1}, {"id": 2}, {"id": 3}], "truncated": False},
        ),
    )
    monkeypatch.setattr(vmod, "_httpx", fake)

    result = await vmod.check_updates(force=True)

    assert result is not None
    assert result["behind"] == 3
    assert result["behind_capped"] is False
    assert result["upstream_branch"] == "next"
    assert result["is_up_to_date"] is False
    # 对比 URL 必须打在部署分支 next 上（不是 master）
    assert fake.calls and fake.calls[0].endswith("/compare/abcdef1234567890...next")

    # 结果进了磁盘缓存：非 force 的第二次调用直接吃缓存，不再请求上游。
    again = await vmod.check_updates(force=False)
    assert again == result
    assert len(fake.calls) == 1
