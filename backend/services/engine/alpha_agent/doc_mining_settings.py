"""文档挖掘链的 MinerU 解析设置（T-FM-21）—— 因子挖掘内的用户级配置存储。

**配置入口在「因子挖掘 → 文档解析设置」**（产品口径：用户中心的 AI 服务
配置不再承载 MinerU；见 ``docs/因子挖掘_文档输入与挖掘历史_规划.md``）。
本模块是该设置的唯一读写口：

- Redis 按 ``(tenant, user)`` 存一条 JSON（``qm:docmining:mineru_settings:*``），
  云端/本地两套字段共存，``mode`` 决定哪套生效——用户来回切换不用重填；
- 密钥字段只回掩码（``to_public``），任何日志/回包不许出现明文；
- **保存即校验**（系统边界 fail fast）：生效模式缺必填项、URL 非 http(s)、
  档位不在白名单、粘贴的是示例占位文案 → ``ValueError``（端点转 400）；
- 密钥的「留空=保留原值」语义：前端回显的是掩码，回存掩码会把真密钥
  写坏——留空即不动，清除走整条 DELETE。

读取错误与「确实没配」严格区分（与旧 Profile 网关同款纪律）：
``strict=True``（后台轮询重建客户端）读不到时抛 :class:`DocMiningSettingsError`
按可重试处理；``strict=False``（展示面/上传预检）只告警并回落 env 兜底。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace

from backend.services.engine.alpha_agent.llm_client import _is_placeholder
from backend.services.engine.alpha_agent.mineru_client import (
    MODE_CLOUD,
    MODE_LOCAL,
    VALID_MODES,
    MineruConfig,
    resolve_mineru_config,
)

logger = logging.getLogger(__name__)

KEY_PREFIX = "qm:docmining:mineru_settings"

#: 本地解析档位白名单（MinerU 4.x：flash/basic/standard/advanced）
VALID_TIERS = ("flash", "basic", "standard", "advanced")


class DocMiningSettingsError(Exception):
    """设置存储读不到（Redis 故障等瞬态问题）——轮询层按可重试处理。"""


def mask_secret(value: str | None) -> str:
    """掩码回显（与 ai_ide/config.py 的 masked_token 同口径）。

    ≤8 字符整个隐去：``前3****后4`` 在短密钥上会泄露大半。空值回空串，
    调用方据 ``*_set`` 布尔判「是否已配」。
    """
    raw = str(value or "")
    if len(raw) > 8:
        return f"{raw[:3]}****{raw[-4:]}"
    return ""


@dataclass(frozen=True)
class MineruUserSettings:
    """一条用户级设置：两套模式的字段共存，``mode`` 决定生效那套。"""

    mode: str
    api_token: str = ""
    local_url: str = ""
    local_api_key: str = ""
    local_tier: str | None = None

    def configured(self) -> bool:
        """生效模式是否配齐（云端要 Token；本地要 URL）。"""
        if self.mode == MODE_LOCAL:
            return bool(self.local_url)
        return bool(self.api_token)

    def to_config(self) -> MineruConfig:
        """生效模式 → :class:`MineruConfig`。字段不全会抛 ``ValueError``
        （保存期已拦住；手改 Redis 的脏记录由调用方按「未配置」兜底）。"""
        if self.mode == MODE_LOCAL:
            return MineruConfig(
                token=self.local_api_key,
                base_url=self.local_url.rstrip("/"),
                mode=MODE_LOCAL,
                local_tier=self.local_tier,
            )
        return MineruConfig(token=self.api_token)

    def to_public(self) -> dict:
        """掩码视图：密钥只给 ``*_set`` 布尔与掩码串，原文绝不外带。"""
        return {
            "mode": self.mode,
            "configured": self.configured(),
            "api_token_set": bool(self.api_token),
            "api_token_masked": mask_secret(self.api_token),
            "local_url": self.local_url,
            "local_api_key_set": bool(self.local_api_key),
            "local_api_key_masked": mask_secret(self.local_api_key),
            "local_tier": self.local_tier,
        }


def _default_redis_client():
    import redis as redis_lib

    return redis_lib.Redis(
        host=os.getenv("REDIS_HOST") or "redis",
        port=int(os.getenv("REDIS_PORT", "6379")),
        db=int(os.getenv("REDIS_DB", "0")),
        password=os.getenv("REDIS_PASSWORD") or None,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=5,
    )


def _merge_secret(provided: object, existing: str) -> str:
    """密钥字段的保留语义：留空/未传 = 保留原值（前端回显的是掩码）。

    非字符串（前端类型坏了）当未传处理——写坏真密钥比忽略一次输入严重得多。
    """
    if not isinstance(provided, str):
        return existing
    return provided.strip() or existing


class DocMiningSettingsStore:
    """设置读写（Redis 客户端惰性构建；测试可注入替身）。"""

    def __init__(self, *, redis_client=None) -> None:
        self._redis = redis_client

    def _client(self):
        if self._redis is None:
            self._redis = _default_redis_client()
        return self._redis

    @staticmethod
    def _key(user_id: str, tenant_id: str) -> str:
        return f"{KEY_PREFIX}:{tenant_id}:{user_id}"

    def get(
        self, user_id: str | None, tenant_id: str | None, *, strict: bool = False
    ) -> MineruUserSettings | None:
        """→ 设置或 None（没配）。

        - 缺 user/tenant：不读（内部调用没有身份维度）→ None；
        - Redis 读故障：strict 抛 :class:`DocMiningSettingsError`（可重试），
          非 strict 告警 + None（回落 env 兜底，可用性优先）；
        - 键在但内容坏（手改 Redis）：告警 + None——设置页会显示未配置，
          用户重存即可自愈；绝不猜一个半坏的记录去建客户端。
        """
        uid, tid = str(user_id or ""), str(tenant_id or "")
        if not uid or not tid:
            return None
        try:
            raw = self._client().get(self._key(uid, tid))
        except Exception as exc:  # noqa: BLE001 —— 连接/超时/认证都算读不到
            if strict:
                raise DocMiningSettingsError(f"解析设置读取失败: {exc}") from exc
            logger.warning("解析设置读取失败（回落环境配置）: %s", exc)
            return None
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning(
                "解析设置内容损坏（按未配置处理），key=%s", self._key(uid, tid)
            )
            return None
        if not isinstance(data, dict):
            logger.warning(
                "解析设置不是对象（按未配置处理），key=%s", self._key(uid, tid)
            )
            return None
        mode = str(data.get("mode") or "").strip().lower()
        if mode not in VALID_MODES:
            logger.warning("解析设置 mode=%r 非法（按未配置处理）", data.get("mode"))
            return None
        return MineruUserSettings(
            mode=mode,
            api_token=str(data.get("api_token") or ""),
            local_url=str(data.get("local_url") or ""),
            local_api_key=str(data.get("local_api_key") or ""),
            local_tier=str(data.get("local_tier") or "") or None,
        )

    def save(
        self,
        user_id: str,
        tenant_id: str,
        payload: Mapping[str, object],
    ) -> MineruUserSettings:
        """校验并落盘（密钥留空=保留原值）。校验不过抛 ``ValueError``（转 400）。"""
        uid, tid = str(user_id or ""), str(tenant_id or "")
        if not uid or not tid:
            raise ValueError("缺少用户身份，无法保存解析设置")
        existing = self.get(uid, tid) or MineruUserSettings(mode=MODE_CLOUD)

        mode_raw = payload.get("mode")
        if not isinstance(mode_raw, str) or mode_raw.strip().lower() not in VALID_MODES:
            raise ValueError("mode 必须是 cloud（云端）或 local（本地/局域网）")
        mode = mode_raw.strip().lower()

        api_token = _merge_secret(payload.get("api_token"), existing.api_token).strip()
        local_api_key = _merge_secret(
            payload.get("local_api_key"), existing.local_api_key
        ).strip()
        local_url = _merge_secret(payload.get("local_url"), existing.local_url).strip()

        tier_raw = payload.get("local_tier")
        if tier_raw is None:
            local_tier = existing.local_tier  # 未传 = 保留
        elif isinstance(tier_raw, str):
            cleaned = tier_raw.strip().lower()
            local_tier = cleaned or None  # 空串 = 清为服务端默认
        else:
            raise ValueError("local_tier 必须是字符串")
        if local_tier is not None and local_tier not in VALID_TIERS:
            raise ValueError(
                f"local_tier 不支持 {local_tier!r}（可选：{'/'.join(VALID_TIERS)}）"
            )

        if mode == MODE_CLOUD:
            if not api_token:
                raise ValueError(
                    "云端模式需要填写 MinerU API Token"
                    "（或在服务器 .env 配置 MINERU_API_TOKEN）"
                )
            if _is_placeholder(api_token):
                raise ValueError("这看起来是示例文案，不是真实 MinerU API Token")
        else:
            if not local_url:
                raise ValueError(
                    "本地模式需要填写 MinerU 服务地址（如 http://192.168.1.10:8000）"
                )
            try:  # 唯一 URL 校验口径在 MineruConfig（http(s) 且非空）
                MineruConfig(
                    token=local_api_key,
                    base_url=local_url.rstrip("/"),
                    mode=MODE_LOCAL,
                    local_tier=local_tier,
                )
            except ValueError as exc:
                raise ValueError(str(exc)) from exc

        settings = MineruUserSettings(
            mode=mode,
            api_token=api_token,
            local_url=local_url,
            local_api_key=local_api_key,
            local_tier=local_tier,
        )
        record = {
            "mode": settings.mode,
            "api_token": settings.api_token,
            "local_url": settings.local_url,
            "local_api_key": settings.local_api_key,
            "local_tier": settings.local_tier,
        }
        self._client().set(self._key(uid, tid), json.dumps(record, ensure_ascii=False))
        logger.info("解析设置已保存（user=%s mode=%s）", uid, settings.mode)
        return settings

    def clear(self, user_id: str | None, tenant_id: str | None) -> bool:
        """整条清除（密钥逐字段清除没有语义——模式即开关，避免半截记录）。"""
        uid, tid = str(user_id or ""), str(tenant_id or "")
        if not uid or not tid:
            return False
        removed = bool(self._client().delete(self._key(uid, tid)))
        if removed:
            logger.info("解析设置已清除（user=%s）", uid)
        return removed


_store: DocMiningSettingsStore | None = None


def get_doc_mining_settings_store() -> DocMiningSettingsStore:
    global _store
    if _store is None:
        _store = DocMiningSettingsStore()
    return _store


def apply_env_cloud_overrides(cfg: MineruConfig) -> MineruConfig:
    """云端用户配置叠加部署级代理设置（``MINERU_BASE_URL``/``MINERU_MODEL_VERSION``）。

    仅当用户配置是云端、且 env 也是云端通道时生效——部署方可能把云端
    指向自己的代理；**env 是本地模式时绝不叠加**（局域网地址绝不渗进
    云端配置）。本地模式用户配置原样返回（自己的地址就是权威）。
    """
    if cfg.mode != MODE_CLOUD:
        return cfg
    env_cfg = resolve_mineru_config()
    if env_cfg is None or env_cfg.mode != MODE_CLOUD:
        return cfg
    return replace(cfg, base_url=env_cfg.base_url, model_version=env_cfg.model_version)
