"""因子研究接口必须要求登录 —— 前缀表是唯一关卡，而它漏一项不会有任何报错。

**这套东西要保护什么**

网关（`backend/services/api/routers/engine_proxy.py:54`）对**每一个**转发到 engine
的请求都无条件注入 `X-Internal-Call`，于是 `auth_middleware` 里「内部密钥匹配」
这一条在网关面前恒为真。真正决定匿名请求命运的，只有 `PROTECTED_PREFIXES` 里
有没有这个前缀。

2026-10-07 实测：`/api/v1/factor-research/` 不在表内，所以

    curl 无 Authorization  http://<host>:8000/api/v1/factor-research/scan?dataset=private

返回 **200**，把本地挖出来的因子名与来源库（私有 alpha）匿名吐了出来；对照组
`/api/v1/qlib/health` 正确地返回 401。整个过程中没有任何一层报错、也没有日志——
前缀表漏一项的表现是「静默变成公开接口」，所以它必须由测试钉住而不是靠人记得。

本文件钉三件事：

1. **匿名必须被拒** —— 请求带着正确的内部密钥（等价于「经网关转发」），但没有用户
   身份，这是网关对匿名访客的实际行为；
2. **这条断言是承重的** —— 把前缀从表里摘掉，同一个请求必须**不再**被 401 拦住。
   否则用例可能在「第一道 401 分支」上白过：密钥对不上时那条分支与前缀表无关，
   测试全绿却什么都没锁住；
3. **每项必须带结尾斜杠** —— 判定是 `str.startswith`，写成 `/api/v1/analysis` 会连
   `/api/v1/analysis-anything` 一起纳入，边界比看上去宽。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.services.engine import main as engine_main

SECRET = "unit-test-internal-secret-9f2a"
# 网关转发时的形状：内部密钥在、用户身份不在
ANON_HEADERS = {"X-Internal-Call": SECRET}
SCAN_PATH = "/api/v1/factor-research/scan?dataset=private"


@pytest.fixture(autouse=True)
def _fixed_internal_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """把服务端认的密钥钉住。

    `main.py:13` 是 `from backend.shared.auth import get_internal_call_secret`，即
    **模块级绑定**：只补丁源头 `backend.shared.auth` 对它无效，中间件读到的仍是真实
    runtime 密钥，请求会被判成「密钥不匹配」——那样第一道分支就直接 401 了，前缀表
    根本没被走到，本文件的全部用例都会变成空转。
    """
    monkeypatch.setattr(engine_main, "get_internal_call_secret", lambda: SECRET)


@pytest.fixture
def client() -> TestClient:
    """不进入 `with`：不触发 lifespan，用例因此不需要 DB / Redis。"""
    return TestClient(engine_main.app)


def test_every_protected_prefix_ends_with_slash() -> None:
    """判定用 startswith，前缀缺结尾斜杠会把同名的兄弟命名空间一并放进来。"""
    for prefix in engine_main.PROTECTED_PREFIXES:
        assert prefix.startswith("/api/v1/"), prefix
        assert prefix.endswith("/"), (
            f"{prefix} 缺结尾斜杠：`startswith` 会顺带放行 {prefix}-anything"
        )


def test_factor_research_is_registered_as_protected() -> None:
    assert "/api/v1/factor-research/" in engine_main.PROTECTED_PREFIXES


def test_anonymous_scan_is_rejected(client: TestClient) -> None:
    r = client.get(SCAN_PATH, headers=ANON_HEADERS)
    assert r.status_code == 401, r.text


def test_control_prefix_is_rejected(client: TestClient) -> None:
    """对照组：证明拦截来自中间件，而不是路由自己 401 或环境问题。"""
    r = client.get("/api/v1/qlib/health", headers=ANON_HEADERS)
    assert r.status_code == 401, r.text


def test_rejection_happens_before_the_route_runs(client: TestClient) -> None:
    """401 必须是中间件短路：响应体里不能漏出因子名。"""
    body = client.get(SCAN_PATH, headers=ANON_HEADERS).text
    assert "free_float_shares" not in body
    assert "library" not in body


def test_prefix_entry_is_load_bearing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """承重：摘掉前缀，同一个匿名请求必须不再被拦。

    前提是内部密钥**已被采信**（`_fixed_internal_secret`），否则第一道分支会替前缀表
    背锅，这条用例就永远绿。
    """
    monkeypatch.setattr(
        engine_main,
        "PROTECTED_PREFIXES",
        tuple(
            p for p in engine_main.PROTECTED_PREFIXES if p != "/api/v1/factor-research/"
        ),
    )
    r = client.get(SCAN_PATH, headers=ANON_HEADERS)
    assert r.status_code != 401, "摘掉前缀仍被 401 = 本文件没有锁住前缀表"


def test_authenticated_request_reaches_the_route(client: TestClient) -> None:
    """带身份要放行到路由——不能把登录用户一起拦掉。"""
    r = client.get(SCAN_PATH, headers={**ANON_HEADERS, "X-User-Id": "10000001"})
    assert r.status_code != 401, r.text
