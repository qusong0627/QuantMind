"""文档挖掘 MinerU 设置存储（doc_mining_settings）单元测试。

锁六件事：
1. **掩码口径**：>8 位才 `前3****后4`，短密钥整个隐去——回包/日志绝不带明文；
2. **留空=保留**：密钥字段留空不许把已存的真密钥写坏（前端回显的是掩码）；
3. **保存即校验**：生效模式缺必填、URL 非 http(s)、档位不合法、占位文案
   一律 ValueError（端点转 400），fail fast 在写入之前；
4. **读故障 ≠ 没配**：strict 抛 DocMiningSettingsError（轮询重试），
   非 strict 告警回落（上传预检/展示面可用性优先）；
5. **坏记录自愈**：手改 Redis 的脏 JSON/非法 mode 按未配置处理，不猜半坏记录；
6. **env 云端代理叠加**：仅云配置+云 env 叠加 base_url（本地 env 绝不渗入）。

Redis 用内存替身，不碰真 Redis。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.engine.alpha_agent.doc_mining_settings import (  # noqa: E402
    KEY_PREFIX,
    DocMiningSettingsError,
    DocMiningSettingsStore,
    MineruUserSettings,
    apply_env_cloud_overrides,
    mask_secret,
)
from backend.services.engine.alpha_agent.mineru_client import (  # noqa: E402
    MODE_LOCAL,
    MineruConfig,
)

TOKEN = "sk-UH6uHFwTXbMamgsvLQl4aV4Gj"


class StubRedis:
    def __init__(self, *, fail: bool = False) -> None:
        self.data: dict[str, object] = {}
        self.fail = fail
        self.calls: list[tuple] = []

    def get(self, key: str):
        self.calls.append(("get", key))
        if self.fail:
            raise ConnectionError("redis down")
        return self.data.get(key)

    def set(self, key: str, value):
        self.calls.append(("set", key))
        if self.fail:
            raise ConnectionError("redis down")
        self.data[key] = value

    def delete(self, key: str) -> int:
        self.calls.append(("delete", key))
        if self.fail:
            raise ConnectionError("redis down")
        return 1 if self.data.pop(key, None) is not None else 0


@pytest.fixture()
def store_and_redis():
    redis = StubRedis()
    return DocMiningSettingsStore(redis_client=redis), redis


# ── 掩码 ─────────────────────────────────────────────────────────────


def test_mask_secret_long_value_keeps_edges() -> None:
    assert mask_secret(TOKEN) == f"{TOKEN[:3]}****{TOKEN[-4:]}"


def test_mask_secret_short_or_empty_is_fully_hidden() -> None:
    assert mask_secret("short12") == ""
    assert mask_secret("") == ""
    assert mask_secret(None) == ""


def test_to_public_never_leaks_secrets() -> None:
    settings = MineruUserSettings(
        mode="cloud", api_token=TOKEN, local_api_key="local-key-12345678"
    )
    public = settings.to_public()
    assert public["api_token_set"] is True
    assert public["api_token_masked"] == f"{TOKEN[:3]}****{TOKEN[-4:]}"
    assert public["local_api_key_set"] is True
    assert TOKEN not in json.dumps(public, ensure_ascii=False)


# ── 读取 ─────────────────────────────────────────────────────────────


def test_get_missing_identity_returns_none_without_redis(store_and_redis) -> None:
    store, redis = store_and_redis
    assert store.get(None, None) is None
    assert store.get("u1", "") is None
    assert redis.calls == []


def test_get_absent_key_returns_none(store_and_redis) -> None:
    store, _ = store_and_redis
    assert store.get("u1", "t1") is None


def test_get_roundtrips_saved_record(store_and_redis) -> None:
    store, redis = store_and_redis
    store.save("u1", "t1", {"mode": "cloud", "api_token": TOKEN})
    redis_key = f"{KEY_PREFIX}:t1:u1"
    assert redis_key in redis.data

    got = store.get("u1", "t1")
    assert got is not None
    assert got.mode == "cloud"
    assert got.api_token == TOKEN


def test_get_corrupt_json_treated_as_unconfigured(store_and_redis) -> None:
    store, redis = store_and_redis
    redis.data[f"{KEY_PREFIX}:t1:u1"] = "{not json"
    assert store.get("u1", "t1") is None


def test_get_unknown_mode_treated_as_unconfigured(store_and_redis) -> None:
    store, redis = store_and_redis
    redis.data[f"{KEY_PREFIX}:t1:u1"] = json.dumps({"mode": "hybrid"})
    assert store.get("u1", "t1") is None


def test_get_decodes_bytes_payload(store_and_redis) -> None:
    store, redis = store_and_redis
    redis.data[f"{KEY_PREFIX}:t1:u1"] = json.dumps(
        {"mode": "cloud", "api_token": TOKEN}
    ).encode()
    got = store.get("u1", "t1")
    assert got is not None and got.api_token == TOKEN


def test_get_read_failure_strict_raises_but_lenient_falls_back() -> None:
    store = DocMiningSettingsStore(redis_client=StubRedis(fail=True))
    with pytest.raises(DocMiningSettingsError):
        store.get("u1", "t1", strict=True)
    assert store.get("u1", "t1") is None, "非 strict 只告警回落 env 兜底"


# ── 保存：校验 ───────────────────────────────────────────────────────


def test_save_cloud_requires_token(store_and_redis) -> None:
    store, redis = store_and_redis
    with pytest.raises(ValueError, match="云端模式"):
        store.save("u1", "t1", {"mode": "cloud"})
    assert redis.data == {}, "校验不过绝不落盘"


def test_save_rejects_placeholder_token(store_and_redis) -> None:
    store, _ = store_and_redis
    with pytest.raises(ValueError, match="示例文案"):
        store.save("u1", "t1", {"mode": "cloud", "api_token": "your-deepseek-api-key"})


def test_save_rejects_invalid_mode(store_and_redis) -> None:
    store, _ = store_and_redis
    with pytest.raises(ValueError, match="mode"):
        store.save("u1", "t1", {"mode": "hybrid", "api_token": TOKEN})
    with pytest.raises(ValueError, match="mode"):
        store.save("u1", "t1", {})


def test_save_missing_identity_rejected(store_and_redis) -> None:
    store, _ = store_and_redis
    with pytest.raises(ValueError, match="用户身份"):
        store.save("", "", {"mode": "cloud", "api_token": TOKEN})


def test_save_local_requires_valid_http_url(store_and_redis) -> None:
    store, _ = store_and_redis
    with pytest.raises(ValueError, match="服务地址"):
        store.save("u1", "t1", {"mode": "local"})
    with pytest.raises(ValueError, match="http"):
        store.save("u1", "t1", {"mode": "local", "local_url": "192.168.1.10:8000"})


def test_save_rejects_unknown_tier(store_and_redis) -> None:
    store, _ = store_and_redis
    with pytest.raises(ValueError, match="local_tier"):
        store.save(
            "u1",
            "t1",
            {
                "mode": "local",
                "local_url": "http://10.0.0.5:8000",
                "local_tier": "ultra",
            },
        )


# ── 保存：留空=保留 / 切换模式 ───────────────────────────────────────


def test_save_blank_secret_keeps_existing(store_and_redis) -> None:
    store, _ = store_and_redis
    store.save("u1", "t1", {"mode": "cloud", "api_token": TOKEN})
    # 前端回显的是掩码：回存留空必须保留原密钥，绝不能把掩码写进去
    again = store.save("u1", "t1", {"mode": "cloud", "api_token": ""})
    assert again.api_token == TOKEN
    masked_echo = f"{TOKEN[:3]}****{TOKEN[-4:]}"
    assert store.get("u1", "t1").api_token == TOKEN
    assert masked_echo != TOKEN, "掩码串绝不等于真密钥（防呆）"


def test_save_switch_mode_keeps_both_sections(store_and_redis) -> None:
    """云端→本地→云端来回切：两边字段都留着，不用重填。"""
    store, _ = store_and_redis
    store.save("u1", "t1", {"mode": "cloud", "api_token": TOKEN})
    store.save(
        "u1",
        "t1",
        {
            "mode": "local",
            "local_url": "http://192.168.31.9:8000",
            "local_api_key": "lan-key-12345678",
            "local_tier": "standard",
        },
    )
    got = store.get("u1", "t1")
    assert got.mode == "local"
    assert got.local_url == "http://192.168.31.9:8000"
    assert got.api_token == TOKEN, "切到本地不许丢云 token"

    back = store.save("u1", "t1", {"mode": "cloud"})
    assert back.api_token == TOKEN
    assert back.local_tier == "standard"


def test_save_tier_empty_string_clears_to_default(store_and_redis) -> None:
    store, _ = store_and_redis
    store.save(
        "u1",
        "t1",
        {"mode": "local", "local_url": "http://10.0.0.5:8000", "local_tier": "flash"},
    )
    cleared = store.save("u1", "t1", {"mode": "local", "local_tier": ""})
    assert cleared.local_tier is None


def test_save_tier_omitted_keeps_existing(store_and_redis) -> None:
    store, _ = store_and_redis
    store.save(
        "u1",
        "t1",
        {"mode": "local", "local_url": "http://10.0.0.5:8000", "local_tier": "basic"},
    )
    kept = store.save("u1", "t1", {"mode": "local"})
    assert kept.local_tier == "basic"


# ── to_config / configured ──────────────────────────────────────────


def test_to_config_cloud_and_local_shapes() -> None:
    cloud = MineruUserSettings(mode="cloud", api_token=TOKEN).to_config()
    assert cloud.mode == "cloud" and cloud.token == TOKEN

    local = MineruUserSettings(
        mode=MODE_LOCAL,
        local_url="http://10.0.0.5:8000/",
        local_api_key="k",
        local_tier="flash",
    ).to_config()
    assert local.mode == MODE_LOCAL
    assert local.base_url == "http://10.0.0.5:8000", "尾部斜杠收敛"
    assert local.token == "k"
    assert local.local_tier == "flash"


def test_configured_follows_active_mode() -> None:
    assert MineruUserSettings(mode="cloud").configured() is False
    assert MineruUserSettings(mode="cloud", api_token="x").configured() is True
    assert MineruUserSettings(mode=MODE_LOCAL, api_token="x").configured() is False, (
        "云 token 不代表本地模式配好了"
    )
    assert (
        MineruUserSettings(mode=MODE_LOCAL, local_url="http://a:1").configured() is True
    )


# ── 清除 ─────────────────────────────────────────────────────────────


def test_clear_removes_key(store_and_redis) -> None:
    store, redis = store_and_redis
    store.save("u1", "t1", {"mode": "cloud", "api_token": TOKEN})
    assert store.clear("u1", "t1") is True
    assert store.get("u1", "t1") is None
    assert store.clear("u1", "t1") is False


# ── env 云端代理叠加 ─────────────────────────────────────────────────


def test_env_cloud_overrides_apply_to_cloud_config(monkeypatch) -> None:
    monkeypatch.setenv("MINERU_API_TOKEN", "env-tok")
    monkeypatch.setenv("MINERU_BASE_URL", "https://proxy.example.com")
    monkeypatch.setenv("MINERU_MODEL_VERSION", "v9")

    merged = apply_env_cloud_overrides(MineruConfig(token=TOKEN))
    assert merged.base_url == "https://proxy.example.com"
    assert merged.model_version == "v9"
    assert merged.token == TOKEN, "只换端点，不动用户凭证"


def test_env_local_never_leaks_into_cloud_config(monkeypatch) -> None:
    monkeypatch.delenv("MINERU_API_TOKEN", raising=False)
    monkeypatch.setenv("MINERU_MODE", "local")
    monkeypatch.setenv("MINERU_LOCAL_URL", "http://192.168.31.9:8000")

    merged = apply_env_cloud_overrides(MineruConfig(token=TOKEN))
    assert merged.base_url != "http://192.168.31.9:8000", "局域网地址绝不渗进云配置"
    assert merged.base_url == "https://mineru.net"


def test_env_overrides_noop_without_cloud_env(monkeypatch) -> None:
    monkeypatch.delenv("MINERU_API_TOKEN", raising=False)
    monkeypatch.delenv("MINERU_MODE", raising=False)
    monkeypatch.delenv("MINERU_BASE_URL", raising=False)
    cfg = MineruConfig(token=TOKEN)
    assert apply_env_cloud_overrides(cfg) == cfg

    local_cfg = MineruConfig(token="", base_url="http://10.0.0.5:8000", mode=MODE_LOCAL)
    assert apply_env_cloud_overrides(local_cfg) == local_cfg


if __name__ == "__main__":  # pragma: no cover
    import pytest as _pytest

    sys.exit(_pytest.main([__file__, "-v"]))
