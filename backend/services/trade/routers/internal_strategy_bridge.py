from fastapi import APIRouter, Depends, Header, HTTPException, Request
from .internal_strategy_utils import *
from .internal_strategy_utils import (
    _bridge_ws_url,
    _agent_template_root,
    _qmt_agent_release_manifest_key,
    _qmt_agent_release_asset_ttl,
    _qmt_agent_release_local_manifest_path,
    _load_qmt_agent_release_manifest,
    _build_qmt_agent_release_asset,
    _iso_or_none,
    _to_float,
    _compute_account_metrics,
    _get_bridge_session_context,
)
from .real_trading_utils import _fetch_latest_real_account_snapshot
from backend.shared.trade_account_cache import (
    write_trade_account_cache,
    write_trade_agent_heartbeat_cache,
)
from backend.shared.trade_redis_keys import (
    build_trade_account_key,
    build_trade_agent_heartbeat_key,
)

router = APIRouter(tags=["Internal Strategy Gateway"])
logger = logging.getLogger(__name__)


def _build_portfolio_sync_payload_from_snapshot(
    latest_snapshot: Optional[dict[str, Any]],
    *,
    fallback_payload: Optional[QMTBridgeAccountPayload] = None,
    fallback_positions: Optional[list[dict[str, Any]]] = None,
) -> Optional[dict[str, Any]]:
    if not isinstance(latest_snapshot, dict):
        return None

    payload_json = latest_snapshot.get("payload_json") or {}
    positions = latest_snapshot.get("positions")
    if not isinstance(positions, list):
        positions = payload_json.get("positions")
    if not isinstance(positions, list):
        positions = fallback_positions or []

    total_asset = _to_float(latest_snapshot.get("total_asset"), 0.0)
    available_cash = _to_float(
        latest_snapshot.get("available_cash", latest_snapshot.get("cash")),
        0.0,
    )
    market_value = _to_float(latest_snapshot.get("market_value"), 0.0)
    raw_frozen = _to_float(
        payload_json.get(
            "frozen_cash",
            latest_snapshot.get("frozen_cash", latest_snapshot.get("frozen")),
        ),
        0.0,
    )
    derived_frozen = max(0.0, total_asset - market_value - available_cash)
    frozen_cash = max(raw_frozen, derived_frozen)

    liabilities = _to_float(payload_json.get("liabilities"), 0.0)
    short_market_value = _to_float(payload_json.get("short_market_value"), 0.0)
    if fallback_payload is not None:
        if liabilities <= 0.0:
            liabilities = _to_float(getattr(fallback_payload, "liabilities", 0.0), 0.0)
        if short_market_value <= 0.0:
            short_market_value = _to_float(
                getattr(fallback_payload, "short_market_value", 0.0), 0.0
            )

    return {
        "total_asset": total_asset,
        "available_cash": available_cash,
        "frozen_cash": frozen_cash,
        "market_value": market_value,
        "liabilities": liabilities,
        "short_market_value": short_market_value,
        "position_rows": positions,
        "source": "latest_valid_snapshot",
        "snapshot_at": latest_snapshot.get("snapshot_at"),
    }


@router.post("/dispatch/item-status", dependencies=[Depends(verify_internal_call)])
async def update_dispatch_item_status(
    payload: Dict[str, Any],
    x_user_id: str = Header(...),
    x_tenant_id: Optional[str] = Header(None),
    db=Depends(get_db),
):
    """
    由 Runner / 执行桥接回写逐单执行状态。

    必填:
    - client_order_id
    - dispatch_status: pending/dispatched/acked/order_submitted/order_filled/rejected/failed
    """
    client_order_id = str(payload.get("client_order_id") or "").strip()
    dispatch_status = str(payload.get("dispatch_status") or "").strip().lower()
    if not client_order_id:
        raise HTTPException(status_code=400, detail="missing client_order_id")
    allowed = {
        "pending",
        "dispatched",
        "acked",
        "order_submitted",
        "order_filled",
        "rejected",
        "failed",
    }
    if dispatch_status not in allowed:
        raise HTTPException(status_code=400, detail="invalid dispatch_status")

    tenant_id = (x_tenant_id or "").strip() or "default"
    user_id = str(x_user_id).strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="invalid x_user_id")

    order_status_map = {
        "pending": "PENDING",
        "dispatched": "SUBMITTED",
        "acked": "SUBMITTED",
        "order_submitted": "SUBMITTED",
        "order_filled": "FILLED",
        "rejected": "REJECTED",
        "failed": "REJECTED",
    }
    order_status = order_status_map.get(dispatch_status, "SUBMITTED")

    exchange_order_id = str(payload.get("exchange_order_id") or "").strip() or None
    exchange_trade_id = str(payload.get("exchange_trade_id") or "").strip() or None
    exec_message = str(payload.get("exec_message") or "").strip() or None
    order_id = str(payload.get("order_id") or "").strip() or None

    # 1) 回写 engine_dispatch_items（若该表存在）
    dispatch_sql = text("""
        UPDATE engine_dispatch_items
        SET dispatch_status = :dispatch_status,
            order_id = COALESCE(CAST(:order_id AS uuid), order_id),
            exchange_order_id = COALESCE(:exchange_order_id, exchange_order_id),
            exchange_trade_id = COALESCE(:exchange_trade_id, exchange_trade_id),
            exec_message = COALESCE(:exec_message, exec_message),
            updated_at = NOW()
        WHERE tenant_id = :tenant_id
          AND user_id = :user_id
          AND client_order_id = :client_order_id
        """)
    dispatch_updated = 0
    try:
        dispatch_ret = await db.execute(
            dispatch_sql,
            {
                "dispatch_status": dispatch_status,
                "order_id": order_id,
                "exchange_order_id": exchange_order_id,
                "exchange_trade_id": exchange_trade_id,
                "exec_message": exec_message,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "client_order_id": client_order_id,
            },
        )
        dispatch_updated = int(dispatch_ret.rowcount or 0)
    except Exception as e:
        # 兼容老环境（未建 contract 表）: 不阻断订单状态更新
        logger.warning("update engine_dispatch_items skipped: %s", e)

    # 2) 回写 orders 状态
    stmt = select(Order).where(
        and_(
            Order.tenant_id == tenant_id,
            Order.user_id == user_id,
            Order.client_order_id == client_order_id,
        )
    )
    result = await db.execute(stmt)
    order = result.scalar_one_or_none()
    if order is not None:
        order.status = order_status
        if exchange_order_id:
            order.exchange_order_id = exchange_order_id
        if exec_message:
            order.remarks = exec_message

    return {
        "ok": True,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "client_order_id": client_order_id,
        "dispatch_status": dispatch_status,
        "order_status": order_status,
        "dispatch_items_updated": dispatch_updated,
        "order_updated": bool(order is not None),
    }


@router.post("/bridge/session", response_model=QMTBridgeSessionResponse)
async def create_qmt_bridge_session(
    payload: QMTBridgeSessionRequest,
    request: Request,
    db=Depends(get_db),
):
    if str(payload.agent_type or "").strip().lower() != "qmt":
        raise HTTPException(status_code=400, detail="Only qmt agent_type is supported")

    key = await resolve_api_key(db, payload.access_key)
    validation_error = validate_api_key_secret(key, payload.secret_key)
    if (
        validation_error == "secret_key_invalid"
        or validation_error == "access_key_not_found"
    ):
        raise HTTPException(status_code=401, detail="Invalid access_key or secret_key")
    if validation_error in {"access_key_inactive", "access_key_expired"}:
        raise HTTPException(status_code=403, detail=validation_error)

    active_binding = await get_active_binding(db, key.tenant_id, payload.account_id)
    if active_binding and active_binding.api_key_id != key.id:
        raise HTTPException(
            status_code=409, detail="account_id already bound to another active agent"
        )

    try:
        binding, _ = await get_or_create_binding(
            db,
            key=key,
            account_id=payload.account_id,
            client_fingerprint=payload.client_fingerprint,
            hostname=payload.hostname,
            client_version=payload.client_version,
            last_ip=request.client.host if request.client else None,
            force_rebind=payload.force_rebind,
        )
    except ValueError as exc:
        if str(exc) == "binding_conflict":
            raise HTTPException(
                status_code=409,
                detail="binding conflict, please unbind previous device first",
            )
        raise

    session_model, raw_token = await create_bridge_session(db, binding)
    key.last_used_at = utcnow()
    await db.commit()
    return QMTBridgeSessionResponse(
        bridge_session_token=raw_token,
        expires_in=SESSION_TTL_SECONDS,
        ws_url=_bridge_ws_url(),
        tenant_id=binding.tenant_id,
        user_id=binding.user_id,
        permissions=list(key.permissions or []),
        binding={
            "account_id": binding.account_id,
            "agent_type": binding.agent_type,
            "status": binding.status,
            "session_id": session_model.id,
        },
    )


@router.post("/bridge/session/refresh", response_model=QMTBridgeRefreshResponse)
async def refresh_qmt_bridge_session(
    authorization: Optional[str] = Header(None),
    db=Depends(get_db),
):
    header = str(authorization or "").strip()
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bridge session token")
    current_token = header[7:].strip()
    refreshed = await refresh_bridge_session(db, current_token)
    if refreshed is None:
        raise HTTPException(status_code=401, detail="Invalid bridge session token")
    await db.commit()
    _context, new_token = refreshed
    return QMTBridgeRefreshResponse(
        bridge_session_token=new_token,
        expires_in=SESSION_TTL_SECONDS,
    )


class QMTBindingResetRequest(BaseModel):
    access_key: str
    secret_key: str


@router.delete("/bridge/binding", status_code=200)
async def reset_qmt_binding(
    payload: QMTBindingResetRequest,
    db=Depends(get_db),
):
    """
    解除当前 access_key 的设备绑定，使下次 POST /bridge/session 可重新绑定新设备。
    用于解决 409 binding_conflict 冲突（换机器或 client_fingerprint 变化）。
    """
    key = await resolve_api_key(db, payload.access_key)
    validation_error = validate_api_key_secret(key, payload.secret_key)
    if validation_error in {"secret_key_invalid", "access_key_not_found"}:
        raise HTTPException(status_code=401, detail="Invalid access_key or secret_key")
    if validation_error in {"access_key_inactive", "access_key_expired"}:
        raise HTTPException(status_code=403, detail=validation_error)

    reset = await reset_binding(db, key)
    await db.commit()
    return {
        "reset": reset,
        "message": "Binding reset. You can now reconnect from any device.",
    }


@router.get("/bridge/download/agent")
async def download_agent_package(
    user_id: Optional[str] = None,
    auth: AuthContext = Depends(get_auth_context),
    db=Depends(get_db),
):
    requested_user_id = str(user_id or auth.user_id).strip()
    if requested_user_id != auth.user_id:
        raise HTTPException(status_code=403, detail="forbidden bridge user_id")

    result = await db.execute(
        select(ApiKey)
        .where(ApiKey.user_id == auth.user_id, ApiKey.tenant_id == auth.tenant_id)
        .order_by(ApiKey.created_at.desc())
        .limit(1)
    )
    key = result.scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=404, detail="No API key found for current user")

    # Fetch most recent binding to determine correct account_id for the agent
    binding_result = await db.execute(
        select(QMTAgentBinding)
        .where(
            QMTAgentBinding.tenant_id == auth.tenant_id,
            QMTAgentBinding.user_id == auth.user_id,
        )
        .order_by(QMTAgentBinding.created_at.desc())
        .limit(1)
    )
    latest_binding = binding_result.scalar_one_or_none()
    latest_account_id = (
        latest_binding.account_id if latest_binding else str(auth.user_id or "").strip()
    )

    config = {
        "agent_type": "qmt",
        "server_url": _bridge_ws_url(),
        "api_base_url": os.getenv(
            "API_GATEWAY_BASE_URL", "http://localhost:8000/api/v1"
        ),
        "tenant_id": str(auth.tenant_id or "default").strip(),
        "user_id": str(auth.user_id or "").strip(),
        "access_key": str(key.access_key or "").strip(),
        "secret_key": "",
        "account_id": latest_account_id,
        "qmt_path": "",
        "qmt_bin_path": "",
        "client_fingerprint": "",
        "client_version": "0.2.0",
        "session_id": 0,
        "renew_before_seconds": SESSION_REFRESH_THRESHOLD_SECONDS,
        "heartbeat_interval_seconds": 15,
        "account_report_interval_seconds": 30,
        "reconnect_interval_seconds": 5,
        "ws_ping_interval_seconds": 20,
        "ws_ping_timeout_seconds": 10,
    }
    bundle_dir = tempfile.mkdtemp(prefix=f"qmt_agent_bundle_{auth.user_id}_")
    config_file = os.path.join(bundle_dir, "qmt_agent_config.json")
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

    agent_root = Path(__file__).resolve().parents[4] / "tools" / "qmt_agent"
    package_files = [
        "qmt_agent.py",
        "desktop_app.py",
        "__init__.py",
        "requirements.txt",
        "build_windows_agent.py",
        "qmt_agent_desktop.spec",
        "qmt_agent_setup.iss",
        "version.json",
        "README.md",
        "agent.py",
        "auth.py",
        "client.py",
        "config.py",
        "reporter.py",
        "_callback.py",
        "theme.qss",
    ]
    for filename in package_files:
        src = agent_root / filename
        if src.exists():
            shutil.copyfile(src, os.path.join(bundle_dir, filename))

    reference_template = Path(_agent_template_root()) / "qmt_bridge.py"
    if reference_template.exists():
        shutil.copyfile(
            reference_template, os.path.join(bundle_dir, "qmt_agent_reference.py")
        )

    archive_path = os.path.join(bundle_dir, "qmt_agent_client.zip")
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename in (
            *package_files,
            "qmt_agent_config.json",
            "qmt_agent_reference.py",
        ):
            file_path = os.path.join(bundle_dir, filename)
            if os.path.exists(file_path):
                zf.write(file_path, arcname=filename)

    return FileResponse(
        archive_path,
        filename="qmt_agent_client.zip",
        media_type="application/zip",
    )


@router.get(
    "/bridge/download/agent/release", response_model=QMTAgentReleaseDownloadResponse
)
async def get_qmt_agent_release_download(
    asset: str = "installer",
    auth: AuthContext = Depends(get_auth_context),
):
    """
    获取 QMT Agent 发布版安装包的下载信息。

    该接口从 COS 的 latest.json 读取版本信息，再返回安装器/便携包的预签名下载地址。
    """
    requested_asset = str(asset or "installer").strip().lower()
    if requested_asset not in {"installer", "portable"}:
        raise HTTPException(
            status_code=400, detail="asset must be installer or portable"
        )

    manifest, _ = _load_qmt_agent_release_manifest()
    product = (
        str(manifest.get("product") or "QuantMindQMTAgent").strip()
        or "QuantMindQMTAgent"
    )
    channel = str(manifest.get("channel") or "release").strip() or "release"
    version = str(manifest.get("version") or "").strip()
    if not version:
        raise HTTPException(
            status_code=503, detail="QMT Agent release manifest missing version"
        )

    build_time = str(manifest.get("build_time") or "").strip() or None
    manifest_key = str(
        manifest.get("manifest_key") or _qmt_agent_release_manifest_key()
    ).strip()

    installer_info = None
    portable_info = None
    installer_payload = manifest.get("installer")
    portable_payload = manifest.get("portable")
    if isinstance(installer_payload, dict):
        installer_info = _build_qmt_agent_release_asset("installer", installer_payload)
    if isinstance(portable_payload, dict):
        portable_info = _build_qmt_agent_release_asset("portable", portable_payload)

    selected = installer_info if requested_asset == "installer" else portable_info
    if selected is None:
        raise HTTPException(
            status_code=404, detail=f"QMT Agent {requested_asset} package not found"
        )

    manifest_url = None
    cos = get_cos_service()
    if cos.client and cos.bucket_name:
        manifest_url = cos.get_presigned_url(
            manifest_key, expired=_qmt_agent_release_asset_ttl()
        )
        if manifest_url:
            manifest_url = str(manifest_url).strip()

    return QMTAgentReleaseDownloadResponse(
        product=product,
        channel=channel,
        version=version,
        build_time=build_time,
        manifest_key=manifest_key,
        manifest_url=manifest_url,
        selected_asset=requested_asset,
        installer=installer_info,
        portable=portable_info,
    )


@router.get("/bridge/binding/status", response_model=QMTBindingStatusResponse)
async def get_qmt_binding_status(
    user_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    auth: AuthContext = Depends(get_auth_context),
    redis: RedisClient = Depends(get_redis),
    db=Depends(get_db),
):
    resolved_user_id = str(user_id or auth.user_id).strip()
    resolved_tenant_id = str(tenant_id or auth.tenant_id or "default").strip()
    if resolved_user_id != auth.user_id:
        raise HTTPException(status_code=403, detail="forbidden bridge user_id")

    result = await db.execute(
        select(ApiKey)
        .where(
            ApiKey.user_id == resolved_user_id, ApiKey.tenant_id == resolved_tenant_id
        )
        .order_by(ApiKey.created_at.desc())
        .limit(1)
    )
    key = result.scalar_one_or_none()
    key_id = key.id if key is not None else None

    binding = None
    if key_id is not None:
        binding_result = await db.execute(
            select(QMTAgentBinding)
            .where(QMTAgentBinding.api_key_id == key_id)
            .order_by(QMTAgentBinding.updated_at.desc())
            .limit(1)
        )
        binding = binding_result.scalar_one_or_none()

    try:
        heartbeat_key = build_trade_agent_heartbeat_key(
            resolved_tenant_id, resolved_user_id
        )
        account_key = build_trade_account_key(resolved_tenant_id, resolved_user_id)

        # 使用 redis_client 包装好的 get 方法，自带错误处理和 JSON 解析
        heartbeat = redis.get(heartbeat_key) or {}
        account = redis.get(account_key) or {}

        heartbeat_at = (
            _iso_or_none(
                datetime.fromtimestamp(
                    float(heartbeat.get("timestamp")), tz=timezone.utc
                )
            )
            if heartbeat.get("timestamp")
            else None
        )

        account_reported_at = (
            _iso_or_none(
                datetime.fromtimestamp(float(account.get("timestamp")), tz=timezone.utc)
            )
            if account.get("timestamp")
            else None
        )
        now_ts = time.time()
        heartbeat_ok = bool(
            heartbeat.get("timestamp") and now_ts - float(heartbeat["timestamp"]) <= 60
        )
        account_ok = bool(
            account.get("timestamp") and now_ts - float(account["timestamp"]) <= 120
        )
        online = heartbeat_ok and account_ok
        stale_reason = None
        if not heartbeat_ok:
            stale_reason = "heartbeat_stale"
        elif not account_ok:
            stale_reason = "account_snapshot_stale"

        return QMTBindingStatusResponse(
            online=online,
            user_id=resolved_user_id,
            tenant_id=resolved_tenant_id,
            account_id=getattr(binding, "account_id", None),
            hostname=getattr(binding, "hostname", None),
            client_version=getattr(binding, "client_version", None),
            last_seen_at=_iso_or_none(getattr(binding, "last_seen_at", None)),
            heartbeat_at=heartbeat_at,
            account_reported_at=account_reported_at,
            stale_reason=stale_reason,
        )
    except Exception as e:
        logger.error(
            f"Error fetching binding status for user {resolved_user_id}: {e}",
            exc_info=True,
        )
        # 即使报错也返回一个基本结构而不是 500
        return QMTBindingStatusResponse(
            online=False,
            user_id=resolved_user_id,
            tenant_id=resolved_tenant_id,
            stale_reason=f"error: {str(e)}",
        )


async def _sync_qmt_account_to_db(
    db,
    tenant_id: str,
    user_id: int,
    total_asset: float,
    available_cash: float,
    frozen_cash: float,
    market_value: float,
    liabilities: float,
    short_market_value: float,
    position_rows: list[dict],
) -> None:
    """将 QMT 账户快照同步到 PostgreSQL portfolios 和 positions 表。"""
    from decimal import Decimal

    # 1. 查找该用户的 REAL 模式活跃 portfolio
    portfolio_result = await db.execute(
        select(Portfolio)
        .where(
            and_(
                Portfolio.tenant_id == tenant_id,
                Portfolio.user_id == user_id,
                Portfolio.trading_mode == "REAL",
                Portfolio.status == "active",
                Portfolio.is_deleted == False,
            )
        )
        .order_by(Portfolio.updated_at.desc())
        .limit(1)
    )
    portfolio = portfolio_result.scalar_one_or_none()
    if portfolio is None:
        logger.debug(
            "[BridgeAccount] no active REAL portfolio for user=%s, skip DB sync",
            user_id,
        )
        return

    # 2. 更新 portfolio 资金数据
    portfolio.total_value = Decimal(str(total_asset))
    portfolio.available_cash = Decimal(str(available_cash))
    portfolio.current_capital = Decimal(str(available_cash))
    portfolio.frozen_cash = Decimal(str(frozen_cash))
    portfolio.liabilities = Decimal(str(liabilities))
    portfolio.short_market_value = Decimal(str(short_market_value))
    portfolio.updated_at = datetime.now()
    db.add(portfolio)

    # 3. upsert positions：先拉出该 portfolio 所有持仓
    existing_result = await db.execute(
        select(Position).where(Position.portfolio_id == portfolio.id)
    )
    existing_positions: dict[str, Position] = {
        pos.symbol: pos for pos in existing_result.scalars().all()
    }

    reported_symbols: set[str] = set()
    for row in position_rows:
        symbol = str(row.get("symbol") or "").strip()
        if not symbol:
            continue
        volume = int(row.get("volume") or 0)
        available_volume = int(row.get("available_volume") or 0)
        cost_price = float(row.get("cost_price") or 0.0)
        last_price = float(row.get("last_price") or 0.0)
        mkt_value = float(row.get("market_value") or volume * last_price)
        total_cost = volume * cost_price
        unrealized_pnl = mkt_value - total_cost
        unrealized_pnl_rate = (unrealized_pnl / total_cost) if total_cost > 0 else 0.0
        reported_symbols.add(symbol)

        if symbol in existing_positions:
            pos = existing_positions[symbol]
            pos.quantity = volume
            pos.available_quantity = available_volume
            pos.frozen_quantity = max(0, volume - available_volume)
            pos.avg_cost = Decimal(str(cost_price))
            pos.total_cost = Decimal(str(total_cost))
            pos.current_price = Decimal(str(last_price))
            pos.market_value = Decimal(str(mkt_value))
            pos.unrealized_pnl = Decimal(str(unrealized_pnl))
            pos.unrealized_pnl_rate = Decimal(str(round(unrealized_pnl_rate, 6)))
            pos.symbol_name = row.get("symbol_name") or pos.symbol_name
            pos.status = "holding" if volume > 0 else "closed"
            pos.updated_at = datetime.now()
        else:
            new_pos = Position(
                portfolio_id=portfolio.id,
                symbol=symbol,
                symbol_name=row.get("symbol_name") or symbol,
                quantity=volume,
                available_quantity=available_volume,
                frozen_quantity=max(0, volume - available_volume),
                avg_cost=Decimal(str(cost_price)),
                total_cost=Decimal(str(total_cost)),
                current_price=Decimal(str(last_price)),
                market_value=Decimal(str(mkt_value)),
                unrealized_pnl=Decimal(str(unrealized_pnl)),
                unrealized_pnl_rate=Decimal(str(round(unrealized_pnl_rate, 6))),
                status="holding" if volume > 0 else "closed",
            )
            db.add(new_pos)

    # 4. 将 QMT 已不持有的仓位标记为 closed
    for symbol, pos in existing_positions.items():
        if symbol not in reported_symbols and pos.status == "holding":
            pos.status = "closed"
            pos.quantity = 0
            pos.available_quantity = 0
            pos.market_value = Decimal("0")
            pos.unrealized_pnl = Decimal("0")
            pos.closed_at = datetime.now()
            pos.updated_at = datetime.now()

    logger.info(
        "[BridgeAccount] synced portfolio=%s user=%s: total_asset=%.2f, positions=%d",
        portfolio.id,
        user_id,
        total_asset,
        len(reported_symbols),
    )


@router.post("/bridge/account")
async def upsert_qmt_account_snapshot(
    payload: QMTBridgeAccountPayload,
    ctx: BridgeSessionContext = Depends(_get_bridge_session_context),
    redis: RedisClient = Depends(get_redis),
    db=Depends(get_db),
):
    if payload.account_id != ctx.account_id:
        raise HTTPException(status_code=403, detail="account_id mismatch")
    timestamp = (payload.reported_at or utcnow()).timestamp()
    snapshot_at = payload.reported_at or utcnow()

    position_rows = []
    for item in payload.positions:
        row = item.model_dump()
        if not row.get("symbol_name"):
            row["symbol_name"] = lookup_symbol_name(item.symbol)
        position_rows.append(row)
    metrics, metrics_meta = await _compute_account_metrics(
        db=db,
        tenant_id=ctx.tenant_id,
        user_id=ctx.user_id,
        account_id=ctx.account_id,
        total_asset=_to_float(payload.total_asset, 0.0),
        cash=_to_float(payload.cash, 0.0),
        market_value=_to_float(payload.market_value, 0.0),
        snapshot_at=snapshot_at,
        payload_today_pnl=_to_float(payload.today_pnl, 0.0),
        payload_total_pnl=_to_float(payload.total_pnl, 0.0),
        payload_floating_pnl=_to_float(payload.floating_pnl, 0.0),
        positions=position_rows,
        payload_json=payload.model_dump(),
    )
    account_info = {
        "user_id": ctx.user_id,
        "tenant_id": ctx.tenant_id,
        "account_id": ctx.account_id,
        "broker": payload.broker,
        "cash": payload.cash,
        "available_cash": payload.available_cash,
        "frozen_cash": payload.frozen_cash,
        "yesterday_balance": payload.yesterday_balance,
        "short_proceeds": payload.short_proceeds,
        "liabilities": payload.liabilities,
        "short_market_value": payload.short_market_value,
        "credit_limit": payload.credit_limit,
        "maintenance_margin_ratio": payload.maintenance_margin_ratio,
        "credit_enabled": payload.credit_enabled,
        "shortable_symbols_count": payload.shortable_symbols_count,
        "last_short_check_at": payload.last_short_check_at,
        "total_asset": payload.total_asset,
        "market_value": payload.market_value,
        "today_pnl": metrics["today_pnl"],
        "total_pnl": metrics["total_pnl"],
        "floating_pnl": metrics["floating_pnl"],
        "monthly_pnl": metrics["monthly_pnl"],
        "total_return": metrics["total_return"],
        "win_rate": metrics["win_rate"],
        "metrics": metrics,
        "metrics_meta": metrics_meta,
        "positions": position_rows,
        "compacts": payload.compacts,
        "credit_subjects": payload.credit_subjects,
        "debug_version": payload.debug_version,
        "timestamp": timestamp,
        "reported_at": _iso_or_none(payload.reported_at),
        "is_online": True,
    }
    portfolio_sync_payload = {
        "total_asset": _to_float(payload.total_asset, 0.0),
        "available_cash": _to_float(payload.available_cash, 0.0),
        "frozen_cash": _to_float(payload.frozen_cash, 0.0),
        "market_value": _to_float(payload.market_value, 0.0),
        "liabilities": _to_float(payload.liabilities, 0.0),
        "short_market_value": _to_float(payload.short_market_value, 0.0),
        "position_rows": position_rows,
        "source": "bridge_payload",
        "snapshot_at": _iso_or_none(payload.reported_at),
    }

    if metrics_meta.get("snapshot_persisted") is False:
        latest_snapshot = await _fetch_latest_real_account_snapshot(
            db,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
        )
        if latest_snapshot is not None:
            payload_json = latest_snapshot.get("payload_json") or {}
            account_info.update(
                {
                    "cash": latest_snapshot.get("cash", account_info["cash"]),
                    "available_cash": latest_snapshot.get(
                        "available_cash", account_info["available_cash"]
                    ),
                    "total_asset": latest_snapshot.get(
                        "total_asset", account_info["total_asset"]
                    ),
                    "market_value": latest_snapshot.get(
                        "market_value", account_info["market_value"]
                    ),
                    "today_pnl": latest_snapshot.get(
                        "today_pnl", account_info["today_pnl"]
                    ),
                    "total_pnl": latest_snapshot.get(
                        "total_pnl", account_info["total_pnl"]
                    ),
                    "floating_pnl": latest_snapshot.get(
                        "floating_pnl", account_info["floating_pnl"]
                    ),
                    "monthly_pnl": latest_snapshot.get(
                        "monthly_pnl", account_info["monthly_pnl"]
                    ),
                    "total_return": latest_snapshot.get(
                        "total_return", account_info["total_return"]
                    ),
                    "positions": latest_snapshot.get(
                        "positions", payload_json.get("positions", position_rows)
                    ),
                    "position_count": latest_snapshot.get("position_count"),
                    "baseline": latest_snapshot.get("baseline"),
                    "is_online": False,
                    "snapshot_guard_triggered": True,
                }
            )
            safe_sync_payload = _build_portfolio_sync_payload_from_snapshot(
                latest_snapshot,
                fallback_payload=payload,
                fallback_positions=position_rows,
            )
            if safe_sync_payload is not None:
                portfolio_sync_payload = safe_sync_payload
        guard_reason = metrics_meta.get("snapshot_reject_reason") or "unknown"
        metrics_meta["quality"] = "guard_rejected"
        metrics_meta["snapshot_guard"] = {
            "triggered": True,
            "reason": guard_reason,
            "fallback_sync_source": portfolio_sync_payload.get("source"),
            "fallback_snapshot_at": portfolio_sync_payload.get("snapshot_at"),
            "rejected_payload": {
                "reported_at": _iso_or_none(payload.reported_at),
                "total_asset": _to_float(payload.total_asset, 0.0),
                "cash": _to_float(payload.cash, 0.0),
                "available_cash": _to_float(payload.available_cash, 0.0),
                "market_value": _to_float(payload.market_value, 0.0),
                "positions": len(position_rows),
            },
        }
        logger.warning(
            "[BridgeAccount] snapshot guard triggered tenant=%s user=%s account=%s reason=%s "
            "rejected(total_asset=%.2f cash=%.2f available_cash=%.2f market_value=%.2f positions=%d) "
            "sync_source=%s fallback_snapshot_at=%s",
            ctx.tenant_id,
            ctx.user_id,
            ctx.account_id,
            guard_reason,
            _to_float(payload.total_asset, 0.0),
            _to_float(payload.cash, 0.0),
            _to_float(payload.available_cash, 0.0),
            _to_float(payload.market_value, 0.0),
            len(position_rows),
            portfolio_sync_payload.get("source"),
            portfolio_sync_payload.get("snapshot_at"),
        )

    # 同步 QMT 账户快照到 PostgreSQL（portfolio + positions 表）
    if (
        metrics_meta.get("snapshot_persisted") is False
        and portfolio_sync_payload.get("source") == "bridge_payload"
    ):
        logger.warning(
            "[BridgeAccount] skip portfolio sync after snapshot guard tenant=%s user=%s account=%s because no valid fallback snapshot payload is available",
            ctx.tenant_id,
            ctx.user_id,
            ctx.account_id,
        )
    else:
        await _sync_qmt_account_to_db(
            db=db,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            total_asset=_to_float(portfolio_sync_payload.get("total_asset"), 0.0),
            available_cash=_to_float(portfolio_sync_payload.get("available_cash"), 0.0),
            frozen_cash=_to_float(portfolio_sync_payload.get("frozen_cash"), 0.0),
            market_value=_to_float(portfolio_sync_payload.get("market_value"), 0.0),
            liabilities=_to_float(portfolio_sync_payload.get("liabilities"), 0.0),
            short_market_value=_to_float(
                portfolio_sync_payload.get("short_market_value"), 0.0
            ),
            position_rows=list(portfolio_sync_payload.get("position_rows") or []),
        )

    await db.commit()

    # PostgreSQL 持久化成功后，再更新 Redis 作为短期缓存与推送源。
    # 若本次快照被 guard 拒绝且没有任何可用回退，则不写入原始坏 payload，
    # 避免前端继续消费到 total_asset=0 但持仓/市值不为空的异常口径。
    should_write_account_cache = not (
        metrics_meta.get("snapshot_persisted") is False
        and portfolio_sync_payload.get("source") == "bridge_payload"
    )
    if should_write_account_cache:
        write_trade_account_cache(redis, ctx.tenant_id, ctx.user_id, account_info)

        # 通知前端刷新 (Event-Driven)
        try:
            event_data = {
                "event_type": "ACCOUNT_UPDATED",
                "user_id": str(ctx.user_id),
                "tenant_id": ctx.tenant_id,
                "account_id": ctx.account_id,
                "timestamp": datetime.now().isoformat(),
            }
            redis.publish_event("trading_events", event_data)
        except Exception as e:
            logger.warning(f"Failed to publish trading event for QMT account: {e}")
    else:
        logger.warning(
            "[BridgeAccount] skip redis cache update after rejected snapshot tenant=%s user=%s account=%s",
            ctx.tenant_id,
            ctx.user_id,
            ctx.account_id,
        )

    return {"ok": True, "timestamp": timestamp}


@router.post("/bridge/heartbeat")
async def report_qmt_heartbeat(
    payload: QMTBridgeHeartbeatPayload,
    ctx: BridgeSessionContext = Depends(_get_bridge_session_context),
    redis: RedisClient = Depends(get_redis),
    db=Depends(get_db),
):
    if payload.account_id != ctx.account_id:
        raise HTTPException(status_code=403, detail="account_id mismatch")
    reported_at = payload.reported_at or utcnow()
    heartbeat_info = {
        "tenant_id": ctx.tenant_id,
        "user_id": ctx.user_id,
        "account_id": ctx.account_id,
        "hostname": payload.hostname or ctx.hostname,
        "client_version": payload.client_version or ctx.client_version,
        "status": payload.status,
        "qmt_connected": payload.qmt_connected,
        "latency_ms": payload.latency_ms,
        "timestamp": reported_at.timestamp(),
        "reported_at": _iso_or_none(reported_at),
    }
    write_trade_agent_heartbeat_cache(redis, ctx.tenant_id, ctx.user_id, heartbeat_info)
    binding = await db.get(QMTAgentBinding, ctx.binding_id)
    if binding is not None:
        binding.hostname = payload.hostname or binding.hostname
        binding.client_version = payload.client_version or binding.client_version
        binding.last_seen_at = reported_at
        await db.commit()
    return {"ok": True, "timestamp": reported_at.timestamp()}


@router.post("/bridge/execution")
async def report_qmt_execution(
    payload: QMTBridgeExecutionPayload,
    ctx: BridgeSessionContext = Depends(_get_bridge_session_context),
    db=Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    if payload.account_id != ctx.account_id:
        raise HTTPException(status_code=403, detail="account_id mismatch")
    if not ctx.user_id:
        raise HTTPException(status_code=400, detail="invalid bridge user_id")

    client_oid = str(payload.client_order_id or "").strip()
    logger.info(
        "[BridgeExecution] incoming tenant=%s user=%s account=%s client_order_id=%s exchange_order_id=%s "
        "exchange_trade_id=%s symbol=%s status=%s",
        ctx.tenant_id,
        ctx.user_id,
        ctx.account_id,
        client_oid,
        str(payload.exchange_order_id or "").strip(),
        str(payload.exchange_trade_id or "").strip(),
        str(payload.symbol or "").strip(),
        str(payload.status or "").strip(),
    )

    # 1) 首选：tenant + user + client_order_id 精确匹配
    result = await db.execute(
        select(Order).where(
            and_(
                Order.tenant_id == ctx.tenant_id,
                Order.user_id == ctx.user_id,
                Order.client_order_id == client_oid,
            )
        )
    )
    order = result.scalar_one_or_none()
    matched_by = "client_order_id"

    # 2) 兼容：某些 Agent 会把 order_id(UUID) 填入 client_order_id 回传
    if order is None:
        try:
            oid = uuid.UUID(client_oid)
        except Exception:
            oid = None
        if oid is not None:
            result = await db.execute(
                select(Order).where(
                    and_(
                        Order.tenant_id == ctx.tenant_id,
                        Order.user_id == ctx_user_id,
                        Order.order_id == oid,
                    )
                )
            )
            order = result.scalar_one_or_none()
            if order is not None:
                matched_by = "order_id"

    # 3) 兼容：如果回传了 exchange_order_id，允许按其匹配历史订单
    if order is None and payload.exchange_order_id:
        ex_oid = str(payload.exchange_order_id).strip()
        if ex_oid:
            result = await db.execute(
                select(Order).where(
                    and_(
                        Order.tenant_id == ctx.tenant_id,
                        Order.user_id == ctx_user_id,
                        Order.exchange_order_id == ex_oid,
                    )
                )
            )
            order = result.scalar_one_or_none()
            if order is not None:
                matched_by = "exchange_order_id"

    # 4) 兜底：回报缺失 client/exchange id 时，按最近 ACK 等待中的同标的同方向订单匹配
    if order is None:
        symbol = str(payload.symbol or "").strip().upper()
        side = str(payload.side or "").strip().upper()
        order_side = None
        if side in {"BUY", "SELL"}:
            try:
                order_side = OrderSide(side)
            except Exception:
                order_side = None
        if symbol and order_side is not None:
            recent_cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
                minutes=15
            )
            candidates_result = await db.execute(
                select(Order)
                .where(
                    and_(
                        Order.tenant_id == ctx.tenant_id,
                        Order.user_id == ctx_user_id,
                        Order.symbol == symbol,
                        Order.side == order_side,
                        Order.submitted_at.is_not(None),
                        Order.submitted_at >= recent_cutoff,
                        Order.exchange_order_id.is_(None),
                        Order.remarks.is_not(None),
                        Order.remarks.like("%[AWAITING_BRIDGE_ACK]%"),
                    )
                )
                .order_by(Order.submitted_at.desc())
                .limit(2)
            )
            candidates = list(candidates_result.scalars().all())
            if len(candidates) == 1:
                order = candidates[0]
                matched_by = "symbol_side_recent_ack_waiting"
            elif len(candidates) > 1:
                logger.warning(
                    "[BridgeExecution] fallback match ambiguous tenant=%s user=%s account=%s symbol=%s side=%s candidates=%s",
                    ctx.tenant_id,
                    ctx.user_id,
                    ctx.account_id,
                    symbol,
                    side,
                    [str(item.order_id) for item in candidates],
                )

    if order is None:
        logger.warning(
            "[BridgeExecution] order not found tenant=%s user=%s account=%s client_order_id=%s "
            "exchange_order_id=%s symbol=%s status=%s",
            ctx.tenant_id,
            ctx.user_id,
            ctx.account_id,
            client_oid,
            str(payload.exchange_order_id or "").strip(),
            str(payload.symbol or "").strip(),
            str(payload.status or "").strip(),
        )
        raise HTTPException(status_code=404, detail="order not found")
    logger.info(
        "[BridgeExecution] matched tenant=%s user=%s order_id=%s via=%s client_order_id=%s",
        ctx.tenant_id,
        ctx.user_id,
        str(order.order_id),
        matched_by,
        str(getattr(order, "client_order_id", "") or ""),
    )

    # 字符串状态映射（QMT Agent 应上报标准英文状态字符串）
    status_map = {
        "SUBMITTED": OrderStatus.SUBMITTED,
        "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
        "FILLED": OrderStatus.FILLED,
        "REJECTED": OrderStatus.REJECTED,
        "CANCELLED": OrderStatus.CANCELLED,
        "PARTIALLY_CANCELLED": OrderStatus.CANCELLED,  # 部撤 → 视为已撤
        "EXPIRED": OrderStatus.EXPIRED,
    }
    # QMT 原始数字状态码兜底映射（防止 Agent 未转换直接上报）
    qmt_code_map = {
        "48": OrderStatus.PENDING,  # 未报
        "49": OrderStatus.SUBMITTED,  # 待报
        "50": OrderStatus.SUBMITTED,  # 已报（交易所确认，未成交）
        "51": OrderStatus.CANCELLED,  # 报撤中
        "52": OrderStatus.PARTIALLY_FILLED,  # 部成待撤
        "53": OrderStatus.CANCELLED,  # 已撤
        "54": OrderStatus.CANCELLED,  # 部撤
        "55": OrderStatus.REJECTED,  # 废单
        "56": OrderStatus.FILLED,  # 已成（实盘回放观测）
        "57": OrderStatus.REJECTED,  # 柜台拒单/无效委托（实盘回放观测）
        "58": OrderStatus.FILLED,  # 已成
    }
    raw_status = str(payload.status).strip()
    normalized_status = status_map.get(raw_status.upper()) or qmt_code_map.get(
        raw_status, OrderStatus.SUBMITTED
    )

    # 防御：FILLED 但 filled_quantity<=0 → 降级为 SUBMITTED（避免 QMT 状态误报产生虚假成交记录）
    filled_qty = (
        float(payload.filled_quantity) if payload.filled_quantity is not None else 0.0
    )
    if normalized_status == OrderStatus.FILLED and filled_qty <= 0:
        normalized_status = OrderStatus.SUBMITTED

    order.status = normalized_status
    order.exchange_order_id = payload.exchange_order_id or order.exchange_order_id
    if payload.message or payload.error_code:
        msg = str(payload.message or "").strip()
        if payload.error_code:
            msg = f"[{payload.error_code}] {msg}".strip()
        order.remarks = msg
    exchange_trade_id = str(payload.exchange_trade_id or "").strip()
    if normalized_status == OrderStatus.PARTIALLY_FILLED and not exchange_trade_id:
        # 订单状态回调可能携带累计 traded_volume，但没有唯一成交 ID；
        # 为避免与后续 trade callback 双计，这里仅更新状态，不累计成交金额/数量。
        pass
    elif normalized_status == OrderStatus.FILLED and not exchange_trade_id:
        # FILLED 的订单状态回调同理，只更新状态与 exchange_order_id；
        # 真实成交入账以带 exchange_trade_id 的 trade callback 为准。
        pass
    elif normalized_status in {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED}:
        if filled_qty > 0:
            price = (
                payload.filled_price
                or getattr(order, "average_price", None)
                or order.price
                or 0.0
            )
            trade_value = filled_qty * price
            dedup_result = await db.execute(
                select(Trade).where(
                    and_(
                        Trade.tenant_id == ctx.tenant_id,
                        Trade.user_id == ctx.user_id,
                        Trade.exchange_trade_id == exchange_trade_id,
                    )
                )
            )
            existing_trade = dedup_result.scalar_one_or_none()
            if existing_trade is None:
                trade = Trade(
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    portfolio_id=order.portfolio_id,
                    order_id=order.order_id,
                    symbol=order.symbol,
                    symbol_name=getattr(order, "symbol_name", None),
                    side=order.side,
                    trading_mode=order.trading_mode,
                    quantity=filled_qty,
                    price=price,
                    trade_value=trade_value,
                    commission=0.0,
                    exchange_trade_id=payload.exchange_trade_id,
                    executed_at=datetime.now(),
                    remarks=(
                        (f"[{payload.error_code}] " if payload.error_code else "")
                        + str(payload.message or "")
                    ).strip()
                    or None,
                )
                db.add(trade)
                order.filled_quantity = (
                    float(getattr(order, "filled_quantity", 0.0) or 0.0) + filled_qty
                )
                order.filled_value = (
                    float(getattr(order, "filled_value", 0.0) or 0.0) + trade_value
                )
                if order.filled_quantity > 0:
                    order.average_price = order.filled_value / order.filled_quantity
        total_quantity = float(order.quantity or 0.0)
        if (
            total_quantity > 0
            and float(getattr(order, "filled_quantity", 0.0) or 0.0) >= total_quantity
        ):
            order.status = OrderStatus.FILLED
        elif float(getattr(order, "filled_quantity", 0.0) or 0.0) > 0:
            order.status = OrderStatus.PARTIALLY_FILLED
    await db.commit()

    # 通知前端刷新 (Event-Driven)
    try:
        # 如果是 FILLED 状态则发送 TRADE_CREATED，否则发送 ORDER_UPDATED
        # 前端 useTradeWebSocket 会监听到此消息并调用 fetchData() 刷新页面数据
        event_data = {
            "event_type": "TRADE_CREATED"
            if normalized_status == OrderStatus.FILLED
            else "ORDER_UPDATED",
            "order_id": str(order.order_id),
            "user_id": str(ctx.user_id),
            "tenant_id": ctx.tenant_id,
            "status": normalized_status.value,
            "symbol": order.symbol,
            "filled_quantity": float(order.filled_quantity or 0),
            "timestamp": datetime.now().isoformat(),
        }
        redis.publish_event("trading_events", event_data)
    except Exception as e:
        logger.warning(f"Failed to publish trading event for QMT execution: {e}")

    return {"ok": True, "status": normalized_status.value}
