from fastapi import APIRouter
import json
import logging
import os
from .real_trading_utils import *
from .real_trading_utils import (
    _active_strategy_key,
    _default_execution_config,
    _default_live_trade_config,
    _delete_active_strategy_aliases,
    _fetch_active_portfolio_snapshot,
    _normalize_execution_config,
    _normalize_identity,
    _normalize_live_trade_config,
    _parse_user_id,
    _read_active_strategy_raw,
    _schedule_status_writeback,
    _schedule_user_notification,
)
from backend.services.live_trading.services.manual_execution_service import (
    manual_execution_service,
)
from backend.services.simulation.services.simulation_hosted_scheduler import (
    run_simulation_cycle_for_active,
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


async def _build_market_and_config_block(
    *,
    active_data: dict,
    strategy: dict | None,
    declared_market: str | None,
    tenant_id: str,
    user_id: str,
) -> dict:
    """``/status`` 的市场与配置回显块（T-RC-15，全部为新增字段，向后兼容）。

    三个 return 分支共用——只在 running 分支加会让「未运行」时页签闸门失效。
    """
    from backend.shared.active_strategy_market import (
        market_gate,
        resolve_active_strategy_market,
        strategy_declared_market,
    )

    active_market, market_source = resolve_active_strategy_market(active_data)
    block: dict = {
        "market": active_market,
        "strategy_market": strategy_declared_market(strategy),
        "market_source": market_source,
        "market_gate": market_gate(declared_market, active_market),
        "config_version": int(active_data.get("config_version") or 0),
        "config_updated_at": active_data.get("config_updated_at"),
        "latest_cycle": None,
    }
    if str(user_id or "").strip():
        try:
            from backend.services.live_trading.services.runtime_log_stream import (
                runtime_log_stream,
            )

            state = runtime_log_stream.read_state(tenant_id=tenant_id, user_id=user_id)
            if state:
                block["latest_cycle"] = {
                    "status": state.get("status") or "",
                    "stage": state.get("stage") or "",
                    "at": state.get("updated_at") or "",
                    "last_line": state.get("last_line") or "",
                }
        except Exception as exc:  # noqa: BLE001 - 回显失败不得让 /status 500
            logger.debug("runtime state read failed: %s", exc)
    # T-RC-16：风控口径分裂体检（D9）。快照里的生效值与策略参数里的值本应一致
    # （/start 与 /runtime-config 都会回写），但历史策略、手工改库、同步失败都会
    # 让它们再次分裂——而分裂的表现是「止损看着配了却不触发」，最隐蔽的一类事故。
    # 只报事实，不在 /status 里偷偷修（读接口不该有副作用）。
    block["execution_config_divergence"] = _execution_config_divergence(
        active_data=active_data, strategy=strategy
    )
    # T-RC-20：守护条数据源。只取与「策略还在不在跑」直接相关的几个循环——
    # 25 个 JobSpec 全量回传会让 /status 变成体检接口，前端也只关心这几个。
    block["schedulers"] = _build_scheduler_health_block()
    return block


#: 守护条关注的调度循环：没有一个活着，托管策略就不会推进
_GUARDIAN_JOB_KEYS = (
    "sim_hosted",  # 进程内模拟托管（SIM）
    "manual_execution",  # 容器/远程 runner 托管（REAL/SHADOW）
    "sentinel_push",  # 盘中哨兵消费（预警链路）
)


def _build_scheduler_health_block() -> list[dict]:
    """采集守护条心跳；任何异常都退化为空列表（/status 不因体检失败而 500）。"""
    try:
        from backend.shared.scheduler_registry import read_heartbeats

        return read_heartbeats(_GUARDIAN_JOB_KEYS)
    except Exception as exc:  # noqa: BLE001
        logger.debug("scheduler heartbeat block failed: %s", exc)
        return []


def _execution_config_divergence(
    *, active_data: dict, strategy: dict | None
) -> dict | None:
    """比对角逐 ``_RISK_EXEC_KEYS``：快照生效值 vs 策略参数值。

    返回 None 表示无从比对（无策略/无参数/未声明），不臆造一致。
    """
    if not isinstance(strategy, dict):
        return None
    params = strategy.get("parameters")
    if not isinstance(params, dict):
        return None
    strategy_exec = params.get("execution_config")
    if not isinstance(strategy_exec, dict):
        return None
    active_exec = (active_data or {}).get("execution_config")
    if not isinstance(active_exec, dict):
        return None
    fields: dict = {}
    for key in _RISK_EXEC_KEYS:
        active_value = active_exec.get(key)
        strategy_value = strategy_exec.get(key)
        if active_value != strategy_value:
            fields[key] = {"active": active_value, "strategy": strategy_value}
    if not fields:
        return {"diverged": False, "fields": {}}
    return {
        "diverged": True,
        "fields": fields,
        "message": (
            "风控生效值与策略参数不一致："
            + "；".join(
                f"{k} 快照={v['active']} 策略={v['strategy']}" for k, v in fields.items()
            )
            + "。退出规则按策略参数执行，请重启策略或提交一次热更新以对齐。"
        ),
    }


#: ``execution_config`` 里属于「风控口径」的键（启动时可被用户配置覆盖）。
#: 其余键（take_profit / max_hold_days / trailing_stop*）属策略作者的退出规则，
#: 同步时必须保留，否则会把策略原有退出规则抹掉。
_RISK_EXEC_KEYS = ("max_buy_drop", "stop_loss")


async def _sync_execution_config_to_strategy(
    *,
    strategy_id: str,
    user_id: str,
    exec_config: dict,
) -> dict:
    """把启动时生效的风控口径合并进策略参数（T-RC-16，修口径分裂）。

    背景：``execution_config`` 有两个读取方，此前从**不同源**取：

    - 隐式止损风控读**运行快照**（``risk_trigger_service.load_implicit_stop_loss``）
    - 持仓退出规则读**策略参数** ``parameters.execution_config``
      （``simulation/engine.py::_load_exit_ruleset``）

    启动向导配的止损只写进快照，于是「用户设定的止损」在退出规则侧根本不生效——
    界面显示止损 -5%，实际退出仍按策略作者（或缺失）的值走。

    这里把生效值**合并**进策略参数：只写风控键，保留策略作者自定的退出规则。
    走 ``strategy_storage.save`` + ``expected_version``，因此运行中改会自然升版并
    留版本快照（受参数锁保护，不绕过审计）。

    返回 ``{"synced", "reason", "version"}``——**不抛异常**：同步失败不应阻断启动，
    但必须如实回传给调用方展示，绝不静默。
    """
    if not str(strategy_id or "").strip().isdigit():
        return {"synced": False, "reason": "非库内策略（系统模板），无参数可写"}
    risk_values = {
        key: exec_config[key]
        for key in _RISK_EXEC_KEYS
        if isinstance(exec_config, dict) and exec_config.get(key) is not None
    }
    if not risk_values:
        return {"synced": False, "reason": "生效配置中无风控参数可同步"}
    try:
        storage_svc = get_strategy_storage_service()
        existing = await storage_svc.get(
            strategy_id=int(strategy_id), user_id=user_id
        )
        if not existing:
            return {"synced": False, "reason": "策略不存在，无法写回参数"}
        parameters = dict(existing.get("parameters") or {})
        current_exec = dict(parameters.get("execution_config") or {})
        merged = {**current_exec, **risk_values}
        if merged == current_exec:
            return {
                "synced": True,
                "reason": "策略参数已与生效配置一致",
                "version": existing.get("version"),
            }
        parameters["execution_config"] = merged
        saved = await storage_svc.save(
            user_id=user_id,
            strategy_id=strategy_id,
            name=existing.get("name", ""),
            code=existing.get("code", ""),
            metadata={
                "description": existing.get("description", ""),
                "tags": existing.get("tags", []) or [],
                "status": existing.get("status", "DRAFT"),
                "is_verified": existing.get("is_verified", False),
                "parameters": parameters,
            },
            expected_version=existing.get("version"),
        )
        return {
            "synced": True,
            "reason": "已写入策略参数（下一轮周期生效）",
            "version": saved.get("version"),
        }
    except (StrategyLockedError, VersionConflictError) as exc:
        return {
            "synced": False,
            "reason": f"策略参数被并发修改，请刷新后重试：{exc}",
        }
    except Exception as exc:  # noqa: BLE001 - 不阻断启动
        logger.warning(
            "sync execution_config to strategy failed strategy=%s: %s", strategy_id, exc
        )
        return {"synced": False, "reason": f"同步失败：{exc}"}


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
    # T-RC-15：策略归属市场（parameters.market），供 /start 一致性校验。
    # 存储详情此前不回传 parameters，调用方无从判定；未声明时为 None（不判定）。
    from backend.shared.active_strategy_market import strategy_declared_market

    return {
        "strategy_name": strategy.get("name") or f"strategy_{strategy_id}",
        "market": strategy_declared_market(strategy),
        "execution_config": strategy.get("execution_config")
        or _default_execution_config(),
        "live_trade_config": strategy.get("live_trade_config")
        or _default_live_trade_config(),
        "live_config_tips": strategy.get("live_config_tips") or [],
        "source": "user_strategy",
        "code": strategy.get("code") or "",
        # T-P3-01 状态机：启动门禁与参数锁使用（sys_ 模板分支无此键）
        "status": strategy.get("status"),
        "version": strategy.get("version"),
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
        declared_market: Optional[str] = None
        if strategy_id:
            detail = await _resolve_strategy_detail(
                strategy_id=strategy_id, user_id=resolved_user_id
            )
            strategy_name = detail["strategy_name"]
            declared_market = detail.get("market")
            exec_config = detail["execution_config"]
            live_config = (
                detail.get("live_trade_config") or _default_live_trade_config()
            )
            # T-P3-01 启动门禁（状态机）：SIM 须 VERIFIED（回测验证）；REAL 须 SIM（模拟证据）。
            # sys_ 模板/文件上传（source 非 user_strategy）不受状态机约束。
            if detail.get("source") == "user_strategy":
                from backend.shared.strategy_lifecycle import can_start

                gate_ok, gate_reason = can_start(detail.get("status"), mode)
                if not gate_ok:
                    raise HTTPException(status_code=400, detail=gate_reason)

                # T-P4-06 ②：晋级门禁（T-P3-05 门槛总表）——回测体检须 A/B；
                # 无体检记录/L（运气嫌疑）/E（证据不足）不得晋级。
                # 应急开关：HEALTH_GATE_ENABLED=false 全局关闭（默认开）。
                from backend.shared.backtest_health import (
                    gate_enabled,
                    latest_strategy_health,
                    latest_user_tracking_error,
                    promotion_gate,
                    sim_trading_days_since,
                )

                if gate_enabled() and strategy_id:
                    health = await latest_strategy_health(
                        strategy_id,
                        tenant_id=resolved_tenant_id,
                        user_id=resolved_user_id,
                    )
                    gate_sim_days = None
                    gate_tracking_error = None
                    if mode == "REAL":
                        gate_sim_days = sim_trading_days_since(
                            redis, resolved_tenant_id, resolved_user_id, strategy_id
                        )
                        gate_tracking_error = latest_user_tracking_error(
                            redis, resolved_user_id
                        )
                    health_ok, health_note = promotion_gate(
                        health,
                        mode=mode,
                        sim_trading_days=gate_sim_days,
                        tracking_error=gate_tracking_error,
                    )
                    if not health_ok:
                        raise HTTPException(status_code=400, detail=health_note)
                    logger.info(
                        "[Lifecycle] 晋级门禁通过 strategy=%s mode=%s：%s",
                        strategy_id, mode, health_note,
                    )
        elif strategy_file:
            strategy_name = strategy_file.filename or strategy_name

        exec_config = _normalize_execution_config({}, exec_config)
        ExecutionConfigSchema.model_validate(exec_config)
        live_config = _normalize_live_trade_config(
            {}, live_config, allow_after_hours=(mode == "SIMULATION")
        )

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
            live_config = _normalize_live_trade_config(
                user_live_cfg, live_config, allow_after_hours=(mode == "SIMULATION")
            )

        deployment_market = str(
            (live_config or {}).get("market")
            or (exec_config or {}).get("market")
            or "CN"
        ).upper()
        # T-RC-15：策略归属市场 vs 本次部署市场一致性校验。此前完全不校验，
        # 港股策略能被 A 股页签启动（反之亦然），跑起来后信号/行情/账户口径
        # 全错却无人拦截。无声明不判定（老策略 parameters 里没有 market）。
        if strategy_id and declared_market and declared_market != deployment_market:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"策略归属市场为 {declared_market}，本次启动市场为 {deployment_market}，"
                    f"二者不一致。请切换到 {declared_market} 市场后再启动，"
                    f"或改用归属该市场的策略。"
                ),
            )
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
                live_config = _normalize_live_trade_config(
                    _code_live_over,
                    live_config,
                    allow_after_hours=(mode == "SIMULATION"),
                )
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
        except ValueError as e:
            # T-P0-02：策略代码未过安全闸门（AST 校验）属调用方错误 → 400 带明细
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"沙箱启动失败: {str(e)}")

        # 4. 状态持久化（started_at 锚定调仓节奏，code_str 保障重启可恢复）
        import hashlib as _hashlib

        _code_sha = _hashlib.sha256(code_str.encode("utf-8")).hexdigest()[:12] if code_str else None
        # T-RC-15：把解析出的部署市场写回快照，让市场判定有稳定出处
        # （此前不写回，desk/status 只能靠 live_config.market 缺省回退到 CN，
        # 港股/美股策略在快照里看不出市场）。仅在配置未显式声明时补写。
        if isinstance(live_config, dict) and not str(live_config.get("market") or "").strip():
            live_config = {**live_config, "market": deployment_market}
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
                    # T-RC-16：配置版本，热更新（/runtime-config）据此乐观并发与留痕
                    "config_version": 1,
                    "config_updated_at": started_at_iso,
                    "config_history": [],
                    # 重启恢复/托管调度解析身份用，避免按键后缀反推（历史 000admin 坑）
                    "runtime_tenant_id": resolved_tenant_id,
                    "runtime_user_id": resolved_user_id,
                }
            ),
        )
        # T-RC-16：把生效风控口径合并进策略参数，消除「快照止损生效、退出规则不生效」
        # 的分裂。失败不阻断启动，但结果如实回传（execution_config_sync）。
        execution_config_sync = await _sync_execution_config_to_strategy(
            strategy_id=strategy_id,
            user_id=resolved_user_id,
            exec_config=exec_config if isinstance(exec_config, dict) else {},
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
        # T-P3-01：状态回写接线（此前 _schedule_status_writeback 已建但全仓无调用方）
        _schedule_status_writeback(
            strategy_id=strategy_id,
            user_id=resolved_user_id,
            lifecycle_status="SIM" if mode == "SIMULATION" else "LIVE",
        )
        # T-P3-05 LIVE 门槛：SIM 首启时间戳（NX 幂等，保留最早；LIVE 晋级需 ≥20 交易日）
        if mode == "SIMULATION" and strategy_id:
            from backend.shared.backtest_health import stamp_sim_start

            stamp_sim_start(redis, resolved_tenant_id, resolved_user_id, strategy_id)

        # 5. 首次启动 Bootstrap：不限时、按最新价、用真实推理立即跑一遍
        # 目的：让用户启动后立刻看到策略在真实运行（等价于手动任务），后续再按
        # 调度计划执行。仅时间门限放开，其余（模型/推理/账户/行情）全部走真实链路；
        # 无可用推理时跳过（不阻断启动），等下一轮推理就绪后由托管调度器自然补跑。
        bootstrap_result = None
        bootstrap_skipped_reason = None
        if mode == "SIMULATION" and trading_permission != "blocked":
            if os.getenv("SIM_BOOTSTRAP_FIRST_RUN_ENABLED", "true").strip().lower() == "true":
                try:
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
                        try:
                            bootstrap_result = await run_simulation_cycle_for_active(
                                tenant_id=resolved_tenant_id,
                                user_id=resolved_user_id,
                                strategy_id=strategy_id or strategy_name,
                                live_trade_config=live_config,
                                run_id=bootstrap_task_id,
                            )
                            if bootstrap_result.get("status") == "failed":
                                bootstrap_skipped_reason = str(
                                    bootstrap_result.get("error") or "simulation cycle failed"
                                )[:300]
                                # 失败不占 24h 锁，否则重置后再启动仍被跳过
                                try:
                                    redis.client.delete(bootstrap_lock_key)
                                except Exception:
                                    pass
                            logger.info(
                                "[SimBootstrap] 首次启动已走 SimulationEngine tenant=%s user=%s strategy=%s task=%s status=%s filled=%s",
                                resolved_tenant_id, resolved_user_id, strategy_id or strategy_name,
                                bootstrap_task_id, (bootstrap_result or {}).get("status"),
                                (bootstrap_result or {}).get("filled_count"),
                            )
                        except Exception:
                            try:
                                redis.client.delete(bootstrap_lock_key)
                            except Exception:
                                pass
                            raise
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
            # T-RC-15/16：市场口径与配置版本回显——前端据此判定页签闸门、
            # 显示「当前生效版本」并作为 /runtime-config 的乐观并发基线。
            "market": deployment_market,
            "strategy_market": declared_market,
            "config_version": 1,
            "execution_config_sync": execution_config_sync,
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
    reason: Optional[str] = Form(None),
    auth: AuthContext = Depends(get_auth_context),
    redis: RedisClient = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
):
    try:
        resolved_user_id, resolved_tenant_id = _normalize_identity(
            auth, user_id=user_id, tenant_id=tenant_id
        )

        active_strat_raw = _read_active_strategy_raw(
            redis, resolved_tenant_id, resolved_user_id
        )
        # T-RC-19：停止原因随请求落审计。之前 /stop 不留原因，事后只能看到
        # 「某时刻停了」，分不清是人工、换策略还是风控告警——而这三者的处置
        # 完全不同（换策略要接着启新的，风控要复盘）。未传则如实记「未填写」。
        stop_reason = str(reason or "").strip() or "unspecified"
        result = {"status": "success", "message": "Stopped", "reason": stop_reason}
        stopped_strategy_id = None

        if active_strat_raw:
            data = json.loads(active_strat_raw)
            strat_id = data.get("strategy_id", "unknown")
            stopped_strategy_id = strat_id
            from backend.services.trade.sandbox.manager import sandbox_manager

            sandbox_manager.stop_strategy(
                resolved_tenant_id, resolved_user_id, strat_id
            )
            logger.info(
                "[Sim] 用户 %s 停止了沙箱模拟盘（原因=%s）", resolved_user_id, stop_reason
            )

        # 停止原因写入运行日志流：用户在界面上看到的「为什么停了」与审计同源
        try:
            from backend.services.live_trading.services.runtime_log_stream import (
                runtime_log_stream,
            )

            runtime_log_stream.log(
                tenant_id=resolved_tenant_id,
                user_id=resolved_user_id,
                line=f"策略已停止（原因：{stop_reason}）",
                level="warning",
                stage="stop",
                summary={"reason": stop_reason, "strategy_id": stopped_strategy_id},
            )
        except Exception as exc:  # noqa: BLE001 - 日志失败不得阻断停止
            logger.debug("stop runtime log failed: %s", exc)

        # Clear active strategy in Redis（含管理员历史别名）
        _delete_active_strategy_aliases(redis, resolved_tenant_id, resolved_user_id)
        # T-P3-01：状态回写——停止后回到 VERIFIED（SIM/LIVE → VERIFIED 为合法迁移）
        if stopped_strategy_id:
            _schedule_status_writeback(
                strategy_id=stopped_strategy_id,
                user_id=resolved_user_id,
                lifecycle_status="VERIFIED",
            )
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
            content=f"当前实盘/模拟策略已停止运行（原因：{stop_reason}）",
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
    market: Optional[str] = None,
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
    active_strat_raw = _read_active_strategy_raw(
        redis, resolved_tenant_id, resolved_user_id
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
    # 必须显式初始化，不能只在 `if active_strat_raw:` 里绑定：没有活跃策略时后者不执行，
    # 后面 `isinstance(active_data, dict)` 会抛 UnboundLocalError → /status 500，
    # 而前端会带着缺省值照常渲染，表现为「界面像在运行、心跳/版本全空」的静默故障。
    active_data: dict = {}
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

    # T-RC-15：市场/配置回显块（三个分支共用）。策略详情单独取一次——
    # strategy_market 要的是策略自身声明的市场，与运行快照的市场是两个概念。
    status_strategy: dict | None = None
    if isinstance(active_strat_id, str) and active_strat_id.isdigit():
        try:
            status_strategy = await get_strategy_storage_service().get(
                strategy_id=int(active_strat_id), user_id=resolved_user_id
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("status strategy lookup failed: %s", exc)
    market_block = await _build_market_and_config_block(
        active_data=active_data if isinstance(active_data, dict) else {},
        strategy=status_strategy,
        declared_market=market,
        tenant_id=resolved_tenant_id,
        user_id=resolved_user_id,
    )

    latest_signal_run_id, signal_source_status = await _build_signal_source_status(
        redis.client,
        resolved_tenant_id,
        resolved_user_id,
    )
    latest_hosted_task = await manual_execution_service.get_latest_hosted_task(
        tenant_id=resolved_tenant_id,
        user_id=resolved_user_id,
        active_runtime_id=active_data.get("run_id")
        if isinstance(active_data, dict)
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
                **market_block,
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
            **market_block,
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
        **market_block,
    }


@router.get("/risk-status")
async def get_risk_status(
    user_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    trade_date: Optional[str] = None,
    auth: AuthContext = Depends(get_auth_context),
    redis: RedisClient = Depends(get_redis),
):
    """风控一屏（T-RC-22）：现在的止损到底是多少、有没有被锁。

    「生效止损」此前散在三处——运行快照、策略参数、界面回显——用户无法自证哪个
    是真的。本端点把三者并排给出并标注 ``source``，同时回传当日风险锁。

    只读，无副作用；Redis 读失败时 ``locks.available=false``（**不谎报「无锁」**：
    「读不到」与「确实没锁」对交易者的含义完全不同）。
    """
    resolved_user_id, resolved_tenant_id = _normalize_identity(
        auth, user_id=user_id, tenant_id=tenant_id
    )

    raw = _read_active_strategy_raw(redis, resolved_tenant_id, resolved_user_id)
    try:
        active_data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        active_data = {}
    if not isinstance(active_data, dict):
        active_data = {}
    active_exec = active_data.get("execution_config")
    active_exec = active_exec if isinstance(active_exec, dict) else {}

    strategy: dict | None = None
    strategy_id = str(active_data.get("strategy_id") or "").strip()
    if strategy_id:
        try:
            from backend.shared.strategy_storage import get_strategy_storage_service

            strategy = await get_strategy_storage_service().get(
                strategy_id, user_id=resolved_user_id
            )
        except Exception as exc:  # noqa: BLE001 - 诊断接口不得因取策略失败而 500
            logger.debug("risk-status strategy load failed: %s", exc)

    # 风险锁：与下单时同一判定源（risk_lock 模块），不重算
    locks_block: dict = {
        "available": False,
        "account_frozen": False,
        "symbols": [],
        "reason": None,
    }
    try:
        from datetime import date as _date

        from backend.services.live_trading.services.risk_lock import load_risk_locks

        # 锁按**交易日**维度写（TTL 到当日收盘 +4h），读的时候必须用同一个维度，
        # 否则会读到空的当日键然后报「无锁」——比不报更危险。
        text = str(trade_date or "").strip()
        day = _date.fromisoformat(text) if text else datetime.now(timezone.utc).date()
        locks = load_risk_locks(redis, resolved_tenant_id, resolved_user_id, day)
        locks_block = {
            "available": True,
            "trade_date": day.isoformat(),
            "account_frozen": bool(locks.account_frozen),
            "symbols": sorted(locks.symbols),
            "reason": None,
        }
    except Exception as exc:  # noqa: BLE001
        locks_block["reason"] = f"风险锁读取失败：{exc}"

    params = (strategy or {}).get("parameters")
    return {
        "status": "success",
        "user_id": resolved_user_id,
        # 生效值：运行快照（托管链路每周期重读的就是它）
        "effective_execution_config": active_exec,
        "source": "runtime_snapshot",
        "strategy_id": strategy_id or None,
        "strategy_execution_config": (
            params.get("execution_config") if isinstance(params, dict) else None
        ),
        "execution_config_divergence": _execution_config_divergence(
            active_data=active_data, strategy=strategy
        ),
        "locks": locks_block,
        "running": bool(active_data),
    }


@router.get("/logs")
async def get_logs(
    tail: int = 100,
    after_id: str = "0-0",
    limit: int = 200,
    level: Optional[str] = None,
    stage: Optional[str] = None,
    source: Optional[str] = None,
    user_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    auth: AuthContext = Depends(get_auth_context),
):
    """运行日志（T-RC-14）：策略「现在在做什么、为什么没做」。

    与 ``/manual-executions/{task_id}/logs`` 的分工：这里是**运行维度**
    （键为 tenant+user，纯模拟托管链路没有任务行也能读），那里是**单次任务**的
    逐单明细。两者互不替代。

    不加 ``@redis_cache``——日志绝不能缓存（``/status`` 的 5s 缓存对它是灾难）。
    """
    resolved_user_id, resolved_tenant_id = _normalize_identity(
        auth, user_id=user_id, tenant_id=tenant_id
    )
    from backend.services.live_trading.services.runtime_log_stream import (
        runtime_log_stream,
    )

    capped = max(1, min(int(limit or 200), 500))
    data = runtime_log_stream.fetch_scope_entries(
        tenant_id=resolved_tenant_id,
        user_id=resolved_user_id,
        after_id=after_id or "0-0",
        limit=capped,
        level=level,
        stage=stage,
        source=source,
    )
    entries = data.get("entries") or []
    tail_count = max(1, min(int(tail or 100), 500))
    # logs 必须是 **string**：既有前端按 RealTradingLogs.logs: string 渲染
    # （<pre>{logs}</pre>），此处返回 list 会渲染成空白——这正是此前"看不到日志"的一半原因。
    text_tail = "\n".join(
        f"[{entry.get('level') or 'info'}] {entry.get('ts') or ''} {entry.get('line') or ''}"
        for entry in entries[-tail_count:]
    )
    return {
        "tenant_id": resolved_tenant_id,
        "user_id": resolved_user_id,
        "logs": text_tail,
        "entries": entries,
        "next_id": data.get("next_id") or (after_id or "0-0"),
        "snapshot": data.get("snapshot"),
        "message": "" if entries else "暂无运行日志：策略尚未产生任何周期记录",
    }


# 影响「何时调仓」的字段。交易日中途改这些会让当天已经跑过的一轮与新的节奏
# 叠加（要么重复建单、要么当天直接不再触发），故同日二次触发需显式 force。
_RHYTHM_KEYS = (
    "rebalance_days",
    "schedule_type",
    "trade_weekdays",
    "enabled_sessions",
    "sell_time",
    "buy_time",
    "sell_first",
    "trigger_window_seconds",
)

#: 热更新**不得**触碰的快照键：它们刻画「这一轮运行的身份与代码」，
#: 改了等于换了一个运行实例（沙箱 code_str 重建 = 重启，run_id 变 = 账本断链）。
_RUNTIME_IDENTITY_KEYS = (
    "run_id",
    "started_at",
    "code_str",
    "code_sha",
    "code_overrides",
    "strategy_id",
    "strategy_name",
    "launch_result",
    "runtime_tenant_id",
    "runtime_user_id",
)


def _runtime_config_diff(before: dict, after: dict, keys: tuple[str, ...]) -> dict:
    """逐 key 比较（仅列变化项），用于响应与 config_history 留痕。"""
    diff: dict = {}
    for key in keys:
        old = before.get(key)
        new = after.get(key)
        if old != new:
            diff[key] = {"from": old, "to": new}
    return diff


def _today_fired_phases(*, redis, tenant_id: str, user_id: str, strategy_id: str, market: str) -> list[str]:
    """今日已触发过的阶段（BUY/SELL/ALL 幂等锁仍在 → 该阶段已跑过）。

    锁键见 ``simulation_hosted_scheduler._lock_key``，TTL 36h、按 phase 区分，
    因此无需扫描 KEYS，逐个精确探测即可。
    """
    try:
        from backend.shared.market_sessions import market_timezone as _tz
    except Exception:  # noqa: BLE001 - 兜底不判定
        return []
    try:
        trade_date = datetime.now(_tz(market)).date().isoformat()
    except Exception:  # noqa: BLE001
        return []
    fired: list[str] = []
    for phase in ("BUY", "SELL", "ALL"):
        key = f"qm:hosted:simulation:{tenant_id}:{user_id}:{strategy_id}:{trade_date}:{phase}"
        try:
            if redis.client.exists(key):
                fired.append(phase)
        except Exception:  # noqa: BLE001
            continue
    return fired


@router.post("/runtime-config")
async def update_runtime_config(
    execution_config: Optional[str] = Form(None),
    live_trade_config: Optional[str] = Form(None),
    expected_config_version: Optional[int] = Form(None),
    user_id: Optional[str] = Form(None),
    tenant_id: Optional[str] = Form(None),
    dry_run: bool = Form(False),
    force: bool = Form(False),
    operator: Optional[str] = Form(None),
    change_reason: Optional[str] = Form(None),
    auth: AuthContext = Depends(get_auth_context),
    redis: RedisClient = Depends(get_redis),
):
    """盘中热更新：改「怎么调仓、怎么风控」，不动持仓、不重启（T-RC-16）。

    生效时机有代码证据（不是承诺）：
    - 调仓节奏：``SimulationHostedScheduler`` **每周期重读**快照（30s 一次）；
    - 退出规则：``SimulationEngine._load_exit_ruleset`` **每周期**从库里读策略参数。
    二者都是「下一周期生效」，故 ``effective_at="next_cycle"``。

    三条硬边界（不满足直接拒绝，绝不「尽力而为」地改一半）：
    1. **REAL/SHADOW 拒绝热更新**：容器 runner 的交易参数是容器 env
       （``k8s_manager``），改配置必须重建容器 → 409 + ``requires_restart=true``。
       谎称零中断比不支持更糟。
    2. **不碰运行身份**：``run_id``/``started_at``/``code_str``/``code_sha`` 原样保留。
       改策略**代码**要另走 ``PUT /user-strategies/{id}``（且沙箱需重启），此端点只管配置。
    3. **同日二次触发需 force**：当天已按旧节奏跑过一轮，再改节奏字段会叠加。
    """
    resolved_user_id, resolved_tenant_id = _normalize_identity(
        auth, user_id=user_id, tenant_id=tenant_id
    )
    key = _active_strategy_key(resolved_tenant_id, resolved_user_id)
    # 注意：函数签名里的 `redis` 是 RedisClient 依赖，遮蔽了同名模块，
    # 故异常类必须显式导入（写 `redis.exceptions.WatchError` 会 AttributeError）。
    from redis.exceptions import WatchError as _WatchError

    if execution_config is None and live_trade_config is None:
        raise HTTPException(
            status_code=400,
            detail="execution_config 与 live_trade_config 至少提供一个",
        )

    patch_exec: dict = {}
    patch_live: dict = {}
    if execution_config is not None:
        try:
            parsed = json.loads(execution_config)
        except Exception:
            raise HTTPException(status_code=400, detail="execution_config 不是合法 JSON")
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=400, detail="execution_config 必须是对象")
        patch_exec = parsed
    if live_trade_config is not None:
        try:
            parsed_live = json.loads(live_trade_config)
        except Exception:
            raise HTTPException(status_code=400, detail="live_trade_config 不是合法 JSON")
        if not isinstance(parsed_live, dict):
            raise HTTPException(status_code=400, detail="live_trade_config 必须是对象")
        patch_live = parsed_live
    # 身份键由服务端持有，客户端提交一律丢弃（防伪造 run_id 断账本）
    for identity_key in _RUNTIME_IDENTITY_KEYS:
        patch_exec.pop(identity_key, None)
        patch_live.pop(identity_key, None)

    client = redis.client
    if client is None:
        raise HTTPException(status_code=503, detail="Redis 不可用，无法热更新")

    pipe = client.pipeline()
    try:
        pipe.watch(key)
        raw = pipe.get(key)
        if not raw:
            pipe.unwatch()
            raise HTTPException(
                status_code=409,
                detail="当前没有运行中的策略，无法热更新；请先在控制台启动策略",
            )
        try:
            snapshot = json.loads(raw)
        except Exception:
            pipe.unwatch()
            raise HTTPException(status_code=500, detail="活跃策略快照损坏，无法解析")
        if not isinstance(snapshot, dict):
            pipe.unwatch()
            raise HTTPException(status_code=500, detail="活跃策略快照格式异常")

        run_mode = str(snapshot.get("mode") or "").upper()
        if run_mode and run_mode != "SIMULATION":
            pipe.unwatch()
            raise HTTPException(
                status_code=409,
                detail=(
                    f"当前为 {run_mode} 模式：交易参数在运行容器内以环境变量固化，"
                    "热更新不生效，需重建运行容器（持仓不受影响）"
                ),
                headers={"X-Requires-Restart": "true"},
            )

        current_version = int(snapshot.get("config_version") or 0)
        if expected_config_version is not None and int(expected_config_version) != current_version:
            pipe.unwatch()
            raise HTTPException(
                status_code=409,
                detail=(
                    f"配置版本已变更（当前 {current_version}，提交基线 {expected_config_version}），"
                    "请刷新后重试"
                ),
            )

        old_exec = snapshot.get("execution_config") if isinstance(snapshot.get("execution_config"), dict) else {}
        old_live = snapshot.get("live_trade_config") if isinstance(snapshot.get("live_trade_config"), dict) else {}
        new_exec = _normalize_execution_config({}, {**old_exec, **patch_exec}) if patch_exec else dict(old_exec)
        new_live = (
            _normalize_live_trade_config({}, {**old_live, **patch_live}, allow_after_hours=True)
            if patch_live
            else dict(old_live)
        )
        # 校验与 /start 同源（不新写一套），非法值在这里就被拒绝。
        # 包成 400 而非 500：越界是用户输入问题，不是服务端故障。
        try:
            ExecutionConfigSchema.model_validate(new_exec)
        except Exception as schema_exc:  # noqa: BLE001 - pydantic ValidationError
            pipe.unwatch()
            raise HTTPException(
                status_code=400, detail=f"execution_config 校验失败：{schema_exc}"
            )

        exec_diff = _runtime_config_diff(old_exec, new_exec, tuple(sorted(set(old_exec) | set(new_exec))))
        live_diff = _runtime_config_diff(old_live, new_live, tuple(sorted(set(old_live) | set(new_live))))
        rhythm_diff = {
            k: v for k, v in live_diff.items() if k in _RHYTHM_KEYS
        }

        strategy_id_for_lock = str(snapshot.get("strategy_id") or "").strip()
        fired_phases: list[str] = []
        if rhythm_diff and strategy_id_for_lock:
            market = str((new_live or {}).get("market") or (new_exec or {}).get("market") or "CN").upper()
            fired_phases = _today_fired_phases(
                redis=redis,
                tenant_id=resolved_tenant_id,
                user_id=resolved_user_id,
                strategy_id=strategy_id_for_lock,
                market=market,
            )
            # 预演（dry_run）不拦：它不写任何东西，而「今天已经跑过一轮」恰恰是
            # 操作者做决定前最需要看到的信息（响应里已带 already_fired_phases）。
            if fired_phases and not force and not dry_run:
                pipe.unwatch()
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"今日已按原节奏触发过 {'/'.join(fired_phases)} 阶段，此时修改调仓节奏"
                        "可能与已执行的一轮叠加。确认要改请带 force=true 重试。"
                    ),
                )

        if dry_run:
            pipe.unwatch()
            return {
                "status": "dry_run",
                "message": "预演：未写入任何变更",
                "config_version": current_version,
                "effective_execution_config": new_exec,
                "effective_live_trade_config": new_live,
                "diff": {"execution_config": exec_diff, "live_trade_config": live_diff},
                "rhythm_changed": bool(rhythm_diff),
                "already_fired_phases": fired_phases,
                "effective_at": "next_cycle",
            }

        next_version = current_version + 1
        now_iso = datetime.now(timezone.utc).isoformat()
        history = snapshot.get("config_history")
        history = list(history) if isinstance(history, list) else []
        history.append(
            {
                "config_version": next_version,
                "at": now_iso,
                "operator": str(operator or resolved_user_id),
                "reason": str(change_reason or ""),
                "forced": bool(force and fired_phases),
                "already_fired_phases": fired_phases,
                "diff": {"execution_config": exec_diff, "live_trade_config": live_diff},
            }
        )
        # 身份键原样保留（run_id/code_str/...），只替换配置与版本元数据
        new_snapshot = {
            **snapshot,
            "execution_config": new_exec,
            "live_trade_config": new_live,
            "config_version": next_version,
            "config_updated_at": now_iso,
            "config_history": history[-50:],
        }
        try:
            pipe.multi()
            pipe.set(key, json.dumps(new_snapshot))
            pipe.execute()
        except _WatchError:
            raise HTTPException(
                status_code=409,
                detail="配置在提交瞬间被其他会话改动（调度器或另一处编辑），请刷新后重试",
            )
    finally:
        try:
            pipe.reset()
        except Exception:  # noqa: BLE001
            pass

    # 风控口径同步进策略参数（D9：快照止损与退出规则必须同源）。失败不阻断，
    # 但如实回传，让前端能提示「生效值已变、策略参数未写回」。
    exec_sync = await _sync_execution_config_to_strategy(
        strategy_id=strategy_id_for_lock,
        user_id=resolved_user_id,
        exec_config=new_exec,
    )

    try:
        from backend.services.live_trading.services.runtime_log_stream import (
            SOURCE_SYSTEM,
            runtime_log_stream,
        )

        changed = ", ".join(
            sorted(set(exec_diff) | set(live_diff))
        ) or "（无字段变化）"
        runtime_log_stream.log(
            tenant_id=resolved_tenant_id,
            user_id=resolved_user_id,
            line=(
                f"配置热更新 → v{next_version}（{changed}）"
                + (f"，原因：{change_reason}" if change_reason else "")
                + (f"，今日已触发 {'/'.join(fired_phases)} 但强制提交" if force and fired_phases else "")
                + "；下一周期生效，持仓不变"
            ),
            level="warning" if (force and fired_phases) else "info",
            source=SOURCE_SYSTEM,
            stage="config_update",
            status="applied",
            strategy_id=strategy_id_for_lock,
        )
    except Exception as exc:  # noqa: BLE001 - 日志失败不影响已生效的配置
        logger.debug("runtime config log failed: %s", exc)

    return {
        "status": "success",
        "message": f"配置已更新至 v{next_version}，将于下一周期生效",
        "config_version": next_version,
        "previous_config_version": current_version,
        "effective_at": "next_cycle",
        "effective_execution_config": new_exec,
        "effective_live_trade_config": new_live,
        "diff": {"execution_config": exec_diff, "live_trade_config": live_diff},
        "rhythm_changed": bool(rhythm_diff),
        "already_fired_phases": fired_phases,
        "forced": bool(force and fired_phases),
        "execution_config_sync": exec_sync,
        "position_untouched": True,
    }


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
