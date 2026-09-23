"""公开仓里不得出现**运营者内网地址**与**明文口令字面量**（随包出厂的文本文件）。

背景
----
本仓是**公开仓库**（gitee + github）。2026-09-23 盘点发现 33 个已跟踪文件带 RFC1918
内网地址，其中 `docs/integrations/tdx-bridge.md` 带一条**明文 SMB 口令**的挂载命令，
另有 `admin123` 形态的数据库口令散在配置与 runbook 里。

这些字面量不是「鉴权根密钥」意义上的秘密，但它们是**坐标**：桥机 IP + 端口就是报单
入口的位置；而 git 历史把它们永久留在了任何能 clone 的人手里——**删掉 HEAD 里的一行
不会让历史里那一行消失**，所以防线只能建在「不写进去」这一侧。

本文件钉三件事
--------------
1. **随包出厂的文本里不得有内网地址**：出厂配置回退值 / 源码默认值 / 文档 / SKILL /
   前端 placeholder —— 一律用回环、RFC 5737 文档段或占位符。
2. **不得有明文口令字面量**：`password=<非占位值>`、`passwd=`、`token=` 后跟真实值。
   占位（`<...>`、`CHANGE_ME`、`${VAR}`、`os.getenv`）与空值放行。
3. **检测器本身必须能抓到植入的样本**：正则写错 / 扫描面塌缩时**不许全绿**
   （本仓「验收假通过」的老形状）。故有防空转用例，且它们扫的是**真实文件**。

与 `test_public_default_secrets.py` 的分工
------------------------------------------
那份管**鉴权根密钥**（拿它能冒充任意用户 → 必须进过滤器）；本份管**地址与口令坐标**
（拿它不能冒充用户，但没有它就连不上；且它暴露运营者的网络拓扑）。
两份都故意扫真实文件，不扫常量副本——否则只证明「我抄进测试里的清单和自己一致」。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

# ---------------------------------------------------------------------------
# 检测器
# ---------------------------------------------------------------------------
#: RFC1918 三段式。**注意各分支吃掉的八位组数不同**：`10` 吃 1 段、`192.168` 与
#: `172.16-31` 各吃 2 段——把它写成统一的 `(?:...)(?:\.\d+){3}` 会全都漏掉，本文件
#: 的开发过程里这个错犯了两次（第二次是 `192.168` 之后仍要求 3 段）。
_PRIVATE_IP = (
    r"(?:10(?:\.[0-9]{1,3}){3}"
    r"|(?:192\.168|172\.(?:1[6-9]|2[0-9]|3[01]))(?:\.[0-9]{1,3}){2})"
)

#: 前后不允许紧邻这些字符：挡掉版本号/包名里的「长得像 IP」的串
#: （`nvidia-curand-cu12==10.3.2.106`、`v10.3.2.1`）。八位组 ≤255 另有校验。
_IP_RE = re.compile(rf"(?<![0-9A-Za-z_=.\-])({_PRIVATE_IP})(?![0-9A-Za-z_\-.])")

#: 允许的地址：回环/未指定不构成拓扑；RFC 5737 三段是**保留给文档**的示例地址
#: （`192.0.2.0/24`、`198.51.100.0/24`、`203.0.113.0/24`），写进文档与默认值正合适。
_ALLOWED_PREFIXES = ("127.", "0.0.0.0", "192.0.2.", "198.51.100.", "203.0.113.")

#: **整段**私网块：`10.0.0.0/8`、`172.16.0.0/12`、`192.168.0.0/16` 覆盖整个私网空间，
#: 不指向任何一台机器——nginx 的 `allow 172.16.0.0/12;` 就是标准的内网放行写法。
#: 判据是「**具体到主机**才算坐标」：`172.20.0.0/16`（某个具体子网）照样要清。
_WHOLE_PRIVATE_BLOCKS = {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"}

#: 口令类键名。值侧只拦「看起来是真值」的：
#: 空、`${...}`/`$VAR`/`%VAR%`、`<...>` 占位、`os.getenv(`、全大写占位常量 一律放行。
_SECRET_KEY_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_$%{])"
    r"(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)\b"
    # 键名后的收尾引号要允许：JSON/YAML 形态是 `"password": "..."`，
    # 少了这个 `["']?` 会**整个漏掉** `db_user_001.json` 那种配置（开发时踩过）。
    r"[\"']?\s*"
    # 只认**赋值**：`:` 或 `=`。`==`/`!=`/`<=`/`>=`/`=>`（比较与箭头）都不是赋值，
    # 不排掉的话 `token === 'dev-admin-token'` 会被当成一次赋值（TS 里到处都是）。
    r"(?::|(?<![=!<>:])=(?!=|>))\s*"
    r"(?P<q>['\"]?)(?P<v>[^\s'\";,)]+)"
)

#: 占位/掩码标记（**小写比较**）。命中即放过——它们是「这里要填个值」的指示，
#: 不是值本身。
_PLACEHOLDER_MARKERS = (
    "<", ">", "${", "$(", "%", "...", "***", "change_me", "your_", "_here",
    "replace", "xxx", "os.getenv", "getenv(", "process.env",
)

#: 出现即判定「这是表达式/结构体，不是字面量」的字符：
#: `getAccessToken()`、`{ bg: 'blue' }`、`sshpass?.[1]`。
_EXPRESSION_CHARS = "(){}[]"

#: 掩码字符集：整串只有这些字符 = 是打码（`***`、`xxxx`、`...`），不是口令。
_MASK_CHARS = set("*xX.")


def _looks_like_a_literal(key: str, val: str, *, quoted: bool) -> bool:
    """这个值看起来是**写死的字面量**吗（而不是变量、表达式、掩码、说明文字）。

    检测器宁可漏一点也不能淹：一条会误报的护栏等于没有护栏——没人看得懂就该关掉它。
    下面每条「放过」都对应一类**真实出现过**的误报形态（括号里是出处）。
    """
    if any(ch in val for ch in _EXPRESSION_CHARS):
        return False  # `getAccessToken()` / `{ bg: ... }` / `this.config.apiKey`
    if val.startswith(("$", "%")):
        return False  # `$REPLY`、`${API_KEY:0:6}` 的尾段、`%VAR%` 都是变量
    if not any(ch.isalnum() for ch in val):
        return False  # 纯标点（模板字符串 `` ` ``、`===`）不是口令
    if re.search(r"[一-鿿]", val):
        return False  # 中文 = 文档里的说明文字（`"password": "Redis密码"`）
    if set(val) <= _MASK_CHARS:
        return False  # `***` / `xx` / `...` 是掩码
    if key.upper() == val.upper():
        return False  # `PASSWORD = "password"` 是字段名常量，不是口令
    if not quoted and re.fullmatch(r"[A-Za-z_]+", val):
        return False  # 裸的纯字母词 = 变量名（`password=db_password`）
    if not quoted and re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", val):
        return False  # 裸的点路径 = 取值（`values.password`、`tokenResponse.access_token`）
    return True

#: 豁免目录：不入库 / 非本仓产物 / vendored 上游。
#: **每条都在这里写明理由**——理由比禁止本身重要，下一个人要知道为什么它能留。
_SKIP_DIR_PARTS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "site-packages",
    # 本地运行数据与计划文档：`.gitignore` 覆盖，从不入库
    "local",
    # vendored 上游：与本仓的出厂配置无关，改动会被下次同步冲掉
    "rd-agent", "TradingAgents-astock", "minibt", "alphaagent",
    # 运行期日志（`logs/`、`backend/logs/`）：内容是**运行时输出**，不是出厂文本——
    # 里面天然会有密钥指纹、access_key 前缀、内网地址（一次排查就能写进去）。
    # `.gitignore` + `.dockerignore:50` 都排除，从未入库。
    "logs",
    # 构建产物目录：见 _EXEMPT_PATHS 的说明
    "build", "dist-react", "dist-electron", ".next",
    # 覆盖率 HTML 产物（155MB，逐行快照源码，跑一次 `pytest --cov` 就重生成一份）：
    # `.gitignore:109` 与 `.dockerignore:59` 双重排除——既不进仓也不进镜像。
    # 不排除的话，**每跑一次覆盖率就翻出一次历史源码里的内网地址**（2026-09-23 实测：
    # 容器里按目录遍历扫到 30+ 条，全是旧快照），护栏会永远红。
    "htmlcov",
}
_SKIP_FILE_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".pdf", ".zip", ".gz",
    ".tar", ".whl", ".so", ".dylib", ".pyd", ".pyc", ".woff", ".woff2", ".ttf",
    ".exe", ".dll", ".bin", ".db", ".sqlite", ".parquet", ".pyo", ".lock",
    ".log",  # 运行期日志：见 _SKIP_DIR_PARTS 里 "logs" 的理由
}

#: 扫哪些目录（相对仓库根）。**只列文本源码/配置/文档**——构建产物与 vendored 目录
#: 已在 _SKIP_DIR_PARTS / _EXEMPT_PATHS 里单独交代。
_SCAN_DIRS = (
    "backend", "config", "deploy", "docker", "docs", "electron/src",
    "skills", "scripts", "tools", ".claude/skills",
)
_SCAN_TOP_FILES = (".env.example", ".env.sample", "docker-compose.yml", "docker-compose.override.yml")

#: 路径级豁免（**子串命中**）：**带理由**。照 `test_public_default_secrets.py` 末尾的写法，
#: 「不覆盖什么、为什么不覆盖」要写在文件里，而不是靠读者猜。
_EXEMPT_PATH_PARTS: dict[str, str] = {
    # 测试夹具里的假地址：`192.168.1.9/10`、`10.0.0.1` 是构造出来的 mock 值，
    # 不指向任何真实主机（真实拓扑不会被测试用例用到）。
    "__tests__": "前端的 __tests__ 夹具（mock 地址与假口令）",
    # 运行期本地文件（root:600，`.gitignore:128`，从未入库）：装载机/管理台写它，
    # 容器模式下按目录遍历会被读到。**它不许入库**——同文件末尾
    # `test_exempt_paths_are_not_git_tracked` 盯着这一点。
    "config/runtime.env": "运行期本机配置（root:600，从不入库）",
}

#: `_EXEMPT_PATH_PARTS` 里属于「**从不入库**的本地文件」那一类：被 git 跟踪即豁免失效。
#: `__tests__` 刻意不在列——那些夹具本来就随源码入库，豁免理由是「值是假的」，不是
#: 「文件不该在」。两者混在一起会让本约束退化成「凡豁免皆不可跟踪」的假命题。
_LOCAL_ONLY_EXEMPT: tuple[str, ...] = ("config/runtime.env",)


def _is_test_file(rel: str) -> bool:
    name = Path(rel).name
    return (
        "/tests/" in f"/{rel}"
        or rel.startswith("backend/tests/")
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


def _root() -> Path:
    """定位仓库根（含 `.env.example` + `docker-compose.yml`）。找不到就失败。

    两条独立证据缺一不可：只有 `.env.example` 的目录可能是个深层的示例目录，
    只有 compose 的目录可能是个子项目。找不到 = 本护栏没得扫 = 必须红，
    静默跳过等于这条护栏不存在。
    """
    seen: list[Path] = []
    for base in (Path(__file__).resolve(), Path.cwd().resolve()):
        for p in (base, *base.parents):
            if p in seen:
                continue
            seen.append(p)
            if (p / ".env.example").is_file() and (p / "docker-compose.yml").is_file():
                return p
    raise AssertionError(
        "找不到仓库根（`.env.example` + `docker-compose.yml`）——本护栏无法执行。\n"
        f"已探过的路径：{[str(p) for p in seen[:8]]}"
    )


def _in_scan_scope(rel: str) -> bool:
    """这个相对路径属不属于「随包出厂的文本」扫描面。"""
    if rel in _SCAN_TOP_FILES:
        return True
    if Path(rel).suffix.lower() in _SKIP_FILE_SUFFIXES:
        return False
    if set(Path(rel).parts) & _SKIP_DIR_PARTS:
        return False
    if _is_test_file(rel):
        return False
    if any(part in rel for part in _EXEMPT_PATH_PARTS):
        return False
    return any(rel == d or rel.startswith(d + "/") for d in _SCAN_DIRS)


def _tracked_files(root: Path) -> list[str] | None:
    """`git ls-files` 的相对路径清单；无 git（容器里）返回 None。

    **扫已跟踪文件，而不是文件系统**：本护栏问的是「公开仓里有什么」，而
    `config/runtime.env` 这类 root:600 的**运行期**文件就躺在 `config/` 里
    （`.gitignore:128` 明确忽略）——按目录扫会把它读进来（开发时实测 PermissionError，
    两条正文用例当场变成「读不到文件」而不是「扫出违规」）。
    """
    if not (root / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return [p for p in proc.stdout.split("\0") if p]


def _iter_shipped_text_files() -> list[tuple[str, Path]]:
    """(相对路径, 绝对路径) —— 随包出厂的文本文件清单。"""
    root = _root()
    out: list[tuple[str, Path]] = []
    tracked = _tracked_files(root)
    if tracked is not None:
        for rel in sorted(tracked):
            if not _in_scan_scope(rel):
                continue
            p = root / rel
            if p.is_file():
                out.append((rel, p))
        return out
    # 容器里没有 .git：退回目录遍历（挂载进来的子集）
    for top in _SCAN_TOP_FILES:
        p = root / top
        if p.is_file():
            out.append((top, p))
    for d in _SCAN_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            rel = str(p.relative_to(root))
            if not _in_scan_scope(rel):
                continue
            out.append((rel, p))
    return out


def find_internal_addresses(text: str) -> list[tuple[int, str, str]]:
    """返回 (行号, 地址, 该行内容) —— 去掉允许段与非法八位组。"""
    hits: list[tuple[int, str, str]] = []
    for i, line in enumerate(text.splitlines(), 1):
        for m in _IP_RE.finditer(line):
            addr = m.group(1)
            if any(addr.startswith(pfx) for pfx in _ALLOWED_PREFIXES):
                continue
            if not all(0 <= int(o) <= 255 for o in addr.split(".")):
                continue
            # 紧跟其后的 `/NN` 若正好是整段私网块，说明这是「整个私网」的写法而非某台机器
            cidr = re.match(r"/(\d{1,2})(?!\d)", line[m.end():])
            if cidr and f"{addr}/{cidr.group(1)}" in _WHOLE_PRIVATE_BLOCKS:
                continue
            hits.append((i, addr, line.strip()))
    return hits


#: 「代码文件」：这些后缀按源码规则看（裸值一律当变量/取值）。
_CODE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".html"}


def find_plaintext_secrets(text: str, *, code: bool = False) -> list[tuple[int, str, str]]:
    """返回 (行号, 键名, 该行内容) —— 值是「看起来像写死的口令」的才算。

    ``code=True``（源码/前端后缀）时**只认带引号的字面量**：源码里
    `const token = getAccessToken()`、`password: values.password` 这类是变量与取值，
    实测照单全收会把整条护栏淹掉（TS 一个仓就是 150+ 条）。非代码文件（配置/文档/
    shell）里裸值也算，`_looks_like_a_literal()` 再把变量名与表达式剔掉——
    **含数字的照抓**（`password=95195`、`password=admin123`）。
    """
    hits: list[tuple[int, str, str]] = []
    for i, line in enumerate(text.splitlines(), 1):
        for m in _SECRET_KEY_RE.finditer(line):
            val = m.group("v").strip()
            quoted = bool(m.group("q"))
            if not val:
                continue
            if any(mk in val.lower() for mk in _PLACEHOLDER_MARKERS):
                continue
            if val.isupper() and "_" in val:  # 形如 CHANGE_ME_TOKEN 的占位常量
                continue
            if code and not quoted:
                continue  # 源码里裸值一律当变量/取值
            if not _looks_like_a_literal(m.group(1), val, quoted=quoted):
                continue
            hits.append((i, m.group(1).lower(), line.strip()))
    return hits


# ---------------------------------------------------------------------------
# 1) 防空转：检测器与扫描面都必须真的工作
# ---------------------------------------------------------------------------


def test_detector_catches_planted_literals() -> None:
    """正则必须抓到真实形态，且不误伤版本号——否则整条护栏是空转的。"""
    should_hit = [
        "TDX_BRIDGE_URL=http://192.168.31.31:8550",
        'url: "http://10.75.220.251:6379"',
        "host: 172.20.0.20",
        "mount -t cifs //192.168.31.22/PYPlugins",
    ]
    for s in should_hit:
        assert find_internal_addresses(s), f"检测器漏掉了真实形态：{s}"

    should_miss = [
        "nvidia-curand-cu12==10.3.2.106",       # 版本号
        "cu12==10.3.2.106",
        "http://127.0.0.1:8000",                # 回环不是拓扑
        "http://192.0.2.10:8550",               # RFC 5737 文档段
        "http://198.51.100.7", "http://203.0.113.9",
        "0.0.0.0:8550",
        "release-2026.09.23.1",                 # 四段版本号
        "999.168.31.39",                        # 八位组越界
        "allow 172.16.0.0/12;",                 # 整段私网（nginx 标准写法）
        "host all all 10.0.0.0/8 trust",
        "192.168.0.0/16",                       # 整段私网
    ]
    for s in should_miss:
        assert not find_internal_addresses(s), f"误报：{s}"

    # 「整段放过」不能变成「带斜杠就放过」：具体子网仍旧是坐标
    for s in ("host all all 172.20.0.0/16 scram-sha-256", "10.0.0.0/16", "192.168.31.0/24"):
        assert find_internal_addresses(s), f"具体子网被放过：{s}"


def test_secret_detector_catches_planted_literals() -> None:
    assert find_plaintext_secrets('password: "admin123"')
    assert find_plaintext_secrets("mount -o username=esxi,password=95195")
    assert find_plaintext_secrets('"password": "hunter2"')
    assert find_plaintext_secrets('token: "8bbe5347f916982abc4f0620ab0d0b50"')  # 桥 token 形态
    assert find_plaintext_secrets("postgresql://u:7f3a9c2e8b1d4f60@h/db".replace(
        "postgresql://u", "password"))  # 非代码文件里裸值也抓

    assert not find_plaintext_secrets('password: ""')
    assert not find_plaintext_secrets('password: "${DB_PASSWORD}"')
    assert not find_plaintext_secrets('password="<从 Secret Manager 读取>"')
    assert not find_plaintext_secrets('password: "replace-with-prod-db-password"')
    assert not find_plaintext_secrets("password=os.getenv('DB_PASSWORD')")
    assert not find_plaintext_secrets("password=db_password")   # 变量名不是字面量
    assert not find_plaintext_secrets('"password": "email_password_here"')
    assert not find_plaintext_secrets('"api_key": "qdb_your_key_here"')
    assert not find_plaintext_secrets('"access_key": "ak_...", "secret_key": "sk_..."')
    assert not find_plaintext_secrets('"password": "Redis密码"')   # 文档里的说明文字
    assert not find_plaintext_secrets('PASSWORD = "password"')     # 字段名常量
    assert not find_plaintext_secrets('MASKED="${API_KEY:0:6}…"')
    assert not find_plaintext_secrets('API_KEY="$REPLY"')
    assert not find_plaintext_secrets('let msg = `已生成 ${n} 个 token:`;')  # 模板串尾段

    # ── 源码里到处都是的**取值**形态：一条都不许抓，否则护栏会被淹掉而被人关掉。
    for src_line in (
        "const token = authService.getAccessToken();",
        "const token = localStorage.getItem('access_token') || '';",
        "password: values.password,",
        "token: tokenResponse.access_token,",
        "state.token = action.payload.token;",
        "payload.token = tokenInput.trim();",
        "if (token === 'dev-admin-token' || token.split('.').length !== 3) {",
        "apiKey: process.env.BINANCE_API_KEY || '',",
        "const password = sshpass?.[1] || labeled?.[1];",
        "password: { bg: 'bg-blue-50', color: 'text-blue-600' },",
        "wsUrl.replace(/token=[^&]+/, 'token=***')",
        "'X-MBX-APIKEY': this.config.apiKey",
    ):
        assert not find_plaintext_secrets(src_line, code=True), f"源码形态被误报：{src_line}"


def test_scan_reaches_the_shipped_files() -> None:
    """扫描面必须真的覆盖到出厂配置与源码——不然上面两条全绿也没意义。"""
    files = _iter_shipped_text_files()
    rels = {rel for rel, _ in files}
    for must in (".env.example", "docker-compose.yml", "backend/config/settings.py"):
        assert must in rels, f"扫描面漏了出厂文件：{must}（改名了？把新名字接进来）"
    assert len(files) >= 200, (
        f"只扫到 {len(files)} 个文件——扫描面塌缩了（目录改名/被整体跳过），本护栏形同虚设"
    )
    # 测试文件本身必须被排除（否则 mock 地址会让本文件永远红）
    assert not any(_is_test_file(rel) for rel, _ in files)
    # 宿主机上必须走「已跟踪文件」而不是目录遍历：`config/runtime.env` 是 gitignore 的
    # root:600 运行期文件，按目录扫会把它读进来（实测 PermissionError）。
    if (_root() / ".git").exists() and (_root() / "config" / "runtime.env").is_file():
        assert "config/runtime.env" not in rels, (
            "扫描面把**未跟踪**的运行期密钥文件读进来了——`_tracked_files()` 没生效"
        )


def test_exempt_paths_are_not_git_tracked() -> None:
    """豁免只对「不该入库的本地文件」成立：一旦它被跟踪，豁免就是在遮真漏。"""
    tracked = _tracked_files(_root())
    if tracked is None:
        # 容器里只挂了仓库的一个子集、没有 .git：「是否已跟踪」无从判断，
        # 这一层由宿主机（有 .git）上的本用例守。**不是**「跳过所以永远绿」——
        # 上面那两条正文用例在容器里跑的是目录遍历版本，口径更宽。
        return
    for pattern in _LOCAL_ONLY_EXEMPT:
        assert pattern in _EXEMPT_PATH_PARTS, (
            f"`{pattern}` 不在豁免名单里——两处清单脱节了（改了名只改一处？）"
        )
        leaked = [rel for rel in tracked if pattern in rel]
        assert not leaked, (
            f"`{pattern}` 已在豁免名单里（理由：{_EXEMPT_PATH_PARTS[pattern]}），"
            f"却被 git 跟踪了：{leaked[:5]}——豁免立刻失效，请把它移出仓库或删掉豁免条目"
        )


def test_dockerignore_excludes_local_credentials() -> None:
    """`.gitignore` 只管 git；**镜像由 `.dockerignore` 决定**。

    2026-09-23 实测：`docker/Dockerfile.oss` 的两行 `COPY backend backend` /
    `COPY config config` 把 `backend/config/users/*.json`（账户口令密文）与
    `config/runtime.env`（root:600 运行期配置）原样烘进了 `quantmind-oss:latest`。
    这两条排除一旦被删，同样的东西会再进一次镜像。
    """
    root = _root()
    dockerignore = root / ".dockerignore"
    if not dockerignore.is_file() and _tracked_files(root) is None:
        # 容器里既没挂 `.dockerignore` 也没 .git（只挂了仓库子集）：读不到 ≠ 不存在，
        # 这层由宿主机守。宿主机上文件不见了才是真事故（镜像会把 data/ 一起收进去）。
        return
    assert dockerignore.is_file(), "`.dockerignore` 不见了——镜像里会连 `data/` 一起进"
    lines = {
        line.strip()
        for line in dockerignore.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    for must in ("backend/config/users/", "config/runtime.env"):
        assert must in lines, (
            f"`.dockerignore` 少了 `{must}`：它会被 `COPY` 烘进镜像"
            f"（凭据/运行期配置不该随镜像出厂）"
        )


# ---------------------------------------------------------------------------
# 2) 正文：随包出厂的文本里不得有内网地址 / 明文口令
# ---------------------------------------------------------------------------


def test_no_internal_addresses_in_shipped_text() -> None:
    offenders: list[str] = []
    for rel, path in _iter_shipped_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # 二进制（扩展名没覆盖到的）不参与
        for lineno, addr, line in find_internal_addresses(text):
            offenders.append(f"{rel}:{lineno}  [{addr}]  {line[:110]}")
    assert not offenders, (
        "这些随包出厂的文本里有**内网地址字面量**——公开仓里它就是运营者的网络坐标，"
        "且会永久留在 git 历史里：\n  "
        + "\n  ".join(offenders)
        + "\n改法：回环 `127.0.0.1`、RFC 5737 文档段（`192.0.2.x` 等）或 `<占位>`；"
        "真值一律走环境变量 / 运行时配置。"
    )


def test_no_plaintext_secrets_in_shipped_text() -> None:
    offenders: list[str] = []
    for rel, path in _iter_shipped_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, key, line in find_plaintext_secrets(text, code=path.suffix.lower() in _CODE_SUFFIXES):
            offenders.append(f"{rel}:{lineno}  [{key}]  {line[:110]}")
    assert not offenders, (
        "这些随包出厂的文本里有**明文口令字面量**：\n  "
        + "\n  ".join(offenders)
        + "\n改法：留空、`${ENV_VAR}` 或 `<占位>`；真值放 `.env`（仓外）或密钥管理器。"
    )


# ---------------------------------------------------------------------------
# 说明：本文件**不**覆盖的构建产物（与 `test_public_default_secrets.py` 末尾同格式）
# ---------------------------------------------------------------------------
# `web/assets/**`（29MB 遗留前端产物）与 `deploy/portable/dist/*.zip`（历史发行包）
# 里**仍然带着**旧的内网地址——它们是从 `electron/src/**` 构建出来的二进制/压缩形态，
# 只能重建或删除，不能「改一行」。当前处置：
#   * 源头（`electron/src/**`）已清洗，**下次重建**后产物即干净；
#   * 这两个目录由 `deploy/portable/build_*.sh` 与 `deploy/update.sh` 消费，
#     删不删是运维决策（删了历史包就取不回来了），故未纳入本文件的扫描面；
#   * 历史 zips 无论删不删都已被 clone 过——**轮换才是唯一有效动作**。
