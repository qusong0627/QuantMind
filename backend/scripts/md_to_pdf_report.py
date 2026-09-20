"""Convert TradingAgents markdown report to a styled Chinese PDF (研报级设计).

设计规范（对应 SKILL.md §7.4）：
- 封面页：深海军蓝底 + 金色双线 + 白字标题 + 报告日期/数据截至 + 免责声明
- 页眉：金色细线 + 当前章节标题（afterFlowable 跟踪当前页所属章节）
- 页脚：金色细线 + 免责声明 + 「第 X 页 / 共 Y 页」（NumberedCanvas 延迟绘制拿总页数）
- 章节标题（H2）：金色竖线 + 深蓝粗体 + 金色细分隔线
- 表格：深蓝表头白字 + 金色表头底线 + 斑马纹 + 数字列自动右对齐
- A股语义配色（红涨绿跌）：涨/买入/偏多→红，跌/卖出/偏空→绿，中性→琥珀
- 引用块 → 米色金边提示框；分隔线 → 金色细线
- 字体：Noto Sans CJK SC（覆盖全、现代感）；缺省回退文泉驿 / CID 字体
"""

import io
import re
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas as PDFCanvas
from reportlab.platypus import (
    CondPageBreak,
    HRFlowable,
    PageBreak,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    SimpleDocTemplate,
)

PAGE_W, PAGE_H = A4

# ---------- 设计 token（金融蓝 + 金色点缀，券商研报风） ----------
C_PRIMARY = colors.HexColor("#1A4D8F")      # 主色·深蓝（表头 / H2 文字）
C_NAVY = colors.HexColor("#0F2A52")         # 深海军蓝（封面背景）
C_ACCENT = colors.HexColor("#C9A227")       # 点缀金（分隔线 / 竖线 / 页眉底线）
C_TEXT = colors.HexColor("#1A1A2E")         # 正文墨色
C_MUTED = colors.HexColor("#5A6472")        # 次级灰（封面元信息 / 页眉页脚）
C_ROW_ALT = colors.HexColor("#F4F7FB")      # 表格斑马纹
C_BORDER = colors.HexColor("#C5CFDC")       # 表格边框
C_CODE_BG = colors.HexColor("#F0F2F5")      # 代码块底色
C_QUOTE_BG = colors.HexColor("#FBF6E6")     # 引用块米色底
C_QUOTE_EDGE = colors.HexColor("#C9A227")   # 引用块金边

# A股语义色（红涨绿跌；评级/信号词映射）
C_UP = colors.HexColor("#C0392B")           # 涨/买入/偏多/优秀
C_DOWN = colors.HexColor("#1E8449")         # 跌/卖出/偏空/恶化
C_NEUTRAL = colors.HexColor("#B07D00")      # 中性/震荡/观望（琥珀）
C_LIGHT_UP = colors.HexColor("#FBEBE9")     # 红底（正数/多方单元格）
C_LIGHT_DOWN = colors.HexColor("#E9F5EE")   # 绿底（负数/空方单元格）
C_LIGHT_NEUTRAL = colors.HexColor("#FBF3DE")  # 琥珀底（中性单元格）

# ---------- 字体（Noto Sans CJK SC 优先，回退文泉驿 / CID） ----------
_FONT_ROOT = Path(__file__).resolve().parents[2] / "docker" / "training" / "fonts"
# 注意：NotoSansCJK.ttf 是 JP 子集（缺 132 简体字），只能做最后回退。
# 首选 ttc 提取的 SC 子字体 TrueType 版（tools/convert_noto_sc_font.py 生成）：
# 子集版（GB2312 6908 字形，2MB）优先；全量版（44810 字形，19MB）兜底
# reportlab 的 splitString 只查主字体 cmap，落到兜底字体会再查一次——所以
# 全量兜底放在子集之后，PDF 里永远不会出现无 cmap 的 CID 回退乱字
_FONT_CANDIDATES = [
    str(_FONT_ROOT / "NotoSansCJKSC-Regular-ttf-subset.ttf"),
    str(_FONT_ROOT / "NotoSansCJKSC-Regular-ttf.ttf"),
    str(_FONT_ROOT / "WQYMicroHei.ttf"),
    str(_FONT_ROOT / "NotoSansCJK.ttf"),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
]
_BOLD_CANDIDATES = [
    str(_FONT_ROOT / "NotoSansCJKSC-Bold-ttf-subset.ttf"),
    str(_FONT_ROOT / "NotoSansCJKSC-Bold-ttf.ttf"),
    str(_FONT_ROOT / "WQYZenHei.ttf"),
]


def _register_cjk_font() -> str:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            try:
                pdfmetrics.registerFont(TTFont("CJK", path))
                for bold_path in _BOLD_CANDIDATES:
                    if Path(bold_path).exists():
                        try:
                            pdfmetrics.registerFont(TTFont("CJK-Bold", bold_path))
                            from reportlab.lib.fonts import addMapping

                            addMapping("CJK", 0, 0, "CJK")
                            addMapping("CJK", 1, 0, "CJK-Bold")
                            addMapping("CJK", 0, 1, "CJK")
                            addMapping("CJK", 1, 1, "CJK-Bold")
                            break
                        except Exception as exc:
                            print(f"注册粗体失败 {bold_path}: {exc}")
                return "CJK"
            except Exception as exc:
                print(f"注册字体失败 {path}: {exc}")
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    return "STSong-Light"


FONT_NAME = _register_cjk_font()


def unescape_html(text: str) -> str:
    # 仅转义，不做 Markdown 解析（代码块用：代码里的 * 是字面量，不能转换）
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_inline_md(text: str) -> str:
    """转义 HTML 特殊字符并应用行内 Markdown 标记（**粗体** / *斜体*）。

    - 全角空格（U+3000）替换为普通空格（reportlab 段落排版会丢失全角空格）
    - emoji 变体选择符（U+FE0F，跟随 emoji 出现）剔除——不在字体子集内，
      reportlab 会渲染成 notdef 方框
    - 先转义再注入 <b>/<i> 标签（比转义后 replace 还原更安全）
    - 所有分支统一走这里：正文/列表/表格/引用/标题内的 ** 都会被转换
    """
    text = text.replace("　", "  ")
    text = text.replace("️", "")  # emoji 变体选择符：不在子集，渲染成方框
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    return text


# ---------- 语义配色（A股红涨绿跌） ----------

_SEMANTIC_KEYWORDS = {
    "up": ("买入", "增持", "优秀", "偏多", "流入", "多头", "低估",
           "buy", "BUY", "bullish", "Bullish"),
    "down": ("卖出", "减持", "恶化", "偏空", "流出", "空头", "高估", "偏高",
             "sell", "SELL", "bearish", "Bearish"),
    "neutral": ("中性", "震荡", "分歧", "观望", "合理", "hold", "HOLD",
                "neutral", "Neutral"),
}

# 含以上关键词但不应配色的长句（如「风险提示：…」全句）
_SEMANTIC_SKIP_PREFIXES = ("风险提示", "数据质量声明", "触发条件")
_SEMANTIC_MAX_LEN = 20


def classify_semantic(text: str) -> str:
    """文本 → 语义类别（'up'/'down'/'neutral'/''，空串=不配色）。

    优先级：负向 > 正向 > 中性；长句和提示类句子不配色，
    避免把整段风险描述染成绿色。"""
    if not text or len(text) > _SEMANTIC_MAX_LEN:
        return ""
    if any(text.startswith(p) for p in _SEMANTIC_SKIP_PREFIXES):
        return ""
    if any(k in text for k in _SEMANTIC_KEYWORDS["down"]):
        return "down"
    if any(k in text for k in _SEMANTIC_KEYWORDS["up"]):
        return "up"
    if any(k in text for k in _SEMANTIC_KEYWORDS["neutral"]):
        return "neutral"
    return ""


_SEMANTIC_TEXT_COLORS = {"up": C_UP, "down": C_DOWN, "neutral": C_NEUTRAL}
_SEMANTIC_BG_COLORS = {
    "up": C_LIGHT_UP,
    "down": C_LIGHT_DOWN,
    "neutral": C_LIGHT_NEUTRAL,
}


# ---------- 数字列右对齐 ----------

_NUM_CELL_RE = re.compile(
    r"^[-+]?\d[\d,]*(?:\.\d+)?(?:\s*(?:%|x|X|pct|亿|万|元|股|手|倍))*$"
)


def is_numeric_cell(text: str) -> bool:
    """判断表格单元格是否纯数值（带可选的单位/百分比后缀，如「1.37 亿股」）。

    数字列右对齐的依据；含说明文字的单元格不算（如「+24.9% 后回落」）。
    """
    t = text.strip().replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    t = t.replace(",", "")
    return bool(_NUM_CELL_RE.match(t))


def cell_numeric_value(text: str) -> float | None:
    """提取单元格开头的数值（用于语义底色的正负判断），无数字返回 None。"""
    t = text.strip().replace("&amp;", "&").replace(",", "")
    m = re.match(r"^([-+]?\d+(?:\.\d+)?)", t)
    return float(m.group(1)) if m else None


# ---------- 表格列宽自适应 + 文字居中 ----------

_TABLE_FONT_PT = 9  # 与 "table"/"th" 样式的 fontSize 一致
_EM_TO_MM = _TABLE_FONT_PT * 0.3528  # 1em（CJK 全宽）= 字号 pt → mm
_CELL_PAD_MM = 8 * 0.3528  # 左右 padding 各 4pt
_MAX_COL_RATIO = 0.40  # 单列最多占可用宽 40%，防长文本列霸占


def display_width_em(text: str) -> float:
    """估算字符串显示宽度（em）：CJK/全角=1.0，ASCII/数字=0.52，空格=0.3。

    Noto Sans 的拉丁字形宽约半角，用于列宽按内容比例分配。
    """
    t = text.replace("**", "")
    w = 0.0
    for ch in t:
        if ord(ch) > 0x2E80:  # CJK 及全角标点
            w += 1.0
        elif ch == " ":
            w += 0.3
        else:
            w += 0.52
    return w


def fit_col_widths(
    header: list[str], data: list[list[str]], available_mm: float
) -> list[float]:
    """按内容估算各列显示宽度，比例分配到可用宽度。

    规则：
    - 每列下限 = 该列最长「不可断词」的宽度（CJK 逐字可断、ASCII 词不可断），
      低于下限文字会竖排
    - 内容总宽 < 可用宽 → 按比例放大填满版面（表格不挤在左边）
    - 内容总宽 > 可用宽 → 按比例压缩，但每列不低于下限（保证可读）

    available_mm = 页面宽 - 左右 margin（A4 210mm - 36mm = 174mm）。
    """
    ncol = len(header)
    col_em: list[float] = []
    col_min_em: list[float] = []
    for c in range(ncol):
        # 内容宽：表头粗体 1.15 倍 + 各数据格
        w = display_width_em(header[c]) * 1.15
        for r in data:
            if c < len(r):
                w = max(w, display_width_em(r[c]))
        col_em.append(w)
        # 下限：最长不可断片段（连续 ASCII/数字词）；CJK 逐字可断不计入
        min_w = max(
            [display_width_em(seg) for cell in [header[c]] + [r[c] for r in data if c < len(r)]
             for seg in _unbreakable_segments(cell)]
            or [0.0]
        )
        col_min_em.append(min_w)
    raw = [em * _EM_TO_MM + _CELL_PAD_MM for em in col_em]
    mins = [em * _EM_TO_MM + _CELL_PAD_MM for em in col_min_em]
    total = sum(raw)
    if total <= available_mm:
        # 未超宽：按比例放大填满版面
        if total > 0:
            scale = available_mm / total
            return [w * scale for w in raw]
        return [available_mm / ncol] * ncol
    # 超宽：按剩余可压空间比例压缩，但不低于各自下限
    widths = list(raw)
    surplus = sum(widths) - available_mm
    while surplus > 0.01:
        squeezable = [i for i in range(ncol) if widths[i] > mins[i] + 0.01]
        if not squeezable:
            break
        room = sum(widths[i] - mins[i] for i in squeezable)
        cut = min(surplus, room)
        for i in squeezable:
            widths[i] -= (widths[i] - mins[i]) * (cut / room)
        surplus = sum(widths) - available_mm
    return widths


def _unbreakable_segments(text: str) -> list[str]:
    """切出单元格里不可断的连续 ASCII/数字片段（CJK 逐字可断不返回）。

    例：「+24.9% 后回落」→ ['+24.9%']；「SELL 4 条」→ ['SELL', '4']。
    """
    return re.findall(r"[^⺀-鿿\s]{2,}", text.replace("**", ""))


# ---------- 封面元信息 ----------

def extract_cover_meta(content: str) -> tuple[str, list[str]]:
    """从 md 提取标题 + 封面元信息（含「报告日期/数据截至」的 blockquote 行）。

    两字段可能分两行，也可能同处一行（全角空格分隔）——都收；
    其余 blockquote（分析框架/免责声明）不上封面（封面底部有统一免责声明）。
    """
    title = "QuantMind 投研分析报告"
    meta_lines: list[str] = []
    for line in content.splitlines():
        s = line.rstrip()
        m = re.match(r"^#\s+(.*)", s)
        if m:
            title = m.group(1).strip()
            continue
        if s.startswith(">"):
            stripped = s.lstrip(">").strip()
            if "报告日期" in stripped or "数据截至" in stripped:
                meta_lines.append(stripped)
    return title, meta_lines


def build_footer_text(page: int, total: int) -> str:
    """页脚：免责声明 + 第 X 页 / 共 Y 页。"""
    disc = "本报告由 QuantMind 自动生成，仅供研究参考，不构成投资建议"
    return f"{disc}　|　第 {page} 页 / 共 {total} 页"


# ---------- 块级换页保护（表/标题不被 LayoutError 打断） ----------

_PT_PER_MM = 2.8346
_AVAIL_W_PT = 174 * _PT_PER_MM   # A4 - 36mm margin
_AVAIL_H_PT = 252 * _PT_PER_MM   # 单帧高度上限
_MEASURE_CANVAS = PDFCanvas(io.BytesIO())


def _measured(flowables: list, min_gap_mm: float = 12) -> list:
    """给块加 CondPageBreak：当前页剩余空间不足时先换页。

    reportlab 的 Table 是不可拆分的 flowable，落在页尾剩余空间不足会直接
    LayoutError 中断生成——量出块的真实高度，空间不够就换页。"""
    total_pt = 0.0
    for fl in flowables:
        try:
            fl.canv = _MEASURE_CANVAS
            _, h = fl.wrap(_AVAIL_W_PT, _AVAIL_H_PT)
            total_pt += h
        except Exception:
            total_pt += 36  # 量不出来按一行高兜底
    return [
        CondPageBreak((total_pt + min_gap_mm * _PT_PER_MM)),
        *flowables,
    ]


# 段落最小空间：一行高(16pt) + 段距(6pt) + 缓冲 → 剩余不足直接换页，
# 否则 Paragraph 装不下首行会 LayoutError（reportlab 不可拆段落报错）
_BODY_MIN_BREAK_PT = 26.0


def _protected_para(markup: str, style: ParagraphStyle) -> list:
    """正文/列表段落 + 最小一行高换页保护。"""
    return [CondPageBreak(_BODY_MIN_BREAK_PT), Paragraph(markup, style)]


# ---------- 章节标题（金色竖线 + 深蓝粗体 + 细分隔线） ----------

def _build_h2(text: str, styles: dict) -> list:
    bar = Table(
        [[""]], colWidths=[0.8 * mm], rowHeights=[7 * mm],
        style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), C_ACCENT)]),
    )
    para = Paragraph(render_inline_md(text), styles["h2"])
    row = Table(
        [[bar, para]], colWidths=[2.8 * mm, None],
        style=TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (0, 0), 1.8 * mm),
            ("RIGHTPADDING", (1, 0), (1, 0), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]),
    )
    return [
        row,
        Spacer(1, 2.5),
        HRFlowable(width="100%", thickness=0.5, color=C_ACCENT),
        Spacer(1, 5),
    ]


def _build_quote(text: str, styles: dict) -> list:
    """引用块 → 米色金边提示框（左侧金色粗竖线 + 浅米底）。"""
    para = Paragraph(render_inline_md(text), styles["blockquote"])
    row = Table(
        [[para]], colWidths=[None],
        style=TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BACKGROUND", (0, 0), (-1, -1), C_QUOTE_BG),
            ("LINEBEFORE", (0, 0), (0, -1), 2.2, C_QUOTE_EDGE),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]),
    )
    return _measured([row, Spacer(1, 6)], min_gap_mm=8)


def _build_code(text: str, styles: dict) -> Paragraph:
    return Paragraph(
        f'<font face="{FONT_NAME}">'
        + unescape_html(text).replace("\n", "<br/>")
        + "</font>",
        styles["code"],
    )


def build_flat_story(content: str, styles: dict) -> list:
    """逐行渲染 markdown → platypus 元素列表。

    每个 H1/H2 flowable 挂 `_qm_heading` 属性（页眉跟踪用），
    H2 块用 KeepTogether 包住防跨页拆分。
    """
    story: list = []
    in_code = False
    code_buf: list[str] = []
    table_buf: list[list[str]] = []
    in_table = False

    def flush_code():
        nonlocal code_buf, in_code
        if code_buf:
            story.append(_build_code("\n".join(code_buf), styles))
            code_buf = []
        in_code = False

    def flush_table():
        nonlocal table_buf, in_table
        if table_buf:
            header = table_buf[0]
            data = table_buf[1:]
            ncol = max(len(r) for r in table_buf)
            rows = [
                [
                    Paragraph(
                        render_inline_md(r[c] if c < len(r) else "").replace("|", ""),
                        styles["table"],
                    )
                    for c in range(ncol)
                ]
                for r in data
            ]
            header_ps = [Paragraph(render_inline_md(h), styles["th"]) for h in header]

            # 语义配色：数据单元格按关键词着色，纯数值按正负着色
            semantic_cells: dict[tuple[int, int], str] = {}
            for r_idx, row in enumerate(data, start=1):
                for c_idx in range(min(ncol, len(row))):
                    kind = classify_semantic(row[c_idx])
                    if not kind:
                        val = cell_numeric_value(row[c_idx])
                        if val is not None:
                            kind = "up" if val > 0 else "down"
                    if kind:
                        semantic_cells[(r_idx, c_idx)] = kind

            # 列宽按内容自适应（可用宽 = A4 210mm - 左右 margin 36mm）
            # fit_col_widths 返回 mm，但 Table colWidths 单位是 points → ×2.8346
            col_widths_mm = fit_col_widths(header, data, 210 - 36)
            t = Table(
                [header_ps] + rows,
                repeatRows=1,
                colWidths=[w * _PT_PER_MM for w in col_widths_mm],
            )
            cmds = [
                ("BACKGROUND", (0, 0), (-1, 0), C_PRIMARY),
                ("LINEBELOW", (0, 0), (-1, 0), 1.2, C_ACCENT),
                ("GRID", (0, 0), (-1, -1), 0.4, C_BORDER),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, C_ROW_ALT]),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
            for (row_i, col_i), kind in semantic_cells.items():
                cmds.append(
                    ("BACKGROUND", (col_i, row_i), (col_i, row_i), _SEMANTIC_BG_COLORS[kind])
                )
                cmds.append(
                    ("TEXTCOLOR", (col_i, row_i), (col_i, row_i), _SEMANTIC_TEXT_COLORS[kind])
                )
            t.setStyle(TableStyle(cmds))
            story.extend(_measured([t, Spacer(1, 8)]))
            table_buf = []
        in_table = False

    for line in content.splitlines():
        s = line.rstrip()
        if s.strip().startswith("```"):
            if in_code:
                flush_code()
            else:
                flush_table()
                in_code = True
            continue
        if in_code:
            code_buf.append(s)
            continue
        if s.strip().startswith("|") and "|" in s:
            cells = [c.strip() for c in s.strip().strip("|").split("|")]
            if not in_table:
                in_table = True
                table_buf = [cells]
            else:
                if all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                    continue  # separator row
                table_buf.append(cells)
            continue
        if in_table:
            flush_table()
        m = re.match(r"^(#{1,6})\s+(.*)", s)
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
            if level == 1:
                para = Paragraph(render_inline_md(text), styles["h1"])
                para._qm_heading = text
                story.append(para)
            else:
                # 标题块量高后加 CondPageBreak：页尾剩余不足时换页，
                # 避免标题竖线条 Table 触发 LayoutError
                h2_flowables = _build_h2(text, styles)
                h2_flowables[0]._qm_heading = text
                story.extend(_measured(h2_flowables, min_gap_mm=14))
            continue
        if s.startswith(">"):
            story.extend(_build_quote(s.lstrip(">").strip(), styles))
            continue
        if s.strip() in ("---", "***", "___"):
            story.append(
                HRFlowable(
                    width="100%", thickness=0.6, color=C_ACCENT,
                    spaceBefore=2, spaceAfter=4,
                )
            )
            continue
        if re.match(r"^[-*]\s+", s):
            story.extend(
                _protected_para(
                    "• " + render_inline_md(re.sub(r"^[-*]\s+", "", s)),
                    styles["body"],
                )
            )
            continue
        if re.match(r"^\d+\.\s+", s):
            story.extend(_protected_para(render_inline_md(s), styles["body"]))
            continue
        if s.strip():
            story.extend(_protected_para(render_inline_md(s), styles["body"]))

    if in_table:
        flush_table()
    if in_code:
        flush_code()
    return story


def main(md_path: str, pdf_path: str) -> None:
    content = Path(md_path).read_text(encoding="utf-8")
    title, meta_lines = extract_cover_meta(content)

    styles = {
        "h1": ParagraphStyle(
            "h1", fontName=FONT_NAME, fontSize=18, leading=26, spaceAfter=8,
            textColor=C_TEXT, alignment=TA_CENTER,
        ),
        "h2": ParagraphStyle(
            "h2", fontName=FONT_NAME, fontSize=14, leading=20,
            spaceBefore=4, spaceAfter=3, textColor=C_PRIMARY,
        ),
        "blockquote": ParagraphStyle(
            "blockquote", fontName=FONT_NAME, fontSize=9.5, leading=15,  # fidelity: allow-limit-threshold — 非阈值：字号 9.5pt
            textColor=C_MUTED, leftIndent=2 * mm,
        ),
        "body": ParagraphStyle(
            "body", fontName=FONT_NAME, fontSize=10, leading=16, spaceAfter=6
        ),
        "table": ParagraphStyle("table", fontName=FONT_NAME, fontSize=9, leading=13),
        "th": ParagraphStyle(
            "th", fontName=FONT_NAME, fontSize=9, leading=13, textColor=colors.white
        ),
        "code": ParagraphStyle(
            "code", fontName=FONT_NAME, fontSize=9, leading=14,
            backColor=C_CODE_BG, spaceAfter=6,
        ),
        "cover_title": ParagraphStyle(
            "cover_title", fontName=FONT_NAME, fontSize=24, leading=34,
            textColor=colors.white, alignment=TA_CENTER,
        ),
        "cover_meta": ParagraphStyle(
            "cover_meta", fontName=FONT_NAME, fontSize=10.5, leading=18,
            textColor=colors.HexColor("#D8E2F0"), alignment=TA_CENTER,
        ),
        "cover_disc": ParagraphStyle(
            "cover_disc", fontName=FONT_NAME, fontSize=9, leading=14,
            textColor=colors.HexColor("#8FA3C0"), alignment=TA_CENTER,
        ),
    }

    def _cover_flowables() -> list:
        """封面 flowables（每遍新建：build 会 mutate 对象，不能复用）。"""
        cover: list = [
            Spacer(1, 58 * mm),
            HRFlowable(
                width=46 * mm, thickness=0.8, color=C_ACCENT,
                spaceBefore=0, spaceAfter=4 * mm,
            ),
            Paragraph(render_inline_md(title), styles["cover_title"]),
            Spacer(1, 5 * mm),
            HRFlowable(
                width=46 * mm, thickness=0.8, color=C_ACCENT,
                spaceBefore=0, spaceAfter=10 * mm,
            ),
        ]
        for line in meta_lines:
            cover.append(Paragraph(render_inline_md(line), styles["cover_meta"]))
        cover += [
            Spacer(1, 46 * mm),
            Paragraph(
                "本报告由 QuantMind 投研分析管线自动生成<br/>"
                "仅供学习研究参考，不构成任何投资建议",
                styles["cover_disc"],
            ),
            PageBreak(),
        ]
        return cover

    # 页眉跟踪：afterFlowable 记录当前页所属章节（按页号，两遍构建各自重建）
    tracker: dict[int, str] = {}

    def _after_flowable(flowable):
        text = getattr(flowable, "_qm_heading", None)
        if text:
            tracker[doc.canv.getPageNumber()] = text

    # 两遍构建：第一遍拿总页数，第二遍页脚带「共 Y 页」。
    # 注意：reportlab build 会 mutate flowable 对象（挂 _frameName 等状态），
    # 同一批 flowable 不能复用两次——每遍都从 markdown 重新构建全新 story
    total_pages = 1

    from reportlab.pdfgen import canvas as _pdfcanvas

    class NumberedCanvas(_pdfcanvas.Canvas):
        """延迟绘制页眉/页脚：先收集每页状态，save() 时统一画（总页数已知）。"""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._saved_page_states = []

        def showPage(self):
            self._saved_page_states.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            num_pages = len(self._saved_page_states)
            for state in self._saved_page_states:
                self.__dict__.update(state)
                self._draw_header_footer(num_pages)
                super().showPage()
            super().save()

        def _heading_for(self, page: int) -> str:
            """当前页所属章节：tracker 按页号记录，缺页向前找最近的章节。"""
            for p in range(page, 0, -1):
                if tracker.get(p):
                    return tracker[p]
            return ""

        def _draw_header_footer(self, total: int):
            page = self._pageNumber
            if page == 1:
                return  # 封面不画页眉页脚
            w, h = PAGE_W, PAGE_H
            # 页眉：金色细线 + 当前章节（跨页延续上一章节）
            self.setStrokeColor(C_ACCENT)
            self.setLineWidth(0.6)
            self.line(18 * mm, h - 15 * mm, w - 18 * mm, h - 15 * mm)
            heading = self._heading_for(page)
            if heading:
                self.setFillColor(C_MUTED)
                self.setFont(FONT_NAME, 7.5)
                self.drawString(18 * mm, h - 13.2 * mm, heading[:38])
            # 页脚：金色细线 + 免责声明 + 页码
            self.setStrokeColor(C_ACCENT)
            self.setLineWidth(0.6)
            self.line(18 * mm, 14 * mm, w - 18 * mm, 14 * mm)
            self.setFillColor(C_MUTED)
            self.setFont(FONT_NAME, 7.5)
            self.drawCentredString(w / 2, 9.5 * mm, build_footer_text(page, total))  # fidelity: allow-limit-threshold — 非阈值：页脚 Y 坐标 9.5mm

    def _canvasmaker(filename, **kwargs):
        return NumberedCanvas(filename, **kwargs)

    def _cover_page(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(C_NAVY)
        canvas.rect(0, 0, PAGE_W, PAGE_H, stroke=0, fill=1)
        canvas.setFillColor(C_ACCENT)
        canvas.rect(0, PAGE_H - 10 * mm, PAGE_W, 1.2 * mm, stroke=0, fill=1)
        canvas.setFillColor(colors.white)
        canvas.setFont(FONT_NAME, 11)
        canvas.drawCentredString(PAGE_W / 2, PAGE_H - 24 * mm, "QuantMind · 量化投研")
        canvas.setFillColor(colors.HexColor("#8FA3C0"))
        canvas.setFont(FONT_NAME, 7.5)
        canvas.drawCentredString(
            PAGE_W / 2, 12 * mm,
            "本报告由 AI 自动生成，仅供研究参考，不构成投资建议",
        )
        canvas.restoreState()

    def _full_story() -> list:
        """每遍构建独立的 story（build 会污染 flowable 状态，不能复用）。"""
        fresh = build_flat_story(content, styles)
        return _cover_flowables() + fresh

    for _ in range(2):
        tracker.clear()
        doc = SimpleDocTemplate(
            pdf_path,
            pagesize=A4,
            leftMargin=18 * mm,
            rightMargin=18 * mm,
            topMargin=20 * mm,
            bottomMargin=20 * mm,
            title=title,
            author="QuantMind 投研分析",
        )
        doc.afterFlowable = _after_flowable
        doc.build(
            _full_story(),
            onFirstPage=_cover_page,
            onLaterPages=lambda c, d: None,
            canvasmaker=_canvasmaker,
        )
        total_pages = doc.page

    print(f"PDF 已生成: {pdf_path}（{total_pages} 页）")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
