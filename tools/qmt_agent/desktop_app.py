#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import fields
import hmac
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import zipfile
from logging.handlers import RotatingFileHandler
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

from PySide6.QtCore import Qt, QTimer, Signal, QObject, QUrl
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap, QAction, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QLabel,
    QLineEdit, QPushButton, QComboBox,
    QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout,
    QScrollArea, QStackedWidget, QListWidget, QListWidgetItem,
    QPlainTextEdit, QFileDialog, QDialog,
    QDialogButtonBox, QButtonGroup, QRadioButton, QToolButton,
    QSystemTrayIcon, QMenu, QSizePolicy, QSplitter,
)

from qmt_agent import (
    AgentConfig,
    AuthManager,
    QMTAgent,
    QMTClient,
    normalize_agent_config_data,
    validate_config_dict,
)

try:
    from .triage import classify_runtime_fault
except ImportError:
    from triage import classify_runtime_fault

try:
    from .updater import UpdateChecker, UpdateCheckResult, get_update_checker
except ImportError:
    from updater import UpdateChecker, UpdateCheckResult, get_update_checker

try:
    import winreg  # type: ignore
except Exception:  # pragma: no cover - non windows
    winreg = None


APP_NAME = "QuantMindQMTAgent"
LEGACY_AUTOSTART_TASK_NAME = "QuantMindQMTAgent-Autostart"
LOCAL_PORT = int(os.getenv("QMT_AGENT_LOCAL_PORT", "18965"))
LOCAL_API_TOKEN = str(os.getenv("QMT_AGENT_LOCAL_API_TOKEN", "")).strip()
APP_ICON_PATH = Path(__file__).resolve().parent / "icon.ico"
logger = logging.getLogger("qmt_agent.desktop")


def load_version_info() -> dict[str, Any]:
    version_file = Path(__file__).resolve().parent / "version.json"
    if not version_file.exists():
        return {"version": "1.0.0", "product_name": "QuantMind QMT Agent"}
    try:
        data = json.loads(version_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"version": "1.0.0"}
    except Exception:
        return {"version": "1.0.0"}


VERSION_INFO = load_version_info()
APP_VERSION = str(VERSION_INFO.get("version") or "1.0.0")


def app_data_dir() -> Path:
    base = Path(os.getenv("APPDATA") or Path.home() / "AppData" / "Roaming")
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


CONFIG_PATH = app_data_dir() / "config.json"
LOG_PATH = app_data_dir() / "desktop.log"
DIAG_DIR = app_data_dir() / "diagnostics"
DIAG_DIR.mkdir(parents=True, exist_ok=True)


def _configure_logging() -> None:
    log_level_name = str(os.getenv("QMT_AGENT_LOG_LEVEL", "INFO")).upper()
    log_level = getattr(logging, log_level_name, logging.INFO)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    file_handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    logging.captureWarnings(True)


def _set_runtime_log_level(level: int) -> None:
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    for handler in root_logger.handlers:
        handler.setLevel(level)
    logging.getLogger("qmt_agent").setLevel(level)
    logging.getLogger("qmt_agent.desktop").setLevel(level)
    logging.getLogger("qmt_agent.callback").setLevel(level)


def _load_help_text() -> str:
    help_file = Path(__file__).resolve().parent / "help.md"
    if help_file.exists():
        try:
            return help_file.read_text(encoding="utf-8")
        except Exception:
            pass
    return "\n".join(
        [
            "# QMT Agent 帮助",
            "",
            "启动桌面版：",
            "  python tools/qmt_agent/desktop_app.py",
            "",
            "启动 CLI 桥接：",
            "  python tools/qmt_agent/qmt_agent.py --config qmt_agent_config.json",
            "  python tools/qmt_agent/qmt_agent.py --open-log-dir",
            "  python tools/qmt_agent/qmt_agent.py --show-log-path",
            "",
            "日志位置：",
            f"  桌面壳日志: {LOG_PATH}",
            f"  CLI 日志: {_app_data_dir() / 'qmt_agent.log'}",
        ]
    )


def default_config() -> dict[str, Any]:
    hostname = socket.gethostname()
    return {
        "api_base_url": "https://api.quantmind.cloud/api/v1",
        "server_url": "wss://api.quantmind.cloud/ws/bridge",
        "access_key": "",
        "secret_key": "",
        "account_id": "",
        "tenant_id": "default",
        "user_id": "",
        "client_version": f"{APP_VERSION}-desktop",
        "client_fingerprint": hostname,
        "hostname": hostname,
        "qmt_path": "",
        "qmt_bin_path": "",
        "session_id": 0,
        "renew_before_seconds": 300,
        "heartbeat_interval_seconds": 15,
        "account_report_interval_seconds": 30,
        "reconnect_interval_seconds": 5,
        "ws_ping_interval_seconds": 20,
        "ws_ping_timeout_seconds": 10,
        "minimize_to_tray": False,
        "auto_start_agent": False,
        "auto_restart_on_crash": True,
        "restart_base_delay_seconds": 3,
        "restart_max_delay_seconds": 60,
        "restart_window_seconds": 600,
        "restart_max_attempts_per_window": 20,
    }


def load_saved_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        data = default_config()
        CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    merged = default_config()
    merged.update(data if isinstance(data, dict) else {})
    # 旧版本允许桌面壳打开后自动拉起 agent，这会与手动托管模式冲突。
    # 这里统一迁移为关闭，后续仅在用户点击“启动 Agent”后进入托管运行。
    merged["auto_start_agent"] = False
    return normalize_agent_config_data(merged)


def save_config(data: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def mask_sensitive(value: str) -> str:
    text = str(value or "").strip()
    if len(text) <= 6:
        return "*" * len(text)
    return f"{text[:4]}******{text[-2:]}"


def scan_qmt_installations(seed_paths: Optional[list[str]] = None) -> dict[str, Any]:
    candidates: list[Path] = []
    checked: list[str] = []

    def _add_candidate(raw: Optional[str | Path]) -> None:
        if raw is None:
            return
        text = str(raw).strip()
        if not text:
            return
        try:
            path = Path(text)
            candidates.append(path)
            # 若给的是安装根目录，自动扩展常用子目录
            candidates.append(path / "userdata_mini")
            candidates.append(path / "bin.x64")
        except Exception:
            return

    for raw in (
        os.getenv("QMT_DATA_PATH"),
        os.getenv("QMT_BIN_PATH"),
    ):
        _add_candidate(raw)

    for raw in (seed_paths or []):
        _add_candidate(raw)

    common_roots = [
        Path("C:/"),
        Path("D:/"),
        Path("E:/"),
        Path("C:/Program Files"),
        Path("C:/Program Files (x86)"),
        Path.home(),
    ]
    common_install_dirs = [
        Path("E:/迅投极速交易终端 睿智融科版"),
        Path("E:/迅投极速交易终端"),
        Path("D:/迅投极速交易终端 睿智融科版"),
        Path("D:/迅投极速交易终端"),
        Path("C:/迅投极速交易终端 睿智融科版"),
        Path("C:/迅投极速交易终端"),
    ]
    for root in common_install_dirs:
        _add_candidate(root)

    patterns = [
        "*/userdata_mini",
        "*/*/userdata_mini",
        "*/*/*/userdata_mini",
        "*/*/*/*/userdata_mini",
        "*/bin.x64",
        "*/*/bin.x64",
        "*/*/*/bin.x64",
        "*/*/*/*/bin.x64",
        "*/迅投*/",
        "*/*/迅投*/",
        "*/QMT*/",
        "*/*/QMT*/",
    ]
    for root in common_roots:
        if not root.exists():
            continue
        for pattern in patterns:
            try:
                for path in root.glob(pattern):
                    _add_candidate(path)
            except Exception:
                continue

    found_data = False
    found_bin = False
    installations: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    for candidate in candidates:
        checked.append(str(candidate))
        if candidate.name.lower() == "userdata_mini" and candidate.exists():
            qmt_path = str(candidate)
            found_data = True
            maybe_bin = candidate.parent / "bin.x64"
            qmt_bin_path = str(maybe_bin) if maybe_bin.exists() else ""
            if qmt_bin_path:
                found_bin = True
            key = (qmt_path, qmt_bin_path)
            if key not in seen_keys:
                seen_keys.add(key)
                label = candidate.parent.name or qmt_path
                installations.append(
                    {
                        "label": label,
                        "qmt_path": qmt_path,
                        "qmt_bin_path": qmt_bin_path,
                        "looks_like_mini_mode": bool(qmt_path and qmt_bin_path),
                    }
                )
        if candidate.name.lower() == "bin.x64" and candidate.exists():
            qmt_bin_path = str(candidate)
            found_bin = True
            maybe_data = candidate.parent / "userdata_mini"
            qmt_path = str(maybe_data) if maybe_data.exists() else ""
            if qmt_path:
                found_data = True
            key = (qmt_path, qmt_bin_path)
            if key not in seen_keys and (qmt_path or qmt_bin_path):
                seen_keys.add(key)
                label = candidate.parent.name or qmt_path or qmt_bin_path
                installations.append(
                    {
                        "label": label,
                        "qmt_path": qmt_path,
                        "qmt_bin_path": qmt_bin_path,
                        "looks_like_mini_mode": bool(qmt_path and qmt_bin_path),
                    }
                )

    qmt_path = installations[0].get("qmt_path", "") if installations else ""
    qmt_bin_path = installations[0].get("qmt_bin_path", "") if installations else ""

    return {
        "found_userdata_mini": found_data,
        "found_bin_x64": found_bin,
        "qmt_path": qmt_path,
        "qmt_bin_path": qmt_bin_path,
        "looks_like_mini_mode": bool(found_data and found_bin),
        "all_installations": installations,
        "checked_candidates": checked[:100],
    }


def build_agent_config(data: dict[str, Any]) -> AgentConfig:
    cfg = dict(default_config())
    cfg.update(data)
    cfg = normalize_agent_config_data(cfg)
    cfg["client_version"] = f"{APP_VERSION}-desktop"
    allowed_keys = {item.name for item in fields(AgentConfig)}
    filtered = {key: value for key, value in cfg.items() if key in allowed_keys}
    return AgentConfig(**filtered)


class DesktopRuntime:
    def __init__(self, on_state_change: Optional[Callable[[], None]] = None):
        self.on_state_change = on_state_change or (lambda: None)
        self._lock = threading.RLock()
        self.agent: Optional[QMTAgent] = None
        self.thread: Optional[threading.Thread] = None
        self._desired_running = False
        self._supervisor_state = "stopped"
        self._supervisor_config: dict[str, Any] = {}
        self._supervisor_policy: dict[str, Any] = {}
        self._restart_marks: list[float] = []
        self._restart_count = 0
        self.last_error: Optional[str] = None
        self.last_test_result: dict[str, Any] = {}
        self._last_bridge_test_at: Optional[float] = None
        self.error_history: list[dict[str, Any]] = []

    def _record_error(self, message: str) -> None:
        text = str(message or "").strip()
        if not text:
            return
        self.last_error = text
        self.error_history.insert(
            0,
            {
                "time": datetime.now().isoformat(timespec="seconds"),
                "message": text,
            },
        )
        self.error_history = self.error_history[:20]

    def _notify(self) -> None:
        try:
            self.on_state_change()
        except Exception:
            logger.exception("state callback failed")

    def _validate_runtime_dependencies(self, cfg: AgentConfig) -> tuple[bool, str]:
        client = QMTClient(cfg)
        try:
            errors = client.runtime_dependency_errors()
        finally:
            client.close()
        if not errors:
            return True, ""
        hint = "请确认 qmt_bin_path 指向 MiniQMT 的 bin.x64，且打包版已包含 xtquant/xtdata 依赖。"
        return False, "；".join(errors + [hint])

    @staticmethod
    def _coerce_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    @staticmethod
    def _coerce_bool(value: Any, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"1", "true", "yes", "y", "on"}:
                return True
            if text in {"0", "false", "no", "n", "off"}:
                return False
        if value is None:
            return default
        return bool(value)

    def _extract_supervisor_policy(self, data: dict[str, Any]) -> dict[str, Any]:
        base_delay = self._coerce_int(data.get("restart_base_delay_seconds"), 3, 1, 300)
        max_delay = self._coerce_int(data.get("restart_max_delay_seconds"), 60, base_delay, 1800)
        return {
            "auto_restart_on_crash": self._coerce_bool(data.get("auto_restart_on_crash"), True),
            "restart_base_delay_seconds": base_delay,
            "restart_max_delay_seconds": max_delay,
            "restart_window_seconds": self._coerce_int(data.get("restart_window_seconds"), 600, 30, 3600),
            "restart_max_attempts_per_window": self._coerce_int(
                data.get("restart_max_attempts_per_window"), 20, 1, 200
            ),
        }

    def _next_restart_delay(self, policy: dict[str, Any]) -> int | None:
        now = time.time()
        window_seconds = int(policy.get("restart_window_seconds") or 600)
        max_attempts = int(policy.get("restart_max_attempts_per_window") or 20)
        self._restart_marks = [ts for ts in self._restart_marks if now - ts <= window_seconds]
        attempt = len(self._restart_marks) + 1
        if attempt > max_attempts:
            return None
        self._restart_marks.append(now)
        base_delay = int(policy.get("restart_base_delay_seconds") or 3)
        max_delay = int(policy.get("restart_max_delay_seconds") or 60)
        return min(max_delay, base_delay * (2 ** (attempt - 1)))

    def _run_agent_supervisor(self) -> None:
        while True:
            with self._lock:
                if not self._desired_running:
                    self._supervisor_state = "stopped"
                    break
                cfg_data = dict(self._supervisor_config)
                policy = dict(self._supervisor_policy)
                self._supervisor_state = "running"

            cfg = build_agent_config(cfg_data)
            agent = QMTAgent(cfg)
            with self._lock:
                self.agent = agent

            crashed = False
            crash_reason = ""
            run_started_at = time.time()
            try:
                agent.start()
            except Exception as exc:
                crashed = True
                crash_reason = str(exc)
                self._record_error(f"Agent 崩溃: {exc}")
                logger.exception("desktop runtime failed")
            finally:
                try:
                    agent.stop()
                except Exception:
                    pass
                with self._lock:
                    if self.agent is agent:
                        self.agent = None
                self._notify()

            with self._lock:
                still_desired = self._desired_running
                policy = dict(self._supervisor_policy)

            if not still_desired:
                with self._lock:
                    self._supervisor_state = "stopped"
                break

            auto_restart = bool(policy.get("auto_restart_on_crash", True))
            if not crashed and not auto_restart:
                self._record_error("Agent 已停止，且自动重启已关闭")
                with self._lock:
                    self._desired_running = False
                    self._supervisor_state = "stopped"
                break

            if not crashed:
                crash_reason = "Agent 主循环意外退出"
                self._record_error(crash_reason)

            if not auto_restart:
                with self._lock:
                    self._desired_running = False
                    self._supervisor_state = "stopped"
                break

            restart_delay = self._next_restart_delay(policy)
            if restart_delay is None:
                with self._lock:
                    self._desired_running = False
                    self._supervisor_state = "stopped"
                self._record_error(
                    f"自动重启超过窗口限制（{policy.get('restart_max_attempts_per_window')} 次/{policy.get('restart_window_seconds')} 秒），已停止"
                )
                break

            run_seconds = max(0, int(time.time() - run_started_at))
            with self._lock:
                self._restart_count += 1
                self._supervisor_state = "restarting"
            self._record_error(
                f"Agent 异常退出（运行 {run_seconds}s，原因: {crash_reason}），{restart_delay}s 后自动重启"
            )
            self._notify()
            if self._wait_for_stop(restart_delay):
                break

        with self._lock:
            if threading.current_thread() is self.thread:
                self.thread = None
                self.agent = None
                self._supervisor_state = "stopped"
        self._notify()

    def _wait_for_stop(self, seconds: int) -> bool:
        deadline = time.time() + max(0, int(seconds))
        while time.time() < deadline:
            with self._lock:
                if not self._desired_running:
                    return True
            time.sleep(0.2)
        return False

    def start(self, data: dict[str, Any]) -> dict[str, Any]:
        errors = validate_config_dict(data)
        if errors:
            return {"ok": False, "message": "；".join(errors)}
        normalized = normalize_agent_config_data(dict(data or {}))
        policy = self._extract_supervisor_policy(normalized)
        cfg = build_agent_config(normalized)
        runtime_ready, runtime_message = self._validate_runtime_dependencies(cfg)
        if not runtime_ready:
            self._record_error(runtime_message)
            return {"ok": False, "message": runtime_message}

        with self._lock:
            if self._desired_running and self.thread and self.thread.is_alive():
                return {"ok": True, "message": "Agent 已在运行"}
            self._desired_running = True
            self._supervisor_policy = policy
            self._supervisor_config = normalized
            self._restart_marks = []
            self._restart_count = 0
            self._supervisor_state = "running"
            self.thread = threading.Thread(target=self._run_agent_supervisor, name="qmt-agent-desktop", daemon=True)
            self.thread.start()
        self._notify()
        return {"ok": True, "message": "Agent 启动中"}

    def stop(self) -> dict[str, Any]:
        thread: Optional[threading.Thread] = None
        agent: Optional[QMTAgent] = None
        with self._lock:
            self._desired_running = False
            self._supervisor_state = "stopped"
            thread = self.thread
            agent = self.agent
        if agent is not None:
            agent.stop()
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)
        with self._lock:
            if self.thread is thread:
                self.thread = None
            self.agent = None
        self._notify()
        return {"ok": True, "message": "Agent 已停止"}

    def restart(self, data: dict[str, Any]) -> dict[str, Any]:
        self.stop()
        time.sleep(0.5)
        return self.start(data)

    def test_bridge(self, data: dict[str, Any]) -> dict[str, Any]:
        errors = validate_config_dict(data)
        if errors:
            return {"ok": False, "message": "；".join(errors)}
        cfg = build_agent_config(data)
        auth = AuthManager(cfg)
        try:
            auth.bootstrap()
            result = {
                "ok": True,
                "message": "云端绑定成功",
                "expires_at": auth.expires_at,
                "hostname": cfg.hostname,
                "tenant_id": cfg.tenant_id,
                "account_id": cfg.account_id,
            }
            self.last_test_result = result
            self._last_bridge_test_at = time.time()
            return result
        except Exception as exc:
            result = {"ok": False, "message": f"云端绑定失败: {exc}"}
            self._record_error(result["message"])
            self.last_test_result = result
            return result

    def test_qmt(self, data: dict[str, Any]) -> dict[str, Any]:
        cfg = build_agent_config(data)
        qmt_path = Path(str(cfg.qmt_path or "").strip())
        qmt_bin_path = Path(str(cfg.qmt_bin_path or "").strip())
        if not qmt_path.exists():
            return {"ok": False, "message": "userdata_mini 不存在"}
        if not qmt_bin_path.exists():
            return {"ok": False, "message": "bin.x64 不存在"}
        if not str(cfg.account_id or "").strip():
            return {"ok": False, "message": "资金账号不能为空"}
        client = QMTClient(cfg)
        dependency_errors = client.runtime_dependency_errors()
        if dependency_errors:
            hint = "请确认 qmt_bin_path 指向 MiniQMT 的 bin.x64，且打包版已包含 xtquant/xtdata 依赖"
            return {"ok": False, "message": f"{'；'.join(dependency_errors)}。{hint}"}
        try:
            if not client.connect():
                return {"ok": False, "message": "QMT 未开启极简模式或资金账号无效"}
            snapshot = client.snapshot()
            return {
                "ok": True,
                "message": "QMT 连接成功",
                "cash": snapshot.get("cash"),
                "total_asset": snapshot.get("total_asset"),
                "position_count": len(snapshot.get("positions") or []),
            }
        except Exception as exc:
            result = {"ok": False, "message": f"QMT 测试失败: {exc}"}
            self._record_error(result["message"])
            return result
        finally:
            client.close()

    def get_status(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            running = bool(self._desired_running and self.thread and self.thread.is_alive())
            agent_status = self.agent.get_runtime_status() if self.agent is not None else {}
            supervisor_state = self._supervisor_state
            restart_count = self._restart_count
            policy = dict(getattr(self, "_supervisor_policy", {}))
        return {
            "desktop_ready": True,
            "app_name": str(VERSION_INFO.get("product_name") or "QuantMind QMT Agent"),
            "app_version": APP_VERSION,
            "hostname": socket.gethostname(),
            "config_exists": CONFIG_PATH.exists(),
            "running": running,
            "config_summary": {
                "api_base_url": data.get("api_base_url"),
                "server_url": data.get("server_url"),
                "access_key": mask_sensitive(str(data.get("access_key") or "")),
                "secret_key": mask_sensitive(str(data.get("secret_key") or "")),
                "account_id": str(data.get("account_id") or "").strip(),
                "qmt_path": data.get("qmt_path"),
                "qmt_bin_path": data.get("qmt_bin_path"),
            },
            "last_error": self.last_error or agent_status.get("last_error"),
            "last_test_result": self.last_test_result,
            "last_bridge_test_at": self._last_bridge_test_at,
            "error_history": self.error_history,
            "supervisor_state": supervisor_state,
            "supervisor_restart_count": restart_count,
            "supervisor_policy": policy,
            "fault_triage": classify_runtime_fault({
                **agent_status,
                "last_error": self.last_error or agent_status.get("last_error"),
            }),
            **agent_status,
        }


def format_ts(value: Any) -> str:
    if value in (None, "", 0):
        return "无数据"
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
        text = str(value).strip()
        return text or "无数据"
    except Exception:
        return str(value)


def _call_local_api(path: str, *, timeout: float = 1.0) -> bool:
    request = Request(f"http://127.0.0.1:{LOCAL_PORT}{path}")
    if LOCAL_API_TOKEN:
        request.add_header("X-Local-Token", LOCAL_API_TOKEN)
    try:
        with urlopen(request, timeout=timeout) as response:
            return 200 <= int(getattr(response, "status", 0) or 0) < 300
    except URLError:
        return False
    except Exception:
        logger.exception("local api call failed: %s", path)
        return False


def _handoff_to_existing_instance(*, start_hidden: bool) -> bool:
    if start_hidden:
        return _call_local_api("/status")
    return _call_local_api("/open_gui")


def _remove_legacy_registry_autostart() -> bool:
    if winreg is None:
        return False
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        )
        try:
            winreg.DeleteValue(key, APP_NAME)
            logger.info("removed legacy registry autostart entry")
            return True
        except FileNotFoundError:
            return False
    except Exception as exc:
        logger.exception("failed to remove legacy registry autostart: %s", exc)
    return False


def _remove_legacy_task_scheduler_autostart() -> bool:
    if os.name != "nt":
        return False
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            ["schtasks", "/Delete", "/TN", LEGACY_AUTOSTART_TASK_NAME, "/F"],
            check=False,
            capture_output=True,
            text=True,
            creationflags=creationflags,
        )
    except Exception as exc:
        logger.exception("failed to remove legacy autostart task: %s", exc)
        return False
    if result.returncode == 0:
        logger.info("removed legacy autostart task: %s", LEGACY_AUTOSTART_TASK_NAME)
        return True
    stdout = str(result.stdout or "").lower()
    stderr = str(result.stderr or "").lower()
    if "cannot find" in stdout or "cannot find" in stderr:
        return False
    if "找不到" in stdout or "找不到" in stderr:
        return False
    logger.warning(
        "failed to remove legacy autostart task %s: %s",
        LEGACY_AUTOSTART_TASK_NAME,
        str(result.stderr or result.stdout or "").strip(),
    )
    return False


def _cleanup_legacy_autostart_artifacts() -> None:
    if os.name != "nt":
        return
    _remove_legacy_task_scheduler_autostart()
    _remove_legacy_registry_autostart()


class LocalAPIHandler(BaseHTTPRequestHandler):
    app_ref: "DesktopApp" = None  # type: ignore[assignment]

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("local_api " + format, *args)

    def _send(self, payload: dict[str, Any], status_code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _auth_ok(self, *, sensitive: bool) -> bool:
        token = LOCAL_API_TOKEN
        provided = str(self.headers.get("X-Local-Token") or "").strip()
        if token:
            return hmac.compare_digest(provided, token)
        return not sensitive

    def _require_auth(self, *, sensitive: bool) -> bool:
        if self._auth_ok(sensitive=sensitive):
            return True
        self._send({"ok": False, "message": "unauthorized"}, 401)
        return False

    def do_GET(self) -> None:
        app = self.app_ref
        if self.path.startswith("/status"):
            if not self._require_auth(sensitive=False):
                return
            self._send(app.build_status())
            return
        if self.path.startswith("/scan_qmt"):
            if not self._require_auth(sensitive=False):
                return
            self._send(scan_qmt_installations())
            return
        if self.path.startswith("/open_gui"):
            if not self._require_auth(sensitive=False):
                return
            app.request_show_window()
            self._send({"ok": True})
            return
        self._send({"ok": False, "message": "not found"}, 404)

    def do_POST(self) -> None:
        app = self.app_ref
        payload = self._read_json()
        if self.path == "/start":
            if not self._require_auth(sensitive=True):
                return
            self._send(app.runtime.start(app.current_config_snapshot()))
            return
        if self.path == "/stop":
            if not self._require_auth(sensitive=True):
                return
            self._send(app.runtime.stop())
            return
        if self.path == "/restart":
            if not self._require_auth(sensitive=True):
                return
            self._send(app.runtime.restart(app.current_config_snapshot()))
            return
        if self.path == "/test_qmt":
            if not self._require_auth(sensitive=False):
                return
            self._send(app.runtime.test_qmt(app.current_config_snapshot()))
            return
        if self.path == "/test_bridge":
            if not self._require_auth(sensitive=False):
                return
            self._send(app.runtime.test_bridge(app.current_config_snapshot()))
            return
        if self.path == "/save_config":
            if not self._require_auth(sensitive=True):
                return
            self._send(app.save_from_payload(payload))
            return
        self._send({"ok": False, "message": "not found"}, 404)


# ─── Helpers ────────────────────────────────────────────────────────────────

def _load_qss() -> str:
    qss_path = Path(__file__).resolve().parent / "theme.qss"
    if qss_path.exists():
        return qss_path.read_text(encoding="utf-8")
    return ""


def _make_tray_icon_pixmap() -> QPixmap:
    px = QPixmap(64, 64)
    px.fill(QColor("#5b6af7"))
    p = QPainter(px)
    p.setBrush(QColor("#ffffff"))
    p.setPen(QColor("#ffffff"))
    p.drawRect(16, 16, 32, 32)
    p.end()
    return px


def _load_app_icon() -> QIcon:
    try:
        if APP_ICON_PATH.exists():
            icon = QIcon(str(APP_ICON_PATH))
            if not icon.isNull():
                return icon
    except Exception:
        pass
    return QIcon(_make_tray_icon_pixmap())


def _card(parent: QWidget | None = None) -> QFrame:
    f = QFrame(parent)
    f.setObjectName("card")
    return f


def _card_title(text: str, parent: QWidget | None = None) -> QLabel:
    lbl = QLabel(text.upper(), parent)
    lbl.setObjectName("cardTitle")
    return lbl


def _label_muted(text: str, parent: QWidget | None = None) -> QLabel:
    lbl = QLabel(text, parent)
    lbl.setObjectName("labelMuted")
    return lbl


def _label_value(text: str, parent: QWidget | None = None) -> QLabel:
    lbl = QLabel(text, parent)
    lbl.setObjectName("labelValue")
    return lbl


def _divider(parent: QWidget | None = None) -> QFrame:
    f = QFrame(parent)
    f.setObjectName("divider")
    f.setFixedHeight(1)
    f.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    return f


def _primary_btn(text: str, parent: QWidget | None = None) -> QPushButton:
    btn = QPushButton(text, parent)
    btn.setObjectName("btnPrimary")
    return btn


def _danger_btn(text: str, parent: QWidget | None = None) -> QPushButton:
    btn = QPushButton(text, parent)
    btn.setObjectName("btnDanger")
    return btn


def _success_btn(text: str, parent: QWidget | None = None) -> QPushButton:
    btn = QPushButton(text, parent)
    btn.setObjectName("btnSuccess")
    return btn


def _action_btn(text: str, parent: QWidget | None = None) -> QPushButton:
    btn = QPushButton(text, parent)
    btn.setObjectName("btnAction")
    btn.setMinimumHeight(36)
    btn.setMinimumWidth(138)
    return btn


def _status_dot(parent: QWidget | None = None) -> QLabel:
    dot = QLabel(parent)
    dot.setObjectName("statusDot")
    dot.setFixedSize(10, 10)
    return dot


def _update_dot_color(dot: QLabel, value: str) -> None:
    color = (
        "#10b981" if value in ("运行中", "已连接") else
        "#ef4444" if value in ("未运行", "错误") else
        "#f59e0b"
    )
    dot.setStyleSheet(f"background-color:{color}; border-radius:5px;")


def _status_badge(text: str, ok: bool | None = None, parent: QWidget | None = None) -> QLabel:
    lbl = QLabel(text, parent)
    if ok is True:
        lbl.setObjectName("badgeGreen")
    elif ok is False:
        lbl.setObjectName("badgeRed")
    else:
        lbl.setObjectName("badgeYellow")
    return lbl


class BinarySwitch(QWidget):
    def __init__(self, checked: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("toggleWrap")
        self.setFixedWidth(168)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._checked = False
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._off_btn = QRadioButton("关闭", self)
        self._off_btn.setObjectName("toggleRadio")
        self._on_btn = QRadioButton("开启", self)
        self._on_btn.setObjectName("toggleRadio")
        self._group.addButton(self._off_btn, 0)
        self._group.addButton(self._on_btn, 1)
        self._off_btn.clicked.connect(lambda: self.setChecked(False))
        self._on_btn.clicked.connect(lambda: self.setChecked(True))
        layout.addWidget(self._off_btn, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._on_btn, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.setChecked(checked)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, value: bool) -> None:
        checked = bool(value)
        self._checked = checked
        self._off_btn.setChecked(not checked)
        self._on_btn.setChecked(checked)

    def setEnabled(self, enabled: bool) -> None:  # type: ignore[override]
        super().setEnabled(enabled)
        self._off_btn.setEnabled(enabled)
        self._on_btn.setEnabled(enabled)


# ─── DesktopApp (PySide6) ────────────────────────────────────────────────────

class _RefreshSignal(QObject):
    refresh = Signal()
    action_done = Signal(str, object)
    scan_done = Signal(object)
    show_window = Signal()
    update_available = Signal(object)


class DesktopApp(QMainWindow):
    def __init__(self, start_hidden: bool = False) -> None:
        super().__init__()
        self.data = load_saved_config()
        self._data_lock = threading.RLock()
        self._sig = _RefreshSignal()
        self._sig.refresh.connect(self.refresh_status)
        self._sig.action_done.connect(self._on_async_action_done)
        self._sig.scan_done.connect(self._on_scan_complete)
        self._sig.show_window.connect(self._show_window_now)
        self._sig.update_available.connect(self._on_update_available)
        self.runtime = DesktopRuntime(on_state_change=lambda: self._sig.refresh.emit())
        self._async_action_lock = threading.RLock()
        self._async_action_running = False
        self._action_buttons: list[QPushButton] = []

        # Update checker
        self._update_checker = get_update_checker()
        self._update_available: bool = False
        self._update_info: Optional[UpdateCheckResult] = None

        self.setWindowTitle("QuantMind QMT Agent")
        self.resize(1220, 820)
        self.setMinimumSize(1100, 720)
        self.setWindowIcon(_load_app_icon())

        # Status values
        self._status_text = "未启动"
        self._qmt_text = "未知"
        self._bridge_text = "未知"
        self._message_text = ""
        self._overview_vals: dict[str, str] = {
            "started_at": "无数据",
            "heartbeat": "无数据",
            "account_report": "无数据",
            "runtime_health": "未知",
            "dispatch_queue": "无数据",
            "dispatch_latency": "无数据",
            "fault_layer": "未知",
            "fault_reason": "无",
            "fault_action": "无",
            "version": APP_VERSION,
            "hostname": str(self.data.get("hostname") or socket.gethostname()),
            "fingerprint": str(self.data.get("client_fingerprint") or socket.gethostname()),
            "account_id": str(self.data.get("account_id") or ""),
            "worker_threads": "{}",
            "last_error": "无",
        }

        # Widgets that need runtime updates
        self.var_map: dict[str, QLineEdit | QComboBox | BinarySwitch] = {}
        self.log_edit: QPlainTextEdit | None = None
        self.error_edit: QPlainTextEdit | None = None
        self.diag_edit: QPlainTextEdit | None = None
        self._status_dots: dict[str, QLabel] = {}
        self._status_labels: dict[str, QLabel] = {}
        self._sidebar_agent_dot: QLabel | None = None
        self._sidebar_agent_text: QLabel | None = None
        self._msg_label: QLabel | None = None
        self._diag_status_label: QLabel | None = None
        self._ctrl_start_btn: QPushButton | None = None
        self._ctrl_stop_btn: QPushButton | None = None
        self._ctrl_restart_btn: QPushButton | None = None
        self._scan_btn: QPushButton | None = None
        self._toast_frame: QFrame | None = None
        self._toast_icon: QLabel | None = None
        self._toast_text: QLabel | None = None
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self._hide_toast)

        self._build_ui()
        self._init_toast()
        self._start_local_api()
        self._setup_tray()
        self._setup_refresh_timer()

        if start_hidden and bool(self.data.get("minimize_to_tray", True)):
            self.hide()
        else:
            self.show()

    def _is_ui_thread(self) -> bool:
        return threading.current_thread() is threading.main_thread()

    def _config_base_snapshot(self) -> dict[str, Any]:
        with self._data_lock:
            return dict(self.data)

    def current_config_snapshot(self) -> dict[str, Any]:
        return normalize_agent_config_data(self._config_base_snapshot())

    def _update_snapshot(self, cfg: dict[str, Any]) -> None:
        with self._data_lock:
            self.data = dict(cfg)

    def _apply_config_to_widgets(self, cfg: dict[str, Any]) -> None:
        for key, widget in self.var_map.items():
            if key not in cfg:
                continue
            if isinstance(widget, QLineEdit):
                widget.setText(str(cfg.get(key) or ""))
            elif isinstance(widget, BinarySwitch):
                widget.setChecked(bool(cfg.get(key)))
            elif isinstance(widget, QComboBox):
                value = str(cfg.get(key) or "")
                idx = widget.findText(value)
                if idx >= 0:
                    widget.setCurrentIndex(idx)

    def request_show_window(self) -> None:
        self._sig.show_window.emit()

    # ── Build UI ─────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # Sidebar
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(190)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 12)
        sidebar_layout.setSpacing(0)

        logo_area = QWidget()
        logo_area.setObjectName("logoArea")
        logo_layout = QVBoxLayout(logo_area)
        logo_layout.setContentsMargins(16, 20, 16, 12)
        logo_layout.setSpacing(2)
        logo_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        logo_main = QLabel("QuantMind")
        logo_main.setObjectName("logoText")
        logo_main.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        logo_sub = QLabel("QMT Agent")
        logo_sub.setObjectName("logoSub")
        logo_sub.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        logo_layout.addWidget(logo_main)
        logo_layout.addWidget(logo_sub)
        sidebar_layout.addWidget(logo_area)
        sidebar_layout.addWidget(_divider())

        self._nav = QListWidget()
        self._nav.setObjectName("navList")
        nav_items = [("概览", 0), ("配置", 1), ("控制", 2), ("诊断", 3)]
        for label, _ in nav_items:
            item = QListWidgetItem(label)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._nav.addItem(item)
        self._nav.setCurrentRow(0)
        sidebar_layout.addWidget(self._nav)
        sidebar_layout.addStretch()

        # Status summary at bottom of sidebar
        sidebar_layout.addWidget(_divider())
        agent_row = QWidget()
        agent_row_layout = QHBoxLayout(agent_row)
        agent_row_layout.setContentsMargins(16, 10, 16, 4)
        agent_row_layout.setSpacing(8)
        self._sidebar_agent_dot = _status_dot()
        _update_dot_color(self._sidebar_agent_dot, self._status_text)
        self._sidebar_agent_text = _label_muted("Agent 未运行")
        agent_row_layout.addWidget(self._sidebar_agent_dot)
        agent_row_layout.addWidget(self._sidebar_agent_text)
        help_btn = QPushButton("帮助中心")
        help_btn.setObjectName("sidebarHelpBtn")
        help_btn.clicked.connect(self.open_help_center)
        agent_row_layout.addStretch()
        agent_row_layout.addWidget(help_btn)
        sidebar_layout.addWidget(agent_row)

        # Stack
        self._stack = QStackedWidget()
        pages = [
            self._build_overview_page,
            self._build_config_page,
            self._build_control_page,
            self._build_diag_page,
        ]
        for builder in pages:
            page = QWidget()
            page.setObjectName("pageBody")
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(24, 20, 24, 20)
            page_layout.setSpacing(16)
            builder(page, page_layout)
            scroll = QScrollArea()
            scroll.setObjectName("pageScroll")
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.viewport().setObjectName("pageViewport")
            self._stack.addWidget(scroll)

        self._nav.currentRowChanged.connect(self._stack.setCurrentIndex)
        root_layout.addWidget(sidebar)
        root_layout.addWidget(self._stack, 1)

    def _init_toast(self) -> None:
        toast = QFrame(self)
        toast.setObjectName("toast")
        toast.hide()
        toast_layout = QHBoxLayout(toast)
        toast_layout.setContentsMargins(12, 10, 10, 10)
        toast_layout.setSpacing(8)
        icon = QLabel("ℹ")
        icon.setObjectName("toastIcon")
        text = QLabel("")
        text.setObjectName("toastText")
        text.setWordWrap(True)
        close_btn = QPushButton("×")
        close_btn.setObjectName("toastClose")
        close_btn.setFixedSize(20, 20)
        close_btn.clicked.connect(self._hide_toast)
        toast_layout.addWidget(icon)
        toast_layout.addWidget(text, 1)
        toast_layout.addWidget(close_btn)
        self._toast_frame = toast
        self._toast_icon = icon
        self._toast_text = text

    def _hide_toast(self) -> None:
        if self._toast_frame is not None:
            self._toast_frame.hide()

    def _position_toast(self) -> None:
        if self._toast_frame is None or not self._toast_frame.isVisible():
            return
        x = max(16, self.width() - self._toast_frame.width() - 24)
        self._toast_frame.move(x, 16)

    def _show_toast(self, message: str, level: str = "info", duration_ms: int = 2800) -> None:
        if self._toast_frame is None or self._toast_text is None or self._toast_icon is None:
            return
        text = str(message or "").strip()
        if not text:
            return
        icon_map = {"success": "✓", "warning": "!", "error": "✕", "info": "ℹ"}
        self._toast_icon.setText(icon_map.get(level, "ℹ"))
        self._toast_text.setText(text)
        self._toast_frame.setProperty("level", level)
        self._toast_frame.style().unpolish(self._toast_frame)
        self._toast_frame.style().polish(self._toast_frame)
        max_width = max(280, min(560, self.width() - 48))
        self._toast_text.setMaximumWidth(max_width - 90)
        self._toast_frame.adjustSize()
        size = self._toast_frame.sizeHint()
        self._toast_frame.resize(min(max_width, size.width()), max(44, size.height()))
        self._toast_frame.show()
        self._toast_frame.raise_()
        self._position_toast()
        self._toast_timer.start(max(1200, duration_ms))

    def _build_overview_page(self, parent: QWidget, layout: QVBoxLayout) -> None:
        title_row = QHBoxLayout()
        title = QLabel("系统概览")
        title.setObjectName("pageTitle")
        title_row.addWidget(title)
        title_row.addStretch()
        self._msg_label = QLabel("")
        self._msg_label.setObjectName("labelMuted")
        self._msg_label.setWordWrap(True)
        title_row.addWidget(self._msg_label)
        layout.addLayout(title_row)

        # Status cards row
        cards_row = QHBoxLayout()
        cards_row.setSpacing(12)
        for key, label, init in [
            ("agent", "Agent 状态", "未启动"),
            ("qmt", "QMT 连接", "未知"),
            ("bridge", "云端连接", "未知"),
        ]:
            card = _card()
            cl = QVBoxLayout(card)
            cl.setContentsMargins(16, 14, 16, 14)
            cl.setSpacing(8)
            title_lbl = _card_title(label)
            title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cl.addWidget(title_lbl)
            val_row = QHBoxLayout()
            dot = _status_dot()
            val_lbl = QLabel(init)
            val_lbl.setObjectName("labelValue")
            val_lbl.setStyleSheet("font-size:16px; font-weight:700;")
            _update_dot_color(dot, init)
            val_row.addStretch()
            val_row.addWidget(dot)
            val_row.addSpacing(8)
            val_row.addWidget(val_lbl)
            val_row.addStretch()
            cl.addLayout(val_row)
            self._status_dots[key] = dot
            self._status_labels[key] = val_lbl
            cards_row.addWidget(card)
        cards_row.setStretch(0, 1)
        cards_row.setStretch(1, 1)
        cards_row.setStretch(2, 1)
        layout.addLayout(cards_row)

        # Info card
        info_card = _card()
        info_layout = QVBoxLayout(info_card)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(0)
        header = QWidget()
        header.setObjectName("cardHeader")
        hhl = QHBoxLayout(header)
        hhl.setContentsMargins(16, 10, 16, 10)
        hhl.addWidget(_card_title("运行信息"))
        hhl.addStretch()
        info_layout.addWidget(header)

        grid_widget = QWidget()
        grid_widget.setStyleSheet("background:transparent;")
        grid = QGridLayout(grid_widget)
        grid.setContentsMargins(16, 14, 16, 14)
        grid.setHorizontalSpacing(32)
        grid.setVerticalSpacing(12)
        items = [
            ("最近启动", "started_at"),
            ("最近心跳", "heartbeat"),
            ("最近账户快照", "account_report"),
            ("运行健康", "runtime_health"),
            ("派单队列", "dispatch_queue"),
            ("排队延迟", "dispatch_latency"),
            ("故障分流", "fault_layer"),
            ("分流原因", "fault_reason"),
            ("处理建议", "fault_action"),
            ("当前版本", "version"),
            ("主机名", "hostname"),
            ("客户端指纹", "fingerprint"),
            ("当前资金账号", "account_id"),
            ("工作线程", "worker_threads"),
            ("最后错误", "last_error"),
        ]
        self._overview_labels: dict[str, QLabel] = {}
        for i, (label, key) in enumerate(items):
            row, col = divmod(i, 2)
            lbl = _label_muted(label)
            lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            grid.addWidget(lbl, row, col * 2)

            val = QLabel(self._overview_vals[key])
            val.setObjectName("labelValue")
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            val.setWordWrap(True)
            val.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            grid.addWidget(val, row, col * 2 + 1)
            self._overview_labels[key] = val
        info_layout.addWidget(grid_widget)
        layout.addWidget(info_card)
        layout.addStretch()

    def _build_config_page(self, parent: QWidget, layout: QVBoxLayout) -> None:
        title = QLabel("连接配置")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        def _section(heading: str) -> tuple[QFrame, QFormLayout]:
            card = _card()
            vl = QVBoxLayout(card)
            vl.setContentsMargins(0, 0, 0, 0)
            vl.setSpacing(0)
            hdr = QWidget()
            hdr.setObjectName("cardHeader")
            hl = QHBoxLayout(hdr)
            hl.setContentsMargins(16, 10, 16, 10)
            hl.addWidget(_card_title(heading))
            vl.addWidget(hdr)
            body = QWidget()
            body.setStyleSheet("background:transparent;")
            fl = QFormLayout(body)
            fl.setContentsMargins(16, 14, 16, 14)
            fl.setVerticalSpacing(10)
            fl.setHorizontalSpacing(20)
            fl.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            vl.addWidget(body)
            layout.addWidget(card)
            return card, fl

        def _field(form: QFormLayout, label: str, key: str,
                   placeholder: str = "", password: bool = False,
                   readonly: bool = False) -> QLineEdit:
            le = QLineEdit()
            le.setPlaceholderText(placeholder)
            if password:
                le.setEchoMode(QLineEdit.EchoMode.Password)
            if readonly:
                le.setReadOnly(True)
            le.setText(str(self.data.get(key) or ""))
            form.addRow(_label_muted(label + " :"), le)
            self.var_map[key] = le
            return le

        def _toggle_field(form: QFormLayout, label: str, key: str) -> BinarySwitch:
            sw = BinarySwitch(checked=bool(self.data.get(key, False)))
            form.addRow(_label_muted(label + " :"), sw)
            self.var_map[key] = sw
            return sw

        def _int_field(form: QFormLayout, label: str, key: str) -> QLineEdit:
            le = QLineEdit()
            le.setText(str(self.data.get(key) or ""))
            form.addRow(_label_muted(label + " :"), le)
            self.var_map[key] = le
            return le

        # Cloud connection
        _, f1 = _section("云端连接")
        _field(f1, "API 地址", "api_base_url", "https://api.quantmind.cloud/api/v1")
        _field(f1, "WebSocket 地址", "server_url", "wss://api.quantmind.cloud/ws/bridge")
        _field(f1, "Access Key", "access_key")
        _field(f1, "Secret Key", "secret_key", password=True)

        # Account
        _, f2 = _section("账号信息")
        _field(f2, "资金账号", "account_id", "如 2057898")
        _field(f2, "租户 ID", "tenant_id", "默认 default")
        _field(f2, "User ID", "user_id")
        _field(f2, "客户端指纹", "client_fingerprint", readonly=False)
        _field(f2, "主机名", "hostname", readonly=False)

        # QMT paths
        _, f3 = _section("QMT 路径")
        _field(f3, "userdata_mini 路径", "qmt_path")
        _field(f3, "bin.x64 路径", "qmt_bin_path")
        btn_row = QHBoxLayout()
        scan_btn = _action_btn("扫描 QMT 客户端")
        self._scan_btn = scan_btn
        scan_btn.clicked.connect(self.scan_and_fill)
        choose_data_btn = _action_btn("选择 userdata_mini")
        choose_data_btn.clicked.connect(lambda: self._choose_directory("qmt_path"))
        choose_bin_btn = _action_btn("选择 bin.x64")
        choose_bin_btn.clicked.connect(lambda: self._choose_directory("qmt_bin_path"))
        btn_row.addWidget(scan_btn)
        btn_row.addWidget(choose_data_btn)
        btn_row.addWidget(choose_bin_btn)
        btn_row.addStretch()
        f3.addRow("", btn_row)

        # Advanced
        _, f4 = _section("高级参数")
        _int_field(f4, "心跳间隔 (秒)", "heartbeat_interval_seconds")
        _int_field(f4, "账户快照间隔 (秒)", "account_report_interval_seconds")
        _int_field(f4, "重连间隔 (秒)", "reconnect_interval_seconds")
        _int_field(f4, "WS Ping 间隔 (秒)", "ws_ping_interval_seconds")
        _int_field(f4, "WS Ping 超时 (秒)", "ws_ping_timeout_seconds")
        _toggle_field(f4, "最小化到托盘", "minimize_to_tray")

        # Save button row
        save_row = QHBoxLayout()
        save_btn = _primary_btn("保存配置")
        save_btn.clicked.connect(self.save_config_ui)
        save_restart_btn = QPushButton("保存并重启 Agent")
        save_restart_btn.clicked.connect(self.save_and_restart)
        self._action_buttons.append(save_restart_btn)
        save_row.addStretch()
        save_row.addWidget(save_btn)
        save_row.addWidget(save_restart_btn)
        layout.addLayout(save_row)
        layout.addStretch()

    def _build_control_page(self, parent: QWidget, layout: QVBoxLayout) -> None:
        title = QLabel("Agent 控制")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        # Control card
        ctrl_card = _card()
        ctrl_vl = QVBoxLayout(ctrl_card)
        ctrl_vl.setContentsMargins(0, 0, 0, 0)
        ctrl_vl.setSpacing(0)
        hdr = QWidget()
        hdr.setObjectName("cardHeader")
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(16, 10, 16, 10)
        hl.addWidget(_card_title("Agent 运行控制"))
        ctrl_vl.addWidget(hdr)

        btn_area = QWidget()
        btn_area.setStyleSheet("background:transparent;")
        btn_layout = QHBoxLayout(btn_area)
        btn_layout.setContentsMargins(16, 14, 16, 14)
        btn_layout.setSpacing(10)

        start_btn = _action_btn("启动 Agent")
        stop_btn = _action_btn("停止 Agent")
        restart_btn = _action_btn("重启 Agent")
        test_bridge_btn = _action_btn("测试云端连接")
        test_qmt_btn = _action_btn("测试 QMT 连接")
        self._ctrl_start_btn = start_btn
        self._ctrl_stop_btn = stop_btn
        self._ctrl_restart_btn = restart_btn

        start_btn.clicked.connect(lambda: self._do_action(lambda: self.runtime.start(self.current_config())))
        stop_btn.clicked.connect(lambda: self._do_action(self.runtime.stop))
        restart_btn.clicked.connect(lambda: self._do_action(lambda: self.runtime.restart(self.current_config())))
        test_bridge_btn.clicked.connect(self.test_bridge)
        test_qmt_btn.clicked.connect(self.test_qmt)

        for btn in (start_btn, stop_btn, restart_btn, test_bridge_btn, test_qmt_btn):
            btn_layout.addWidget(btn)
        self._action_buttons.extend([test_bridge_btn, test_qmt_btn])
        btn_layout.addStretch()
        ctrl_vl.addWidget(btn_area)
        layout.addWidget(ctrl_card)

        # Log level
        log_card = _card()
        log_vl = QVBoxLayout(log_card)
        log_vl.setContentsMargins(0, 0, 0, 0)
        hdr2 = QWidget()
        hdr2.setObjectName("cardHeader")
        hl2 = QHBoxLayout(hdr2)
        hl2.setContentsMargins(16, 10, 16, 10)
        hl2.addWidget(_card_title("日志级别"))
        hl2.addStretch()
        self._log_level_combo = QComboBox()
        self._log_level_combo.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self._log_level_combo.setCurrentText(logging.getLevelName(logger.getEffectiveLevel()))
        apply_level_btn = QPushButton("应用")
        apply_level_btn.clicked.connect(self.update_log_level)
        hl2.addWidget(self._log_level_combo)
        hl2.addWidget(apply_level_btn)
        log_vl.addWidget(hdr2)
        open_dir_btn = QPushButton("打开日志目录")
        open_dir_btn.setObjectName("")
        open_dir_btn.clicked.connect(self.open_log_dir)
        open_dir_btn.setContentsMargins(16, 0, 0, 0)
        log_body = QWidget()
        log_body.setStyleSheet("background:transparent;")
        lb = QHBoxLayout(log_body)
        lb.setContentsMargins(16, 10, 16, 14)
        lb.addWidget(open_dir_btn)
        lb.addStretch()
        log_vl.addWidget(log_body)
        layout.addWidget(log_card)

        # Runtime status JSON
        status_card = _card()
        status_vl = QVBoxLayout(status_card)
        status_vl.setContentsMargins(0, 0, 0, 0)
        hdr3 = QWidget()
        hdr3.setObjectName("cardHeader")
        hl3 = QHBoxLayout(hdr3)
        hl3.setContentsMargins(16, 10, 16, 10)
        hl3.addWidget(_card_title("运行时状态（JSON）"))
        hl3.addStretch()
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self.refresh_status)
        hl3.addWidget(refresh_btn)
        status_vl.addWidget(hdr3)
        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setFixedHeight(260)
        status_vl.addWidget(self.log_edit)
        layout.addWidget(status_card)
        layout.addStretch()

    def _build_diag_page(self, parent: QWidget, layout: QVBoxLayout) -> None:
        title = QLabel("诊断")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        # Error history card
        err_card = _card()
        err_vl = QVBoxLayout(err_card)
        err_vl.setContentsMargins(0, 0, 0, 0)
        hdr = QWidget()
        hdr.setObjectName("cardHeader")
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(16, 10, 16, 10)
        hl.addWidget(_card_title("最近错误"))
        hl.addStretch()
        self._diag_status_label = QLabel("未导出诊断包")
        self._diag_status_label.setObjectName("labelMuted")
        copy_err_btn = QPushButton("复制错误")
        copy_err_btn.setObjectName("btnAction")
        copy_err_btn.clicked.connect(self.copy_recent_errors)
        export_btn = _primary_btn("导出诊断包")
        export_btn.clicked.connect(self.export_diagnostics)
        hl.addWidget(self._diag_status_label)
        hl.addWidget(copy_err_btn)
        hl.addWidget(export_btn)
        err_vl.addWidget(hdr)
        self.error_edit = QPlainTextEdit()
        self.error_edit.setReadOnly(True)
        self.error_edit.setFixedHeight(160)
        err_vl.addWidget(self.error_edit)
        layout.addWidget(err_card)

        # Config summary card
        diag_card = _card()
        diag_vl = QVBoxLayout(diag_card)
        diag_vl.setContentsMargins(0, 0, 0, 0)
        hdr2 = QWidget()
        hdr2.setObjectName("cardHeader")
        hl2 = QHBoxLayout(hdr2)
        hl2.setContentsMargins(16, 10, 16, 10)
        hl2.addWidget(_card_title("当前配置摘要（脱敏）"))
        diag_vl.addWidget(hdr2)
        self.diag_edit = QPlainTextEdit()
        self.diag_edit.setReadOnly(True)
        self.diag_edit.setFixedHeight(260)
        diag_vl.addWidget(self.diag_edit)
        layout.addWidget(diag_card)
        layout.addStretch()

    # ── Config helpers ────────────────────────────────────────────────────────

    def _choose_directory(self, key: str) -> None:
        result = QFileDialog.getExistingDirectory(self, "选择目录")
        if result and key in self.var_map:
            self.var_map[key].setText(result)

    def current_config(self) -> dict[str, Any]:
        cfg = self._config_base_snapshot()
        for key, widget in self.var_map.items():
            if isinstance(widget, QLineEdit):
                cfg[key] = widget.text()
            elif isinstance(widget, BinarySwitch):
                cfg[key] = widget.isChecked()
            elif isinstance(widget, QComboBox):
                cfg[key] = widget.currentText()
        self._update_snapshot(cfg)
        return cfg

    def save_from_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        base_cfg = self.current_config() if self._is_ui_thread() else self.current_config_snapshot()
        cfg = dict(base_cfg)
        cfg.update(payload or {})
        cfg = normalize_agent_config_data(cfg)
        errors = validate_config_dict(cfg)
        if errors:
            return {"ok": False, "message": "；".join(errors)}
        self._update_snapshot(cfg)
        if self._is_ui_thread():
            self._apply_config_to_widgets(cfg)
        save_config(cfg)
        return {"ok": True, "message": "配置已保存"}

    def save_config_ui(self) -> None:
        result = self.save_from_payload({})
        self._show_result(result)

    def save_and_restart(self) -> None:
        result = self.save_from_payload({})
        if not result.get("ok"):
            self._show_result(result)
            return
        cfg = self.current_config_snapshot()
        self.run_async_action("保存并重启 Agent", lambda: self.runtime.restart(cfg))

    def build_status(self) -> dict[str, Any]:
        cfg = self.current_config() if self._is_ui_thread() else self.current_config_snapshot()
        return self.runtime.get_status(cfg)

    # ── Scan ─────────────────────────────────────────────────────────────────

    def scan_and_fill(self) -> None:
        if self._msg_label:
            self._msg_label.setText("⏳ 正在扫描全盘 QMT 安装目录，请稍候…")
        if self._scan_btn is not None:
            self._scan_btn.setEnabled(False)

        def _run() -> None:
            cfg = self.current_config_snapshot()
            seeds = [
                str(cfg.get("qmt_path") or ""),
                str(cfg.get("qmt_bin_path") or ""),
                str(Path(str(cfg.get("qmt_path") or "")).parent if str(cfg.get("qmt_path") or "").strip() else ""),
                str(Path(str(cfg.get("qmt_bin_path") or "")).parent if str(cfg.get("qmt_bin_path") or "").strip() else ""),
            ]
            result = scan_qmt_installations(seeds)
            self._sig.scan_done.emit(result)

        threading.Thread(target=_run, name="qmt-scan", daemon=True).start()

    def _on_scan_complete(self, result: dict[str, Any]) -> None:
        if self._scan_btn is not None:
            self._scan_btn.setEnabled(True)
        all_inst = result.get("all_installations") or []
        if not all_inst:
            if self._msg_label:
                self._msg_label.setText("")
            self._show_toast(
                "未找到 QMT 安装目录，请确认已安装极速终端并开启极简模式，或手动填写路径。",
                level="warning",
                duration_ms=4200,
            )
            return
        if len(all_inst) == 1:
            self._apply_qmt_installation(all_inst[0])
            if self._msg_label:
                self._msg_label.setText(f"✅ 已自动填入：{all_inst[0].get('qmt_path', '')}")
            return
        if self._msg_label:
            self._msg_label.setText(f"发现 {len(all_inst)} 个 QMT 安装，请选择")
        self._show_scan_dialog(all_inst)

    def _apply_qmt_installation(self, inst: dict[str, str]) -> None:
        if inst.get("qmt_path") and "qmt_path" in self.var_map:
            self.var_map["qmt_path"].setText(inst["qmt_path"])  # type: ignore[union-attr]
        if inst.get("qmt_bin_path") and "qmt_bin_path" in self.var_map:
            self.var_map["qmt_bin_path"].setText(inst["qmt_bin_path"])  # type: ignore[union-attr]

    def _show_scan_dialog(self, installations: list[dict[str, str]]) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("选择 QMT 安装")
        dlg.resize(640, 420)
        dlg.setModal(True)
        vl = QVBoxLayout(dlg)
        vl.setSpacing(0)
        vl.setContentsMargins(0, 0, 0, 0)

        # Header
        hdr = QWidget()
        hdr.setObjectName("cardHeader")
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(20, 16, 20, 16)
        title = QLabel("发现多个 QMT 安装")
        title.setObjectName("pageTitle")
        sub = QLabel("请选择要使用的版本")
        sub.setObjectName("labelMuted")
        hl.addWidget(title)
        hl.addStretch()
        hl.addWidget(sub)
        vl.addWidget(hdr)

        # Scroll list
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        list_widget = QWidget()
        list_layout = QVBoxLayout(list_widget)
        list_layout.setContentsMargins(16, 12, 16, 12)
        list_layout.setSpacing(8)

        bg = QButtonGroup(dlg)
        for i, inst in enumerate(installations):
            label = inst.get("label") or inst.get("qmt_path") or f"安装 {i + 1}"
            dp = inst.get("qmt_path") or "—"
            bp = inst.get("qmt_bin_path") or "（未找到）"
            row = QFrame()
            row.setObjectName("card")
            row.setCursor(Qt.CursorShape.PointingHandCursor)
            rl = QVBoxLayout(row)
            rl.setContentsMargins(14, 10, 14, 10)
            rl.setSpacing(4)
            rb = QRadioButton(label)
            rb.setChecked(i == 0)
            bg.addButton(rb, i)
            rb.setStyleSheet("font-weight:600; font-size:13px;")
            rl.addWidget(rb)
            rl.addWidget(_label_muted(f"userdata_mini: {dp}"))
            rl.addWidget(_label_muted(f"bin.x64: {bp}"))
            list_layout.addWidget(row)

        list_layout.addStretch()
        scroll.setWidget(list_widget)
        vl.addWidget(scroll)

        # Buttons
        btn_row = QWidget()
        btn_layout = QHBoxLayout(btn_row)
        btn_layout.setContentsMargins(16, 12, 16, 12)
        btn_layout.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(dlg.reject)
        confirm_btn = _primary_btn("确认选择")

        def _confirm() -> None:
            idx = bg.checkedId()
            if 0 <= idx < len(installations):
                self._apply_qmt_installation(installations[idx])
                if self._msg_label:
                    self._msg_label.setText(f"✅ 已选择：{installations[idx].get('qmt_path', '')}")
            dlg.accept()

        confirm_btn.clicked.connect(_confirm)
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(confirm_btn)
        vl.addWidget(btn_row)
        dlg.exec()

    # ── Actions ──────────────────────────────────────────────────────────────

    def _do_action(self, fn: Callable) -> None:
        result = fn()
        self._show_result(result)

    def test_qmt(self) -> None:
        cfg = self.current_config_snapshot()
        self.run_async_action("测试 QMT 连接", lambda: self.runtime.test_qmt(cfg))

    def test_bridge(self) -> None:
        cfg = self.current_config_snapshot()
        self.run_async_action("测试云端连接", lambda: self.runtime.test_bridge(cfg))

    def _set_action_busy(self, busy: bool, action_name: str = "") -> None:
        for btn in self._action_buttons:
            btn.setEnabled(not busy)
        if busy:
            if self._ctrl_start_btn is not None:
                self._ctrl_start_btn.setEnabled(False)
            if self._ctrl_stop_btn is not None:
                self._ctrl_stop_btn.setEnabled(False)
            if self._ctrl_restart_btn is not None:
                self._ctrl_restart_btn.setEnabled(False)
        else:
            status = self.build_status()
            self._update_control_button_state(bool(status.get("running")))
        if self._msg_label:
            self._msg_label.setText(f"⏳ {action_name}进行中..." if busy else self._msg_label.text())

    def run_async_action(self, action_name: str, fn: Callable[[], dict[str, Any]]) -> None:
        with self._async_action_lock:
            if self._async_action_running:
                if self._msg_label:
                    self._msg_label.setText("请等待当前操作完成")
                return
            self._async_action_running = True
        self._set_action_busy(True, action_name)

        def _run() -> None:
            try:
                result = fn()
            except Exception as exc:
                result = {"ok": False, "message": f"{action_name}失败: {exc}"}
            self._sig.action_done.emit(action_name, result)

        threading.Thread(target=_run, name=f"qmt-action-{action_name}", daemon=True).start()

    def _on_async_action_done(self, action_name: str, result: object) -> None:
        with self._async_action_lock:
            self._async_action_running = False
        self._set_action_busy(False)
        payload = result if isinstance(result, dict) else {"ok": False, "message": f"{action_name}返回异常"}
        self._show_result(payload)

    def _show_result(self, result: dict[str, Any]) -> None:
        msg = str(result.get("message") or "")
        if self._msg_label:
            self._msg_label.setText(msg)
        if not result.get("ok"):
            self.runtime._record_error(msg)
        self._show_toast(
            msg,
            level="success" if result.get("ok") else "error",
            duration_ms=2600 if result.get("ok") else 4200,
        )
        self.refresh_status()

    # ── Refresh ───────────────────────────────────────────────────────────────

    def refresh_status(self) -> None:
        status = self.build_status()
        running = bool(status.get("running"))
        runtime_health = str(status.get("runtime_health") or "未知")
        qmt_ok = bool(status.get("qmt_connected"))
        bridge_connected = bool(status.get("last_bridge_connect_at"))
        bridge_ok = bridge_connected
        dispatch_metrics = status.get("dispatch_metrics") or {}

        self._status_text = "运行中" if running and runtime_health == "healthy" else ("异常" if running else "未运行")
        self._qmt_text = "已连接" if qmt_ok else "未连接"
        self._bridge_text = "已连接" if bridge_ok and runtime_health == "healthy" else ("异常" if bridge_ok else "未连接")

        for key, text in [("agent", self._status_text), ("qmt", self._qmt_text), ("bridge", self._bridge_text)]:
            if key in self._status_dots:
                _update_dot_color(self._status_dots[key], text)
            if key in self._status_labels:
                self._status_labels[key].setText(text)
        if self._sidebar_agent_dot is not None:
            _update_dot_color(self._sidebar_agent_dot, "运行中" if running else "未运行")
        if self._sidebar_agent_text is not None:
            self._sidebar_agent_text.setText("Agent 运行中" if running else "Agent 未运行")
        self._update_control_button_state(running)

        updates = {
            "started_at": format_ts(status.get("last_start_at")),
            "heartbeat": format_ts(status.get("last_heartbeat_at")),
            "account_report": format_ts(status.get("last_account_report_at")),
            "runtime_health": runtime_health,
            "dispatch_queue": self._format_dispatch_queue(dispatch_metrics),
            "dispatch_latency": self._format_dispatch_latency(dispatch_metrics),
            "fault_layer": str((status.get("fault_triage") or {}).get("layer") or "未知"),
            "fault_reason": str((status.get("fault_triage") or {}).get("reason") or "无"),
            "fault_action": str((status.get("fault_triage") or {}).get("action") or "无"),
            "version": str(status.get("app_version") or APP_VERSION),
            "hostname": str(status.get("hostname") or self.data.get("hostname") or ""),
            "fingerprint": str(status.get("client_fingerprint") or self.data.get("client_fingerprint") or ""),
            "account_id": str(status.get("account_id") or self.current_config().get("account_id") or ""),
            "worker_threads": self._format_worker_threads(status.get("worker_threads") or {}),
            "last_error": str(status.get("last_error") or "无"),
        }
        for key, val in updates.items():
            self._overview_vals[key] = val
            if key in self._overview_labels:
                self._overview_labels[key].setText(val)

        log_json = json.dumps(
            {"status": status, "config_path": str(CONFIG_PATH), "log_path": str(LOG_PATH), "version": APP_VERSION},
            ensure_ascii=False, indent=2,
        )
        if self.log_edit is not None:
            self.log_edit.setPlainText(log_json)
        if self.error_edit is not None:
            self.error_edit.setPlainText(json.dumps(status.get("error_history") or [], ensure_ascii=False, indent=2))
        if self.diag_edit is not None:
            self.diag_edit.setPlainText(json.dumps(self.build_redacted_diagnostics(status), ensure_ascii=False, indent=2))

    def _format_worker_threads(self, threads: dict[str, bool]) -> str:
        if not threads:
            return "{}"
        lines = []
        for name, alive in threads.items():
            status = "✓" if alive else "✗"
            lines.append(f"{name}: {status}")
        return "\n".join(lines)

    def _format_dispatch_queue(self, metrics: dict[str, Any]) -> str:
        if not metrics:
            return "无数据"
        queue_size = int(metrics.get("queue_size") or 0)
        queue_maxsize = int(metrics.get("queue_maxsize") or 0)
        dropped = int(metrics.get("dropped") or 0)
        processed = int(metrics.get("processed") or 0)
        max_depth = int(metrics.get("max_queue_depth") or 0)
        submit_kind = str(metrics.get("last_submit_kind") or "无")
        interval_ms = int(metrics.get("submit_interval_ms") or 0)
        return (
            f"当前 {queue_size}/{queue_maxsize}\n"
            f"累计处理 {processed}，累计丢弃 {dropped}\n"
            f"峰值深度 {max_depth}，最近提交 {submit_kind}，节流 {interval_ms}ms"
        )

    def _format_dispatch_latency(self, metrics: dict[str, Any]) -> str:
        if not metrics:
            return "无数据"
        wait_ms = int(metrics.get("last_queue_wait_ms") or 0)
        last_submit_at = format_ts(metrics.get("last_submit_at"))
        return f"最近排队 {wait_ms} ms\n最近提交 {last_submit_at}"

    def _setup_refresh_timer(self) -> None:
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh_status)
        self._timer.start(3000)
        self.refresh_status()

    def _update_control_button_state(self, running: bool) -> None:
        if self._ctrl_start_btn is None or self._ctrl_stop_btn is None or self._ctrl_restart_btn is None:
            return
        if self._async_action_running:
            return
        self._ctrl_start_btn.setEnabled(not running)
        self._ctrl_stop_btn.setEnabled(running)
        self._ctrl_restart_btn.setEnabled(running)

    # ── Diagnostics ──────────────────────────────────────────────────────────

    def build_redacted_diagnostics(self, status: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        current = dict(self.current_config())
        return {
            "status": status or self.build_status(),
            "config_summary": {
                "api_base_url": current.get("api_base_url"),
                "server_url": current.get("server_url"),
                "access_key": mask_sensitive(str(current.get("access_key") or "")),
                "secret_key": mask_sensitive(str(current.get("secret_key") or "")),
                "account_id": current.get("account_id"),
                "qmt_path": current.get("qmt_path"),
                "qmt_bin_path": current.get("qmt_bin_path"),
                "session_id": current.get("session_id"),
                "heartbeat_interval_seconds": current.get("heartbeat_interval_seconds"),
                "account_report_interval_seconds": current.get("account_report_interval_seconds"),
            },
            "paths": {
                "config_path": str(CONFIG_PATH),
                "log_path": str(LOG_PATH),
                "diagnostic_dir": str(DIAG_DIR),
            },
        }

    def export_diagnostics(self) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_path = DIAG_DIR / f"qmt_agent_diag_{timestamp}.zip"
        diag_json_path = DIAG_DIR / f"qmt_agent_diag_{timestamp}.json"
        diag_payload = self.build_redacted_diagnostics()
        diag_json_path.write_text(
            json.dumps(diag_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(diag_json_path, arcname=diag_json_path.name)
            if CONFIG_PATH.exists():
                zf.write(CONFIG_PATH, arcname="config.json")
            if LOG_PATH.exists():
                zf.write(LOG_PATH, arcname="desktop.log")
        msg = f"诊断包已导出到 {archive_path}"
        if self._diag_status_label:
            self._diag_status_label.setText(f"已导出：{archive_path.name}")
        if self._msg_label:
            self._msg_label.setText(msg)

    def copy_recent_errors(self) -> None:
        if self.error_edit is None:
            return
        text = self.error_edit.toPlainText().strip()
        if not text:
            text = "[]"
        QApplication.clipboard().setText(text)
        if self._msg_label:
            self._msg_label.setText("最近错误已复制到剪贴板")
        self._show_toast("最近错误已复制到剪贴板", level="success", duration_ms=2200)

    def update_log_level(self) -> None:
        level_name = self._log_level_combo.currentText().upper()
        level = getattr(logging, level_name, logging.INFO)
        _set_runtime_log_level(level)
        if self._msg_label:
            self._msg_label.setText(f"日志级别已切换为 {level_name}")

    def open_log_dir(self) -> None:
        path = LOG_PATH.parent
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])

    def open_help_center(self) -> None:
        dlg = QDialog(self)
        dlg.setObjectName("helpDialog")
        dlg.setWindowTitle("QMT Agent 帮助")
        dlg.resize(720, 480)
        dlg.setModal(True)
        dlg.setStyleSheet(
            """
            QDialog#helpDialog {
                background: #ffffff;
                color: #111827;
            }
            QDialog#helpDialog QLabel {
                color: #111827;
            }
                color: #6b7280;
            }
            QDialog#helpDialog QPlainTextEdit {
                background: #ffffff;
                color: #111827;
                border: 1px solid #d1d5db;
                border-radius: 6px;
                padding: 8px;
                selection-background-color: #dbeafe;
            }
            QDialog#helpDialog QPushButton {
                background: #ffffff;
                color: #111827;
                border: 1px solid #d1d5db;
                border-radius: 6px;
                padding: 4px 12px;
                min-height: 0px;
            }
            QDialog#helpDialog QPushButton:hover {
                background: #f9fafb;
            }
            QDialog#helpDialog QPushButton:pressed {
                background: #f3f4f6;
            }
            """
        )

        vl = QVBoxLayout(dlg)
        vl.setContentsMargins(16, 14, 16, 14)
        vl.setSpacing(8)

        title = QLabel("QMT Agent 本地帮助")
        title.setStyleSheet("font-size: 16px; font-weight: 600;")
        subtitle = QLabel("包含启动命令、日志目录和常用排障入口。")
        subtitle.setObjectName("labelMuted")
        vl.addWidget(title)
        vl.addWidget(subtitle)

        help_text = QPlainTextEdit()
        help_text.setReadOnly(True)
        help_text.setFont(QFont("Consolas", 10))
        help_text.setPlainText(_load_help_text())
        help_text.setMinimumHeight(300)
        vl.addWidget(help_text, 1)

        btn_row = QWidget()
        btn_layout = QHBoxLayout(btn_row)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.addStretch()
        button_box = QDialogButtonBox()
        open_web_btn = QPushButton("打开在线帮助")
        open_web_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://www.quantmindai.cn/help")))
        copy_btn = QPushButton("复制帮助内容")

        def _copy_help() -> None:
            QApplication.clipboard().setText(help_text.toPlainText())
            if self._msg_label:
                self._msg_label.setText("帮助内容已复制到剪贴板")

        copy_btn.clicked.connect(_copy_help)
        button_box.addButton(open_web_btn, QDialogButtonBox.ButtonRole.ActionRole)
        button_box.addButton(copy_btn, QDialogButtonBox.ButtonRole.ActionRole)
        button_box.addButton("关闭", QDialogButtonBox.ButtonRole.RejectRole)
        button_box.rejected.connect(dlg.accept)
        for button in button_box.buttons():
            button.setFixedHeight(28)
        btn_layout.addWidget(button_box)
        vl.addWidget(btn_row)

        dlg.exec()

    # ── Tray ─────────────────────────────────────────────────────────────────

    def _setup_tray(self) -> None:
        self._tray = QSystemTrayIcon(_load_app_icon(), self)
        self._tray.setToolTip("QuantMind QMT Agent")

        menu = QMenu()
        open_act = QAction("打开主窗口", self)
        open_act.triggered.connect(self.show_window)
        start_act = QAction("启动 Agent", self)
        start_act.triggered.connect(lambda: self.runtime.start(self.current_config()))
        stop_act = QAction("停止 Agent", self)
        stop_act.triggered.connect(self.runtime.stop)
        restart_act = QAction("重启 Agent", self)
        restart_act.triggered.connect(lambda: self.runtime.restart(self.current_config()))
        log_act = QAction("打开日志目录", self)
        log_act.triggered.connect(self.open_log_dir)

        # Update action
        self._check_update_act = QAction("检查更新", self)
        self._check_update_act.triggered.connect(self._check_for_updates)
        self._update_act = QAction("下载新版本", self)
        self._update_act.setVisible(False)
        self._update_act.triggered.connect(self._open_update_download)

        quit_act = QAction("退出", self)
        quit_act.triggered.connect(self.exit_app)

        for act in (open_act, start_act, stop_act, restart_act, log_act):
            menu.addAction(act)
        menu.addSeparator()
        menu.addAction(self._check_update_act)
        menu.addAction(self._update_act)
        menu.addSeparator()
        menu.addAction(quit_act)

        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

        # Start background update check after 5 seconds
        QTimer.singleShot(5000, self._background_update_check)

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_window()

    # ── Update checking ────────────────────────────────────────────────────────

    def _background_update_check(self) -> None:
        """Check for updates in the background."""
        self._update_checker.check_async(
            callback=lambda result: self._sig.update_available.emit(result),
            force=False,
        )

    def _check_for_updates(self) -> None:
        """Manually check for updates."""
        if self._msg_label:
            self._msg_label.setText("正在检查更新...")
        self._update_checker.check_async(
            callback=lambda result: self._sig.update_available.emit(result),
            force=True,
        )

    def _on_update_available(self, result: UpdateCheckResult) -> None:
        """Handle update check result."""
        if result.error:
            if self._msg_label:
                self._msg_label.setText(f"检查更新失败: {result.error}")
            self._show_toast("检查更新失败", level="error", duration_ms=3000)
            return

        if result.available and result.update_info:
            self._update_available = True
            self._update_info = result
            version = result.update_info.version

            # Update tray menu
            self._update_act.setVisible(True)
            self._update_act.setText(f"下载新版本 v{version}")

            # Show notification
            if self._msg_label:
                self._msg_label.setText(f"发现新版本 v{version}")
            self._show_toast(
                f"发现新版本 v{version}，点击托盘菜单下载",
                level="info",
                duration_ms=5000,
            )

            # Update tooltip
            self._tray.setToolTip(f"QuantMind QMT Agent (有新版本 v{version})")
        else:
            if self._msg_label:
                self._msg_label.setText("当前已是最新版本")
            self._show_toast("当前已是最新版本", level="success", duration_ms=2000)

    def _open_update_download(self) -> None:
        """Open the update download page or start download."""
        if not self._update_info or not self._update_info.update_info:
            return

        download_url = self._update_info.update_info.download_url
        if download_url:
            QDesktopServices.openUrl(QUrl(download_url))
        else:
            # Fallback to update base URL
            update_url = self._update_checker.update_base_url
            QDesktopServices.openUrl(QUrl(f"{update_url}/"))

    # ── Window management ────────────────────────────────────────────────────

    def show_window(self) -> None:
        QTimer.singleShot(0, self._show_window_now)

    def _show_window_now(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event: Any) -> None:
        if bool(self.current_config().get("minimize_to_tray", True)):
            event.ignore()
            self.hide()
            if self._msg_label:
                self._msg_label.setText("已最小化到托盘，Agent 仍在后台运行")
            self._show_toast("已最小化到托盘，Agent 仍在后台运行", level="info", duration_ms=2200)
        else:
            self.exit_app()

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._position_toast()

    def exit_app(self) -> None:
        self.runtime.stop()
        QApplication.quit()

    # ── Local API ────────────────────────────────────────────────────────────

    def _start_local_api(self) -> None:
        LocalAPIHandler.app_ref = self
        self.httpd = ThreadingHTTPServer(("127.0.0.1", LOCAL_PORT), LocalAPIHandler)
        threading.Thread(target=self.httpd.serve_forever, name="qmt-agent-local-api", daemon=True).start()


def main() -> int:
    _configure_logging()

    _cleanup_legacy_autostart_artifacts()

    def _log_unhandled_exception(exc_type, exc_value, exc_traceback) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logger.critical("unhandled exception", exc_info=(exc_type, exc_value, exc_traceback))

    def _log_thread_exception(args: threading.ExceptHookArgs) -> None:
        logger.critical(
            "unhandled thread exception in %s",
            getattr(args.thread, "name", "unknown"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _log_unhandled_exception
    if hasattr(threading, "excepthook"):
        threading.excepthook = _log_thread_exception  # type: ignore[assignment]

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    start_hidden = "--background" in sys.argv
    if _handoff_to_existing_instance(start_hidden=start_hidden):
        logger.info("existing desktop instance detected, handoff completed")
        return 0

    app = QApplication(sys.argv)
    app.setWindowIcon(_load_app_icon())
    app.setStyle("Fusion")
    qss = _load_qss()
    if qss:
        app.setStyleSheet(qss)

    try:
        window = DesktopApp(start_hidden=start_hidden)
    except Exception as exc:
        print(f"QMT Agent Desktop 启动失败: {exc}", file=sys.stderr)
        return 1

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
