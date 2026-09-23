"""转发头过滤（`backend/shared/trusted_headers.py`）的登记与行为测试。

**本文件要钉住的是「类」，不是「某一处」。**

C1 事故的收尾教训是「校验点清单要按类枚举」——同一条规则手抄到 8 个文件，
新增一个信任头就要记得改 8 处，忘一处就是一个洞。所以这里有两层断言：

1. ``sanitize_forward_headers`` 本身的行为（丢什么、留什么、不改入参）；
2. **每一个客户端面向的代理都真的接上了它** ——
   ``test_every_forwarding_module_uses_sanitizer`` 扫描 routers 目录，
   凡是出现 ``request.headers.items()``（= 把客户端头往外递的形状）的模块，
   必须引用 ``sanitize_forward_headers``。新增代理照抄老写法会在这里红。

第 2 条是**结构性**的：它不关心某个代理当前对不对，它关心「有没有人绕过去」。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from starlette.requests import Request

from backend.shared.trusted_headers import (
    DEFAULT_DROP_HEADERS,
    HOP_HEADERS,
    TRUST_HEADERS,
    sanitize_forward_headers,
)

ROUTERS_DIR = Path(__file__).resolve().parents[1] / "services" / "api" / "routers"

#: 必须被剥离的信任头字面量（小写）。这里是**手写的期望值**，故意不引用
#: `TRUST_HEADERS`——否则「把集合改空」这种改动会让测试跟着一起变绿。
EXPECTED_TRUST_HEADERS = {"x-internal-call", "x-user-id", "x-tenant-id"}


def _make_request(headers: dict[str, str]) -> Request:
    """构造一个只带 headers 的最小 ASGI Request。"""
    raw = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "headers": raw,
        }
    )


# ---------------------------------------------------------------------------
# 第 1 层：sanitize_forward_headers 的行为
# ---------------------------------------------------------------------------


def test_trust_header_constants_pin_exact_membership() -> None:
    """信任头集合的成员是手写钉死的——收紧容易，放宽必须显式改这里。"""
    assert TRUST_HEADERS == EXPECTED_TRUST_HEADERS


def test_hardened_subset_of_default_drop() -> None:
    """默认丢弃集合必须同时含逐跳头与信任头。"""
    assert TRUST_HEADERS <= DEFAULT_DROP_HEADERS
    assert HOP_HEADERS <= DEFAULT_DROP_HEADERS


@pytest.mark.parametrize("header", sorted(EXPECTED_TRUST_HEADERS))
def test_trust_headers_are_dropped(header: str) -> None:
    """信任头一律剥离（大小写不敏感）。"""
    poisoned = {header: "attack", header.upper(): "attack", "accept": "application/json"}
    out = sanitize_forward_headers(poisoned.items())
    assert "accept" in out
    assert not any(k.lower() in EXPECTED_TRUST_HEADERS for k in out)


@pytest.mark.parametrize("header", sorted(HOP_HEADERS))
def test_hop_headers_are_dropped(header: str) -> None:
    out = sanitize_forward_headers({header: "x", "accept": "*/*"}.items())
    assert header not in out
    assert out["accept"] == "*/*"


def test_business_headers_survive() -> None:
    """普通业务头不能被误杀——剥多了同样出事（C1 有「拦多了」那一面）。"""
    kept = {"accept": "application/json", "content-type": "application/json", "x-request-id": "r-1"}
    assert sanitize_forward_headers(kept.items()) == kept


def test_extra_drop_is_honored_case_insensitively() -> None:
    out = sanitize_forward_headers(
        {"Authorization": "Bearer user-jwt", "accept": "*/*"}.items(),
        extra_drop={"authorization"},
    )
    assert "Authorization" not in out
    assert out["accept"] == "*/*"


def test_input_is_not_mutated() -> None:
    """本仓「不可变」约定：返回新 dict，不改入参。"""
    src = {"accept": "*/*", "x-user-id": "1"}
    snapshot = dict(src)
    sanitize_forward_headers(src)
    assert src == snapshot


def test_accepts_mapping_as_well_as_items() -> None:
    assert sanitize_forward_headers({"x-user-id": "1", "accept": "*/*"}) == {"accept": "*/*"}


# ---------------------------------------------------------------------------
# 第 2 层：每个客户端面向的代理都必须接上
# ---------------------------------------------------------------------------


def _forwarding_modules() -> list[Path]:
    """routers 目录（**含子目录**）下所有「把客户端头往外递」形状的模块。

    判据：源码里出现 ``request.headers.items()``。这是当前所有代理的手写形态，
    也是最容易被复制粘贴扩散的形态。

    ⚠️ 用 `rglob` 而不是 `glob`。第一版是 `glob("*.py")`——只扫顶层，
    `routers/admin/` 整个子树对扫描器不可见，而那里恰恰藏着唯一一个漏改的
    转发器（`admin/trading_agents.py`）。「按类枚举」的断言不能自己先漏掉一类。
    """
    hits = []
    for path in sorted(ROUTERS_DIR.rglob("*.py")):
        if path.name.startswith("__"):
            continue
        if "request.headers.items()" in path.read_text(encoding="utf-8"):
            hits.append(path)
    return hits


def test_forwarding_module_discovery_is_not_empty() -> None:
    """防空转：扫描不到任何模块说明判据失效了，那时下面的断言毫无意义。"""
    assert _forwarding_modules(), (
        f"在 {ROUTERS_DIR} 没扫到任何 `request.headers.items()` 模块——"
        "判据或目录变了，本文件的第 2 层断言已失去意义"
    )


def test_every_forwarding_module_uses_sanitizer() -> None:
    """**本文件的核心断言**：凡把客户端头往外递的模块，必须走统一剥离函数。

    漏掉一处的后果是 C1 同款：客户端自带 ``X-Internal-Call`` + ``X-User-Id``
    被透传到下游，下游采信后冒充任意用户。

    ⚠️ 判据是**调用**（带左括号），不是「文件里出现过这个名字」。
    第一版查的是后者，于是**导入了但不用**也能通过——变异测试（把
    `admin/trading_agents.py` 换回手抄清单、只留 import）实测仍是 23 passed，
    即那条断言是空转的。带括号才分得出 `sanitize_forward_headers(...)`
    与 `from ... import sanitize_forward_headers`。
    """
    missing = [
        str(p.relative_to(ROUTERS_DIR))
        for p in _forwarding_modules()
        if "sanitize_forward_headers(" not in p.read_text(encoding="utf-8")
    ]
    assert not missing, (
        "以下代理在转发客户端头但没有接上 shared.trusted_headers.sanitize_forward_headers："
        f"{missing}。请在构造上游头时改为 `sanitize_forward_headers(request.headers.items())`。"
    )


def test_proxies_strip_trust_headers_behaviourally() -> None:
    """逐代理真跑一遍：带着信任头的请求，过滤后一个都不剩。

    与上一条的区别：上一条查「有没有引用」，这条查「引用得对不对」。
    """
    from backend.services.api.routers import data_gateway_proxy, hub_proxy, qwenpaw_proxy

    # `agent_arena_proxy` 是**本机独有**的 gitignore 产物（.gitignore:290-291），
    # 不在 git 里。硬 import 会让这条测试在 CI / 其他开发者机器 / 客户交付版上
    # **必红**——而它正是本批「按类枚举」的核心断言之一。
    # 缺省时收敛掉这一条，但下面 `test_forwarding_module_discovery_is_not_empty`
    # 仍会保证扫描面非空，不会因为这里缩水而整体空转。
    agent_arena_proxy = pytest.importorskip(
        "backend.services.api.routers.agent_arena_proxy",
        reason="本机独有的 arena 代理，净检出中不存在",
    )

    request = _make_request(
        {
            "X-Internal-Call": "leaked-secret",
            "X-User-Id": "1",
            "X-Tenant-Id": "default",
            "Accept": "application/json",
        }
    )

    filters = {
        "agent_arena": lambda: agent_arena_proxy._upstream_headers(request),
        "data_gateway": lambda: data_gateway_proxy._forward_headers(request),
        "hub": lambda: hub_proxy._forward_headers(request),
        "qwenpaw": lambda: qwenpaw_proxy._sanitize_headers(request.headers.items()),
    }

    for name, build in filters.items():
        out = build()
        leaked = {k for k in out if k.lower() in EXPECTED_TRUST_HEADERS}
        assert not leaked, f"{name} 代理把信任头透传给了上游：{leaked}"
        assert "Accept" in out or "accept" in out, f"{name} 代理把普通业务头也丢了"


#: 允许保留信任头字面量的两种形状 —— 它们是**写**（服务→服务重建身份），
#: 不是**过滤清单**。都不构成本文件要防的「私有名单」问题。
#:
#:   1. 下标赋值：``out["X-User-Id"] = str(...)``  ← 内部调用重建身份，必须留
#:   2. 字面量作字典键：``"X-User-Id": str(...)``    ← 同上，另一种写法
_ALLOWED_TRUST_LITERAL_SHAPES = (
    re.compile(r"""(?i)\[\s*["']x-(?:internal-call|user-id|tenant-id)["']\s*\]"""),
    re.compile(r"""(?i)["']x-(?:internal-call|user-id|tenant-id)["']\s*:"""),
)


def test_proxy_modules_have_no_private_trust_header_lists() -> None:
    """不允许再出现「私有的一份信任头清单」——这正是 C1 之后复发的那条路。

    私有集合可以有（额外的 drop 项），但里面**不能**再枚举信任头：过滤用的
    信任头只能从 `shared.trusted_headers` 来。

    本断言是**启发式**的（扫字面量），所以显式放过两种「写」的形状——
    宁可漏判也不要制造常态假红，否则它会很快被人加 `xfail` 绕过。
    """
    offenders = []
    for path in _forwarding_modules():
        src = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(src.splitlines(), start=1):
            match = re.search(r"""(?i)["']x-(?:internal-call|user-id|tenant-id)["']""", line)
            if not match:
                continue
            if any(shape.search(line) for shape in _ALLOWED_TRUST_LITERAL_SHAPES):
                continue
            offenders.append(f"{path.name}:{line_no}: {line.strip()}")
    assert not offenders, (
        "以下位置仍手写信任头字面量作过滤用，应改为从 shared.trusted_headers 派生：\n"
        + "\n".join(offenders)
    )
