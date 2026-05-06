#!/usr/bin/env python3
from __future__ import annotations

import ast
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOL_ROOT = ROOT / "tools" / "qmt_agent"
DIST_ROOT = ROOT / "dist" / "qmt_agent"
SPEC_PATH = TOOL_ROOT / "qmt_agent_desktop.spec"
ISS_PATH = TOOL_ROOT / "qmt_agent_setup.iss"
VERSION_PATH = TOOL_ROOT / "version.json"
REFERENCE_TEMPLATE_PATH = ROOT / "backend" / "static" / "templates" / "bridge" / "qmt_bridge.py"
RELEASE_MANIFEST_PATH = DIST_ROOT / "latest.json"
STARTUP_CHAIN_FILES = [
    TOOL_ROOT / "desktop_app.py",
    TOOL_ROOT / "qmt_agent.py",
    TOOL_ROOT / "agent.py",
    TOOL_ROOT / "auth.py",
    TOOL_ROOT / "client.py",
    TOOL_ROOT / "config.py",
    TOOL_ROOT / "reporter.py",
    TOOL_ROOT / "_callback.py",
    TOOL_ROOT / "triage.py",
]
ZIP_PACKAGE_FILES = [
    "qmt_agent.py",
    "desktop_app.py",
    "__init__.py",
    "requirements.txt",
    "build_windows_agent.py",
    "qmt_agent_desktop.spec",
    "qmt_agent_setup.iss",
    "version.json",
    "help.md",
    "README.md",
    "_callback.py",
    "agent.py",
    "auth.py",
    "client.py",
    "config.py",
    "reporter.py",
    "triage.py",
    "theme.qss",
]


def run(cmd: list[str]) -> None:
    print("[qmt-agent-build]", " ".join(cmd))
    subprocess.run(cmd, cwd=str(ROOT), check=True)


def read_version() -> str:
    try:
        data = json.loads(VERSION_PATH.read_text(encoding="utf-8"))
        return str(data.get("version") or "1.0.0")
    except Exception:
        return "1.0.0"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _top_level_module(name: str) -> str:
    return str(name or "").split(".", 1)[0].strip()


def _is_stdlib_module(name: str) -> bool:
    top_level = _top_level_module(name)
    if not top_level:
        return True
    if top_level in {"PySide6", "requests", "websocket", "xtquant"}:
        return False
    if top_level in sys.builtin_module_names:
        return True
    stdlib_names = getattr(sys, "stdlib_module_names", set())
    return top_level in stdlib_names


def audit_startup_chain_imports() -> None:
    allowed_local = {
        "qmt_agent",
        "desktop_app",
        "agent",
        "auth",
        "client",
        "config",
        "reporter",
        "_callback",
        "triage",
    }
    allowed_external = {"PySide6", "requests", "websocket", "xtquant", "winreg"}
    unexpected: set[str] = set()

    for file_path in STARTUP_CHAIN_FILES:
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = _top_level_module(alias.name)
                    if root and root not in allowed_local and not _is_stdlib_module(root) and root not in allowed_external:
                        unexpected.add(root)
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.module is None:
                    continue
                root = _top_level_module(node.module or "")
                if root and root not in allowed_local and not _is_stdlib_module(root) and root not in allowed_external:
                    unexpected.add(root)

    if unexpected:
        joined = ", ".join(sorted(unexpected))
        raise RuntimeError(
            "Unexpected third-party imports in QMT desktop startup chain: "
            f"{joined}. Update qmt_agent_desktop.spec and the packaging smoke test."
        )


def smoke_test_desktop_chain() -> None:
    smoke_dir = Path(tempfile.mkdtemp(prefix="qmt_agent_smoke_", dir=str(DIST_ROOT)))
    try:
        for filename in (
            "desktop_app.py",
            "qmt_agent.py",
            "agent.py",
            "auth.py",
            "client.py",
            "config.py",
            "reporter.py",
            "_callback.py",
            "triage.py",
            "__init__.py",
        ):
            shutil.copy2(TOOL_ROOT / filename, smoke_dir / filename)

        smoke_script = smoke_dir / "_smoke_desktop_chain.py"
        smoke_script.write_text(
            """
import importlib.util
import sys
from pathlib import Path

bundle = Path(sys.argv[1])
sys.path.insert(0, str(bundle))
sys._MEIPASS = str(bundle)

spec = importlib.util.spec_from_file_location("desktop_app_smoke", bundle / "desktop_app.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print("desktop_app chain smoke ok")
""".strip()
            + "\n",
            encoding="utf-8",
        )
        run([sys.executable, str(smoke_script), str(smoke_dir)])
    finally:
        shutil.rmtree(smoke_dir, ignore_errors=True)


def zip_dist(version: str) -> Path:
    archive_path = DIST_ROOT / f"QuantMindQMTAgent-{version}-win64.zip"
    if archive_path.exists():
        archive_path.unlink()
    DIST_ROOT.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix="qmt_agent_bundle_", dir=str(DIST_ROOT)))
    try:
        for filename in ZIP_PACKAGE_FILES:
            src = TOOL_ROOT / filename
            if src.exists():
                shutil.copy2(src, staging_dir / filename)
        if REFERENCE_TEMPLATE_PATH.exists():
            shutil.copy2(REFERENCE_TEMPLATE_PATH, staging_dir / "qmt_agent_reference.py")
        config_payload = {
            "api_base_url": "https://api.quantmind.cloud/api/v1",
            "server_url": "wss://api.quantmind.cloud/ws/bridge",
            "access_key": "",
            "secret_key": "",
            "account_id": "",
            "tenant_id": "default",
            "user_id": "",
            "client_version": f"{version}-desktop",
            "client_fingerprint": "",
            "hostname": "",
            "qmt_path": "",
            "qmt_bin_path": "",
            "session_id": 0,
            "renew_before_seconds": 300,
            "heartbeat_interval_seconds": 20,
            "account_report_interval_seconds": 45,
            "reconnect_interval_seconds": 3,
            "ws_ping_interval_seconds": 60,
            "ws_ping_timeout_seconds": 20,
            "minimize_to_tray": True,
            "auto_start_agent": False,
            "auto_restart_on_crash": True,
            "restart_base_delay_seconds": 3,
            "restart_max_delay_seconds": 60,
            "restart_window_seconds": 600,
            "restart_max_attempts_per_window": 20,
            "enable_short_trading": False,
            "account_type": "STOCK",
            "short_check_cache_ttl_sec": 30,
            "reconcile_lookback_seconds": 86400,
            "reconcile_max_orders": 200,
            "reconcile_max_trades": 200,
            "reconcile_cancel_after_seconds": 60,
        }
        import json

        (staging_dir / "qmt_agent_config.json").write_text(
            json.dumps(config_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in sorted(staging_dir.iterdir(), key=lambda p: p.name):
                if file_path.is_file():
                    zf.write(file_path, arcname=file_path.name)
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
    print(f"[qmt-agent-build] portable_zip={archive_path}")
    return archive_path


def write_release_manifest(version: str, portable_path: Path, installer_path: Path | None = None) -> Path:
    build_time = datetime.now(timezone.utc).astimezone().isoformat()
    manifest: dict[str, object] = {
        "product": "QuantMindQMTAgent",
        "channel": "release",
        "version": version,
        "build_time": build_time,
        "manifest_key": "qmt-agent/windows/release/latest.json",
        "installer": {
            "key": f"qmt-agent/windows/release/v{version}/{installer_path.name if installer_path else f'QuantMindQMTAgent-Setup-{version}.exe'}",
            "file_name": installer_path.name if installer_path else f"QuantMindQMTAgent-Setup-{version}.exe",
            "sha256": _sha256(installer_path) if installer_path and installer_path.exists() else "",
            "content_type": "application/vnd.microsoft.portable-executable",
        }
        if installer_path is not None
        else None,
        "portable": {
            "key": f"qmt-agent/windows/release/v{version}/{portable_path.name}",
            "file_name": portable_path.name,
            "sha256": _sha256(portable_path),
            "content_type": "application/zip",
        },
    }
    RELEASE_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    RELEASE_MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[qmt-agent-build] release_manifest={RELEASE_MANIFEST_PATH}")
    return RELEASE_MANIFEST_PATH


def maybe_build_inno_installer(version: str) -> Path | None:
    if not ISS_PATH.exists():
        print("[qmt-agent-build] skip installer: missing iss script")
        return None
    iscc = shutil.which("iscc") or shutil.which("ISCC")
    # Also search common Inno Setup install locations on Windows
    if not iscc and sys.platform == "win32":
        candidates = [
            r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
            r"C:\Program Files\Inno Setup 6\ISCC.exe",
            r"C:\Program Files (x86)\Inno Setup 5\ISCC.exe",
        ]
        for c in candidates:
            if Path(c).exists():
                iscc = c
                break
    if not iscc:
        print("[qmt-agent-build] skip installer: Inno Setup Compiler (iscc) not found")
        return None
    run([iscc, str(ISS_PATH)])
    installer_path = DIST_ROOT / "installer" / f"QuantMindQMTAgent-Setup-{version}.exe"
    return installer_path if installer_path.exists() else None


def main() -> int:
    if sys.platform != "win32":
        print("This build script only supports Windows.", file=sys.stderr)
        return 1

    try:
        import PyInstaller  # noqa: F401
    except Exception:
        run([sys.executable, "-m", "pip", "install", "pyinstaller"])

    audit_startup_chain_imports()
    smoke_test_desktop_chain()

    shutil.rmtree(DIST_ROOT, ignore_errors=True)
    shutil.rmtree(ROOT / "build" / "qmt_agent", ignore_errors=True)

    run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--clean",
            "--noconfirm",
            "--distpath",
            str(DIST_ROOT),
            "--workpath",
            str(ROOT / "build" / "qmt_agent"),
            str(SPEC_PATH),
        ]
    )
    version = read_version()
    portable_path = zip_dist(version)
    installer_path = maybe_build_inno_installer(version)
    write_release_manifest(version, portable_path, installer_path)
    print(f"[qmt-agent-build] output={DIST_ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
