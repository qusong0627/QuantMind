"""导出件统一免责段 —— **后端措辞的唯一实现**。

为什么免责段必须进文件本体
--------------------------
屏幕上的免责条（前端 `components/shared/compliance/ComplianceChrome`）只在
应用内可见。导出件一旦落盘、下载或转发就脱离了那个上下文：收件人拿到的是一张
「AI 选出来的股票表」或一份回测报告，却看不到「这不构成投资建议」。
所以免责段是**文件的一部分**，不是可选的装饰。

生成时间与数据区间是必要成分，不是装饰
--------------------------------------
- **生成时间**：说明这是某个时点的快照，避免被当成实时结论；
- **数据区间**：说明结论覆盖哪段数据，避免跨期误读（回测口径尤其）。

两者都**不编造**：区间拿不到就整行不出现，绝不留空格子、也不写半截区间。
（与展示面的「缺失一律 `—`，绝不显示成 0」同一条纪律：缺失就是缺失。）

与前端的关系
------------
前端另有一份实现（``electron/src/utils/exportDisclaimer.ts``）—— 不同语言、
不同产物，无法共享代码。两侧「不许漂」由**金样**机器钉住：

    backend/tests/fixtures/exportDisclaimerGolden.json

前后端测试都读它并逐字对拍，任一侧改了措辞而另一侧没跟，两侧测试都红。
措辞本身沿用项目根 ``CLAUDE.md`` 免责声明段的说法，不另创一套。

本模块**零项目依赖**（只用标准库）：导出点分布在 API 路由、报告生成器、脚本
三处，让它们各自依赖一个重模块不值得。
"""

from __future__ import annotations

import csv
from datetime import datetime

#: 免责语句本体。改这里 = 改所有后端导出件的措辞；**必须同步金样与前端**。
DISCLAIMER_SENTENCE = (
    "本文件由 QuantMind 自动生成，仅供学习研究与技术演示，不构成任何投资建议。"
)

#: 免责段的标签。与前端同名同值，避免两侧叫法漂移（金样对拍）。
DISCLAIMER_LABELS = {
    "exportedAt": "生成时间",
    "dataRange": "数据区间",
    "sentence": "免责声明",
}


def format_export_time(now: datetime | None = None) -> str:
    """``YYYY-MM-DD HH:mm:ss``（本地时区，人读且可排序）。

    用本地时区而非 UTC：导出件是给人看的，收件人对照自己的钟即可。库里存的
    瞬时时间另有口径（``shared/utc_datetime.utc_now()``，aware UTC），两回事。
    """
    return (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def data_range_text(start: object, end: object) -> str | None:
    """由起止构一个区间文本；**缺一端退化为单端**，两端都缺返回 ``None``。

    ``None`` 不是「空区间」，是「不知道」—— 调用方据此**省略整行**，
    而不是写一个空值（空值在表里长得像「有区间但没填」）。
    """
    s = str(start).strip() if start not in (None, "") else ""
    e = str(end).strip() if end not in (None, "") else ""
    if s and e:
        return s if s == e else f"{s} ~ {e}"
    if s:
        return f"{s} 起"
    if e:
        return f"截至 {e}"
    return None


def normalize_data_range(data_range: object) -> str | None:
    """把「一个区间值」归一成文本，供 :func:`disclaimer_rows` 使用。

    导出点手上的区间形态不一：回测结果是 ``start_date``/``end_date`` 两个字段
    （调用方常顺手传一个元组），表格导出可能只有一个展示串。这里统一收口，
    免得每个调用点各自判一次 —— 那种判法必然漏掉某个分支。
    """
    if data_range is None:
        return None
    if isinstance(data_range, (tuple, list)) and len(data_range) == 2:
        return data_range_text(data_range[0], data_range[1])
    text = str(data_range).strip()
    return text or None


def disclaimer_rows(
    data_range: object = None,
    now: datetime | None = None,
) -> list[tuple[str, str]]:
    """免责段的行：``[(标签, 值), ...]``，顺序固定。

    顺序：生成时间 → 数据区间（拿得到才出现）→ 免责语句（压尾）。

    返回**标签化的对**而不是一整句，是为了各渲染器都能正确落格：CSV 交给
    ``csv.writer`` 逐格转义，Excel 逐格写单元格 —— 一整句直接塞进去，
    值里带逗号就会破列。
    """
    rows: list[tuple[str, str]] = [
        (DISCLAIMER_LABELS["exportedAt"], format_export_time(now)),
    ]

    range_text = normalize_data_range(data_range)
    if range_text:
        rows.append((DISCLAIMER_LABELS["dataRange"], range_text))

    rows.append((DISCLAIMER_LABELS["sentence"], DISCLAIMER_SENTENCE))
    return rows


def write_csv_disclaimer(
    writer: csv.writer,
    data_range: object = None,
    now: datetime | None = None,
) -> None:
    """把免责段写进一个已写完全部数据行的 ``csv.writer``：空行 + 若干行。

    空行分隔：多数 CSV 解析器会跳过空行，人来读也一眼看得出「数据到此为止」。
    """
    writer.writerow([])
    for label, value in disclaimer_rows(data_range=data_range, now=now):
        writer.writerow([label, value])
