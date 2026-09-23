"""`/api/v1/public/sync/*` 的三条加固不变式。

**背景（2026-09-23 审计）**：这个 router 全仓无调用方（前端/测试/其他代码都没有），
却有两个生产级问题：

1. **源码里硬编码了另一台机器的生产库口令**
   （``readonly_sync:<明文>@139.199.75.121``）。口令进过 git 历史即视为已泄露。
2. **三个端点零鉴权**。它们返回全市场行情/特征快照的整表分页数据，
   任何能连到网关的人都能批量拖走。

本文件钉住修好之后的样子：

* 源码里不得再出现凭据字面量；
* 远程库地址从环境读，**未配置即拒绝**（不是回落到一个默认值——
  公开默认回退值正是 C1 事故的成因）；
* 三个端点都要鉴权。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.services.api.routers import public_sync

MODULE_PATH = Path(public_sync.__file__).resolve()
SOURCE = MODULE_PATH.read_text(encoding="utf-8")

#: 真实挂载路径 = router 自带 prefix（/public/sync） + main.py 的 /api/v1。
API = "/api/v1"
ENDPOINTS = tuple(
    f"{API}/public/sync/{name}"
    for name in ("stock-daily", "feature-snapshots", "calendar")
)


# ---------------------------------------------------------------------------
# 1. 凭据不得出现在源码里
# ---------------------------------------------------------------------------


def test_no_embedded_database_credentials() -> None:
    """源码里不得出现 `scheme://user:password@host` 形状的连接串。"""
    pattern = re.compile(r"\w+://[^/\s\"']+:[^/\s\"'@]+@", re.IGNORECASE)
    hits = [
        f"{MODULE_PATH.name}:{i}: {line.strip()}"
        for i, line in enumerate(SOURCE.splitlines(), start=1)
        if pattern.search(line) and not line.strip().startswith("#")
    ]
    assert not hits, "public_sync 源码里仍有内联凭据（进过 git 即视为已泄露）：\n" + "\n".join(hits)


def test_no_known_leaked_secret_literal() -> None:
    """旧口令字面量不得以任何形式残留（含注释/默认值/测试夹具）。"""
    assert "qm_sync_2026_readonly" not in SOURCE
    assert "139.199.75.121" not in SOURCE


# ---------------------------------------------------------------------------
# 2. 远程库地址：未配置即拒绝，且每次调用实时读取
# ---------------------------------------------------------------------------


def test_remote_url_missing_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QM_PUBLIC_SYNC_REMOTE_DB_URL", raising=False)
    # ⚠️ 必须打在 **public_sync 的模块属性**上，不是
    # `backend.shared.runtime_secrets.get_secret`。public_sync 用的是
    # `from ... import get_secret`（模块级绑定），patch 源头对它没有任何影响——
    # 那样写的时候这条测试实际上取决于跑它的机器上 config/runtime.env 里有没有
    # 「碰巧」配了这个键：本机配了就红、净检出上就绿。
    monkeypatch.setattr(public_sync, "get_secret", lambda key, default="": "")
    with pytest.raises(RuntimeError):
        public_sync.resolve_remote_db_url()


def test_remote_url_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QM_PUBLIC_SYNC_REMOTE_DB_URL", "postgresql://u:p@h:5432/d")
    assert public_sync.resolve_remote_db_url() == "postgresql://u:p@h:5432/d"


def test_remote_url_is_read_at_call_time_not_import_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """轮换后免重启即生效——C1 教训：导入期快照会让轮换静默失效。"""
    monkeypatch.setenv("QM_PUBLIC_SYNC_REMOTE_DB_URL", "postgresql://a@h/d")
    first = public_sync.resolve_remote_db_url()
    monkeypatch.setenv("QM_PUBLIC_SYNC_REMOTE_DB_URL", "postgresql://b@h/d")
    assert first != public_sync.resolve_remote_db_url()


# ---------------------------------------------------------------------------
# 3. 三个端点都要鉴权
# ---------------------------------------------------------------------------


@pytest.fixture()
def app() -> FastAPI:
    application = FastAPI()
    application.include_router(public_sync.router, prefix="/api/v1")
    return application


@pytest.mark.parametrize("path", ENDPOINTS)
def test_endpoints_reject_anonymous(app: FastAPI, path: str) -> None:
    """匿名请求不得返回数据。这是本文件存在的首要理由。"""
    client = TestClient(app)
    resp = client.get(path, params={"trade_date": "2026-01-01", "start_date": "2026-01-01"})
    assert resp.status_code in (401, 403), (
        f"{path} 匿名可访问（HTTP {resp.status_code}）——全市场整表数据可被批量拖走"
    )


@pytest.mark.parametrize("path", ENDPOINTS)
def test_endpoints_do_not_leak_data_when_anonymous(app: FastAPI, path: str) -> None:
    """即使状态码被中间件改写，响应体里也不许夹带 data 行。

    先断言路由真的存在——路径写错会得到 404，那时「响应里没有数据」是
    **假通过**（本文件第一次跑就是这样绿掉的：router 自带 prefix，测试却
    少写了 ``/api/v1``）。
    """
    client = TestClient(app)
    resp = client.get(path, params={"trade_date": "2026-01-01", "start_date": "2026-01-01"})
    assert resp.status_code != 404, (
        f"{path} 不存在（404）——路径写错了，这条断言会假通过。"
        "请核对 router 的 prefix 与 main.py 的挂载前缀。"
    )
    body = resp.text
    assert '"data": [' not in body and '"data":[' not in body, (
        f"{path} 匿名响应里夹带了数据行：{body[:200]}"
    )


# ---------------------------------------------------------------------------
# 3. DSN 在边界处校验（2026-09-23 加）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("postgresql://u:p@h:5432/d", "postgresql+asyncpg://u:p@h:5432/d"),
        # 已经是 asyncpg 的要幂等——否则会变成 postgresql+asyncpg+asyncpg://
        ("postgresql+asyncpg://u:p@h:5432/d", "postgresql+asyncpg://u:p@h:5432/d"),
    ],
)
def test_valid_dsn_is_converted_correctly(given: str, expected: str) -> None:
    assert public_sync._async_dsn(given) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "postgres://u:p@h:5432/d",  # 高频手滑：少个 ql（Heroku 的老写法）
        "mysql://u:p@h:3306/d",
        "sqlite:///tmp/x.db",
        "",
        "h:5432/d",
    ],
)
def test_unsupported_dsn_scheme_fails_fast(bad: str) -> None:
    """**核心断言**：不支持的协议在这里就拒，不是透给 SQLAlchemy 再炸。

    此前用 `str.replace` 静默处理：`postgres://`（少个 ql）原样透传，
    `create_async_engine` 报的是方言解析错——错误现场离配置处很远，
    运维看不出是**写错了配置值**。
    """
    with pytest.raises(RuntimeError) as exc:
        public_sync._async_dsn(bad)
    # 报错必须点名是哪个配置项，否则用户不知道该去改哪一行
    assert public_sync.REMOTE_DB_URL_ENV_KEY in str(exc.value)


def test_remote_url_actually_flows_through_dsn_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """接线检查：`resolve_remote_db_url` 取到的值必须真的会被 `_async_dsn` 校验。

    防「校验写了但没接上」——那样上面两条测的是个孤儿函数。
    """
    monkeypatch.setenv("QM_PUBLIC_SYNC_REMOTE_DB_URL", "mysql://u:p@h/d")
    url = public_sync.resolve_remote_db_url()  # 取值这一步不校验
    with pytest.raises(RuntimeError):
        public_sync._async_dsn(url)
