#!/usr/bin/env python3
"""Render a confirmed storyboard emotion table from Markdown to PNG."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


WIDTH = 1800
HEIGHT = 1050
MARGIN_LEFT = 130
MARGIN_RIGHT = 80
PLOT_TOP = 200
PLOT_BOTTOM = 790


def find_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size, index=0)
    return ImageFont.load_default()


def parse_intensity(value: str) -> int:
    match = re.search(r"(?<!\d)([1-5])(?!\d)", value)
    if not match:
        raise ValueError(f"无法读取情绪强度：{value!r}")
    return int(match.group(1))


def split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_markdown(path: Path) -> list[dict[str, object]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    header_index = None
    headers: list[str] = []
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            continue
        cells = split_row(line)
        required = {"场次", "主导情绪", "入场强度", "场内峰值", "离场强度"}
        if required.issubset(set(cells)):
            header_index = index
            headers = cells
            break
    if header_index is None:
        raise ValueError("未找到包含场次、主导情绪、入场强度、场内峰值和离场强度的逐场曲线表。")

    records: list[dict[str, object]] = []
    for line in lines[header_index + 1 :]:
        if not line.lstrip().startswith("|"):
            if records:
                break
            continue
        cells = split_row(line)
        if cells and all(re.fullmatch(r":?-+:?", cell) for cell in cells):
            continue
        if len(cells) != len(headers):
            if records:
                break
            continue
        row = dict(zip(headers, cells))
        records.append(
            {
                "scene": row["场次"],
                "emotion": row["主导情绪"],
                "entry": parse_intensity(row["入场强度"]),
                "peak": parse_intensity(row["场内峰值"]),
                "exit": parse_intensity(row["离场强度"]),
            }
        )
    if not records:
        raise ValueError("逐场曲线表没有可绘制的数据行。")
    return records


def text_center(draw: ImageDraw.ImageDraw, xy: tuple[float, float], value: str, font, fill):
    box = draw.textbbox((0, 0), value, font=font)
    draw.text((xy[0] - (box[2] - box[0]) / 2, xy[1] - (box[3] - box[1]) / 2), value, font=font, fill=fill)


def scene_label(value: object) -> str:
    scene = str(value).strip()
    return f"第{scene}场" if scene.isdecimal() else scene


def render(records: list[dict[str, object]], output: Path, title: str, *, branding: bool = True) -> None:
    canvas_width = max(WIDTH, MARGIN_LEFT + MARGIN_RIGHT + len(records) * 140)
    image = Image.new("RGB", (canvas_width, HEIGHT), "#F7F6F2")
    draw = ImageDraw.Draw(image)
    font_title = find_font(44, bold=True)
    font_axis = find_font(25)
    font_small = find_font(21)
    font_scene = find_font(25, bold=True)
    font_emotion = find_font(21)

    draw.text((MARGIN_LEFT, 50), title, font=font_title, fill="#20252B")
    draw.text((MARGIN_LEFT, 105), "强度：1 极低 · 2 偏低 · 3 中等 · 4 偏高 · 5 峰值", font=font_small, fill="#68717B")

    plot_width = canvas_width - MARGIN_LEFT - MARGIN_RIGHT
    plot_height = PLOT_BOTTOM - PLOT_TOP
    total_points = len(records) * 3
    step = plot_width / max(total_points - 1, 1)

    def y_of(level: int) -> float:
        return PLOT_BOTTOM - (level - 1) * plot_height / 4

    for index, record in enumerate(records):
        start = MARGIN_LEFT + (index * 3 - 0.5) * step if index else MARGIN_LEFT
        end = MARGIN_LEFT + (index * 3 + 2.5) * step if index < len(records) - 1 else canvas_width - MARGIN_RIGHT
        if index % 2 == 0:
            draw.rectangle((start, PLOT_TOP, end, PLOT_BOTTOM), fill="#EEF1F3")

    for level in range(1, 6):
        y = y_of(level)
        draw.line((MARGIN_LEFT, y, canvas_width - MARGIN_RIGHT, y), fill="#CBD1D6", width=2)
        text_center(draw, (MARGIN_LEFT - 55, y), str(level), font_axis, "#39424B")

    points: list[tuple[float, float, int, str]] = []
    stage_names = ("入", "峰", "离")
    for scene_index, record in enumerate(records):
        levels = (int(record["entry"]), int(record["peak"]), int(record["exit"]))
        for stage_index, level in enumerate(levels):
            point_index = scene_index * 3 + stage_index
            x = MARGIN_LEFT + point_index * step
            points.append((x, y_of(level), level, stage_names[stage_index]))

    draw.line([(x, y) for x, y, _, _ in points], fill="#B6413C", width=7, joint="curve")
    global_peak = max(level for _, _, level, _ in points)
    for x, y, level, stage in points:
        radius = 11 if level < global_peak else 15
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill="#F7F6F2", outline="#B6413C", width=5)
        label_y = y + 38 if level == 5 else y - 35
        text_center(draw, (x, label_y), f"{stage}{level}", font_small, "#7C2E2A")

    for index, record in enumerate(records):
        center_x = MARGIN_LEFT + (index * 3 + 1) * step
        text_center(draw, (center_x, 845), scene_label(record['scene']), font_scene, "#20252B")
        emotion = str(record["emotion"])
        if len(emotion) > 14:
            emotion = emotion[:13] + "…"
        text_center(draw, (center_x, 890), emotion, font_emotion, "#58616B")
        if index < len(records) - 1:
            boundary_x = MARGIN_LEFT + (index * 3 + 2.5) * step
            draw.line((boundary_x, PLOT_TOP, boundary_x, PLOT_BOTTOM), fill="#AEB6BD", width=2)

    draw.text((MARGIN_LEFT, 965), "数据源：storyboard_global_analysis.md 已确认逐场曲线表", font=font_small, fill="#7A838B")
    if branding:
        label = "DIAGONAL / DIRECTOR MASTER"
        box = draw.textbbox((0, 0), label, font=font_small)
        draw.text((canvas_width - MARGIN_RIGHT - (box[2] - box[0]), 1005),
                  label, font=font_small, fill="#92999E")
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, format="PNG", optimize=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="把已确认的全片情绪曲线Markdown表绘制为PNG。")
    parser.add_argument("input", type=Path, help="storyboard_global_analysis.md 路径")
    parser.add_argument("output", type=Path, help="storyboard_emotion_curve.png 路径")
    parser.add_argument("--title", default="全片情绪曲线", help="图片标题")
    parser.add_argument("--no-branding", action="store_true", help="不绘制底部 DIAGONAL 工具署名")
    args = parser.parse_args()
    render(parse_markdown(args.input), args.output, args.title, branding=not args.no_branding)
    print(f"已生成：{args.output}")


if __name__ == "__main__":
    main()
