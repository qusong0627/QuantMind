import json
import os
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    import exchange_calendars as xcals
except Exception:
    xcals = None

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from backend.services.api.user_app.middleware.auth import require_admin
from backend.services.engine.inference.script_runner import InferenceScriptRunner
from backend.services.engine.data_platform.quantdb_factor_reader import (
    QuantDBFactorReader,
)
from backend.shared.database_manager_v2 import get_session
from backend.shared.redis_sentinel_client import get_redis_sentinel_client
from backend.shared.trading_calendar import calendar_service

try:
    from backend.services.engine.qlib_app.celery_config import celery_app
except ImportError:
    celery_app = None

from .data_status_scanner import scan_data_status
from .quantdb_factor_catalog import load_active_factor_catalog
from .model_management_utils import (
    FEATURE_SNAPSHOT_DIR,
    MODELS_PRODUCTION,
    MODELS_ROOT,
    _enrich_feature_catalog_with_data_coverage_async,
    _find_model_directories,
    _load_feature_catalog_from_db,
    _load_feature_catalog_from_file,
    _resolve_expected_feature_dim,
    _resolve_inference_dates_with_calendar,
    _resolve_ready_threshold,
    _scan_model_directory,
    _scan_feature_snapshots_status,
)

router = APIRouter(dependencies=[Depends(require_admin)])  # 路由器级认证兜底


def _model_data_context(model_dir: Path) -> tuple[Path, dict[str, Any]]:
    """Resolve the only valid data root from immutable model metadata.

    Direct QuantDB models must never be date-discovered or backtested from a
    feature snapshot merely because that was the historic API default.
    """
    try:
        meta = json.loads((model_dir / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        meta = {}
    if str(meta.get("data_source") or "").lower() == "quantdb_factors":
        # 与 script_runner._resolve_quantdb_data_dir 保持一致：
        # QUANTDB_DATA_DIR → QM_QUANTDB_DATA_DIR → hub 统一解析。
        # 仅读 QUANTDB_DATA_DIR 会在容器内落到不存在的默认 /app/data/quantdb，
        # 导致回测日期发现/交易日期列表拿不到数据。
        try:
            from backend.services.engine.inference.script_runner import _resolve_quantdb_data_dir
            return Path(_resolve_quantdb_data_dir()), meta
        except Exception:  # pragma: no cover - 兜底
            return Path(os.getenv("QUANTDB_DATA_DIR", "/app/data/quantdb")), meta
    return Path(os.getcwd()) / "db" / "feature_snapshots", meta


@router.get("/scan", summary="扫描本地模型目录")
async def scan_model_directories(
    refresh: bool = Query(False, description="是否强制重新扫描（绕过 Redis 缓存）"),
    current_user: dict = Depends(require_admin),
):
    """
    自动扫描 models/ 下所有有效模型目录，聚合 metadata.json、
    workflow_config.yaml、best_params.yaml 等元数据文件，返回结构化列表。
    有 15 秒硬超时保护，超时返回已扫描的部分结果。
    """
    import asyncio
    import time as _time_module

    CACHE_KEY = "qm:admin:models:scan:v1"
    CACHE_TTL = 300  # 5 分钟

    # 1. 命中 Redis 缓存（除非 ?refresh=true）
    redis = None
    try:
        redis = get_redis_sentinel_client()
    except Exception:
        redis = None

    if not refresh and redis is not None:
        try:
            cached = redis.get(CACHE_KEY)
            if cached:
                payload = json.loads(cached)
                payload["from_cache"] = True
                return payload
        except Exception:
            pass

    # 2. 实际扫描（线程池避免阻塞事件循环）
    try:
        dirs = _find_model_directories(MODELS_ROOT)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"扫描目录失败: {e}") from e

    def _sanitize(obj):
        """Replace NaN/Inf with None so FastAPI can JSON-serialize."""
        import math

        if isinstance(obj, float):
            if math.isnan(obj) or math.isinf(obj):
                return None
            return obj
        if isinstance(obj, dict):
            return {k: _sanitize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_sanitize(v) for v in obj]
        if isinstance(obj, tuple):
            return [_sanitize(v) for v in obj]
        return obj

    def _scan_all():
        results = []
        for d in dirs:
            try:
                results.append(_sanitize(_scan_model_directory(d)))
            except Exception as e:
                results.append(
                    {"model_id": Path(d).name, "dir_path": d, "error": str(e)}
                )
        return results

    loop = asyncio.get_event_loop()

    # 15 秒硬超时：即使 OSS/生产环境磁盘慢，也保证不 hang
    try:
        results = await asyncio.wait_for(
            loop.run_in_executor(None, _scan_all),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail="模型目录扫描超时（>15s），请检查磁盘 I/O 或 models/ 目录下是否有大量超大文件",
        )

    # 按最近更新倒序：最新训练的模型排最前（否则新模型埋在 148 个目录末尾，
    # 前端 10 条/页要翻十几页才看得到）。updated_at 缺失/空排最后。
    def _sort_key(m):
        v = m.get("updated_at")
        if v is None:
            return ""
        if hasattr(v, "isoformat"):
            return v.isoformat()
        return str(v)

    results.sort(key=_sort_key, reverse=True)

    payload = {
        "total": len(results),
        "models": results,
        "from_cache": False,
        "cached_at": int(_time_module.time()),
    }

    # 3. 写入 Redis 缓存
    if redis is not None:
        try:
            redis.setex(CACHE_KEY, CACHE_TTL, json.dumps(payload, default=str))
        except Exception:
            pass

    return payload


@router.get("/feature-catalog", summary="获取模型训练特征字典（动态）")
async def get_model_feature_catalog(
    market: str | None = None,
    factor_source: str = Query("l1_l2_factors", description="QuantDB 因子源"),
    include_coverage: bool = Query(
        False, description="是否附带 parquet 数据覆盖统计（默认 false，加速首屏）"
    ),
    current_user: dict = Depends(require_admin),
):
    """
    返回训练页第一步所需的特征分类与字段列表：
    - 优先读取 PostgreSQL 特征注册表
    - 若注册表未初始化，回退到 config/features/*.json
    - data_coverage（parquet 行数/日期范围）默认不附带，需 ?include_coverage=true
    """
    _ = current_user
    market_upper = str(market or "CN").upper()
    if market_upper in {"CN", "A", "A_SHARE"}:
        try:
            direct_catalog = await load_active_factor_catalog(factor_source)
        except Exception:
            direct_catalog = None
        if direct_catalog:
            if include_coverage:
                direct_catalog["data_coverage"] = (
                    QuantDBFactorReader().describe(factor_source).to_dict()
                )
            return direct_catalog
    try:
        catalog = await _load_feature_catalog_from_db(market=market)
    except Exception:
        catalog = None

    if not catalog:
        catalog = _load_feature_catalog_from_file(market=market)

    if not catalog:
        raise HTTPException(
            status_code=404, detail="未找到可用的特征字典（DB/文件均不可用）"
        )

    if include_coverage:
        return await _enrich_feature_catalog_with_data_coverage_async(
            catalog, market=market
        )
    return catalog


@router.put("/feature-catalog", summary="更新特征字典（保存到 JSON 文件）")
async def update_feature_catalog(
    catalog: dict[str, Any],
    current_user: dict = Depends(require_admin),
):
    """保存特征字典到 JSON 文件，前端训练页下次加载时自动生效。"""
    categories = catalog.get("categories")
    if not isinstance(categories, list):
        raise HTTPException(status_code=400, detail="categories must be a list")

    valid_markets = {"CN", "HK", "US", "CRYPTO", "FUTURES", "CUSTOM"}
    # 计算总特征数 + 归一化 explanation/markets
    total_features = 0
    for cat in categories:
        features = cat.get("features", [])
        if not isinstance(features, list):
            raise HTTPException(
                status_code=400,
                detail=f"Category '{cat.get('id')}' features must be a list",
            )
        for feat in features:
            if not isinstance(feat, dict):
                continue
            expl = str(feat.get("explanation") or feat.get("detail") or "")
            if len(expl) > 500:
                raise HTTPException(status_code=400, detail=f"Feature '{feat.get('key')}' explanation 超过500字")
            feat["explanation"] = expl
            markets = feat.get("markets")
            if isinstance(markets, list):
                cleaned = [str(m).upper() for m in markets if str(m).strip()]
                unknown = [m for m in cleaned if m not in valid_markets]
                if unknown:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Feature '{feat.get('key')}' 含非法市场: {','.join(unknown)}",
                    )
                feat["markets"] = cleaned
        cat["feature_count"] = len(features)
        total_features += len(features)
    catalog["feature_count"] = total_features

    path = (
        Path(os.getcwd())
        / "config"
        / "features"
        / "model_training_feature_catalog_v1.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "ok", "feature_count": total_features, "path": str(path)}


@router.get("/data-status", summary="查看当前数据状态（Qlib + 特征快照）")
async def get_data_status(
    refresh: bool = Query(False, description="是否强制刷新（后台异步）"),
    market: str = Query(
        "a_share", description="市场: a_share, crypto, hong_kong, us_stock"
    ),
    current_user: dict = Depends(require_admin),
):
    """
    管理后台数据管理接口：
    - 优先从 Redis 获取缓存结果
    - Qlib 文件数据（calendar/instruments/features）状态
    - feature_snapshots 目录下的 parquet 文件状态
    - 支持按市场筛选
    """
    _ = current_user
    redis = None
    try:
        redis = get_redis_sentinel_client()
    except Exception:
        pass

    # 非 A 股市场使用市场专属缓存 key
    cache_key = f"qm:admin:data_status:{market}"

    # 1. 如果不是强制刷新，尝试读取缓存
    if not refresh and redis:
        try:
            cached = redis.get(cache_key)
            if cached:
                result = json.loads(cached)
                result["from_cache"] = True
                return result
        except Exception as e:
            print(f"Redis cache read failed: {e}")

    # 2. 如果强制刷新，或者没缓存，则触发后台任务
    if celery_app:
        try:
            celery_app.send_task(
                "engine.tasks.get_data_status_task",
                kwargs={"market": market},
            )
        except Exception as e:
            print(f"Failed to trigger background task: {e}")

    # 3. 实时辅助扫描（作为 fallback 或首次加载的快速反馈）
    try:
        tenant_id = str(current_user.get("tenant_id") or "default")
        user_id = str(current_user.get("user_id") or current_user.get("sub") or "admin")

        result = await scan_data_status(
            market=market,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        result["async_trigger"] = bool(celery_app)
        result["message"] = (
            "数据正在后台扫描中，请稍后刷新" if not refresh else "已触发强制刷新任务"
        )
        return result
    except Exception as e:
        import traceback

        error_msg = f"Data status scanning failed: {str(e)}"
        print(error_msg)
        print(traceback.format_exc())
        return {
            "error": error_msg,
            "checked_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
            "market": market,
            "qlib_data": {"exists": False},
            "feature_snapshots": {"exists": False},
            "message": f"状态扫描异常: {str(e)}",
        }


@router.post(
    "/sync-stock-daily-latest",
    summary="手动触发 Baostock 同步基础行情到 stock_daily_latest",
)
async def sync_stock_daily_latest(
    target_date: str | None = Query(None, description="目标日期 YYYY-MM-DD，默认今天"),
    max_symbols: int = Query(
        0, ge=0, le=10000, description="仅处理前 N 个标的（0=全部）"
    ),
    apply: bool = Query(True, description="是否执行写入（false=dry-run）"),
    background: bool = Query(
        True, description="是否在后台执行（解决 504 超时业务推荐）"
    ),
    current_user: dict = Depends(require_admin),
):
    """
    [已废弃] 数据现由官方服务器统一推送，不再需要手动从 Baostock 同步。
    """
    _ = current_user

    raise HTTPException(
        status_code=410, detail="该接口已废弃，数据由官方服务器统一推送，无需手动同步"
    )


@router.post(
    "/update-feature-parquet",
    summary="更新特征快照（从 QuantDB 生成特征快照 Parquet）",
)
async def update_feature_parquet(
    year: int = Query(0, description="指定年份 (默认: 当前年份)"),
    current_user: dict = Depends(require_admin),
):
    """Deprecated: new models must use direct QuantDB factor sources."""
    _ = year, current_user
    raise HTTPException(
        status_code=410,
        detail="特征快照生成已停用；请在模型训练数据集发布 QuantDB 因子映射后直接训练",
    )


@router.post(
    "/update-market-features",
    summary="更新非 A 股市场的特征快照（从 H5 数据计算 OHLCV 特征）",
)
async def update_market_features(
    market: str = Query(..., description="市场: crypto, hong_kong, us_stock"),
    rebuild: bool = Query(False, description="是否重建全部特征（默认增量）"),
    current_user: dict = Depends(require_admin),
):
    """
    运行 update_market_features.py 脚本，从 H5 文件读取 OHLCV 数据，
    计算 134 维模型特征，保存到 market-specific parquet。
    """
    _ = current_user

    if market not in ("crypto", "hong_kong", "us_stock"):
        raise HTTPException(status_code=400, detail=f"不支持的市场: {market}")

    script_path = Path("/app/backend/scripts/update_market_features.py")
    if not script_path.exists():
        # 回退到主机路径
        script_path = (
            Path(os.getcwd()) / "backend" / "scripts" / "update_market_features.py"
        )
    if not script_path.exists():
        raise HTTPException(status_code=404, detail=f"脚本不存在: {script_path}")

    cmd = ["python", str(script_path), "--market", market]
    if rebuild:
        cmd.append("--rebuild")

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(script_path.parent.parent.parent),
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        return {
            "success": proc.returncode == 0,
            "market": market,
            "exit_code": proc.returncode,
            "stdout": proc.stdout[-3000:] if proc.stdout else "",
            "stderr": proc.stderr[-3000:] if proc.stderr else "",
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="特征更新超时（>600s），请稍后重试")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post(
    "/sync-stock-daily-full",
    summary="日常全量同步：从 QuantDB 补齐 stock_daily_latest 可同步列（features_daily 技术/估值 + 未复权K线 + roe/行业）",
)
async def sync_stock_daily_full(
    max_days: int = Query(
        30, ge=1, le=365, description="同步最近 N 个交易日（默认30）"
    ),
    current_user: dict = Depends(require_admin),
):
    """
    从 QuantDB (features_daily + 未复权K线 + 3_financial_data/roe + instrument_detail/行业)
    全量同步 stock_daily_latest 中 QuantDB 能提供的列。
    注：QuantDB 目前不产 is_st/idx_*/concept_*/涨停统计 等列，这些列不参与同步。
    """
    _ = current_user

    script_path = Path("/app/scripts/data/maintenance/sync_stock_daily_full.py")
    if not script_path.exists():
        raise HTTPException(status_code=404, detail=f"同步脚本不存在: {script_path}")

    try:
        env = os.environ.copy()
        env["SYNC_MAX_DAYS"] = str(max_days)
        proc = subprocess.run(
            ["python", str(script_path)],
            cwd="/app",
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
            env=env,
        )
        return {
            "success": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": proc.stdout[-3000:] if proc.stdout else "",
            "stderr": proc.stderr[-3000:] if proc.stderr else "",
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="同步超时，请检查数据量是否过大")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"同步执行异常: {exc}")


@router.get("/precheck-inference", summary="生成明日信号前置检查")
async def precheck_inference(
    current_user: dict = Depends(require_admin),
):
    """
    在执行“生成明日信号”前检查关键依赖是否存在。
    仅返回可读检查结果，不执行实际推理。
    """
    checks: list[dict[str, Any]] = []

    production_dir = Path(MODELS_PRODUCTION)
    production_exists = production_dir.exists() and production_dir.is_dir()
    checks.append(
        {
            "key": "production_model_dir",
            "label": "生产模型目录存在",
            "passed": production_exists,
            "detail": str(production_dir),
        }
    )

    model_files = []
    for ext in ["bin", "txt", "pkl", "pth", "onnx", "pt"]:
        model_files.extend(list(production_dir.glob(f"model.{ext}")))

    model_exists = len(model_files) > 0
    checks.append(
        {
            "key": "model_file",
            "label": "模型文件存在（model.txt/bin/pkl/etc）",
            "passed": model_exists,
            "detail": str(model_files[0]) if model_files else "None",
        }
    )

    metadata_file = production_dir / "metadata.json"
    metadata_exists = metadata_file.exists() and metadata_file.is_file()
    checks.append(
        {
            "key": "metadata",
            "label": "模型元数据存在（metadata.json）",
            "passed": metadata_exists,
            "detail": str(metadata_file),
        }
    )

    try:
        from backend.shared.qlib_paths import resolve_qlib_provider_uri
        qlib_data_dir = Path(resolve_qlib_provider_uri("CN"))
    except Exception:  # noqa: BLE001
        qlib_data_dir = Path(os.path.join(os.getcwd(), "db", "qlib_data"))
    qlib_data_exists = qlib_data_dir.exists() and qlib_data_dir.is_dir()
    checks.append(
        {
            "key": "qlib_data_dir",
            "label": "Qlib 数据目录存在",
            "passed": qlib_data_exists,
            "detail": str(qlib_data_dir),
        }
    )

    # 业务门禁：统一日期口径（数据交易日 + 预测生效交易日）
    tz = ZoneInfo("Asia/Shanghai")
    now_local = datetime.now(tz)
    (
        requested_data_trade_date_str,
        data_trade_date_str,
        prediction_trade_date_str,
        calendar_adjusted,
    ) = await _resolve_inference_dates_with_calendar(
        current_user=current_user, now_local=now_local
    )
    trade_date_obj = date.fromisoformat(data_trade_date_str)
    checks.append(
        {
            "key": "calendar_trade_date",
            "label": "交易日历校验",
            "passed": True,
            "detail": (
                f"候选 {requested_data_trade_date_str} 非交易日，已回退到 {data_trade_date_str}"
                if calendar_adjusted
                else f"{data_trade_date_str} 为交易日"
            ),
        }
    )

    checks.append(
        {
            "key": "data_trade_date",
            "label": "检测数据交易日",
            "passed": True,
            "detail": data_trade_date_str,
        }
    )
    checks.append(
        {
            "key": "prediction_trade_date",
            "label": "预测生效交易日（明日）",
            "passed": True,
            "detail": prediction_trade_date_str,
        }
    )

    runner = InferenceScriptRunner(MODELS_PRODUCTION)
    primary_script_exists = runner.check_script_exists()
    fallback_script_exists = runner.check_fallback_script_exists()
    inference_script_exists = primary_script_exists or fallback_script_exists
    checks.append(
        {
            "key": "inference_script",
            "label": "推理脚本存在（主/兜底至少一套）",
            "passed": inference_script_exists,
            "detail": (
                f"primary={Path(runner.primary_model_dir) / runner.primary_script_name} exists={primary_script_exists}; "
                f"fallback={Path(runner.fallback_model_dir) / runner.fallback_script_name} exists={fallback_script_exists}"
            ),
        }
    )

    expected_feature_dim = _resolve_expected_feature_dim(production_dir)
    checks.append(
        {
            "key": "expected_feature_dim",
            "label": "生产模型期望特征维度",
            "passed": True,
            "detail": str(expected_feature_dim),
        }
    )

    # 业务门禁：检查当日数据是否已落库
    data_stats: dict[str, Any] = {}
    dim_ready_rows = 0
    feature_cols_count = 0
    has_features_json = False
    dim_source = "none"
    try:
        async with get_session(read_only=True) as session:
            stat_sql = text("""
                SELECT
                    MAX(trade_date) AS latest_trade_date,
                    MAX(updated_at) AS latest_updated_at,
                    COUNT(*) FILTER (WHERE trade_date = :trade_date) AS today_rows
                FROM stock_daily_latest
                """)
            row = (
                (
                    await session.execute(
                        stat_sql,
                        {
                            "trade_date": trade_date_obj,
                        },
                    )
                )
                .mappings()
                .first()
            )
            data_stats = dict(row or {})

            schema_columns = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT column_name
                            FROM information_schema.columns
                            WHERE table_schema = 'public'
                              AND table_name = 'stock_daily_latest'
                            """
                        )
                    )
                )
                .mappings()
                .all()
            )
            column_names = {
                str((row or {}).get("column_name") or "") for row in schema_columns
            }
            has_features_json = "features" in column_names
            feature_columns = sorted(
                [c for c in column_names if re.fullmatch(r"feature_\d+", c)],
                key=lambda c: int(c.split("_", 1)[1]),
            )
            feature_cols_count = len(feature_columns)

            dim_expr_candidates: list[str] = []
            if has_features_json:
                dim_expr_candidates.append(
                    "CASE WHEN jsonb_typeof(features) = 'array' THEN jsonb_array_length(features) ELSE 0 END"
                )
            if feature_columns:
                cols_dim_expr = " + ".join(
                    [
                        f"(CASE WHEN {col} IS NULL THEN 0 ELSE 1 END)"
                        for col in feature_columns
                    ]
                )
                dim_expr_candidates.append(f"({cols_dim_expr})")

            if len(dim_expr_candidates) >= 2:
                dim_source = "features_json+feature_columns"
                dim_expr = f"GREATEST({', '.join(dim_expr_candidates)})"
            elif len(dim_expr_candidates) == 1:
                dim_source = "features_json" if has_features_json else "feature_columns"
                dim_expr = dim_expr_candidates[0]
            else:
                # 表中既没有 features 也没有 feature_* 时，维度门禁必然不通过（但不应报 SQL 错）
                dim_source = "none"
                dim_expr = "0"

            dim_condition = f"({dim_expr}) >= :expected_feature_dim"

            dim_row = (
                (
                    await session.execute(
                        text(
                            f"""
                            SELECT COUNT(*) FILTER (
                                WHERE trade_date = :trade_date AND ({dim_condition})
                            ) AS dim_ready_rows
                            FROM stock_daily_latest
                            """
                        ),
                        {
                            "trade_date": trade_date_obj,
                            "expected_feature_dim": expected_feature_dim,
                        },
                    )
                )
                .mappings()
                .first()
            )
            dim_ready_rows = int((dim_row or {}).get("dim_ready_rows") or 0)
    except Exception as e:
        checks.append(
            {
                "key": "market_data_daily_query",
                "label": "market_data_daily 可查询",
                "passed": False,
                "detail": f"query_error={e}",
            }
        )

    if data_stats:
        latest_trade_date = data_stats.get("latest_trade_date")
        today_rows = int(data_stats.get("today_rows") or 0)
        required_ready_symbols, min_ready_symbols, min_ready_ratio, min_ready_floor = (
            _resolve_ready_threshold(today_rows)
        )

        checks.append(
            {
                "key": "latest_trade_date_today",
                "label": "最新特征交易日已就绪",
                "passed": str(latest_trade_date) >= data_trade_date_str,
                "detail": f"latest_trade_date={latest_trade_date} expected={data_trade_date_str}",
            }
        )
        checks.append(
            {
                "key": "today_rows_exists",
                "label": f"目标日({data_trade_date_str})数据已入库",
                "passed": today_rows > 0,
                "detail": (
                    f"rows={today_rows}"
                    if today_rows > 0
                    else f"stock_daily_latest 未发现 {data_trade_date_str} 数据"
                ),
            }
        )
        checks.append(
            {
                "key": "ready_symbols_threshold",
                "label": f"今日数据覆盖数 >= {required_ready_symbols}（自适应）",
                "passed": today_rows >= required_ready_symbols,
                "detail": (
                    f"actual={today_rows}, threshold={required_ready_symbols}, "
                    f"min_symbols={min_ready_symbols}, ratio={min_ready_ratio:.2f}, floor={min_ready_floor}"
                ),
            }
        )
        checks.append(
            {
                "key": "feature_dim_ready_threshold",
                "label": f"今日满足模型维度({expected_feature_dim})覆盖数 >= {required_ready_symbols}（自适应）",
                "passed": dim_ready_rows >= required_ready_symbols,
                "detail": (
                    f"dim_ready_rows={dim_ready_rows}, threshold={required_ready_symbols}, "
                    f"feature_columns={feature_cols_count}, features_json={has_features_json}, "
                    f"dim_source={dim_source}, min_symbols={min_ready_symbols}, "
                    f"ratio={min_ready_ratio:.2f}, floor={min_ready_floor}"
                ),
            }
        )

    return {
        "passed": all(bool(item.get("passed")) for item in checks),
        "checked_at": datetime.now().isoformat(),
        "requested_inference_date": requested_data_trade_date_str,
        "calendar_adjusted": calendar_adjusted,
        "data_trade_date": data_trade_date_str,
        "prediction_trade_date": prediction_trade_date_str,
        "items": checks,
    }


# ── 滚动回测（模型预测质量评估） ─────────────────────────────────────────


class TradingCostParams(BaseModel):
    """交易成本覆盖参数。留空则回退到模型 metadata.context，再回退 A 股标准费率。"""

    commission_rate: float | None = Field(
        default=None, description="佣金费率（双边），如 0.00025"
    )
    stamp_duty: float | None = Field(
        default=None, description="印花税（仅卖出），如 0.001"
    )
    transfer_fee: float | None = Field(
        default=None, description="过户费（沪市），如 0.00001"
    )
    slippage: float | None = Field(default=None, description="滑点（单边），如 0.001")

    def to_override(self) -> dict[str, float]:
        return {k: v for k, v in self.model_dump().items() if v is not None}


class BacktestRequest(BaseModel):
    model_id: str = Field(..., description="模型ID（目录名）")
    start_date: str = Field(..., description="回测起始日期 YYYY-MM-DD")
    end_date: str = Field(..., description="回测结束日期 YYYY-MM-DD")
    horizon: int = Field(default=10, description="预测周期 T+N（天）")
    sample_interval: int = Field(default=3, description="每隔 N 个交易日采样一次")
    cost: TradingCostParams | None = Field(default=None, description="交易成本覆盖参数")
    exclude_limit_moves: bool = Field(
        default=True, description="剔除信号日触及涨跌停的标的（次日一字板买不进）"
    )
    model_config = {"protected_namespaces": ()}


@router.post("/backtest", summary="滚动回测评估模型预测质量")
async def run_model_backtest(
    request: BacktestRequest,
    current_user: dict = Depends(require_admin),
):
    """
    对指定模型在历史日期范围内进行滚动回测。
    每隔 sample_interval 个交易日执行一次推理，对比预测分数与真实的 T+N 前瞻收益
    （close[T+N]/close[T]-1），计算 IC、IC_IR、扣费后多头超额、Sharpe 等指标。
    """
    import asyncio

    from backend.services.engine.inference.backtest_service import BacktestService
    from backend.services.engine.inference.data_loader import get_available_dates

    # 1. Resolve model directory
    model_id = request.model_id
    model_dir = None

    # Search in user models
    user_models_root = Path(MODELS_ROOT) / "users"
    for d in user_models_root.rglob(model_id):
        if (d / "metadata.json").exists():
            model_dir = d
            break

    # Search in production
    if model_dir is None:
        prod_dir = Path(MODELS_PRODUCTION)
        for d in prod_dir.rglob(model_id):
            if (d / "metadata.json").exists():
                model_dir = d
                break

    if model_dir is None:
        raise HTTPException(status_code=404, detail=f"模型 {model_id} 未找到")

    # 2. Get available trading dates in range
    data_dir, model_meta = _model_data_context(model_dir)
    available_dates = get_available_dates(
        data_dir=data_dir,
        start_date=request.start_date,
        end_date=request.end_date,
        meta=model_meta,
    )

    if not available_dates:
        raise HTTPException(
            status_code=400,
            detail=f"日期范围 {request.start_date} ~ {request.end_date} 内无可用数据",
        )

    # 3. Sample dates
    interval = max(1, request.sample_interval)
    sampled_dates = available_dates[::interval]

    # Ensure last date is included
    if available_dates[-1] not in sampled_dates:
        sampled_dates.append(available_dates[-1])

    if len(sampled_dates) < 2:
        raise HTTPException(
            status_code=400,
            detail=f"采样后日期不足（仅 {len(sampled_dates)} 天），请扩大日期范围或减小采样间隔",
        )

    # 4. Run backtest
    try:
        backtest_service = BacktestService()
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: backtest_service.run_backtest(
                model_id=model_id,
                dates=sampled_dates,
                horizon=request.horizon,
                model_dir=model_dir,
                data_dir=data_dir,
                sample_interval=interval,
                cost_override=request.cost.to_override() if request.cost else None,
                exclude_limit_moves=request.exclude_limit_moves,
            ),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"回测执行失败: {e}")

    return result


class MultiHorizonBacktestRequest(BaseModel):
    model_id: str = Field(..., description="模型ID（目录名）")
    start_date: str = Field(..., description="回测起始日期 YYYY-MM-DD")
    end_date: str = Field(..., description="回测结束日期 YYYY-MM-DD")
    horizons: list[int] = Field(default=[1, 5, 10, 20], description="预测周期列表")
    sample_interval: int = Field(default=3, description="每隔 N 个交易日采样一次")
    cost: TradingCostParams | None = Field(default=None, description="交易成本覆盖参数")
    exclude_limit_moves: bool = Field(
        default=True, description="剔除信号日触及涨跌停的标的"
    )
    model_config = {"protected_namespaces": ()}


@router.post("/backtest/multi-horizon", summary="多周期对比回测")
async def run_multi_horizon_backtest(
    request: MultiHorizonBacktestRequest,
    current_user: dict = Depends(require_admin),
):
    """对同一模型在多个预测周期（T+1, T+5, T+10, T+20）上进行回测比较。"""
    import asyncio

    from backend.services.engine.inference.backtest_service import BacktestService
    from backend.services.engine.inference.data_loader import get_available_dates

    model_id = request.model_id
    model_dir = None

    user_models_root = Path(MODELS_ROOT) / "users"
    for d in user_models_root.rglob(model_id):
        if (d / "metadata.json").exists():
            model_dir = d
            break

    if model_dir is None:
        prod_dir = Path(MODELS_PRODUCTION)
        for d in prod_dir.rglob(model_id):
            if (d / "metadata.json").exists():
                model_dir = d
                break

    if model_dir is None:
        raise HTTPException(status_code=404, detail=f"模型 {model_id} 未找到")

    data_dir, model_meta = _model_data_context(model_dir)
    available_dates = get_available_dates(
        data_dir=data_dir,
        start_date=request.start_date,
        end_date=request.end_date,
        meta=model_meta,
    )

    if not available_dates:
        raise HTTPException(
            status_code=400,
            detail=f"日期范围 {request.start_date} ~ {request.end_date} 内无可用数据",
        )

    interval = max(1, request.sample_interval)
    sampled_dates = available_dates[::interval]
    if available_dates[-1] not in sampled_dates:
        sampled_dates.append(available_dates[-1])

    if len(sampled_dates) < 2:
        raise HTTPException(
            status_code=400,
            detail=f"采样后日期不足（仅 {len(sampled_dates)} 天）",
        )

    try:
        backtest_service = BacktestService()
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: backtest_service.run_multi_horizon_backtest(
                model_id=model_id,
                dates=sampled_dates,
                horizons=request.horizons,
                model_dir=model_dir,
                data_dir=data_dir,
                sample_interval=interval,
                cost_override=request.cost.to_override() if request.cost else None,
                exclude_limit_moves=request.exclude_limit_moves,
            ),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"多周期回测失败: {e}")

    return result


@router.get("/backtest/trading-dates", summary="获取可用回测日期列表")
async def get_backtest_trading_dates(
    model_id: str = Query(..., description="模型ID，用于选择其绑定的数据源"),
    start: str = Query(..., description="起始日期 YYYY-MM-DD"),
    end: str = Query(..., description="结束日期 YYYY-MM-DD"),
    current_user: dict = Depends(require_admin),
):
    """返回指定日期范围内的所有可用交易日。"""
    from backend.services.engine.inference.data_loader import get_available_dates

    model_dir = next(
        (Path(d) for d in _find_model_directories(MODELS_ROOT) if Path(d).name == model_id), None
    )
    if model_dir is None:
        raise HTTPException(status_code=404, detail=f"模型 {model_id} 未找到")
    data_dir, model_meta = _model_data_context(model_dir)
    dates = get_available_dates(
        data_dir=data_dir, start_date=start, end_date=end, meta=model_meta
    )
    return {"status": "success", "dates": dates, "count": len(dates)}


@router.get("/list-for-backtest", summary="获取可用于回测的模型列表")
async def list_models_for_backtest(
    current_user: dict = Depends(require_admin),
):
    """列出所有已训练完成的模型（含 user 和 system 模型），用于回测选择。"""
    models = []

    # User models
    user_root = Path(MODELS_ROOT) / "users"
    if user_root.exists():
        for meta_file in user_root.rglob("metadata.json"):
            model_dir = meta_file.parent
            try:
                with open(meta_file, encoding="utf-8") as f:
                    meta = json.load(f)
                model_id = model_dir.name
                models.append(
                    {
                        "model_id": model_id,
                        "model_dir": str(model_dir),
                        "framework": meta.get("framework", "unknown"),
                        "feature_count": len(
                            meta.get("feature_columns") or meta.get("features", [])
                        ),
                        "target_horizon_days": meta.get("target_horizon_days", 1),
                        "metrics": meta.get("metrics", {}),
                        "type": "user",
                    }
                )
            except Exception:
                continue

    # Production models
    prod_root = Path(MODELS_PRODUCTION)
    if prod_root.exists():
        for meta_file in prod_root.rglob("metadata.json"):
            model_dir = meta_file.parent
            model_id = model_dir.name
            if any(m["model_id"] == model_id for m in models):
                continue
            try:
                with open(meta_file, encoding="utf-8") as f:
                    meta = json.load(f)
                models.append(
                    {
                        "model_id": model_id,
                        "model_dir": str(model_dir),
                        "framework": meta.get("framework", "unknown"),
                        "feature_count": len(
                            meta.get("feature_columns") or meta.get("features", [])
                        ),
                        "target_horizon_days": meta.get("target_horizon_days", 1),
                        "metrics": meta.get("metrics", {}),
                        "type": "production",
                    }
                )
            except Exception:
                continue

    return {"status": "success", "models": models, "count": len(models)}


@router.get("/backtest/history/{model_id}", summary="获取模型回测历史")
async def get_backtest_history(
    model_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    current_user: dict = Depends(require_admin),
):
    """返回指定模型的回测历史记录列表（最新在前）。"""
    from backend.services.engine.inference.backtest_service import BacktestService

    svc = BacktestService()
    records = svc.list_history(model_id, limit=limit)
    return {"status": "success", "records": records, "count": len(records)}


@router.get("/backtest/history/{model_id}/{run_id}", summary="获取回测详情")
async def get_backtest_detail(
    model_id: str,
    run_id: str,
    current_user: dict = Depends(require_admin),
):
    """返回指定回测运行的完整详情（含逐日数据）。"""
    from backend.services.engine.inference.backtest_service import BacktestService

    svc = BacktestService()
    detail = svc.get_history_detail(model_id, run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"回测记录 {run_id} 未找到")
    return detail


@router.delete("/backtest/history/{model_id}/{run_id}", summary="删除回测记录")
async def delete_backtest_history(
    model_id: str,
    run_id: str,
    current_user: dict = Depends(require_admin),
):
    """删除指定回测记录。"""
    from backend.services.engine.inference.backtest_service import BacktestService

    svc = BacktestService()
    deleted = svc.delete_history(model_id, run_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"回测记录 {run_id} 未找到")
    return {"status": "ok", "deleted": run_id}
