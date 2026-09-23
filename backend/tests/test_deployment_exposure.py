"""部署暴露面回归：**宿主机上只有网关端口对外**。

为什么值得一条测试
------------------
C1 事故（2026-09-17）复盘里有一条常被忽略的事实：当时 `8000-8003` 四个端口
全部发布到宿主机，于是「匿名者用一枚公开默认密钥冒充任意用户直下真单」这条
路径**可以完全绕过网关**——nginx/网关上的鉴权、限流、审计统统是装饰，直接
打 `trade:8002` 即可。2026-09-23 已收敛为「8000 对外，8001-8003 回环」。

这条不变式靠人记不住：`docker-compose.yml` 里少写一个 `127.0.0.1:` 前缀就退回
原状，而 `docker compose up -d` 不会有任何报错。所以钉在测试里。

本文件证明什么、不证明什么
--------------------------
证明：compose 里 `ports:` 的**宿主绑定地址**符合预期（解析逻辑见下，用合成
compose 夹具真测）。不证明：容器内进程实际监听的地址（那是 `main_oss.py` 的
`host=0.0.0.0`，容器网络内必需，与本文件无关）。

环境说明：`docker-compose.yml` 在仓库根，**不在容器镜像里**。容器内跑本文件时
真实文件那一条会 skip（并打印原因），解析逻辑的断言仍然全跑——所以本文件在
两种环境下都不是空转。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

#: 后端服务端口 → 期望的宿主绑定地址。
#: 8000 是唯一网关入口（局域网客户端、AutoDL 训练节点回调都依赖它），
#: 因此保持 0.0.0.0；其余三个只允许回环。
EXPECTED_HOST_BINDINGS: dict[str, str] = {
    "8000": "0.0.0.0",
    "8001": "127.0.0.1",
    "8002": "127.0.0.1",
    "8003": "127.0.0.1",
}

#: 允许在这些值之间浮动（compose 变量默认值的写法差异）
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


#: `${VAR:-default}` —— 取 default（compose 在无 .env 时的取值）。
#: 必须先做这一步：`:-` 自带冒号，直接按 `:` 切会把端口串切碎。
_VAR_WITH_DEFAULT = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-([^}]*)\}")
_VAR_WITHOUT_DEFAULT = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")


def _resolve_var_defaults(spec: str) -> str:
    spec = _VAR_WITH_DEFAULT.sub(lambda m: m.group(1), spec)
    # 无默认值的 ${VAR}：解析成空，由下面的判定把它当「未指定主机 IP」
    # （= 全网卡）处理——失败方向偏保守，宁可报危险也不漏判。
    return _VAR_WITHOUT_DEFAULT.sub("", spec)


def parse_published_bindings(compose_text: str) -> dict[str, str]:
    """从 compose 文本里抽出 {容器端口: 宿主绑定地址}。

    覆盖两种写法：

    * 短语法 ``"HOST_IP:HOST_PORT:CONTAINER_PORT"`` / ``"HOST_PORT:CONTAINER_PORT"``
      / ``"CONTAINER_PORT"``。**后两种没有主机 IP，等价于绑定所有网卡** ——
      这正是 C1 当时的形状，必须能识别出来。
    * 长语法（``target``/``host_ip`` 映射）。

    ``${VAR:-default}`` 按 default 取值：本函数的用途是判断「默认部署长什么样」，
    不是复现某个特定的 .env。
    """
    cfg = yaml.safe_load(compose_text) or {}
    out: dict[str, str] = {}
    for svc in (cfg.get("services") or {}).values():
        for entry in svc.get("ports") or []:
            if isinstance(entry, dict):
                target = str(entry.get("target", ""))
                host_ip = str(entry.get("host_ip", "") or "")
                if target:
                    out[target] = host_ip or "0.0.0.0"
                continue

            parts = _resolve_var_defaults(str(entry)).split(":")
            if len(parts) >= 3:
                # HOST_IP:HOST_PORT:CONTAINER_PORT —— 空主机 IP 视同全网卡
                out[parts[2]] = parts[0] or "0.0.0.0"
            elif len(parts) == 2:
                # HOST_PORT:CONTAINER_PORT —— 无主机 IP = 全网卡
                out[parts[1]] = "0.0.0.0"
            else:
                out[parts[0]] = "0.0.0.0"
    return out


# ---------------------------------------------------------------------------
# 解析逻辑本身（合成夹具，两种环境都真跑）
# ---------------------------------------------------------------------------


def test_short_syntax_without_host_ip_means_all_interfaces() -> None:
    """`"8000:8000"` 就是绑全网卡——这是 C1 当时的形状，必须能识别。"""
    got = parse_published_bindings(
        "services:\n  app:\n    ports:\n      - \"8000:8000\"\n"
    )
    assert got == {"8000": "0.0.0.0"}


def test_short_syntax_with_loopback_host_ip() -> None:
    got = parse_published_bindings(
        "services:\n  app:\n    ports:\n      - \"127.0.0.1:8002:8002\"\n"
    )
    assert got == {"8002": "127.0.0.1"}


def test_long_syntax_is_parsed() -> None:
    got = parse_published_bindings(
        "services:\n"
        "  app:\n"
        "    ports:\n"
        "      - mode: ingress\n"
        "        host_ip: 127.0.0.1\n"
        "        target: 8003\n"
        "        published: \"8003\"\n"
    )
    assert got == {"8003": "127.0.0.1"}


def test_var_default_form_is_resolved_before_splitting() -> None:
    """`${VAR:-default}` 里的 `:-` 自带冒号，必须先取值再切——否则端口串被切碎。

    这是当前 compose 的实际写法，切错了本文件对真实文件的断言会全程失灵。
    """
    got = parse_published_bindings(
        "services:\n"
        "  app:\n"
        "    ports:\n"
        "      - \"${QM_API_BIND:-0.0.0.0}:8000:8000\"\n"
        "      - \"${QM_TRADE_BIND:-127.0.0.1}:8002:8002\"\n"
    )
    assert got == {"8000": "0.0.0.0", "8002": "127.0.0.1"}


def test_var_without_default_is_treated_as_all_interfaces() -> None:
    """无默认值的 `${VAR}` → 空主机 IP → 保守判为全网卡（宁可报危险也不漏判）。"""
    got = parse_published_bindings(
        "services:\n  app:\n    ports:\n      - \"${SOME_BIND}:8003:8003\"\n"
    )
    assert got == {"8003": "0.0.0.0"}


def test_shipped_shape_is_recognized_as_unsafe() -> None:
    """回归锚点：C1 当时的 compose 形状必须被判为「全网卡」。

    如果解析器哪天变得宽松到把这种形状读成回环，下面的真实文件断言就形同虚设。
    """
    c1_like = (
        "services:\n"
        "  quantmind:\n"
        "    ports:\n"
        "      - \"8000:8000\"\n"
        "      - \"8001:8001\"\n"
        "      - \"8002:8002\"\n"
        "      - \"8003:8003\"\n"
    )
    got = parse_published_bindings(c1_like)
    assert set(got.values()) == {"0.0.0.0"}, "解析器没能认出危险形状"


# ---------------------------------------------------------------------------
# 真实文件（容器内不可达时 skip，并打印原因）
# ---------------------------------------------------------------------------


def _load_real_compose() -> str:
    path = _repo_root() / "docker-compose.yml"
    if not path.is_file():
        pytest.skip(
            f"{path} 不在当前环境（容器内只有 backend/ 是 bind mount）。"
            "解析逻辑已由上面的合成夹具覆盖；真实文件的校验请在宿主机跑。"
        )
    return path.read_text(encoding="utf-8")


def test_gateway_port_is_publicly_bound() -> None:
    """8000 必须保持对外——局域网客户端与 AutoDL 训练节点回调都依赖它。"""
    got = parse_published_bindings(_load_real_compose())
    assert "8000" in got, "compose 里找不到 8000 端口发布，网关将不可达"
    assert got["8000"] not in _LOOPBACK and got["8000"] != "localhost", (
        f"8000 被绑到了回环（{got['8000']}）——局域网客户端与 AutoDL 训练回调会连不上"
    )


@pytest.mark.parametrize("port", ["8001", "8002", "8003"])
def test_internal_service_ports_are_loopback_only(port: str) -> None:
    """engine/trade/stream **不得**绑全网卡：绑了就能绕过网关上的鉴权直打。"""
    got = parse_published_bindings(_load_real_compose())
    assert port in got, f"compose 里找不到 {port} 端口发布"
    assert got[port] in _LOOPBACK, (
        f"{port} 绑到了 {got[port]!r}（非回环）。"
        "这会绕过网关鉴权/限流/审计——C1 事故的同类形状。"
        "若确需临时放开，请在 .env 覆盖 QM_*_BIND，不要改这个默认值。"
    )
