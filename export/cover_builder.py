from __future__ import annotations

import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def find_font() -> str | None:
    candidates = [
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/simsun.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
    ]
    return next((str(path) for path in candidates if path.exists()), None)


def add_cover_title(source: Path, title: str, output: Path) -> Path:
    image = Image.open(source).convert("RGB")
    image.thumbnail((1536, 1024), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (1536, 1024), (28, 28, 32))
    offset = ((1536 - image.width) // 2, (1024 - image.height) // 2)
    canvas.paste(image, offset)
    draw = ImageDraw.Draw(canvas, "RGBA")
    font_path = find_font()
    font = ImageFont.truetype(font_path, 58) if font_path else ImageFont.load_default()
    lines = textwrap.wrap(title, width=18)[:3]
    box_height = max(150, 100 + len(lines) * 78)
    draw.rounded_rectangle((65, 1024 - box_height - 60, 1471, 964), radius=24, fill=(0, 0, 0, 170))
    y = 1024 - box_height - 28
    for line in lines:
        draw.text((105, y), line, font=font, fill=(255, 247, 214, 255), stroke_width=2, stroke_fill=(104, 35, 27, 255))
        y += 76
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output, format="PNG")
    return output
