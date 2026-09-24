"""市场代码 → 中文展示名（**后端唯一实现**）。

同一份映射写两遍就会分叉：`eval_scores` 曾自带 `_MARKET_LABELS`，告警文案再写一份
就是第三份，而三份里只要有一份漏了 `CRYPTO`，用户就会在界面上看到裸的 ``CRYPTO``。
本模块是后端唯一出处；前端展示层那份在
``electron/src/features/desk/components/copilotModel.ts``，
`backend/tests/test_alert_text.py` 有用例盯着两边的键值一致。
"""

from __future__ import annotations

#: 市场代码（大写）→ 中文名。未收录的代码**回原样**而不是编一个名字。
MARKET_LABELS: dict[str, str] = {
    "CN": "A股",
    "HK": "港股",
    "US": "美股",
    "CRYPTO": "加密",
    "FUTURES": "期货",
}


def market_label(code: object) -> str:
    """市场代码 → 中文名；空值 → ``""``，未收录 → 原样（大写去空白）。"""
    key = str(code or "").strip().upper()
    if not key:
        return ""
    return MARKET_LABELS.get(key, key)
