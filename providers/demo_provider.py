from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


class DemoImageProvider:
    """没有图片 API 时用于本地联调，输出会明确标记为 DEMO。"""

    def generate(self, prompt: str, output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (1536, 1024), (171, 43, 36))
        draw = ImageDraw.Draw(image)
        draw.rectangle((80, 80, 1456, 944), fill=(218, 100, 46), outline=(255, 214, 117), width=12)
        draw.ellipse((520, 210, 1016, 706), fill=(244, 185, 86), outline=(105, 36, 34), width=16)
        draw.text((120, 790), "DEMO IMAGE · 请配置图片 API", fill=(255, 242, 202))
        image.save(output_path, format="PNG")
        return output_path
