"""TdxAiData 接入（T-P6-01）——配置面契约测试。

覆盖：
1. 配置解析优先级：Redis 配置（前端保存）> 环境变量 > 默认 /opt/tdx-aidata；
2. ini Token 外科手术式读写：只动 [Token] 段、其余字节级保留、原子落盘；
3. 状态脱敏：token 只回传掩码（不入日志、不进 API 响应明文）；
4. 源守卫：全仓只有 worker.py 一处 import tqServer（唯一适配器纪律）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]

_SAMPLE_INI = """[TcDsHost]
HostName01=hs.example.com
HostNum=1
IPAddress01=10.0.0.1
Port01=7709

[TcHqHost]
HostName01=hq.example.com
HostNum=1
IPAddress01=10.0.0.2
Port01=7709
PrimaryHost=1

[Token]
token=OLD_TOKEN_VALUE
"""


# ── 1. 配置解析优先级 ───────────────────────────────────────────────


@pytest.mark.unit
def test_resolve_dir_precedence(monkeypatch, tmp_path):
    from backend.shared.tdx_aidata import config

    # 默认
    monkeypatch.delenv("TDX_AIDATA_DIR", raising=False)
    monkeypatch.setattr(config, "_read_redis_config_sync", lambda: {})
    assert config.resolve_dir() == "/opt/tdx-aidata"

    # env 覆盖默认
    monkeypatch.setenv("TDX_AIDATA_DIR", "/tmp/env-dir")
    assert config.resolve_dir() == "/tmp/env-dir"

    # Redis 配置（前端保存）最高
    monkeypatch.setattr(
        config, "_read_redis_config_sync", lambda: {"dir": "/tmp/redis-dir"}
    )
    assert config.resolve_dir() == "/tmp/redis-dir"


@pytest.mark.unit
def test_enabled_flag_precedence(monkeypatch):
    from backend.shared.tdx_aidata import config

    monkeypatch.setattr(config, "_read_redis_config_sync", lambda: {})
    monkeypatch.delenv("TDX_AIDATA_DISABLED", raising=False)
    assert config.is_enabled() is True  # 默认开（目录存在时；开关语义=显式禁用）

    monkeypatch.setenv("TDX_AIDATA_DISABLED", "true")
    assert config.is_enabled() is False

    monkeypatch.setattr(
        config, "_read_redis_config_sync", lambda: {"enabled": "false"}
    )
    assert config.is_enabled() is False

    monkeypatch.setattr(
        config, "_read_redis_config_sync", lambda: {"enabled": "true"}
    )
    assert config.is_enabled() is True


@pytest.mark.unit
def test_socket_and_ini_paths_follow_dir(monkeypatch):
    from backend.shared.tdx_aidata import config

    monkeypatch.setattr(
        config, "_read_redis_config_sync", lambda: {"dir": "/tmp/x-dir"}
    )
    assert config.ini_path() == "/tmp/x-dir/TdxAiData.ini"
    # socket 固定路径（不随 dir 漂移，保证跨服务单实例）
    assert config.socket_path().endswith("qm-tdx-aidata.sock")


# ── 2. ini Token 手术式写入 ─────────────────────────────────────────


@pytest.mark.unit
def test_write_token_surgical(tmp_path):
    from backend.shared.tdx_aidata import config

    ini = tmp_path / "TdxAiData.ini"
    ini.write_text(_SAMPLE_INI, encoding="utf-8")

    config.write_token(str(ini), "NEW_TOKEN_123")

    text = ini.read_text(encoding="utf-8")
    assert "token=NEW_TOKEN_123" in text
    # 其它段与行逐字节保留（只动 token 一行）
    assert "HostName01=hs.example.com" in text
    assert "PrimaryHost=1" in text
    assert "OLD_TOKEN_VALUE" not in text
    # 行序不变且只替换一行
    assert text.count("token=") == 1


@pytest.mark.unit
def test_write_token_appends_section_when_missing(tmp_path):
    from backend.shared.tdx_aidata import config

    ini = tmp_path / "TdxAiData.ini"
    ini.write_text("[TcDsHost]\nHostNum=1\n", encoding="utf-8")
    config.write_token(str(ini), "TK2")
    text = ini.read_text(encoding="utf-8")
    assert "[Token]" in text and "token=TK2" in text
    assert text.index("[TcDsHost]") < text.index("[Token]")


@pytest.mark.unit
def test_read_token_status_masked(tmp_path):
    from backend.shared.tdx_aidata import config

    ini = tmp_path / "TdxAiData.ini"
    ini.write_text(_SAMPLE_INI, encoding="utf-8")
    st = config.token_status(str(ini))
    assert st["configured"] is True
    assert st["masked"].endswith(_SAMPLE_INI.split("token=")[1].strip()[-4:])
    assert "OLD_TOKEN_VALUE" not in str(st)

    ini2 = tmp_path / "empty.ini"
    ini2.write_text("", encoding="utf-8")
    assert config.token_status(str(ini2))["configured"] is False


# ── 3. 目录可用性 ───────────────────────────────────────────────────


@pytest.mark.unit
def test_dir_ready_checks_required_files(tmp_path):
    from backend.shared.tdx_aidata import config

    assert config.dir_ready(str(tmp_path)) is False
    (tmp_path / "libTdxAiData.so").write_bytes(b"x")
    assert config.dir_ready(str(tmp_path)) is False  # 还缺 tqServer.py
    (tmp_path / "tqServer.py").write_text("", encoding="utf-8")
    assert config.dir_ready(str(tmp_path)) is True


# ── 4. 源守卫：唯一适配器 ───────────────────────────────────────────


@pytest.mark.unit
def test_only_worker_imports_tqserver():
    hits = []
    for path in _BACKEND.rglob("*.py"):
        if "/tests/" in str(path) or str(path).endswith("__init__.py"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "from tqServer import" in text or "import tqServer" in text:
            hits.append(path.name)
    assert hits == ["worker.py"], f"tqServer 只能由 worker.py 引入，命中 {hits}"
