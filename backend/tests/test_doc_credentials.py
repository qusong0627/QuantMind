"""文档解析链凭据解析（doc_credentials / profile_gateway strict）单元测试。

口径唯一且顺序固定（2026-10-09 定稿）：

1. 用户本地配置（因子挖掘内，doc_mining_settings，mode=local）
2. env 本地模式硬顶（``MINERU_MODE=local`` + ``MINERU_LOCAL_URL``）——
   部署方隐私决定，压过用户保存的云 token
3. 用户云端配置（叠加部署级代理 base_url）
4. env 云端配置（``MINERU_API_TOKEN``）
5. 未配置

后台轮询用 strict 解析：「设置存储读不到」必须与「用户没配/清掉了」
区分开——前者重试（引擎重启期 Redis 短暂不可用），后者才是定格失败。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.engine.alpha_agent import (  # noqa: E402
    doc_credentials as cred_mod,
)
from backend.services.engine.alpha_agent import profile_gateway as pg  # noqa: E402
from backend.services.engine.alpha_agent.doc_mining_settings import (  # noqa: E402
    DocMiningSettingsError,
    MineruUserSettings,
)
from backend.services.engine.alpha_agent.mineru_client import (  # noqa: E402
    MODE_LOCAL,
)

TOKEN = "user-token-abcdef123456"


@pytest.fixture()
def clean_env(monkeypatch):
    for name in (
        "MINERU_API_TOKEN",
        "MINERU_MODE",
        "MINERU_BASE_URL",
        "MINERU_MODEL_VERSION",
        "MINERU_LOCAL_URL",
        "MINERU_LOCAL_API_KEY",
        "MINERU_LOCAL_TIER",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _install_settings(
    monkeypatch, settings: MineruUserSettings | None, *, calls: list | None = None
):
    class FakeStore:
        def get(self, user_id, tenant_id, *, strict=False):
            if calls is not None:
                calls.append((user_id, tenant_id, strict))
            return settings

    monkeypatch.setattr(cred_mod, "get_doc_mining_settings_store", lambda: FakeStore())


# ── resolve_effective_mineru_config ─────────────────────────────────


@pytest.mark.asyncio
async def test_user_settings_win_over_env(clean_env) -> None:
    clean_env.setenv("MINERU_API_TOKEN", "env-tok")
    _install_settings(clean_env, MineruUserSettings(mode="cloud", api_token=TOKEN))

    cfg, src = await cred_mod.resolve_effective_mineru_config("u1", "t1")
    assert src == "user"
    assert cfg is not None and cfg.token == TOKEN and cfg.mode == "cloud"


@pytest.mark.asyncio
async def test_env_fallback_when_user_settings_missing(clean_env) -> None:
    clean_env.setenv("MINERU_API_TOKEN", "env-tok")
    _install_settings(clean_env, None)

    cfg, src = await cred_mod.resolve_effective_mineru_config("u1", "t1")
    assert src == "env"
    assert cfg is not None and cfg.token == "env-tok"


@pytest.mark.asyncio
async def test_none_when_nothing_configured(clean_env) -> None:
    _install_settings(clean_env, None)
    assert await cred_mod.resolve_effective_mineru_config("u1", "t1") == (None, "none")


@pytest.mark.asyncio
async def test_env_local_mode_resolves_local_config(clean_env) -> None:
    clean_env.setenv("MINERU_MODE", "local")
    clean_env.setenv("MINERU_LOCAL_URL", "http://192.168.31.9:8000")
    _install_settings(clean_env, None)

    cfg, src = await cred_mod.resolve_effective_mineru_config("u1", "t1")
    assert src == "env"
    assert cfg is not None and cfg.mode == MODE_LOCAL
    assert cfg.base_url == "http://192.168.31.9:8000"


@pytest.mark.asyncio
async def test_user_local_settings_win_and_env_never_leaks_in(clean_env) -> None:
    """用户配本地：自己的地址即权威；env 是云端也不许把云 token 混进来。"""
    clean_env.setenv("MINERU_API_TOKEN", "env-tok")
    _install_settings(
        clean_env,
        MineruUserSettings(
            mode=MODE_LOCAL,
            api_token="kept-cloud-token",
            local_url="http://10.0.0.5:8000",
            local_tier="standard",
        ),
    )

    cfg, src = await cred_mod.resolve_effective_mineru_config("u1", "t1")
    assert src == "user"
    assert cfg is not None and cfg.mode == MODE_LOCAL
    assert cfg.token == "", "本地配置绝不带云 token"
    assert cfg.base_url == "http://10.0.0.5:8000"
    assert cfg.local_tier == "standard"


@pytest.mark.asyncio
async def test_env_local_mode_is_hard_ceiling_over_user_cloud(clean_env) -> None:
    """部署方 MINERU_MODE=local 是隐私硬顶：用户存的云 token 不得把数据引回云。"""
    clean_env.setenv("MINERU_MODE", "local")
    clean_env.setenv("MINERU_LOCAL_URL", "http://192.168.31.9:8000")
    _install_settings(clean_env, MineruUserSettings(mode="cloud", api_token=TOKEN))

    cfg, src = await cred_mod.resolve_effective_mineru_config("u1", "t1")
    assert src == "env", "硬顶生效时取 env 本地配置"
    assert cfg is not None and cfg.mode == MODE_LOCAL
    assert cfg.base_url == "http://192.168.31.9:8000"
    assert TOKEN not in (cfg.token or ""), "用户云 token 绝不出现在硬顶后的配置里"


@pytest.mark.asyncio
async def test_env_local_ceiling_still_lets_user_choose_own_local(clean_env) -> None:
    """env local 不下压用户自己的 local：用户本地地址更具体，仍归用户。"""
    clean_env.setenv("MINERU_MODE", "local")
    clean_env.setenv("MINERU_LOCAL_URL", "http://192.168.31.9:8000")
    _install_settings(
        clean_env,
        MineruUserSettings(mode=MODE_LOCAL, local_url="http://10.0.0.5:8000"),
    )

    cfg, src = await cred_mod.resolve_effective_mineru_config("u1", "t1")
    assert src == "user"
    assert cfg is not None and cfg.mode == MODE_LOCAL
    assert cfg.base_url == "http://10.0.0.5:8000"


@pytest.mark.asyncio
async def test_user_cloud_config_gets_env_proxy_override(clean_env) -> None:
    clean_env.setenv("MINERU_API_TOKEN", "env-tok")
    clean_env.setenv("MINERU_BASE_URL", "https://proxy.example.com")
    _install_settings(clean_env, MineruUserSettings(mode="cloud", api_token=TOKEN))

    cfg, src = await cred_mod.resolve_effective_mineru_config("u1", "t1")
    assert src == "user"
    assert cfg is not None and cfg.base_url == "https://proxy.example.com"
    assert cfg.token == TOKEN


@pytest.mark.asyncio
async def test_incomplete_stored_record_falls_back_to_env(clean_env) -> None:
    """手改 Redis 的半坏记录（mode=cloud 无 token）：按未配置回落 env，不炸。"""
    clean_env.setenv("MINERU_API_TOKEN", "env-tok")
    _install_settings(clean_env, MineruUserSettings(mode="cloud"))

    cfg, src = await cred_mod.resolve_effective_mineru_config("u1", "t1")
    assert src == "env"
    assert cfg is not None and cfg.token == "env-tok"


@pytest.mark.asyncio
async def test_missing_ids_skip_settings_read(clean_env) -> None:
    """没有 user_id/tenant_id（老行/内部调用）：不读设置存储（缺身份维度，
    再试多少次都一样），直接走 env。"""
    clean_env.setenv("MINERU_API_TOKEN", "env-tok")
    calls: list = []
    _install_settings(
        clean_env, MineruUserSettings(mode="cloud", api_token=TOKEN), calls=calls
    )

    cfg, src = await cred_mod.resolve_effective_mineru_config(None, None)
    assert src == "env" and cfg is not None and cfg.token == "env-tok"
    assert calls == [], "缺 id 不许读设置存储"


@pytest.mark.asyncio
async def test_strict_flag_is_forwarded(clean_env) -> None:
    calls: list = []
    _install_settings(clean_env, None, calls=calls)

    await cred_mod.resolve_effective_mineru_config("u1", "t1", strict=True)
    assert calls == [("u1", "t1", True)]


@pytest.mark.asyncio
async def test_strict_store_error_propagates(clean_env) -> None:
    """strict 下「读不到」不许降级成「没配」——异常必须上抛给轮询层重试。"""

    class BoomStore:
        def get(self, user_id, tenant_id, *, strict=False):
            raise DocMiningSettingsError("redis down")

    clean_env.setattr(cred_mod, "get_doc_mining_settings_store", lambda: BoomStore())

    with pytest.raises(DocMiningSettingsError):
        await cred_mod.resolve_effective_mineru_config("u1", "t1", strict=True)


# ── profile_gateway.fetch_profile_raw 的 strict 语义 ────────────────


class _FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def _install_fake_httpx(monkeypatch, *, get):
    def _respond(spec):
        if isinstance(spec, BaseException):
            raise spec
        status, payload = spec
        return _FakeResponse(status, payload)

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def get(self, url, **kwargs):
            return _respond(get)

    monkeypatch.setattr(pg.httpx, "AsyncClient", FakeAsyncClient)


@pytest.mark.asyncio
async def test_fetch_profile_raw_strict_returns_none_on_404(monkeypatch) -> None:
    """404 = 账号确实没有这条 Profile（非瞬态）：strict 也不抛，返回 None。"""
    _install_fake_httpx(monkeypatch, get=(404, {"detail": "no"}))
    assert await pg.fetch_profile_raw("u1", "t1", strict=True) is None


@pytest.mark.asyncio
async def test_fetch_profile_raw_strict_raises_on_5xx(monkeypatch) -> None:
    _install_fake_httpx(monkeypatch, get=(500, {"detail": "boom"}))
    with pytest.raises(pg.ProfileGatewayError):
        await pg.fetch_profile_raw("u1", "t1", strict=True)


@pytest.mark.asyncio
async def test_fetch_profile_raw_strict_raises_on_transport_error(monkeypatch) -> None:
    _install_fake_httpx(monkeypatch, get=pg.httpx.ConnectError("refused"))
    with pytest.raises(pg.ProfileGatewayError):
        await pg.fetch_profile_raw("u1", "t1", strict=True)


@pytest.mark.asyncio
async def test_fetch_profile_raw_returns_data_on_200(monkeypatch) -> None:
    _install_fake_httpx(monkeypatch, get=(200, {"data": {"mineru_api_token": "tok"}}))
    data = await pg.fetch_profile_raw("u1", "t1", strict=True)
    assert data == {"mineru_api_token": "tok"}


if __name__ == "__main__":  # pragma: no cover
    import pytest as _pytest

    sys.exit(_pytest.main([__file__, "-v"]))
