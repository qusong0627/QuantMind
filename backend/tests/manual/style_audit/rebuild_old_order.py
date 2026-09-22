#!/usr/bin/env python3
"""用**修复前**的排序重建一份风格产物到 /tmp/sf_old_order，供暴露面板逐格对差。

做法：读 build_style_factors.py 源码 → 把两处 ``pit_order`` 调用还原成修复前的写法
（balance: ``np.argsort(ann)`` 非稳定；flow: ``np.argsort(ann, kind="stable")``）→ exec。
不复制文件、不改仓库里的脚本，避免「审计脚本本身变成第二份实现」。

产物只写 ``--out-dir``，不碰线上目录。

运行：docker exec -w /app quantmind python /app/backend/tests/manual/style_audit/rebuild_old_order.py
"""

from __future__ import annotations

import sys

SRC = "/app/backend/scripts/build_style_factors.py"
OUT_DIR = "/tmp/sf_old_order"

REPL = [
    # balance：修复前
    ("order = pit_order(ann.to_numpy()[ok], np.where(np.isfinite(tt), tt, 0.0))",
     "order = np.argsort(ann.to_numpy()[ok])"),
    # flow：修复前（本来就是 stable）
    ('by_ann = pit_order(ann_a, np.where(np.isfinite(tt_a), tt_a, 0.0))',
     'by_ann = np.argsort(ann_a, kind="stable")'),
]


def main() -> int:
    src = open(SRC, encoding="utf-8").read()
    for old, new in REPL:
        assert old in src, f"锚点没找到，源码已变：{old[:50]}"
        src = src.replace(old, new)
    sys.argv = [SRC, "--out-dir", OUT_DIR, "--start", "2016-01-01"]
    globals_ = {"__name__": "__main__", "__file__": SRC}
    exec(compile(src, "bsf_old_order", "exec"), globals_)   # noqa: S102 — 审计脚本，输入是仓库内源码
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
