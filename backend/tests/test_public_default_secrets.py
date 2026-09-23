"""公开默认密钥的护栏：**出厂配置里不得存在可作为有效值的密钥字面量**。

背景
----
本仓出过两次同形状事故，都是「公开字面量被当成有效密钥」：

* **C1（2026-09-17）**：compose 回退 ``${INTERNAL_CALL_SECRET:-changeme-internal-secret}``
  令匿名者仅凭 ``X-Internal-Call`` + ``X-User-Id`` 两枚请求头即可冒充任意用户。
* **2026-09-23 复核**：同一个洞在 **用户 JWT 签名密钥** 上原样存在，且更直接——
  ``config/settings.py`` 兜底 ``dev-secret-key``、compose 回退
  ``changeme-generate-a-random-secret``、``.env.example`` 印 ``CHANGE_ME_...``，
  三条都公开且**没有任何过滤**。拿仓库里读到的字面量自签一枚
  ``{"sub":"1","roles":["admin"]}`` 就能通过标准登录口冒充管理员（修复前已实测复现）。
  同一次复核还发现 ``.env.example:22`` 印着 ``quantmind-internal-secret``，而它
  **不在** ``_PUBLIC_INTERNAL_DEFAULTS`` 里——注释声称「代码一律视为未配置」是假的。

本文件钉住三件事
----------------
1. **过滤器得跟上出厂配置**：出厂文件里出现的每一个非空密钥字面量，都必须在过滤器里。
   （加字面量而不加过滤 → 红。）
2. **公开字面量签不出可用令牌**：拿字面量自签的 JWT 必须被拒。
3. **未配置 = 拒绝服务**：不是「回落到某个字面量」，也不是「拿空串去验签」。

⚠️ 本文件**故意扫真实文件**（`.env.example` / `docker-compose.yml`），不是扫常量副本——
否则它只会证明「我抄进测试里的那份清单和自己一致」。若哪天改了出厂文件名，下面的
防空转断言会红，提醒把扫描目标接上，而不是静默变成空转。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

import jwt as pyjwt
import pytest
from fastapi import HTTPException

from backend.shared import auth as auth_mod
from backend.shared.auth import AuthManager, get_jwt_secret

def _shipped_files() -> tuple[Path, Path]:
    """定位出厂配置（`.env.example` 与 `docker-compose.yml`），返回 (仓库根, 根)。

    测试可能跑在宿主机（cwd=仓库根），也可能跑在容器里（compose 把这两份文件
    只读挂到 `/app/`）。两处都找。**找不到就失败**——本文件的本职就是扫这两份
    文件，扫不到时静默跳过等于这条护栏不存在（这正是本仓「验收假通过」的老形状）。
    """
    seen: list[Path] = []
    for base in (Path(__file__).resolve(), Path.cwd().resolve()):
        for p in (base, *base.parents):
            if p in seen:
                continue
            seen.append(p)
            if (p / ".env.example").is_file() and (p / "docker-compose.yml").is_file():
                return p, p
    raise AssertionError(
        "找不到 `.env.example` / `docker-compose.yml`——本护栏无法执行。\n"
        f"已探过的路径：{[str(p) for p in seen[:8]]}\n"
        "若在容器里跑：确认 docker-compose.yml 的 quantmind 服务挂载了这两份文件（只读）。"
    )

#: 「冒充类」根密钥——公开即等于可冒充任意用户。其它带默认值的口令
#: （DB_PASSWORD / HUNTLY_PASSWORD / IB_VNC_PASSWORD）不在本表：它们不是鉴权根，
#: 且默认部署里 db/huntly 无端口映射、ib-gateway 只绑回环。见测试末尾的说明。
AUTH_ROOT_KEYS = ("SECRET_KEY", "JWT_SECRET_KEY", "INTERNAL_CALL_SECRET", "EXTERNAL_API_SECRET")


def _filters_for(key: str) -> frozenset[str]:
    if key == "EXTERNAL_API_SECRET":
        from backend.services.api.routers.external import auth as ext_auth

        return ext_auth._PUBLIC_DEFAULTS
    return {
        "INTERNAL_CALL_SECRET": auth_mod._PUBLIC_INTERNAL_DEFAULTS,
        "SECRET_KEY": auth_mod._PUBLIC_JWT_DEFAULTS,
        "JWT_SECRET_KEY": auth_mod._PUBLIC_JWT_DEFAULTS,
    }[key]


# ---------------------------------------------------------------------------
# 1) 出厂配置 → 过滤器的覆盖（防「加了字面量忘了加过滤」）
# ---------------------------------------------------------------------------


def _compose_defaults() -> dict[str, str]:
    """从 docker-compose.yml 取 `${KEY:-default}` 形式的默认值。"""
    src = (_shipped_files()[1] / "docker-compose.yml").read_text(encoding="utf-8")
    found = {}
    for key, default in re.findall(r"\$\{([A-Z_]+):-([^}]*)\}", src):
        found.setdefault(key, default)
    return found


def _env_example_values() -> dict[str, str]:
    src = (_shipped_files()[1] / ".env.example").read_text(encoding="utf-8")
    found = {}
    for line in src.splitlines():
        m = re.match(r"^([A-Z_]+)=(.*)$", line.strip())
        if m:
            found[m.group(1)] = m.group(2).strip()
    return found


def test_shipped_config_files_are_actually_parsed() -> None:
    """防空转：扫描目标必须真的存在且能解析出内容。

    没有这条，一旦文件改名/迁移，下面几条会因为「一个字面量都没扫到」而全绿——
    那正是「验收假通过」的形状。
    """
    root = _shipped_files()[1]
    assert (root / ".env.example").is_file(), f"出厂配置不在了：{root / '.env.example'}"
    assert (root / "docker-compose.yml").is_file(), f"出厂配置不在了：{root / 'docker-compose.yml'}"

    compose = _compose_defaults()
    env_ex = _env_example_values()
    assert compose, "docker-compose.yml 里一个 ${VAR:-default} 都没解析出来，正则已失效"
    assert env_ex, ".env.example 里一个赋值都没解析出来，正则已失效"

    # 这四把根密钥在本仓确实被 compose 传递过——否则本文件测的是一组不存在的键
    missing = [k for k in AUTH_ROOT_KEYS if k not in compose and k not in env_ex]
    assert not missing, f"这些根密钥既不在 compose 也不在 .env.example，扫描面已失效：{missing}"


@pytest.mark.parametrize("key", AUTH_ROOT_KEYS)
def test_shipped_defaults_are_empty_or_filtered(key: str) -> None:
    """出厂文件里给出的默认值：**要么留空，要么在过滤器里**。

    非空且未过滤 = 新装部署直接拿到一个公开密钥 = 本文件开头那两次事故的形状。
    """
    filters = _filters_for(key)
    offenders: list[str] = []

    compose_val = _compose_defaults().get(key)
    if compose_val:
        if compose_val not in filters:
            offenders.append(f"docker-compose.yml: ${{{key}:-{compose_val}}}")

    env_val = _env_example_values().get(key)
    if env_val and env_val not in filters:
        offenders.append(f".env.example: {key}={env_val}")

    assert not offenders, (
        "出厂配置里出现了**未过滤**的密钥字面量——新装部署会把它当成有效密钥：\n  "
        + "\n  ".join(offenders)
        + f"\n请把字面量加进 {key} 对应的过滤器，或（更好）把出厂默认值留空。"
    )


def test_the_two_historical_literals_are_filtered() -> None:
    """事故实物：这两枚字面量必须永远留在过滤器里。

    它们是真实发生过的默认值，全网可读。就算出厂文件已清干净，**已经拷过 .env
    的存量部署**手里还攥着它们——过滤是那些部署的唯一防线。
    """
    assert "quantmind-internal-secret" in auth_mod._PUBLIC_INTERNAL_DEFAULTS, (
        ".env.example 曾印过这枚字面量且未过滤（2026-09-23 发现）"
    )
    assert "changeme-generate-a-random-secret" in auth_mod._PUBLIC_JWT_DEFAULTS, (
        "compose 曾用它做 SECRET_KEY 回退（2026-09-23 发现）"
    )
    for literal in ("dev-secret-key", "CHANGE_ME_GENERATE_YOUR_OWN_SECRET_KEY"):
        assert literal in auth_mod._PUBLIC_JWT_DEFAULTS, f"{literal} 是从出厂配置里读得到的字面量"


def test_docs_do_not_print_the_internal_secret() -> None:
    """随包出厂的文档里不得印内部密钥明文。

    `config/qwenpaw/AGENTS.md` 与 `docker/dsh/AGENTS.md` 曾被便携包原样打包出厂，
    等于把内部密钥随发行物一起发出去。
    """
    offenders = []
    for rel in ("config/qwenpaw/AGENTS.md", "docker/dsh/AGENTS.md"):
        p = _shipped_files()[1] / rel
        if not p.is_file():
            continue
        if "quantmind-internal-secret" in p.read_text(encoding="utf-8"):
            offenders.append(rel)
    assert not offenders, f"这些随包文档里印着内部密钥明文：{offenders}"


# ---------------------------------------------------------------------------
# 2) 公开字面量签不出可用令牌（回归：修复前这条会「令牌被接受」）
# ---------------------------------------------------------------------------


def _sign(secret: str, **claims: object) -> str:
    payload = {"sub": "1", "username": "admin", "roles": ["admin"],
               "exp": datetime.utcnow() + timedelta(hours=1)}
    payload.update(claims)
    return pyjwt.encode(payload, secret, algorithm="HS256")


@pytest.mark.parametrize(
    "public_literal",
    ["changeme-generate-a-random-secret", "dev-secret-key",
     "CHANGE_ME_GENERATE_YOUR_OWN_SECRET_KEY", "dev-secret"],
)
def test_public_literal_counts_as_unconfigured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, public_literal: str
) -> None:
    """公开字面量一律被判为「未配置」——它不能成为有效密钥。"""
    monkeypatch.setattr("backend.shared.runtime_secrets.runtime_env_path",
                        lambda: tmp_path / "absent.env")
    for k in ("SECRET_KEY", "JWT_SECRET_KEY", "JWT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SECRET_KEY", public_literal)
    assert get_jwt_secret() == "", "公开字面量没有被过滤掉"


@pytest.mark.parametrize(
    "public_literal",
    ["changeme-generate-a-random-secret", "dev-secret-key",
     "CHANGE_ME_GENERATE_YOUR_OWN_SECRET_KEY", "dev-secret"],
)
def test_public_literal_cannot_forge_an_admin_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, public_literal: str
) -> None:
    """**核心回归**：服务正常运行时（已配真实密钥），攻击者拿公开字面量自签的
    admin 令牌必须被判为「签名无效」。

    修复前实测：`verify_token` 接受它并返回 `roles=["admin"]`——因为服务当时
    用的签名密钥**就是**那枚公开字面量。

    这里刻意把服务配成正确状态（而不是留空），才能测到「签名不符 → 401」这条
    真实攻击路径；「服务自己没配」是另一种处境（503），由下面的用例覆盖。
    """
    monkeypatch.setattr("backend.shared.runtime_secrets.runtime_env_path",
                        lambda: tmp_path / "absent.env")
    for k in ("SECRET_KEY", "JWT_SECRET_KEY", "JWT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SECRET_KEY", "server-side-real-key-" + "z" * 40)
    mgr = AuthManager()

    with pytest.raises(HTTPException) as exc:
        mgr.verify_token(_sign(public_literal))
    assert exc.value.status_code == 401, "公开字面量伪造的令牌没被判为签名无效"

    # 反面：服务自己签的令牌仍然有效——证明上一条不是「什么都拒」。
    assert mgr.verify_token(mgr.create_access_token({"sub": "9"}))["sub"] == "9"


# ---------------------------------------------------------------------------
# 3) 未配置 = 拒绝服务（不是回落、不是拿空串验签）
# ---------------------------------------------------------------------------


@pytest.fixture()
def _unconfigured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("backend.shared.runtime_secrets.runtime_env_path",
                        lambda: tmp_path / "absent.env")
    for k in ("SECRET_KEY", "JWT_SECRET_KEY", "JWT_SECRET"):
        monkeypatch.delenv(k, raising=False)


@pytest.mark.usefixtures("_unconfigured")
def test_unconfigured_refuses_to_sign() -> None:
    """未配置时**不得签发**令牌（而不是拿某个兜底字面量签）。"""
    with pytest.raises(HTTPException) as exc:
        AuthManager().create_access_token({"sub": "1"})
    assert exc.value.status_code == 503
    assert exc.value.detail == "jwt_secret_not_configured"


@pytest.mark.usefixtures("_unconfigured")
def test_unconfigured_refuses_to_verify() -> None:
    """未配置时**不得验签**——尤其不能把空串喂给 jwt.decode（那等于承认空密钥令牌）。"""
    with pytest.raises(HTTPException) as exc:
        AuthManager().verify_token(_sign("anything"))
    assert exc.value.status_code == 503


def test_configured_secret_round_trips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """正常配置时功能不受影响（防「修安全把功能修没了」）。"""
    monkeypatch.setattr("backend.shared.runtime_secrets.runtime_env_path",
                        lambda: tmp_path / "absent.env")
    monkeypatch.setenv("SECRET_KEY", "a-real-random-key-" + "x" * 48)
    mgr = AuthManager()
    token = mgr.create_access_token({"sub": "42"})
    assert mgr.verify_token(token)["sub"] == "42"


def test_auth_manager_reads_the_secret_live_not_at_construction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """密钥是**每次实时解析**的，不是构造期快照。

    启动期自动生成的密钥写入时 `AuthManager` 单例已经存在（`auth.py` 模块级创建），
    构造期快照会让那枚新密钥到重启前都不生效。
    """
    monkeypatch.setattr("backend.shared.runtime_secrets.runtime_env_path",
                        lambda: tmp_path / "absent.env")
    monkeypatch.setenv("SECRET_KEY", "first-key-" + "a" * 40)
    mgr = AuthManager()
    assert mgr.secret_key.startswith("first-key-")

    monkeypatch.setenv("SECRET_KEY", "second-key-" + "b" * 40)
    assert mgr.secret_key.startswith("second-key-"), "构造期快照了密钥，轮换不生效"


def test_runtime_env_wins_over_public_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """runtime.env 优先于环境变量——否则 compose 注入的公开回退值会遮蔽落盘密钥，
    表现为**每次重启轮换一次签名密钥**（内部密钥在 2026-09-17 踩过同一个坑）。
    """
    runtime = tmp_path / "runtime.env"
    runtime.write_text("SECRET_KEY=from-runtime-env-file\n", encoding="utf-8")
    monkeypatch.setattr("backend.shared.runtime_secrets.runtime_env_path", lambda: runtime)
    monkeypatch.setenv("SECRET_KEY", "changeme-generate-a-random-secret")
    assert get_jwt_secret() == "from-runtime-env-file"


def test_second_validator_shares_the_same_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`decode_jwt_token`（python-jose）必须与 `AuthManager` 同源。

    它此前自带一份 `or "dev-secret"` 兜底——修一处漏一处的形状。
    """
    monkeypatch.setattr("backend.shared.runtime_secrets.runtime_env_path",
                        lambda: tmp_path / "absent.env")
    monkeypatch.setenv("SECRET_KEY", "shared-source-key-" + "c" * 40)
    token = AuthManager().create_access_token({"sub": "7"})
    assert auth_mod.decode_jwt_token(token)["sub"] == "7"


@pytest.mark.usefixtures("_unconfigured")
def test_second_validator_also_refuses_when_unconfigured() -> None:
    with pytest.raises(HTTPException) as exc:
        auth_mod.decode_jwt_token(_sign("x"))
    assert exc.value.status_code == 503


# ---------------------------------------------------------------------------
# 说明：本文件**不**覆盖的默认口令
# ---------------------------------------------------------------------------
# compose 里另有 ${DB_PASSWORD:-quantmind2026} / ${HUNTLY_PASSWORD:-changeme} /
# ${IB_VNC_PASSWORD:-quantmind-vnc} 三个公开默认口令。它们不在 AUTH_ROOT_KEYS 里：
#   * 不是鉴权根——泄露它们拿到的是「一个服务的口令」，不是「冒充任意用户」；
#   * 默认部署里 db / huntly 无 ports 映射（仅 Docker 内网可达），ib-gateway 绑 127.0.0.1；
#   * 改 DB_PASSWORD 会让既有部署的 postgres 数据卷对不上口令，是运维决策不是代码修复。
# 若将来给它们暴露端口，应先回来把这三条纳入本文件。
