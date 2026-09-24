"""实盘节点包（``--profile live``）的判据用例：**同一套检测器，两份相反的判据清单**。

为什么单独一份文件
------------------
``test_pack_guard.py`` 钉的是通用便携包那一侧（``pack.env``/``bridge/**`` 是泄漏）。
实盘包恰好相反：同一批文件是**必备**，少一个就是残包；而通用包禁止的私有栏目产物
（``web/assets/LiveTradingPage*``）反过来是这一包存在的理由。两侧判据写在同一个
文件里，改错一行就会把一份包判成另一份 —— 而且**两份都全绿**，那是最坏的形态。

这里逐个钉住翻转点，每一条都配一个「反向也会红」的用例：只测「实盘包收得下」会养出
一份什么都不查的空清单；只测「通用包拦得住」，改一行判据就能让实盘包静默变形。

判据清单本体在 ``deploy/portable/pack_rules.py`` 的包形态一节（``PROFILES["live"]``）。
跑法::

    cd backend && python -m pytest tests/test_pack_guard_live.py -q --no-cov
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PACK_DIR = REPO_ROOT / "deploy" / "portable"
GUARD = PACK_DIR / "pack_guard.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"加载不到 {path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # 先登记：pack_guard 里的 `import pack_rules` 要拿到同一个
    spec.loader.exec_module(mod)
    return mod


sys.path.insert(0, str(PACK_DIR))
pack_rules = _load("pack_rules_livecase", PACK_DIR / "pack_rules.py")
pack_guard = _load("pack_guard_livecase", GUARD)


# ---------------------------------------------------------------------------
# 夹具（与 test_pack_guard.py 同形状，各留一份：两份文件要能各自单独跑）
# ---------------------------------------------------------------------------

#: 实盘包里**必须**在位的两样构建期形态：前端两个开关与关掉的桥止损 daemon。
#: 桥那条是钱的门：daemon 一开，桥自己 5s 轮询市价卖出，与 QuantMind 的
#: sltp_executor 对同一持仓重复卖。
FRONT_FLAGS = 'VITE_ENABLE_REAL_TRADING:"true" VITE_LIVE_NODE_ONLY:"true"'
DISARMED_BRIDGE = "sltp_daemon:\n  enabled: false\n"


def _write(root: Path, rel: str, text: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _probe_env(tmp_path: Path) -> Path:
    """受控探针来源：**不许**让用例去读打包机真实的 ``.env`` / ``config/runtime.env``。"""
    return _write(tmp_path, "probe.env", "SOME_TOKEN=whatever-value-here\n")


def _make_live_stage(tmp_path: Path) -> Path:
    """最小合规的实盘 staging。

    目录名故意用 ``QuantMind-Portable-win-x64``：实盘构建器复用的就是通用包那份
    staging（共用是这套 profile 存在的全部原因），顶层目录名靠 ``--root`` 单独给。
    """
    stage = tmp_path / "QuantMind-Portable-win-x64"
    for rel in pack_rules.LIVE_REQUIRED_FILES:
        _write(stage, rel)
    _write(stage, "data/upgrade_v1.0.0.sql")
    _write(stage, "live/import_live.py")
    _write(stage, "models/production/README.txt")
    _write(stage, "web/assets/index-Abc123.js", FRONT_FLAGS + "\n")
    _write(stage, "web/assets/LiveTradingPage-Xyz789.js", "// 实盘栏目\n")
    _write(stage, "bridge/tdx/config.yaml", DISARMED_BRIDGE)
    return stage


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GUARD), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


def _verify(stage: Path, env: Path) -> subprocess.CompletedProcess[str]:
    return _run("--stage", str(stage), "--env-file", str(env), "--profile", "live")


def _make_zip(stage: Path, out: Path, env: Path, *extra: str):
    return _run(
        "--make-zip",
        str(stage),
        str(out),
        "--env-file",
        str(env),
        "--profile",
        "live",
        *extra,
    )


def _names(zip_path: Path) -> set[str]:
    with zipfile.ZipFile(zip_path) as zf:
        return set(zf.namelist())


def _add_quantbot_payload(stage: Path) -> None:
    _write(stage, "dsh/dsh.cordis.yml", "name: quantbot\n")
    _write(stage, "dsh/node/node.exe", "binary")
    _write(stage, "dsh/node/node_modules/npm/index.js", "// npm\n")
    _write(stage, "dsh/global/node_modules/express/package.json", "{}\n")


# ---------------------------------------------------------------------------
# 1) 干净必放行（否则下面「抓得到」全部没有意义）
# ---------------------------------------------------------------------------


def test_live_stage_with_all_requirements_passes(tmp_path: Path) -> None:
    stage = _make_live_stage(tmp_path)
    proc = _verify(stage, _probe_env(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "形态 live" in proc.stdout, proc.stdout
    assert "合计：违规 0 项" in proc.stdout, proc.stdout


def test_live_stage_fails_without_the_live_frontend(tmp_path: Path) -> None:
    """私有栏目 chunk 是这一包存在的理由：缺了就是「实盘控制台未启用」那个故障。"""
    stage = _make_live_stage(tmp_path)
    (stage / "web/assets/LiveTradingPage-Xyz789.js").unlink()
    proc = _verify(stage, _probe_env(tmp_path))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "缺必备" in proc.stdout and "LiveTradingPage" in proc.stdout


@pytest.mark.parametrize(
    "blob,needle",
    [
        (
            'VITE_ENABLE_REAL_TRADING:"false" VITE_LIVE_NODE_ONLY:"true"',
            "VITE_ENABLE_REAL_TRADING",
        ),
        (
            'VITE_ENABLE_REAL_TRADING:"true" VITE_LIVE_NODE_ONLY:"false"',
            "VITE_LIVE_NODE_ONLY",
        ),
    ],
    ids=["real-trading-off", "live-node-off"],
)
def test_live_stage_requires_both_front_flags(
    tmp_path: Path, blob: str, needle: str
) -> None:
    """**文件在、内容错**也要拦：这正是「夹具形状≠生产形状」那类假通过。

    两个开关都是构建期内联的：漏一个，节点起来要么整页兜底成「实盘未启用」，
    要么多出一堆点开全是空白的栏目。
    """
    stage = _make_live_stage(tmp_path)
    _write(stage, "web/assets/index-Abc123.js", blob + "\n")
    proc = _verify(stage, _probe_env(tmp_path))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "缺必备" in proc.stdout and needle in proc.stdout, proc.stdout


@pytest.mark.parametrize(
    "bridge",
    [
        "sltp_daemon:\n  enabled: true\n",  # 出厂包 2026-09-24 之前的真实形态
        "sltp_daemon:\n  enabled: yes\n",  # YAML 的另一种真值写法，也要拦
        "",  # 段整个不在：不许靠代码默认值兜（那两处默认值曾被写成 true）
        "sltp_daemon:\n  poll_interval_seconds: 5\n",  # 段在、键不在
    ],
    ids=["explicit-true", "yes", "no-section", "no-key"],
)
def test_live_stage_requires_the_bridge_daemon_explicitly_disarmed(
    tmp_path: Path, bridge: str
) -> None:
    stage = _make_live_stage(tmp_path)
    _write(stage, "bridge/tdx/config.yaml", bridge)
    proc = _verify(stage, _probe_env(tmp_path))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "sltp_daemon" in proc.stdout, proc.stdout


# ---------------------------------------------------------------------------
# 2) 同一批文件：通用包拦、实盘包必备（翻转点逐个钉）
# ---------------------------------------------------------------------------


LIVE_ONLY_FILES = (
    "pack.env",
    "bridge/tdx/main.py",
    "live/import_live.py",
    "README-LIVE.md",
    "CHECKLIST.md",
    "quantbot_front.py",
    "start-quantbot.bat",
)


def test_live_only_files_are_required_one_by_one(tmp_path: Path) -> None:
    """这七样在通用包里是泄漏，在实盘包里**缺一个就是残包**。逐个删一遍。

    报告里的标签可能是文件本身（``required_files``），也可能是覆盖它的形态
    （``live/*.py`` 这类 ``required_globs``）—— 两种都算报到了，所以这里按
    判据清单推出可接受的标签，而不是硬写文件名。
    """
    env = _probe_env(tmp_path)
    live = pack_rules.PROFILES["live"]
    for i, rel in enumerate(LIVE_ONLY_FILES):
        broken = _make_live_stage(tmp_path / f"miss{i}")
        (broken / rel).unlink()
        proc = _verify(broken, env)
        assert proc.returncode == 1, f"{rel}\n{proc.stdout}"
        labels = [rel] + [
            pattern
            for pattern, _ in live.required_globs
            if pack_rules.matches_pattern(pattern, rel)
        ]
        assert "缺必备" in proc.stdout and any(
            cand in proc.stdout for cand in labels
        ), f"{rel} 删掉后没报到（标签候选 {labels}）：\n{proc.stdout}"


def test_same_files_are_listed_as_exclusions_in_the_general_profile(
    tmp_path: Path,
) -> None:
    """反向：同一份 staging 过通用包判据时，这批文件必须出现在「将排除」里。"""
    stage = _make_live_stage(tmp_path)
    proc = _run("--stage", str(stage), "--env-file", _probe_env(tmp_path))
    assert "将排除" in proc.stdout, proc.stdout
    for rel in LIVE_ONLY_FILES:
        assert rel in proc.stdout, f"{rel} 没被通用包列为排除项：\n{proc.stdout}"


def test_live_make_zip_keeps_live_only_files_and_drops_the_general_manifest(
    tmp_path: Path,
) -> None:
    """产物侧：实盘包**留着**这批文件，同时**丢掉**通用包那份 ``VERSION``。

    一份包里两份身份清单（``pack=QuantMind-Portable-win-x64`` 与
    ``pack=QuantMind-Live-win-x64``）是最容易看错的那种出厂物。
    """
    stage = _make_live_stage(tmp_path)
    out = tmp_path / "live.zip"
    proc = _make_zip(stage, out, _probe_env(tmp_path), "--root", "QuantMind-Live")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    names = _names(out)
    for rel in LIVE_ONLY_FILES + ("web/assets/LiveTradingPage-Xyz789.js",):
        assert f"QuantMind-Live/{rel}" in names, rel
    assert "QuantMind-Live/VERSION" not in names, "通用包的清单不许进实盘包"
    assert not any(n.endswith("/VERSION-LIVE") for n in names), (
        "VERSION-LIVE 由构建器第 6 步写入，staging 里不该有它"
    )


def test_version_live_is_required_in_the_artifact_but_not_on_staging(
    tmp_path: Path,
) -> None:
    """``VERSION-LIVE`` 是构建器**最后一步**写进 zip 的：staging 阶段查它必然报缺，
    而报一个改不掉的错会让人学会无视整条闸门。所以只在 zip 侧判死。"""
    stage = _make_live_stage(tmp_path)
    env = _probe_env(tmp_path)
    out = tmp_path / "live.zip"
    assert _make_zip(stage, out, env).returncode == 0

    proc = _run("--zip", str(out), "--env-file", str(env), "--profile", "live")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "VERSION-LIVE" in proc.stdout, proc.stdout

    with zipfile.ZipFile(out, "a", zipfile.ZIP_STORED) as zf:
        zf.writestr(f"{stage.name}/VERSION-LIVE", "pack=x\ngit=abc\n")
    proc = _run("--zip", str(out), "--env-file", str(env), "--profile", "live")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_live_zip_contains_none_of_the_excluded_entries(tmp_path: Path) -> None:
    """构建器写 zip 时用的排除清单**就是**闸门那份 —— 产物里不许再有排除项。

    2026-09-22 那份实盘包实测带出去 35244 个本该排除的文件（13 个账户口令密文、
    运行期日志、269 个同步日志、``.pytest_cache``、1.3G 用户模型），因为构建器手写了
    一份 4 条目录的清单、与闸门那份从来没有同步过。本用例钉住「手写第二份」不再
    出现：把排除项塞进 staging，产物里必须一个都不剩，而保留目录仍要在。
    """
    stage = _make_live_stage(tmp_path)
    _write(stage, "backend/config/users/db_user_001.json", "{}\n")
    _write(stage, "backend/logs/api.log", "地址与路径\n")
    _write(stage, "qwenpaw_runtime/python/python.exe", "binary")
    out = tmp_path / "live.zip"
    proc = _make_zip(stage, out, _probe_env(tmp_path), "--root", "QuantMind-Live")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    names = _names(out)
    assert "QuantMind-Live/backend/config/users/db_user_001.json" not in names
    assert "QuantMind-Live/backend/logs/api.log" not in names
    assert "QuantMind-Live/qwenpaw_runtime/python/python.exe" not in names
    # 目录保留（后端按固定路径找它，缺目录比空目录更容易出怪问题）
    assert "QuantMind-Live/models/users/" in names
    assert "QuantMind-Live/backend/logs/" in names


# ---------------------------------------------------------------------------
# 3) 实盘包特有的保留与豁免：**窄**是判据的一部分
# ---------------------------------------------------------------------------


def test_quantbot_payload_dependency_tree_is_kept_in_live_and_dropped_in_general(
    tmp_path: Path,
) -> None:
    """QuantBot 载荷自带 node 依赖树（内嵌 npm + global 包），运行时要它。

    通用清单那条 ``**/node_modules/**`` 是为前端依赖树写的，照搬到实盘包会把
    QuantBot 拆成「页面在、点开报错」。两侧都钉：实盘留下（且**不静默**，报告里
    要有「有意保留」），通用仍然排除。
    """
    stage = _make_live_stage(tmp_path)
    _add_quantbot_payload(stage)
    env = _probe_env(tmp_path)

    live_out = tmp_path / "live.zip"
    proc = _make_zip(stage, live_out, env, "--root", "QuantMind-Live")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    names = _names(live_out)
    assert "QuantMind-Live/dsh/node/node_modules/npm/index.js" in names
    assert "QuantMind-Live/dsh/global/node_modules/express/package.json" in names
    assert "有意保留" in proc.stdout, proc.stdout

    # 通用侧只在判据上钉：同一份 staging 过通用闸门会因为**别的**原因被拦
    # （``pack.env``/``bridge/**`` 那些在这边是泄漏），跑 zip 比不出结论。
    general = pack_rules.PROFILES["general"]
    sample = "dsh/node/node_modules/npm/index.js"
    assert general.match_excludes(sample) is not None, "通用包必须排掉 dsh 载荷"
    assert general.keep_reason(sample) is None, "通用包对 dsh 没有任何保留例外"


def test_half_a_quantbot_payload_is_fatal(tmp_path: Path) -> None:
    """载荷有入口、没有内嵌 node 依赖树 = 页面点开即报错（比整块没有更难排查）。"""
    stage = _make_live_stage(tmp_path)
    _write(stage, "dsh/dsh.cordis.yml", "name: quantbot\n")
    _write(stage, "dsh/node/node.exe", "binary")
    proc = _verify(stage, _probe_env(tmp_path))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "express/package.json" in proc.stdout, proc.stdout


def test_quantbot_payload_absent_is_a_loud_note_not_a_silent_pass(
    tmp_path: Path,
) -> None:
    """载荷没注入（payload 脚本没跑）**不拦出包**（构建器允许这种退化形态），
    但必须出现在报告里 —— 静默少一栏是最坏的形态。"""
    stage = _make_live_stage(tmp_path)
    proc = _verify(stage, _probe_env(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "QuantBot" in proc.stdout, proc.stdout


def test_credentials_inside_the_payload_are_still_dropped(tmp_path: Path) -> None:
    """保留规则必须**窄**：``dsh/**`` 下的凭据与会话缓存照样排除。
    （实测 2026-09-24：载荷里除 node_modules 外没有别的命中项，所以这条不误伤。）"""
    stage = _make_live_stage(tmp_path)
    _add_quantbot_payload(stage)
    _write(stage, "dsh/integrations/sessions/abc.yaml", "cookie: x\n")
    _write(stage, "dsh/home/.credentials.yaml", "token: x\n")
    out = tmp_path / "live.zip"
    proc = _make_zip(stage, out, _probe_env(tmp_path), "--root", "QuantMind-Live")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    names = _names(out)
    assert not any("/credentials" in n for n in names)
    assert not any("/integrations/sessions/" in n for n in names)


def test_live_keep_globs_do_not_swallow_security_excludes() -> None:
    """保留规则只放行 node_modules，不是放行整棵 ``dsh/``。"""
    live = pack_rules.PROFILES["live"]
    assert live.keep_reason("dsh/node/node_modules/npm/index.js")
    for rel in ("dsh/home/.credentials.yaml", "dsh/integrations/sessions/a.yaml"):
        assert live.keep_reason(rel) is None, rel
        assert live.match_excludes(rel) is not None, rel


# ---------------------------------------------------------------------------
# 4) 内网坐标：只对**点名的**那几个文件免报，别处出现仍然判死
# ---------------------------------------------------------------------------


def test_coord_exemption_is_per_file_not_blanket(tmp_path: Path) -> None:
    """实盘包必须带着库里那台的坐标（不然连不上），但豁免是**按文件**给的：

    同一个地址写在 ``pack.env`` / ``README-LIVE.md`` 里是设计（报告里点名做成提示），
    写在源码里仍然是违规 —— 那才是「顺手把坐标复制进去」。
    """
    # 地址按段拼（理由与下面的口令同源，外加一条：本仓是**公开仓库**，
    # 任何具体私网地址都是运营者拓扑的坐标）。用 10.42 这个通用私网段，
    # 不写真实网段；运行期拼出来仍然是权威检测器认得的内网地址。
    address = "http://" + ".".join(("10", "42", "0", "17")) + ":8550"
    stage = _make_live_stage(tmp_path)
    env = _probe_env(tmp_path)
    _write(stage, "pack.env", f"DB_HOST={address}\n")
    _write(stage, "README-LIVE.md", f"# 拓扑\n库里那台 {address}\n")
    proc = _verify(stage, env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "有意分发" in proc.stdout and "pack.env" in proc.stdout, proc.stdout

    _write(stage, "backend/config/settings.py", f'HOST = "{address}"\n')
    proc = _verify(stage, env)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "内网地址" in proc.stdout and "settings.py" in proc.stdout


def test_live_profile_still_scans_for_plaintext_secrets(tmp_path: Path) -> None:
    """豁免只免「坐标」，不免口令：实盘包一样不许有明文口令落进源码。

    值按碎片拼（``_probe_value``）：闸门会拿 ``is_published()`` 把「已经在跟踪文件里
    出现过的值」剔出探针，整串字面量写进来会把自己变成「已公开」，用例仍然全绿、
    探针静默失效。

    口令的形态按**权威检测器**的现行判据写（``password = "..."``）。顺带记一笔它
    现在拦不到的那类：键名带前缀的 ``DB_PASSWORD = "..."`` 不触发正则（lookbehind
    挡掉了 ``_`` 前缀）。这类值的兜底是探针层（宿主 ``.env`` 里的**真值**逐字匹配，
    不看键名大小写）——两层互补，但正则侧的这道口子确实存在。
    """
    stage = _make_live_stage(tmp_path)
    secret = "".join(("Nettle", "92b4", "Qq"))
    _write(stage, "backend/services/api/config.py", f'password = "{secret}"\n')
    proc = _verify(stage, _probe_env(tmp_path))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "明文口令" in proc.stdout and "config.py:1" in proc.stdout
    assert secret not in proc.stdout + proc.stderr, "回显了命中值"


# ---------------------------------------------------------------------------
# 5) 顶层目录名与用法边界
# ---------------------------------------------------------------------------


def test_root_name_is_explicit_and_rejected_outside_make_zip(tmp_path: Path) -> None:
    """顶层目录名只能用 ``--root`` 给：staging 目录名是共用的
    ``QuantMind-Portable-win-x64``，两份包顶层同名时在 Windows 上会解压进同一个
    文件夹、互相覆盖（实测发生过，文档写的是 Live，产物里却是 Portable）。"""
    stage = _make_live_stage(tmp_path)
    env = _probe_env(tmp_path)

    default_out = tmp_path / "default.zip"
    assert _make_zip(stage, default_out, env).returncode == 0
    assert all(n.startswith("QuantMind-Portable-win-x64/") for n in _names(default_out))

    named_out = tmp_path / "named.zip"
    proc = _make_zip(stage, named_out, env, "--root", "QuantMind-Live-win-x64")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert all(n.startswith("QuantMind-Live-win-x64/") for n in _names(named_out))

    proc = _run("--stage", str(stage), "--env-file", str(env), "--root", "X")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "--root" in proc.stderr


def test_unknown_profile_is_a_usage_error(tmp_path: Path) -> None:
    stage = _make_live_stage(tmp_path)
    proc = _run(
        "--stage", str(stage), "--env-file", _probe_env(tmp_path), "--profile", "nope"
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr


def test_general_profile_is_still_the_default(tmp_path: Path) -> None:
    """不给 ``--profile`` 时仍是通用包判据（这次改动不许挪动默认行为）。"""
    stage = _make_live_stage(tmp_path)
    proc = _run("--stage", str(stage), "--env-file", _probe_env(tmp_path))
    assert "形态 general" in proc.stdout, proc.stdout
    # 实盘形态的前端标记在通用包那边是违规（整页只剩两栏，其余功能全没了）
    assert proc.returncode == 1, proc.stdout
    assert "形态" in proc.stdout, proc.stdout


# ---------------------------------------------------------------------------
# 6) 清单自身的完整性：死规则与漏样例
# ---------------------------------------------------------------------------


#: 每条实盘排除规则配一个**真实形状**的样例路径。规则改了名字或改成匹配不上任何
#: 路径的形状（例如误用 Windows 反斜杠），这里会红 —— 死规则比没有规则更危险：
#: 它让人以为有人在守。
#: 注意 ``pack.env`` **不在**实盘排除清单里：它在实盘包是必备项（``LIVE_KEEPS``
#: 把通用清单那条整条撤掉了），所以样例表里也没有它 —— 见下面的翻转用例。
LIVE_EXCLUDE_SAMPLES: dict[str, str] = {
    "**/.env": "backend/.env",
    "**/.env.*": "web/.env.production",
    "**/runtime.env": "config/runtime.env",
    "**/secrets.env": "config/secrets.env",
    "**/secrets.cmd": "run/secrets.cmd",
    "**/.credentials*": "dsh/home/.credentials.yaml",
    "**/integrations/sessions/**": "dsh/integrations/sessions/a.yaml",
    "backend/config/users/**": "backend/config/users/db_user_001.json",
    "**/*.pem": "backend/certs/srv.pem",
    "**/*.key": "backend/certs/srv.key",
    "**/*.pfx": "backend/certs/srv.pfx",
    "**/id_rsa*": "backend/keys/id_rsa",
    "**/id_ed25519*": "backend/keys/id_ed25519",
    "backend/logs/**": "backend/logs/api.log",
    "logs/**": "logs/api.log",
    "backend/scripts/log/**": "backend/scripts/log/202609/sync.log",
    "**/*.log": "backend/x.log",
    "backend/user_strategies/**": "backend/user_strategies/s.py",
    "models/users/**": "models/users/a/model.bin",
    "**/.DS_Store": "backend/.DS_Store",
    "**/__pycache__/**": "backend/__pycache__/a.pyc",
    "**/*.pyc": "backend/a.pyc",
    "**/*.pyo": "backend/a.pyo",
    "**/.pytest_cache/**": "backend/.pytest_cache/CACHEDIR.TAG",
    "**/.ruff_cache/**": "backend/.ruff_cache/CACHEDIR.TAG",
    "**/.mypy_cache/**": "backend/.mypy_cache/CACHEDIR.TAG",
    "**/.coverage*": "backend/.coverage",
    "**/coverage.xml": "backend/coverage.xml",
    "**/htmlcov/**": "backend/htmlcov/index.html",
    "backend/scratch/**": "backend/scratch/tmp.py",
    ".git/**": ".git/config",
    "**/node_modules/**": "web/node_modules/pkg/index.js",
    "models/.qm_models_ok": "models/.qm_models_ok",
    "qwenpaw_runtime/**": "qwenpaw_runtime/python/python.exe",
    "huntly/**": "huntly/server.jar",
    "docker/training/**": "docker/training/train.py",
    "VERSION": "VERSION",
}


def test_every_live_exclude_rule_can_actually_fire() -> None:
    live = pack_rules.PROFILES["live"]
    for rule in live.excludes:
        sample = LIVE_EXCLUDE_SAMPLES.get(rule.pattern)
        assert sample is not None, (
            f"新增/改动了实盘排除规则 {rule.pattern!r}：请在上面补一条能命中它的样例"
            "路径，否则它可能是条永远匹配不上的死规则"
        )
        assert live.match_excludes(sample) is not None, (
            f"规则 {rule.pattern} 匹配不到样例 {sample}"
        )
    patterns = {r.pattern for r in live.excludes}
    stale = set(LIVE_EXCLUDE_SAMPLES) - patterns
    assert not stale, f"样例表里有已不存在的规则（请删）：{sorted(stale)}"
    # 翻转点本身：``LIVE_KEEPS`` 那几条在实盘清单里必须是**整条撤掉**的
    # （撤掉 = 打包时不再跳过 = 真的进包），在通用清单里仍然在。
    general = {r.pattern for r in pack_rules.PROFILES["general"].excludes}
    for pattern in pack_rules.LIVE_KEEPS:
        assert pattern not in patterns, f"{pattern} 还留在实盘排除清单里"
        assert pattern in general, f"{pattern} 不在通用排除清单里？（翻转点写错了）"


def test_live_required_lists_are_not_swallowed_by_the_live_excludes() -> None:
    """必备项与排除项不许打架：一条路径同时出现在两边 = 必然缺一件。"""
    live = pack_rules.PROFILES["live"]
    for rel in live.required_files:
        assert live.match_excludes(rel) is None, f"{rel} 既必备又被排除"
    for must in live.artifact_only_required:
        assert live.match_excludes(must) is None, f"{must} 既必备又被排除"


# ---------------------------------------------------------------------------
# 7) 执行器契约：内容判据必须**真的读到文件**
# ---------------------------------------------------------------------------


def _missing(findings) -> list[str]:
    return [f"{f.rel}: {f.detail}" for f in findings if f.kind == "缺必备"]


def test_check_required_reads_content_even_with_a_one_shot_iterator(
    tmp_path: Path,
) -> None:
    """回归（2026-09-24 首次真跑实盘出包时暴露）。

    ``check_required`` 收的是**一次性**迭代器，而它先用集合推导吃了一遍去数必备项，
    随后把同一个（已空的）迭代器交给了内容判据 —— 于是三条内容判据**一个字节都没读**
    就判定缺必备：任何包、任何内容都过不了这一关。看起来在守，其实一次都没生效。

    这里刻意传 ``iter(list)`` 而不是列表：传列表就把这个契约掩盖掉了。两条都钉 ——
    合规树必须不报，坏树必须报出**内容**原因。
    """
    live = pack_rules.PROFILES["live"]
    clean = _make_live_stage(tmp_path / "clean")
    findings = pack_guard.check_required(
        iter(list(pack_guard.iter_stage(clean))), live, mode="stage"
    )
    assert not _missing(findings), _missing(findings)

    bad = _make_live_stage(tmp_path / "bad")
    # 文件在、内容错：把实盘开关改成 false（真跑过的那次形态）
    _write(
        bad,
        "web/assets/index-Abc123.js",
        'VITE_ENABLE_REAL_TRADING:"false" VITE_LIVE_NODE_ONLY:"true"\n',
    )
    findings = pack_guard.check_required(
        iter(list(pack_guard.iter_stage(bad))), live, mode="stage"
    )
    missing = _missing(findings)
    assert any("VITE_ENABLE_REAL_TRADING" in m for m in missing), missing


def test_artifact_only_required_is_skipped_on_staging_but_not_in_zip(
    tmp_path: Path,
) -> None:
    """同一条必备项在两种模式下结论相反 —— 这正是 ``mode`` 存在的理由。

    直接调 ``check_required`` 钉住：staging 不报 ``VERSION-LIVE``，zip 侧必须报。
    （``VERSION-LIVE`` 是构建器写 zip 之后才追加的：staging 阶段报它 = 一个永远修不好
    的错，只会教人无视整条闸门。）
    """
    live = pack_rules.PROFILES["live"]
    stage = _make_live_stage(tmp_path)
    items = list(pack_guard.iter_stage(stage))
    stage_missing = _missing(pack_guard.check_required(iter(items), live, mode="stage"))
    assert not any("VERSION-LIVE" in m for m in stage_missing), stage_missing
    zip_missing = _missing(pack_guard.check_required(iter(items), live, mode="zip"))
    assert any("VERSION-LIVE" in m for m in zip_missing), zip_missing


# ---------------------------------------------------------------------------
# 8) 模板后缀的放行是**逐条**的：整片裁剪不受它影响
# ---------------------------------------------------------------------------


#: 两片裁剪（``LIVE_TRIMMED``）里**真实存在**的上游模板文件 —— 2026-09-24 从产物里
#: 逐个数出来的（qwenpaw_runtime 4 个、huntly 1 个）。写的是产物里那份相对路径。
TRIMMED_SAMPLE_PATHS = (
    "qwenpaw_runtime/python/Lib/site-packages/agentscope/workspace/_docker/"
    "Dockerfile.template",
    "qwenpaw_runtime/python/Lib/site-packages/agentscope/workspace/_docker/"
    "Dockerfile.node_copy.template",
    "qwenpaw_runtime/python/Lib/site-packages/agentscope/workspace/_docker/"
    "Dockerfile.node_from.template",
    "qwenpaw_runtime/python/Lib/site-packages/numpy/f2py/_backends/meson.build.template",
    "huntly/jre/conf/management/jmxremote.password.template",
)


def test_template_suffix_does_not_rescue_files_from_a_directory_trim() -> None:
    """回归（2026-09-24 产物独立核验抓出）。

    ``match_excludes`` 曾经在循环**外面**写了 ``if is_template_env(rel): return None``：
    名字以 ``.example``/``.sample``/``.template`` 结尾的文件绕过**全部**规则。
    它是为 ``.env.example`` 写的，却顺手在两片裁剪上各开了一个洞 —— 上游随包带的
    模板进了实盘节点包，而报告里一条都不报（闸门当时显示「违规 0 项」）。

    钉住：裁剪目录里的模板**照切**，后缀不是通行证。
    """
    live = pack_rules.PROFILES["live"]
    for rel in TRIMMED_SAMPLE_PATHS:
        rule = live.match_excludes(rel)
        assert rule is not None, f"{rel} 从裁剪里漏了出来"
        assert rule.pattern in {"qwenpaw_runtime/**", "huntly/**"}, (rel, rule.pattern)


def test_credential_carrier_rules_still_let_templates_through() -> None:
    """反向也要钉：把放行收紧成「一律排除」会把模板本身剔掉。

    ``.env.example`` 与 ``.env`` 是两个东西 —— 前者该随包（操作者照着填），后者是真值。
    放行的机制从全局早退挪到规则上（``Exclude.template_exempt``），语义不变。
    """
    for profile_name in ("general", "live"):
        prof = pack_rules.PROFILES[profile_name]
        for rel in ("backend/.env.example", "web/.env.sample", "backend/.env.template"):
            assert prof.match_excludes(rel) is None, f"{profile_name}: {rel} 被剔掉了"
        # 同一条规则的**非**模板形态仍必须排除：收紧过头同样是错
        for rel in ("backend/.env.production", "backend/.env"):
            assert prof.match_excludes(rel) is not None, f"{profile_name}: {rel} 漏了"
    assert (
        pack_rules.PROFILES["live"].match_excludes("dsh/home/.credentials.yaml")
        is not None
    ), "实盘包里真实凭据缓存必须排除"


def test_a_template_under_a_trimmed_tree_is_still_cut() -> None:
    """放行用的是 ``continue`` 而不是 ``return None``。

    形状：``.credentials.yaml.example`` 落在被裁的树里 —— 第一条规则（凭据载体，
    放行模板）说「这条不算」；若它顺手结束整个查找，文件就从裁剪里复活了。
    """
    live = pack_rules.PROFILES["live"]
    rule = live.match_excludes("huntly/.credentials.yaml.example")
    assert rule is not None and rule.pattern == "huntly/**", rule
    # dsh 在实盘包是保留区（LIVE_KEEPS）：同一个文件在那里就该放行（模板无真值）
    assert live.match_excludes("dsh/home/.credentials.yaml.example") is None


def test_no_directory_trim_is_template_exempt() -> None:
    """不变量：目录级裁剪（``xxxx/**``）不许挂 ``template_exempt``。

    挂上就是把这次的 bug 重新写回去 —— 一整片树只因为文件叫 ``*.template`` 就漏。
    """
    rules = (
        pack_rules.EXCLUDES
        + pack_rules.LIVE_TRIMMED
        + pack_rules.LIVE_DROPS
        + pack_rules.PROFILES["general"].excludes
        + pack_rules.PROFILES["live"].excludes
    )
    bad = sorted(
        {r.pattern for r in rules if r.template_exempt and r.pattern.endswith("/**")}
    )
    assert not bad, f"这些裁剪规则挂了模板豁免：{bad}"
    exempt = sorted({r.pattern for r in rules if r.template_exempt})
    assert exempt == ["**/.credentials*", "**/.env.*"], (
        f"template_exempt 的规则集合变了：{exempt} —— 放行面必须是一条条点名的凭据载体"
    )
