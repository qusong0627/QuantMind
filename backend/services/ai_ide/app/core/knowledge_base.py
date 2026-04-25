import os
import time


class KnowledgeBase:
    """
    负责管理本地量化研发知识库。
    缓存 TTL 5 分钟，过期后自动重新加载文档。
    """

    _CACHE_TTL_SEC = 300

    def __init__(self, project_root: str):
        self.project_root = project_root
        self.doc_paths = [
            "docs/Qlib内部策略开发规范.md",
            "docs/QuantMind_152维特征方案规范.md",
            "docs/Qlib回测API集成指南.md",
        ]
        self._cached_context = ""
        self._cached_at = 0.0

    def get_context_summary(self) -> str:
        now = time.monotonic()
        if self._cached_context and (now - self._cached_at) < self._CACHE_TTL_SEC:
            return self._cached_context

        summary = "### QuantMind Project Standards & API Reference:\n"

        for doc_rel_path in self.doc_paths:
            full_path = os.path.join(self.project_root, doc_rel_path)
            if os.path.exists(full_path):
                try:
                    with open(full_path, encoding="utf-8") as f:
                        content = f.read()
                        summary += f"\n-- From {doc_rel_path} --\n{content[:3000]}\n"
                except Exception as e:
                    summary += f"\n-- Error reading {doc_rel_path}: {str(e)} --\n"
            else:
                summary += f"\n-- File not found: {doc_rel_path} --\n"

        self._cached_context = summary
        self._cached_at = now
        return summary
