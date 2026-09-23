"""P3-② 隔壁（quant-Trader）资产落盘：选择规则 / 凭据闸门 / 清单 / 复验。

**要解的题**：切换停机后隔壁就停了，它的知识资产（Pine 公式源、事件 parquet、LLM 决策
审计、复盘与日报历史）与**待转换的活跃状态**（守护计划、风控档位、盘前探针……）必须先
落到本仓能看见的地方，且要**可复验**——60MB 搬完没有清单，日后没人能回答「搬全了吗、
和源逐字节一致吗、有没有把密钥一起搬进来」。

三层纪律
--------

1. **只搬白名单**（:data:`CATEGORIES`）：每个类别是显式的 glob 表。没有 include 规则
   的文件**一律不搬**——``.env`` / ``.service.env`` / ``config/``（券商私钥）/
   ``dsh/`` / ``.venv`` 这些从不进入任何 include。
2. **两道凭据闸门**（纵深防御，闸在**落地字节**上而不是源路径上）：
   * 路径闸（:data:`DENY_NAME_PATTERNS`）：名字就带 token/key/secret/credentials 的一律
     拒绝，哪怕被某个 glob 误收；
   * 内容闸：硬形状（``sk-`` / ``ghp_`` / ``AKIA`` / PEM 私钥 / Bearer JWT）**不落盘**，
     该文件跳过并计入「要人看一眼」；软形状（``"api_key": "…"`` 这类字段名）**落但告警**
     ——Pine 公式源与 webhook 模板里天然有 ``"token"`` 字面量，一刀切会把知识资产全挡在门外。
   * ``configs/**`` 走 **脱敏转换**（:func:`redact_json`）而不是原样搬：配置本身要留档给
     P5 转换用，密钥字段值换成 ``<redacted>`` 并记进 ``transforms``。
3. **清单 + 复验**：:func:`apply_plan` 落 ``MANIFEST.jsonl``（逐文件源/落地两个 sha256、
   体积、mtime、类别、转换、告警）+ ``MANIFEST.sha256``（清单自身校验）+ ``REPORT.md``；
   :func:`verify` 反向复算：清单完整性、逐文件哈希与体积、**缺失 / 多出 / 被改**、
   可选的源侧重比（``--source-check``）。多出的文件也要报——落地区里多一个没人认领的
   文件，和少一个一样可疑。

**清单是确定性的**：同一份源 → ``MANIFEST.jsonl`` / ``REPORT.md`` 逐字节相同
（无时间戳；时间在文件 mtime 上），所以「搬两遍」可以拿来证明幂等。

**本模块只读源**：不删、不改、不动源目录里任何东西（复验也只读）。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple

#: 清单文件名（落在地点根，不在 ``archive/`` 里）
MANIFEST_NAME = "MANIFEST.jsonl"
#: 清单自身的校验文件
MANIFEST_SEAL = "MANIFEST.sha256"
#: 人读报告
REPORT_NAME = "REPORT.md"
#: 文件本体所在子目录
ARCHIVE_DIR = "archive"

#: 哈希分块（大 parquet 不要一次性读进内存）
_CHUNK = 1 << 20


class Category(NamedTuple):
    """一个搬运类别：``include`` 是相对源根的 glob 表，``transform`` 见 :data:`TRANSFORMS`。"""

    name: str
    include: tuple[str, ...]
    transform: str = "copy"


#: 可用转换：``copy`` 逐字节照搬；``redact-json`` 解析 JSON 并把凭据字段值打码
TRANSFORMS = ("copy", "redact-json")

#: 白名单类别。**顺序即优先级**：先匹配到的类别赢（同一文件不会被搬两次）。
CATEGORIES: tuple[Category, ...] = (
    Category(
        "ledger",
        (
            "logs/live_ledger.json",
            "logs/live_trade_*.jsonl",
            "logs/live_watch_*.jsonl",
            "logs/live_equity.jsonl",
            "logs/live_roundtrips.jsonl",
            "logs/ghost_ledger.jsonl",
        ),
    ),
    Category(
        "state",
        (
            "logs/budget/state.json",
            "logs/live_analysis_state.json",
            "logs/alert_push_state.json",
            "logs/sentiment_zt.jsonl",
            "logs/premarket_probe_state.json",
            "logs/rt_status.json",
            "logs/live_analysis_round.json",
            "logs/live_llm_trade_state.json",
            "logs/bridge_scan.json",
            "logs/preflight.json",
            "data/live_watch.json",
            "data/live_watch_halt.json",
            "data/live_pending_orders.json",
            "data/live_order_events.json",
            "data/live_order_outcomes.json",
            "data/l2_state.json",
            "data/l2_factors_live.json",
            "data/l2_status.json",
            "data/market_snapshot.json",
            "data/agent_last_decisions.json",
        ),
    ),
    Category("decisions", ("data/agent_data_astock/**",)),
    Category(
        "history",
        (
            "logs/review/**",
            "logs/night_pool/**",
            "logs/daily_report/**",
            "logs/decision_pool.jsonl",
            "logs/live_equity.jsonl.bak-*",
        ),
    ),
    Category(
        "knowledge",
        (
            "data/pine_library/source/**",
            "data/pine_library/index.json",
            "data/pine_library/chat/**",
            "data/pine_library/pine_audit/**",
        ),
    ),
    Category("events", ("data/events/*.parquet",)),
    Category(
        "exclusions",
        (
            "data/risk_block.json",
            "data/fundamental_flags.json",
            "data/news_blacklist_2026.json",
            "data/长期排除清单_*",
        ),
    ),
    Category(
        "research",
        (
            "data/lab_batch/**",
            "data/agent_data/market_memory.md",
            "data/agent_data_hk/market_memory.md",
        ),
    ),
    # 配置分两支：JSON 走脱敏（里面有 ``models.openai_api_key`` 这类真密钥），
    # 同目录下的说明文档与补丁脚本（``configs/patches/*.py``）按文本照搬——
    # 一条 ``configs/**`` 全走 redact-json 会把这些非 JSON 判成「读不懂 ⇒ 阻断」
    # （真源实测 8 只），把整批卡在出口。
    Category("configs", ("configs/**/*.json",), transform="redact-json"),
    Category("configs-src", ("configs/**",)),
)

#: 路径闸：名字命中即拒绝（``fnmatch`` 风格，比对 basename 与整条相对路径）。
#: 命中是**预期行为**不是故障（``logs/qqbot_token.json`` 本来就不该搬），故只计数并列出。
DENY_NAME_PATTERNS: tuple[str, ...] = (
    "*.env",
    ".env*",
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "*credential*",
    "*secret*",
    "*token*.json",
)

#: 内容闸·硬形状：命中即**不落盘**（这些不是「可能是密钥」，是「就是密钥」）。
HARD_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("github-token", re.compile(r"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}")),
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer-jwt", re.compile(r"(?i)\bbearer\s+eyJ[A-Za-z0-9_\-. ]{10,}")),
)

#: 内容闸·软形状：命中**照搬但告警**（Pine/webhook 模板里的字段名天然长这样）。
SOFT_SECRET_RE = re.compile(
    r'(?i)"(api[_-]?key|apikey|secret|token|password|passwd|authorization)"\s*:\s*"[^"]{8,}"'
)

#: ``redact-json`` 要打码的字段名（大小写不敏感，匹配**键名**）。
#: 前缀可有可无：隔壁实测字段名是 ``models.openai_api_key``——只认裸 ``api_key`` 会把
#: 真实密钥原样搬走（首版就是这么漏的，被 ``test_no_secret_shaped_bytes_survive…`` 抓住）。
REDACT_KEY_RE = re.compile(
    r"(?i)^(?:.*[_-])?(?:api[_-]?key|apikey|access[_-]?key|secret[_-]?key"
    r"|private[_-]?key|secret|token|password|passwd|credentials?|authorization|bearer|key)$"
)

#: 打码后的占位值（单一写法，复验时一眼能认出）
REDACTED = "<redacted>"


@dataclass(frozen=True)
class PlannedFile:
    """一只待落盘的文件（``landed_*`` 是**落地后**的字节口径，转换已应用）。"""

    path: str
    category: str
    size: int
    source_sha256: str
    landed_sha256: str
    source_mtime: str
    transforms: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def record(self) -> dict[str, Any]:
        """清单里的一行（字段顺序固定 = JSONL 确定性）。"""
        return {
            "path": self.path,
            "category": self.category,
            "size": self.size,
            "source_sha256": self.source_sha256,
            "landed_sha256": self.landed_sha256,
            "source_mtime": self.source_mtime,
            "transforms": list(self.transforms),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class SkippedFile:
    """被闸门拦下的文件（**必须留下痕迹**，否则「为什么这个文件没搬」永远答不上来）。"""

    path: str
    reason: str

    def record(self) -> dict[str, str]:
        return {"path": self.path, "reason": self.reason}


@dataclass
class LegacyPlan:
    """一次搬运的完整计划：搬什么、跳什么、要人看什么。"""

    source_root: Path
    files: list[PlannedFile] = field(default_factory=list)
    skipped: list[SkippedFile] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def warnings(self) -> list[str]:
        """逐文件的软告警（软形状命中，落盘但要人看）。"""
        return [f"{f.path}: {w}" for f in self.files for w in f.warnings]

    @property
    def categories(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.files:
            out[f.category] = out.get(f.category, 0) + 1
        return out

    @property
    def ok(self) -> bool:
        """干净 = 无阻断问题、无跳过、无告警。"""
        return not self.problems and not self.skipped and not self.warnings

    def as_doc(self) -> dict[str, Any]:
        return {
            "source_root": str(self.source_root),
            "files": [f.record() for f in self.files],
            "skipped": [s.record() for s in self.skipped],
            "problems": list(self.problems),
            "warnings": self.warnings,
        }


# --- 选择与读取 -------------------------------------------------------------


def _rel_posix(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat()


def _denied(rel: str) -> str | None:
    """路径闸：返回命中的模式（``None`` = 放行）。"""
    from fnmatch import fnmatch

    name = PurePosixPath(rel).name
    for pat in DENY_NAME_PATTERNS:
        if fnmatch(name, pat) or fnmatch(rel, pat):
            return pat
    return None


def redact_json(raw: bytes) -> tuple[bytes, tuple[str, ...]]:
    """把 JSON 里的凭据字段值换成 :data:`REDACTED`，返回（新字节, 打码键路径）。

    非 JSON / 非法 UTF-8 会抛 :class:`ValueError`——由调用方转成阻断问题：**宁可整份
    不搬，也不要把一份读不懂的配置当成已脱敏搬走**（读不懂就无法保证里面没有密钥）。
    """
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"不是合法 UTF-8 JSON：{exc}") from exc
    keys: list[str] = []

    def walk(node: Any, prefix: str) -> Any:
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for key, val in node.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                if isinstance(key, str) and REDACT_KEY_RE.match(key) and val:
                    out[key] = REDACTED
                    keys.append(path)
                else:
                    out[key] = walk(val, path)
            return out
        if isinstance(node, list):
            return [walk(v, f"{prefix}[{i}]") for i, v in enumerate(node)]
        return node

    masked = walk(doc, "")
    return (
        json.dumps(masked, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        + b"\n",
        tuple(keys),
    )


def landed_bytes(path: Path, transform: str) -> tuple[bytes, tuple[str, ...]]:
    """按转换算出**将要落盘**的字节。``redact-json`` 的第二个返回值是打码键路径。"""
    raw = path.read_bytes()
    if transform == "copy":
        return raw, ()
    if transform == "redact-json":
        return redact_json(raw)
    raise ValueError(f"未知转换：{transform!r}（可用：{TRANSFORMS}）")


def hard_secret_hits(data: bytes) -> tuple[str, ...]:
    """内容闸·硬形状：返回命中的模式名（去重保序）。二进制文件按 latin-1 粗扫。"""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    hits: list[str] = []
    for name, rx in HARD_SECRET_PATTERNS:
        if rx.search(text) and name not in hits:
            hits.append(name)
    return tuple(hits)


def soft_secret_hits(data: bytes) -> tuple[str, ...]:
    """内容闸·软形状（只告警）。二进制文件不参与（parquet 里的字节不是「字段名」）。"""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ()
    return ("secret-shaped-field",) if SOFT_SECRET_RE.search(text) else ()


def _category_of(rel: str) -> Category | None:
    from fnmatch import fnmatch

    for cat in CATEGORIES:
        for pat in cat.include:
            if fnmatch(rel, pat):
                return cat
    return None


def _expand(entry: str, root: Path) -> list[Path]:
    """把一条 include 规则展开成实际文件（``**`` 递归；只收普通文件，不收符号链接）。

    **必须用 :mod:`glob` 而不是 ``Path.glob``**：py3.10 的 ``Path.glob('**')`` 只吐
    **目录**（实测 0 个文件），``data/agent_data_astock/**`` 这种整目录类别会静默搬空
    ——「搬了但一个文件没搬」是这套东西最坏的失效形态，故此处有专测盯着。

    ``glob`` 的通配段默认**不含隐藏文件**（``.foo`` 不匹配 ``*``）：白名单里没有隐藏
    文件，这是有意的（``configs/.env`` 这类绝不误收）。
    """
    from glob import glob as _glob

    out: list[Path] = []
    for rel in _glob(entry, root_dir=root, recursive=True):
        path = root / rel
        if path.is_file() and not path.is_symlink():
            out.append(path)
    return sorted(out)


def plan_legacy_assets(source_root: Path) -> LegacyPlan:
    """扫源目录 → 白名单选择 → 路径闸 → 内容闸 → 逐文件元数据（**不写任何东西**）。"""
    src = Path(source_root).expanduser()
    plan = LegacyPlan(source_root=src)
    if not src.is_dir():
        plan.problems.append(f"源目录不存在或不是目录：{src}")
        return plan

    seen: dict[str, PlannedFile] = {}
    for cat in CATEGORIES:
        for entry in cat.include:
            for path in _expand(entry, src):
                rel = _rel_posix(src, path)
                if rel in seen:
                    continue
                denied = _denied(rel)
                if denied is not None:
                    plan.skipped.append(
                        SkippedFile(rel, f"路径闸命中 {denied!r}：凭据类名字不落盘")
                    )
                    continue
                try:
                    data, keys = landed_bytes(path, cat.transform)
                except (ValueError, OSError) as exc:
                    plan.problems.append(f"{rel}: 读取/转换失败——{exc}")
                    continue
                hard = hard_secret_hits(data)
                if hard:
                    plan.skipped.append(
                        SkippedFile(
                            rel, f"内容闸命中 {', '.join(hard)}：疑似凭据，不落盘"
                        )
                    )
                    continue
                transforms = (cat.transform,) if keys or cat.transform != "copy" else ()
                seen[rel] = PlannedFile(
                    path=rel,
                    category=cat.name,
                    size=len(data),
                    source_sha256=_sha256_file(path),
                    landed_sha256=_sha256_bytes(data),
                    source_mtime=_mtime_iso(path),
                    transforms=transforms,
                    warnings=soft_secret_hits(data),
                )
    plan.files = [seen[k] for k in sorted(seen)]
    return plan


# --- 落盘 -------------------------------------------------------------------


@dataclass
class ApplyReport:
    written: int = 0
    bytes_written: int = 0
    dest: Path | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _manifest_text(files: Iterable[PlannedFile]) -> str:
    return "".join(
        json.dumps(f.record(), ensure_ascii=False, sort_keys=True) + "\n" for f in files
    )


def _report_text(plan: LegacyPlan, dest: Path) -> str:
    lines = [
        "# 隔壁（quant-Trader）资产落盘报告",
        "",
        f"- 源根：`{plan.source_root}`",
        f"- 落地点：`{dest}`",
        f"- 文件：**{len(plan.files)}** 只 / {plan.total_bytes} 字节",
        "",
        "## 分类",
        "",
        "| 类别 | 文件数 |",
        "|---|---|",
    ]
    for name, count in sorted(plan.categories.items()):
        lines.append(f"| {name} | {count} |")
    if plan.skipped:
        lines += ["", "## 已跳过（闸门，逐条留痕）", ""]
        lines += [f"- `{s.path}` —— {s.reason}" for s in plan.skipped]
    if plan.warnings:
        lines += ["", "## 告警（已落盘，建议人看一眼）", ""]
        lines += [f"- `{w}`" for w in plan.warnings]
    if plan.problems:
        lines += ["", "## 阻断问题", ""]
        lines += [f"- {p}" for p in plan.problems]
    lines += [
        "",
        "## 复验",
        "",
        "```bash",
        f"python backend/scripts/migrate_legacy_assets.py --verify --dest {dest}",
        "```",
        "",
    ]
    return "\n".join(lines)


def apply_plan(plan: LegacyPlan, dest: Path) -> ApplyReport:
    """按计划落盘：文件本体 + 清单 + 清单校验 + 报告。

    **只增不改不删**：目标区里已存在的无关文件不动（多出文件由 :func:`verify` 报出）。
    计划自身带阻断问题时**一只都不写**（半份档案比没有档案更难查）。
    """
    out = ApplyReport(dest=Path(dest))
    if plan.problems:
        out.problems = [f"计划有阻断问题，拒绝落盘：{p}" for p in plan.problems]
        return out
    dest = Path(dest)
    archive = dest / ARCHIVE_DIR
    # 第一遍**只算不写**：源在计划之后被改过时，必须一只都不写（半份档案 + 没有清单
    # = 日后没人分得清哪些是当时的、哪些是后来的）。先验完再落。
    payloads: list[tuple[PlannedFile, bytes]] = []
    try:
        for f in plan.files:
            data, _ = landed_bytes(plan.source_root / f.path, _transform_of(f))
            if _sha256_bytes(data) != f.landed_sha256:
                out.problems.append(
                    f"{f.path}: 源在计划之后变了（落地哈希不符），整批中止、一只未写"
                )
                return out
            payloads.append((f, data))
        archive.mkdir(parents=True, exist_ok=True)
        for f, data in payloads:
            target = archive / f.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            out.written += 1
            out.bytes_written += len(data)
        manifest = _manifest_text(plan.files)
        (dest / MANIFEST_NAME).write_text(manifest, encoding="utf-8")
        (dest / MANIFEST_SEAL).write_text(
            f"{_sha256_bytes(manifest.encode('utf-8'))}  {MANIFEST_NAME}\n",
            encoding="ascii",
        )
        (dest / REPORT_NAME).write_text(_report_text(plan, dest), encoding="utf-8")
    except OSError as exc:
        out.problems.append(f"落盘失败：{exc}")
    return out


def _transform_of(f: PlannedFile) -> str:
    """从记录的转换标记反推转换名（``transforms`` 为空 = 逐字节照搬）。"""
    for t in f.transforms:
        for known in TRANSFORMS:
            if t.startswith(known):
                return known
    return "copy"


# --- 复验 -------------------------------------------------------------------


@dataclass
class VerifyReport:
    """复验结果：清单 / 逐文件 / 多出 / 源侧（可选）。"""

    checked: int = 0
    missing: list[str] = field(default_factory=list)
    mismatched: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    source_drift: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (
            self.problems
            or self.missing
            or self.mismatched
            or self.extra
            or self.source_drift
        )

    def detail(self) -> str:
        bits = [f"核过 {self.checked} 只"]
        for label, items in (
            ("缺失", self.missing),
            ("哈希/体积不符", self.mismatched),
            ("多出", self.extra),
            ("源侧已变", self.source_drift),
        ):
            if items:
                bits.append(f"{label} {len(items)}：{', '.join(items[:5])}")
        if self.problems:
            bits.append("；".join(self.problems))
        return "；".join(bits)


def read_manifest(dest: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """读清单 + 核清单自身的封印。返回（记录表, 问题表）。"""
    dest = Path(dest)
    problems: list[str] = []
    mpath = dest / MANIFEST_NAME
    if not mpath.is_file():
        return [], [f"清单不存在：{mpath}"]
    raw = mpath.read_text(encoding="utf-8")
    seal = dest / MANIFEST_SEAL
    if not seal.is_file():
        problems.append(f"清单封印不存在：{seal}")
    else:
        want = seal.read_text(encoding="ascii").split()[0]
        got = _sha256_bytes(raw.encode("utf-8"))
        if want != got:
            problems.append(f"清单被改过：封印 {want[:12]}… ≠ 实算 {got[:12]}…")
    records: list[dict[str, Any]] = []
    for i, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            problems.append(f"清单第 {i} 行不是 JSON：{exc}")
    return records, problems


def verify(
    dest: Path,
    *,
    source_root: Path | None = None,
    check_source: bool = False,
) -> VerifyReport:
    """反向复算落盘结果。

    * 清单完整性（封印）；
    * 逐文件：在不在（``missing``）、字节哈希与体积对不对（``mismatched``）；
    * ``archive/`` 里**多出来的**文件（不在清单里 = 来路不明，报！）；
    * ``check_source`` 且给了 ``source_root`` 时，源侧重算一次源哈希（证明「搬的是当时的源」）。
    """
    dest = Path(dest)
    out = VerifyReport()
    records, problems = read_manifest(dest)
    out.problems = problems
    archive = dest / ARCHIVE_DIR
    listed = {str(r.get("path") or "") for r in records}
    for rec in records:
        rel = str(rec.get("path") or "")
        f = archive / rel
        if not f.is_file():
            out.missing.append(rel)
            continue
        data = f.read_bytes()
        if len(data) != rec.get("size") or _sha256_bytes(data) != rec.get(
            "landed_sha256"
        ):
            out.mismatched.append(rel)
            continue
        out.checked += 1
        if check_source and source_root is not None:
            src = Path(source_root) / rel
            if not src.is_file() or _sha256_file(src) != rec.get("source_sha256"):
                out.source_drift.append(rel)
    if archive.is_dir():
        for p in sorted(archive.rglob("*")):
            if p.is_file() and not p.is_symlink():
                rel = p.relative_to(archive).as_posix()
                if rel not in listed:
                    out.extra.append(rel)
    return out
