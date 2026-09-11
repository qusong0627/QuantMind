from fastapi import APIRouter
import json
import logging
import os
from .real_trading_utils import *
from .real_trading_utils import (
    _active_strategy_key,
    _default_execution_config,
    _default_live_trade_config,
    _fetch_active_portfolio_snapshot,
    _normalize_execution_config,
    _normalize_identity,
    _normalize_live_trade_config,
    _parse_user_id,
    _schedule_user_notification,
)
from backend.services.live_trading.services.manual_execution_service import (
    manual_execution_service,
)

router = APIRouter()
logger = logging.getLogger(__name__)


async def _build_signal_source_status(
    _redis_client, tenant_id: str, user_id: str
) -> tuple[str | None, dict]:
    try:
        hosted_status = await manual_execution_service.get_default_model_hosted_status(
            tenant_id=tenant_id,
            user_id=user_id,
        )
    except Exception as exc:
        return None, {
            "available": False,
            "source": "missing",
            "message": f"读取默认模型自动托管状态失败: {exc}",
        }

    latest_run_id = str(hosted_status.get("latest_run_id") or "").strip() or None
    if not bool(hosted_status.get("available")):
        return latest_run_id, hosted_status

    return latest_run_id, hosted_status


async def _resolve_strategy_detail(*, strategy_id: str, user_id: str) -> dict:
    """解析策略来源并返回标准化元数据。"""
    if strategy_id.startswith("sys_"):
        template_id = strategy_id.replace("sys_", "", 1)
        # 运行时导入，避免顶层导入 engine 模块触发 qlib 报错
        try:
            from backend.services.engine.qlib_app.services.strategy_templates import (
                get_template_by_id,
            )

            template = get_template_by_id(template_id)
        except (ImportError, ModuleNotFoundError):
            template = None

        if not template:
            raise HTTPException(status_code=404, detail="内置策略模板不存在")
        return {
            "strategy_name": template.name,
            "execution_config": getattr(template, "execution_defaults", None)
            or _default_execution_config(),
            "live_trade_config": getattr(template, "live_defaults", None)
            or _default_live_trade_config(),
            "live_config_tips": getattr(template, "live_config_tips", None) or [],
            "source": "template",
            "template_id": template_id,
            "code": template.code,
        }

    if not strategy_id.isdigit():
        raise HTTPException(status_code=400, detail="strategy_id 格式非法")

    storage_svc = get_strategy_storage_service()
    strategy = await storage_svc.get(strategy_id=int(strategy_id), user_id=user_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="用户策略不存在")
    return {
        "strategy_name": strategy.get("name") or f"strategy_{strategy_id}",
        "execution_config": strategy.get("execution_config")
        or _default_execution_config(),
        "live_trade_config": strategy.get("live_trade_config")
        or _default_live_trade_config(),
        "live_config_tips": strategy.get("live_config_tips") or [],
        "source": "user_strategy",
        "code": strategy.get("code") or "",
    }


# 策略代码 STRATEGY_CONFIG kwargs 中允许覆盖交易参数的键。
# 优先级：策略代码 > 前端传入 > 存储详情 > 默认值。模型只负责生成信号，
# 真正决定买卖节奏的是策略；代码没写才由前端补充。
_CODE_LIVE_OVERRIDE_KEYS = (
    "rebalance_days",
    "schedule_type",
    "trade_weekdays",
    "enabled_sessions",
    "sell_time",
    "buy_time",
    "sell_first",
    "order_type",
    "max_price_deviation",
    "max_orders_per_cycle",
)
_CODE_EXEC_OVERRIDE_KEYS = ("max_buy_drop", "stop_loss")


def _extract_code_trade_overrides(code_str: str) -> tuple[dict, dict]:
    """从策略代码 STRATEGY_CONFIG kwargs 提取交易参数覆盖。"""
    import ast

    exec_over: dict = {}
    live_over: dict = {}
    if not code_str:
        return exec_over, live_over
    try:
        tree = ast.parse(code_str)
    except Exception:
        return exec_over, live_over
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(t, ast.Name) and t.id == "STRATEGY_CONFIG" for t in targets):
            continue
        try:
            cfg = ast.literal_eval(node.value)
        except Exception:
            break
        if not isinstance(cfg, dict):
            break
        kwargs = cfg.get("kwargs") if isinstance(cfg.get("kwargs"), dict) else {}
        # 兼容顶层直写（少数模板把交易参数放 STRATEGY_CONFIG 顶层）
        merged_source = {**kwargs}
        for key in list(cfg.keys()):
            if key in _CODE_LIVE_OVERRIDE_KEYS + _CODE_EXEC_OVERRIDE_KEYS and key not in merged_source:
                merged_source[key] = cfg[key]
        for key in _CODE_EXEC_OVERRIDE_KEYS:
            if merged_source.get(key) is not None:
                exec_over[key] = merged_source[key]
        for key in _CODE_LIVE_OVERRIDE_KEYS:
            if merged_source.get(key) is not None:
                live_over[key] = merged_source[key]
        break
    return exec_over, live_over


@router.post("/start")
async def start_trading(
    user_id: Optional[str] = Form(None),
    strategy_id: Optional[str] = Form(None),
    strategy_file: Optional[UploadFile] = File(None),
    trading_mode: str = Form("SIMULATION"),  # 仅支持 SIMULATION
    execution_config: Optional[str] = Form(None),
    live_trade_config: Optional[str] = Form(None),
    tenant_id: Optional[str] = Form(None),
    auth: AuthContext = Depends(get_auth_context),
    redis: RedisClient = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
):
    resolved_user_id, resolved_tenant_id = _normalize_identity(
        auth, user_id=user_id, tenant_id=tenant_id
    )

    try:
        strategy_name = "unknown_strategy"
        mode = str(trading_mode or "SIMULATION").strip().upper()
        if mode not in {"SIMULATION", "REAL"}:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的交易模式: {mode}。支持 SIMULATION(模拟盘) / REAL(通达信实盘)",
            )
        # REAL 模式需确认通达信实盘桥已启用
        if mode == "REAL":
            enable_real = (
                os.getenv("ENABLE_REAL_TRADING", "false").strip().lower() == "true"
            )
            if not enable_real:
                raise HTTPException(
                    status_code=400,
                    detail="REAL 模式需要设置 ENABLE_REAL_TRADING=true 并配置 TDX 桥，请检查 .env",
                )

        if not strategy_id and not strategy_file:
            raise HTTPException(
                status_code=400, detail="strategy_id 或 strategy_file 至少提供一个"
            )

        strategy_name = "uploaded_strategy.py"
        exec_config = _default_execution_config()
        live_config = _default_live_trade_config()
        if strategy_id:
            detail = await _resolve_strategy_detail(
                strategy_id=strategy_id, user_id=resolved_user_id
            )
            strategy_name = detail["strategy_name"]
            exec_config = detail["execution_config"]
            live_config = (
                detail.get("live_trade_config") or _default_live_trade_config()
            )
        elif strategy_file:
            strategy_name = strategy_file.filename or strategy_name

        exec_config = _normalize_execution_config({}, exec_config)
        ExecutionConfigSchema.model_validate(exec_config)
        live_config = _normalize_live_trade_config({}, live_config)

        # 前端可覆盖风控参数（以本次启动快照为准）
        if execution_config:
            try:
                user_exec_cfg = json.loads(execution_config)
            except Exception:
                raise HTTPException(
                    status_code=400, detail="execution_config 不是合法 JSON"
                )
            if not isinstance(user_exec_cfg, dict):
                raise HTTPException(
                    status_code=400, detail="execution_config 必须是对象"
                )
            exec_config = _normalize_execution_config(user_exec_cfg, exec_config)
            ExecutionConfigSchema.model_validate(exec_config)
        if live_trade_config:
            try:
                user_live_cfg = json.loads(live_trade_config)
            except Exception:
                raise HTTPException(
                    status_code=400, detail="live_trade_config 不是合法 JSON"
                )
            if not isinstance(user_live_cfg, dict):
                raise HTTPException(
                    status_code=400, detail="live_trade_config 必须是对象"
                )
            live_config = _normalize_live_trade_config(user_live_cfg, live_config)

        deployment_market = str(
            (live_config or {}).get("market")
            or (exec_config or {}).get("market")
            or "CN"
        ).upper()
        readiness = await run_trading_readiness_precheck(
            db,
            mode=mode,
            redis_client=redis.client,
            user_id=resolved_user_id,
            tenant_id=resolved_tenant_id,
            market=deployment_market,
        )
        signal_readiness = readiness.get("signal_readiness") or {}
        trading_permission = str(
            readiness.get("trading_permission")
            or signal_readiness.get("trading_permission")
            or "trade_enabled"
        )
        if not readiness.get("passed"):
            failed_items = [
                item
                for item in readiness.get("items", [])
                if not bool(item.get("passed"))
            ]
            first_failed = failed_items[0] if failed_items else None
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "交易准备度检测未通过，请先确认模型、数据库与本地行情数据状态",
                    "precheck_failed": True,
                    "checked_at": readiness.get("checked_at"),
                    "items": readiness.get("items", []),
                    "first_failed_reason": (first_failed or {}).get("detail")
                    or (first_failed or {}).get("label"),
                    "signal_readiness": signal_readiness,
                    "trading_permission": trading_permission,
                },
            )
        if trading_permission == "blocked":
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "默认模型没有可交易的目标交易日推理信号，模拟盘启动已阻断",
                    "precheck_failed": True,
                    "checked_at": readiness.get("checked_at"),
                    "items": readiness.get("items", []),
                    "signal_readiness": signal_readiness,
                    "trading_permission": trading_permission,
                },
            )
        if trading_permission == "observe_only":
            exec_config = {
                **exec_config,
                "trading_permission": "observe_only",
                "auto_trade_enabled": False,
            }
            live_config = {
                **live_config,
                "trading_permission": "observe_only",
                "auto_trade_enabled": False,
            }

        run_id = f"run_{int(time.time())}"
        started_at_iso = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        strategy_dir = get_strategy_path(resolved_user_id)
        os.makedirs(strategy_dir, exist_ok=True)
        file_path = os.path.join(strategy_dir, f"{run_id}.py")
        code_str = ""  # 初始化，避免未定义
        if strategy_file:
            content = await strategy_file.read()
            with open(file_path, "wb") as f:
                f.write(content)
            code_str = content.decode("utf-8")
        else:
            # 从存储加载的策略代码也持久化一份快照，供重启恢复直接使用
            try:
                _detail_code = detail.get("code") if 'detail' in locals() and isinstance(detail, dict) else None
                if _detail_code:
                    code_str = str(_detail_code)
            except Exception:
                pass
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(code_str or f"# strategy_ref={strategy_id}\n")

        # 策略代码优先：STRATEGY_CONFIG kwargs 写了交易频率/时点/风控则覆盖前端传入；
        # 代码没写才由前端补充。生效配置以本次快照为准并持久化。
        code_overrides: dict = {}
        try:
            _code_exec_over, _code_live_over = _extract_code_trade_overrides(code_str)
            if _code_exec_over:
                exec_config = _normalize_execution_config(_code_exec_over, exec_config)
                ExecutionConfigSchema.model_validate(exec_config)
                code_overrides["execution_config"] = sorted(_code_exec_over.keys())
            if _code_live_over:
                live_config = _normalize_live_trade_config(_code_live_over, live_config)
                code_overrides["live_trade_config"] = sorted(_code_live_over.keys())
            if code_overrides:
                logger.info(
                    "[Sim] 策略代码覆盖交易参数 tenant=%s user=%s strategy=%s overrides=%s effective_rebalance=%s",
                    resolved_tenant_id,
                    resolved_user_id,
                    strategy_id or strategy_name,
                    code_overrides,
                    (live_config or {}).get("rebalance_days"),
                )
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning(
                "[Sim] 策略代码交易参数解析失败 tenant=%s user=%s strategy=%s err=%s",
                resolved_tenant_id,
                resolved_user_id,
                strategy_id or strategy_name,
                exc,
            )

        # 3. 沙箱模拟盘执行
        result = {"status": "success", "mode": "SIMULATION"}
        from backend.services.trade.sandbox.manager import sandbox_manager

        try:
            sandbox_run_id = sandbox_manager.submit_strategy(
                tenant_id=resolved_tenant_id,
                user_id=resolved_user_id,
                strategy_id=strategy_id or strategy_name,
                code_str=code_str,
                exec_config=exec_config,
                live_trade_config=live_config,
            )
            logger.info(
                f"[Sim] 用户 {resolved_user_id} 启动了沙箱模拟盘 {strategy_name} -> PID Task"
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"沙箱启动失败: {str(e)}")

        # 4. 状态持久化（started_at 锚定调仓节奏，code_str 保障重启可恢复）
        import hashlib as _hashlib

        _code_sha = _hashlib.sha256(code_str.encode("utf-8")).hexdigest()[:12] if code_str else None
        redis.client.set(
            _active_strategy_key(resolved_tenant_id, resolved_user_id),
            json.dumps(
                {
                    "strategy_id": strategy_id,
                    "run_id": run_id,
                    "mode": mode,
                    "strategy_name": strategy_name,
                    "execution_config": exec_config,
                    "live_trade_config": live_config,
                    "trading_permission": trading_permission,
                    "signal_readiness": signal_readiness,
                    "launch_result": result,
                    "started_at": started_at_iso,
                    "code_sha": _code_sha,
                    "code_str": code_str[:8000] if code_str else None,
                    "code_overrides": code_overrides,
                    # 重启恢复/托管调度解析身份用，避免按键后缀反推（历史 000admin 坑）
                    "runtime_tenant_id": resolved_tenant_id,
                    "runtime_user_id": resolved_user_id,
                }
            ),
        )
        _schedule_user_notification(
            user_id=resolved_user_id,
            tenant_id=resolved_tenant_id,
            title="模拟策略已启动",
            content=f"策略 {strategy_name} 启动成功",
            type="strategy",
            level="success",
            action_url="/trading",
        )

        # 5. 首次启动 Bootstrap：不限时、按最新价、用真实推理立即跑一遍
        # 目的：让用户启动后立刻看到策略在真实运行（等价于手动任务），后续再按
        # 调度计划执行。仅时间门限放开，其余（模型/推理/账户/行情）全部走真实链路；
        # 无可用推理时跳过（不阻断启动），等下一轮推理就绪后由托管调度器自然补跑。
        bootstrap_result = None
        bootstrap_skipped_reason = None
        if mode == "SIMULATION" and trading_permission != "blocked":
            if os.getenv("SIM_BOOTSTRAP_FIRST_RUN_ENABLED", "true").strip().lower() == "true":
                try:
                    from datetime import datetime, timezone

                    now_iso = datetime.now(timezone.utc).isoformat()
                    bootstrap_lock_key = (
                        f"qm:hosted:simulation:bootstrap:{resolved_tenant_id}:{resolved_user_id}:{strategy_id or strategy_name}"
                    )
                    bootstrap_task_id = f"bootstrap_{run_id}_{strategy_id or strategy_name}"
                    # 24h 内同一策略只 bootstrap 一次，避免重复启动短时间内重复建单
                    try:
                        acquired = redis.client.set(bootstrap_lock_key, bootstrap_task_id, ex=24 * 3600, nx=True)
                    except Exception:
                        acquired = True  # Redis 异常不阻断，仍尝试建单（靠 task_id 去重兜底）
                    if acquired:
                        bootstrap_result = await manual_execution_service.create_hosted_task(
                            tenant_id=resolved_tenant_id,
                            user_id=resolved_user_id,
                            strategy_id=strategy_id or strategy_name,
                            trading_mode="SIMULATION",
                            execution_config=exec_config,
                            live_trade_config=live_config,
                            trigger_context={
                                "source": "bootstrap_first_run",
                                "runner_trade_date": datetime.now(timezone.utc).date().isoformat(),
                                "triggered_at": now_iso,
                                "started_at": now_iso,
                                "runner_mode": "SIMULATION",
                                "note": "first_run_unlimited_time_latest_price",
                            },
                            parent_runtime_id=run_id,
                            note="bootstrap: first run unlimited time, latest price, real inference",
                            task_id=bootstrap_task_id,
                        )
                        logger.info(
                            "[SimBootstrap] 首次启动即时任务已创建 tenant=%s user=%s strategy=%s task=%s status=%s",
                            resolved_tenant_id, resolved_user_id, strategy_id or strategy_name,
                            bootstrap_task_id, (bootstrap_result or {}).get("status"),
                        )
                    else:
                        bootstrap_skipped_reason = "bootstrap_lock_exists"
                        logger.info(
                            "[SimBootstrap] 跳过（24h 内已 bootstrap） tenant=%s user=%s strategy=%s",
                            resolved_tenant_id, resolved_user_id, strategy_id or strategy_name,
                        )
                except Exception as exc:
                    # HTTPException 409/400（无可用推理/无模拟账户）等仅 warning，不阻断 start_trading 成功返回
                    from fastapi import HTTPException as _HTTPException

                    if isinstance(exc, _HTTPException) and exc.status_code in (400, 409):
                        bootstrap_skipped_reason = str(exc.detail)[:300] if isinstance(exc.detail, str) else str(exc.detail)[:300]
                        logger.warning(
                            "[SimBootstrap] 跳过（推理/账户未就绪） tenant=%s user=%s strategy=%s reason=%s",
                            resolved_tenant_id, resolved_user_id, strategy_id or strategy_name, bootstrap_skipped_reason,
                        )
                    else:
                        bootstrap_skipped_reason = str(exc)[:300]
                        logger.warning(
                            "[SimBootstrap] 创建失败 tenant=%s user=%s strategy=%s err=%s",
                            resolved_tenant_id, resolved_user_id, strategy_id or strategy_name, exc, exc_info=True,
                        )

        # 启动成功后立即失效该用户的 status 缓存，避免读到上一轮旧值
        try:
            from backend.services.trade_shared.utils.redis_cache import (
                invalidate_user_cache as _invalidate_user_cache,
            )

            _invalidate_user_cache(
                resolved_tenant_id,
                resolved_user_id,
                func_names=["get_status", "get_orders"],
            )
        except Exception:
            pass

        return {
            "status": "success",
            "message": f"策略 {strategy_name} 已成功启动",
            "effective_execution_config": exec_config,
            "effective_live_trade_config": live_config,
            "code_overrides": code_overrides,
            "trading_permission": trading_permission,
            "signal_readiness": signal_readiness,
            "bootstrap": {
                "attempted": mode == "SIMULATION" and trading_permission != "blocked",
                "task_id": (bootstrap_result or {}).get("task_id") if isinstance(bootstrap_result, dict) else None,
                "status": (bootstrap_result or {}).get("status") if isinstance(bootstrap_result, dict) else None,
                "skipped_reason": bootstrap_skipped_reason,
            } if mode == "SIMULATION" else None,
        }
    except HTTPException:
        _schedule_user_notification(
            user_id=resolved_user_id,
            tenant_id=resolved_tenant_id,
            title="策略启动失败",
            content=f"启动失败：{strategy_name}",
            type="strategy",
            level="error",
            action_url="/trading",
        )
        raise
    except Exception as e:
        logger.error(
            f"Failed to start trading for {resolved_user_id}: {e}", exc_info=True
        )
        _schedule_user_notification(
            user_id=resolved_user_id,
            tenant_id=resolved_tenant_id,
            title="策略启动失败",
            content=f"启动异常：{str(e)}",
            type="strategy",
            level="error",
            action_url="/trading",
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/stop")
async def stop_trading(
    user_id: Optional[str] = Form(None),
    tenant_id: Optional[str] = Form(None),
    auth: AuthContext = Depends(get_auth_context),
    redis: RedisClient = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
):
    try:
        resolved_user_id, resolved_tenant_id = _normalize_identity(
            auth, user_id=user_id, tenant_id=tenant_id
        )

        active_strat_raw = redis.client.get(
            _active_strategy_key(resolved_tenant_id, resolved_user_id)
        )
        result = {"status": "success", "message": "Stopped"}
        stopped_strategy_id = None

        if active_strat_raw:
            data = json.loads(active_strat_raw)
            strat_id = data.get("strategy_id", "unknown")
            stopped_strategy_id = strat_id
            from backend.services.trade.sandbox.manager import sandbox_manager

            sandbox_manager.stop_strategy(
                resolved_tenant_id, resolved_user_id, strat_id
            )
            logger.info(f"[Sim] 用户 {resolved_user_id} 停止了沙箱模拟盘")

        # Clear active strategy in Redis
        redis.client.delete(_active_strategy_key(resolved_tenant_id, resolved_user_id))
        # 清理 24h bootstrap 锁，避免同策略 24h 内重启被误挡
        for pat in (
            f"qm:hosted:simulation:bootstrap:{resolved_tenant_id}:{resolved_user_id}:*",
            f"qm:hosted:simulation:{resolved_tenant_id}:{resolved_user_id}:*",
        ):
            try:
                redis.delete_pattern(pat)
            except Exception:
                pass
        # 启停后立即失效该用户的 status/orders 缓存，避免 5-10s 内读到旧值
        try:
            from backend.services.trade_shared.utils.redis_cache import (
                invalidate_user_cache,
            )

            invalidate_user_cache(
                resolved_tenant_id,
                resolved_user_id,
                func_names=["get_status", "get_orders"],
            )
        except Exception:
            pass

        # 同步更新数据库中 portfolio 的 run_status
        try:
            stmt = select(Portfolio).where(
                Portfolio.tenant_id == resolved_tenant_id,
                Portfolio.user_id == resolved_user_id,
                Portfolio.run_status == "running",
                Portfolio.is_deleted.is_(False),
            ).order_by(desc(Portfolio.updated_at)).limit(1)
            db_result = await db.execute(stmt)
            portfolio = db_result.scalars().first()

            if portfolio:
                old_status = portfolio.run_status
                portfolio.run_status = "stopped"
                portfolio.updated_at = datetime.utcnow()
                await db.commit()
                logger.info(
                    "Updated portfolio %d run_status: %s -> stopped",
                    portfolio.id, old_status
                )
        except Exception as db_err:
            logger.warning("Failed to update portfolio run_status: %s", db_err)
            await db.rollback()

        _schedule_user_notification(
            user_id=resolved_user_id,
            tenant_id=resolved_tenant_id,
            title="策略已停止",
            content="当前实盘/模拟策略已停止运行",
            type="strategy",
            level="info",
            action_url="/trading",
        )
        return result
    except HTTPException:
        if "resolved_user_id" in locals():
            _schedule_user_notification(
                user_id=resolved_user_id,
                tenant_id=resolved_tenant_id,
                title="策略停止失败",
                content="停止请求失败，请稍后重试",
                type="strategy",
                level="error",
                action_url="/trading",
            )
        raise
    except Exception as e:
        if "resolved_user_id" in locals():
            _schedule_user_notification(
                user_id=resolved_user_id,
                tenant_id=resolved_tenant_id,
                title="策略停止失败",
                content=f"停止异常：{str(e)}",
                type="strategy",
                level="error",
                action_url="/trading",
            )
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status")
@redis_cache(ttl=5)
async def get_status(
    user_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    trading_mode: Optional[str] = None,
    auth: AuthContext = Depends(get_auth_context),
    redis: RedisClient = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
):
    resolved_user_id, resolved_tenant_id = _normalize_identity(
        auth, user_id=user_id, tenant_id=tenant_id
    )
    status = None  # k8s status removed — simulation-only mode

    # Get active strategy info
    strategy_info = None
    active_strat_id = None
    active_strat_raw = redis.client.get(
        _active_strategy_key(resolved_tenant_id, resolved_user_id)
    )
    portfolio_snapshot = None
    latest_hosted_task = None
    latest_signal_run_id = None
    signal_source_status = {
        "available": False,
        "source": "missing",
        "message": "未检测到当前用户的最新推理信号版本",
    }

    current_mode = "SIMULATION"
    active_exec_config = None
    active_live_trade_config = None
    trading_permission = "trade_enabled"
    signal_readiness = None
    if active_strat_raw:
        try:
            active_data = json.loads(active_strat_raw)
        except Exception as e:
            logger.warning(
                "Invalid active strategy payload for tenant=%s user=%s: %s",
                resolved_tenant_id,
                resolved_user_id,
                e,
            )
            active_data = {}
        if not isinstance(active_data, dict):
            logger.warning(
                "Unexpected active strategy payload type for tenant=%s user=%s: %s",
                resolved_tenant_id,
                resolved_user_id,
                type(active_data).__name__,
            )
            active_data = {}
        active_strat_id = active_data.get("strategy_id")
        current_mode = active_data.get("mode", "SIMULATION")
        if isinstance(active_data.get("execution_config"), dict):
            active_exec_config = active_data.get("execution_config")
        if isinstance(active_data.get("live_trade_config"), dict):
            active_live_trade_config = active_data.get("live_trade_config")
        if active_data.get("trading_permission"):
            trading_permission = str(active_data.get("trading_permission"))
        if isinstance(active_data.get("signal_readiness"), dict):
            signal_readiness = active_data.get("signal_readiness")
        if active_data.get("strategy_name"):
            strategy_info = {
                "id": active_strat_id,
                "name": active_data.get("strategy_name"),
            }

        # 兼容老数据：没有 strategy_name 时再按 strategy_id 回查
        if (
            strategy_info is None
            and isinstance(active_strat_id, str)
            and active_strat_id.startswith("sys_")
        ):
            template_id = active_strat_id.replace("sys_", "", 1)
            try:
                from backend.services.engine.qlib_app.services.strategy_templates import (
                    get_template_by_id,
                )

                template = get_template_by_id(template_id)
            except Exception:
                template = None
            if template:
                strategy_info = {
                    "id": active_strat_id,
                    "name": template.name,
                    "description": template.description,
                }
        elif (
            strategy_info is None
            and isinstance(active_strat_id, str)
            and active_strat_id.isdigit()
        ):
            try:
                storage_svc = get_strategy_storage_service()
                strat = await storage_svc.get(
                    strategy_id=int(active_strat_id), user_id=resolved_user_id
                )
                if strat:
                    strategy_info = {
                        "id": strat["id"],
                        "name": strat["name"],
                        "description": strat["description"],
                    }
            except Exception:
                pass

    latest_signal_run_id, signal_source_status = await _build_signal_source_status(
        redis.client,
        resolved_tenant_id,
        resolved_user_id,
    )
    latest_hosted_task = await manual_execution_service.get_latest_hosted_task(
        tenant_id=resolved_tenant_id,
        user_id=resolved_user_id,
        active_runtime_id=active_data.get("run_id")
        if "active_data" in locals() and isinstance(active_data, dict)
        else None,
    )

    # 获取投资组合快照，优先尊重请求的 trading_mode
    lookup_mode = trading_mode or current_mode
    portfolio_snapshot = await _fetch_active_portfolio_snapshot(
        db,
        tenant_id=resolved_tenant_id,
        user_id=resolved_user_id,
        strategy_id=str(active_strat_id or "").strip() or None
        if not trading_mode
        else None,
        mode=lookup_mode,
    )

    if current_mode == "SIMULATION" and strategy_info:
        strategy_id_for_runtime = str(active_strat_id or "").strip()
        simulation_runtime_alive = False
        simulation_runtime_msg = None
        if strategy_id_for_runtime:
            try:
                from backend.services.trade.sandbox.manager import sandbox_manager

                simulation_runtime_alive = sandbox_manager.is_strategy_running(
                    resolved_tenant_id,
                    resolved_user_id,
                    strategy_id_for_runtime,
                )
            except Exception as exc:
                logger.warning(
                    "Simulation runtime health check failed: tenant=%s user=%s strategy=%s error=%s",
                    resolved_tenant_id,
                    resolved_user_id,
                    strategy_id_for_runtime,
                    exc,
                )
                simulation_runtime_msg = "模拟盘运行状态校验失败，请稍后重试"

        if not simulation_runtime_alive:
            return {
                "status": "not_running",
                "message": simulation_runtime_msg
                or "检测到模拟策略标记，但沙箱运行进程未存活，请重新启动模拟盘",
                "user_id": resolved_user_id,
                "mode": "SIMULATION",
                "strategy": strategy_info,
                "execution_config": active_exec_config,
                "live_trade_config": active_live_trade_config,
                "daily_pnl": portfolio_snapshot["daily_pnl"] if portfolio_snapshot else None,
                "daily_return": portfolio_snapshot["daily_return"] if portfolio_snapshot else None,
                "portfolio": portfolio_snapshot,
                "latest_hosted_task": latest_hosted_task,
                "latest_signal_run_id": latest_signal_run_id,
                "signal_source_status": signal_source_status,
            }

        return {
            "status": "running",
            "user_id": resolved_user_id,
            "mode": "SIMULATION",
            "strategy": strategy_info,
            "execution_config": active_exec_config,
            "live_trade_config": active_live_trade_config,
            "daily_pnl": portfolio_snapshot["daily_pnl"] if portfolio_snapshot else None,
            "daily_return": portfolio_snapshot["daily_return"] if portfolio_snapshot else None,
            "portfolio": portfolio_snapshot,
            "latest_hosted_task": latest_hosted_task,
            "latest_signal_run_id": latest_signal_run_id,
            "signal_source_status": signal_source_status,
            "trading_permission": trading_permission,
            "signal_readiness": signal_readiness,
        }

    # No active strategy
    return {
        "status": "not_running",
        "user_id": resolved_user_id,
        "mode": current_mode,
        "strategy": strategy_info,
        "execution_config": active_exec_config,
        "live_trade_config": active_live_trade_config,
        "daily_pnl": portfolio_snapshot["daily_pnl"] if portfolio_snapshot else None,
        "daily_return": portfolio_snapshot["daily_return"] if portfolio_snapshot else None,
        "portfolio": portfolio_snapshot,
        "latest_hosted_task": latest_hosted_task,
        "latest_signal_run_id": latest_signal_run_id,
        "signal_source_status": signal_source_status,
        "trading_permission": trading_permission,
        "signal_readiness": signal_readiness,
    }


@router.get("/logs")
async def get_logs(
    tail: int = 100,
    user_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    auth: AuthContext = Depends(get_auth_context),
):
    resolved_user_id, resolved_tenant_id = _normalize_identity(
        auth, user_id=user_id, tenant_id=tenant_id
    )
    return {"user_id": resolved_user_id, "logs": [], "message": "模拟盘日志暂不支持远程查看"}


@router.get("/orders")
@redis_cache(ttl=10)
async def get_orders(
    user_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """
    获取订单记录
    """
    try:
        resolved_user_id, resolved_tenant_id = _normalize_identity(
            auth, user_id=user_id, tenant_id=tenant_id
        )
        uid = _parse_user_id(resolved_user_id)
        stmt = select(Order).where(
            Order.user_id == uid, Order.tenant_id == resolved_tenant_id
        )

        if status:
            stmt = stmt.where(Order.status == status)

        stmt = stmt.order_by(desc(Order.created_at)).limit(limit)

        result = await db.execute(stmt)
        orders = result.scalars().all()

        return orders
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch orders: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history")
async def get_trade_history(
    user_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    limit: int = 50,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """
    获取成交历史
    """
    try:
        resolved_user_id, resolved_tenant_id = _normalize_identity(
            auth, user_id=user_id, tenant_id=tenant_id
        )
        uid = _parse_user_id(resolved_user_id)
        stmt = select(Trade).where(
            Trade.user_id == uid, Trade.tenant_id == resolved_tenant_id
        )
        stmt = stmt.order_by(desc(Trade.executed_at)).limit(limit)

        result = await db.execute(stmt)
        trades = result.scalars().all()

        return trades
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch trades: {e}")
        raise HTTPException(status_code=500, detail=str(e))
