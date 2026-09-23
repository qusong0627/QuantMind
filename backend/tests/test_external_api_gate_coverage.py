"""对外命名空间（`/api/ext/v1`）必须被实盘闸门覆盖，且**默认拒绝**。

为什么单独一条闸门测试
----------------------
`live_trading_gate.py` 的两张表（`_BLOCKED_PREFIXES` / `_BLOCKED_EXACT`）里每一个
字面量都以 `/api/v1` 开头，`_matches` 的边界又落在 `/` 上。所以
`/api/ext/v1/orders` 这种路径：

* 不等于 `/api/v1/...`，也对不上任何前缀；
* 于是 `is_blocked()` 返回 `False`，**闸门放行**。

也就是说，闸门目前只守住了 `/api/v1` 这一个命名空间，而「新路由默认落在拒绝侧」
这个说法在**新命名空间**上是不成立的——对外命名空间是后来者，它落在拒绝表的
覆盖范围之外，默认可达。这与闸门模块 docstring 里承诺的失败方向相反。

本文件钉住修好之后的形状：

1. 对外命名空间**未登记即拒绝**（fail-closed），登记走显式放行/拒绝两张表；
2. 该策略对**运行时新加的路由**同样生效——不是只对当前这几个端点特判；
3. 放行表是**承重**的：把它清空，已登记端点必须变成拒绝
   （否则放行表与实现脱节，测试会变成空转）；
4. 闸门路由发现**不是空集**——零个端点参与时本文件必须失败，而不是全绿。
"""

from __future__ import annotations

import warnings

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.shared import live_trading_gate as gate

# ---------------------------------------------------------------------------
# 命名空间的唯一出处
# ---------------------------------------------------------------------------


def test_ext_namespace_constant_exists_and_is_distinct() -> None:
    """对外命名空间必须与 `/api/v1` 分开且不以它为前缀。

    若哪天有人把它改成 `/api/v1/ext`，闸门现有的拒绝表就会自动覆盖到它——
    那也行，但必须是有意的：这条会红，逼人改这里的断言并重新想一遍。
    """
    assert gate.EXT_API == "/api/ext/v1"
    assert not gate._matches(gate.EXT_API, gate._API), (
        "对外命名空间落在了 /api/v1 之下，闸门原有的拒绝表会连带生效，"
        "本文件的默认拒绝策略就不再是唯一防线了——请重新评估。"
    )


# ---------------------------------------------------------------------------
# 默认拒绝
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/ext/v1/orders",
        "/api/ext/v1/orders/123",
        "/api/ext/v1/trade/anything",
        "/api/ext/v1/something-nobody-registered-yet",
    ],
)
def test_unregistered_ext_paths_are_denied(path: str) -> None:
    """对外命名空间里**没登记过**的路径一律拒绝——新端点不会默认可达。"""
    assert gate.is_blocked("GET", path) is True
    assert gate.is_blocked("POST", path) is True


#: 对外路由的**形状**清单（带 `{参数}` 占位符）。加端点必须改这里——
#: 它同时驱动下面两条断言：router 实际枚举 与 网关 OpenAPI。
EXPECTED_EXT_ROUTES = {
    "/api/ext/v1/auth/session",
    "/api/ext/v1/capabilities",
    # 批次 3：数据面
    "/api/ext/v1/data/datasets",
    "/api/ext/v1/data/datasets/{name}/partitions",
    "/api/ext/v1/data/datasets/{name}/partitions/{partition}/file",
    "/api/ext/v1/data/datasets/{name}/blob",
    "/api/ext/v1/data/{dataset}/changes",
    # 批次 4：控制面（只读）
    "/api/ext/v1/control/strategies",
    "/api/ext/v1/control/models",
    # 批次 4：任务面
    "/api/ext/v1/task/kinds",
    "/api/ext/v1/task/{kind}",
    "/api/ext/v1/task/{kind}/{ref}",
    # 批次 4：交易面（**只有模拟盘**，实盘见 live_trading_gate 的说明）
    "/api/ext/v1/trading/sim/account",
    "/api/ext/v1/trading/sim/orders",
    "/api/ext/v1/trading/sim/orders/{order_id}",
    "/api/ext/v1/trading/sim/orders/{order_id}/cancel",
    "/api/ext/v1/trading/sim/trades",
}

#: 把形状里的参数位换成**真实取值**，用来跑闸门判定。
#: 取值一律取自注册表里确实存在的名字——用一个不存在的名字会让
#: 「闸门放行」和「注册表查不到」混在一起，测出来的就不是闸门了。
_SAMPLE_PARAMS = {
    "{name}": "daily_forward",
    "{dataset}": "news_enrichment",
    "{partition}": "2026-09-22",
    # 任务种类取自 `task.py` 的注册表（用真名字，不用 `{kind}` 占位）。
    "{kind}": "training",
    # 作业 id：上游几种形状之一（uuid4().hex[:16]）。闸门的字符类要吃得下
    # 全部几种，所以这里特意用最长的那个形状。
    "{ref}": "3f9a1c0d5e7b2468",
    # UUID 的标准 36 字符小写形（`sim_orders.order_id`）。
    "{order_id}": "0b6d2f4a-7c31-4e58-9a02-1f8b3c5d7e90",
}


def _concretize(path: str) -> str:
    for placeholder, value in _SAMPLE_PARAMS.items():
        path = path.replace(placeholder, value)
    return path


@pytest.mark.parametrize("path", sorted(EXPECTED_EXT_ROUTES))
def test_registered_ext_paths_are_allowed(path: str) -> None:
    """已登记的非交易端点放行（否则对外接入连握手都做不了）。"""
    concrete = _concretize(path)
    assert gate.is_blocked("POST", concrete) is False
    assert gate.is_blocked("GET", concrete) is False


def test_every_real_ext_route_is_covered_by_the_policy() -> None:
    """防漂移：从 router **实际**枚举路由，逐条断言已登记。

    上面那些 `is False` 用的是手写字面量路径。一旦有人给 router 换前缀或
    改端点名，那些字面量就变成在测一个不存在的路径，而新路径默默落进
    「未登记即拒绝」——功能没坏，但**外部节点会莫名其妙收 403**，
    而测试全绿。这条把两边钉在一起。

    参数位换成真实取值再判：`_ALLOWED_EXT_PATTERNS` 只认具体形状，
    拿 `{name}` 去判会得到「拒绝」——那是**测试的输入不合法**，
    不是闸门拦错了。

    ⚠️ 枚举走**一次性的 FastAPI 装配 + openapi()**，不是 `ext_router.routes`：
    本仓的 FastAPI 把 `include_router` 进来的子路由包成一个 `_IncludedRouter`
    节点（没有 `.path`），`r.path` 会直接 AttributeError——数据面一挂进来
    这条测试就炸了。`app.openapi()` 是这台框架上**唯一**能拿到展开后路径的入口。
    这里单独装一个小 app（而不是用真网关），是为了把「router 模块自己的路由集」
    与「main.py 挂在哪」分成两条断言：这条管前者，下面那条管后者。
    """
    from backend.services.api.routers.external.router import router as ext_router

    probe = FastAPI()
    probe.include_router(ext_router, prefix=gate.EXT_API)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        real = {p for p in probe.openapi().get("paths", {}) if p.startswith(gate.EXT_API)}
    assert real, "对外 router 一条路由都没有——下面的断言会空转"
    assert real == EXPECTED_EXT_ROUTES, (
        f"对外路由集合变了：{sorted(real)}。请同步更新本文件、"
        "`test_external_api_contract.py` 与闸门登记表。"
    )

    uncovered = [p for p in real if gate.is_blocked("GET", _concretize(p))]
    assert not uncovered, (
        f"这些对外路由没在闸门里登记，实盘关闭的部署上会 403：{uncovered}。"
        "请在 live_trading_gate._ALLOWED_EXT_ENDPOINTS / "
        "_ALLOWED_EXT_PATTERNS / _BLOCKED_EXT_PREFIXES 登记。"
    )


def test_real_app_serves_ext_at_the_expected_paths() -> None:
    """router 声明的前缀必须与 `main.py` 实际挂载的结果一致。

    上面几条测的是 router 对象（`EXT_API + r.path`）。若有人把 `main.py` 里的
    `prefix=EXT_API` 改成别的字面量，router 级断言全绿而**线上路径已经变了**——
    外部节点会全部 404，而闸门那边还在拦一个不存在的路径。

    ⚠️ 用 OpenAPI schema 而不是 `app.routes`：本仓库的 FastAPI 会把被 include 的
    router 包成 `_IncludedRouter` 节点而非摊平，`app.routes` 里根本看不到
    `/api/ext/...`（第一版探针就是这么被误导的）。

    ⚠️ **这里不设 skip**。第一版用 `try/except → pytest.skip` 兜装配失败，
    结果真出过一次：`main.py` 从一个已删掉的转出里 import `EXT_API`，网关
    **整个启动不了**，而这条测试只报了一句 skip、其余全绿。装配失败就是故障本身，
    不该被降级成一个安静的跳过。
    """
    from backend.services.api.main import app

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # 网关里已有若干 duplicate-operation-id 噪声
        paths = set(app.openapi().get("paths", {}))

    served = {p for p in paths if p.startswith(gate.EXT_API)}
    assert served == EXPECTED_EXT_ROUTES, (
        f"网关实际伺服的对外路径与预期不符：{sorted(served)}。"
        "如果确实改了，请同步更新 router 测试与闸门登记表。"
    )


def test_router_does_not_hardcode_the_namespace() -> None:
    """对外 router 里不得出现命名空间字面量——前缀只能有一个出处。

    第一版这条写的是 `assert "EXT_API" in src`，**那是个空转断言**：
    import 行里出现一次就绿了，跟 router 有没有真的用它对不上。改成
    查「不许有字面量」——这个性质才是有意义的那个。
    """
    import inspect
    import re

    from backend.services.api.routers.external import router as router_module

    src = inspect.getsource(router_module)
    hits = [
        f"L{i}: {line.strip()}"
        for i, line in enumerate(src.splitlines(), 1)
        if re.search(r"""["']/api/ext""", line)
    ]
    assert not hits, (
        "对外 router 里硬写了命名空间字面量，前缀出现了第二个出处：\n" + "\n".join(hits)
    )


def test_ext_denies_are_not_confused_with_v1() -> None:
    """`/api/ext/v1/...` 的判定不得被 `/api/v1` 的规则污染，反之亦然。"""
    # ext 命名空间不该影响 v1 的既有结论
    assert gate.is_blocked("POST", "/api/v1/orders") is True
    assert gate.is_blocked("GET", "/api/v1/tdx/quote-feed") is False
    # v1 的行情白名单不该漏到 ext 上（`/api/ext/v1/market` 没登记 = 拒绝）
    assert gate.is_blocked("GET", "/api/ext/v1/market") is True


#: 对外模拟盘的全部路径（形状已具体化）。
_SIM_PATHS = (
    "/api/ext/v1/trading/sim/account",
    "/api/ext/v1/trading/sim/orders",
    "/api/ext/v1/trading/sim/orders/0b6d2f4a-7c31-4e58-9a02-1f8b3c5d7e90",
    "/api/ext/v1/trading/sim/orders/0b6d2f4a-7c31-4e58-9a02-1f8b3c5d7e90/cancel",
    "/api/ext/v1/trading/sim/trades",
)


def test_blocked_prefix_never_covers_simulation() -> None:
    """**实盘拒绝表不许覆盖 `trading/sim/`。**

    这条守的是一个很容易犯、后果很具体的错。想「在实盘关闭的部署上关掉对外
    交易面」时，最自然的一行是往 `_BLOCKED_EXT_PREFIXES` 里写：

        "/api/ext/v1/trading"          # ← 看起来对，其实错

    那是个**前缀**匹配（边界落在 `/`），于是 `/api/ext/v1/trading/sim/orders`
    一起被拒。而 OSS 默认 `ENABLE_REAL_TRADING=false`，也就是**每一个** OSS
    部署上模拟交易都会 403——恰恰是实盘关闭时唯一该能用的那条路。

    今天这张表是空的，所以断言本身平凡成立。**平凡成立的断言不算数**，
    所以下面还有一段：把那个诱人的错值塞进去，先证明它**真的会**打死模拟盘，
    再证明本测试的检查逻辑确实能发现它。少了那一段，这个文件就只是在
    复述「表是空的」，而表明天就可能不空。
    """
    blocked = [p for p in _SIM_PATHS if gate.is_blocked("GET", p) is True]
    assert not blocked, (
        f"这些模拟盘路径被 `_BLOCKED_EXT_PREFIXES` 覆盖了：{blocked}。"
        "登记实盘侧端点时不要写父路径 `/api/ext/v1/trading`——它会连带打死"
        "模拟盘，见 live_trading_gate 里那段说明。"
    )


def test_the_simulation_guard_would_actually_catch_a_bad_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证明上一条**有牙**：塞进那个错值，它必须能被打死、也能被发现。

    两件事都要验：

    1. 那个前缀**确实**会拦住模拟盘（否则上一条的检查是在防一个不存在的风险，
       将来有人真写了它，大家会以为已经防住了）；
    2. 上一条用的检查逻辑（`is_blocked` 跑具体路径）会把它揪出来——
       不是「拿前缀做字符串比较」那种同义复述。
    """
    monkeypatch.setattr(gate, "_BLOCKED_EXT_PREFIXES", ("/api/ext/v1/trading",))

    hit = [p for p in _SIM_PATHS if gate.is_blocked("GET", p) is True]
    assert hit == list(_SIM_PATHS), (
        "把 `/api/ext/v1/trading` 放进拒绝表居然没有拦住模拟盘——"
        "那说明本文件的模型（前缀语义会连带覆盖子树）已经不对了，"
        "上一条测试的前提需要重新推导。实际被拦住的："
        f"{hit}"
    )

    # 而按端点登记（模拟盘之外的另一段）不该误伤：
    monkeypatch.setattr(gate, "_BLOCKED_EXT_PREFIXES", ("/api/ext/v1/trading/real",))
    assert [p for p in _SIM_PATHS if gate.is_blocked("GET", p) is True] == []


# ---------------------------------------------------------------------------
# 承重性：放行表真的在起作用（防「策略写了但没接线」）
# ---------------------------------------------------------------------------


def test_allowlist_entries_are_endpoint_scoped_not_namespace_scoped() -> None:
    """放行表必须登记到**端点**，不能登记到命名空间。

    `_matches` 是前缀匹配，所以登记 `f"{EXT_API}/auth"` 会把整段
    `/api/ext/v1/auth/*` 一起放行——将来加改密/换机/吊销端点时，它们**不会**
    触发「未登记即拒绝」，而这正是本文件与闸门 docstring 承诺的摩擦。
    写多深，放行就止于多深。
    """
    siblings = (
        "/api/ext/v1/auth/rotate",
        "/api/ext/v1/auth/revoke",
        "/api/ext/v1/auth/session/extra",
        "/api/ext/v1/authz",
    )
    leaked = [p for p in siblings if gate.is_blocked("POST", p) is False]
    assert not leaked, (
        f"这些路径落在放行表的命名空间前缀下、被自动放行了：{leaked}。"
        "放行表登记得太宽——请登记到端点（如 /auth/session）。"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/api/ext/v1/auth/session",
        "/api/ext/v1/auth/session/",  # 尾斜杠必须同判，否则外部节点莫名 403
        "/api/ext/v1/capabilities",
        "/api/ext/v1/capabilities/",
    ],
)
def test_exact_match_survives_trailing_slash(path: str) -> None:
    """放行改精确相等之后，尾斜杠仍须同判。

    `_normalize` 去尾斜杠，所以这两种写法落在同一个字符串上。若哪天有人把
    `_normalize` 去掉，这条会红——而那意味着「客户端多打一个 `/` 就 403」，
    对外部节点是极难排查的故障（网关的 307 重定向在中间件**之后**，
    根本轮不到它）。
    """
    assert gate.is_blocked("GET", path) is False
    assert gate.is_blocked("POST", path) is False


def test_pattern_entries_are_shape_scoped_not_prefix_scoped() -> None:
    """带路径参数的端点走**模式表**，模式必须**逐段写死**。

    这里列的全是「差一点点」的形状：多一段、少一段、日期写法松一点、
    字符类之外的字符。它们必须**全部**被拒——如果哪天有人图省事把模式表
    换成 `f"{EXT_API}/data/"` 这样的整段前缀放行，这些断言会一起红。
    """
    near_miss = (
        # 多一段 / 少一段
        "/api/ext/v1/data/datasets/daily_forward/partitions/2026-09-22/file/extra",
        "/api/ext/v1/data/datasets/daily_forward/blob/extra",
        "/api/ext/v1/data/news_enrichment/changes/extra",
        "/api/ext/v1/data/datasets/daily_forward/partitions/2026-09-22",
        # 日期写法松一格就走不到注册表，这里也不该放行
        "/api/ext/v1/data/datasets/daily_forward/partitions/2026-9-22/file",
        "/api/ext/v1/data/datasets/daily_forward/partitions/20260922/file",
        # 参数位的字符类之外
        "/api/ext/v1/data/datasets/Daily_Forward/blob",
        "/api/ext/v1/data/datasets/daily-forward/blob",
        "/api/ext/v1/data/datasets/daily_forward/partitions/2026-09-22/files",
        # 参数位后面直接跟别的东西，以及整个数据面之外的路径
        "/api/ext/v1/data/datasets/x",
        "/api/ext/v1/data/orders",
        # --- 批次 4 的形状：控制面是精确登记，多一段就不认 ---
        "/api/ext/v1/control/strategies/123",
        "/api/ext/v1/control/models/ready",
        "/api/ext/v1/control/models/../strategies",
        "/api/ext/v1/control/accounts",  # 没这个端点（账户在交易面）
        # --- 任务面：kind 是窄字符类，ref 逐段锚死 ---
        "/api/ext/v1/task/TRAINING",  # 大写不在 [a-z_]+ 里
        "/api/ext/v1/task/training/3f9a/extra",  # 多一段
        "/api/ext/v1/task/training/../../control/models",  # 想拼路径
        # --- 交易面：**实盘侧一个都没开**，父路径也不许整段放行 ---
        "/api/ext/v1/trading/real/orders",
        "/api/ext/v1/trading/orders",
        "/api/ext/v1/trading/sim/positions",  # 没这个端点：分组是精确枚举
        "/api/ext/v1/trading/sim/orders/not-a-uuid",
        "/api/ext/v1/trading/sim/orders/"
        "0b6d2f4a-7c31-4e58-9a02-1f8b3c5d7e90/extra",
    )
    leaked = [p for p in near_miss if gate.is_blocked("GET", p) is False]
    assert not leaked, (
        f"这些形状没被模式表挡住：{leaked}。"
        "模式表可能退化成了前缀放行——见 _ALLOWED_EXT_PATTERNS 上方的注释。"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/api/ext/v1/data/datasets/daily_forward/blob/",  # 尾斜杠同判
        "/api/ext/v1/data/news_enrichment/changes/",
    ],
)
def test_pattern_match_survives_trailing_slash(path: str) -> None:
    """与精确表同理：`_normalize` 先去掉尾斜杠，两种写法落同一个字符串。"""
    assert gate.is_blocked("GET", path) is False


def test_allowlist_is_load_bearing(monkeypatch: pytest.MonkeyPatch) -> None:
    """清空放行表后，已登记端点必须变成拒绝。

    若放行表根本没被 `is_blocked` 读取，这条会绿——那说明上面那些
    `is False` 的断言测的是别的东西。
    """
    monkeypatch.setattr(gate, "_ALLOWED_EXT_ENDPOINTS", ())
    assert gate.is_blocked("POST", "/api/ext/v1/auth/session") is True
    assert gate.is_blocked("GET", "/api/ext/v1/capabilities") is True


def test_pattern_table_is_load_bearing(monkeypatch: pytest.MonkeyPatch) -> None:
    """与上一条同构：清空模式表，数据面必须整段变成拒绝。

    没有这条的话，「模式表被读到了」这件事全靠间接推断——而它与精确表
    是**两条**独立的判据，`is_blocked` 里少一次 `any(...)` 不会影响
    精确表那几条断言。
    """
    monkeypatch.setattr(gate, "_ALLOWED_EXT_PATTERNS", ())
    for shape in EXPECTED_EXT_ROUTES:
        if "{" not in shape:
            continue  # 无参数的走精确表，本就不该受模式表影响
        assert gate.is_blocked("GET", _concretize(shape)) is True, shape


def test_default_deny_is_not_an_accident(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认拒绝必须来自「未登记」，不是来自「刚好没匹配上任何拒绝项」。

    这条与上一条互为反面：证明拒绝侧的判定**读的是登记表**。
    """
    monkeypatch.setattr(gate, "_ALLOWED_EXT_ENDPOINTS", ("/api/ext/v1/whatever",))
    assert gate.is_blocked("GET", "/api/ext/v1/whatever") is False, (
        "登记为放行后仍被拒——默认拒绝没有读放行表，策略是硬编码的"
    )
    monkeypatch.setattr(gate, "_BLOCKED_EXT_PREFIXES", ("/api/ext/v1/trade",))
    assert gate.is_blocked("POST", "/api/ext/v1/trade/x") is True, (
        "登记进拒绝表仍被放行——拒绝表没被读取"
    )


# ---------------------------------------------------------------------------
# 行为面：真装中间件跑一遍，并用「运行时新加的路由」证明默认拒绝
# ---------------------------------------------------------------------------


@pytest.fixture()
def _disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(gate.ENV_KEY, "false")


def _app_with_gate() -> FastAPI:
    app = FastAPI()

    @app.post("/api/ext/v1/auth/session")
    async def _session() -> dict[str, str]:  # pragma: no cover - 桩
        return {"ok": "session"}

    @app.get("/api/ext/v1/capabilities")
    async def _caps() -> dict[str, str]:  # pragma: no cover - 桩
        return {"ok": "capabilities"}

    gate.install_live_trading_gate_middleware(app, "test")
    return app


@pytest.mark.usefixtures("_disabled")
def test_middleware_lets_registered_ext_routes_through() -> None:
    client = TestClient(_app_with_gate())
    assert client.post("/api/ext/v1/auth/session").status_code == 200
    assert client.get("/api/ext/v1/capabilities").status_code == 200


@pytest.mark.usefixtures("_disabled")
def test_middleware_blocks_a_route_added_at_runtime() -> None:
    """**核心断言**：闸门装好之后再挂一个对外交易路由，它必须立刻被 403。

    这条模拟的是真实风险——将来有人往对外命名空间加 `/orders`，
    只挂 router 不改闸门。默认拒绝策略下他什么都不用做就是安全的；
    策略没接的话，这个端点在实盘关闭的部署上直接可达。
    """
    app = _app_with_gate()

    @app.post("/api/ext/v1/orders")
    async def _orders() -> dict[str, str]:  # pragma: no cover - 桩
        return {"ok": "placed a real order"}

    client = TestClient(app)
    resp = client.post("/api/ext/v1/orders")
    assert resp.status_code == 403, (
        f"对外命名空间里后加的交易路由没被挡住（HTTP {resp.status_code}）——"
        "实盘关闭的部署上它是可达的。"
    )
    assert resp.json()["detail"] == gate.DISABLED_DETAIL


def test_enabled_deployment_does_not_block_ext(monkeypatch: pytest.MonkeyPatch) -> None:
    """实盘开启时闸门整体让路——对外命名空间也不例外（否则开关只开了一半）。"""
    monkeypatch.setenv(gate.ENV_KEY, "true")
    client = TestClient(_app_with_gate())
    assert client.post("/api/ext/v1/orders").status_code == 404  # 路由不存在，但没被 403
