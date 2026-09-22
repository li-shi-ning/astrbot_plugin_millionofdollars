"""卡图合成。

身份揭露时把本轮参与抢劫的角色卡合并成**一张**图片发送；卡片顺序由调用方
打乱，避免固定顺序暗示任何玩家与角色的对应关系。

本模块不导入 AstrBot 或 botpy：输出目录与随机源都由调用方提供。
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Sequence
from pathlib import Path

CARD_HEIGHT = 402
MAX_PER_ROW = 5
GAP = 10
MARGIN = 14
BACKGROUND = (24, 26, 32)
LABEL_HEIGHT = 46
LABEL_BACKGROUND = (24, 26, 32)
LABEL_COLOR = (240, 240, 240)

def _cjk_font_candidates() -> tuple[str, ...]:
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ]
    windir = os.environ.get("WINDIR")
    if windir:
        candidates.insert(0, str(Path(windir) / "Fonts" / "msyh.ttc"))
    return tuple(candidates)


CJK_FONT_CANDIDATES = _cjk_font_candidates()


class CardImageError(RuntimeError):
    """卡图合成失败。"""


def shuffled(paths: Sequence[Path], rng: secrets.SystemRandom | None = None) -> list[Path]:
    """用系统熵源打乱卡片顺序。"""
    result = list(paths)
    (rng or secrets.SystemRandom()).shuffle(result)
    return result


def compose_grid(
    image_paths: Sequence[Path],
    output_path: Path,
    *,
    labels: Sequence[str] | None = None,
    card_height: int = CARD_HEIGHT,
    max_per_row: int = MAX_PER_ROW,
) -> Path:
    """把多张卡图合并成一张图片（超过一行时自动换行）。

    Args:
        image_paths: 卡片图片路径，顺序即为最终展示顺序。
        output_path: 输出文件路径，父目录不存在时自动创建。
        labels: 可选的卡片标题；只有系统存在中文字体时才绘制。
        card_height: 统一缩放后的卡片高度。
        max_per_row: 每行最多放几张卡。

    Returns:
        输出文件路径。
    """
    from PIL import Image  # 延迟导入：纯逻辑测试不依赖 Pillow

    if not image_paths:
        raise CardImageError("没有可合成的卡图。")
    if max_per_row < 1:
        raise CardImageError("max_per_row 必须大于 0。")

    cards = []
    for path in image_paths:
        if not path.is_file():
            raise CardImageError(f"卡图不存在：{path}")
        image = Image.open(path).convert("RGB")
        ratio = card_height / image.height
        cards.append(image.resize((max(1, round(image.width * ratio)), card_height)))

    font = _label_font(round(card_height * 0.075))
    draw_labels = labels is not None and font is not None
    label_height = LABEL_HEIGHT if draw_labels else 0

    rows = [
        cards[index : index + max_per_row]
        for index in range(0, len(cards), max_per_row)
    ]
    row_widths = [
        sum(card.width for card in row) + GAP * (len(row) - 1) for row in rows
    ]
    width = MARGIN * 2 + max(row_widths)
    height = (
        MARGIN * 2
        + len(rows) * (card_height + label_height)
        + GAP * (len(rows) - 1)
    )
    canvas = Image.new("RGB", (width, height), BACKGROUND)

    y = MARGIN
    index = 0
    for row, row_width in zip(rows, row_widths, strict=True):
        x = MARGIN + (width - MARGIN * 2 - row_width) // 2
        for card in row:
            canvas.paste(card, (x, y))
            if draw_labels:
                _draw_label(
                    canvas,
                    font,
                    str(labels[index]) if index < len(labels) else "",
                    x,
                    y + card_height,
                    card.width,
                    label_height,
                )
            x += card.width + GAP
            index += 1
        y += card_height + label_height + GAP

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="JPEG", quality=88)
    return output_path


def _draw_label(canvas, font, text: str, x: int, y: int, width: int, height: int) -> None:
    from PIL import ImageDraw

    draw = ImageDraw.Draw(canvas)
    draw.rectangle([x, y, x + width, y + height], fill=LABEL_BACKGROUND)
    if not text:
        return
    box = draw.textbbox((0, 0), text, font=font)
    text_width = box[2] - box[0]
    text_height = box[3] - box[1]
    draw.text(
        (x + (width - text_width) / 2 - box[0], y + (height - text_height) / 2 - box[1]),
        text,
        font=font,
        fill=LABEL_COLOR,
    )


def _label_font(size: int):
    """系统存在中文字体时返回字体对象，否则返回 ``None``（不绘制文字）。"""
    from PIL import ImageFont

    for candidate in CJK_FONT_CANDIDATES:
        path = Path(candidate)
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:  # pragma: no cover - 字体损坏时跳过
                continue
    return None
