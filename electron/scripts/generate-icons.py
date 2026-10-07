#!/usr/bin/env python3
"""生成 Electron 应用图标的多尺寸 PNG 与多分辨率 ICO。

背景：Windows 的任务栏/桌面/Alt-Tab 会各自取最接近的原生尺寸渲染图标。
若 .ico 里只有 256×256 单一尺寸，系统只能把小尺寸场景的 256 硬缩放，
导致桌面/任务栏图标发虚。此脚本按标准尺寸逐一高质量下采样后打包。

源图：electron/build/logo.png（正方形，建议 >= 1024×1024）。

产物：
  electron/build/icons/icon-{16,24,32,48,64,128,256,512}.png   # 完整品牌图（含文字）
  electron/build/logo.ico                                     # Windows 多分辨率（16~256，完整图）
  electron/public/favicon.ico                                 # 浏览器 favicon（16/32/48）
  electron/build/icons-glyph/icon-*.png                       # 「仅 Q 图形标」小尺寸变体
  electron/build/logo-glyph.ico                               # 同上变体的 ICO

说明：完整品牌图含 "QuantMind" 文字与标语，在 <=48px（任务栏/桌面）会糊成一团。
`icons-glyph/` 与 `logo-glyph.ico` 只保留 Q + 走势图图形标，供小尺寸场景选用；
若希望桌面/任务栏更清晰，可把 `build/logo.ico` 换成 `logo-glyph.ico`。

用法（需 Pillow）：
  python electron/scripts/generate-icons.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ELECTRON_DIR = Path(__file__).resolve().parents[1]
SRC = ELECTRON_DIR / "build" / "logo.png"

# 输出的 PNG 尺寸（覆盖 Windows/macOS/Linux 常见档位）
PNG_SIZES = (16, 24, 32, 48, 64, 128, 256, 512)
# ICO 内嵌尺寸（ICO 上限 256）
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
FAVICON_SIZES = (16, 32, 48)

# 从 1024 源图中裁出「Q + 走势图」图形标（去掉下方 QuantMind 文字与标语）的像素框
GLYPH_CROP = (160, 150, 890, 585)

RESAMPLE = Image.Resampling.LANCZOS


def build_glyph(src: Image.Image) -> Image.Image:
    """裁出图形标并居中补成正方形（保留透明边）。"""
    crop = src.crop(GLYPH_CROP)
    w, h = crop.size
    side = max(w, h)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(crop, ((side - w) // 2, (side - h) // 2))
    return canvas


def main() -> int:
    if not SRC.exists():
        print(f"[ERROR] 缺少源图: {SRC}", file=sys.stderr)
        return 1

    src = Image.open(SRC).convert("RGBA")
    if src.width != src.height:
        print(f"[ERROR] 源图需为正方形，当前 {src.width}x{src.height}", file=sys.stderr)
        return 1
    if src.width < 512:
        print(
            f"[ERROR] 源图分辨率过低（{src.width}px），建议使用 >=1024px 的图",
            file=sys.stderr,
        )
        return 1

    icons_dir = ELECTRON_DIR / "build" / "icons"
    icons_dir.mkdir(parents=True, exist_ok=True)

    for size in PNG_SIZES:
        out = icons_dir / f"icon-{size}.png"
        src.resize((size, size), RESAMPLE).save(out, format="PNG", optimize=True)
        print(f"[OK] {out.relative_to(ELECTRON_DIR)}")

    # 多分辨率 ICO：从 1024 源图逐尺寸下采样打包
    ico_out = ELECTRON_DIR / "build" / "logo.ico"
    src.save(ico_out, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    print(f"[OK] {ico_out.relative_to(ELECTRON_DIR)}  sizes={list(ICO_SIZES)}")

    favicon_out = ELECTRON_DIR / "public" / "favicon.ico"
    src.save(favicon_out, format="ICO", sizes=[(s, s) for s in FAVICON_SIZES])
    print(f"[OK] {favicon_out.relative_to(ELECTRON_DIR)}  sizes={list(FAVICON_SIZES)}")

    # 「仅图形标」小尺寸变体（不含文字），用于任务栏/桌面等小尺寸场景
    glyph = build_glyph(src)
    glyph_dir = ELECTRON_DIR / "build" / "icons-glyph"
    glyph_dir.mkdir(parents=True, exist_ok=True)
    for size in PNG_SIZES:
        out = glyph_dir / f"icon-{size}.png"
        glyph.resize((size, size), RESAMPLE).save(out, format="PNG", optimize=True)
        print(f"[OK] {out.relative_to(ELECTRON_DIR)}")

    glyph_ico = ELECTRON_DIR / "build" / "logo-glyph.ico"
    glyph.save(glyph_ico, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    print(f"[OK] {glyph_ico.relative_to(ELECTRON_DIR)}  sizes={list(ICO_SIZES)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
