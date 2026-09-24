"""便携包净化闸门（``deploy/portable/pack_guard.py``）的用例：**植入必被抓，干净必放行**。

为什么这些用例是必须的
----------------------
这道闸门守的是一次性、不可回滚的事件：**包已经发给别人了**。包里带出去的是
运营者机器的坐标（内网地址、SMB 共享、桥端口）与凭据载体（``pack.env``、
``runtime.env``、``backend/config/users/**``）——`git revert` 追不回来。

而它自己失败的形态恰恰是静默的：检测器没加载上、排除清单写了个永远匹配不上的
模式（比如带 Windows 反斜杠）、扫描面塌缩成空——**全都表现为「合计：违规 0 项」**。
本仓管这叫「验收假通过」：零项参与即失败。所以这里逐条植入真实形态的违规，
要求闸门一条不漏地红，并且**在它拒绝出包时产物文件不许存在**。

覆盖四类判据（每一类都有植入用例）
----------------------------------
1. 路径：排除项（压缩时跳过／压缩后还在即违规）与必备项（少一个即残包）——**两侧都测**，
   只测一侧的话反向写错（把必备文件也排掉、或把空 zip 判成全绿）照样全绿；
2. 内容：内网地址与明文口令（复用公仓那份检测器）；
3. 宿主残留值：打包机 ``.env`` 里的真值逐字匹配——**且命中值绝不许出现在输出里**
   （构建日志会被贴进 issue，回显等于把口令抄了两份）；
4. 产物形态与产物结构：live-node 前端标记、两个顶层目录的 zip。

外加两条「不误报」用例（第三方运行时、公仓检测器自己划的两块豁免）。刻意放在同一份
文件里：**只测「抓得到」会逼出乱杀，只测「不误报」会养出空转**，两个方向都要钉。

跑法::

    cd backend && python -m pytest tests/test_pack_guard.py -q --no-cov
"""

from __future__ import annotations

import importlib.util
import shutil
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
    sys.modules[name] = (
        mod  # 先登记：pack_guard 里的 `import pack_rules` 要拿到同一个对象
    )
    spec.loader.exec_module(mod)
    return mod


sys.path.insert(0, str(PACK_DIR))
pack_rules = _load("pack_rules", PACK_DIR / "pack_rules.py")
pack_guard = _load("pack_guard", GUARD)


# ---------------------------------------------------------------------------
# 夹具：最小合规 staging（必备文件齐、内容干净）
# ---------------------------------------------------------------------------

#: 打包机上的真实形态：**全是排除清单的对象**。逐条都在实测里出现过
#: （``pack.env`` 16 个键全有值、``bridge/`` 32 个文件、``models/users`` 1.3G）。
HOST_RESIDUE: dict[str, str] = {
    "pack.env": "DB_PASSWORD=whatever\n",
    "config/runtime.env": "INTERNAL_CALL_SECRET=whatever\n",
    "config/secrets.cmd": "set INTERNAL_CALL_SECRET=whatever\r\n",
    "README-LIVE.md": "# 实盘节点部署\n",
    "CHECKLIST.md": "# 实盘执行清单\n",
    "quantbot_front.py": "# QuantBot 前门\n",
    "start-quantbot.bat": "@echo off\r\n",
    "bridge/bridge.py": "# 通达信桥\n",
    "live/import_db.py": "# 实盘导入\n",
    "dsh/payload.py": "# 免 Docker 载荷\n",
    "backend/config/users/db_user_001.json": "{}\n",
    "backend/logs/app.log": "访问路径与地址\n",
    "backend/scripts/log/sync.log": "同步残留\n",
    "backend/__pycache__/mod.cpython-310.pyc": "",
    "backend/scratch/tmp.py": "# 开发期脚手架\n",
    "models/users/mine/model.bin": "binary",
    ".git/config": "[core]\n",
    "node_modules/pkg/index.js": "// x\n",
    ".DS_Store": "",
}

#: 不该被任何规则吃掉的一份（钉住「不过度排除」）：测试夹具是自家树的一部分，
#: 公仓那份检测器只是**不扫**它，不是**不出厂**它。
KEPT_DESPITE_RESIDUE = "backend/services/api/tests/fixtures/sample.json"


def _write(root: Path, rel: str, text: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _make_stage(tmp_path: Path) -> Path:
    """最小合规 staging：必备文件全空、必备 glob 一个、预置模型占位一个。"""
    stage = tmp_path / "QuantMind-Portable-win-x64"
    for rel in pack_rules.REQUIRED_FILES:
        _write(stage, rel)
    _write(stage, "data/upgrade_v1.0.0.sql")
    _write(stage, "models/production/README.txt")
    return stage


def _probe_env(tmp_path: Path, **kv: str) -> Path:
    """受控探针来源。

    **每个用例都必须显式传它**：不传的话闸门会去读打包机真实的 ``.env`` /
    ``config/runtime.env``，测试结果就取决于跑测试那台机器上有什么——那不叫测试。
    """
    return _write(tmp_path, "probe.env", "".join(f"{k}={v}\n" for k, v in kv.items()))


def _clean(tmp_path: Path) -> tuple[Path, Path]:
    return _make_stage(tmp_path), _probe_env(tmp_path)


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GUARD), *args],
        capture_output=True,
        text=True,
        cwd=str(cwd or REPO_ROOT),
    )


def _verify(stage: Path, env: Path) -> subprocess.CompletedProcess[str]:
    return _run("--stage", str(stage), "--env-file", str(env))


def _names(zip_path: Path) -> set[str]:
    with zipfile.ZipFile(zip_path) as zf:
        return set(zf.namelist())


# ---------------------------------------------------------------------------
# 1) 干净必放行（否则下面所有「抓到」都没有意义）
# ---------------------------------------------------------------------------


def test_clean_stage_passes(tmp_path: Path) -> None:
    stage, env = _clean(tmp_path)
    proc = _verify(stage, env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "合计：违规 0 项" in proc.stdout, proc.stdout


def test_make_zip_then_verify_the_artifact(tmp_path: Path) -> None:
    """出包 → 产物复核，整条链在干净夹具上必须全绿，且结构正确。"""
    stage, env = _clean(tmp_path)
    out = tmp_path / "pack.zip"
    proc = _run("--make-zip", str(stage), str(out), "--env-file", str(env))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out.is_file()

    names = _names(out)
    root = f"{stage.name}/"
    assert root + "web/index.html" in names
    assert root + "data/upgrade_v1.0.0.sql" in names
    for rel in pack_rules.EMPTY_DIRS:
        # 目录项必须显式写出：后端按固定路径找它们，缺目录比空目录更容易出怪问题
        assert f"{root}{rel}/" in names, f"保留目录 {rel} 没进产物"

    proc = _run("--zip", str(out), "--env-file", str(env))
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_guard_is_cwd_independent(tmp_path: Path) -> None:
    """从任意目录调用都得工作（构建脚本并不总在仓库根跑）。"""
    stage, env = _clean(tmp_path)
    proc = _run("--stage", str(stage), "--env-file", str(env), cwd=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# 2) 植入必被抓：路径判据（两侧）
# ---------------------------------------------------------------------------


def test_missing_required_file_is_fatal(tmp_path: Path) -> None:
    stage, env = _clean(tmp_path)
    (stage / "web/index.html").unlink()
    proc = _verify(stage, env)
    assert proc.returncode == 1
    assert "缺必备" in proc.stdout and "web/index.html" in proc.stdout


def test_missing_required_glob_is_fatal(tmp_path: Path) -> None:
    """增量升级 SQL 一个都没有 = 增量迁移永不执行（历史 bug），必须拦。"""
    stage, env = _clean(tmp_path)
    (stage / "data/upgrade_v1.0.0.sql").unlink()
    proc = _verify(stage, env)
    assert proc.returncode == 1
    assert "data/upgrade_*.sql" in proc.stdout


def test_missing_preinstalled_models_is_only_a_note(tmp_path: Path) -> None:
    """预置模型缺失不拦出包（还有别的交付形态），但要在报告里说出来——不许静默。"""
    stage, env = _clean(tmp_path)
    (stage / "models/production/README.txt").unlink()
    proc = _verify(stage, env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "models/production" in proc.stdout


def test_required_files_are_not_swallowed_by_the_exclude_list() -> None:
    """必备清单与排除清单不许打架：一个文件不能既必须有、又被排掉。"""
    for rel in (*pack_rules.REQUIRED_FILES, "data/upgrade_v1.0.0.sql"):
        assert pack_rules.match_excludes(rel) is None, f"{rel} 被排除清单吃掉了"


def test_every_exclude_rule_is_well_formed() -> None:
    """模式写错的形态：Windows 反斜杠、前导斜杠、空理由——都表现为**永远匹配不上**。"""
    for rule in pack_rules.EXCLUDES:
        assert rule.reason.strip(), f"{rule.pattern} 没写理由"
        assert rule.scope in ("ours", "all"), rule.pattern
        assert not rule.pattern.startswith("/"), rule.pattern
        assert "\\" not in rule.pattern, f"{rule.pattern}：路径分隔符必须是正斜杠"


def test_every_rule_can_actually_fire() -> None:
    """**死规则检测**：模式首段是字面量时，它必须是自家树的一员（或 scope=``all``）。

    这条不变量来自一次实测事故：``models`` 不在 ``OUR_TREES`` 里，于是
    ``models/users/**``（1.3G 私有模型）与 ``models/.qm_models_ok`` 两条规则的
    ``scope="ours"`` 让它们**从未命中过**——staging 报告一条不报、产物复核也放行，
    规则本身却写得漂漂亮亮。首段是通配（``**/*.log``）的规则不受约束：
    它本来就只覆盖自家树，那是刻意的误报纪律。
    """
    for rule in pack_rules.EXCLUDES:
        head = rule.pattern.rstrip("/").split("/", 1)[0]
        if any(ch in head for ch in "*?[") or rule.scope == "all":
            continue
        assert head in pack_rules.OUR_TREES, (
            f"{rule.pattern}：首段 {head!r} 不在 OUR_TREES 里，"
            "这条 ours 规则永远不会命中（要么加进 OUR_TREES，要么改 scope='all'）"
        )


def test_the_previously_dead_rules_now_fire() -> None:
    """把上面那条不变量的两个具体受害者钉成行为断言，防止再次回归。"""
    assert pack_rules.match_excludes("models/users/mine/model.bin") is not None
    assert pack_rules.match_excludes("models/.qm_models_ok") is not None
    assert pack_rules.match_excludes("logs/app.log") is not None
    # 反向：预置模型与第三方运行时的同名后缀不许被连带吃掉
    assert pack_rules.match_excludes("models/production/m1/model.onnx") is None
    assert pack_rules.match_excludes("runtime/python/x.log") is None


@pytest.mark.parametrize(
    ("pattern", "rel", "hit"),
    [
        ("**/.env", ".env", True),
        ("**/.env", "backend/.env", True),
        ("**/.env", "backend/app.env", False),  # 段内要精确等于 .env
        ("**/.env.*", "config/.env.production", True),
        ("pack.env", "pack.env", True),
        ("pack.env", "backend/pack.env", False),  # 只匹配包根那份（约定如此）
        ("bridge/", "bridge/a/b.py", True),
        ("bridge/", "bridge", False),  # 目录自身不匹配：规则只判文件
        ("data/upgrade_*.sql", "data/upgrade_v1.0.7.sql", True),
        ("data/upgrade_*.sql", "data/upgrade_v1.0.7.sql.bak", False),
        ("models/users/**", "models/users/a/model.bin", True),
        ("backend/logs/**", "backend/logs/x.log", True),
        # `**` 吃 0 段，所以 `a/**` 也匹配裸的 `a`。闸门只拿文件路径来匹配
        # （目录走 EMPTY_DIRS），这一条实际到不了；钉住是为了下次改动时有个准星。
        ("backend/logs/**", "backend/logs", True),
    ],
)
def test_matches_pattern_semantics(pattern: str, rel: str, hit: bool) -> None:
    assert pack_rules.matches_pattern(pattern, rel) is hit


# ---------------------------------------------------------------------------
# 3) 植入必被抓：内容判据
# ---------------------------------------------------------------------------


def test_planted_internal_address_is_fatal(tmp_path: Path) -> None:
    stage, env = _clean(tmp_path)
    _write(
        stage,
        "backend/config/settings.py",
        'TDX_BRIDGE_URL = "http://192.168.31.31:8550"\n',
    )
    proc = _verify(stage, env)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "内网地址" in proc.stdout
    assert "192.168.31.31" in proc.stdout  # 地址本身是坐标型信息，报告里要给出处


def test_planted_plaintext_secret_is_fatal_and_the_value_is_not_echoed(
    tmp_path: Path,
) -> None:
    """命中行里就是明文口令——报告只许给「文件:行 + 键名」。"""
    stage, env = _clean(tmp_path)
    _write(stage, "backend/config/db.py", 'password = "Nettle92b4Qq"\n')
    proc = _verify(stage, env)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "明文口令" in proc.stdout and "db.py:1" in proc.stdout
    assert "Nettle92b4Qq" not in proc.stdout
    assert "Nettle92b4Qq" not in proc.stderr


def test_planted_host_needle_is_fatal_and_the_value_is_never_echoed(
    tmp_path: Path,
) -> None:
    """探针层：拿打包机上 ``.env`` 的**真值**逐字匹配出厂物。

    值回显比命中本身更危险（构建日志会被贴进 issue / 提交到 CI），所以这里同时钉
    两件事：命中了，且值一次都没出现在输出里——只出现来源键名。
    """
    stage, _ = _clean(tmp_path)
    secret = "Zq7xNettle92b4Qq"
    env = _probe_env(tmp_path, FAKE_DB_PASSWORD=secret)
    _write(stage, "backend/config/app.yaml", f"db_password: {secret}\n")

    proc = _verify(stage, env)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "宿主残留值" in proc.stdout
    assert "probe.env:FAKE_DB_PASSWORD" in proc.stdout
    assert secret not in proc.stdout and secret not in proc.stderr


def test_needle_scan_reaches_model_metadata(tmp_path: Path) -> None:
    """模型产物目录也在探针扫描面内：``metadata.json`` 记着训练时的端点与 key。"""
    stage, _ = _clean(tmp_path)
    secret = "Zq7xNettle92b4Qq"
    env = _probe_env(tmp_path, TRAIN_API_KEY=secret)
    _write(stage, "models/production/m1/metadata.json", f'{{"endpoint": "{secret}"}}\n')
    proc = _verify(stage, env)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "metadata.json" in proc.stdout


def test_planted_live_node_frontend_shape_is_fatal(tmp_path: Path) -> None:
    """实盘瘦节点形态的前端打进通用包 = 整页只剩两栏，其它功能全没了。"""
    stage, env = _clean(tmp_path)
    _write(stage, "web/assets/app.js", 'x={VITE_LIVE_NODE_ONLY:"true"};\n')
    proc = _verify(stage, env)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "形态" in proc.stdout and "app.js" in proc.stdout


# ---------------------------------------------------------------------------
# 4) 排除侧：staging 上只提示、产物里必须消失、**且不许动 staging**
# ---------------------------------------------------------------------------


def test_stage_mode_reports_exclusions_without_failing(tmp_path: Path) -> None:
    """staging 是**两个构建器共用**的（Live 包往同一份里覆盖），所以 stage 模式不判死。"""
    stage, env = _clean(tmp_path)
    for rel, text in HOST_RESIDUE.items():
        _write(stage, rel, text)
    proc = _verify(stage, env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "将排除" in proc.stdout
    assert "pack.env" in proc.stdout and "bridge/bridge.py" in proc.stdout


def test_make_zip_drops_exclusions_and_leaves_staging_intact(tmp_path: Path) -> None:
    """本文件的中心用例：写进产物的那一刻才排除，staging 一个字节都不动。"""
    stage, env = _clean(tmp_path)
    for rel, text in HOST_RESIDUE.items():
        _write(stage, rel, text)
    _write(stage, KEPT_DESPITE_RESIDUE, "{}\n")
    out = tmp_path / "pack.zip"

    proc = _run("--make-zip", str(stage), str(out), "--env-file", str(env))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    names = _names(out)
    root = f"{stage.name}/"

    for rel in HOST_RESIDUE:
        assert root + rel not in names, f"排除清单没生效：{rel}"
    assert not any("__pycache__" in n for n in names)
    assert root + KEPT_DESPITE_RESIDUE in names, "过度排除：自家树里的普通文件被吃掉了"
    assert f"{root}models/users/" in names and f"{root}logs/" in names

    # staging 不许被删：Live 构建器还要往同一份里覆盖自己的启动器
    for rel in HOST_RESIDUE:
        assert (stage / rel).is_file(), f"staging 被改动了：{rel} 没了"

    proc = _run("--zip", str(out), "--env-file", str(env))
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_make_zip_refuses_when_the_stage_has_a_violation(tmp_path: Path) -> None:
    """压缩前复核是真闸门，不是打印：违规时**产物文件不许存在**。"""
    stage, env = _clean(tmp_path)
    _write(stage, "backend/config/settings.py", 'HOST = "http://192.168.31.31:8550"\n')
    out = tmp_path / "pack.zip"
    proc = _run("--make-zip", str(stage), str(out), "--env-file", str(env))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert not out.exists(), "违规时仍然写出了产物"
    assert not out.with_name(out.name + ".tmp").exists(), "临时产物没清掉"
    assert "拒绝出包" in proc.stdout


def test_zip_with_two_roots_is_rejected(tmp_path: Path) -> None:
    """两个顶层目录的 zip 解压时会并进别人的文件夹，覆掉对方的 start.bat / pack.env。"""
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("A/x.txt", "x")
        zf.writestr("B/y.txt", "y")
    proc = _run("--zip", str(bad), "--env-file", _probe_env(tmp_path))
    assert proc.returncode != 0
    assert "不是一个顶层目录" in proc.stderr


def test_excluded_content_is_not_scanned(tmp_path: Path) -> None:
    """已排除的文件不再做内容判读：``models/users`` 上千个文件不该被逐个读一遍。"""
    stage, env = _clean(tmp_path)
    _write(
        stage, "backend/config/users/db_user_001.json", '{"password": "Nettle92b4Qq"}\n'
    )
    _write(stage, "models/users/mine/leak.json", '{"token": "Nettle92b4Qq"}\n')
    proc = _verify(stage, env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "明文口令" not in proc.stdout


# ---------------------------------------------------------------------------
# 5) 不误报：第三方运行时与公仓检测器已经裁定过的两块豁免
# ---------------------------------------------------------------------------


def test_vendor_runtimes_are_not_false_positived(tmp_path: Path) -> None:
    """``runtime/`` 等是上游原样下载的：``__pycache__``、``cacert.pem``、
    ``tornado/test/test.key`` 在里面都是**正常**文件。误报会淹掉护栏，
    而被淹掉的护栏下一步就是被人关掉。"""
    stage, env = _clean(tmp_path)
    vendor = {
        "runtime/python/Lib/__pycache__/os.cpython-310.pyc": "",
        "runtime/python/site-packages/tornado/test/test.key": "key\n",
        "runtime/python/site-packages/certifi/cacert.pem": "pem\n",
        "redis/redis-server.exe": "",
    }
    for rel, text in vendor.items():
        _write(stage, rel, text)
    proc = _verify(stage, env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "将排除" not in proc.stdout, "第三方运行时被误判成排除项"


def test_regex_scope_carveouts_follow_the_public_repo_detector(tmp_path: Path) -> None:
    """公仓那份检测器划的豁免在这里同样生效：测试夹具（构造的地址）与 ``web/``
    构建产物（只能重建、不能改一行）。同一串地址放在自家源码里必须照样报——
    这一条由上文的 ``test_planted_internal_address_is_fatal`` 钉住。"""
    stage, env = _clean(tmp_path)
    addr = "http://192.168.31.31:8550"
    _write(stage, "backend/tests/fixtures/sample.py", f'BRIDGE = "{addr}"\n')
    _write(stage, "web/assets/bundle.js", f'const U = "{addr}";\n')
    proc = _verify(stage, env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "内网地址" not in proc.stdout


# ---------------------------------------------------------------------------
# 6) 检测器接缝：**拒绝降级**
# ---------------------------------------------------------------------------


def test_detector_contract_is_pinned() -> None:
    """闸门直接调公仓那份检测器的函数——它改名/改签名，闸门就静默失效。"""
    det = pack_guard.load_detectors()
    for name in ("find_internal_addresses", "find_plaintext_secrets", "_is_test_file"):
        assert callable(getattr(det, name, None)), f"检测器契约变了：{name}"
    assert det.find_internal_addresses('x = "http://192.168.31.31:8550"')


def test_load_detectors_refuses_to_degrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """检测器不在位 = 整条闸门报错，**绝不**留下「正则没加载上所以全绿」的空转护栏。"""
    monkeypatch.setattr(pack_guard, "DETECTORS_PATH", tmp_path / "gone.py")
    with pytest.raises(SystemExit) as excinfo:
        pack_guard.load_detectors()
    assert "拒绝降级" in str(excinfo.value)


@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git 才能判定「已公开」")
def test_is_published_only_covers_tracked_literals(tmp_path: Path) -> None:
    """已跟踪文件里的字面量 = 已经公开（本仓是公开仓），拿它当探针只会淹掉护栏；
    git 不可用时一律**保守留作探针**（宁可多报也不放过）。"""
    assert pack_rules.is_published("QuantMind", REPO_ROOT) is True
    assert pack_rules.is_published("Zq7xNettle92b4Qq", REPO_ROOT) is False
    assert pack_rules.is_published("QuantMind", tmp_path) is False  # 没有 .git
