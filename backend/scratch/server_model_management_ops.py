import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    import exchange_calendars as xcals
except Exception:
    xcals = None

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from backend.services.api.user_app.middleware.auth import require_admin
from backend.services.engine.inference.script_runner import InferenceScriptRunner
from backend.shared.database_manager_v2 import get_session
from backend.shared.redis_sentinel_client import get_redis_sentinel_client

try:
    from backend.services.engine.qlib_app.celery_config import celery_app
except ImportError:
    celery_app = None

from .model_management_utils import (
    FEATURE_SNAPSHOT_DIR,
    MODELS_PRODUCTION,
    MODELS_ROOT,
    _enrich_feature_catalog_with_data_coverage,
    _find_model_directories,
    _load_feature_catalog_from_db,
    _load_feature_catalog_from_file,
    _resolve_expected_feature_dim,
    _resolve_inference_dates_with_calendar,
    _resolve_ready_threshold,
    _scan_model_directory,
    _scan_feature_snapshots_status,
)

router = APIRouter()


@router.get("/scan", summary="扫描本地模型目录")
async def scan_model_directories(
    current_user: dict = Depends(require_admin),
):
    """
    自动扫描 models/ 下所有有效模型目录，聚合 metadata.json、
    workflow_config.yaml、best_params.yaml 等元数据文件，返回结构化列表。
    """
    try:
        dirs = _find_model_directories(MODELS_ROOT)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"扫描目录失败: {e}") from e

    results = []
    for d in dirs:
        try:
            results.append(_scan_model_directory(d))
        except Exception as e:
            results.append({"model_id": Path(d).name, "dir_path": d, "error": str(e)})

    return {"total": len(results), "models": results}


@router.get("/feature-catalog", summary="获取模型训练特征字典（动态）")
async def get_model_feature_catalog(
    current_user: dict = Depends(require_admin),
):
    """
    返回训练页第一步所需的特征分类与字段列表：
    - 优先读取 PostgreSQL 特征注册表
    - 若注册表未初始化，回退到 config/features/*.json
    """
    _ = current_user
    try:
        catalog = await _load_feature_catalog_from_db()
    except Exception:
        catalog = None

    if catalog:
        return _enrich_feature_catalog_with_data_coverage(catalog)

    fallback = _load_feature_catalog_from_file()
    if fallback:
        return _enrich_feature_catalog_with_data_coverage(fallback)

    raise HTTPException(
        status_code=404, detail="未找到可用的特征字典（DB/文件均不可用）"
    )


@router.get("/data-status", summary="查看当前数据状态（Qlib + 特征快照）")
async def get_data_status(
    refresh: bool = Query(False, description="是否强制刷新（后台异步）"),
    current_user: dict = Depends(require_admin),
):
    """
    管理后台数据管理接口：
    - 优先从 Redis 获取缓存结果
    - Qlib 文件数据（calendar/instruments/features）状态
    - feature_snapshots 目录下的 parquet 文件状态
    """
    _ = current_user
    redis = None
    try:
        redis = get_redis_sentinel_client()
    except Exception:
        pass

    # 1. 如果不是强制刷新，尝试读取缓存
    if not refresh and redis:
        try:
            cached = redis.get("qm:admin:data_status")
            if cached:
                result = json.loads(cached)
                result["from_cache"] = True
                return result
        except Exception as e:
            print(f"Redis cache read failed: {e}")

    # 2. 如果强制刷新，或者没缓存，则触发后台任务
    if celery_app:
        try:
            celery_app.send_task("engine.tasks.get_data_status_task")
        except Exception as e:
            print(f"Failed to trigger background task: {e}")

    # 3. 实时辅助扫描（作为 fallback 或首次加载的快速反馈）
    now_local = datetime.now(ZoneInfo("Asia/Shanghai"))

    # 获取目标日期规则（兼容非交易日：周末/节假日）
    cal_xshg = xcals.get_calendar("XSHG") if xcals else None

    def _latest_trading_session(ref_date) -> date:
        """返回 ref_date 当天或之前最近的一个交易日。"""
        if cal_xshg is None:
            return ref_date
        import pandas as pd

        ref_ts = pd.Timestamp(ref_date)
        past = cal_xshg.sessions[cal_xshg.sessions <= ref_ts]
        return past[-1].date() if len(past) else ref_date

    today = now_local.date()
    if now_local.time() < datetime.strptime("09:30", "%H:%M").time():
        # 未到开盘时间，取昨天（或更早）的最后一个交易日
        import pandas as pd

        trade_date_obj = _latest_trading_session(
            pd.Timestamp(today) - pd.Timedelta(days=1)
        )
    else:
        # 已过开盘时间，取今天或更早的最后一个交易日
        trade_date_obj = _latest_trading_session(today)
    trade_date = trade_date_obj.isoformat()

    # ========== Qlib 数据状态扫描 ==========
    qlib_data_dir = Path(os.getcwd()) / "db" / "qlib_data"
    calendars_path = qlib_data_dir / "calendars" / "day.txt"
    instruments_all_path = qlib_data_dir / "instruments" / "all.txt"
    features_root = qlib_data_dir / "features"

    qlib_info: dict[str, Any] = {
        "qlib_dir": str(qlib_data_dir),
        "exists": qlib_data_dir.exists() and qlib_data_dir.is_dir(),
        "calendar_total_days": 0,
        "calendar_start_date": None,
        "calendar_last_date": None,
        "instruments": {"total": 0, "sh": 0, "sz": 0, "bj": 0, "other": 0},
        "feature_dirs_total": 0,
        "feature_dirs_sh_sz_bj": 0,
        "latest_date_coverage": {
            "target_date": None,
            "at_target_count": 0,
            "older_count": 0,
            "invalid_count": 0,
        },
    }

    calendar: list[str] = []
    if calendars_path.exists():
        try:
            calendar = [
                x.strip()
                for x in calendars_path.read_text(encoding="utf-8").splitlines()
                if x.strip()
            ]
            if calendar:
                qlib_info["calendar_total_days"] = len(calendar)
                qlib_info["calendar_start_date"] = calendar[0]
                qlib_info["calendar_last_date"] = calendar[-1]
                qlib_info["latest_date_coverage"]["target_date"] = calendar[-1]
        except Exception:
            pass

    if instruments_all_path.exists():
        try:
            for line in instruments_all_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                code = line.split()[0].strip().upper()
                qlib_info["instruments"]["total"] += 1
                if code.startswith("SH"):
                    qlib_info["instruments"]["sh"] += 1
                elif code.startswith("SZ"):
                    qlib_info["instruments"]["sz"] += 1
                elif code.startswith("BJ"):
                    qlib_info["instruments"]["bj"] += 1
                else:
                    qlib_info["instruments"]["other"] += 1
        except Exception:
            pass

    if features_root.exists() and features_root.is_dir():
        feature_dirs = [p for p in features_root.iterdir() if p.is_dir()]
        qlib_info["feature_dirs_total"] = len(feature_dirs)
        qlib_info["sync_partial"] = True  # 标记为部分同步结果

    # ========== Feature Snapshots 状态扫描 ==========
    feature_snapshots_info = _scan_feature_snapshots_status(
        target_date=trade_date,
        topn=20,
    )

    return {
        "checked_at": now_local.isoformat(),
        "trade_date": trade_date,
        "qlib_data": qlib_info,
        "feature_snapshots": feature_snapshots_info,
        "async_trigger": bool(celery_app),
        "message": "数据正在后台扫描中，请稍后刷新"
        if not refresh
        else "已触发强制刷新任务",
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
