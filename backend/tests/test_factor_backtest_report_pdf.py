"""单测：报告 PDF 导出的 Markdown 装配与落盘（纯函数优先，渲染 smoke 殿后）。

钉死的边：

- **装配单源**：Markdown 里的每个数字逐值与 ``build_report_block`` 产出相等
  （本模块不重算统计量；改排版不改数字）；
- **封面契约**：引用行同时含「报告日期」「数据截至」（md_to_pdf 封面提取靠
  它们）；样本内/样本外标注必须出现（因子×市场适配第一眼信息）；
- **降级拒绝**：降级块（available=False）装配与导出都直接 ValueError，
  绝不产出「无数字的 PDF」；
- **文件名确定性**：同 run 同名（幂等覆盖），字符清洗不让路径分隔溜进名字；
- **禁用词**：PDF 面向用户，正文不含内部工单号；
- **字形纪律**：正文禁希腊字母（μ/σ/Π 在嵌入 CJK 字体里渲染成豆腐块）；
- **表格纪律**：每张表逐行 `|` 计数一致（裸竖线会撕列，动态值过 `_cell`，
  静态常量由测试兜底）；
- **落盘 smoke**：真实渲染（reportlab）→ %PDF 魔数 + 幂等重导出（无 reportlab
  的环境自动 skip）。
"""

from __future__ import annotations

import pytest

rp = pytest.importorskip("backend.services.engine.factor_backtest.report")
rpdf = pytest.importorskip("backend.services.engine.factor_backtest.report_pdf")
from backend.tests.test_factor_backtest_report import _golden_inputs


def _run_with_cover(run: dict) -> dict:
    """给金样 run 补封面字段（金样 run 只带装配层需要的最小键）。"""
    return {
        **run,
        "factor_name": "动量强度A",
        "market": "a_share",
        "universe": "csi300",
        "date_range": "2024-01-01~2024-06-30",
        "data_source": "qlib_bin",
        "batch_id": None,
    }


def _block_and_md(**kwargs) -> tuple[dict, dict, str]:
    run, series = _golden_inputs()
    run = _run_with_cover(run)
    block = rp.build_report_block(run, series, **kwargs)
    md = rpdf.build_report_markdown(run, series, block, generated_at="2026-10-11")
    return run, block, md


def test_装配_核心数值与封面口径():
    run, block, md = _block_and_md()
    h = block["headline"]

    assert md.startswith("# 因子回测报告 · 动量强度A")
    assert "**报告日期**" in md and "**数据截至**" in md
    assert "2026-10-11" in md
    # 数据截至 = 序列最后一天（不是 date_range 文本）
    _, series = _golden_inputs()
    assert series["dates"][-1] in md

    # 核心指标逐值进表（百分比两位小数，与块内数值同源）
    assert f"{h['returns'] * 100:.2f}%" in md
    assert f"{h['cum_return'] * 100:.2f}%" in md
    assert f"{h['ir']:.2f}" in md
    assert f"{h['turnover'] * 100:.2f}%" in md

    # 样本内标注（a_share = 挖掘原始市场）与市场中文标签
    assert "沪深A股" in md and "样本内" in md

    # 显著性：NW 行与 p 值逐值进表；判定口径句必须在
    sig = block["significance"]
    assert f"{sig['nw_t']:.2f}" in md
    assert f"{sig['p_value']:.4f}" in md
    assert "显著性判定以此行为准" in md

    # 暂缺清单与免责
    assert "容量测算" in md
    assert "不构成投资建议" in md


def test_装配_试次数来源与族注进入DSR行():
    _, block, md = _block_and_md(n_trials=8, n_trials_source="param")
    assert block["significance"]["n_trials"] == 8
    assert "试次数 N=8（显式指定参数）" in md
    sig = block["significance"]
    assert f"{sig['q_value_bhy']:.4f}" in md
    assert "无批次族上下文" in md  # 金样无族 → q=p 的口径句


def test_装配_成本网格默认档标注与盈亏平衡():
    _, block, md = _block_and_md()
    cost = block["cost_grid"]
    assert "（默认）" in md
    assert f"{cost['default_bps']:.2f}（默认）" in md
    assert f"盈亏平衡成本约 {cost['break_even_bps']:.2f} bps" in md
    # 成本档每行净收益逐值进表
    for row in cost["rows"]:
        assert f"{row['net_return'] * 100:.2f}%" in md


def test_装配_禁用词与降级拒绝():
    _, _, md = _block_and_md()
    assert "T-FB" not in md
    assert "工单" not in md

    degraded = rp.build_report_block(
        {"run_id": "r", "status": "failed", "error": "engine_restarted_mid_run"},
        None,
    )
    with pytest.raises(ValueError):
        rpdf.build_report_markdown({"run_id": "r"}, None, degraded)
    # 导出层的 ValueError 必须带业务原因原文（router 409 detail 直接取自它）
    with pytest.raises(ValueError, match="engine_restarted_mid_run"):
        rpdf.export_report_pdf({"run_id": "r"}, None, degraded)


def test_装配_字形纪律_无希腊字母():
    """μ/σ/Π 在嵌入式 CJK 字体里没有字形，会渲染成豆腐块（实测缺陷）。

    字形纪律：口径列用中文词写开（「均值/标准差/逐日连乘」），正文禁整个
    Greek and Coptic 区段（U+0370–U+03FF）——比逐个禁字更难绕过。
    """
    _, _, md = _block_and_md()
    greek = [(ch, f"U+{ord(ch):04X}") for ch in md if 0x0370 <= ord(ch) <= 0x03FF]
    assert greek == [], f"正文含希腊字母（PDF 渲染会成豆腐块）: {greek}"


def test_装配_表格纪律_每个表格竖线数一致():
    """口径文本里的裸 ``|`` 会把表格行撕成多列（实测缺陷）。

    表格纪律：每一段连续 ``|`` 行内，各行的 ``|`` 计数必须完全一致
    （动态值已由 ``_cell()`` 换全角，静态常量表由本测试兜底）。
    """
    _, _, md = _block_and_md()

    blocks: list[list[str]] = []
    current: list[str] = []
    for line in md.splitlines():
        if line.startswith("|"):
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)

    assert blocks, "金样报告应至少含一张表格"
    for idx, table in enumerate(blocks, start=1):
        counts = sorted({line.count("|") for line in table})
        assert len(counts) == 1, (
            f"第 {idx} 张表格竖线数不一致（会撕列）: {counts}\n" + "\n".join(table)
        )


def test_文件名_确定性与清洗():
    run = _run_with_cover(_golden_inputs()[0])
    name = rpdf.report_pdf_filename(run)
    assert name == "因子回测_动量强度A_f-golden_a_share_fb-golde.pdf"
    assert rpdf.report_pdf_filename(run) == name  # 确定性

    dirty = {**run, "factor_name": "a/b:c *d|e  f", "market": "us_stock"}
    cleaned = rpdf.report_pdf_filename(dirty)
    assert "/" not in cleaned and "|" not in cleaned and " " not in cleaned
    assert cleaned.endswith(".pdf")


def test_导出落盘_PDF魔数与幂等覆盖(tmp_path, monkeypatch):
    pytest.importorskip("reportlab")
    monkeypatch.setenv("TRADING_AGENTS_RESULTS_DIR", str(tmp_path))

    run, series = _golden_inputs()
    run = _run_with_cover(run)
    block = rp.build_report_block(run, series)

    path, filename = rpdf.export_report_pdf(
        run, series, block, generated_at="2026-10-11"
    )
    assert path.parent == tmp_path / "因子研究"
    assert path.name == filename == rpdf.report_pdf_filename(run)
    data = path.read_bytes()
    assert data[:5] == b"%PDF-"
    assert len(data) > 1000
    # 无临时残渣（同目录 .pdf_tmp_ 已被 os.replace 收走）
    assert not list(path.parent.glob(".pdf_tmp_*"))

    # 幂等：二次导出同路径覆盖，不产生第二个文件
    path2, _ = rpdf.export_report_pdf(run, series, block, generated_at="2026-10-12")
    assert path2 == path
    assert [p.name for p in path.parent.iterdir()] == [filename]
    assert path.read_bytes()[:5] == b"%PDF-"
