"""文档解析链凭据解析 —— 用户设置（因子挖掘内）优先、env 兜底（唯一口径）。

配置入口在**「因子挖掘 → 文档解析设置」**（``doc_mining_settings``，
按 tenant+user 存 Redis），不是用户中心的 Profile——2026-10-09 产品裁定，
与「设置属于挖因子这里」同口径。本模块是解析链**唯一**的有效配置判定：
上传端点（未配就 503 拦下）、解析服务（按行记录的来源重建客户端）、
配额/统计端点（是否已配提示用户去哪配）共用，避免出现第二个
「悄悄不一样」的判断。

返回的是完整的 :class:`MineruConfig`（不再只是 token）：用户配置自带
mode（cloud/local 及其端点/档位），env 配置由 ``resolve_mineru_config``
读部署环境。优先级（2026-10-09 定稿）：

1. **用户本地配置**（mode=local 且已配 URL）——最具体且数据不出网，永远优先；
2. **env 本地模式硬顶**（``MINERU_MODE=local``）：部署方的隐私决定，压过
   用户保存的云 token——绝不允许用户配置把数据引回云端（方向不可逆）；
3. 用户云端配置（叠加部署级代理 base_url）；
4. env 云端配置（MINERU_API_TOKEN）；
5. 都没有 → ``(None, "none")``。

``strict`` 透传设置存储的读取语义：
- False（展示面/上传预检）：Redis 读不到 → 告警 + 回落 env 兜底；
- True（后台轮询重建客户端）：读不到 → 抛 ``DocMiningSettingsError``
  （轮询层按可重试处理），与「用户确实清掉了设置」区分开。
"""

from __future__ import annotations

import logging

from backend.services.engine.alpha_agent.doc_mining_settings import (
    apply_env_cloud_overrides,
    get_doc_mining_settings_store,
)
from backend.services.engine.alpha_agent.mineru_client import (
    MODE_LOCAL,
    MineruConfig,
    resolve_mineru_config,
)

logger = logging.getLogger(__name__)

TOKEN_SRC_USER = "user"
TOKEN_SRC_ENV = "env"
TOKEN_SRC_NONE = "none"


async def resolve_effective_mineru_config(
    user_id: str | None,
    tenant_id: str | None,
    *,
    strict: bool = False,
) -> tuple[MineruConfig | None, str]:
    """→ ``(config, src)``，src ∈ ``{"user", "env", "none"}``（行上列沿用旧值域）。

    没有 user_id/tenant_id（老行/内部调用）不读设置存储，直接走 env——
    设置按身份维度存，缺身份再试多少次都一样。
    """
    uid = str(user_id or "")
    tid = str(tenant_id or "")
    settings = None
    if uid and tid:
        settings = get_doc_mining_settings_store().get(uid, tid, strict=strict)

    user_cfg: MineruConfig | None = None
    if settings is not None:
        try:
            user_cfg = settings.to_config()
        except ValueError as exc:
            # 手改 Redis 的半坏记录：按未配置回落 env（设置页会显示未配置，
            # 用户重存自愈）；绝不拿半坏配置去建客户端。
            logger.warning("解析设置无法转配置（按未配置处理）: %s", exc)

    # 1. 用户本地配置永远优先：最具体，且数据不出网的方向不可逆。
    if user_cfg is not None and user_cfg.mode == MODE_LOCAL:
        return user_cfg, TOKEN_SRC_USER

    env_cfg = resolve_mineru_config()

    # 2. env 本地模式硬顶：部署方的隐私决定压过用户保存的云 token——
    #    绝不允许用户配置把数据引回云端（这里 user_cfg 若存在必为 cloud）。
    if env_cfg is not None and env_cfg.mode == MODE_LOCAL:
        if user_cfg is not None:
            logger.info(
                "部署为本地解析模式（MINERU_MODE=local），用户云端配置不生效（隐私硬顶）"
            )
        return env_cfg, TOKEN_SRC_ENV

    # 3. 用户云端配置（叠加部署级代理 base_url）。
    if user_cfg is not None:
        return apply_env_cloud_overrides(user_cfg), TOKEN_SRC_USER

    # 4. env 云端配置。
    if env_cfg is not None:
        return env_cfg, TOKEN_SRC_ENV
    return None, TOKEN_SRC_NONE
