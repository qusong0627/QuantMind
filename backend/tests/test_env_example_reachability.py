"""`.env.example` 里登记的每一个键，都必须真的能到达进程。

问题形状（2026-09-23 复核）
--------------------------
`docker-compose.yml` 用 **environment 白名单**（没有 `env_file:`）——只有被
`${...}` 显式引用的键才会进容器。于是「往 `.env.example` 加一行 + 在 `.env` 里填值」
这条本仓最自然的配置路径，对**没被转发**的键是**静默无效**的：

    运维按文档填了 .env → 重启 → 容器里读不到 → 功能不生效

而且与「压根没打算开」不可区分。`/api/ext/v1` 的 `EXTERNAL_API_SECRET` 正是这样：
文档教人填 `.env`，容器里却永远读不到 → 握手永远 503。

本文件钉住的规则
----------------
`.env.example` 里的每个键，必须至少满足一条：

1. **被 compose 转发**（`docker-compose.yml` 里出现 `${KEY...}`）；或
2. **在 `_RUNTIME_ENV_ONLY` 里显式登记**——即「这个键只经 `config/runtime.env`
   （compose 挂载卷，启动时 `load_runtime_env()` 注入进程环境）或 `-e` 生效」。

加新键时若不满足任何一条，本文件会红，逼作者做一次显式选择，而不是留下
「文档说有、实际没有」的第三种状态。
"""

from __future__ import annotations

import re
from pathlib import Path


def _find_root() -> Path:
    """定位仓库根（容器里 compose 把出厂文件只读挂在 /app/）。找不到即失败。"""
    seen: list[Path] = []
    for base in (Path(__file__).resolve(), Path.cwd().resolve()):
        for p in (base, *base.parents):
            if p in seen:
                continue
            seen.append(p)
            if (p / ".env.example").is_file() and (p / "docker-compose.yml").is_file():
                return p
    raise AssertionError(
        "找不到 `.env.example` / `docker-compose.yml`——本护栏无法执行。\n"
        f"已探过：{[str(p) for p in seen[:8]]}\n"
        "若在容器里跑：确认 docker-compose.yml 的 quantmind 服务挂载了这两份文件（只读）。"
    )


#: 只在 `config/runtime.env`（或 `-e`）生效、**不经 .env** 的键。
#:
#: 这些键在容器内的代码里确实被读取（`os.getenv`），只是 compose 的 environment
#: 白名单没有转发它们。填在宿主 `.env` 里不会生效——要么写 `config/runtime.env`
#: （热生效，推荐），要么在 compose 里补一行转发。
#:
#: ⚠️ 这张表是**已知缺口清单**，不是「设计如此」。往里加东西前先问一句
#: 「为什么不直接让 compose 转发它？」——转发才是用户从 `.env.example` 学到的默认路径。
_RUNTIME_ENV_ONLY: frozenset[str] = frozenset(
    {
        # 以下 10 个键 2026-09-23 复核时发现「被容器内代码读取但 compose 未转发」。
        # 保留在此是为了让缺口可见；逐条确认默认值后应当改为 compose 转发。
        "SIM_REDIS_QUOTE_MAX_AGE_SEC",  # shared/freshness.py
        "WEB_PORT",  # engine/stock_query_app/config.py 等
        "AI_IDE_MINIBT_RUNNER_IMAGE",  # engine/routers/ai_ide/executor.py
        "AI_IDE_DOCKER_TIMEOUT",  # 同上
        "QWENPAW_SHARED_FILES_DIR",  # api/routers/qwenpaw_proxy.py
        "QWENPAW_PORT",  # api/routers/admin/dashboard.py
        "QWENPAW_BIND",
        "TRAINING_AUTODL_PYTHON",  # engine/training/remote_ssh_orchestrator.py
        "TRAINING_AUTODL_EXEC_MODE",
        "TRAINING_AUTODL_QUANTDB_DIR",
    }
)


def _env_example_keys() -> list[str]:
    src = (_find_root() / ".env.example").read_text(encoding="utf-8")
    keys = []
    for line in src.splitlines():
        m = re.match(r"^([A-Z_][A-Z0-9_]*)=", line.strip())
        if m:
            keys.append(m.group(1))
    return keys


def _compose_text() -> str:
    return (_find_root() / "docker-compose.yml").read_text(encoding="utf-8")


def test_env_example_is_actually_parsed() -> None:
    """防空转：一个键都没解析出来说明格式变了，下面的断言全在空转。"""
    keys = _env_example_keys()
    assert len(keys) > 50, (
        f"只从 .env.example 解析出 {len(keys)} 个键，正则或文件格式已变"
    )
    assert "EXTERNAL_API_SECRET" in keys, "新加的对外密钥键没被解析到——扫描面失效"


def test_every_documented_key_can_actually_reach_the_process() -> None:
    """**核心断言**：文档里的每个键，要么被 compose 转发，要么显式登记为 runtime.env-only。

    第三种状态（文档有、实际读不到、也没人知道）就是 `EXTERNAL_API_SECRET` 踩过的坑。
    """
    compose = _compose_text()
    orphans = [
        k
        for k in _env_example_keys()
        if k not in _RUNTIME_ENV_ONLY
        and f"${{{k}" not in compose
        and f"- {k}=" not in compose
    ]
    assert not orphans, (
        "这些键写在 .env.example 里，但 compose 的 environment 白名单没有转发它们——"
        f"填在宿主 .env 里**不会生效**：{orphans}\n"
        "两条出路：① 在 docker-compose.yml 的 quantmind 服务里加 "
        "`- KEY=${KEY:-<默认>}`（推荐，这才是用户从 .env.example 学到的路径）；"
        "② 若确实只走 config/runtime.env，加进本文件的 _RUNTIME_ENV_ONLY 并写明理由。"
    )


def test_the_two_keys_that_were_silently_broken_are_forwarded() -> None:
    """回归：这两个键曾在文档里教人填 `.env`、而 compose 不转发。

    后果是 8000 网关上对外 API **永远 503**，且与「没打算开」不可区分。
    """
    compose = _compose_text()
    for key in ("EXTERNAL_API_SECRET", "QM_PUBLIC_SYNC_REMOTE_DB_URL"):
        assert f"${{{key}" in compose, (
            f"{key} 没被 compose 转发——按 .env.example 填 .env 不会生效"
        )


def test_forwarded_secrets_do_not_get_nonempty_public_defaults() -> None:
    """转发可以，但**不许**顺手给个非空默认值。

    `${EXTERNAL_API_SECRET:-something}` 就是 C1 与 JWT 密钥两次事故的形状：
    公开回退值被当成有效密钥。空默认 = 未配置 = 拒绝服务，才是安全方向。
    """
    compose = _compose_text()
    offenders = []
    for match in re.finditer(
        r"\$\{(EXTERNAL_API_SECRET|SECRET_KEY|JWT_SECRET_KEY|INTERNAL_CALL_SECRET):-([^}]*)\}",
        compose,
    ):
        if match.group(2):
            offenders.append(match.group(0))
    assert not offenders, f"这些根密钥被转发时带了非空默认值：{offenders}"
