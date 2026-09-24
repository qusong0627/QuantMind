#!/usr/bin/env python3
"""skills lint：前置检查（提交前跑，CI 可复用）。

检查项：
  1. SKILL.md frontmatter：name == 目录名，description 非空且含触发词
  2. 触发词跨技能重复告警（意图识别冲突）
  3. 运行环境契约不得全文粘贴（必须引用 skills/_shared/env-contract.md）
     第三方原样引入的技能（见 EXEMPT_CONTRACT）豁免第 3 项。

用法：python scripts/lint_skills.py [--strict]
  默认只告警；--strict 下任何问题都返回非零。
"""
import pathlib
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

REPO = pathlib.Path(__file__).resolve().parent.parent
SKILLS = REPO / "skills"

# 第三方原样引入：无须套自研契约/目录规范
EXEMPT_CONTRACT = {
    "futuapi",
    "install-futu-opend",
    "tigeropen",
    "tigeropen-cpp",
    "tigeropen-csharp",
    "tigeropen-go",
    "tigeropen-java",
    "tigeropen-rust",
    "tigeropen-typescript",
}

errors: list[str] = []
warnings: list[str] = []


def parse_frontmatter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return {}
    out: dict = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def main() -> int:
    strict = "--strict" in sys.argv
    skill_dirs = sorted(
        d for d in SKILLS.iterdir()
        if d.is_dir() and not d.name.startswith(("_", ".")) and (d / "SKILL.md").exists()
    )
    if not skill_dirs:
        errors.append("skills/ 下没有可用技能目录")
    trigger_index: dict[str, list[str]] = {}
    for d in skill_dirs:
        text = (d / "SKILL.md").read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        if not fm:
            errors.append(f"{d.name}: 缺 frontmatter ---name/description---")
            continue
        if fm.get("name") != d.name:
            errors.append(f"{d.name}: frontmatter name={fm.get('name')!r} 与目录名不一致")
        desc = fm.get("description", "")
        if not desc:
            errors.append(f"{d.name}: description 为空")
        m = re.search(r"触发词[:：](.+)", desc)
        if not m:
            # 第三方原样引入的技能保持上游描述，不强制加触发词
            if d.name not in EXEMPT_CONTRACT:
                warnings.append(f"{d.name}: description 建议含'触发词：…'供意图识别")
        else:
            seen_here: set[str] = set()
            # 只按顿号/逗号/分号切：含空格的触发词（如 quantdb 结构）保持整体，避免误拆出裸词
            for tok in re.split(r"[、，,；;]+", m.group(1).strip()):
                tok = tok.strip().strip("”").strip('"')
                if tok and tok not in seen_here:
                    seen_here.add(tok)
                    trigger_index.setdefault(tok, []).append(d.name)
        # 契约全文粘贴检查：出现“运行环境契约”且引用块超长即判为粘贴
        if d.name not in EXEMPT_CONTRACT:
            quote_lines = [ln for ln in text.splitlines() if ln.startswith(">")]
            pasted = any("运行环境契约" in ln for ln in quote_lines) and len(quote_lines) > 6
            ref = "_shared/env-contract.md" in text
            if pasted and not ref:
                warnings.append(f"{d.name}: 粘贴了契约全文，P2 应替换为 _shared/env-contract.md 引用")
            if "localhost:800x" in text and "quantmind:8000" not in text:
                warnings.append(f"{d.name}: 疑似缺容器地址映射说明（quantmind:8000）")
    for tok, owners in sorted(trigger_index.items()):
        if len(owners) > 1:
            warnings.append(f"触发词'{tok}'被多个技能声明：{owners}（意图识别可能冲突）")

    print(f"技能数：{len(skill_dirs)}")
    for w in warnings:
        print(f"WARN: {w}")
    for e in errors:
        print(f"ERROR: {e}")
    rc = 0
    if errors or (strict and warnings):
        rc = 1
    print("LINT " + ("FAIL" if rc else "PASS"))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
