"""宽容读文本：非法 UTF-8 字节替换成 U+FFFD，不抛 `UnicodeDecodeError`。

**为什么需要它**（2026-09-21 15:45 实录）：三个 agent 的
`data/agent_data_astock/*/log/2026-09-21/log.jsonl` 被同时截到整 KiB 边界
（110592 / 94208 字节），其中两个的残尾**正好断在一个汉字中间**。

`Path.read_text(encoding="utf-8")` 此时抛 `UnicodeDecodeError` —— 它不是 `OSError`
（继承链是 `UnicodeError → ValueError`），所以各处 `except OSError: continue`
全都接不住，后果是**整个端点 500**：

- `agent_data._read_jsonl` → `GET /api/agents/{agent}/logs` 500，
  同一份文件里 4 条**完好**的决策记录跟着一起消失（前端只剩空对话流）；
- `api_server.token_usage` → `GET /api/token-usage` 500，模型卡的 token 数全灭。

换成 `errors="replace"` 之后，坏字节变成 U+FFFD → 该行 `json.JSONDecodeError`
→ 被各调用点**已有**的坏行分支跳过，好记录照常返回。

**不算修数据**：截断的残尾本来就解析不出来，原件保持原样不动 —— 用户的历史记录
（大半个月的 agent 台账）不该被读取端的容错顺手改写。

新写「读追加型日志文件」的代码请走这里，别再裸 `read_text(encoding="utf-8")`。
"""

from __future__ import annotations

from pathlib import Path


def read_text_lossy(path: Path) -> str:
    """读文本文件，非法 UTF-8 字节替换为 U+FFFD（健康文件逐字节不变）。"""
    return path.read_text(encoding="utf-8", errors="replace")
