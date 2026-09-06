"""数据库存储 - 策略元数据与注册

⚠️  已废弃（DEPRECATED）
    此模块使用独立的 PostgreSQL/MySQL 表（ai_strategies），与 shared.strategy_storage 的
    PostgreSQL strategies 表相互独立，会造成数据不一致。

    新代码请使用：
        from backend.shared.strategy_storage import get_strategy_storage_service

    本模块保留仅用于向后兼容，CRUD 路由已在 engine/main.py 中被
    engine/qlib_app/api/user_strategies.py（shared.strategy_storage）覆盖。
"""

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, Optional

import httpx
from sqlalchemy import Column, DateTime, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .cloud import save_strategy_to_cos

logger = logging.getLogger(__name__)


_DB_URL_RAW = os.getenv("DATABASE_URL", "").strip()
if not _DB_URL_RAW:
    _DB_URL_RAW = os.getenv("AI_STRATEGY_DB_URL", "postgresql+psycopg2://postgres:@localhost:5432/quantmind").strip()

DB_URL = _DB_URL_RAW
if DB_URL.startswith("postgresql://"):
    DB_URL = DB_URL.replace("postgresql://", "postgresql+psycopg2://", 1)
elif DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql+psycopg2://", 1)

engine = create_engine(
    DB_URL,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class StrategyRecord(Base):
    __tablename__ = "ai_strategies"
    id = Column(Integer, primary_key=True, index=True)
    strategy_id = Column(String(64), unique=True, index=True)
    user_id = Column(String(64), index=True)
    name = Column(String(255))
    description = Column(Text)
    market = Column(String(32))
    risk_level = Column(String(16))
    provider = Column(String(32))
    code = Column(Text)  # 保留数据库中的代码副本作为备份
    cos_file_key = Column(String(500))  # COS中的文件key
    cos_file_url = Column(String(1000))  # COS文件URL
    factors = Column(Text)
    risk_controls = Column(Text)
    assumptions = Column(Text)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


# 注释掉自动创建表,避免在导入时连接数据库
# Base.metadata.create_all(bind=engine)


def save_strategy(result, request_desc: str, market: str, risk_level: str, user_id: str | None):
    from ..models import StrategyGenerationResult

    if not isinstance(result, StrategyGenerationResult):
        return

    # 统一管理：优先写入共享存储
    try:
        import asyncio as _asyncio

        from backend.shared.strategy_storage import get_strategy_storage_service

        code_tmp = result.artifacts[0].code if result.artifacts else ""
        try:
            _loop = _asyncio.get_running_loop()
        except RuntimeError:
            _loop = None
        _svc = get_strategy_storage_service()
        _coro = _svc.save(
            user_id=str(user_id or "0"),
            name=result.strategy_name,
            code=code_tmp,
            metadata={
                "description": request_desc,
                "strategy_type": "CUSTOM",
                "status": "DRAFT",
                "parameters": {"market": market, "risk_level": risk_level, "source": "ai_strategy_storage_compat"},
                "tags": ["ai_generated", f"market:{market}"],
            },
        )
        if _loop and _loop.is_running():
            import concurrent.futures as _cf

            with _cf.ThreadPoolExecutor() as _ex:
                _sid = _ex.submit(_asyncio.run, _coro).result(timeout=15).get("id")
        else:
            _sid = _asyncio.run(_coro).get("id")  # type: ignore
        if _sid:
            logger.info("策略已写入共享存储 strategies.id=%s (compat from ai_strategy.storage.database)", _sid)
            return _sid
    except Exception as _e:
        logger.warning(f"共享存储写入失败，回落到 ai_strategies 镜像: {_e}")

    session = SessionLocal()
    try:
        # 生成策略ID
        strategy_id = f"{int(datetime.now().timestamp() * 1000)}"
        code = result.artifacts[0].code if result.artifacts else ""

        # 尝试保存到COS
        cos_result = save_strategy_to_cos(
            strategy_code=code,
            strategy_name=result.strategy_name,
            strategy_id=strategy_id,
            user_id=user_id,
        )

        # 准备数据库记录
        rec = StrategyRecord(
            strategy_id=strategy_id,
            user_id=user_id,
            name=result.strategy_name,
            description=request_desc,
            market=market,
            risk_level=risk_level,
            provider=result.provider,
            code=code,  # 保留数据库中的代码副本作为备份
            cos_file_key=(cos_result.get("file_key", "") if cos_result.get("success") else ""),
            cos_file_url=(cos_result.get("file_url", "") if cos_result.get("success") else ""),
            factors=json.dumps(result.metadata.factors or []),
            risk_controls=json.dumps(result.metadata.risk_controls or []),
            assumptions=json.dumps(result.metadata.assumptions or []),
            notes=result.metadata.notes,
        )

        session.add(rec)
        session.commit()

        # 记录COS保存状态
        if cos_result.get("success"):
            logger.info(
                "策略 %s 已成功保存到COS: %s",
                strategy_id,
                cos_result.get("file_url"),
            )
        else:
            logger.warning(
                "策略 %s COS保存失败: %s",
                strategy_id,
                cos_result.get("error", "Unknown error"),
            )

        # 同步注册到 Strategy Service（失败不影响主流程）
        try:
            sync_result = register_to_strategy_service(
                strategy_id=strategy_id,
                name=result.strategy_name,
                description=request_desc,
                code=code,
                market=market,
                risk_level=risk_level,
                user_id=user_id,
                provider=result.provider,
            )
            if sync_result.get("success"):
                logger.info("策略 %s 已同步到 strategy_service", strategy_id)
        except Exception as sync_err:
            logger.warning("策略 %s 同步到 strategy_service 失败: %s", strategy_id, sync_err)

        return strategy_id
    finally:
        session.close()


def save_strategy_record(data: dict[str, Any]) -> dict[str, Any]:
    """
    保存前端提交的策略记录（兼容端点）
    """
    session = SessionLocal()
    try:
        strategy_id = data.get("strategy_id") or f"{int(datetime.now().timestamp() * 1000)}"
        rec = StrategyRecord(
            strategy_id=strategy_id,
            user_id=data.get("user_id"),
            name=data.get("name", f"strategy-{strategy_id}"),
            description=data.get("description", ""),
            market=data.get("market", "CN"),
            risk_level=data.get("risk_level", "medium"),
            provider=data.get("provider", "manual"),
            code=data.get("code", ""),
            cos_file_key=data.get("cos_file_key", ""),
            cos_file_url=data.get("cos_file_url", ""),
            factors=json.dumps(data.get("factors", [])),
            risk_controls=json.dumps(data.get("risk_controls", [])),
            assumptions=json.dumps(data.get("assumptions", [])),
            notes=data.get("notes"),
            created_at=data.get("created_at", datetime.now()),
        )
        session.add(rec)
        session.commit()
        return {"success": True, "strategy_id": strategy_id}
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        logger.error("保存策略记录失败: %s", exc)
        return {"success": False, "error": str(exc)}
    finally:
        session.close()


def list_strategies(
    offset: int = 0,
    limit: int = 20,
    keyword: str | None = None,
    user_id: str | None = None,
):
    session = SessionLocal()
    try:
        q = session.query(StrategyRecord).order_by(StrategyRecord.created_at.desc())
        if user_id:
            q = q.filter(StrategyRecord.user_id == user_id)
        if keyword:
            like = f"%{keyword}%"
            q = q.filter(StrategyRecord.name.like(like) | StrategyRecord.description.like(like))
        total = q.count()
        rows = q.offset(offset).limit(limit).all()
        data = []
        for r in rows:
            data.append(
                {
                    "strategy_id": r.strategy_id,
                    "name": r.name,
                    "user_id": r.user_id,
                    "description": r.description,
                    "market": r.market,
                    "risk_level": r.risk_level,
                    "provider": r.provider,
                    "cos_file_url": r.cos_file_url,  # 添加COS文件URL
                    "cos_file_key": r.cos_file_key,  # 添加COS文件key
                    "created_at": r.created_at.isoformat(),
                }
            )
        return {"total": total, "items": data}
    finally:
        session.close()


def get_strategy_code(strategy_id: str) -> dict[str, Any]:
    """
    获取策略代码

    Args:
        strategy_id: 策略ID

    Returns:
        Dict: 包含策略代码和文件信息的字典
    """
    try:
        from backend.shared.cos_service import get_cos_service as _get_cos
    except ImportError:

        def _get_cos():
            return None  # noqa: E731

    session = SessionLocal()
    try:
        # 从数据库获取策略信息
        strategy = session.query(StrategyRecord).filter(StrategyRecord.strategy_id == strategy_id).first()

        if not strategy:
            return {"success": False, "error": "策略不存在"}

        result = {
            "success": True,
            "strategy_id": strategy.strategy_id,
            "name": strategy.name,
            "provider": strategy.provider,
            "code": strategy.code,  # 数据库中的代码备份
            "cos_file_url": strategy.cos_file_url,
            "cos_file_key": strategy.cos_file_key,
        }

        # 尝试从COS获取最新代码
        if strategy.cos_file_key:
            try:
                cos_service = _get_cos()
                if cos_service:
                    cos_result = cos_service.download_file(strategy.cos_file_key)
                    if cos_result["success"]:
                        # 解析COS文件内容，提取代码部分
                        file_content = cos_result["file_content"].decode("utf-8")
                        # 找到代码开始位置（在文档字符串之后的部分）
                        code_start = file_content.find('"""\n\n')
                        if code_start != -1:
                            code_from_cos = file_content[code_start + 5 :]  # +5 跳过'"""\n\n'
                            result["code_from_cos"] = code_from_cos
                            result["cos_updated"] = True
                        else:
                            result["cos_updated"] = False
                    else:
                        result["cos_error"] = cos_result.get("error", "COS下载失败")
                else:
                    result["cos_error"] = "COS服务不可用"
            except Exception as e:
                result["cos_error"] = f"COS访问失败: {str(e)}"

        return result

    finally:
        session.close()


def get_strategy_by_id(strategy_id: str) -> dict[str, Any]:
    """
    根据策略ID获取完整策略信息

    Args:
        strategy_id: 策略ID

    Returns:
        Dict: 策略信息
    """
    session = SessionLocal()
    try:
        strategy = session.query(StrategyRecord).filter(StrategyRecord.strategy_id == strategy_id).first()

        if not strategy:
            return {"success": False, "error": "策略不存在"}

        return {
            "success": True,
            "strategy": {
                "strategy_id": strategy.strategy_id,
                "name": strategy.name,
                "description": strategy.description,
                "user_id": strategy.user_id,
                "market": strategy.market,
                "risk_level": strategy.risk_level,
                "provider": strategy.provider,
                "cos_file_url": strategy.cos_file_url,
                "cos_file_key": strategy.cos_file_key,
                "factors": json.loads(strategy.factors) if strategy.factors else [],
                "risk_controls": (json.loads(strategy.risk_controls) if strategy.risk_controls else []),
                "assumptions": (json.loads(strategy.assumptions) if strategy.assumptions else []),
                "notes": strategy.notes,
                "created_at": strategy.created_at.isoformat(),
            },
        }

    finally:
        session.close()


def update_strategy_by_id(strategy_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    """
    根据策略ID更新策略

    Args:
        strategy_id: 策略ID
        updates: 更新字段字典

    Returns:
        Dict: 更新结果
    """
    session = SessionLocal()
    try:
        strategy = session.query(StrategyRecord).filter(StrategyRecord.strategy_id == strategy_id).first()

        if not strategy:
            return {"success": False, "error": "策略不存在"}

        # 更新允许的字段
        allowed_fields = ["name", "description", "market", "risk_level", "notes", "code"]
        for field, value in updates.items():
            if field in allowed_fields and hasattr(strategy, field):
                setattr(strategy, field, value)

        session.commit()

        return {
            "success": True,
            "strategy": {
                "strategy_id": strategy.strategy_id,
                "name": strategy.name,
                "description": strategy.description,
                "user_id": strategy.user_id,
                "market": strategy.market,
                "risk_level": strategy.risk_level,
                "provider": strategy.provider,
                "cos_file_url": strategy.cos_file_url,
                "cos_file_key": strategy.cos_file_key,
                "factors": json.loads(strategy.factors) if strategy.factors else [],
                "risk_controls": (json.loads(strategy.risk_controls) if strategy.risk_controls else []),
                "assumptions": (json.loads(strategy.assumptions) if strategy.assumptions else []),
                "notes": strategy.notes,
                "code": strategy.code,
                "created_at": strategy.created_at.isoformat(),
            },
        }

    except Exception as e:
        session.rollback()
        return {"success": False, "error": f"更新失败: {str(e)}"}
    finally:
        session.close()


def delete_strategy_by_id(strategy_id: str) -> dict[str, Any]:
    """
    根据策略ID删除策略

    Args:
        strategy_id: 策略ID

    Returns:
        Dict: 删除结果
    """
    try:
        from backend.shared.cos_service import get_cos_service as _get_cos
    except ImportError:

        def _get_cos():
            return None  # noqa: E731

    session = SessionLocal()
    try:
        strategy = session.query(StrategyRecord).filter(StrategyRecord.strategy_id == strategy_id).first()

        if not strategy:
            return {"success": False, "error": "策略不存在"}

        # 尝试删除COS中的文件
        if strategy.cos_file_key:
            try:
                cos_service = _get_cos()
                if cos_service:
                    cos_result = cos_service.delete_file(strategy.cos_file_key)
                    if cos_result["success"]:
                        print(f"已从COS删除文件: {strategy.cos_file_key}")
                    else:
                        print(f"COS文件删除失败: {cos_result.get('error', 'Unknown error')}")
            except Exception as e:
                print(f"删除COS文件时出错: {str(e)}")

        # 删除数据库记录
        session.delete(strategy)
        session.commit()

        return {"success": True}

    except Exception as e:
        session.rollback()
        return {"success": False, "error": f"删除失败: {str(e)}"}
    finally:
        session.close()


def duplicate_strategy_by_id(strategy_id: str, new_name: str | None = None) -> dict[str, Any]:
    """
    根据策略ID复制策略

    Args:
        strategy_id: 原策略ID
        new_name: 新策略名称（可选）

    Returns:
        Dict: 复制结果
    """
    session = SessionLocal()
    try:
        original_strategy = session.query(StrategyRecord).filter(StrategyRecord.strategy_id == strategy_id).first()

        if not original_strategy:
            return {"success": False, "error": "原策略不存在"}

        # 生成新的策略ID
        new_strategy_id = f"{int(datetime.now().timestamp() * 1000)}"

        # 创建新的策略记录
        new_strategy = StrategyRecord(
            strategy_id=new_strategy_id,
            user_id=original_strategy.user_id,
            name=new_name or f"{original_strategy.name} (副本)",
            description=original_strategy.description,
            market=original_strategy.market,
            risk_level=original_strategy.risk_level,
            provider=original_strategy.provider,
            code=original_strategy.code,
            cos_file_key="",  # 复制的策略先不保存到COS
            cos_file_url="",
            factors=original_strategy.factors,
            risk_controls=original_strategy.risk_controls,
            assumptions=original_strategy.assumptions,
            notes=original_strategy.notes,
        )

        session.add(new_strategy)
        session.commit()

        return {
            "success": True,
            "strategy": {
                "strategy_id": new_strategy.strategy_id,
                "name": new_strategy.name,
                "description": new_strategy.description,
                "user_id": new_strategy.user_id,
                "market": new_strategy.market,
                "risk_level": new_strategy.risk_level,
                "provider": new_strategy.provider,
                "cos_file_url": new_strategy.cos_file_url,
                "cos_file_key": new_strategy.cos_file_key,
                "factors": (json.loads(new_strategy.factors) if new_strategy.factors else []),
                "risk_controls": (json.loads(new_strategy.risk_controls) if new_strategy.risk_controls else []),
                "assumptions": (json.loads(new_strategy.assumptions) if new_strategy.assumptions else []),
                "notes": new_strategy.notes,
                "created_at": new_strategy.created_at.isoformat(),
            },
        }

    except Exception as e:
        session.rollback()
        return {"success": False, "error": f"复制失败: {str(e)}"}
    finally:
        session.close()


def get_strategy_statistics() -> dict[str, Any]:
    """
    获取策略统计信息

    Returns:
        Dict: 统计信息
    """
    session = SessionLocal()
    try:
        total = session.query(StrategyRecord).count()

        # 这里可以添加更多统计维度，比如按状态分组等
        # 由于当前数据库没有status字段，我们先返回基本统计
        stats = {
            "total": total,
            "active": total,  # 暂时将所有策略视为活跃状态
            "draft": 0,  # 当前数据库没有状态字段
            "archived": 0,  # 当前数据库没有状态字段
        }

        return stats

    except Exception as e:
        return {"error": f"获取统计信息失败: {str(e)}"}
    finally:
        session.close()


def get_file_stats() -> dict[str, Any]:
    """
    获取文件统计信息（兼容 support.py 的旧导入）

    Returns:
        Dict: 文件统计信息
    """
    try:
        from ..services.file_manager import file_manager

        stats = file_manager.get_storage_stats()
        return {
            "total_files": stats.get("total_files", 0),
            "total_size": stats.get("total_size", 0),
            "base_directory": stats.get("base_directory"),
            "categories": stats.get("categories", {}),
            "users": stats.get("users", {}),
            "storage_limit": stats.get("storage_limit"),
            "last_cleanup": stats.get("last_cleanup"),
        }
    except Exception as exc:
        return {"error": f"获取文件统计失败: {str(exc)}"}


def register_to_strategy_service(
    strategy_id: str,
    name: str,
    description: str,
    code: str,
    market: str,
    risk_level: str,
    user_id: str | None,
    provider: str | None = None,
) -> dict[str, Any]:
    """
    将 AI 生成的策略同步注册到 Strategy Service (8003)，
    解决 ai_strategy 与 strategy_service 数据孤岛问题。

    Args:
        strategy_id: ai_strategy 内部 ID
        name: 策略名称
        description: 策略描述
        code: 策略代码
        market: 市场类型
        risk_level: 风险等级
        user_id: 用户ID
        provider: LLM 提供商

    Returns:
        Dict: 注册结果，包含 strategy_service 中的新 ID
    """
    from ..ai_strategy_config import get_config

    config = get_config()

    if not config.STRATEGY_SYNC_ENABLED:
        logger.info("Strategy sync to strategy_service is disabled")
        return {"success": False, "reason": "sync_disabled"}

    url = f"{config.STRATEGY_SERVICE_URL}/api/v1/strategies"

    payload = {
        "name": name,
        "description": description or "",
        "strategy_type": "quantitative",
        "config": {
            "code": code,
            "source": "ai_strategy",
            "ai_strategy_id": strategy_id,
            "market": market,
            "risk_level": risk_level,
            "provider": provider or "unknown",
        },
        "parameters": {
            "market": market,
            "risk_level": risk_level,
        },
        "tags": ["ai_generated", f"market:{market}", f"risk:{risk_level}"],
        "is_public": False,
    }

    # 用 user_id 构造 internal auth header
    # X-User-Id 仅在 X-Internal-Call 密钥匹配时才被下游信任
    from backend.shared.auth import get_internal_call_secret

    headers = {
        "Content-Type": "application/json",
        "X-User-Id": str(user_id) if user_id else "0",
        "X-Internal-Service": "ai_strategy",
        "X-Internal-Call": get_internal_call_secret(),
    }

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(url, json=payload, headers=headers)

            if response.status_code in (200, 201):
                data = response.json()
                registered_id = None
                if isinstance(data, dict):
                    # 适配 Response[StrategyResponse] 格式
                    inner = data.get("data", data)
                    registered_id = inner.get("id")
                logger.info(
                    "策略 %s 已同步注册到 strategy_service, registered_id=%s",
                    strategy_id,
                    registered_id,
                )
                return {
                    "success": True,
                    "registered_id": registered_id,
                    "status_code": response.status_code,
                }
            else:
                logger.warning(
                    "策略 %s 同步到 strategy_service 失败: HTTP %s - %s",
                    strategy_id,
                    response.status_code,
                    response.text[:200],
                )
                return {
                    "success": False,
                    "status_code": response.status_code,
                    "error": response.text[:200],
                }

    except Exception as e:
        logger.warning("策略 %s 同步到 strategy_service 异常（不影响主流程）: %s", strategy_id, e)
        return {"success": False, "error": str(e)}
