"""P2-6 不可变发布的结构与行为回归（钉住三件事，任一漂移即红）。

1. ``docker-compose.prod.yml`` 覆盖层：三个应用服务 ``volumes: !override`` 后
   只剩数据类挂载（白名单唯一事实源 = ``deploy/release_lib.sh``）；
2. base compose 里被覆盖层剥掉的每个代码挂载，都在 ``docker/Dockerfile.oss``
   有烘焙对应（``BAKED`` 映射；例外须显式列入 ``NOT_BAKED`` 并写清理由）；
3. ``deploy/release_lib.sh`` 的挂载过滤器行为（bash 子进程实测，含符号链接
   PROJECT_DIR 的双前缀匹配）。

可选：``QM_TEST_BAKED_PATHS=1`` 时起一次性容器核对镜像内烘焙路径真实存在
（需本地已有 quantmind-oss:latest，默认跳过）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

APP_SERVICES = ("quantmind", "celery-worker", "celery-beat")

# 覆盖层剥离的挂载源 → Dockerfile.oss 里对应的 COPY（源, 归一化目标））。
# dest 不以 / 开头时按 WORKDIR /app 归一。
BAKED = {
    "./backend": ("backend", "/app/backend"),
    "./config": ("config", "/app/config"),
    "./docs": ("docs", "/app/docs"),
    "./scripts": ("scripts", "/app/scripts"),
    "./docker/training": ("docker/training", "/app/docker/training"),
    "./strategy_templates": ("strategy_templates", "/app/strategy_templates"),
    "./docker/conda-shim": ("docker/conda-shim", "/usr/local/bin/conda"),
    "./docker/litellm_sitecustomize.py": (
        "docker/litellm_sitecustomize.py",
        "/usr/local/lib/python3.10/site-packages/sitecustomize.py",
    ),
    "./.env.example": (".env.example", "/app/.env.example"),
    "./docker-compose.yml": ("docker-compose.yml", "/app/docker-compose.yml"),
}
# 剥离但**故意不烘焙**的挂载（新增例外必须写理由；测试同时校验这些项确实被剥离）
NOT_BAKED = {
    "./.git": "镜像身份以 qm.git.commit Label + /app/deploy_stamp.json 为准，不烘焙 .git",
    "./rd-agent/rdagent": "由 docker/install_rdagent.sh 以 pip 安装（源码随后删除），非 COPY 烘焙",
}

# 覆盖层主动多烘焙的两份（不在 base 挂载差集里，但生产运行面依赖）
PROD_EXTRA_BAKED = (
    ("deploy/release_lib.sh", "/app/deploy/release_lib.sh"),
    ("docker-compose.prod.yml", "/app/docker-compose.prod.yml"),
)


class _ComposeLoader(yaml.SafeLoader):
    """能解析 compose 的 !override / !reset 标签（取值语义按序列/映射原样构造）。"""


def _passthrough(loader: yaml.SafeLoader, node: yaml.Node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return loader.construct_scalar(node)


_ComposeLoader.add_constructor("!override", _passthrough)
_ComposeLoader.add_constructor("!reset", _passthrough)


def _find_repo_root() -> Path:
    env = os.environ.get("QM_TEST_PROJECT_DIR") or os.environ.get("QUANTMIND_PROJECT_DIR")
    if env and (Path(env) / "docker-compose.yml").is_file():
        return Path(env)
    for cand in Path(__file__).resolve().parents:
        if (cand / "docker-compose.yml").is_file() and (cand / "docker-compose.prod.yml").is_file():
            return cand
    pytest.fail("找不到仓库根（docker-compose.yml + docker-compose.prod.yml 所在目录）")


REPO = _find_repo_root()
BASE_COMPOSE = REPO / "docker-compose.yml"
PROD_COMPOSE = REPO / "docker-compose.prod.yml"
RELEASE_LIB = REPO / "deploy" / "release_lib.sh"
DOCKERFILE = REPO / "docker" / "Dockerfile.oss"


def _load_compose(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return yaml.load(fh, Loader=_ComposeLoader)


def _volumes_of(compose: dict, service: str) -> list:
    return compose["services"][service].get("volumes") or []


def _vol_source_target(item) -> tuple[str, str]:
    if isinstance(item, str):
        parts = item.split(":")
        return parts[0], parts[1] if len(parts) > 1 else ""
    return str(item.get("source", "")), str(item.get("target", ""))


def _data_mount_whitelist() -> set[str]:
    text = RELEASE_LIB.read_text(encoding="utf-8")
    m = re.search(r'^QM_DATA_MOUNT_NAMES="([^"]*)"', text, flags=re.M)
    assert m, "release_lib.sh 缺少 QM_DATA_MOUNT_NAMES（白名单唯一事实源）"
    return set(m.group(1).split())


def _dockerfile_copies() -> set[tuple[str, str]]:
    copies: set[tuple[str, str]] = set()
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("COPY "):
            continue
        parts = line.split()[1:]
        if len(parts) < 2:
            continue
        dest = parts[-1]
        if not dest.startswith("/"):
            dest = f"/app/{dest}"
        for src in parts[:-1]:
            copies.add((src.rstrip("/"), dest.rstrip("/")))
    return copies


# ── 1. 覆盖层结构 ─────────────────────────────────────────────────────────


def test_prod_overlay_covers_exactly_app_services():
    prod = _load_compose(PROD_COMPOSE)
    assert set(prod["services"]) == set(APP_SERVICES), (
        "覆盖层只应改写三个应用服务；新增服务加入 volumes: !override 前先想清代码挂载面"
    )


def test_prod_overlay_uses_override_tag():
    text = PROD_COMPOSE.read_text(encoding="utf-8")
    assert text.count("volumes: !override") == len(APP_SERVICES), (
        "三个服务都须用 !override（!reset 会清空列表、丢弃给定值——已实测）"
    )


def test_prod_overlay_volumes_are_data_only():
    whitelist = _data_mount_whitelist()
    prod = _load_compose(PROD_COMPOSE)
    for service in APP_SERVICES:
        volumes = _volumes_of(prod, service)
        assert volumes, f"{service}: !override 后 volumes 为空 = 误用 !reset 的征兆"
        for item in volumes:
            src, target = _vol_source_target(item)
            assert src, f"{service}: 挂载项缺 source: {item!r}"
            if src.startswith("/"):
                assert not src.startswith(str(REPO)), (
                    f"{service}: 项目目录被绝对路径挂入（{src} → {target}）——代码面泄漏"
                )
                continue
            name = src[2:] if src.startswith("./") else src
            name = name.split("/")[0]
            assert name in whitelist, (
                f"{service}: 生产容器仍挂载非数据类路径 {src} → {target}；"
                "要么删掉该挂载，要么在 Dockerfile.oss 烘焙后在白名单评审"
            )


def test_prod_overlay_keeps_expected_mounts():
    prod = _load_compose(PROD_COMPOSE)
    expected = {
        "quantmind": {
            "./data",
            "/opt/tdx-aidata",
            "./models",
            "./db",
            "./logs",
            "./configs",
            "./user_pools_local",
            "./alphaagent",
            "/var/run/docker.sock",
            "/mnt/tdx-shared",
        },
        "celery-worker": {"./data", "./models", "./db", "./logs", "./user_pools_local"},
        "celery-beat": {"./logs", "./data", "./db"},
    }
    for service in APP_SERVICES:
        got = {_vol_source_target(item)[0] for item in _volumes_of(prod, service)}
        assert got == expected[service], (
            f"{service}: 数据类挂载集漂移（丢挂载=断数据面，加挂载=评审白名单）；got={sorted(got)}"
        )


# ── 2. 剥离项 ↔ 烘焙映射 ──────────────────────────────────────────────────


def test_every_removed_code_mount_is_baked():
    if not DOCKERFILE.is_file():
        pytest.skip("容器内无构建源（docker/Dockerfile.oss 不烘焙），本项仅宿主/CI 有意义")
    base = _load_compose(BASE_COMPOSE)
    prod = _load_compose(PROD_COMPOSE)
    removed: set[str] = set()
    for service in APP_SERVICES:
        base_srcs = {_vol_source_target(i)[0] for i in _volumes_of(base, service)}
        prod_srcs = {_vol_source_target(i)[0] for i in _volumes_of(prod, service)}
        removed |= base_srcs - prod_srcs

    assert removed, "base compose 已无代码挂载可剥——覆盖层与映射表该整体复核了"
    assert set(NOT_BAKED) <= removed, (
        f"NOT_BAKED 里出现过期豁免（实际未被剥离）: {sorted(set(NOT_BAKED) - removed)}"
    )
    copies = _dockerfile_copies()
    for src in sorted(removed):
        if src in NOT_BAKED:
            continue
        assert src in BAKED, (
            f"{src} 被覆盖层剥离，但既不在 BAKED 烘焙映射也不在 NOT_BAKED 豁免——"
            "生产容器里该路径会直接消失"
        )
        assert BAKED[src] in copies, (
            f"{src} 的烘焙 COPY {BAKED[src]!r} 在 docker/Dockerfile.oss 里不存在（被删/改名？）"
        )


def test_prod_extra_baked_files_present():
    if not DOCKERFILE.is_file():
        pytest.skip("容器内无构建源（docker/Dockerfile.oss 不烘焙），本项仅宿主/CI 有意义")
    copies = _dockerfile_copies()
    for pair in PROD_EXTRA_BAKED:
        assert pair in copies, f"生产运行面依赖的 {pair[0]} 未烘焙（/app 内会缺文件）"


def test_release_lib_whitelist_pinned():
    assert _data_mount_whitelist() == {
        "data",
        "models",
        "db",
        "logs",
        "configs",
        "user_pools_local",
        "alphaagent",
    }, "挂载白名单变了：只在评审过新数据的性质（数据 vs 代码）后同步这里"


# ── 3. 挂载过滤器行为（bash 子进程实测） ─────────────────────────────────────


def _run_filter(project_dir: Path, sources: list[str]) -> list[str]:
    assert shutil.which("bash"), "需要 bash 执行 release_lib.sh"
    script = (
        'set -Eeuo pipefail\n'
        'source "$1"\n'
        'printf "%s\\n" "${@:3}" | qm_filter_code_mount_sources "$2"\n'
    )
    proc = subprocess.run(
        ["bash", "-c", script, "qm-test", str(RELEASE_LIB), str(project_dir), *sources],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return [ln for ln in proc.stdout.splitlines() if ln]


def test_mount_filter_flags_code_paths_only(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    sources = [
        str(project / "data"),
        str(project / "backend"),
        str(project / "config"),
        str(project / ".git"),
        str(project / "docker-compose.yml"),
        str(project),  # 项目目录整体挂入 = 越界
        "/opt/tdx-aidata",  # 项目外路径一律放行
        "/var/run/docker.sock",
    ]
    got = _run_filter(project, sources)
    assert got == [
        str(project / "backend"),
        str(project / "config"),
        str(project / ".git"),
        str(project / "docker-compose.yml"),
        str(project),
    ], "过滤器分类漂移：代码面路径必须全部被标记为越界"


def test_mount_filter_matches_symlinked_project_dir(tmp_path: Path):
    real = tmp_path / "real"
    (real / "data").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)

    # PROJECT_DIR 为符号链接时，字面与物理两种形态都必须同样判定
    sources = [
        str(link / "backend"),
        str(real / "backend"),
        str(link / "data"),
        str(real / "data"),
        str(real),  # 物理形态的项目目录本身
    ]
    got = _run_filter(link, sources)
    assert got == [
        str(link / "backend"),
        str(real / "backend"),
        str(real),
    ], "符号链接 PROJECT_DIR 双前缀匹配失效（漏一种形态=假通过）"


# ── 4. 可选：镜像内烘焙路径真实存在（QM_TEST_BAKED_PATHS=1） ─────────────────


@pytest.mark.skipif(
    os.environ.get("QM_TEST_BAKED_PATHS") != "1",
    reason="设 QM_TEST_BAKED_PATHS=1 才起容器核对（需本地已有 quantmind-oss:latest）",
)
def test_baked_paths_exist_in_image():
    paths = [
        "/app/backend/main_oss.py",
        "/app/config",
        "/app/docs",
        "/app/scripts",
        "/app/strategy_templates",
        "/app/docker/training/train.py",
        "/app/deploy/release_lib.sh",
        "/app/docker-compose.prod.yml",
        "/app/docker-compose.yml",
        "/app/.env.example",
        "/app/deploy_stamp.json",
        "/usr/local/bin/conda",
        "/usr/local/lib/python3.10/site-packages/sitecustomize.py",
    ]
    script = "for p in \"$@\"; do [ -e \"$p\" ] || { echo \"MISSING $p\"; exit 1; }; done"
    proc = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "sh",
            "quantmind-oss:latest",
            "-c",
            script,
            "qm-check",
            *paths,
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"镜像内烘焙路径缺失：{proc.stdout or proc.stderr}"
