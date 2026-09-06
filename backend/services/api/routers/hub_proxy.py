"""
模型广场（Model Hub）反向代理 与 本地模型发布

1. 反向代理：/api/v1/hub/* 转发到远程量化模型社区广场（quantdb.quantmind.cloud），
   写入类操作注入服务端已配置的 QUANTDB_API_KEY（见 shared/runtime_secrets.py）。

2. 本地模型发布：/api/v1/hub/publish-local 由后端在容器内完成「打包模型目录(tar.gz)
   → 申请上传凭据 → 直传 COS → 激活发布」整个流程。模型文件只存在于后端容器，
   前端无法读取，因此真正的压缩与上传都发生在后端。
"""

import hashlib
import io
import json
import logging
import os
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel
from sqlalchemy import text

from backend.services.api.user_app.middleware.auth import get_current_user
from backend.shared.database_manager_v2 import get_session
from backend.shared.model_registry import model_registry_service
from backend.shared.runtime_secrets import get_secret

logger = logging.getLogger(__name__)

router = APIRouter(tags=["HubProxy"])

HUB_BASE_URL = os.getenv("QUANTDB_HUB_URL", "https://quantdb.quantmind.cloud").rstrip(
    "/"
)

_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

# 远端只接受 gzip 压缩的模型包
_UPLOAD_CONTENT_TYPE = "application/gzip"


class PublishLocalRequest(BaseModel):
    model_id: str
    name: str
    description: str = ""
    market: str = "CN"
    algorithm: str = "CatBoost"
    target_horizon: str = "T+5"
    target_mode: str = "classification"
    test_ic: float = 0.0
    rank_ic: float = 0.0
    sharpe_ratio: float = 0.0
    annual_return: float = 0.0
    max_drawdown: float = 0.0
    calmar_ratio: float = 0.0
    visibility: str = "public"


class ImportRemoteRequest(BaseModel):
    hub_model_id: str
    local_name: str | None = None


def _forward_headers(request: Request) -> dict[str, str]:
    return {k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS}


def _require_api_key() -> str:
    api_key = get_secret("QUANTDB_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="QUANTDB_API_KEY 未配置，无法访问模型广场。请在「个人中心 → 数据平台 QuantDB」中配置 API Key。",
        )
    return api_key


def _build_model_archive(model_dir: Path) -> bytes:
    """把模型目录（metadata.json + 模型文件等）打包成内存中的 tar.gz。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(model_dir.rglob("*")):
            if p.is_file():
                tar.add(p, arcname=p.relative_to(model_dir).as_posix())
    return buf.getvalue()


def _safe_extract_tar_gz(archive_bytes: bytes, dest_dir: Path) -> None:
    """安全解压 tar.gz（防路径穿越），落盘到 dest_dir。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    resolved_dest = dest_dir.resolve()
    buf = io.BytesIO(archive_bytes)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        for member in tar.getmembers():
            member_path = (dest_dir / member.name).resolve()
            try:
                member_path.relative_to(resolved_dest)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail=f"压缩包包含非法路径: {member.name}"
                ) from exc
        buf.seek(0)
        with tarfile.open(fileobj=buf, mode="r:gz") as tar2:
            tar2.extractall(path=dest_dir)


def _find_model_file_in_dir(model_dir: Path) -> str:
    """探测模型权重文件（与 ModelRegistryService 一致的候选集）。"""
    candidates = [
        "ensemble_config.json",
        "model.lgb",
        "model.xgb",
        "model.cbm",
        "model.pkl",
        "model.pth",
        "model.txt",
        "model.bin",
        "model_xgb.xgb",
        "model_lgb.lgb",
        "model_cbm.cbm",
        "model_lin.pkl",
        "meta_model.pkl",
    ]
    for name in candidates:
        if (model_dir / name).is_file():
            return name
    # 兜底：目录下任意 model.* / *.pkl / *.lgb
    for p in sorted(model_dir.rglob("*")):
        if p.is_file() and p.suffix.lower() in {
            ".lgb",
            ".xgb",
            ".cbm",
            ".pkl",
            ".bin",
            ".txt",
            ".pth",
            ".onnx",
            ".pt",
            ".json",
        }:
            if p.name.startswith("model") or p.name == "ensemble_config.json":
                return p.relative_to(model_dir).as_posix()
    return ""


def _normalize_target_mode(raw: str) -> str:
    """把 OSS 内的 target_mode（如 return/rank_IC）归一为广场允许的 3 枚举。"""
    s = str(raw or "").strip().lower()
    if s in {"classification", "regression", "ranking"}:
        return s
    # 兼容历史值：return/continuous/value → regression；含 rank 词 → ranking
    if s in {"return", "continuous", "value", "regress"}:
        return "regression"
    if "rank" in s:
        return "ranking"
    if "class" in s:
        return "classification"
    if "regress" in s:
        return "regression"
    return "classification"


def _slugify_hub_name(raw: str, max_len: int = 32) -> str:
    """把广场公开名转成可读 slug（保留中英数，空格/-转下划线）。

    中文的 str.isalnum() 为 True，会被保留；其余符号转下划线并压缩。
    """
    import re

    s = str(raw or "").strip()
    if not s:
        return ""
    s = s.replace("-", "_").replace(" ", "_")
    s = "".join(c if c.isalnum() or c == "_" else "_" for c in s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:max_len].strip("_")


def _build_import_model_id(
    *, hub_model_id: str, market: str, hub_name: str = ""
) -> str:
    """为导入模型生成本地 model_id（可读 slug + hub 短尾，避免 UUID 乱名）。

    格式：mdl_{market}_hub_{slug(广场公开名)}_{hub_id末6位}
    如 mdl_cn_hub_L2_CatBoost_T5_3f9a2c；无公开名时回退旧规则保证幂等。
    """
    hub_id = hub_model_id.strip()
    short = "".join(c for c in hub_id if c.isalnum())[-6:].lower() or "hub"
    slug = _slugify_hub_name(hub_name)
    if not slug:
        # 回退旧规则（兼容存量幂等）：基于 hub_model_id 全量派生
        return _build_legacy_import_model_id(hub_model_id=hub_id, market=market)
    digest = hashlib.sha1(f"hub_{hub_id}".encode()).hexdigest()[:4]
    market_prefix = str(market or "CN").upper().strip()[:8] or "CN"
    base = f"mdl_{market_prefix.lower()}_hub_{slug}_{short}"
    # 预留 digest 防同名不同包碰撞，总长控制在 128 内（qm_user_models.model_id）
    model_id = f"{base}_{digest}"
    return model_id[:128]


def _build_legacy_import_model_id(*, hub_model_id: str, market: str) -> str:
    """旧版导入 ID 规则（仅用于存量幂等兼容查询）。"""
    raw = f"hub_{hub_model_id.strip()}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    # 复用统一命名规则：mdl_{market}_hub_{sanitized}_{digest}
    sanitized = "".join(
        c if c.isalnum() or c in {"_", "-"} else "_" for c in hub_model_id.strip()
    )
    sanitized = sanitized[:24].strip("_") or "hub"
    market_prefix = str(market or "CN").upper().strip()[:8] or "CN"
    return f"mdl_{market_prefix.lower()}_hub_{sanitized}_{digest}"


# ── 注意顺序：具体的 /api/v1/hub/publish-local 必须先于下方的 catch-all 注册，
# ── 否则会被 {path:path} 捕获、误当普通广场接口代理到远端而 404。


@router.post("/api/v1/hub/publish-local", summary="打包并发布本地模型到广场")
async def publish_local_model(
    req: PublishLocalRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")

    api_key = _require_api_key()

    # 发布名称即 COS 包内/广场展示的原始命名，必填以保证导入侧可读 slug
    if not str(req.name or "").strip():
        raise HTTPException(status_code=400, detail="发布名称不能为空")

    model = await model_registry_service.get_model(
        tenant_id=tenant_id, user_id=user_id, model_id=req.model_id
    )
    if not model:
        raise HTTPException(status_code=404, detail=f"未找到本地模型 {req.model_id}")
    if model.get("status") not in ("ready", "active"):
        raise HTTPException(
            status_code=422, detail=f"模型状态为 {model.get('status')}，暂不可发布"
        )

    model_dir = Path(model.get("storage_path") or "")
    if not model_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"模型文件目录不存在: {model_dir}")

    # 1. 打包模型目录（tar.gz）
    archive = _build_model_archive(model_dir)

    # 补全广场展示用的净值曲线/因子清单等（从本地 metadata/metrics 读取）
    metadata_json = model.get("metadata_json") or {}
    metrics_json = model.get("metrics_json") or {}
    if isinstance(metadata_json, str):
        try:
            metadata_json = json.loads(metadata_json)
        except Exception:
            metadata_json = {}
    if isinstance(metrics_json, str):
        try:
            metrics_json = json.loads(metrics_json)
        except Exception:
            metrics_json = {}
    # factors_summary：优先取 features/feature_columns，否则取 metadata 的 factors_summary
    factors_summary: Any = None
    for key in ("features", "feature_columns", "factors_summary"):
        val = metadata_json.get(key) if isinstance(metadata_json, dict) else None
        if isinstance(val, list) and len(val) > 0:
            factors_summary = val
            break
    equity_curve = None
    if isinstance(metrics_json, dict):
        equity_curve = metrics_json.get("equity_curve") or metrics_json.get(
            "equity_curve_data"
        )
    if equity_curve is None and isinstance(metadata_json, dict):
        equity_curve = metadata_json.get("equity_curve") or metadata_json.get(
            "equity_curve_data"
        )
    psi_val = 0.0
    if isinstance(metrics_json, dict):
        raw_psi = metrics_json.get("psi")
        if isinstance(raw_psi, (int, float)):
            psi_val = float(raw_psi)
    extra_metrics: Any = metrics_json if isinstance(metrics_json, dict) else {}

    # 回测指标回填：训练阶段的 metrics 往往只有 IC，夏普/年化/回撤/Calmar/净值曲线需取最新一次已完成回测
    # 若本模型无回测，则保留训练指标；若有则用回测覆盖（更贴近用户在“回测”页看到的收益）
    backtest_equity: Any | None = None
    try:
        async with get_session(read_only=True) as session:
            bt_row = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT result_json, result_file_path
                        FROM qlib_backtest_runs
                        WHERE user_id = :uid AND tenant_id = :tid
                          AND config_json->>'model_id' = :mid
                          AND status = 'completed'
                        ORDER BY completed_at DESC NULLS LAST, created_at DESC
                        LIMIT 1
                        """
                        ),
                        {"uid": user_id, "tid": tenant_id, "mid": req.model_id},
                    )
                )
                .mappings()
                .first()
            )
            if bt_row:
                bt_summary = bt_row.get("result_json") or {}
                if isinstance(bt_summary, str):
                    try:
                        bt_summary = json.loads(bt_summary)
                    except Exception:
                        bt_summary = {}
                if isinstance(bt_summary, dict):
                    # 训练指标若为 0/缺失则用回测覆盖，显著提升卡片可读性
                    if (
                        isinstance(bt_summary.get("sharpe_ratio"), (int, float))
                        and not req.sharpe_ratio
                    ):
                        req.sharpe_ratio = float(bt_summary["sharpe_ratio"])
                    if (
                        isinstance(bt_summary.get("annual_return"), (int, float))
                        and not req.annual_return
                    ):
                        req.annual_return = float(bt_summary["annual_return"])
                    if (
                        isinstance(bt_summary.get("max_drawdown"), (int, float))
                        and not req.max_drawdown
                    ):
                        # 库中 max_drawdown 为负（或正），统一取绝对值
                        req.max_drawdown = abs(float(bt_summary["max_drawdown"]))
                    if (
                        isinstance(bt_summary.get("calmar_ratio"), (int, float))
                        and not req.calmar_ratio
                    ):
                        req.calmar_ratio = float(bt_summary["calmar_ratio"])
                    elif isinstance(
                        bt_summary.get("annual_return"), (int, float)
                    ) and isinstance(bt_summary.get("max_drawdown"), (int, float)):
                        # 无 calmar 时由年化/回撤推导
                        _dd = abs(float(bt_summary["max_drawdown"])) or 1e-9
                        _calc = float(bt_summary["annual_return"]) / _dd if _dd else 0
                        if not req.calmar_ratio and abs(_calc) < 1e6:
                            req.calmar_ratio = float(_calc)
                    # 额外可展示指标：波动率/胜率等写入 extra_metrics，供卡片“波动率/胜率”展示
                    if not isinstance(extra_metrics, dict):
                        extra_metrics = {}
                    for _k in (
                        "volatility",
                        "win_rate",
                        "profit_factor",
                        "total_trades",
                        "total_return",
                    ):
                        _v = bt_summary.get(_k)
                        if isinstance(_v, (int, float)) and _v not in (None,):
                            # 仅当 extra_metrics 尚无该键时回填，避免覆盖训练已有值
                            if extra_metrics.get(_k) is None:
                                extra_metrics[_k] = (
                                    float(_v) if isinstance(_v, float) else _v
                                )
                # 净值曲线在本地大字段文件中
                _bt_path = bt_row.get("result_file_path")
                if _bt_path:
                    try:
                        p = Path(str(_bt_path))
                        if p.is_file():
                            _payload = json.loads(p.read_text(encoding="utf-8"))
                            if isinstance(_payload, dict):
                                _ec = _payload.get("equity_curve")
                                if isinstance(_ec, list) and len(_ec) > 1:
                                    backtest_equity = _ec
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("读取回测净值曲线失败 %s: %s", req.model_id, exc)
                # summary 中也可能直接含小份 equity_curve
                if backtest_equity is None and isinstance(bt_summary, dict):
                    _ec2 = bt_summary.get("equity_curve")
                    if isinstance(_ec2, list) and len(_ec2) > 1:
                        backtest_equity = _ec2
                if backtest_equity is not None:
                    equity_curve = backtest_equity
    except Exception as exc:  # noqa: BLE001
        logger.debug("回测回填跳过 %s: %s", req.model_id, exc)

    hub_headers = {"X-API-Key": api_key}
    timeout = httpx.Timeout(connect=5.0, read=120.0, write=180.0, pool=10.0)

    # 归一校验字段，避免被广场 400 拦掉
    normalized_mode = _normalize_target_mode(req.target_mode)
    # 兜底：horizon 至少保留 T+N 形态
    normalized_horizon = str(req.target_horizon or "T+5").strip() or "T+5"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # 2. 申请上传凭据
            ticket_payload: dict[str, Any] = {
                "name": req.name,
                "description": req.description,
                "market": req.market,
                "algorithm": req.algorithm,
                "target_horizon": normalized_horizon,
                "target_mode": normalized_mode,
                "test_ic": req.test_ic,
                "rank_ic": req.rank_ic,
                "sharpe_ratio": req.sharpe_ratio,
                "annual_return": req.annual_return,
                "max_drawdown": req.max_drawdown,
                "calmar_ratio": req.calmar_ratio,
                "visibility": req.visibility,
                "file_size_bytes": len(archive),
            }
            if psi_val:
                ticket_payload["psi"] = psi_val
            if factors_summary is not None:
                ticket_payload["factors_summary"] = factors_summary
            if equity_curve is not None:
                ticket_payload["equity_curve"] = equity_curve
            if extra_metrics:
                ticket_payload["extra_metrics"] = extra_metrics
            ticket_resp = await client.post(
                f"{HUB_BASE_URL}/api/v1/hub/models/upload-ticket",
                json=ticket_payload,
                headers=hub_headers,
            )
            if ticket_resp.status_code >= 300:
                raise HTTPException(
                    status_code=502,
                    detail=f"获取上传凭据失败({ticket_resp.status_code}): {ticket_resp.text[:300]}",
                )
            ticket = ticket_resp.json()
            hub_model_id = ticket.get("model_id")
            upload_url = ticket.get("upload_url")
            if not hub_model_id or not upload_url:
                raise HTTPException(
                    status_code=502, detail="上传凭据缺少 model_id/upload_url"
                )

            # 3. 直传模型包（gzip）
            upload_resp = await client.put(
                upload_url,
                content=archive,
                headers={"Content-Type": _UPLOAD_CONTENT_TYPE},
            )
            if upload_resp.status_code >= 300:
                raise HTTPException(
                    status_code=502,
                    detail=f"模型包上传失败({upload_resp.status_code}): {upload_resp.text[:300]}",
                )

            # 4. 激活发布
            publish_resp = await client.post(
                f"{HUB_BASE_URL}/api/v1/hub/models/{hub_model_id}/publish",
                headers=hub_headers,
            )
            if publish_resp.status_code >= 300:
                raise HTTPException(
                    status_code=502,
                    detail=f"发布激活失败({publish_resp.status_code}): {publish_resp.text[:300]}",
                )
            try:
                publish_detail = publish_resp.json()
            except Exception:  # noqa: BLE001
                publish_detail = {}
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"模型广场服务不可达: {exc}"
        ) from exc

    return {
        "success": True,
        "model_id": hub_model_id,
        "packaged_size": len(archive),
        "detail": publish_detail,
    }


@router.post("/api/v1/hub/import-remote", summary="从模型广场下载并导入为本地模型")
async def import_remote_model(
    req: ImportRemoteRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")
    hub_model_id = str(req.hub_model_id or "").strip()
    if not hub_model_id:
        raise HTTPException(status_code=400, detail="hub_model_id 不能为空")
    if not user_id:
        raise HTTPException(status_code=401, detail="未获取到用户信息")

    await model_registry_service.ensure_tables()

    # 1. 拉取广场模型详情（用于展示名/市场兜底），失败不阻断导入
    hub_detail: dict[str, Any] = {}
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0)
        ) as client:
            detail_resp = await client.get(
                f"{HUB_BASE_URL}/api/v1/hub/models/{hub_model_id}"
            )
            if detail_resp.status_code == 200:
                try:
                    hub_detail = detail_resp.json() or {}
                except Exception:
                    hub_detail = {}
            elif detail_resp.status_code == 404:
                raise HTTPException(
                    status_code=404, detail=f"广场模型不存在: {hub_model_id}"
                )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("获取广场模型详情失败 %s: %s", hub_model_id, exc)

    # 2. 获取下载直链（公共接口，无需 API Key）
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)
        ) as client:
            ticket_resp = await client.get(
                f"{HUB_BASE_URL}/api/v1/hub/models/{hub_model_id}/download-ticket"
            )
            if ticket_resp.status_code >= 300:
                raise HTTPException(
                    status_code=502,
                    detail=f"获取下载地址失败({ticket_resp.status_code}): {ticket_resp.text[:300]}",
                )
            ticket = ticket_resp.json() or {}
            download_url = str(ticket.get("download_url") or "").strip()
            if not download_url:
                raise HTTPException(
                    status_code=502, detail="下载地址为空，模型可能尚未发布"
                )
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"模型广场服务不可达: {exc}"
        ) from exc

    # 3. 下载模型包
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=120.0, write=120.0, pool=10.0),
            follow_redirects=True,
        ) as client:
            dl_resp = await client.get(download_url)
            if dl_resp.status_code >= 300:
                raise HTTPException(
                    status_code=502, detail=f"下载模型包失败({dl_resp.status_code})"
                )
            archive_bytes = dl_resp.content
            if not archive_bytes or len(archive_bytes) < 32:
                raise HTTPException(status_code=502, detail="下载的模型包为空或异常")
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"下载模型包失败: {exc}") from exc

    # 4. 生成本地模型 ID 与目录（可读 slug + hub 短尾，不再直接用 UUID）
    market_str = str(hub_detail.get("market") or "CN").upper().strip() or "CN"
    hub_public_name = str(hub_detail.get("name") or "").strip()
    preferred_name = (
        str(req.local_name or "").strip() or hub_public_name or hub_model_id
    )
    local_model_id = _build_import_model_id(
        hub_model_id=hub_model_id, market=market_str, hub_name=preferred_name
    )
    # 幂等：优先按 source_run_id（= hub_model_id）查，其次兼容新/旧派生 ID。
    # 改为按源查后，重命名导入不会重复落库。
    existing = None
    try:
        async with get_session(read_only=True) as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT model_id, storage_path, model_file, status
                        FROM qm_user_models
                        WHERE tenant_id = :tid AND user_id = :uid
                          AND source_run_id = :hub_id
                        ORDER BY updated_at DESC
                        LIMIT 1
                        """
                        ),
                        {"tid": tenant_id, "uid": user_id, "hub_id": hub_model_id},
                    )
                )
                .mappings()
                .first()
            )
            if row is not None:
                existing = {
                    "model_id": str(row.get("model_id") or ""),
                    "storage_path": str(row.get("storage_path") or ""),
                    "model_file": str(row.get("model_file") or ""),
                    "status": str(row.get("status") or ""),
                }
    except Exception as exc:  # noqa: BLE001
        logger.debug("幂等 source_run_id 查询跳过 %s: %s", hub_model_id, exc)
    if existing is None:
        legacy_id = _build_legacy_import_model_id(
            hub_model_id=hub_model_id, market=market_str
        )
        for _candidate in (local_model_id, legacy_id):
            _m = await model_registry_service.get_model(
                tenant_id=tenant_id, user_id=user_id, model_id=_candidate
            )
            if _m:
                existing = _m
                break
    if existing and str(existing.get("status") or "") in (
        "ready",
        "active",
        "candidate",
    ):
        return {
            "success": True,
            "model_id": existing.get("model_id") or local_model_id,
            "storage_path": existing.get("storage_path") or "",
            "model_file": existing.get("model_file") or "",
            "message": "模型已存在，直接返回已有记录",
            "already_exists": True,
        }

    # 市场分段目录（与 register_model_from_training_run 一致：非 CN 按市场子目录）
    base_root = model_registry_service.user_models_root
    model_dir = base_root / tenant_id / user_id / local_model_id
    if market_str and market_str != "CN":
        model_dir = (
            base_root / tenant_id / user_id / market_str.lower() / local_model_id
        )
    # 清理旧残留（若曾失败过）
    if model_dir.exists():
        try:
            shutil.rmtree(model_dir)
        except Exception:
            pass

    # 5. 解压
    try:
        _safe_extract_tar_gz(archive_bytes, model_dir)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"解压模型包失败: {exc}") from exc

    # 6. 探测模型文件与 metadata
    model_file = _find_model_file_in_dir(model_dir)
    metadata_path = model_dir / "metadata.json"
    metadata: dict[str, Any] = {}
    if metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict):
                metadata = {}
        except Exception:
            metadata = {}

    # 展示名优先级：用户重命名 > 广场公开名（COS 原始命名）> 包内自带名 > hub_id
    package_display = str(
        metadata.get("display_name") or metadata.get("model_name") or ""
    ).strip()
    resolved_display = (
        str(req.local_name or "").strip()
        or hub_public_name
        or package_display
        or hub_model_id
    )
    # 若包内无 metadata.json，用广场详情合成一份基础 metadata
    if not metadata:
        metadata = {
            "display_name": resolved_display,
            "model_name": resolved_display,
            "model_type": str(hub_detail.get("algorithm") or "unknown"),
            "framework": str(hub_detail.get("algorithm") or "unknown"),
            "market": market_str,
            "target_horizon_days": 5,
            "target_mode": str(hub_detail.get("target_mode") or "unknown"),
        }
        # 特征清单：优先取广场 factors_summary
        hub_factors = hub_detail.get("factors_summary")
        if isinstance(hub_factors, list) and hub_factors:
            metadata["feature_columns"] = hub_factors
            metadata["features"] = hub_factors
            metadata["feature_count"] = len(hub_factors)
    else:
        # 补齐导入来源标识与市场信息
        if "market" not in metadata or not str(metadata.get("market") or "").strip():
            metadata["market"] = market_str
        metadata["imported_from_hub"] = True
        metadata["hub_model_id"] = hub_model_id
        metadata["hub_author"] = hub_detail.get("author_username") or ""
        metadata["hub_name"] = hub_public_name
        if package_display and package_display != resolved_display:
            metadata.setdefault("origin_display_name", package_display)
        # 展示名统一为 COS/广场原始命名（用户重命名优先）
        metadata["display_name"] = resolved_display
        metadata["model_name"] = resolved_display
        # 确保 feature_count
        if metadata.get("feature_count") is None:
            feats = metadata.get("feature_columns") or metadata.get("features") or []
            if isinstance(feats, list):
                metadata["feature_count"] = len(feats)

    # 回写 metadata.json（便于后续推理与展示）
    try:
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("回写导入模型 metadata.json 失败 %s: %s", local_model_id, exc)

    if not model_file:
        # 无可识别模型文件：仍落库为 failed，便于排查
        model_file = ""

    metrics: dict[str, Any] = {}
    # 从广场详情同步关键指标到 metrics_json
    for key in (
        "test_ic",
        "rank_ic",
        "sharpe_ratio",
        "annual_return",
        "max_drawdown",
        "calmar_ratio",
        "psi",
    ):
        val = hub_detail.get(key)
        if isinstance(val, (int, float)):
            metrics[key] = float(val)
    hub_equity = hub_detail.get("equity_curve")
    if hub_equity is not None:
        metrics["equity_curve"] = hub_equity
    # 也尝试从包内 metadata 的 performance_metrics 汲取
    pm = (
        metadata.get("performance_metrics")
        if isinstance(metadata.get("performance_metrics"), dict)
        else None
    )
    if isinstance(pm, dict):
        for k, v in pm.items():
            if k not in metrics and isinstance(v, (int, float, str)):
                metrics[k] = v

    # 7. 入库 qm_user_models（status=ready，可直接用于推理/设为默认）
    now = datetime.now(timezone.utc)
    status_to_write = "ready" if model_file else "failed"
    context = (
        metadata.get("context") if isinstance(metadata.get("context"), dict) else {}
    )
    if not isinstance(context, dict):
        context = {}
    # 合并市场到 context
    if not str(context.get("market") or "").strip():
        context["market"] = market_str
        metadata["context"] = context

    async with get_session() as session:
        # 是否需要设为默认（无业务默认时自动设为默认）
        has_business_default = (
            await session.execute(
                text(
                    """
                    SELECT 1 FROM qm_user_models
                    WHERE tenant_id = :tenant_id AND user_id = :user_id
                      AND is_default = TRUE
                      AND COALESCE((metadata_json->>'system_default')::boolean, FALSE) = FALSE
                    LIMIT 1
                    """
                ),
                {"tenant_id": tenant_id, "user_id": user_id},
            )
        ).first()
        should_default = not bool(has_business_default) and status_to_write == "ready"
        if should_default:
            await session.execute(
                text(
                    "UPDATE qm_user_models SET is_default = FALSE, updated_at = :updated_at WHERE tenant_id = :tenant_id AND user_id = :user_id AND is_default = TRUE"
                ),
                {"tenant_id": tenant_id, "user_id": user_id, "updated_at": now},
            )

        await session.execute(
            text(
                """
                INSERT INTO qm_user_models (
                    tenant_id, user_id, model_id, source_run_id, status, storage_path, model_file,
                    metadata_json, metrics_json, is_default, created_at, updated_at, activated_at
                ) VALUES (
                    :tenant_id, :user_id, :model_id, :source_run_id, :status, :storage_path, :model_file,
                    CAST(:metadata_json AS JSONB), CAST(:metrics_json AS JSONB), :is_default,
                    :created_at, :updated_at, :activated_at
                )
                ON CONFLICT (tenant_id, user_id, model_id)
                DO UPDATE SET
                    status = EXCLUDED.status,
                    storage_path = EXCLUDED.storage_path,
                    model_file = EXCLUDED.model_file,
                    metadata_json = EXCLUDED.metadata_json,
                    metrics_json = EXCLUDED.metrics_json,
                    updated_at = EXCLUDED.updated_at
                """
            ),
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "model_id": local_model_id,
                "source_run_id": hub_model_id,
                "status": status_to_write,
                "storage_path": str(model_dir.resolve()),
                "model_file": model_file,
                "metadata_json": json.dumps(metadata, ensure_ascii=False),
                "metrics_json": json.dumps(metrics, ensure_ascii=False),
                "is_default": bool(should_default),
                "created_at": now,
                "updated_at": now,
                "activated_at": now if should_default else None,
            },
        )

    if status_to_write == "failed":
        raise HTTPException(
            status_code=500,
            detail="模型包解压后未找到可识别的模型文件，请联系广场作者检查打包内容",
        )

    return {
        "success": True,
        "model_id": local_model_id,
        "display_name": resolved_display,
        "storage_path": str(model_dir.resolve()),
        "model_file": model_file,
        "market": market_str,
        "already_exists": False,
    }


@router.api_route(
    "/api/v1/hub/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"]
)
async def proxy_model_hub(path: str, request: Request):
    api_key = _require_api_key()

    upstream_url = f"{HUB_BASE_URL}/api/v1/hub/{path}"
    if request.url.query:
        upstream_url += f"?{request.url.query}"

    method = request.method.upper()
    headers = _forward_headers(request)
    headers["X-API-Key"] = api_key
    body = await request.body()

    timeout = httpx.Timeout(connect=5.0, read=60.0, write=60.0, pool=10.0)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(
                method,
                upstream_url,
                content=body if body else None,
                headers=headers,
            )
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers={
                    "content-type": resp.headers.get("content-type", "application/json")
                },
            )
    except httpx.HTTPError:
        return PlainTextResponse("模型广场服务不可达", status_code=502)
