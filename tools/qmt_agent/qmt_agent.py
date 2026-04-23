#!/usr/bin/env python3
"""
QMT Agent entry point.

Usage:
  python tools/qmt_agent/qmt_agent.py --config qmt_agent_config.json

This file is intentionally thin.  All logic lives in the sub-modules:
  config.py    – AgentConfig, constants, load_config
  auth.py      – AuthManager
  reporter.py  – BridgeReporter
  _callback.py – QMT event callback factory
  client.py    – QMTClient
  agent.py     – QMTAgent
"""
from __future__ import annotations

import argparse
import logging
import os
import importlib.util
import sys
from logging.handlers import RotatingFileHandler
import signal
import subprocess
import threading
import time
from pathlib import Path

try:
    from .agent import QMTAgent
    from .auth import AuthManager  # noqa: F401
    from .client import QMTClient  # noqa: F401
    from .config import (  # noqa: F401
        AgentConfig,
        load_config,
        normalize_agent_config_data,
        validate_config_dict,
    )
except ImportError:
    _MODULE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

    def _load_local_module(module_name: str):
        qualified_name = f"qmt_agent_local_{module_name}"
        module = sys.modules.get(qualified_name)
        if module is not None:
            return module
        module_path = _MODULE_DIR / f"{module_name}.py"
        spec = importlib.util.spec_from_file_location(qualified_name, module_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load local module {module_name} from {module_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[qualified_name] = module
        spec.loader.exec_module(module)
        return module

    _agent_mod = _load_local_module("agent")
    _auth_mod = _load_local_module("auth")  # noqa: F401
    _client_mod = _load_local_module("client")  # noqa: F401
    _config_mod = _load_local_module("config")
    QMTAgent = _agent_mod.QMTAgent  # type: ignore[attr-defined]
    AuthManager = _auth_mod.AuthManager  # type: ignore[attr-defined]  # noqa: F401
    QMTClient = _client_mod.QMTClient  # type: ignore[attr-defined]  # noqa: F401
    AgentConfig = _config_mod.AgentConfig  # type: ignore[attr-defined]  # noqa: F401
    load_config = _config_mod.load_config  # type: ignore[attr-defined]  # noqa: F401
    normalize_agent_config_data = _config_mod.normalize_agent_config_data  # type: ignore[attr-defined]  # noqa: F401
    validate_config_dict = _config_mod.validate_config_dict  # type: ignore[attr-defined]  # noqa: F401


APP_NAME = "QuantMindQMTAgent"


def _app_data_dir() -> Path:
    base = Path(os.getenv("APPDATA") or Path.home() / "AppData" / "Roaming")
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _configure_logging() -> None:
    log_level_name = str(os.getenv("QMT_AGENT_LOG_LEVEL", "INFO")).upper()
    log_level = getattr(logging, log_level_name, logging.INFO)
    log_path = _resolve_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear()

    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)


def _open_log_dir() -> None:
    log_dir = _app_data_dir()
    if sys.platform == "win32":
        os.startfile(log_dir)  # type: ignore[attr-defined]
        return
    command = ["open" if sys.platform == "darwin" else "xdg-open", str(log_dir)]
    subprocess.Popen(command)


def _resolve_log_path() -> Path:
    return Path(os.getenv("QMT_AGENT_LOG_PATH") or (_app_data_dir() / "qmt_agent.log"))


def _supervised_run(
    cfg: AgentConfig,
    shutdown_event: threading.Event,
    disable_auto_restart: bool,
    restart_base_delay_seconds: int,
    restart_max_delay_seconds: int,
    restart_window_seconds: int,
    restart_max_attempts_per_window: int,
) -> int:
    logger = logging.getLogger("qmt_agent")
    restart_marks: list[float] = []
    agent_holder: dict[str, QMTAgent | None] = {"agent": None}

    def _handle_signal(_signum, _frame) -> None:
        shutdown_event.set()
        current = agent_holder.get("agent")
        if current is not None:
            current.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    while not shutdown_event.is_set():
        agent = QMTAgent(cfg)
        agent_holder["agent"] = agent
        run_started_at = time.time()
        exit_reason: str | None = None
        try:
            agent.start()
            if not shutdown_event.is_set():
                exit_reason = "agent 主循环意外退出"
        except Exception as exc:
            if shutdown_event.is_set():
                break
            exit_reason = str(exc)
            logger.exception("qmt agent crashed, will evaluate restart policy")
        finally:
            agent_holder["agent"] = None
            agent.stop()

        if shutdown_event.is_set():
            break

        if not exit_reason:
            break

        if disable_auto_restart:
            logger.error("qmt agent exited and auto-restart is disabled: %s", exit_reason)
            return 1

        now = time.time()
        restart_marks = [ts for ts in restart_marks if now - ts <= restart_window_seconds]
        attempt = len(restart_marks) + 1
        if attempt > restart_max_attempts_per_window:
            logger.error(
                "qmt agent restart attempts exceeded window limit (%s in %ss), last_error=%s",
                restart_max_attempts_per_window,
                restart_window_seconds,
                exit_reason,
            )
            return 1

        restart_marks.append(now)
        run_duration = max(0, int(now - run_started_at))
        restart_delay = min(restart_max_delay_seconds, restart_base_delay_seconds * (2 ** (attempt - 1)))
        logger.warning(
            "qmt agent exited unexpectedly after %ss (attempt %s/%s), restarting in %ss, reason=%s",
            run_duration,
            attempt,
            restart_max_attempts_per_window,
            restart_delay,
            exit_reason,
        )

        if shutdown_event.wait(restart_delay):
            break

    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument(
        "--open-log-dir",
        action="store_true",
        help="打开 QMT Agent 日志目录后退出",
    )
    parser.add_argument(
        "--show-log-path",
        action="store_true",
        help="输出 QMT Agent 日志文件路径后退出",
    )
    parser.add_argument(
        "--disable-auto-restart",
        action="store_true",
        help="关闭崩溃自动重启（默认开启）",
    )
    parser.add_argument(
        "--restart-base-delay-seconds",
        type=int,
        default=int(os.getenv("QMT_AGENT_RESTART_BASE_DELAY_SECONDS", "3")),
        help="自动重启基础延迟秒数（指数退避起点）",
    )
    parser.add_argument(
        "--restart-max-delay-seconds",
        type=int,
        default=int(os.getenv("QMT_AGENT_RESTART_MAX_DELAY_SECONDS", "60")),
        help="自动重启最大延迟秒数（指数退避上限）",
    )
    parser.add_argument(
        "--restart-window-seconds",
        type=int,
        default=int(os.getenv("QMT_AGENT_RESTART_WINDOW_SECONDS", "600")),
        help="自动重启统计窗口秒数",
    )
    parser.add_argument(
        "--restart-max-attempts-per-window",
        type=int,
        default=int(os.getenv("QMT_AGENT_RESTART_MAX_ATTEMPTS_PER_WINDOW", "20")),
        help="统计窗口内最大重启次数，超限后退出",
    )
    args = parser.parse_args()
    if args.open_log_dir:
        _open_log_dir()
        return 0
    if args.show_log_path:
        print(_resolve_log_path())
        return 0
    if not args.config:
        parser.error("--config is required unless --open-log-dir or --show-log-path is used")

    restart_base_delay_seconds = max(1, int(args.restart_base_delay_seconds or 3))
    restart_max_delay_seconds = max(restart_base_delay_seconds, int(args.restart_max_delay_seconds or 60))
    restart_window_seconds = max(30, int(args.restart_window_seconds or 600))
    restart_max_attempts_per_window = max(1, int(args.restart_max_attempts_per_window or 20))

    _configure_logging()
    cfg = load_config(args.config)
    shutdown_event = threading.Event()
    return _supervised_run(
        cfg=cfg,
        shutdown_event=shutdown_event,
        disable_auto_restart=bool(args.disable_auto_restart),
        restart_base_delay_seconds=restart_base_delay_seconds,
        restart_max_delay_seconds=restart_max_delay_seconds,
        restart_window_seconds=restart_window_seconds,
        restart_max_attempts_per_window=restart_max_attempts_per_window,
    )


if __name__ == "__main__":
    raise SystemExit(main())
