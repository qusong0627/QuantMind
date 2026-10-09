import logging
import os

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from backend.services.engine.alpha_agent.profile_gateway import fetch_profile_raw
from backend.shared.auth import get_internal_call_secret

logger = logging.getLogger(__name__)
router = APIRouter()


class LLMConfig(BaseModel):
    qwen_api_key: str | None = None
    model: str | None = None
    base_url: str | None = None
    provider: str | None = None
    extra_headers: str | None = None  # 自定义请求头（JSON 文本）
    # 向量检索（embedding）通道 —— 与 chat 独立配置。
    # chat 供应商（如 DeepSeek）通常不提供 embedding 接口，必须能单独指向另一个
    # 供应商或本地服务（如 ollama / SiliconFlow / 自建 OpenAI 兼容端点）。
    embedding_model: str | None = None
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None


def _get_user_info(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def _get_api_gateway_url():
    """获取 API Gateway URL，OSS 模式下使用 127.0.0.1"""
    # 优先使用环境变量
    url = os.getenv("INTERNAL_API_GATEWAY_URL", "")
    if url:
        return url
    # OSS 单容器模式，所有服务在同一容器内
    return "http://127.0.0.1:8000"


def _validation_fields(resp: httpx.Response) -> list[str]:
    """从下游错误回包里**只**取校验失败的字段路径，绝不带值。

    下游是 FastAPI，422 的 detail 形如
    ``[{"loc": ["body", "embedding_api_key"], "input": "sk-真实密钥"}]`` ——
    ``input`` 就是明文密钥，所以整包 ``resp.text`` 不能进日志。
    """
    try:
        detail = resp.json().get("detail")
    except Exception:
        return []
    if not isinstance(detail, list):
        return []
    return sorted(
        {
            ".".join(str(part) for part in item.get("loc", ()))
            for item in detail
            if isinstance(item, dict) and item.get("loc")
        }
    )


def _build_profile_payload(config: LLMConfig, raw_body: dict | None = None) -> dict:
    """组装 Profile 更新 payload；显式空字符串表示清除已有 Key。"""
    payload: dict = {}
    # 优先通过 Pydantic 的显式字段集判断（比 raw_body 更可靠，避免重复读 body）
    if "qwen_api_key" in getattr(config, "model_fields_set", set()):
        # 前端发 {"qwen_api_key": ""} => 清除；发 {"qwen_api_key": "sk-..."} => 设置
        payload["api_key"] = str(config.qwen_api_key or "").strip()
    elif raw_body is not None and "qwen_api_key" in raw_body:
        raw_val = raw_body.get("qwen_api_key")
        if raw_val is not None:
            payload["api_key"] = str(raw_val).strip()
    elif config.qwen_api_key is not None and isinstance(config.qwen_api_key, str) and config.qwen_api_key.strip():
        payload["api_key"] = config.qwen_api_key.strip()
    if config.model is not None and config.model.strip():
        payload["llm_model"] = config.model.strip()
    if config.base_url is not None and config.base_url.strip():
        payload["llm_base_url"] = config.base_url.strip()
    if config.provider is not None and config.provider.strip():
        payload["llm_provider"] = config.provider.strip()
    # 自定义请求头：None=不动；空串=清除；有值=覆盖
    if config.extra_headers is not None:
        payload["llm_extra_headers"] = str(config.extra_headers).strip()
    # Embedding 通道：同样遵循 None=不动 / 空串=清除 / 有值=覆盖。
    # 只写显式传入的字段，未传的留给 Profile 现有值 / 容器级 EMBEDDING_* 兜底。
    if config.embedding_model is not None:
        payload["embedding_model"] = str(config.embedding_model).strip()
    if config.embedding_base_url is not None:
        payload["embedding_base_url"] = str(config.embedding_base_url).strip()
    if config.embedding_api_key is not None:
        payload["embedding_api_key"] = str(config.embedding_api_key).strip()
    return payload


@router.get("/llm")
async def get_llm_config(request: Request):
    """获取 LLM 配置状态（从用户 Profile 中读取并脱敏）"""
    user = _get_user_info(request)
    user_id = user["user_id"]
    tenant_id = user.get("tenant_id", "default")

    api_gateway = _get_api_gateway_url()

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            headers = {
                "X-Internal-Call": get_internal_call_secret(),
                "X-User-Id": user_id,
                "X-Tenant-Id": tenant_id,
            }
            # 调用 Gateway 的 profiles 接口获取详情
            resp = await client.get(f"{api_gateway}/api/v1/profiles/{user_id}", headers=headers)
            if resp.status_code == 200:
                body = resp.json()
                data = body.get("data", {})
                key = data.get("api_key")
                has_key = bool(key and key.strip())
                masked = f"{key[:3]}****{key[-4:]}" if has_key and len(key) > 8 else ""
                emb_key = data.get("embedding_api_key")
                has_emb_key = bool(emb_key and emb_key.strip())
                masked_emb = f"{emb_key[:3]}****{emb_key[-4:]}" if has_emb_key and len(emb_key) > 8 else ""
                return {
                    "success": True,
                    "has_key": has_key,
                    "masked_key": masked,
                    "model": data.get("llm_model") or "",
                    "base_url": data.get("llm_base_url") or "",
                    "provider": data.get("llm_provider") or "",
                    "extra_headers": data.get("llm_extra_headers") or "",
                    "embedding_model": data.get("embedding_model") or "",
                    "embedding_base_url": data.get("embedding_base_url") or "",
                    "has_embedding_key": has_emb_key,
                    "masked_embedding_key": masked_emb,
                }
            else:
                logger.warning(f"Failed to fetch profile: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.error(f"Failed to fetch profile for user {user_id}: {e}")

    return {
        "success": True,
        "has_key": False,
        "masked_key": "",
        "model": "",
        "base_url": "",
        "provider": "",
        "extra_headers": "",
        "embedding_model": "",
        "embedding_base_url": "",
        "has_embedding_key": False,
        "masked_embedding_key": "",
    }


@router.post("/llm")
async def save_llm_config(request: Request, config: LLMConfig):
    """保存 LLM 配置（API Key / 模型 / 接口地址），同步到用户 Profile

    前端清除按钮会发送 {"qwen_api_key": ""}，此前被判定为空 payload 而 400；
    此处通过原始 body 区分“未传”与“显式清空”，允许空字符串触发清除。
    """
    user = _get_user_info(request)
    user_id = user["user_id"]
    tenant_id = user.get("tenant_id", "default")

    try:
        raw_body = await request.json()
        if not isinstance(raw_body, dict):
            raw_body = {}
    except Exception:
        raw_body = {}

    payload = _build_profile_payload(config, raw_body)
    # 显式清空 api_key 视为有效 payload，避免 400
    if not payload:
        if "qwen_api_key" in raw_body:
            # 前端明确要清空 Key，即使其他字段为空也放行
            payload = {"api_key": str(raw_body.get("qwen_api_key") or "").strip()}
        else:
            raise HTTPException(status_code=400, detail="请至少填写 API Key、模型或接口地址")
    # 空字符串的 api_key 需要保留以触发清除，正常非空校验已在 _build 中处理
    if payload.get("api_key") == "":
        # 允许仅清空 Key 的请求，不要求其他字段
        pass
    elif not payload:
        raise HTTPException(status_code=400, detail="请至少填写 API Key、模型或接口地址")

    api_gateway = _get_api_gateway_url()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            headers = {
                "X-Internal-Call": get_internal_call_secret(),
                "X-User-Id": user_id,
                "X-Tenant-Id": tenant_id,
            }
            # 更新 Profile (使用网关内部已有的 profiles/{user_id} 接口)
            resp = await client.put(
                f"{api_gateway}/api/v1/profiles/{user_id}",
                headers=headers,
                json=payload,
            )
            if resp.status_code != 200:
                # 不打印 resp.text：FastAPI 422 的 detail 会把违规入参**原样回显**
                # （detail[].input），而本请求体含 api_key / embedding_api_key 明文，
                # 等于把密钥写进服务端日志。只记状态码与出错字段名。
                logger.error(
                    f"Failed to update profile for user {user_id}: "
                    f"HTTP {resp.status_code}, 校验失败字段={_validation_fields(resp)}"
                )
                raise HTTPException(status_code=resp.status_code, detail="同步到用户服务失败")

        return {"success": True, "message": "配置已成功同步到个人档案"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to save config for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class LLMTestConfig(BaseModel):
    qwen_api_key: str = ""
    model: str | None = None
    base_url: str | None = None
    extra_headers: str | None = None  # 自定义请求头（JSON 文本）


@router.post("/llm/test")
async def test_llm_config(request: Request, config: LLMTestConfig):
    """测试 LLM 配置连通性（不落库）：用传入的 Key / 模型 / 地址发一个最小 chat 请求"""
    user = _get_user_info(request)

    api_key = config.qwen_api_key.strip()
    base_url = (config.base_url or "").strip()
    model = (config.model or "").strip()

    if not api_key:
        raise HTTPException(status_code=400, detail="请填写 API Key 后再测试")
    if not base_url:
        raise HTTPException(status_code=400, detail="请填写接口地址后再测试")
    if not model:
        raise HTTPException(status_code=400, detail="请填写模型名称后再测试")

    # 与实际调用共用同一套 URL 拼接 + 自定义头，避免「测试通过、实际 404/400」
    from backend.services.engine.alpha_agent.llm_client import (
        openai_chat_url,
        parse_extra_headers,
    )

    endpoint = openai_chat_url(base_url)

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        **parse_extra_headers(config.extra_headers),
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(endpoint, headers=headers, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "success": True,
                    "status_code": resp.status_code,
                    "model": data.get("model", ""),
                    "message": "连接成功，API 配置有效",
                }
            return {
                "success": False,
                "status_code": resp.status_code,
                "message": f"HTTP {resp.status_code}: {(resp.text or '')[:200]}",
            }
    except httpx.TimeoutException:
        return {"success": False, "message": "请求超时，请检查接口地址或网络"}
    except Exception as e:
        logger.error(f"LLM test failed for user {user.get('user_id')}: {e}")
        return {"success": False, "message": f"连接失败: {e}"}


class DocParseConfig(BaseModel):
    """用户自带 MinerU 文档解析 Token（照 LLM Key 用户级模式）。"""

    mineru_api_token: str | None = None


@router.get("/doc-parse")
async def get_doc_parse_config(request: Request):
    """MinerU Token 状态：用户自带（掩码）+ 服务器 env 兜底 + 有效来源。"""
    user = _get_user_info(request)
    user_id = user["user_id"]
    tenant_id = user.get("tenant_id", "default")

    data = await fetch_profile_raw(user_id, tenant_id)
    profile_token = str((data or {}).get("mineru_api_token") or "").strip()
    env_token = (os.getenv("MINERU_API_TOKEN") or "").strip()
    has_user_token = bool(profile_token)
    return {
        "success": True,
        # data is None = Profile 读不到（网关故障）：前端进「状态未加载」态，
        # 不许把「读不到」显示成「没配」——那会诱导用户覆盖掉自己的 Token。
        "profile_readable": data is not None,
        "has_user_token": has_user_token,
        "masked_token": (
            f"{profile_token[:3]}****{profile_token[-4:]}"
            if has_user_token and len(profile_token) > 8
            else ""
        ),
        "env_configured": bool(env_token),
        "effective_source": (
            "user" if has_user_token else ("env" if env_token else "none")
        ),
    }


@router.post("/doc-parse")
async def save_doc_parse_config(request: Request, config: DocParseConfig):
    """保存/清除用户自带 MinerU Token。

    三态（与 embedding Key 同款）：不传字段=400；空串=清除（回落服务器
    env 配置）；有值=覆盖。
    """
    user = _get_user_info(request)
    user_id = user["user_id"]
    tenant_id = user.get("tenant_id", "default")

    if config.mineru_api_token is None:
        raise HTTPException(
            status_code=400, detail="缺少 mineru_api_token（空串=清除）"
        )
    token = str(config.mineru_api_token).strip()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.put(
                f"{_get_api_gateway_url()}/api/v1/profiles/{user_id}",
                headers={
                    "X-Internal-Call": get_internal_call_secret(),
                    "X-User-Id": user_id,
                    "X-Tenant-Id": tenant_id,
                },
                json={"mineru_api_token": token},
            )
        if resp.status_code != 200:
            # 不打印 resp.text：FastAPI 422 的 detail[].input 会把 Token 明文
            # 原样回显，等于把凭据写进服务端日志。只记状态码与出错字段名。
            logger.error(
                f"Failed to update profile for user {user_id}: "
                f"HTTP {resp.status_code}, 校验失败字段={_validation_fields(resp)}"
            )
            raise HTTPException(status_code=resp.status_code, detail="同步到用户服务失败")
    except HTTPException:
        raise
    except Exception as e:
        # 不把异常原文回吐：httpx 异常链可能带请求体（含 Token）
        logger.error(f"Failed to save doc-parse config for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail="保存失败，请稍后重试") from None

    return {"success": True, "message": "已保存" if token else "已清除"}
