"""迁移产物落点判据（共用层）：**产物不许进仓**。

为什么单独一个模块，而不是放进 ``legacy_assets``
------------------------------------------------
``legacy_assets`` 有一条自带的纯度守卫（``test_shared_module_never_imports_subprocess``
——那份计划/落盘/复验的纯逻辑不许摸到外部世界）。git 探测是**外部世界**，所以它不能
住在那里。但它又必须只有**一份**：判据在 P3-②（落地区）与 P5（守护计划迁移的
``--record`` 存档）两处使用，各写一份的话，收紧一处必然漏掉另一处——而漏掉的那一处
正好是**恢复现场时唯一还留着证据的那个文件**。

判据本身为什么不是「字符串前缀在不在仓里」
------------------------------------------
本仓 ``data/`` 是指向 ``/media/zbox/data/quantmind`` 的**符号链接**：``data/legacy``
字面上在仓里，``resolve()`` 之后在另一块盘上。所以两步：先解析路径，再问 git
（``rev-parse --show-toplevel``）。只看字面前缀会把合规落点误判成违规，进而让操作员
在切换窗口里看到一条假的拒绝——那时人会去改命令而不是查判据。
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def artifact_refusal(path: Path, *, project_root: Path) -> str | None:
    """产物落点不合规时返回理由（``None`` = 放行）。两条判据**并列**，缺一不可：

    1. 在 **QuantMind 仓库树**内——这条不依赖 git。容器里看不到 ``.git``（挂载进来的
       是子目录），只靠 git 判断会**静默放行**，把带证据链的产物写进用户挂载的仓库树；
    2. 在某个 **git 工作树**内——宿主上覆盖「产物落到别的 checkout」。

    两条都先 ``resolve()``：``data/`` 是符号链接，解析后在另一块盘上，看字面前缀会
    误判（合规落点被拒 ⇔ 操作员在切换窗口里去改命令，而不是查判据）。

    容器里的注意点：``/app`` 就是仓库树，所以容器内 ``/app/data/...`` 也会被第 1 条
    拦下（宿主上那里其实是符号链接、在工作树之外）。这是**故意**的——容器里写
    ``/tmp`` 再 ``docker cp`` 取出来，比让判据去猜挂载拓扑可靠。
    """
    resolved = path.resolve()
    root = project_root.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        pass
    else:
        return f"在 QuantMind 仓库树 {root} 内"
    worktree = inside_git_worktree(path)
    if worktree is not None:
        return f"在 git 工作树 {worktree} 内"
    return None


def inside_git_worktree(path: Path) -> str | None:
    """``path`` 若在某个 git 工作树里，返回该工作树根（否则 ``None``）。

    路径还不存在时沿父目录上溯（``data/legacy/quanttrader/...` 常常是第一次创建）。
    git 不可用（容器里没装、不是仓）一律返回 ``None``——**放行**：判据的用途是拦
    「已知会进仓」的落点，工具缺失时拦住合法操作才是更大的错。
    """
    probe = path.resolve()
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        cp = subprocess.run(
            ["git", "-C", str(probe), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if cp.returncode != 0:
        return None
    top = Path(cp.stdout.strip()).resolve()
    try:
        path.resolve().relative_to(top)
    except ValueError:
        return None
    return str(top)


__all__ = ["artifact_refusal", "inside_git_worktree"]
