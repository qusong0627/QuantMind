import ast
import asyncio
import hashlib
import logging
import os
import sys
import uuid
import re
import time
from typing import Dict, Any, Optional, List

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logger = logging.getLogger("AI-IDE-Executor")
router = APIRouter()


class SyntaxCheckRequest(BaseModel):
    code: str | None = None
    content: str | None = None


@router.post("/check-syntax")
async def check_syntax(request: Request, item: SyntaxCheckRequest):
    """
    检查 Python 代码语法是否正确。
    """
    try:
        code = item.code or item.content
        if not code:
            return {"valid": True}  # 空代码视为通过

        ast.parse(code)
        return {"valid": True}
    except SyntaxError as e:
        return {
            "valid": False,
            "error": str(e),
            "line": e.lineno,
            "offset": e.offset,
            "text": e.text,
        }
    except Exception as e:
        return {"valid": False, "error": f"Internal error during syntax check: {e}"}


from backend.shared.strategy_storage import get_strategy_storage_service

# 存储执行任务状态
jobs = {}

_DEFAULT_IMAGE = os.getenv("AI_IDE_RUNNER_IMAGE", "quantmind-oss:latest")
_IMAGE = _DEFAULT_IMAGE
_NETWORK = os.getenv("AI_IDE_DOCKER_NETWORK", "quantmind_quantmind-net")
_SMOKE_CACHE_TTL = int(os.getenv("AI_IDE_SMOKE_CACHE_TTL_SECONDS", "1800"))
_SMOKE_ALLOW_PULL = os.getenv(
    "AI_IDE_SMOKE_ALLOW_PULL", "true"
).strip().lower() not in {"0", "false", "no"}
_SMOKE_IMPORTS = [
    item.strip()
    for item in os.getenv("AI_IDE_SMOKE_IMPORTS", "numpy,pandas").split(",")
    if item.strip()
]
_SMOKE_OPTIONAL_IMPORTS = [
    item.strip()
    for item in os.getenv("AI_IDE_SMOKE_OPTIONAL_IMPORTS", "").split(",")
    if item.strip()
]
_SMOKE_CACHE: dict[str, dict[str, Any]] = {}

# 使用共享卷目录，以便 Docker 宿主机能看到并挂载到下级容器
TMP_ROOT = os.getenv("AI_IDE_TEMP_DIR", "/app/db/ai_ide_tmp")
HOST_PROJECT_PATH = os.getenv("HOST_PROJECT_PATH", "/home/quantmind")


class StartRequest(BaseModel):
    file_id: str | None = None
    path: str | None = None  # 兼容前端字段，通常即 ID
    content: str | None = None  # 运行未保存的代码
    code: str | None = None  # 兼容前端字段
    filename: str | None = None  # 辅助文件名
    runner_image: str | None = None  # 可显式切换临时验证镜像


class SmokeImageRequest(BaseModel):
    image: str | None = None
    strict: bool = False
    imports: list[str] | None = None
    optional_imports: list[str] | None = None
    cache_bypass: bool = False


def _is_docstring_expr(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(getattr(node, "value", None), ast.Constant)
        and isinstance(node.value.value, str)
    )


def _is_main_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if (
        not isinstance(test, ast.Compare)
        or len(test.ops) != 1
        or len(test.comparators) != 1
    ):
        return False
    left = test.left
    comparator = test.comparators[0]
    return (
        isinstance(left, ast.Name)
        and left.id == "__name__"
        and isinstance(test.ops[0], ast.Eq)
        and isinstance(comparator, ast.Constant)
        and comparator.value == "__main__"
    )


def _analyze_execution_entrypoint(code: str) -> dict[str, Any]:
    tree = ast.parse(code)
    info = {
        "has_main_guard": False,
        "has_top_level_exec": False,
        "function_names": [],
        "class_names": [],
        "has_strategy_config": False,
        "has_get_strategy_config": False,
        "has_get_strategy_instance": False,
    }

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            info["function_names"].append(node.name)
            if node.name == "get_strategy_config":
                info["has_get_strategy_config"] = True
            if node.name == "get_strategy_instance":
                info["has_get_strategy_instance"] = True
            continue

        if isinstance(node, ast.ClassDef):
            info["class_names"].append(node.name)
            continue

        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "STRATEGY_CONFIG":
                    info["has_strategy_config"] = True
                    break
            continue

        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "STRATEGY_CONFIG"
        ):
            info["has_strategy_config"] = True
            continue

        if _is_docstring_expr(node):
            continue
        if _is_main_guard(node):
            info["has_main_guard"] = True
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Pass)):
            continue

        info["has_top_level_exec"] = True

    # 更新 runnable 判断：模块型策略也视为可运行
    info["runnable"] = bool(
        info["has_main_guard"]
        or info["has_top_level_exec"]
        or "main" in info["function_names"]
        or "run" in info["function_names"]
        or info["has_strategy_config"]
        or info["has_get_strategy_config"]
        or info["has_get_strategy_instance"]
    )
    return info


def _build_runnable_error(info: dict[str, Any]) -> str:
    details: list[str] = []
    if info.get("has_strategy_config"):
        details.append("已检测到 STRATEGY_CONFIG")
    if info.get("has_get_strategy_config"):
        details.append("已检测到 get_strategy_config()")
    if info.get("has_get_strategy_instance"):
        details.append("已检测到 get_strategy_instance()")
    if info.get("class_names"):
        details.append(f"策略类: {', '.join(info['class_names'][:3])}")
    if info.get("function_names"):
        details.append(f"函数: {', '.join(info['function_names'][:5])}")

    # 如果是模块型策略，不再报错，而是提示将使用兼容模式
    if (
        info.get("has_strategy_config")
        or info.get("has_get_strategy_config")
        or info.get("has_get_strategy_instance")
    ):
        return (
            "当前策略文件是模块型策略，AI-IDE 将使用回测中心兼容模式执行。"
            f"（{'；'.join(details)}）"
        )

    suffix = f"（{'；'.join(details)}）" if details else ""
    return (
        "当前策略文件是模块型策略或配置文件，不包含可直接执行入口。"
        "AI-IDE 运行器只会执行脚本入口（main/run 或 if __name__ == '__main__'），"
        "不会自动替你启动 Qlib 回测。"
        f"{suffix}"
    )


def _normalize_image_ref(image: str | None) -> str:
    normalized = str(image or "").strip()
    return normalized or _DEFAULT_IMAGE


def _build_smoke_script(
    mandatory_imports: list[str], optional_imports: list[str], strict: bool
) -> str:
    mandatory_repr = repr([item for item in mandatory_imports if item])
    optional_repr = repr([item for item in optional_imports if item])
    strict_flag = "True" if strict else "False"
    return f"""import importlib
import platform
import sys
import traceback

MANDATORY_IMPORTS = {mandatory_repr}
OPTIONAL_IMPORTS = {optional_repr}
STRICT = {strict_flag}

print(f"[SMOKE] python={{sys.version.replace(chr(10), ' ')}}")
print(f"[SMOKE] platform={{platform.platform()}}")
print(f"[SMOKE] strict={{STRICT}}")

for module_name in MANDATORY_IMPORTS:
    try:
        importlib.import_module(module_name)
        print(f"[SMOKE] import ok: {{module_name}}")
    except Exception as exc:
        print(f"[ERROR] mandatory import failed: {{module_name}}: {{exc}}")
        traceback.print_exc()
        raise

failed_optional = []
for module_name in OPTIONAL_IMPORTS:
    try:
        importlib.import_module(module_name)
        print(f"[SMOKE] optional import ok: {{module_name}}")
    except Exception as exc:
        failed_optional.append(module_name)
        print(f"[WARN] optional import failed: {{module_name}}: {{exc}}")

if STRICT and failed_optional:
    raise RuntimeError(f"optional imports failed: {{', '.join(failed_optional)}}")

print("[SMOKE] smoke test completed")
"""


def _smoke_cache_key(
    image_id: str,
    mandatory_imports: list[str],
    optional_imports: list[str],
    strict: bool,
) -> str:
    payload = "|".join(
        [
            str(image_id or "").strip(),
            ",".join(sorted([item for item in mandatory_imports if item])),
            ",".join(sorted([item for item in optional_imports if item])),
            "strict" if strict else "loose",
        ]
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


async def _cleanup_job_artifacts(job_info: dict[str, Any]) -> None:
    for key in ("file", "runner", "smoke"):
        path = job_info.get(key)
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass


async def _resolve_docker_image(client, image_ref: str):
    from docker.errors import ImageNotFound

    normalized = _normalize_image_ref(image_ref)
    try:
        return await asyncio.to_thread(client.images.get, normalized)
    except ImageNotFound:
        if not _SMOKE_ALLOW_PULL:
            raise
        logger.info(
            "Image %s not found locally, pulling for smoke validation", normalized
        )
        return await asyncio.to_thread(client.images.pull, normalized)


async def _run_image_smoke_check(
    client,
    image_ref: str,
    queue: asyncio.Queue,
    *,
    strict: bool = False,
    mandatory_imports: list[str] | None = None,
    optional_imports: list[str] | None = None,
    cache_bypass: bool = False,
    smoke_name_prefix: str = "qm-ide-smoke",
) -> dict[str, Any]:
    mandatory = [item for item in (mandatory_imports or _SMOKE_IMPORTS) if item]
    optional = [item for item in (optional_imports or _SMOKE_OPTIONAL_IMPORTS) if item]
    normalized_image = _normalize_image_ref(image_ref)
    image_obj = await _resolve_docker_image(client, normalized_image)
    image_attrs = getattr(image_obj, "attrs", {}) or {}
    image_id = str(
        getattr(image_obj, "id", "") or image_attrs.get("Id") or normalized_image
    ).strip()
    cache_key = _smoke_cache_key(image_id, mandatory, optional, strict)
    now = time.time()
    cache_entry = _SMOKE_CACHE.get(cache_key)
    if (
        not cache_bypass
        and cache_entry
        and now - float(cache_entry.get("checked_at", 0)) <= _SMOKE_CACHE_TTL
        and cache_entry.get("ok")
    ):
        await queue.put(f"[SMOKE] 使用缓存结果跳过镜像检查: {normalized_image}")
        return cache_entry

    smoke_id = str(uuid.uuid4())
    smoke_name = f"{smoke_name_prefix}-{smoke_id[:8]}"
    smoke_root = os.path.join(TMP_ROOT, "smoke")
    os.makedirs(smoke_root, exist_ok=True)
    smoke_script_path = os.path.join(smoke_root, f"{smoke_id}_smoke.py")
    smoke_logs: list[str] = []
    result: dict[str, Any] = {
        "ok": False,
        "image": normalized_image,
        "image_id": image_id,
        "container_id": None,
        "digest": None,
        "duration_ms": 0,
        "logs": smoke_logs,
        "warnings": [],
        "stage": "prepare",
        "error": None,
    }

    with open(smoke_script_path, "w", encoding="utf-8") as f:
        f.write(_build_smoke_script(mandatory, optional, strict))

    start_ts = time.time()
    container = None
    try:
        container = await asyncio.to_thread(
            client.containers.run,
            normalized_image,
            command=["python", "-u", "/tmp/smoke.py"],
            name=smoke_name,
            detach=True,
            volumes={smoke_script_path: {"bind": "/tmp/smoke.py", "mode": "ro"}},
            read_only=True,
            network_mode="none",
            tmpfs={"/tmp": "rw,noexec,nosuid,size=64m"},
            mem_limit="256m",
            cpu_quota=50000,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            environment={
                "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
        result["container_id"] = getattr(container, "short_id", None) or getattr(
            container, "id", None
        )
        result["stage"] = "stream"

        loop = asyncio.get_running_loop()

        def stream_logs():
            try:
                for line in container.logs(stream=True, follow=True):
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                    smoke_logs.append(text)
                    loop.call_soon_threadsafe(queue.put_nowait, text)
            except Exception as le:
                logger.error("Smoke log streaming error: %s", le, exc_info=True)
                smoke_logs.append(f"[ERROR] smoke log streaming error: {le}")
                loop.call_soon_threadsafe(
                    queue.put_nowait, f"[ERROR] smoke log streaming error: {le}"
                )

        await asyncio.to_thread(stream_logs)
        result["stage"] = "wait"
        wait_result = await asyncio.to_thread(container.wait)
        exit_code = int(wait_result.get("StatusCode", 0) or 0)
        result["duration_ms"] = int((time.time() - start_ts) * 1000)
        if exit_code != 0:
            result["stage"] = "exit"
            result["error"] = f"smoke container exit code {exit_code}"
            raise RuntimeError(f"smoke container exited with code {exit_code}")

        result["ok"] = True
        result["stage"] = "ok"
        repo_digests = (
            image_attrs.get("RepoDigests") if isinstance(image_attrs, dict) else None
        )
        result["digest"] = (
            repo_digests[0] if isinstance(repo_digests, list) and repo_digests else None
        )
        await queue.put(
            f"[SMOKE] 镜像验证通过: image={normalized_image}, container={result['container_id']}, digest={result['digest'] or image_id}"
        )
        _SMOKE_CACHE[cache_key] = {**result, "checked_at": time.time()}
        return _SMOKE_CACHE[cache_key]
    except Exception as exc:
        result["duration_ms"] = int((time.time() - start_ts) * 1000)
        result["error"] = str(exc)
        await queue.put(f"[ERROR] [SMOKE] 镜像验证失败: {exc}")
        logger.error(
            "Runner image smoke failed: image=%s stage=%s error=%s",
            normalized_image,
            result["stage"],
            exc,
            exc_info=True,
        )
        return result
    finally:
        if container is not None:
            try:
                await asyncio.to_thread(container.remove, force=True)
            except Exception:
                pass
        try:
            if os.path.exists(smoke_script_path):
                os.remove(smoke_script_path)
        except Exception:
            pass


def _build_runner_script() -> str:
    return r'''import ast
import importlib.util
import inspect
import os
import pathlib
import runpy
import sys
import traceback

STRATEGY_PATH = "/app/strategy.py"
QLIB_DATA_PATH = "/app/db/qlib_data"


def _is_docstring_expr(node):
    return isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant) and isinstance(
        node.value.value, str
    )


def _is_main_guard(node):
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or len(test.comparators) != 1:
        return False
    left = test.left
    comparator = test.comparators[0]
    return (
        isinstance(left, ast.Name)
        and left.id == "__name__"
        and isinstance(test.ops[0], ast.Eq)
        and isinstance(comparator, ast.Constant)
        and comparator.value == "__main__"
    )


def _analyze(code):
    tree = ast.parse(code)
    info = {
        "has_main_guard": False,
        "has_top_level_exec": False,
        "function_names": [],
        "has_strategy_config": False,
        "has_get_strategy_config": False,
        "has_get_strategy_instance": False,
    }
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            info["function_names"].append(node.name)
            if node.name == "get_strategy_config":
                info["has_get_strategy_config"] = True
            if node.name == "get_strategy_instance":
                info["has_get_strategy_instance"] = True
            continue
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "STRATEGY_CONFIG":
                    info["has_strategy_config"] = True
                    break
            continue
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "STRATEGY_CONFIG":
            info["has_strategy_config"] = True
            continue
        if _is_docstring_expr(node) or isinstance(node, (ast.Import, ast.ImportFrom, ast.ClassDef, ast.Pass)):
            continue
        if _is_main_guard(node):
            info["has_main_guard"] = True
            continue
        info["has_top_level_exec"] = True
    info["runnable"] = bool(
        info["has_main_guard"]
        or info["has_top_level_exec"]
        or "main" in info["function_names"]
        or "run" in info["function_names"]
        or info["has_strategy_config"]
        or info["has_get_strategy_config"]
        or info["has_get_strategy_instance"]
    )
    return info


def _load_module(path):
    spec = importlib.util.spec_from_file_location("ai_ide_strategy", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载策略模块: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _call_callable(module, name):
    fn = getattr(module, name, None)
    if not callable(fn):
        return False
    print(f"[SYSTEM] 调用 {name}() 入口")
    result = fn()
    if inspect.isawaitable(result):
        import asyncio

        asyncio.run(result)
    return True


def _init_qlib():
    """初始化 Qlib"""
    import qlib
    from qlib.data import D

    provider_uri = QLIB_DATA_PATH
    if not os.path.exists(provider_uri):
        print(f"[ERROR] Qlib 数据目录不存在: {provider_uri}")
        return False

    print(f"[SYSTEM] 初始化 Qlib: provider_uri={provider_uri}")
    qlib.init(provider_uri=provider_uri, region="cn")
    print("[SYSTEM] Qlib 初始化成功")
    return True


def _run_module_backtest(module):
    """执行模块型策略（回测中心兼容模式）"""
    import json
    import time
    from datetime import datetime, timedelta

    # 1. 获取策略配置
    config = None
    if callable(getattr(module, "get_strategy_config", None)):
        try:
            config = module.get_strategy_config()
            print("[SYSTEM] 通过 get_strategy_config() 获取策略配置")
        except Exception as exc:
            print(f"[ERROR] get_strategy_config() 执行失败: {exc}")
            traceback.print_exc()
            return 2
    elif isinstance(getattr(module, "STRATEGY_CONFIG", None), dict):
        config = module.STRATEGY_CONFIG
        print("[SYSTEM] 通过 STRATEGY_CONFIG 获取策略配置")

    if not isinstance(config, dict):
        print("[ERROR] 无法获取有效的策略配置 (dict)")
        return 2

    class_name = config.get("class", "<unknown>")
    module_path = config.get("module_path", "")
    kwargs = config.get("kwargs", {})

    print(f"[SYSTEM] 策略配置: class={class_name}, module_path={module_path}")
    print(f"[SYSTEM] 策略参数: {json.dumps(kwargs, ensure_ascii=False, default=str)}")

    # 2. 初始化 Qlib
    if not _init_qlib():
        return 2

    # 3. 从环境变量获取回测参数（由 AI-IDE 前端/后端注入）
    start_date = os.getenv("AI_IDE_BACKTEST_START_DATE", (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d"))
    end_date = os.getenv("AI_IDE_BACKTEST_END_DATE", datetime.now().strftime("%Y-%m-%d"))
    initial_capital = float(os.getenv("AI_IDE_BACKTEST_INITIAL_CAPITAL", "1000000"))
    universe = os.getenv("AI_IDE_BACKTEST_UNIVERSE", "csi300")
    benchmark = os.getenv("AI_IDE_BACKTEST_BENCHMARK", "SH000300")
    commission = float(os.getenv("AI_IDE_BACKTEST_COMMISSION", "0.0003"))
    stamp_duty = float(os.getenv("AI_IDE_BACKTEST_STAMP_DUTY", "0.001"))

    print(f"[SYSTEM] 回测参数: {start_date} ~ {end_date}, capital={initial_capital}, universe={universe}")

    # 4. 处理 signal
    signal = kwargs.get("signal", "<PRED>")
    if isinstance(signal, str) and signal == "<PRED>":
        # 尝试从环境变量或默认路径加载预测文件
        pred_path = os.getenv("AI_IDE_PRED_PATH", os.path.join(QLIB_DATA_PATH, "predictions", "pred.pkl"))
        if os.path.exists(pred_path):
            print(f"[SYSTEM] 加载预测文件: {pred_path}")
            import pandas as pd

            try:
                pred = pd.read_pickle(pred_path)
                kwargs["signal"] = pred
                print("[SYSTEM] 预测文件加载成功")
            except Exception as e:
                print(f"[WARN] 预测文件加载失败，使用 $close 作为信号: {e}")
                kwargs["signal"] = "$close"
        else:
            print(f"[WARN] 预测文件不存在 ({pred_path})，使用 $close 作为信号")
            kwargs["signal"] = "$close"

    # 5. 构建策略对象
    from qlib.strategy.base import BaseStrategy

    strategy_obj = None
    strategy_dict = None

    # 尝试实例化策略类
    if module_path:
        try:
            spec = importlib.util.spec_from_file_location("strategy_mod", module_path.replace(".", "/") + ".py")
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                sys.modules["strategy_mod"] = mod
                spec.loader.exec_module(mod)
                if hasattr(mod, class_name):
                    cls = getattr(mod, class_name)
                    strategy_obj = cls(**kwargs)
                    print(f"[SYSTEM] 成功实例化策略: {class_name}")
        except Exception as e:
            print(f"[WARN] 从 module_path 实例化失败: {e}")

    # 如果 module_path 为空或实例化失败，尝试从当前模块获取类
    if strategy_obj is None:
        if hasattr(module, class_name):
            cls = getattr(module, class_name)
            if issubclass(cls, BaseStrategy):
                try:
                    strategy_obj = cls(**kwargs)
                    print(f"[SYSTEM] 从当前模块实例化策略: {class_name}")
                except Exception as e:
                    print(f"[WARN] 实例化失败: {e}")

    # 如果无法实例化，使用字典配置模式
    if strategy_obj is None:
        print(f"[SYSTEM] 使用字典配置模式执行回测")
        if not module_path:
            # 尝试从命名空间推断
            for name, obj in vars(module).items():
                if isinstance(obj, type) and issubclass(obj, BaseStrategy) and obj is not BaseStrategy:
                    module_path = f"ai_ide_strategy"
                    class_name = name
                    print(f"[SYSTEM] 推断策略类: {class_name}")
                    break

        strategy_dict = {
            "class": class_name,
            "module_path": module_path or "ai_ide_strategy",
            "kwargs": kwargs,
        }

    # 6. 构建回测配置
    executor = {
        "class": "SimulatorExecutor",
        "module_path": "qlib.backtest.executor",
        "kwargs": {
            "time_per_step": "d",
            "generate_portfolio_metrics": True,
        },
    }

    backtest_config = {
        "start_time": start_date,
        "end_time": end_date,
        "account": initial_capital,
        "benchmark": benchmark,
    }

    # 7. 执行回测
    from backend.services.engine.qlib_app.utils.qlib_utils import backtest as safe_backtest

    print("[SYSTEM] 开始执行回测...")
    start_time = time.time()

    try:
        if strategy_obj is not None:
            portfolio_dict, indicator_dict = safe_backtest(
                strategy=strategy_obj,
                executor=executor,
                freq="d",
                **backtest_config,
            )
        else:
            portfolio_dict, indicator_dict = safe_backtest(
                strategy=strategy_dict,
                executor=executor,
                freq="d",
                **backtest_config,
            )

        execution_time = time.time() - start_time
        print(f"\n{'='*60}")
        print(f"[RESULT] 回测完成 (耗时: {execution_time:.2f}s)")
        print(f"{'='*60}")

        # 输出指标
        if indicator_dict:
            risk_analysis = indicator_dict.get("risk_analysis", {})
            if isinstance(risk_analysis, dict):
                print(f"\n[RESULT] 收益指标:")
                for key, value in risk_analysis.items():
                    if isinstance(value, (int, float)):
                        print(f"  {key}: {value:.4f}")

        if portfolio_dict:
            portfolio_analysis = portfolio_dict.get("portfolio_analysis", {})
            if isinstance(portfolio_analysis, dict):
                print(f"\n[RESULT] 组合指标:")
                for key, value in portfolio_analysis.items():
                    if isinstance(value, (int, float)):
                        print(f"  {key}: {value:.4f}")

        print(f"\n[RESULT] 回测成功")
        return 0

    except Exception as e:
        print(f"\n[ERROR] 回测执行失败: {e}")
        traceback.print_exc()
        return 2


def main():
    source = pathlib.Path(STRATEGY_PATH).read_text(encoding="utf-8")
    info = _analyze(source)

    # 模式 1: 可执行脚本（有 __main__ 或顶层执行）
    if info["has_main_guard"] or info["has_top_level_exec"]:
        print("[SYSTEM] 检测到可执行脚本入口，开始运行 strategy.py")
        runpy.run_path(STRATEGY_PATH, run_name="__main__")
        return 0

    # 模式 2: 有 main() 或 run() 函数
    module = _load_module(STRATEGY_PATH)
    if _call_callable(module, "main") or _call_callable(module, "run"):
        return 0

    # 模式 3: 模块型策略（回测中心兼容模式）
    if info.get("has_strategy_config") or info.get("has_get_strategy_config") or info.get("has_get_strategy_instance"):
        print("[SYSTEM] 检测到模块型策略，启动回测中心兼容模式")
        return _run_module_backtest(module)

    # 无法执行
    print("[ERROR] 当前策略文件没有可识别的执行入口。")
    print("[ERROR] 支持以下模式:")
    print("  1. if __name__ == '__main__': 或顶层可执行代码")
    print("  2. main() 或 run() 函数")
    print("  3. get_strategy_config() 或 STRATEGY_CONFIG (模块型策略)")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _get_user_id(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return str(user.get("user_id") or user.get("sub"))


@router.post("/start")
@router.post("/run-tmp")
async def start_execution(request: Request, item: StartRequest):
    """
    启动执行。针对云端模式进行了对齐，支持 file_id/path 或直接传 content/code。
    """
    try:
        user_id = _get_user_id(request)

        # 1. 提取代码内容
        code = item.code or item.content
        filename = item.filename or "tmp_code.py"

        # 如果没有代码内容，尝试从存储加载
        actual_id = item.file_id or item.path
        if not code and actual_id:
            svc = get_strategy_storage_service()
            # 移除可能存在的 .py 后缀
            sid = actual_id
            if sid.endswith(".py"):
                sid = sid[:-3]

            strategy = await svc.get(sid, user_id=user_id)
            if not strategy:
                raise HTTPException(status_code=404, detail=f"Strategy {sid} not found")
            code = strategy.get("code", "")
            if not item.filename:
                filename = f"{strategy.get('name', 'unnamed')}.py"

        if not code:
            raise HTTPException(
                status_code=422, detail="No code content or valid file_id provided"
            )

        try:
            entrypoint_info = _analyze_execution_entrypoint(code)
        except SyntaxError as e:
            raise HTTPException(
                status_code=422,
                detail=f"策略代码语法错误，无法执行: {e.msg} (line {e.lineno})",
            )
        except Exception as e:
            logger.exception("Execution entrypoint analysis failed")
            raise HTTPException(
                status_code=500, detail=f"Execution analysis failed: {e}"
            )

        if not entrypoint_info.get("runnable"):
            raise HTTPException(
                status_code=422, detail=_build_runnable_error(entrypoint_info)
            )

        # 2. 准备临时运行环境
        tmp_root = TMP_ROOT
        user_tmp_dir = os.path.join(tmp_root, user_id)
        os.makedirs(user_tmp_dir, exist_ok=True)

        safe_filename = "".join([c for c in filename if c.isalnum() or c in "._-"])
        if not safe_filename:
            safe_filename = "tmp_code.py"
        runner_image = _normalize_image_ref(item.runner_image)

        job_id = str(uuid.uuid4())
        file_path = os.path.join(user_tmp_dir, f"{job_id}_{safe_filename}")
        runner_path = os.path.join(user_tmp_dir, f"{job_id}_runner.py")

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)
        with open(runner_path, "w", encoding="utf-8") as f:
            f.write(_build_runner_script())

        jobs[job_id] = {
            "status": "running",
            "queue": asyncio.Queue(),
            "user_id": user_id,
            "file": file_path,
            "runner": runner_path,
            "image": runner_image,
        }

        # 跳过已知生产镜像的 smoke test，避免误报和延迟
        if "quantmind-oss" in runner_image:
            logger.info(f"[SMOKE] 跳过生产镜像 {runner_image} 的验证")
        else:
            smoke_client = None
            try:
                import docker

                smoke_client = docker.from_env()
                smoke_result = await _run_image_smoke_check(
                    smoke_client,
                    runner_image,
                    jobs[job_id]["queue"],
                    strict=False,
                    mandatory_imports=_SMOKE_IMPORTS,
                    optional_imports=_SMOKE_OPTIONAL_IMPORTS,
                )
                if not smoke_result.get("ok"):
                    jobs[job_id]["status"] = "failed"
                    await jobs[job_id]["queue"].put(
                        "[ERROR] AI-IDE 临时镜像验证未通过，已阻断正式执行"
                    )
                    await jobs[job_id]["queue"].put("[EOF]")
                    return {
                        "job_id": job_id,
                        "status": "blocked",
                        "smoke": smoke_result,
                    }
            finally:
                if smoke_client is not None:
                    try:
                        smoke_client.close()
                    except Exception:
                        pass

        # 启动异步任务执行进程
        asyncio.create_task(run_process(job_id, file_path))

        return {"job_id": job_id, "status": "started", "runner_image": runner_image}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Execution start failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/smoke-image")
async def smoke_image(request: Request, item: SmokeImageRequest):
    """
    独立验证 AI-IDE 运行镜像是否可用。
    适合在切换临时镜像后先执行一次，再决定是否运行真实策略。
    """
    try:
        _get_user_id(request)
        import docker

        client = docker.from_env()
        queue: asyncio.Queue = asyncio.Queue()
        try:
            result = await _run_image_smoke_check(
                client,
                item.image or _DEFAULT_IMAGE,
                queue,
                strict=bool(item.strict),
                mandatory_imports=item.imports or _SMOKE_IMPORTS,
                optional_imports=item.optional_imports or _SMOKE_OPTIONAL_IMPORTS,
                cache_bypass=bool(item.cache_bypass),
                smoke_name_prefix="qm-ide-manual-smoke",
            )
            logs: list[str] = []
            while not queue.empty():
                try:
                    logs.append(queue.get_nowait())
                except Exception:
                    break
            result["queued_logs"] = logs
            return result
        finally:
            try:
                client.close()
            except Exception:
                pass
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Image smoke failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/stop/{job_id}")
async def stop_execution(request: Request, job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    user_id = _get_user_id(request)
    job_info = jobs[job_id]

    if job_info["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Forbidden")

    container = job_info.get("container")
    if container:
        try:
            import asyncio

            await asyncio.to_thread(container.stop, timeout=5)
            await job_info["queue"].put("[ERROR] Process terminated by user")
            return {"status": "stopped"}
        except Exception as e:
            logger.error(f"Stop container failed: {e}")
            return {"status": "error", "message": str(e)}

    return {"status": "already_stopped"}


@router.get("/logs/{job_id}")
async def get_logs_stream(request: Request, job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    user_id = _get_user_id(request)
    if jobs[job_id]["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Forbidden")

    async def log_generator():
        queue = jobs[job_id]["queue"]
        while True:
            try:
                line = await queue.get()
                if line == "[EOF]":
                    yield "data: [PROCESS_FINISHED]\n\n"
                    break

                # 兼容原有 SSE 格式
                yield f"data: {line}\n\n"
            except Exception as e:
                yield f"data: [ERROR] Log stream internal error: {e}\n\n"
                break

        # 清理任务
        if job_id in jobs:
            await _cleanup_job_artifacts(jobs[job_id])
            del jobs[job_id]

    return StreamingResponse(log_generator(), media_type="text/event-stream")


async def run_process(job_id: str, file_path: str):
    if job_id not in jobs:
        return

    import docker

    job_info = jobs[job_id]
    queue = job_info["queue"]
    user_id = job_info["user_id"]
    runner_path = job_info.get("runner")
    image_ref = _normalize_image_ref(job_info.get("image"))

    try:
        client = docker.from_env()

        # 计算宿主机上的文件路径 (用于 Docker 挂载)
        # 假设 API 容器内的 /app 对应 宿主机的 {HOST_PROJECT_PATH}
        rel_path = os.path.relpath(file_path, "/app")
        host_script_path = os.path.join(HOST_PROJECT_PATH, rel_path)

        # 准备挂载 (策略代码 + Qlib 数据)
        volumes = {
            host_script_path: {"bind": "/app/strategy.py", "mode": "ro"},
            os.path.join(HOST_PROJECT_PATH, os.path.relpath(runner_path, "/app")): {
                "bind": "/app/runner.py",
                "mode": "ro",
            },
            # 挂载 Qlib 数据目录以便运行回测脚本
            os.path.join(HOST_PROJECT_PATH, "db/qlib_data"): {
                "bind": "/app/db/qlib_data",
                "mode": "ro",
            },
            # 挂载内置模块以便调用
            os.path.join(HOST_PROJECT_PATH, "backend"): {
                "bind": "/app/backend",
                "mode": "ro",
            },
        }

        container_name = f"qm-ide-run-{job_id}"

        # 启动容器
        container = await asyncio.to_thread(
            client.containers.run,
            image_ref,
            command=["python", "-u", "/app/runner.py"],
            name=container_name,
            detach=True,
            volumes=volumes,
            network=_NETWORK,
            environment={
                "PYTHONPATH": "/app",
                "PYTHONUNBUFFERED": "1",
                "USER_ID": user_id,
            },
            mem_limit="2g",
            cpu_quota=100000,  # 1 CPU
        )

        job_info["container"] = container

        # 流式读取日志
        loop = asyncio.get_running_loop()

        def stream_logs():
            logger.info(f"Start streaming logs for container {container_name}")
            try:
                for line in container.logs(stream=True, follow=True):
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                    loop.call_soon_threadsafe(queue.put_nowait, text)
            except Exception as le:
                logger.error(f"Log streaming error: {le}")

        log_thread = asyncio.to_thread(stream_logs)
        await log_thread

        # 等待结束
        result = await asyncio.to_thread(container.wait)
        exit_code = result.get("StatusCode", 0)

        if exit_code != 0:
            await queue.put(f"[ERROR] 进程异常退出 (ExitCode: {exit_code})")

    except Exception as e:
        logger.error(f"Docker execution failed: {e}", exc_info=True)
        await queue.put(f"[ERROR] Container isolation failed: {e}")
    finally:
        await queue.put("[EOF]")
        # 销毁容器
        if "container" in job_info:
            try:
                c = job_info["container"]
                await asyncio.to_thread(c.remove, force=True)
            except:
                pass
        await _cleanup_job_artifacts(job_info)
        job_info.pop("runner", None)
        job_info.pop("container", None)
