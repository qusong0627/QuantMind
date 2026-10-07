#!/usr/bin/env python3
"""dsh skill 入口：转调实现脚本（单一实现，勿在此堆逻辑）。

布局敏感：实现本体按部署布局落在不同仓的 `scripts/<NAME>.py`——
  · 本仓（QuantMind）：壳在 `<root>/skills/<skill>/scripts/`，根 = 上溯 3 层；
  · 旧仓（baymax,dsh 布局）：壳在 `<root>/dsh/skills/<skill>/scripts/`，根 = 上溯 4 层。
按候选根探测**文件路径**并用 importlib 加载。曾踩坑：`from <NAME> import main`
会把本文件自己导进来（脚本所在目录在 sys.path[0]，同名即自我遮蔽）——按路径
加载没有这个歧义。实现不在本仓时（迁移方案 P4：实现与 venv「保留在隔壁」），
用 `QM_SKILL_REPO_ROOT=<实现仓根>` 指路。候选全落空 → 报错退 2，绝不静默。
"""
import importlib.util
import os
import sys
from pathlib import Path

NAME = "hypothesis_lab"
HERE = Path(__file__).resolve()


def _candidate_impl_paths():
    yield HERE.parents[3] / "scripts" / f"{NAME}.py"  # 本仓布局
    yield HERE.parents[4] / "scripts" / f"{NAME}.py"  # 旧仓 dsh 布局
    env_root = os.environ.get("QM_SKILL_REPO_ROOT", "").strip()
    if env_root:
        yield Path(env_root) / "scripts" / f"{NAME}.py"


def _load_impl():
    tried = []
    for path in _candidate_impl_paths():
        tried.append(str(path))
        if path.is_file():
            spec = importlib.util.spec_from_file_location(f"_skill_impl_{NAME}", path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            return module
    sys.stderr.write(
        f"[FATAL] 未找到实现 {NAME}.py（已探测）：\n  "
        + "\n  ".join(tried)
        + "\n提示：设 QM_SKILL_REPO_ROOT=<实现所在仓根> 后重试。\n"
    )
    raise SystemExit(2)


if __name__ == "__main__":
    sys.exit(_load_impl().main())
