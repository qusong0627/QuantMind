"""知识库检索相关性验收（批次 1b）。

`create_embedding` 曾被替换成伪随机向量（sha256 播种 → 高斯噪声），修复后才第一次
有真实的语义检索。但「记忆已经打开」这件事**不能只看 KB 文件在增长**：

  `rdagent/components/knowledge_management/graph.py:142-146` 的插入去重是
  **被注释掉的**（与基线 `6762f84f` 比对为 0 行差异，是上游自身状态，不是本地补丁），
  所以图谱**必然**越长越大——增长不构成任何健康证据。

唯一能证明记忆有用的是：检索出来的东西**确实和查询语义相关**。本文件只测这一条，
并且走真实链路：

    semantic_search → PDVectorBase.search → APIBackend().create_embedding

因此需要真实 embedding 通道；未配置时跳过（与 `test_embedding_config.py` 同款约定）。
宿主未装 rdagent，本文件在容器内跑：

    docker exec quantmind python -m pytest backend/tests/test_kb_retrieval.py -v -s
"""

from __future__ import annotations

import os
import pickle

import pytest

graph_mod = pytest.importorskip(
    "rdagent.components.knowledge_management.graph",
    reason="rdagent 未安装（宿主跳过，容器内运行）",
)
UndirectedGraph = graph_mod.UndirectedGraph
UndirectedNode = graph_mod.UndirectedNode


def _embedding_configured() -> bool:
    return all(
        (os.environ.get(k) or "").strip()
        for k in ("EMBEDDING_MODEL", "EMBEDDING_BASE_URL", "EMBEDDING_API_KEY")
    )


pytestmark = pytest.mark.skipif(
    not _embedding_configured(),
    reason="未配置 EMBEDDING_*，跳过真实检索验收",
)

# 三类互不相同的「因子实现踩坑」经验，KB 里实际存的就是这种粒度
KB_INDEX = "错误：因子计算时索引超出范围 / 解决：先按交易日历 reindex 对齐再取数"
KB_NAN = "错误：算出来的因子值全是 NaN / 解决：检查停牌股与复权处理，缺失值前向填充"
KB_PATH = "错误：读取数据文件报 FileNotFoundError / 解决：确认数据目录路径与文件命名"

SEEDS = (KB_INDEX, KB_NAN, KB_PATH)


def _seed_graph() -> graph_mod.UndirectedGraph:
    """建一张新的语义图谱并写入三条经验（每次调用都重新建，保证用例独立）。"""
    graph = UndirectedGraph()
    for content in SEEDS:
        graph.add_node(UndirectedNode(content=content, label="knowledge"))
    return graph


def _ranking(graph: graph_mod.UndirectedGraph, query: str, k: int = 3) -> list[str]:
    hits = graph.semantic_search(node=query, topk_k=k, similarity_threshold=0.0)
    return [node.content for node in hits]


def test_retrieval_ranks_semantically_nearest_first() -> None:
    """问「因子全是空值」应命中 NaN 那条，而不是任意一条。"""
    # Arrange
    graph = _seed_graph()
    query = "因子算出来全是空值，没有有效数字"

    # Act
    top = _ranking(graph, query, k=1)

    # Assert
    assert top, "检索返回空——知识库通路断了"
    assert top[0] == KB_NAN, f"最近邻不是语义最相关那条，实际命中：{top[0][:60]}"


def test_distinct_queries_pick_distinct_knowledge() -> None:
    """不同查询必须命中不同节点。

    这正是伪随机向量做不到的：修复前三条查询的向量余弦相似度≈0，
    排序等同于随机，三个 top-1 会互相打架。
    """
    graph = _seed_graph()
    cases = {
        KB_INDEX: "报错说索引越界了",
        KB_NAN: "因子值都是空的",
        KB_PATH: "找不到数据文件",
    }
    wrong = []
    for expected, query in cases.items():
        top = _ranking(graph, query, k=1)
        if not top or top[0] != expected:
            wrong.append((query, expected[:26], top[0][:26] if top else "<空>"))
    assert not wrong, "以下查询未命中预期经验：\n" + "\n".join(
        f"  查询 {q!r} → 期望 {e!r}，实际 {a!r}" for q, e, a in wrong
    )


def test_knowledge_survives_pickle_round_trip() -> None:
    """落盘再读回，检索结果不变——这才叫「记忆」。

    对应 `dump_knowledge_base()` / `load_dumped_knowledge_base()` 的 pickle 往返。
    """
    # Arrange
    graph = _seed_graph()
    query = "找不到数据文件"
    before = _ranking(graph, query, k=1)

    # Act
    restored = pickle.loads(pickle.dumps(graph))
    after = _ranking(restored, query, k=1)

    # Assert
    assert before and before[0] == KB_PATH
    assert after == before, f"往返后检索结果变了：{before} → {after}"


def test_unconfigured_channel_raises_instead_of_faking(monkeypatch: pytest.MonkeyPatch) -> None:
    """通道缺失时必须响亮失败，绝不退回伪向量。

    这是本次修复要根除的失效模式：配置不全会静默产出噪声向量，
    调用方无从察觉。此用例把「未就绪 → 报错」钉成契约。

    这里只问前置判据 `_embedding_channel_ready()`，不发真实请求。
    """
    from rdagent.oai.backend import litellm as litellm_backend

    backend = litellm_backend.LiteLLMAPIBackend()
    ready, reason = backend._embedding_channel_ready()
    assert (ready, reason) == (True, ""), "本用例需要已配置的 EMBEDDING_* 环境"

    # litellm.py:21 是 `from ...embedding import resolve_embedding_channel` ——直接绑定名字，
    # 所以补丁必须打在 litellm 模块上；打 rdagent.oai.utils.embedding 不会生效。
    monkeypatch.setattr(
        litellm_backend, "resolve_embedding_channel", lambda: ("", "", "")
    )
    ready, reason = backend._embedding_channel_ready()
    assert ready is False
    assert "EMBEDDING_MODEL" in reason
