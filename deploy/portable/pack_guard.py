#!/usr/bin/env python3
"""便携包出厂净化闸门：打包时排除、压缩前复核、压缩后复核（三用一件工具）。

用法::

    python3 deploy/portable/pack_guard.py --stage  <staging 目录>
    python3 deploy/portable/pack_guard.py --make-zip <staging 目录> <输出.zip>
    python3 deploy/portable/pack_guard.py --zip    <输出.zip>

三种模式都接受 ``--allow-local-live``（显式放行本机独有实盘栏目的前端产物，
自用包；默认拒绝，与 ``scripts/deploy_frontend.sh`` 同名同义）。
退出码：``0`` 通过 / ``1`` 有违规（非零即中止出包）/ ``2`` 用法或 IO 错误。

为什么要有这道闸门（不是「再加一层保险」）
------------------------------------------
2026-09 盘点实测：`deploy/portable/build/QuantMind-Portable-win-x64` 这份 staging 被
**两个构建器共用**，Live 构建器往里面覆盖了 `pack.env`（16 个键全有值，含
`DB_PASSWORD`、内网地址、SMB 共享路径）、`bridge/`、`live/`、`README-LIVE.md`；
通用构建器**不删**这些，于是「通用便携包」里带着运营者机器的坐标与口令出厂。
另外 `cp -a backend` 会连 `backend/logs/*.log`、`backend/scripts/log/**`（269 个）、
`.pytest_cache`、`htmlcov` 一起收；`cp -a models` 会连 `models/users/`（1.3G 私有
模型）一起收。这些都是**看构建日志看不出来**的（cp 不报错），只能靠出包后拿清单核对。

四层判据（全部在 ``pack_rules.py``，本文件只负责执行）
------------------------------------------------------
1. **路径清单**：排除项（打包时跳过；压缩后还在 = 违规）与必备项（少一个 = 残包）；
   另有 **私有栏目产物**（``R.PRIVATE_CHUNKS``）：``electron/src/features/local-live/``
   是运营者本机独有、不开源的实盘栏目（``.gitignore`` 排除），本机构建会把它打进
   ``dist-react/``，而两份包都从那里取 ``web/`` —— 产物里出现它的 chunk 即违规，
   与 ``scripts/deploy_frontend.sh`` 第 3 步同源同判；
2. **内容判据**：内网地址与明文口令——检测器**复用**
   ``backend/tests/test_no_internal_addresses_or_plaintext_secrets.py``，
   本文件不重写正则。那份是权威定义（含全部误报豁免与理由），这里只是把扫描面
   从「git 已跟踪文件」换成「即将出厂的树」。检测器导入失败**不降级**：宁可整条
   闸门报错，也不留下一道「正则没加载上所以全绿」的空转护栏。
3. **宿主残留值**：拿打包机上 ``.env`` / ``config/runtime.env`` 里的**真实值**逐字
   精确匹配出厂物（键名带 pass/secret/token/key 的、长度达标的才算探针）。正则抓
   的是「长得像密钥」的形态，这一层抓的是「就是那把密钥」——两者互补，且精确匹配
   零误报。**命中处只报文件、行号与来源键名，绝不回显值本身。**

已知不覆盖（写在这里，而不是靠读者猜）
--------------------------------------
- 第三方运行时（``runtime/``、``pgsql/``、``redis/``、``huntly/``、``qwenpaw_runtime/``）
  不做内容扫描：它们是上游原样下载的，且里面有 ``cacert.pem``／``test.key`` 这类
  正常文件（不分范围会淹掉护栏）。它们也**不是**宿主密钥能落进去的地方。
- 大二进制（> 8MB）不做探针扫描：模型权重的字节不可能逐字等于一个 ASCII 密钥；
  真正会夹带凭据的是 ``metadata.json`` / 小 pickle 这类小文件，它们都在扫描面内。
- UI 开关（``VITE_ENABLE_REAL_TRADING``）不是泄漏，本闸门不管——只拦会让通用包
  整块功能消失的 live-node 形态（见 ``pack_rules.SHAPE_MARKERS``）。
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import NamedTuple

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))

import pack_rules as R  # noqa: E402  （同目录模块：`sys.path` 上面那行已备好）

#: 公仓那份检测器的位置。**改路径 = 闸门报错**（不是静默降级），见模块 docstring。
DETECTORS_PATH = (
    REPO_ROOT
    / "backend"
    / "tests"
    / "test_no_internal_addresses_or_plaintext_secrets.py"
)

#: 报告里每类最多列几行（总数照实报）。防的是一次误配置刷屏几千行、真问题被埋掉。
MAX_LINES_PER_KIND = 20

#: 文本整读上限：超过就跳过正则扫描并**记一行**（绝不静默）。
MAX_TEXT_BYTES = 16 * 1024 * 1024

#: 探针扫描面里的二进制上限：见模块 docstring「已知不覆盖」第二条。
MAX_SMALL_BINARY_BYTES = 8 * 1024 * 1024

CHUNK = 1024 * 1024


# ---------------------------------------------------------------------------
# 检测器（复用公仓那份，不重写）
# ---------------------------------------------------------------------------


def load_detectors():
    if not DETECTORS_PATH.is_file():
        raise SystemExit(
            f"[guard] 检测器不在位：{DETECTORS_PATH}\n"
            "        内网地址/明文口令的判据在 backend/tests/ 那份测试里（唯一实现）。\n"
            "        闸门拒绝降级成全绿——找不到就整条报错，改名了请同步这里。"
        )
    spec = importlib.util.spec_from_file_location("qm_pack_detectors", DETECTORS_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# 待检条目：staging 文件与 zip 成员统一成同一种形状，两处共用一份检查逻辑
# ---------------------------------------------------------------------------


class Entry(NamedTuple):
    rel: str
    size: int
    chunks: Callable[[], Iterator[bytes]]


def iter_stage(stage: Path) -> Iterator[Entry]:
    for dirpath, dirnames, filenames in os.walk(stage):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            rel = str(path.relative_to(stage))
            try:
                size = path.stat().st_size
            except OSError:
                size = -1  # 断链等：大小未知，照样进检查（读的时候会报出来）
            yield Entry(
                rel,
                size,
                (lambda p=path: _read_chunks(p)),
            )


def _read_chunks(path: Path, limit: int | None = None) -> Iterator[bytes]:
    read = 0
    with open(path, "rb") as fh:
        while True:
            block = fh.read(CHUNK)
            if not block:
                return
            read += len(block)
            yield block
            if limit is not None and read >= limit:
                return


@contextmanager
def iter_zip(zip_path: Path) -> Iterator[tuple[str, list[Entry]]]:
    """产出 (包根目录名, 条目)。目录项不进检查（空目录是要留的，见 pack_rules）。

    **一个 zip 只解析一次中央目录**：``ZipFile.open()`` 每次调用都要重读一遍中央目录，
    12 万条目的包里逐文件重开 = O(条目数 × 中央目录)，实测 `--zip` 复核从 1 分钟涨到
    十几分钟——而这条命令在发版路径上，慢到没人愿意跑就等于没有闸门。

    共用一个句柄要求**顺序读**：条目生成器必须用完再取下一个（本文件的两个扫描函数
    都是逐条目整读，没有交叉持有）。谁要在同一条目上读两遍（``_line_of`` 就是这样）
    也没问题——前一个生成器那时已经耗尽、句柄已还。
    """
    with zipfile.ZipFile(zip_path) as zf:
        names = [i.filename for i in zf.infolist() if not i.is_dir()]
        roots = {n.split("/", 1)[0] for n in names if "/" in n}
        if len(roots) != 1:
            raise SystemExit(
                f"[guard] 产物里不是一个顶层目录：{sorted(roots)}\n"
                "        解压时会**并进**别的文件夹，覆掉对方的 start.bat / pack.env。"
            )
        root = roots.pop()

        def chunks(name: str) -> Iterator[bytes]:
            with zf.open(name) as fh:
                while True:
                    block = fh.read(CHUNK)
                    if not block:
                        return
                    yield block

        entries = [
            Entry(
                info.filename.split("/", 1)[1],
                info.file_size,
                partial(chunks, info.filename),
            )
            for info in zf.infolist()
            if not info.is_dir()
        ]
        yield root, entries


# ---------------------------------------------------------------------------
# 检查
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    kind: str
    rel: str
    detail: str
    fatal: bool


def _line_of(offset: int, chunks_factory) -> int:
    """命中处的行号（1 起）。命中值本身**不参与计算也不回显**。"""
    seen = 0
    line = 1
    for block in chunks_factory():
        if seen + len(block) > offset:
            line += block[: offset - seen].count(b"\n")
            return line
        seen += len(block)
        line += block.count(b"\n")
    return line


def scan_entries(
    entries: Iterator[Entry],
    *,
    mode: str,
    needles: list[tuple[str, str]],
    detectors,
    allow_private: bool = False,
) -> tuple[list[Finding], list[str]]:
    """``mode`` ∈ {"stage", "zip"}：stage 下命中排除项只是提示，zip 下是违规。

    私有栏目产物（``R.PRIVATE_CHUNKS``）在**两种模式下都是违规**：它不在排除清单里，
    打包时不会消失——出现在 staging 就等于会出现在产物里。``allow_private`` 是
    ``--allow-local-live`` 的显式放行（与 ``scripts/deploy_frontend.sh`` 同名同义）。
    """
    findings: list[Finding] = []
    notes: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        rel = entry.rel
        if rel in seen:
            continue
        seen.add(rel)
        rule = R.match_excludes(rel)
        if rule is not None:
            findings.append(
                Finding(
                    "排除项" if mode == "zip" else "将排除",
                    rel,
                    rule.reason,
                    fatal=(mode == "zip"),
                )
            )
            # 排除项不会出厂 ⇒ 内容判据不必再看它（省掉 models/users 那 1.3G 的读）。
            continue
        for pattern, reason in R.PRIVATE_CHUNKS:
            if R.matches_pattern(pattern, rel):
                findings.append(
                    Finding(
                        "私有栏目",
                        rel,
                        reason
                        + (
                            "（已由 --allow-local-live 显式放行）"
                            if allow_private
                            else ""
                        ),
                        fatal=not allow_private,
                    )
                )
        findings.extend(_scan_content(entry, needles, detectors, notes))
    return findings, notes


def _regex_scope(rel: str, detectors) -> bool:
    """正则扫描面：自家树，**再扣掉公仓那份检测器自己已经裁定过的两类**。

    不重立规矩、直接沿用它的裁定（它有理由，理由写在它文件里）：
    - **测试夹具**（``_is_test_file``，含 ``**/tests/**``）：里面的地址与口令是构造的；
    - **构建产物**（``web/``，前端 bundle 与 monaco 这类第三方压缩包）：它的收尾
      「说明：本文件不覆盖的构建产物」一节明确把 ``web/assets/**`` 划在扫描面外，
      理由是「只能重建或删除，不能改一行」——那件事由源头（``electron/src/**``）
      和 ``SHAPE_MARKERS`` 管。
    探针（逐字精确匹配）**不受这两条限制**：精确匹配零误报，多扫一片只有好处。
    """
    if not R.in_trees(rel, R.SCAN_TREES):
        return False
    if rel.startswith("web/"):
        return False
    return not detectors._is_test_file(rel)


def _scan_content(entry: Entry, needles, detectors, notes: list[str]) -> list[Finding]:
    """单个条目的内容判据：形态标记 / 内网地址 / 明文口令 / 宿主残留值。"""
    rel = entry.rel
    out: list[Finding] = []
    in_regex_scope = _regex_scope(rel, detectors)
    in_needle_scope = R.in_trees(rel, R.NEEDLE_TREES)
    if not (in_regex_scope or in_needle_scope):
        return out
    text_like = R.is_text_path(rel)

    # 形态标记：web/assets 里的构建期内联开关（二进制串匹配，与是否文本后缀无关）
    if rel.startswith("web/"):
        blob = b"".join(entry.chunks())
        for marker, reason in R.SHAPE_MARKERS:
            if marker in blob:
                out.append(Finding("形态", rel, reason, True))
        del blob

    if text_like and in_regex_scope:
        if entry.size > MAX_TEXT_BYTES:
            notes.append(
                f"跳过正则扫描（{entry.size // 1024 // 1024}MB > 上限）：{rel}"
            )
        else:
            raw = b"".join(entry.chunks())
            text = raw.decode("utf-8", "replace")
            for lineno, addr, _line in detectors.find_internal_addresses(text):
                out.append(Finding("内网地址", f"{rel}:{lineno}", f"地址 {addr}", True))
            code = Path(rel).suffix.lower() in getattr(
                detectors, "_CODE_SUFFIXES", {".py", ".ts", ".tsx", ".js"}
            )
            for lineno, key, _line in detectors.find_plaintext_secrets(text, code=code):
                # 只报键名与位置：命中行里就是明文口令，回显等于把它抄进构建日志。
                out.append(Finding("明文口令", f"{rel}:{lineno}", f"键名 {key}", True))

    if (
        needles
        and in_needle_scope
        and (text_like or 0 <= entry.size <= MAX_SMALL_BINARY_BYTES)
    ):
        out.extend(_scan_needles(entry, needles))
    return out


def _scan_needles(entry: Entry, needles: list[tuple[str, str]]) -> list[Finding]:
    """宿主真实值逐字匹配（流式，跨块用 needle 长度做重叠）。"""
    probes = [(src, val.encode()) for src, val in needles]
    longest = max(len(b) for _, b in probes)
    tail = b""
    offset = 0
    hits: list[tuple[str, int]] = []
    for block in entry.chunks():
        buf = tail + block
        base = offset - len(tail)
        for source, needle in probes:
            start = 0
            while True:
                idx = buf.find(needle, start)
                if idx < 0:
                    break
                hits.append((source, base + idx))
                start = idx + 1
        tail = buf[-longest:] if longest else b""
        offset += len(block)
    out: list[Finding] = []
    for source, at in hits[:MAX_LINES_PER_KIND]:
        # 行号要再读一遍（流式扫描不留全文）；值仍然不回显。
        lineno = _line_of(at, entry.chunks)
        out.append(
            Finding(
                "宿主残留值",
                f"{entry.rel}:{lineno}",
                f"与打包机 {source} 的值逐字相同",
                True,
            )
        )
    if len(hits) > MAX_LINES_PER_KIND:
        out.append(
            Finding(
                "宿主残留值",
                entry.rel,
                f"（同一文件还有 {len(hits) - MAX_LINES_PER_KIND} 处）",
                True,
            )
        )
    return out


def check_required(entries: Iterator[Entry]) -> list[Finding]:
    rels = {e.rel for e in entries}
    out: list[Finding] = []
    for must in R.REQUIRED_FILES:
        if must not in rels:
            out.append(Finding("缺必备", must, "包不完整", True))
    for pattern, reason in R.REQUIRED_GLOBS:
        if not any(R.matches_pattern(pattern, rel) for rel in rels):
            out.append(Finding("缺必备", pattern, reason, True))
    # 成对项：哨兵在、必备不在 → 半个组件（看着有、点开报错）。两个都不在只提示。
    for sentinel, must, reason in R.REQUIRED_PAIRS:
        if sentinel in rels and must not in rels:
            out.append(Finding("缺必备", must, reason, True))
    for sentinel, note in R.OPTIONAL_COMPONENTS:
        if sentinel not in rels:
            out.append(Finding("提示", sentinel, note, False))
    if not any(rel.startswith("models/production/") for rel in rels):
        out.append(
            Finding(
                "提示",
                "models/production",
                "预置模型缺失：装完没有模型可选（不拦出包）",
                False,
            )
        )
    return out


# ---------------------------------------------------------------------------
# 写 zip（排除清单唯一生效点）
# ---------------------------------------------------------------------------


def make_zip(stage: Path, out: Path, *, deflate: bool = False) -> tuple[str, int, int]:
    if not stage.is_dir():
        raise SystemExit(f"[guard] staging 不存在：{stage}")
    entries = list(iter_stage(stage))
    root = stage.name
    skipped: list[str] = []
    written: list[str] = []
    dirs: set[str] = set()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    comp = zipfile.ZIP_DEFLATED if deflate else zipfile.ZIP_STORED
    try:
        with zipfile.ZipFile(tmp, "w", comp) as zf:
            for entry in entries:
                if R.match_excludes(entry.rel) is not None:
                    skipped.append(entry.rel)
                    continue
                zf.write(stage / entry.rel, f"{root}/{entry.rel}")
                written.append(entry.rel)
                parts = entry.rel.split("/")[:-1]
                for i in range(1, len(parts) + 1):
                    dirs.add("/".join(parts[:i]))
            # 目录项：写出文件的祖先目录 + 显式保留的空目录（后端按固定路径找它们，
            # 缺目录比空目录更容易出怪问题——实盘包那边同一条理由）。
            for d in sorted(dirs | set(R.EMPTY_DIRS)):
                zf.writestr(zipfile.ZipInfo(f"{root}/{d}/"), b"")
        tmp.replace(out)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return root, len(written), len(skipped)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _report(findings: list[Finding], notes: list[str], headline: str) -> int:
    """按类别 → 按原因归并后打印。

    **归并是必须的**：``backend/config/users/**`` 一家就是 14 个文件、``models/users``
    1.3G 上千个，逐条列会把真正的违规淹掉（本仓「一条会误报的护栏等于没有护栏」）。
    每类给总数 + 前几个路径，原因相同的合并成一行。
    """
    order = [
        "缺必备",
        "排除项",
        "私有栏目",
        "形态",
        "内网地址",
        "明文口令",
        "宿主残留值",
        "将排除",
        "提示",
    ]
    by_kind: dict[str, list[Finding]] = {}
    for f in findings:
        by_kind.setdefault(f.kind, []).append(f)
    print(headline)
    fatal = 0
    for kind in order + [k for k in by_kind if k not in order]:
        items = by_kind.get(kind)
        if not items:
            continue
        is_fatal = any(i.fatal for i in items)
        print(
            f"\n  [{kind}] {len(items)} 项" + ("（违规）" if is_fatal else "（提示）")
        )
        groups: dict[str, list[str]] = {}
        for item in items:
            groups.setdefault(item.detail, []).append(item.rel)
        for detail, rels in list(groups.items())[:MAX_LINES_PER_KIND]:
            sample = "、".join(rels[:3])
            more = f" 等 {len(rels)} 个" if len(rels) > 3 else ""
            print(f"    - {sample}{more}  —— {detail}")
        if len(groups) > MAX_LINES_PER_KIND:
            print(f"    … 另有 {len(groups) - MAX_LINES_PER_KIND} 类原因（未逐条列出）")
        fatal += len(items) if is_fatal else 0
    for note in notes[:MAX_LINES_PER_KIND]:
        print(f"  [提示] {note}")
    if len(notes) > MAX_LINES_PER_KIND:
        print(f"  [提示] … 另有 {len(notes) - MAX_LINES_PER_KIND} 条")
    print(f"\n  合计：违规 {fatal} 项" + (f"，提示 {len(notes)} 条" if notes else ""))
    return 1 if fatal else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="便携包出厂净化闸门")
    ap.add_argument("--stage", metavar="DIR", help="校验 staging（压缩前）")
    ap.add_argument("--zip", metavar="ZIP", help="校验产物（压缩后）")
    ap.add_argument(
        "--make-zip",
        nargs=2,
        metavar=("STAGE", "OUT"),
        help="按排除清单打包（写之前先跑一遍 staging 校验，违规即拒绝出包）",
    )
    ap.add_argument(
        "--deflate",
        action="store_true",
        help="压缩（默认 store：包体大头是二进制，压不动）",
    )
    ap.add_argument(
        "--env-file", action="append", default=[], help="补充宿主探针来源（可多次）"
    )
    ap.add_argument(
        "--allow-local-live",
        action="store_true",
        help="显式放行本机独有实盘栏目的前端产物（自用包；"
        "与 scripts/deploy_frontend.sh 同名同义，默认拒绝）",
    )
    args = ap.parse_args(argv)
    modes = [bool(args.stage), bool(args.zip), bool(args.make_zip)]
    if sum(modes) != 1:
        ap.error("--stage / --zip / --make-zip 三选一")

    detectors = load_detectors()
    env_files = [Path(p) for p in args.env_file] or [
        REPO_ROOT / ".env",
        REPO_ROOT / "config" / "runtime.env",
    ]
    needles, unreadable, dropped = R.host_needles(env_files, REPO_ROOT)

    if args.make_zip:
        stage, out = Path(args.make_zip[0]), Path(args.make_zip[1])
        code = _verify_stage(
            stage,
            needles,
            unreadable,
            dropped,
            detectors,
            allow_private=args.allow_local_live,
        )
        if code != 0:
            print(
                "\n  ✗ staging 有违规，拒绝出包（修正后重跑；排除清单在 pack_rules.py）"
            )
            return code
        root, written, skipped = make_zip(stage, out, deflate=args.deflate)
        print(
            f"\n  ✓ 已写出 {out}（根目录 {root}/，{written} 个文件，"
            f"按清单排除 {skipped} 个）"
        )
        return 0

    if args.stage:
        return _verify_stage(
            Path(args.stage),
            needles,
            unreadable,
            dropped,
            detectors,
            allow_private=args.allow_local_live,
        )

    zip_path = Path(args.zip)
    if not zip_path.is_file():
        raise SystemExit(f"[guard] 产物不存在：{zip_path}")
    with iter_zip(zip_path) as (root, entries):
        findings, notes = scan_entries(
            entries,
            mode="zip",
            needles=needles,
            detectors=detectors,
            allow_private=args.allow_local_live,
        )
        findings.extend(check_required(iter(entries)))
    headline = (
        f"[guard] 产物校验：{zip_path}（根目录 {root}/，{len(entries)} 个文件）\n"
        f"        探针来源 {len(needles)} 个"
        + (f"，另有 {len(unreadable)} 个读不到" if unreadable else "")
    )
    for path in unreadable:
        notes.append(f"探针来源读不到（该来源未参与匹配）：{path}")
    for key in dropped:
        notes.append(f"探针已丢弃（该值与已跟踪文件里的字面量相同 ⇒ 已公开）：{key}")
    return _report(findings, notes, headline)


def _verify_stage(
    stage: Path, needles, unreadable, dropped, detectors, *, allow_private: bool = False
) -> int:
    if not stage.is_dir():
        raise SystemExit(f"[guard] staging 不存在：{stage}")
    entries = list(iter_stage(stage))
    findings, notes = scan_entries(
        iter(entries),
        mode="stage",
        needles=needles,
        detectors=detectors,
        allow_private=allow_private,
    )
    findings.extend(check_required(iter(entries)))
    headline = f"[guard] staging 校验：{stage}（{len(entries)} 个文件）"
    for path in unreadable:
        notes.append(f"探针来源读不到（该来源未参与匹配）：{path}")
    for key in dropped:
        notes.append(f"探针已丢弃（该值与已跟踪文件里的字面量相同 ⇒ 已公开）：{key}")
    return _report(findings, notes, headline)


if __name__ == "__main__":
    sys.exit(main())
