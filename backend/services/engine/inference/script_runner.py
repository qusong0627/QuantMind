"""
InferenceScriptRunner
=====================
执行模型目录中用户编写的 inference.py 推理脚本，解析输出并写库/发布信号。

inference.py 规范
-----------------
调用方式：
    python inference.py --date YYYY-MM-DD

平台注入环境变量：
    DATABASE_URL   PostgreSQL 连接串
    MODEL_DIR      模型目录绝对路径
    TRADE_DATE     推理日期（同 --date 参数）
    OUTPUT_FORMAT  固定值 json

stdout 输出（JSON 数组，每项含 symbol 和 score）：
    [{"symbol": "sh600519", "score": 0.82}, ...]

exit code：
    0  = 成功
    1  = 致命错误
    2  = 数据质量不足，触发兜底模型推理
    其他非零 = 失败
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

import exchange_calendars as xcals
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.services.engine.services.event_stream import EngineSignalStreamPublisher

logger = logging.getLogger(__name__)

# 默认超时 600 秒（10 分钟），可通过环境变量覆盖
_SCRIPT_TIMEOUT_SEC = int(os.getenv("INFERENCE_SCRIPT_TIMEOUT_SEC", "600"))
_DEFAULT_FEATURE_DIM = int(os.getenv("INFERENCE_DEFAULT_FEATURE_DIM", "48"))
_MIN_READY_SYMBOLS = int(os.getenv("INFERENCE_MIN_READY_SYMBOLS", "3000"))
_MIN_READY_RATIO = float(os.getenv("INFERENCE_MIN_READY_RATIO", "0.9"))
_MIN_READY_FLOOR = int(os.getenv("INFERENCE_MIN_READY_FLOOR", "100"))
_PREDICTION_RETENTION_DAYS = int(os.getenv("INFERENCE_PREDICTION_RETENTION_DAYS", "30"))

# Redis 标记键：记录当日推理已完成
_COMPLETED_REDIS_KEY_PREFIX = "qm:inference:completed"


@dataclass
class ExecutionResult:
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    signals_count: int = 0
    run_id: str = ""
    error: str = ""
    signals: list[dict] = field(default_factory=list)
    fallback_used: bool = False  # True = alpha158 兜底脚本实际执行
    fallback_reason: str = ""  # 触发兜底的原因描述
    failure_stage: str = ""  # main_script/fallback_script/output_parse
    active_model_id: str = ""
    active_data_source: str = ""
    data_trade_date: str = ""
    prediction_trade_date: str = ""


class InferenceScriptRunner:
    """执行模型目录中的 inference.py 并处理结果。

    执行顺序：
    1. 主模型推理脚本（默认 inference.py）
       - exit 0 → 成功
       - exit 1 → 致命失败，返回错误
       - exit 2 → 数据质量不足，自动执行兜底模型脚本
    2. 兜底模型推理脚本（默认 inference.py）
       - exit 0 → 兜底成功，结果标记 fallback_used=True
       - 非 0   → 兜底失败，返回错误
    """

    # exit code 2: 数据质量不足，触发兜底
    _EXIT_DATA_QUALITY = 2

    def __init__(
        self,
        models_production: str | None = None,
        *,
        primary_model_dir: str | None = None,
        fallback_model_dir: str | None = None,
        primary_data_dir: str | None = None,
        fallback_data_dir: str | None = None,
        primary_model_id: str | None = None,
        fallback_model_id: str | None = None,
        primary_script_name: str | None = None,
        fallback_script_name: str | None = None,
    ):
        # `models_production` 为历史兼容参数，等价于 primary_model_dir。
        resolved_primary = (
            primary_model_dir
            or models_production
            or os.getenv("MODELS_PRODUCTION", "/app/models/production/model_qlib")
        )
        self.primary_model_dir = Path(resolved_primary)
        self.fallback_model_dir = Path(
            fallback_model_dir
            or os.getenv(
                "MODELS_FALLBACK_PRODUCTION", "/app/models/production/alpha158"
            )
        )
        self.primary_data_dir = self._normalize_provider_uri(
            str(primary_data_dir or os.getenv("QLIB_PRIMARY_DATA_PATH", "db/qlib_data"))
        )
        self.fallback_data_dir = self._normalize_provider_uri(
            str(
                fallback_data_dir
                or os.getenv("QLIB_FALLBACK_DATA_PATH", "db/qlib_data")
            ),
            prefer_alpha158=True,
        )
        self.primary_model_id = str(
            primary_model_id or os.getenv("PRIMARY_MODEL_ID", "model_qlib")
        )
        self.fallback_model_id = str(
            fallback_model_id or os.getenv("FALLBACK_MODEL_ID", "alpha158")
        )
        self.primary_script_name = str(
            primary_script_name or os.getenv("INFERENCE_PRIMARY_SCRIPT", "inference.py")
        )
        self.fallback_script_name = str(
            fallback_script_name
            or os.getenv("INFERENCE_FALLBACK_SCRIPT", "inference.py")
        )

    @staticmethod
    def _normalize_provider_uri(
        provider_uri: str, *, prefer_alpha158: bool = False
    ) -> str:
        """
        规范化 Qlib provider uri，避免相对路径在子进程 cwd 下被错误解析。

        规则：
        1) 若能在候选路径中命中真实目录，返回该绝对路径；
        2) 相对路径默认转换为 /app/<path>；
        3) 兜底场景优先尝试 /app/db/qlib_data。
        """
        raw = str(provider_uri or "").strip()
        if not raw:
            raw = "db/qlib_data"

        candidates: list[Path] = []
        p = Path(raw)
        if p.is_absolute():
            candidates.append(p)
        else:
            candidates.append(Path("/app") / p)
            candidates.append(p)

        if prefer_alpha158:
            candidates = [
                Path("/app/db/qlib_data"),
                Path("db/qlib_data"),
                *candidates,
            ]

        seen = set()
        for c in candidates:
            key = str(c)
            if key in seen:
                continue
            seen.add(key)
            if c.exists():
                return str(c)

        if p.is_absolute():
            return raw
        return str(Path("/app") / p)

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------

    def check_script_exists(self) -> bool:
        """检查主模型推理脚本是否存在。"""
        script = self.primary_model_dir / self.primary_script_name
        return script.is_file()

    def check_fallback_script_exists(self) -> bool:
        """检查兜底模型推理脚本是否存在。"""
        script = self.fallback_model_dir / self.fallback_script_name
        return script.is_file()

    def _resolve_expected_feature_dim(self) -> int:
        """
        解析主模型期望特征维度。
        优先级：
        1) metadata.json 中 feature_count
        2) feature_schema.json 中 features 长度
        3) inference.py 顶部注释中的“XX 特征”
        4) 环境变量默认值（48）
        """
        metadata_path = self.primary_model_dir / "metadata.json"
        if metadata_path.is_file():
            try:
                meta = json.loads(metadata_path.read_text(encoding="utf-8"))
                for key in ("feature_count", "feature_dim", "input_dim"):
                    val = meta.get(key)
                    if isinstance(val, int) and val > 0:
                        return val
                feature_columns = meta.get("feature_columns")
                if isinstance(feature_columns, list) and feature_columns:
                    return len(feature_columns)
                input_spec = meta.get("input_spec")
                if isinstance(input_spec, dict):
                    tensor_shape = input_spec.get("tensor_shape")
                    if isinstance(tensor_shape, list) and len(tensor_shape) >= 3:
                        try:
                            dim = int(tensor_shape[2] or 0)
                            if dim > 0:
                                return dim
                        except Exception:
                            pass
                model_info = meta.get("model_info") if isinstance(meta, dict) else None
                if isinstance(model_info, dict):
                    for key in ("feature_count", "feature_dim", "input_dim"):
                        val = model_info.get(key)
                        if isinstance(val, int) and val > 0:
                            return val
                    feature_columns = model_info.get("feature_columns")
                    if isinstance(feature_columns, list) and feature_columns:
                        return len(feature_columns)
            except Exception:
                pass

        schema_path = self.primary_model_dir / "feature_schema.json"
        if schema_path.is_file():
            try:
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
                if isinstance(schema, dict):
                    for key in ("features", "feature_columns", "columns"):
                        cols = schema.get(key)
                        if isinstance(cols, list) and cols:
                            return len(cols)
            except Exception:
                pass

        main_script = self.primary_model_dir / self.primary_script_name
        if main_script.is_file():
            try:
                text_part = main_script.read_text(encoding="utf-8", errors="ignore")[
                    :4000
                ]
                match = re.search(r"(\d+)\s*特征", text_part)
                if match:
                    dim = int(match.group(1))
                    if dim > 0:
                        return dim
            except Exception:
                pass

        return _DEFAULT_FEATURE_DIM

    def _read_primary_metadata(self) -> dict:
        """读取主模型 metadata.json，失败返回空字典。"""
        meta_path = self.primary_model_dir / "metadata.json"
        if meta_path.is_file():
            try:
                return json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _try_deploy_parquet_template(self, script_path: Path) -> bool:
        """
        当 parquet 模型缺少 inference.py 时，自动从内置模板写入。
        成功返回 True，失败返回 False（不影响主流程）。
        """
        template_path = Path(__file__).parent / "templates" / "inference_parquet.py"
        if not template_path.is_file():
            logger.warning(
                "[InferenceScriptRunner] parquet 推理模板不存在: %s", template_path
            )
            return False
        try:
            script_path.parent.mkdir(parents=True, exist_ok=True)
            script_path.write_text(
                template_path.read_text(encoding="utf-8"), encoding="utf-8"
            )
            logger.info(
                "[InferenceScriptRunner] 已自动写入 parquet 推理脚本: %s", script_path
            )
            return True
        except Exception as exc:
            logger.warning("[InferenceScriptRunner] 自动写入推理脚本失败: %s", exc)
            return False

    def _query_parquet_readiness(self, trade_date: str) -> dict:
        """
        Parquet 数据源就绪检查。
        当模型 metadata.json 中 data_source=parquet 时使用，
        检查对应年份的 parquet 文件是否存在且含有目标日期的数据。
        """
        meta = self._read_primary_metadata()
        # 解析 parquet 数据目录（优先 metadata 中的 data_dir，否则用默认路径）
        parquet_dir = Path(
            meta.get("data_dir")
            or os.getenv("MODEL_TRAINING_DATA_DIR", "/app/db/feature_snapshots")
        )
        year = int(trade_date[:4])
        parquet_path = parquet_dir / f"model_features_{year}.parquet"

        if not parquet_path.exists():
            return {
                "ready": False,
                "detail": f"parquet 文件不存在: {parquet_path}",
            }

        # 快速检查：读取 trade_date 列验证日期存在性（只读 trade_date 列，避免全量加载）
        try:
            import pandas as pd  # noqa: PLC0415

            df = pd.read_parquet(parquet_path, columns=["trade_date"], engine="pyarrow")
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
            rows = int((df["trade_date"] == trade_date).sum())
            ready = rows > 0
            return {
                "ready": ready,
                "detail": (
                    f"parquet={parquet_path.name}, date={trade_date}, rows={rows}"
                    + ("" if ready else " (该日期无数据)")
                ),
            }
        except Exception as exc:
            return {"ready": False, "detail": f"parquet 读取失败: {exc}"}

    def _query_qlib_readiness(self, trade_date: str) -> dict:
        """
        Qlib 二进制数据源就绪检查。
        检查 calendars/day.txt 是否包含目标日期。
        """
        provider_uri = self.primary_data_dir
        calendar_path = Path(provider_uri) / "calendars" / "day.txt"

        if not calendar_path.exists():
            return {
                "ready": False,
                "detail": f"qlib 日历文件不存在: {calendar_path}",
            }

        try:
            content = calendar_path.read_text(encoding="utf-8")
            if trade_date in content:
                return {
                    "ready": True,
                    "detail": f"qlib_data={provider_uri}, date={trade_date} (已在日历中找到)",
                }
            else:
                last_date = (
                    content.strip().splitlines()[-1] if content.strip() else "empty"
                )
                return {
                    "ready": False,
                    "detail": f"qlib_data={provider_uri}, date={trade_date} (未在日历中找到，最后日期={last_date})",
                }
        except Exception as exc:
            return {"ready": False, "detail": f"qlib 日历读取失败: {exc}"}

    @staticmethod
    def _resolve_ready_threshold(total_rows: int) -> int:
        if total_rows <= 0:
            return _MIN_READY_SYMBOLS
        ratio = min(max(_MIN_READY_RATIO, 0.0), 1.0)
        abs_target = min(_MIN_READY_SYMBOLS, total_rows)
        ratio_target = int(math.ceil(total_rows * ratio))
        required = min(abs_target, ratio_target)
        required = max(_MIN_READY_FLOOR, required)
        return min(required, total_rows)

    @staticmethod
    def _resolve_prediction_trade_date(data_trade_date: str) -> str:
        """
        统一口径：
        - data_trade_date：用于读取特征的数据交易日 (T)
        - prediction_trade_date：信号生效交易日 (T+1)
        """
        try:
            import exchange_calendars as xcals

            cal = xcals.get_calendar("XSHG")
            # 将输入日期转换为下一个交易日
            nxt = cal.next_session(data_trade_date)
            return (
                nxt.date().isoformat()
                if hasattr(nxt, "date")
                else str(nxt).split(" ")[0]
            )
        except Exception as e:
            logger.warning(
                f"[InferenceScriptRunner] 计算预测日期失败，回退到 T+1 自然日: {e}"
            )
            # 兜底：如果日历解析失败，至少加 1 天（自然日）
            from datetime import datetime, timedelta

            dt = datetime.strptime(data_trade_date, "%Y-%m-%d")
            return (dt + timedelta(days=1)).strftime("%Y-%m-%d")

    def _query_dimension_readiness(self, trade_date: str, expected_dim: int) -> dict:
        # shared.database 的 SessionLocal 在 asyncpg URL 下会触发 greenlet 错误，
        # 这里显式构造一个同步驱动会话，仅用于就绪度查询链路。
        sync_db_url = os.getenv("DATABASE_URL", "")
        if "+asyncpg" in sync_db_url:
            sync_db_url = sync_db_url.replace("+asyncpg", "+psycopg2")
        if not sync_db_url.startswith("postgresql"):
            sync_db_url = (
                "postgresql+psycopg2://postgres:password@localhost:5432/quantmind"
            )
        sync_engine = create_engine(sync_db_url, pool_pre_ping=True, future=True)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=sync_engine)

        db = SessionLocal()
        try:
            schema_columns = (
                db.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'market_data_daily'
                        """
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
                dim_source = "none"
                dim_expr = "0"

            dim_condition = f"({dim_expr}) >= :expected_dim"

            row = (
                db.execute(
                    text(
                        f"""
                        SELECT
                            COUNT(*) FILTER (WHERE date = :trade_date) AS total_rows,
                            COUNT(*) FILTER (WHERE date = :trade_date AND ({dim_condition})) AS ready_rows
                        FROM market_data_daily
                        """
                    ),
                    {"trade_date": trade_date, "expected_dim": expected_dim},
                )
                .mappings()
                .first()
            )

            total_rows = int((row or {}).get("total_rows") or 0)
            ready_rows = int((row or {}).get("ready_rows") or 0)
            required_ready = self._resolve_ready_threshold(total_rows)
            ready = total_rows > 0 and ready_rows >= required_ready
            detail = (
                f"trade_date={trade_date}, expected_dim>={expected_dim}, "
                f"total_rows={total_rows}, ready_rows={ready_rows}, "
                f"required_ready={required_ready}, min_ready_symbols={_MIN_READY_SYMBOLS}, "
                f"min_ready_ratio={_MIN_READY_RATIO:.2f}, min_ready_floor={_MIN_READY_FLOOR}, "
                f"feature_cols_count={feature_cols_count}, features_json={has_features_json}, dim_source={dim_source}"
            )
            return {"ready": ready, "detail": detail}
        except Exception as exc:
            return {"ready": False, "detail": f"dimension_readiness_query_error={exc}"}
        finally:
            db.close()

    # ------------------------------------------------------------------
    # 兜底执行
    # ------------------------------------------------------------------

    def _get_python_executable(self) -> str:
        """解析容器内正确的 Python 解释器路径。"""
        # 1. 优先检查环境变量
        env_py = os.getenv("PYTHON_EXECUTABLE")
        if env_py and Path(env_py).exists():
            return env_py
        # 2. 检查常用的容器内路径
        for p in [
            "/usr/local/bin/python3",
            "/usr/bin/python3",
            "/usr/local/bin/python",
            "/usr/bin/python",
        ]:
            if Path(p).exists():
                return p
        # 3. 兜底使用 sys.executable
        return sys.executable

    def _get_subprocess_env(self) -> dict:
        """构造子进程运行环境，确保路径和库能被正确找到。"""
        env = os.environ.copy()
        # 确保 /usr/local/bin 在 PATH 中，许多 pip 包安装在这里
        if "/usr/local/bin" not in env.get("PATH", ""):
            env["PATH"] = "/usr/local/bin:" + env.get("PATH", "")

        # 强制设置 PYTHONPATH
        curr_python_path = env.get("PYTHONPATH", "")
        if "/app" not in curr_python_path:
            env["PYTHONPATH"] = f"/app:{curr_python_path}".strip(":")

        return env

    def _execute_fallback(
        self,
        date: str,
        tenant_id: str,
        user_id: str,
        redis_client,
        run_id: str,
        v10_stderr: str,
        fallback_reason: str,
        prediction_trade_date: str,
    ) -> ExecutionResult:
        """执行 inference_alpha158.py 兜底推理脚本。"""
        fallback_path = self.fallback_model_dir / self.fallback_script_name
        if not fallback_path.is_file():
            return ExecutionResult(
                success=False,
                exit_code=self._EXIT_DATA_QUALITY,
                stdout="",
                stderr=v10_stderr,
                error=f"v10 数据质量不足且兜底脚本不存在: {fallback_path}",
                run_id=run_id,
                fallback_used=False,
                fallback_reason=fallback_reason,
                failure_stage="fallback_script",
                active_model_id=self.fallback_model_id,
                active_data_source=self.fallback_data_dir,
                data_trade_date=date,
                prediction_trade_date=prediction_trade_date,
            )

        env = self._get_subprocess_env()
        env.update(
            {
                "MODEL_DIR": str(self.fallback_model_dir),
                "TRADE_DATE": date,
                "OUTPUT_FORMAT": "json",
                "QLIB_PROVIDER_URI": self.fallback_data_dir,
            }
        )

        out_file = self.fallback_model_dir / f"fallback_{run_id}.json"

        try:
            from backend.shared.notification_publisher import publish_notification

            publish_notification(
                user_id="system",
                tenant_id="default",
                title="触发 Alpha158 兜底模型",
                content=f"由于 [{fallback_reason}] 触发了兜底机制，请尽快排查主模型和数据状态。",
                type="system",
                level="error",
            )
        except Exception as e:
            logger.warning("[InferenceScriptRunner] 发布兜底告警通知失败: %s", e)

        python_exec = self._get_python_executable()
        try:
            # 增加环境诊断
            diag_cmd = [
                python_exec,
                "-c",
                "import sys, os; print(f'SUB_PATH: {sys.path}'); import qlib; print(f'QLIB_OK: {qlib.__file__}')",
            ]
            diag_proc = subprocess.run(
                diag_cmd, capture_output=True, text=True, env=env, timeout=10
            )
            logger.info(
                f"[InferenceScriptRunner] 子进程环境诊断: stdout={diag_proc.stdout.strip()}, stderr={diag_proc.stderr.strip()}"
            )

            proc = subprocess.run(
                [
                    python_exec,
                    str(fallback_path),
                    "--date",
                    date,
                    "--output",
                    str(out_file),
                ],
                capture_output=True,
                text=True,
                timeout=_SCRIPT_TIMEOUT_SEC,
                env=env,
                cwd=str(self.fallback_model_dir),
            )
        except subprocess.TimeoutExpired as exc:
            return ExecutionResult(
                success=False,
                exit_code=-1,
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
                error=f"alpha158 兜底脚本超时 ({_SCRIPT_TIMEOUT_SEC}s)",
                run_id=run_id,
                fallback_used=True,
                fallback_reason=fallback_reason,
                failure_stage="fallback_script",
                active_model_id=self.fallback_model_id,
                active_data_source=self.fallback_data_dir,
                data_trade_date=date,
                prediction_trade_date=prediction_trade_date,
            )
        except Exception as exc:
            return ExecutionResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr="",
                error=f"alpha158 兜底脚本启动失败: {exc}",
                run_id=run_id,
                fallback_used=True,
                fallback_reason=fallback_reason,
                failure_stage="fallback_script",
                active_model_id=self.fallback_model_id,
                active_data_source=self.fallback_data_dir,
                data_trade_date=date,
                prediction_trade_date=prediction_trade_date,
            )

        fb_stdout = proc.stdout or ""
        fb_stderr = (
            v10_stderr + "\n--- alpha158 fallback ---\n" + (proc.stderr or "")
        ).strip()
        fb_exitcode = proc.returncode

        if fb_exitcode != 0:
            logger.error(
                f"[InferenceScriptRunner] alpha158 兜底脚本失败 exit={fb_exitcode}, run_id={run_id}"
            )
            return ExecutionResult(
                success=False,
                exit_code=fb_exitcode,
                stdout=fb_stdout,
                stderr=fb_stderr,
                error=f"alpha158 兜底脚本返回非零退出码: {fb_exitcode}",
                run_id=run_id,
                fallback_used=True,
                fallback_reason=fallback_reason,
                failure_stage="fallback_script",
                active_model_id=self.fallback_model_id,
                active_data_source=self.fallback_data_dir,
                data_trade_date=date,
                prediction_trade_date=prediction_trade_date,
            )

        signals = self._parse_signals(str(out_file))
        if signals is None:
            return ExecutionResult(
                success=False,
                exit_code=0,
                stdout=fb_stdout,
                stderr=fb_stderr,
                error="alpha158 兜底未能写入合法的 JSON 信号数组",
                run_id=run_id,
                fallback_used=True,
                fallback_reason=fallback_reason,
                failure_stage="fallback_script",
                active_model_id=self.fallback_model_id,
                active_data_source=self.fallback_data_dir,
                data_trade_date=date,
                prediction_trade_date=prediction_trade_date,
            )

        logger.info(
            f"[InferenceScriptRunner] alpha158 兜底成功，{len(signals)} 条信号, run_id={run_id}"
        )
        self._persist_and_publish(
            run_id, prediction_trade_date, tenant_id, user_id, signals
        )

        if redis_client is not None:
            try:
                redis_client.set(
                    f"{_COMPLETED_REDIS_KEY_PREFIX}:{prediction_trade_date}",
                    run_id,
                    ex=86400,
                )
            except Exception as exc:
                logger.warning(f"[InferenceScriptRunner] 写 Redis 完成标记失败: {exc}")

        return ExecutionResult(
            success=True,
            exit_code=0,
            stdout=fb_stdout,
            stderr=fb_stderr,
            signals_count=len(signals),
            run_id=run_id,
            signals=signals,
            fallback_used=True,
            fallback_reason=fallback_reason,
            active_model_id=self.fallback_model_id,
            active_data_source=self.fallback_data_dir,
            data_trade_date=date,
            prediction_trade_date=prediction_trade_date,
        )

    def execute(
        self,
        date: str,
        tenant_id: str = "default",
        user_id: str = "system",
        redis_client=None,
    ) -> ExecutionResult:
        """
        执行 inference.py 脚本，解析信号输出，写库并发布 Redis Stream。

        Parameters
        ----------
        date        : 推理日期，格式 YYYY-MM-DD
        tenant_id   : 租户 ID（用于写库和信号流）
        user_id     : 用户 ID
        redis_client: 可选 Redis 客户端，用于写完成标记
        """
        script_path = self.primary_model_dir / self.primary_script_name
        prediction_trade_date = self._resolve_prediction_trade_date(date)
        if not script_path.is_file():
            # parquet 数据源模型：自动写入模板脚本，无需手动部署
            primary_meta = self._read_primary_metadata()
            data_source = str(primary_meta.get("data_source") or "").lower()
            if data_source == "parquet" and self._try_deploy_parquet_template(
                script_path
            ):
                logger.info(
                    "[InferenceScriptRunner] parquet 模型自动注入推理脚本: %s",
                    script_path,
                )
            else:
                run_id = f"run_{date.replace('-', '')}_{uuid.uuid4().hex[:8]}"
                fallback_reason = f"主模型推理脚本不存在: {script_path}"
                logger.warning(
                    "[InferenceScriptRunner] 主模型脚本缺失，触发 alpha158 兜底, run_id=%s, reason=%s",
                    run_id,
                    fallback_reason,
                )
                return self._execute_fallback(
                    date=date,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    redis_client=redis_client,
                    run_id=run_id,
                    v10_stderr=fallback_reason,
                    fallback_reason=fallback_reason,
                    prediction_trade_date=prediction_trade_date,
                )

        run_id = f"run_{date.replace('-', '')}_{uuid.uuid4().hex[:8]}"
        logger.info(
            f"[InferenceScriptRunner] 启动推理脚本, run_id={run_id}, date={date}"
        )

        expected_dim = self._resolve_expected_feature_dim()

        # 判断数据源：针对不同存储引擎执行对应的就绪检查
        primary_meta = self._read_primary_metadata()
        data_source = str(primary_meta.get("data_source") or "").lower()

        if data_source == "parquet":
            readiness = self._query_parquet_readiness(trade_date=date)
        elif data_source in ("qlib", "qlib_bin", "bin"):
            readiness = self._query_qlib_readiness(trade_date=date)
        else:
            # 默认回退到数据库维度检查（兼容旧模型）
            readiness = self._query_dimension_readiness(
                trade_date=date, expected_dim=expected_dim
            )

        logger.info(
            "[InferenceScriptRunner] 数据源就绪检查: source=%s, ready=%s, detail=%s",
            data_source or "default_db",
            readiness.get("ready"),
            readiness.get("detail"),
        )

        if not readiness.get("ready", False):
            fallback_reason = f"主模型维度门禁未通过: {readiness.get('detail', 'N/A')}"
            logger.warning(
                "[InferenceScriptRunner] 主模型数据维度不足，触发 alpha158 兜底, run_id=%s, reason=%s",
                run_id,
                fallback_reason,
            )
            return self._execute_fallback(
                date=date,
                tenant_id=tenant_id,
                user_id=user_id,
                redis_client=redis_client,
                run_id=run_id,
                v10_stderr=fallback_reason,
                fallback_reason=fallback_reason,
                prediction_trade_date=prediction_trade_date,
            )

        # 注入平台环境变量
        env = self._get_subprocess_env()
        env.update(
            {
                "MODEL_DIR": str(self.primary_model_dir),
                "TRADE_DATE": date,
                "OUTPUT_FORMAT": "json",
                "QLIB_PROVIDER_URI": self.primary_data_dir,
            }
        )

        # 执行子进程
        out_file = self.primary_model_dir / f"main_{run_id}.json"
        python_exec = self._get_python_executable()
        try:
            proc = subprocess.run(
                [
                    python_exec,
                    str(script_path),
                    "--date",
                    date,
                    "--output",
                    str(out_file),
                ],
                capture_output=True,
                text=True,
                timeout=_SCRIPT_TIMEOUT_SEC,
                env=env,
                cwd=str(self.primary_model_dir),
            )
        except subprocess.TimeoutExpired as exc:
            logger.error(
                f"[InferenceScriptRunner] 脚本超时 ({_SCRIPT_TIMEOUT_SEC}s), run_id={run_id}"
            )
            return ExecutionResult(
                success=False,
                exit_code=-1,
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
                error=f"脚本执行超时（{_SCRIPT_TIMEOUT_SEC}s）",
                run_id=run_id,
                failure_stage="main_script",
                active_model_id=self.primary_model_id,
                active_data_source=self.primary_data_dir,
            )
        except Exception as exc:
            logger.error(f"[InferenceScriptRunner] 脚本启动失败: {exc}")
            return ExecutionResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr="",
                error=str(exc),
                run_id=run_id,
                failure_stage="main_script",
                active_model_id=self.primary_model_id,
                active_data_source=self.primary_data_dir,
            )

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        exit_code = proc.returncode

        if exit_code != 0:
            # exit code 2 = 数据质量不足 → 尝试 alpha158 兜底
            if exit_code == self._EXIT_DATA_QUALITY:
                fallback_reason = (
                    stderr.strip().splitlines()[-1]
                    if stderr.strip()
                    else "v10 数据质量不足"
                )
                logger.warning(
                    f"[InferenceScriptRunner] v10 数据质量不足 (exit=2)，启动 alpha158 兜底, run_id={run_id}"
                )
                return self._execute_fallback(
                    date=date,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    redis_client=redis_client,
                    run_id=run_id,
                    v10_stderr=stderr,
                    fallback_reason=fallback_reason,
                    prediction_trade_date=prediction_trade_date,
                )

            logger.error(
                f"[InferenceScriptRunner] 脚本异常退出 exit_code={exit_code}, run_id={run_id}\nstderr: {stderr[:500]}"
            )
            return ExecutionResult(
                success=False,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                error=f"脚本返回非零退出码: {exit_code}",
                run_id=run_id,
                failure_stage="main_script",
                active_model_id=self.primary_model_id,
                active_data_source=self.primary_data_dir,
                data_trade_date=date,
                prediction_trade_date=prediction_trade_date,
            )

        # 解析信号
        signals = self._parse_signals(str(out_file))
        if signals is None:
            return ExecutionResult(
                success=False,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                error='输出文件不存在或不是合法的 JSON 信号数组，期望格式：[{"symbol":"...","score":0.0},...]',
                run_id=run_id,
                failure_stage="output_parse",
                active_model_id=self.primary_model_id,
                active_data_source=self.primary_data_dir,
            )

        logger.info(
            f"[InferenceScriptRunner] 解析到 {len(signals)} 条信号, run_id={run_id}"
        )

        # 写库 + 发布 Redis Stream
        self._persist_and_publish(
            run_id, prediction_trade_date, tenant_id, user_id, signals
        )

        # 写 Redis 完成标记
        if redis_client is not None:
            try:
                redis_client.set(
                    f"{_COMPLETED_REDIS_KEY_PREFIX}:{prediction_trade_date}",
                    run_id,
                    ex=86400,
                )
            except Exception as exc:
                logger.warning(
                    f"[InferenceScriptRunner] 写 Redis 完成标记失败（不影响主流程）: {exc}"
                )

        return ExecutionResult(
            success=True,
            exit_code=0,
            stdout=stdout,
            stderr=stderr,
            signals_count=len(signals),
            run_id=run_id,
            signals=signals,
            active_model_id=self.primary_model_id,
            active_data_source=self.primary_data_dir,
            data_trade_date=date,
            prediction_trade_date=prediction_trade_date,
        )

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_signals(file_path: str) -> list[dict] | None:
        """从指定的 json 文件读取信号数组，解析成功后自动删除文件。返回 None 表示失败。"""
        p = Path(file_path)
        if not p.is_file():
            return None
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError:
            return None
        finally:
            try:
                p.unlink()
            except Exception:
                pass

        if not isinstance(data, list):
            return None
        valid = []
        for item in data:
            if isinstance(item, dict) and "symbol" in item and "score" in item:
                try:
                    valid.append(
                        {"symbol": str(item["symbol"]), "score": float(item["score"])}
                    )
                except (ValueError, TypeError):
                    pass
        return valid if valid else None

    def _persist_and_publish(
        self,
        run_id: str,
        prediction_trade_date: str,
        tenant_id: str,
        user_id: str,
        signals: list[dict],
    ) -> None:
        """
        将推理结果写入 engine_signal_scores 并发布到 Redis Stream。

        存储策略：**覆盖**——先删除同一预测交易日的旧数据，再写入本次结果。
        保证每个交易日只保留最新一次推理，下游查询始终得到单一一致的信号集。
        """
        symbols = [s["symbol"] for s in signals]
        scores = [s["score"] for s in signals]
        feature_dim = max(1, self._resolve_expected_feature_dim())

        # shared.database 的 SessionLocal 在 asyncpg URL 下会触发 greenlet 错误，
        # 这里显式构造一个同步驱动会话，仅用于脚本写库链路。
        sync_db_url = os.getenv("DATABASE_URL", "")
        if "+asyncpg" in sync_db_url:
            sync_db_url = sync_db_url.replace("+asyncpg", "+psycopg2")
        if not sync_db_url.startswith("postgresql"):
            sync_db_url = (
                "postgresql+psycopg2://postgres:password@localhost:5432/quantmind"
            )
        sync_engine = create_engine(sync_db_url, pool_pre_ping=True, future=True)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=sync_engine)

        db = SessionLocal()
        try:
            prediction_day = date.fromisoformat(prediction_trade_date)
            retention_floor = (
                prediction_day - timedelta(days=max(1, _PREDICTION_RETENTION_DAYS))
            ).isoformat()

            # ── Step 0.1: 清理超出保留期的历史数据（默认 30 天）────────────
            db.execute(
                text("""
                    DELETE FROM engine_signal_scores
                    WHERE tenant_id = :tenant_id
                      AND user_id = :user_id
                      AND model_version = 'inference_script'
                      AND trade_date < :retention_floor
                """),
                {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "retention_floor": retention_floor,
                },
            )
            db.execute(
                text("""
                    DELETE FROM engine_feature_runs
                    WHERE tenant_id = :tenant_id
                      AND user_id = :user_id
                      AND source = 'inference_script'
                      AND trade_date < :retention_floor
                """),
                {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "retention_floor": retention_floor,
                },
            )

            # ── Step 0.2: 删除当日旧推理结果（覆盖策略）───────────────────
            db.execute(
                text("""
                    DELETE FROM engine_signal_scores
                    WHERE trade_date    = :trade_date
                      AND tenant_id    = :tenant_id
                      AND user_id      = :user_id
                      AND model_version = 'inference_script'
                """),
                {
                    "trade_date": prediction_trade_date,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                },
            )
            # 同步清除旧 feature_runs 记录（保留最新 run_id）
            db.execute(
                text("""
                    DELETE FROM engine_feature_runs
                    WHERE trade_date = :trade_date
                      AND tenant_id  = :tenant_id
                      AND user_id    = :user_id
                      AND source     = 'inference_script'
                """),
                {
                    "trade_date": prediction_trade_date,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                },
            )
            logger.info(
                f"[InferenceScriptRunner] 已清除 {prediction_trade_date} 旧推理数据, run_id={run_id}"
            )

            # ── Step 1: 写入本次 feature run 记录 ────────────────────────
            db.execute(
                text("""
                    INSERT INTO engine_feature_runs (
                        run_id, tenant_id, user_id, trade_date, model_name, model_version,
                        feature_version, feature_dim, status, expected_symbols, ready_symbols,
                        source, created_at, updated_at
                    ) VALUES (
                        :run_id, :tenant_id, :user_id, :trade_date,
                        'inference_script', 'inference_script',
                        'script_v1', :feature_dim, 'signal_ready',
                        :n, :n, 'inference_script', NOW(), NOW()
                    )
                    ON CONFLICT (run_id) DO UPDATE SET
                        status = 'signal_ready', updated_at = NOW()
                """),
                {
                    "run_id": run_id,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "trade_date": prediction_trade_date,
                    "n": len(signals),
                    "feature_dim": feature_dim,
                },
            )

            # ── Step 2: 批量写入信号评分（含 signal_side 和 expected_price）──────────
            import redis as redis_lib

            redis_host = os.getenv("REDIS_HOST", "quantmind-redis")
            redis_port = int(os.getenv("REDIS_PORT", "6379"))
            redis_password = os.getenv("REDIS_PASSWORD", "")
            redis_db = int(os.getenv("REDIS_DB_MARKET", "3"))
            try:
                quote_redis = redis_lib.Redis(
                    host=redis_host,
                    port=redis_port,
                    password=redis_password,
                    db=redis_db,
                    decode_responses=True,
                    socket_timeout=2,
                )
                quote_redis.ping()
                logger.info(
                    f"[InferenceScriptRunner] 已连接行情 Redis: {redis_host}:{redis_port}"
                )
            except Exception as redis_err:
                logger.warning(
                    f"[InferenceScriptRunner] 无法连接行情 Redis: {redis_err}, 价格将缺失"
                )
                quote_redis = None

            score_sql = text("""
                INSERT INTO engine_signal_scores (
                    run_id, tenant_id, user_id, trade_date, symbol,
                    model_version, feature_version,
                    light_score, tft_score, fusion_score, risk_weight, regime,
                    signal_side, expected_price, created_at
                ) VALUES (
                    :run_id, :tenant_id, :user_id, :trade_date, :symbol,
                    'inference_script', 'script_v1',
                    NULL, NULL, :score, 1.0, 'normal',
                    :signal_side, :expected_price, NOW()
                )
                ON CONFLICT (tenant_id, user_id, trade_date, symbol, model_version, feature_version, run_id)
                DO UPDATE SET
                    fusion_score = EXCLUDED.fusion_score,
                    signal_side = EXCLUDED.signal_side,
                    expected_price = EXCLUDED.expected_price
            """)
            for sym, score in zip(symbols, scores):
                expected_price = None
                signal_side = "BUY" if score > 0 else "HOLD"
                if quote_redis:
                    try:
                        raw_sym = (
                            sym.replace("SH", "").replace("SZ", "").replace("BJ", "")
                        )
                        if sym.startswith("SH"):
                            redis_key = f"stock:{raw_sym}.SH"
                        elif sym.startswith("SZ"):
                            redis_key = f"stock:{raw_sym}.SZ"
                        elif sym.startswith("BJ") or sym.startswith("920"):
                            redis_key = f"stock:{raw_sym}.BJ"
                        else:
                            redis_key = f"stock:{sym}"
                        now_price = quote_redis.hget(redis_key, "Now")
                        if now_price:
                            expected_price = float(now_price)
                    except Exception as e:
                        logger.debug(
                            f"[InferenceScriptRunner] 获取 {sym} 价格失败: {e}"
                        )
                db.execute(
                    score_sql,
                    {
                        "run_id": run_id,
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "trade_date": prediction_trade_date,
                        "symbol": sym,
                        "score": score,
                        "signal_side": signal_side,
                        "expected_price": expected_price,
                    },
                )
            if quote_redis:
                try:
                    quote_redis.close()
                except Exception:
                    pass
            db.commit()
            logger.info(
                f"[InferenceScriptRunner] 写入 {len(signals)} 条信号, run_id={run_id}"
            )
        except Exception as exc:
            logger.error(f"[InferenceScriptRunner] 写库失败: {exc}")
            db.rollback()
        finally:
            db.close()
            try:
                sync_engine.dispose()
            except Exception:
                pass

        # 发布信号到 Redis Stream（失败不影响主流程）
        try:
            signal_events = [
                {
                    "signal_id": f"{run_id}-{idx:04d}",
                    "client_order_id": f"coid-{run_id}-{idx:04d}",
                    "symbol": sym,
                    "score": score,
                    "quantity": 100,
                    "price": 0.0,
                }
                for idx, (sym, score) in enumerate(zip(symbols, scores))
            ]
            publisher = EngineSignalStreamPublisher()
            publisher.mark_latest_run(
                tenant_id=tenant_id,
                user_id=str(user_id),
                run_id=run_id,
            )
            published = publisher.publish_signals(
                tenant_id=tenant_id,
                user_id=str(user_id),
                run_id=run_id,
                trace_id=run_id,
                signal_source="inference_script",
                signals=signal_events,
            )
            logger.info(
                f"[InferenceScriptRunner] 已发布 {published} 条信号, run_id={run_id}"
            )
        except Exception as exc:
            logger.warning(
                f"[InferenceScriptRunner] 信号发布失败（不影响 DB 结果）: {exc}"
            )
