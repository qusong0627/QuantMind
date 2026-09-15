"""回归（2026-09-15 诊断）：登录路径禁止输出凭据明文。

auth_service.login() 曾以 ERROR 级别打印完整 credentials（含明文密码）与
用户名探测信息，自 OSS 初始提交（2026-05-22）起所有部署均在泄露管理员口令。
此处以源码扫描做防回归闸门（登录链路依赖 DB/JWT，用源扫描成本最低且足够）。
"""

from pathlib import Path

_AUTH_SERVICE = (
    Path(__file__).resolve().parents[1]
    / "services"
    / "api"
    / "user_app"
    / "services"
    / "auth_service.py"
)

_FORBIDDEN_MARKERS = (
    "DEBUG credentials",
    "DEBUG user found",
    "DEBUG user not found",
    "DEBUG password verify failed",
)


def test_auth_service_source_has_no_debug_credential_markers():
    src = _AUTH_SERVICE.read_text(encoding="utf-8")
    for marker in _FORBIDDEN_MARKERS:
        assert marker not in src, f"auth_service 中出现调试凭据日志: {marker}"


def test_no_log_statement_references_raw_credentials():
    src = _AUTH_SERVICE.read_text(encoding="utf-8")
    for lineno, line in enumerate(src.splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith(("logger.", "logging.")):
            continue
        lowered = stripped.lower()
        # 日志语句中禁止出现凭据对象/明文密码；password_hash 属合法哈希引用
        if "credential" in lowered or "password" in lowered:
            assert "password_hash" in lowered, f"auth_service:{lineno} 日志疑似输出凭据: {stripped}"
