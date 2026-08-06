from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def render_docx(docx_path: str | Path, output_path: str | Path) -> Path:
    """Render a lightweight placeholder PNG for legacy acceptance scripts.

    The production Word export is verified by opening the .docx structure in tests;
    this helper only keeps older smoke scripts importable on machines without a
    full Office/LibreOffice renderer.
    """
    source = Path(docx_path)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (1000, 1400), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 28)
    except Exception:
        font = ImageFont.load_default()
    draw.text((60, 60), f"Word render placeholder\n{source.name}", fill="black", font=font)
    image.save(output)
    return output
