"""便携包出厂规则**唯一实现**：排除清单 / 必备清单 / 内容判据 / 宿主残留取样。

谁在用
------
``pack_guard.py`` 的三个模式共用本模块：``--make-zip`` 按 :data:`EXCLUDES` 跳过、
``--stage`` 与 ``--zip`` 按同一份清单复核。**排除与校验不能各写一份清单**——本仓
踩过「两个消费方各记一种读法」的坑（见 ``services/trade/routers/decision_roster.py``
把 data/applied 收敛的那次），这里从一开始就只留一个事实源。

语义（三模式共用一套判据，只差「命中排除清单」算不算错）
--------------------------------------------------------
- 命中排除清单：``--stage`` 视为「打包时会被丢掉」（列出来，不算错）；``--zip``
  视为**违规**——文件既然进了产物，就说明排除没生效。
- **只匹配文件，不判目录**：空目录（``run/``、``logs/``、``models/users/``）是
  要留的——后端按固定路径找它们，缺目录比空目录更容易出怪问题。
- ``scope="ours"`` 的规则只作用于自家树（``backend/``、``config/``、``docker/``、
  ``web/``、``strategy_templates/``、``data/`` 与包根文件）。``runtime/``、
  ``qwenpaw_runtime/``、``pgsql/``、``redis/``、``huntly/`` 是上游原样下载的
  第三方运行时：里面有 ``__pycache__``（正常）、``certifi/cacert.pem``（正常）、
  ``tornado/test/test.key``（正常）。不分 scope 的话这些每一条都误报，护栏会被
  淹掉然后被人关掉——一条会误报的护栏等于没有护栏。

为什么排除发生在**打包时**而不是在 staging 上删
------------------------------------------------
``deploy/portable/build/QuantMind-Portable-win-x64`` 这份 staging 是**两个构建器
共用**的：通用便携包（``build_windows_pack.sh``）与实盘瘦节点包
（``deploy/live-win/build_live_pack.sh``，往同一份 staging 覆盖 ``start.bat`` /
``pack.env`` / ``bridge/`` / ``live/`` / 前端产物）。在 staging 上删 = 偷偷改掉
别人要打的包（Live 构建器自己也立了这条规矩），所以：**清单只作用在 zip 写入侧**，
写出来的产物由 ``--zip`` 复核。
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# 路径规则
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Exclude:
    """一条排除规则。``reason`` 必填——理由比禁止本身重要，下一个人要知道为什么。"""

    pattern: str
    reason: str
    scope: str = "ours"  # "ours" | "all"

    #: 这条规则放行「模板/示例」后缀（见 :func:`is_template_env`）。
    #:
    #: **只给凭据载体这类规则开**：那里 ``.env.example`` 与 ``.env`` 是两个东西——
    #: 前者是给人抄的模板、后者是真值，一刀切会把模板也剔掉。
    #:
    #: **绝不要开在目录级裁剪上**（``qwenpaw_runtime/**``、``huntly/**`` 这类）。
    #: 实测踩过：豁免曾经写成 ``match_excludes`` 开头的**全局早退**，于是任何名字以
    #: ``.example``/``.sample``/``.template`` 结尾的文件**绕过全部规则**——两片裁剪各漏了
    #: 文件（上游随包带的 ``Dockerfile.template``、``jmxremote.password.template``）进产物，
    #: 而报告里一条都不报。同名后缀是**文件的写法**，不是「这不是真值」的证明：
    #: 按后缀发通行证的检查，等于教人把要排除的东西改个名。
    template_exempt: bool = False


#: 自家树的顶层（``scope="ours"`` 的判定依据）。包根文件（``start.bat`` 等）也算自家。
#:
#: **漏一个顶层目录 = 该目录下的 ``scope="ours"`` 规则全部变成死规则**：模式写得再对，
#: ``match_excludes`` 也会因为「不在自家树里」直接跳过——报告里一条都不报，包照出。
#: 实测踩过：``models`` 不在表里时 ``models/users/**``（1.3G 私有模型）与
#: ``models/.qm_models_ok`` 两条规则从未命中过。``backend/tests/test_pack_guard.py``
#: 里有一条不变量用例专门钉这件事（新增规则没生效会被测试拦下）。
OUR_TREES = (
    "backend",
    "config",
    "data",
    "docker",
    "models",
    "strategy_templates",
    "web",
)

#: 第三方运行时目录：``scope="ours"`` 的规则不作用于它们。列在这里而不是靠
#: 后缀白名单兜，是为了让「哪些目录不是我们的」这件事**显式**。
VENDOR_TREES = ("runtime", "qwenpaw_runtime", "pgsql", "redis", "huntly", "dsh")


EXCLUDES: tuple[Exclude, ...] = (
    # ── 1. 密钥与凭据载体：出厂的包只带**模板**，真值一律目标机自生成 ──
    Exclude(
        "pack.env",
        "打包机上的已填配置（DB_PASSWORD / 内网地址 / SMB 共享路径都在里面）。"
        "出厂物只带 pack.env.example，目标机首次启动自己生成",
        "all",
    ),
    Exclude(
        "**/.env",
        "运营者本机环境（真实密钥全集）",
        "all",
    ),
    Exclude(
        "**/.env.*",
        "同上（.env.production 这类变体；模板后缀由本规则的 template_exempt 放行——"
        "只放行这一条，裁剪照切）",
        "all",
        template_exempt=True,
    ),
    Exclude(
        "**/runtime.env",
        "运行期密钥文件（INTERNAL_CALL_SECRET 等由后端首启自生成）",
        "all",
    ),
    Exclude(
        "**/secrets.env",
        "运行期密钥（与 run/secrets.cmd 同源）",
        "all",
    ),
    Exclude(
        "**/secrets.cmd",
        "start.bat 首次启动在本机生成的密钥文件——随包等于把上一位用户的密钥发给下一位",
        "all",
    ),
    Exclude(
        "**/.credentials*",
        "凭据缓存（dsh/integrations 那一族的落盘形态）；.credentials.yaml.example 这类"
        "模板放行（本规则 template_exempt）",
        "all",
        template_exempt=True,
    ),
    Exclude("**/integrations/sessions/**", "登录会话缓存（拿它可冒充登录态）", "all"),
    Exclude(
        "backend/config/users/**",
        "账户口令密文；`.dockerignore` 连镜像都不带它（见 "
        "backend/tests/test_no_internal_addresses_or_plaintext_secrets.py 的用例）",
        "all",
    ),
    Exclude("**/*.pem", "私钥/证书", "ours"),
    Exclude("**/*.key", "私钥", "ours"),
    Exclude("**/*.pfx", "PKCS#12 私钥容器", "ours"),
    Exclude("**/id_rsa*", "SSH 私钥", "all"),
    Exclude("**/id_ed25519*", "SSH 私钥", "all"),
    # ── 2. 运行期残留：打包机跑出来的东西 ──
    Exclude(
        "backend/logs/**",
        "运行期日志：写满访问路径、内网地址、密钥指纹（一次排查就能写进去）。"
        "目录保留（后端往这里写日志），只清文件",
    ),
    # 包根那份用 ``all``：``logs`` 是运行期目录、不是源码树，进不了 OUR_TREES，
    # 写成 ``ours`` 就成了死规则（``start.bat`` 往这儿写日志，实测最容易带出坐标）。
    Exclude("logs/**", "同上（包根那份）", "all"),
    Exclude(
        "backend/scripts/log/**",
        "同步脚本的历史日志树（实测 269 个文件，同样带地址与路径）",
    ),
    Exclude("**/*.log", "构建机残留日志（自家树）", "ours"),
    Exclude(
        "backend/user_strategies/**",
        "运营者自己的策略内容（目录保留：strategy_storage 按固定路径找它）",
    ),
    Exclude(
        "models/users/**",
        "运营者自己训练的模型（实测 1.3G；私有 alpha，且让包体翻倍）。"
        "目录保留（model_registry 扫固定路径）",
    ),
    Exclude("**/.DS_Store", "macOS 目录垃圾", "all"),
    # ── 3. 开发/构建残留 ──
    Exclude(
        "**/__pycache__/**",
        "字节码缓存（自家树；第三方运行时里的是上游原样带的）",
        "ours",
    ),
    Exclude("**/*.pyc", "同上", "ours"),
    Exclude("**/*.pyo", "同上", "ours"),
    Exclude(
        "**/.pytest_cache/**", "测试缓存（nodeids 实测 465KB，带全部用例名）", "ours"
    ),
    Exclude("**/.ruff_cache/**", "lint 缓存", "ours"),
    Exclude("**/.mypy_cache/**", "类型检查缓存", "ours"),
    Exclude("**/.coverage*", "覆盖率数据（逐行记录跑过哪些代码）"),
    Exclude("**/coverage.xml", "覆盖率报告"),
    Exclude("**/htmlcov/**", "覆盖率 HTML（逐行快照源码，155MB）"),
    Exclude("backend/scratch/**", "开发期脚手架（未跟踪的临时脚本）"),
    Exclude(".git/**", "版本库（含全部历史与凭据痕迹）", "all"),
    Exclude("**/node_modules/**", "Node 依赖树（前端产物已在 web/）", "all"),
    Exclude(
        "models/.qm_models_ok",
        "构建期标记（models 复制完整性哨兵），不是出厂物",
    ),
    # ── 4. 实盘瘦节点专属注入物：通用包带上 = 把别人指向运营者那台机器 ──
    # 这四条是这份闸门存在的直接原因：两份包共用一份 staging，通用构建器不删
    # Live 构建器的覆盖物。曾实测 staging 里就躺着 Live 的 pack.env 与 start.bat。
    Exclude("bridge/**", "通达信桥源码（Live 包注入；通用包不该内置桥）", "all"),
    Exclude("live/**", "实盘节点导入/同步脚本（指向 Ubuntu 那台）", "all"),
    Exclude("README-LIVE.md", "实盘节点部署文档（含运营者拓扑与账号形态）", "all"),
    Exclude("CHECKLIST.md", "实盘节点执行清单（同上）", "all"),
    Exclude("quantbot_front.py", "QuantBot 回环前门（dsh 专用）", "all"),
    Exclude("start-quantbot.bat", "QuantBot 启动器（dsh 专用）", "all"),
    Exclude("dsh/**", "QuantBot 免 Docker 载荷（Live 包注入）", "all"),
)


#: 「模板/示例」后缀：给人抄的，不是真值。
#:
#: **它本身不是一条规则**，只是 :func:`is_template_env` 的词表；放行与否由规则上的
#: :attr:`Exclude.template_exempt` 决定（见那里的长注释：这里曾经是全局早退，让裁剪漏文件）。
_KEEP_SUFFIXES = (".example", ".sample", ".template", ".example.env")


#: 必备文件（相对包根）。少一个 = 这一包装不起来或装起来是残的，一律非零退出。
#: **这是闸门的另一半**：只列禁止项的话，空 zip 也能全绿（本仓「验收假通过」的老形状）。
REQUIRED_FILES: tuple[str, ...] = (
    "start.bat",
    "stop.bat",
    "install.bat",
    "install.ps1",
    "pack.env.example",
    "README.md",
    "VERSION",
    "LICENSE",
    "pg_setup.py",
    "restore_backup.bat",
    "backend/main_oss.py",
    "web/index.html",
    "data/stocks/stocks_index.json",
    "docker/training/train.py",
    "runtime/python/python.exe",
    "pgsql/bin/initdb.exe",
    "redis/redis-server.exe",
)

#: 必备文件族：每条至少 1 个（升级 SQL 缺了 = system_events 等增量迁移永不执行）
REQUIRED_GLOBS: tuple[tuple[str, str], ...] = (
    ("data/upgrade_*.sql", "增量升级 SQL（缺了增量迁移永不执行——历史 bug）"),
)

#: **成对必备项** ``(哨兵, 必备, 理由)``：哨兵在、必备不在 = 违规；两个都不在只给提示
#: （整块没有是受支持的降级形态，见 :data:`OPTIONAL_COMPONENTS`）。
#:
#: 用于「附属组件要么整块在、要么整块不在」的情形。**半个组件是最坏形态**：文件清单
#: 看着有、界面入口也在，点开才报错——比整块没有更难排查，也正是本闸门存在的理由。
REQUIRED_PAIRS: tuple[tuple[str, str, str], ...] = (
    (
        "huntly/server.jar",
        "huntly/jre/bin/java.exe",
        "Huntly 有 jar 没有 Windows JRE（新闻聚合点开即报错；"
        "组装判据按内容，不按可执行位——解压不还原权限位）",
    ),
)

#: 可选组件**整块缺失**时的提示（不拦出包）：受支持的降级形态，别让护栏误报。
OPTIONAL_COMPONENTS: tuple[tuple[str, str], ...] = (
    (
        "huntly/server.jar",
        "未内置 Huntly（构建机上没有 lcomplete/huntly 镜像、缓存也没有）：新闻聚合降级",
    ),
)

#: **显式保留的空目录**：内容按排除清单清掉了，目录本身要留在产物里。
#: 后端按固定路径找它们（模型注册表扫 ``models/users``、日志写 ``backend/logs``、
#: start.bat 往 ``run/`` 写密钥），缺目录比空目录更容易出怪问题。
EMPTY_DIRS: tuple[str, ...] = ("models/users", "backend/logs", "logs", "run")

#: 内容级「形态」判据：**结构性错误**才拦。UI 开关（如 VITE_ENABLE_REAL_TRADING）
#: 是策略不是泄漏——Live 包那个构建器已经在构建期挡了形态，这里只拦会让通用包
#: 整块功能消失的那一个。
SHAPE_MARKERS: tuple[tuple[bytes, str], ...] = (
    (
        b'VITE_LIVE_NODE_ONLY:"true"',
        "实盘节点形态前端：整页只剩 QuantBot/实盘两栏，通用包的其它栏目全没了",
    ),
)

#: **私有栏目产物**：按路径判（路径在排除清单之外，所以是独立的一类判据）。
#:
#: ``electron/src/features/local-live/`` 是运营者本机独有的实盘交易栏目
#: （``.gitignore:221`` 排除、不开源）：本机 ``npm run build`` 会把它整块打进
#: ``dist-react/``，而两份包都从这里取 ``web/``。判据（chunk 名）与
#: ``scripts/deploy_frontend.sh`` 第 3 步**同源**——那份脚本拿它拦「别把不开源的
#: 部分 ``docker cp`` 到面向公网的容器」，这里拦的是同一件事的另一条出口。
#:
#: 元组第二项是显式放行开关的名字（``--allow-local-live``，与那份脚本同名同义）：
#: 本机自用包允许带，默认拒绝。**拦的是意外，不是决定**——本机开发时那个目录
#: 一直在，没有这条判据就只剩「构建日志里什么都没有」。
PRIVATE_CHUNKS: tuple[tuple[str, str], ...] = (
    (
        "web/assets/LiveTradingPage*",
        "本机独有实盘栏目的前端产物（源码 electron/src/features/local-live/ 未跟踪、"
        "不开源）——随包出厂等于把不开源的部分发给别人",
    ),
)


#: 文本类后缀：内容扫描（正则 + 宿主值）都只扫这些。白名单而不是黑名单——
#: 后缀不认识就当二进制跳过，避免在 .bin/.onnx 上做正则。
TEXT_SUFFIXES: tuple[str, ...] = (
    ".py",
    ".pyi",
    ".sh",
    ".bash",
    ".bat",
    ".cmd",
    ".ps1",
    ".sql",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".css",
    ".html",
    ".xml",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".properties",
    ".md",
    ".rst",
    ".txt",
    ".example",
    ".sample",
    ".env",
)

#: 内容扫描（内网地址 / 明文口令）的目录范围：只扫自家树 + 包根。
#: 第三方运行时与模型目录不扫——见模块 docstring 的「误报面」。
SCAN_TREES: tuple[str, ...] = (
    "backend",
    "config",
    "data",
    "docker",
    "strategy_templates",
    "web",
)

#: 宿主残留值（逐字精确匹配）的目录范围：比上面宽松——**models/ 也要扫**。
#: 模型产物里的 metadata.json 常常记着训练时的 LLM 端点与 key 变量值，
#: 那是「密钥换了个地方躺着」的典型形态。
NEEDLE_TREES: tuple[str, ...] = SCAN_TREES + ("models",)

#: 单文件扫描上限。超过就**记录并跳过**（绝不静默）——正常出厂文本没有这么大。
MAX_SCAN_BYTES = 512 * 1024 * 1024


# ---------------------------------------------------------------------------
# 匹配
# ---------------------------------------------------------------------------


def in_trees(rel: str, trees: Iterable[str]) -> bool:
    parts = rel.split("/")
    if len(parts) == 1:
        return True  # 包根文件
    return parts[0] in trees


def matches_pattern(pattern: str, rel: str) -> bool:
    """包根相对路径 ``rel`` 是否命中 ``pattern``。

    支持的形态（够用即止，不引第三方 glob 库）：
    - ``a/b``：精确文件；``a/b/``、``a/b/**``：目录下所有文件；
    - ``**`` 吃任意段数（含 0 段）：``**/.env`` 命中 ``.env`` 与 ``config/.env``；
    - 段内 ``*``：``data/upgrade_*.sql``。
    """
    parts = rel.split("/")
    if pattern.endswith("/"):
        pat = pattern.rstrip("/").split("/")
        return len(parts) > len(pat) and parts[: len(pat)] == pat
    pat = pattern.split("/")

    def rec(i: int, j: int) -> bool:
        if i == len(pat):
            return j == len(parts)
        if pat[i] == "**":
            return any(rec(i + 1, k) for k in range(j, len(parts) + 1))
        if j >= len(parts):
            return False
        return fnmatch.fnmatchcase(parts[j], pat[i]) and rec(i + 1, j + 1)

    return rec(0, 0)


def is_template_env(rel: str) -> bool:
    """``.env.example`` 这类**模板**：名字像凭据载体，内容却是给人抄的空壳。

    **它自己不做任何判断**——放行与否要问命中的那条规则有没有 :attr:`Exclude.template_exempt`。
    判据只有一个「文件叫什么」，所以拿它当全局通行证会让整片目录裁剪静默失效。
    """
    name = rel.rsplit("/", 1)[-1].lower()
    return name.endswith(_KEEP_SUFFIXES)


def match_excludes(
    rel: str, rules: tuple[Exclude, ...] | None = None
) -> Exclude | None:
    """命中哪条排除规则（没命中返回 None）。``scope="ours"`` 的规则只作用于自家树。

    模板后缀的放行是**逐条**的（``rule.template_exempt``），不是在循环外面开一个总开关：
    命中一条 ``template_exempt`` 规则只是「这条不算」，后面的规则照查——``.env.example``
    不该被当密钥剔掉，但也**不该因此从裁剪目录里复活**（用 ``continue`` 而不是 ``return None``）。
    """
    for rule in rules if rules is not None else EXCLUDES:
        if rule.scope == "ours" and not in_trees(rel, OUR_TREES):
            continue
        if not matches_pattern(rule.pattern, rel):
            continue
        if rule.template_exempt and is_template_env(rel):
            continue
        return rule
    return None


def find_private_chunks(
    dist_root: Path, chunks: tuple[tuple[str, str], ...] | None = None
) -> list[str]:
    """前端产物目录里命中的私有栏目 chunk（返回 ``web/...`` 形式的包根相对路径）。

    :data:`PRIVATE_CHUNKS` 的模式按**包根**写（``web/`` 即前端的 ``dist-react/``），
    这里补上前缀，让同一份模式既能校验 staging，也能直接校验前端产物目录本身——
    ``build_windows_pack.sh`` 用它把这道判据提到构建期（否则要等 4GB 依赖下完、
    走到最后一步才报）。判据一份，两个调用点。
    """
    pats = PRIVATE_CHUNKS if chunks is None else chunks
    hits: list[str] = []
    root = Path(dist_root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            rel = "web/" + str(path.relative_to(root))
            if any(matches_pattern(pat, rel) for pat, _ in pats):
                hits.append(rel)
    return hits


def is_text_path(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1].lower()
    if name in ("license", "readme", "version", "notice", "makefile", "dockerfile"):
        return True
    return name.endswith(TEXT_SUFFIXES)


# ---------------------------------------------------------------------------
# 宿主残留取样（**只返回 (来源名, 值)，调用方绝不许把值打印出来**）
# ---------------------------------------------------------------------------

#: 取哪些键的值：键名带这些词的才算「值本身敏感」。
_SECRET_KEY_WORDS = re.compile(r"(?i)(pass|pwd|secret|token|key|credential)")

#: 显式补采的坐标类键（键名里没有敏感词，但值是运营者机器的坐标）。
_COORD_KEYS = (
    "HOST_PROJECT_PATH",
    "PROJECT_ROOT",
    "QM_DATA_SHARE",
    "QM_DATA_SHARE_DRIVE",
)

#: 太短的值不当探针：``INFO``/``local`` 这种遍地都是，扫了只会误报。
_MIN_NEEDLE = 12
#: 机器身份探针（家目录/主机名）可以短一点，但不能短到「到处都出现」。
_MIN_IDENTITY_NEEDLE = 5

#: 值里的占位标记：出现即不是真值。
_PLACEHOLDER = (
    "<",
    ">",
    "${",
    "$(",
    "%",
    "change",
    "your",
    "here",
    "xxx",
    "placeholder",
)


def _is_real_value(value: str) -> bool:
    v = value.strip().strip('"').strip("'")
    if len(v) < _MIN_NEEDLE:
        return False
    low = v.lower()
    if any(mark in low for mark in _PLACEHOLDER):
        return False
    return not v.isupper()  # 全大写的多半是占位常量（CHANGE_ME_TOKEN）


def _parse_env(text: str) -> Iterator[tuple[str, str]]:
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        yield key.strip().removeprefix("export ").strip(), value.strip()


def is_published(value: str, repo_root: Path) -> bool:
    """这个值是否**已经出现在已跟踪文件里**（=已经公开）。

    为什么要这一条：``.env`` 里的 ``DB_PASSWORD`` 就是项目公开的默认口令（实测该值
    出现在 58 个已跟踪文件里）。拿一个**已经公开**的值当探针，只会让 52 处正常代码
    默认值全被标成「宿主残留」——护栏被淹掉，真问题（某个私有值漏进包）反而看不见。

    前提：**本仓是公开仓**。哪天转私有，把调用点删掉即可（一行）——那时「已跟踪」
    不再等于「已公开」，这条规则就会放过真泄漏。

    git 不可用（容器里没有 .git / 没装 git）时返回 False：**保守留作探针**，
    宁可多报也不放过。

    ⚠️ 给测试的提醒：夹具里**原样写死**的探针值一旦提交，就把自己也变成了
    「已公开值」——探针当场失效，用例静默退化（本文件配套测试里踩过一次：
    ``test_pack_guard.py`` 的探针一律由碎片拼出，且在文件里说明了原因）。
    """
    if not (repo_root / ".git").exists():
        return False
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "grep", "-q", "-F", "-e", value, "--", "."],
            capture_output=True,
        )
    except OSError:
        return False
    return proc.returncode == 0


def host_needles(
    env_files: Iterable[Path], repo_root: Path | None = None
) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """返回 (探针列表, 读不到的来源, 因已公开而丢弃的键名)。

    探针 = ``(来源名, 值)``。来源名会打进报告（``.env:DB_PASSWORD`` 这样），
    **值不进任何输出**——这是调用方必须守住的纪律。
    """
    needles: list[tuple[str, str]] = []
    unreadable: list[str] = []
    dropped: list[str] = []
    for path in env_files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            unreadable.append(f"{path}（{type(exc).__name__}）")
            continue
        for key, value in _parse_env(text):
            if not value:
                continue
            if _SECRET_KEY_WORDS.search(key) and _is_real_value(value):
                needles.append((f"{path.name}:{key}", value))
            elif key in _COORD_KEYS and len(value) >= _MIN_IDENTITY_NEEDLE:
                needles.append((f"{path.name}:{key}", value))
    # 机器身份：家目录与主机名。用户名太短（4 字符）不当探针——「zbox」会撞出一片噪声。
    home = os.path.expanduser("~")
    if len(home) >= _MIN_IDENTITY_NEEDLE and home != "/":
        needles.append(("$HOME", home))
    hostname = os.uname().nodename
    if len(hostname) >= _MIN_IDENTITY_NEEDLE:
        needles.append(("hostname", hostname))
    # 去重（同一个值可能既是 DB_PASSWORD 又是别处的默认），保留第一个来源名。
    seen: dict[str, str] = {}
    for source, value in needles:
        seen.setdefault(value, source)
    kept: list[tuple[str, str]] = []
    for value, source in seen.items():
        if repo_root is not None and is_published(value, repo_root):
            dropped.append(source)
            continue
        kept.append((source, value))
    return sorted(kept, key=lambda x: x[0]), unreadable, sorted(dropped)


# ---------------------------------------------------------------------------
# 包形态（profile）：同一套检测器，判据按「这份包该长什么样」分叉
# ---------------------------------------------------------------------------
#
# 为什么要有这一层：通用便携包与实盘节点包**共用一份 staging**（实盘构建器往同一
# 份 staging 覆盖 start.bat / pack.env / bridge/ / live/ / 前端产物）。于是同一批
# 东西，在一份包里是「绝不许带的泄漏」，在另一份包里是「少一个就是残包」：
#
#   pack.env / bridge/** / live/** / README-LIVE.md / dsh/** —— 通用包：违规；
#                                                              实盘包：必备。
#   web/assets/LiveTradingPage*                              —— 通用包：违规（不开源）；
#                                                              实盘包：必须有。
#
# **检测器不复制**（正则、宿主探针、明文口令都是同一份实现），分叉的只是判据清单。
# 两份清单各写一套禁止项 = 迟早分叉，这正是本闸门从一开始就要避免的形状。


@dataclass(frozen=True)
class ContentAssert:
    """内容级必备判据：``pattern`` 命中的文件里，**至少一个**要 ``check`` 通过。

    ``check`` 返回 None = 通过，返回字符串 = 违规原因（原因要写清「为什么会错」，
    不只是「不符合要求」）。
    """

    pattern: str
    check: Callable[[bytes], str | None]
    reason: str


def _flag_present(needle: bytes) -> Callable[[bytes], str | None]:
    def check(blob: bytes) -> str | None:
        return None if needle in blob else f"找不到构建期内联标记 {needle.decode()}"

    return check


def _bridge_daemon_disarmed(blob: bytes) -> str | None:
    """出厂桥配置里 ``sltp_daemon.enabled`` 必须**显式**写成 false。

    桥自带 StopLossDaemon 是个独立卖出者（5s 轮询、触发即市价卖出），而守护单已由
    QuantMind 的 sltp_executor 承担 —— 同一账户同一持仓，两边同时触发就是超卖。
    2026-09-23 翻转了两处**代码**默认值，漏了出厂 config.yaml（它写着 true，压过
    默认值），节点起来桥就在自己卖。

    判据要求**显式**，不接受「靠代码默认值兜」：这个文件的上一版就是显式写错值。
    """
    in_section = False
    for raw in blob.decode("utf-8", "replace").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if len(line) - len(line.lstrip()) == 0:
            in_section = stripped == "sltp_daemon:"
            continue
        if in_section and stripped.startswith("enabled:"):
            value = stripped.split(":", 1)[1].strip()
            if value == "false":
                return None
            return (
                f"sltp_daemon.enabled = {value}（必须显式 false：桥会自己市价卖出，"
                "与 QuantMind 的 sltp_executor 对同一持仓重复卖）"
            )
    return "找不到 sltp_daemon.enabled（不许靠代码默认值兜：出厂配置必须显式关）"


@dataclass(frozen=True)
class Profile:
    """一份包的判据全集。``GENERAL``（出厂通用包）与 ``LIVE``（实盘节点包）各一份。"""

    name: str
    excludes: tuple[Exclude, ...]
    required_files: tuple[str, ...]
    required_globs: tuple[tuple[str, str], ...]
    required_pairs: tuple[tuple[str, str, str], ...]
    optional_components: tuple[tuple[str, str], ...]
    #: 禁止的构建期内联标记（``web/`` 里的二进制串匹配）。
    shape_markers: tuple[tuple[bytes, str], ...]
    #: 默认禁止的私有栏目产物（``--allow-local-live`` 可显式放行）。
    private_chunks: tuple[tuple[str, str], ...]
    #: 必备的栏目产物：``pattern`` 至少命中 1 个文件，否则违规（通用/实盘正好相反）。
    required_chunks: tuple[tuple[str, str], ...] = ()
    #: 内容级必备判据（见 :class:`ContentAssert`）。
    required_content: tuple[ContentAssert, ...] = ()
    #: **有意随包分发**的内网坐标：``pattern`` 命中的文件里出现内网地址只记为提示
    #: （报告里会点名），其它文件里出现仍然是违规。理由必填——「为什么这个文件可以
    #: 带坐标」比「允许它带」重要，下一个人要知道这是决定而不是疏忽。
    coord_exempt: tuple[tuple[str, str], ...] = ()
    #: 有意随包分发的宿主探针值：``(文件pattern, 探针来源后缀, 理由)``。
    #: 例：``("pack.env", ".env:DB_PASSWORD", …)`` —— 节点就是要用这个口令连库。
    needle_exempt: tuple[tuple[str, str, str], ...] = ()
    #: 只存在于产物里、staging 阶段不校验的必备文件（构建器写进 zip 的清单）。
    artifact_only_required: tuple[str, ...] = ()
    #: **先于排除清单判定**的保留路径：命中即不排除（但仍照常过内容判据与探针）。
    #: 用于「某条排除规则在这份包里有一条合法的例外」——例：实盘包的 dsh 载荷自带
    #: ``node_modules``（运行时要它），而通用清单把 ``**/node_modules/**`` 全排除。
    #: 写得**尽量窄**：宽到 ``dsh/**`` 就会把 ``.credentials*`` 那类安全排除也一起放行。
    keep_globs: tuple[tuple[str, str], ...] = ()

    def match_excludes(self, rel: str) -> Exclude | None:
        for pattern, _reason in self.keep_globs:
            if matches_pattern(pattern, rel):
                return None
        return match_excludes(rel, self.excludes)

    def keep_reason(self, rel: str) -> str | None:
        for pattern, reason in self.keep_globs:
            if matches_pattern(pattern, rel):
                return reason
        return None

    def coord_reason(self, rel: str) -> str | None:
        for pattern, reason in self.coord_exempt:
            if matches_pattern(pattern, rel):
                return reason
        return None

    def needle_reason(self, rel: str, source: str) -> str | None:
        for pattern, suffix, reason in self.needle_exempt:
            if matches_pattern(pattern, rel) and source.endswith(suffix):
                return reason
        return None


#: 实盘构建器**有意**随包分发的那几样：通用包按泄漏拦，实盘包按必备管。
#: 这份名单就是 :data:`EXCLUDES` 第 4 节那 8 条的一一对应物。
LIVE_KEEPS = frozenset(
    {
        "pack.env",
        "bridge/**",
        "live/**",
        "README-LIVE.md",
        "CHECKLIST.md",
        "quantbot_front.py",
        "start-quantbot.bat",
        "dsh/**",
    }
)

#: 实盘节点裁掉的整块内容（README-LIVE.md 8.3）。出现在产物里 = 瘦身没生效：
#: 包体白涨（qwenpaw 几百 MB、huntly 240MB+、训练环境 156MB 字体），还把运营者的
#: 工具链一起发出去。判据在**产物**侧，所以打包时真的漏了会被拦下。
LIVE_TRIMMED: tuple[Exclude, ...] = (
    Exclude("qwenpaw_runtime/**", "实盘节点裁掉：AI 编码助手与交易无关（8.3）", "all"),
    Exclude(
        "huntly/**",
        "实盘节点裁掉：新闻读 Ubuntu 那台（pack.env HUNTLY_BASE_URL）",
        "all",
    ),
    Exclude(
        "docker/training/**", "实盘节点裁掉：本机不训练（156MB 是 PDF 字体）", "all"
    ),
)

#: 实盘包**不是**通用包的子集：dsh 载荷（QuantBot 免 Docker）自带 ``node_modules``
#: ——Node 自带的 npm、global 包（express 那一族）、web profile 的依赖都在里面，
#: 运行时真的要用（``install_tools.bat`` 就是调它）。通用清单那条 ``**/node_modules/**``
#: 是为前端依赖树写的（实盘包的前端产物在 ``web/``，源码树根本不进包），照搬会把
#: QuantBot 拆成「页面在、点开报错」。**只放行这一条**：``dsh/**`` 下的
#: ``.credentials*`` / ``integrations/sessions/**`` 仍按安全排除拦（实测 2026-09-24：
#: 载荷里除 node_modules 外再没有别的命中项，34939 个文件全部来自这一条规则）。
LIVE_KEEP_GLOBS: tuple[tuple[str, str], ...] = (
    (
        "dsh/**/node_modules/**",
        "QuantBot 载荷的运行期依赖树（内嵌 Node/npm + 网关的 global 包）",
    ),
)

#: 通用便携包的 ``VERSION`` 是**那份包**的清单（``pack=QuantMind-Portable-win-x64``）。
#: 实盘包复用同一份 staging，会在 zip 里同时出现两份互相矛盾的清单 —— 出问题时
#: 第一眼就会看错。实盘包只留自己的 ``VERSION-LIVE``（本项在 zip 侧校验）。
LIVE_DROPS: tuple[Exclude, ...] = (
    Exclude(
        "VERSION",
        "通用便携包的清单（身份写的是 Portable）；实盘包的清单是 VERSION-LIVE",
        "all",
    ),
)

LIVE_EXCLUDES: tuple[Exclude, ...] = (
    tuple(r for r in EXCLUDES if r.pattern not in LIVE_KEEPS)
    + LIVE_TRIMMED
    + LIVE_DROPS
)

LIVE_REQUIRED_FILES: tuple[str, ...] = (
    "start.bat",
    "stop.bat",
    "install.bat",
    "install.ps1",
    "pack.env",
    "README-LIVE.md",
    "CHECKLIST.md",
    "start-quantbot.bat",
    "quantbot_front.py",
    "backend/main_oss.py",
    "web/index.html",
    "data/stocks/stocks_index.json",
    "runtime/python/python.exe",
    "redis/redis-server.exe",
    "bridge/tdx/main.py",
    "bridge/tdx/config.yaml",
    "pg_setup.py",
)

LIVE_REQUIRED_GLOBS: tuple[tuple[str, str], ...] = (
    ("data/upgrade_*.sql", "增量升级 SQL（缺了增量迁移永不执行）"),
    ("live/*.py", "实盘节点的导入脚本（缺了 live\\*.bat 全是死链）"),
)

#: 半个 QuantBot 是最坏形态：页面入口在、点开才报错。
LIVE_REQUIRED_PAIRS: tuple[tuple[str, str, str], ...] = (
    (
        "dsh/dsh.cordis.yml",
        "dsh/node/node.exe",
        "QuantBot 载荷有入口没有内嵌 Node（页面点开即报错）",
    ),
    (
        "dsh/dsh.cordis.yml",
        "dsh/global/node_modules/express/package.json",
        "载荷的 node 依赖树被整棵排掉了（QuantBot 起不来；见 LIVE_KEEP_GLOBS）",
    ),
)

LIVE_REQUIRED_CONTENT: tuple[ContentAssert, ...] = (
    ContentAssert(
        "web/assets/*.js",
        _flag_present(b'VITE_LIVE_NODE_ONLY:"true"'),
        "前端必须是实盘节点形态：漏传 VITE_LIVE_NODE_ONLY=true 的产物是全栏目形态，"
        "大盘分析/回测/训练那些栏目在这台机器上点开全是空的",
    ),
    ContentAssert(
        "web/assets/*.js",
        _flag_present(b'VITE_ENABLE_REAL_TRADING:"true"'),
        "前端实盘开关必须是 true：否则整页兜底成「实盘未启用」，这一包就白打了",
    ),
    ContentAssert(
        "bridge/tdx/config.yaml",
        _bridge_daemon_disarmed,
        "桥自带止损 daemon 必须关（见 _bridge_daemon_disarmed 的说明）",
    ),
)

#: 有意随包分发的内网坐标：这台节点存在的意义就是去连那台机器。
LIVE_COORD_EXEMPT: tuple[tuple[str, str], ...] = (
    ("pack.env", "节点配置：库里那台的地址、SMB 共享路径（没它连不上）"),
    ("CHECKLIST.md", "首次部署清单：照着连那台机器做的步骤"),
    ("README-LIVE.md", "部署文档：拓扑与排障步骤"),
    ("live/**", "节点侧的导入/同步脚本：目标就是那台机器"),
)

PROFILES: dict[str, Profile] = {
    "general": Profile(
        name="general",
        excludes=EXCLUDES,
        required_files=REQUIRED_FILES,
        required_globs=REQUIRED_GLOBS,
        required_pairs=REQUIRED_PAIRS,
        optional_components=OPTIONAL_COMPONENTS,
        shape_markers=SHAPE_MARKERS,
        private_chunks=PRIVATE_CHUNKS,
    ),
    "live": Profile(
        name="live",
        excludes=LIVE_EXCLUDES,
        required_files=LIVE_REQUIRED_FILES,
        required_globs=LIVE_REQUIRED_GLOBS,
        required_pairs=LIVE_REQUIRED_PAIRS,
        optional_components=(
            (
                "dsh/dsh.cordis.yml",
                "QuantBot 载荷没注入：这一包没有 QuantBot 整栏"
                "（要带上先跑 deploy/live-win/linux/build_dsh_payload.sh）",
            ),
        ),
        shape_markers=(),
        private_chunks=(),
        required_chunks=(
            (
                "web/assets/LiveTradingPage*",
                "实盘栏目的前端产物——整包的意义就是它（源码在 "
                "electron/src/features/local-live/，本机独有）",
            ),
        ),
        required_content=LIVE_REQUIRED_CONTENT,
        coord_exempt=LIVE_COORD_EXEMPT,
        artifact_only_required=("VERSION-LIVE",),
        keep_globs=LIVE_KEEP_GLOBS,
    ),
}
