from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


WIDTH = 1800
HEIGHT = 2200
BACKGROUND = "#f4f6f8"
INK = "#17202a"
MUTED = "#53606d"
LINE = "#7b8794"
GROUP_BORDER = "#aeb7c2"
GROUP_FILL = "#ffffff"

FONT_PATH = Path(r"C:\Windows\Fonts\msyh.ttc")


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = Path(r"C:\Windows\Fonts\msyhbd.ttc") if bold else FONT_PATH
    return ImageFont.truetype(str(path), size=size)


TITLE_FONT = font(42, bold=True)
GROUP_FONT = font(27, bold=True)
NODE_FONT = font(21, bold=True)
SMALL_FONT = font(17)
TINY_FONT = font(15)


image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
draw = ImageDraw.Draw(image)


def centered_text(
    box: tuple[int, int, int, int],
    title: str,
    subtitle: str = "",
    title_font: ImageFont.FreeTypeFont = NODE_FONT,
    subtitle_font: ImageFont.FreeTypeFont = SMALL_FONT,
    title_color: str = INK,
    subtitle_color: str = MUTED,
) -> None:
    x1, y1, x2, y2 = box
    title_bbox = draw.multiline_textbbox((0, 0), title, font=title_font, spacing=4, align="center")
    title_h = title_bbox[3] - title_bbox[1]
    subtitle_h = 0
    gap = 0
    if subtitle:
        subtitle_bbox = draw.multiline_textbbox(
            (0, 0), subtitle, font=subtitle_font, spacing=3, align="center"
        )
        subtitle_h = subtitle_bbox[3] - subtitle_bbox[1]
        gap = 7
    total_h = title_h + gap + subtitle_h
    top = y1 + ((y2 - y1) - total_h) / 2
    draw.multiline_text(
        ((x1 + x2) / 2, top),
        title,
        font=title_font,
        fill=title_color,
        anchor="ma",
        align="center",
        spacing=4,
    )
    if subtitle:
        draw.multiline_text(
            ((x1 + x2) / 2, top + title_h + gap),
            subtitle,
            font=subtitle_font,
            fill=subtitle_color,
            anchor="ma",
            align="center",
            spacing=3,
        )


def group(box: tuple[int, int, int, int], label: str, accent: str) -> None:
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(box, radius=10, fill=GROUP_FILL, outline=GROUP_BORDER, width=2)
    draw.rectangle((x1, y1, x1 + 9, y2), fill=accent)
    draw.text((x1 + 28, y1 + 18), label, font=GROUP_FONT, fill=INK)


def node(
    box: tuple[int, int, int, int],
    title: str,
    subtitle: str = "",
    accent: str = "#3b5ccc",
    fill: str = "#ffffff",
    border_width: int = 3,
) -> None:
    draw.rounded_rectangle(box, radius=8, fill=fill, outline=accent, width=border_width)
    centered_text(box, title, subtitle)


def line_segment(
    start: tuple[float, float],
    end: tuple[float, float],
    color: str,
    width: int,
    dashed: bool,
) -> None:
    if not dashed:
        draw.line((start, end), fill=color, width=width)
        return
    x1, y1 = start
    x2, y2 = end
    length = math.hypot(x2 - x1, y2 - y1)
    if length == 0:
        return
    dash = 12
    gap = 8
    position = 0.0
    while position < length:
        next_position = min(position + dash, length)
        sx = x1 + (x2 - x1) * position / length
        sy = y1 + (y2 - y1) * position / length
        ex = x1 + (x2 - x1) * next_position / length
        ey = y1 + (y2 - y1) * next_position / length
        draw.line(((sx, sy), (ex, ey)), fill=color, width=width)
        position += dash + gap


def arrow(
    points: list[tuple[float, float]],
    color: str = LINE,
    width: int = 3,
    dashed: bool = False,
    head: bool = True,
) -> None:
    for start, end in zip(points, points[1:]):
        line_segment(start, end, color, width, dashed)
    if not head or len(points) < 2:
        return
    x1, y1 = points[-2]
    x2, y2 = points[-1]
    angle = math.atan2(y2 - y1, x2 - x1)
    size = 13
    left = (x2 - size * math.cos(angle - 0.55), y2 - size * math.sin(angle - 0.55))
    right = (x2 - size * math.cos(angle + 0.55), y2 - size * math.sin(angle + 0.55))
    draw.polygon(((x2, y2), left, right), fill=color)


def center(box: tuple[int, int, int, int]) -> tuple[float, float]:
    return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)


def top(box: tuple[int, int, int, int]) -> tuple[float, float]:
    return ((box[0] + box[2]) / 2, box[1])


def bottom(box: tuple[int, int, int, int]) -> tuple[float, float]:
    return ((box[0] + box[2]) / 2, box[3])


def left(box: tuple[int, int, int, int]) -> tuple[float, float]:
    return (box[0], (box[1] + box[3]) / 2)


def right(box: tuple[int, int, int, int]) -> tuple[float, float]:
    return (box[2], (box[1] + box[3]) / 2)


draw.text((WIDTH / 2, 42), "LegaSynth 完整生成流程", font=TITLE_FONT, fill=INK, anchor="ma")
draw.text(
    (WIDTH / 2, 98),
    "唯一规划 LLM · MuseCoco 主题生成 · Python 确定性装配 · MIDI-GPT 补全",
    font=SMALL_FONT,
    fill=MUTED,
    anchor="ma",
)

heart_input = (250, 145, 700, 245)
story_input = (1100, 145, 1550, 245)
node(heart_input, "输入一：心音 WAV", "听诊器采集的真实心音", "#1b7f5c", "#f2fbf7")
node(story_input, "输入二：用户故事", "自然语言叙事", "#3b5ccc", "#f3f6ff")

stage1 = (70, 300, 1730, 1040)
group(stage1, "阶段一：双链并行准备", "#3b5ccc")

heart_lane = (105, 375, 850, 1000)
story_lane = (950, 375, 1695, 1000)
draw.rounded_rectangle(heart_lane, radius=8, fill="#f7fcf9", outline="#b8d7ca", width=2)
draw.rounded_rectangle(story_lane, radius=8, fill="#f7f9ff", outline="#c2cbed", width=2)
draw.text((135, 397), "心音素材链", font=NODE_FONT, fill="#145c45")
draw.text((980, 397), "故事规划与主题链", font=NODE_FONT, fill="#2d479e")

h1 = (190, 465, 765, 555)
h2 = (190, 590, 765, 680)
h3 = (190, 715, 765, 805)
hevents = (190, 850, 765, 955)
node(h1, "heart_extraction", "稳定区间与 S1/S2 提取", "#1b7f5c")
node(h2, "tempo_bar_renderer", "BPM、拍号与完整事件排列", "#1b7f5c")
node(h3, "heartbeat_midi_exporter", "符号事件 MIDI 与患者 SF2", "#1b7f5c")
node(hevents, "S1/S2 心音事件素材", "MIDI＋事件清单＋SF2", "#1b7f5c", "#eaf7f1")
arrow([bottom(h1), top(h2)], "#1b7f5c")
arrow([bottom(h2), top(h3)], "#1b7f5c")
arrow([bottom(h3), top(hevents)], "#1b7f5c")

llm = (1035, 465, 1610, 555)
cplan = (985, 610, 1295, 710)
mplan = (1350, 610, 1660, 710)
muse = (1035, 760, 1610, 850)
motifs = (1035, 895, 1505, 975)
motif_check = (1530, 895, 1660, 975)
node(llm, "唯一规划 LLM", "一次规划：内容、主题、心音与填充区域", "#3b5ccc")
node(cplan, "content_plan.json", "曲式＋MuseCoco 要求", "#3b5ccc", "#eef2ff")
node(mplan, "music_plan.json", "心音＋放置＋填充计划", "#3b5ccc", "#eef2ff")
node(muse, "MuseCoco", "按受控文本生成主题动机", "#7a4bb7")
node(motifs, "主题动机素材", "MIDI＋motif manifest", "#7a4bb7", "#f7f1fc")
node(motif_check, "可选检查", "边界\n重复", "#7a4bb7", "#ffffff")

branch_y = 585
arrow([bottom(llm), (center(llm)[0], branch_y), (center(cplan)[0], branch_y), top(cplan)], "#3b5ccc")
arrow([bottom(llm), (center(llm)[0], branch_y), (center(mplan)[0], branch_y), top(mplan)], "#3b5ccc")
arrow([bottom(cplan), (center(cplan)[0], 735), (center(muse)[0], 735), top(muse)], "#3b5ccc")
arrow([bottom(muse), top(motifs)], "#7a4bb7")
arrow([right(motifs), left(motif_check)], "#7a4bb7", dashed=True)

arrow([bottom(heart_input), (center(heart_input)[0], 345), (center(h1)[0], 345), top(h1)], "#1b7f5c")
arrow([bottom(story_input), (center(story_input)[0], 345), (center(llm)[0], 345), top(llm)], "#3b5ccc")
arrow([left(cplan), (900, center(cplan)[1]), (900, center(h2)[1]), right(h2)], "#6b7280", dashed=True)
draw.text((868, 555), "BPM / 拍号", font=TINY_FONT, fill=MUTED, anchor="mm")

stage2 = (70, 1110, 1730, 1465)
group(stage2, "阶段二：Python 确定性框架装配", "#b56a16")

merge = (900, 1072)
arrow([bottom(hevents), (center(hevents)[0], 1055), merge], "#1b7f5c", width=2, head=False)
arrow([left(cplan), (925, center(cplan)[1]), (925, 1055), merge], "#3b5ccc", width=2, head=False)
arrow([right(mplan), (1715, center(mplan)[1]), (1715, 1055), merge], "#3b5ccc", width=2, head=False)
arrow([bottom(motifs), (center(motifs)[0], 1055), merge], "#7a4bb7", width=2, head=False)
draw.ellipse((890, 1062, 910, 1082), fill="#b56a16")

boxes2 = [
    (100, 1215, 335, 1325),
    (370, 1215, 605, 1325),
    (640, 1215, 875, 1325),
    (910, 1215, 1145, 1325),
    (1180, 1215, 1415, 1325),
    (1450, 1215, 1690, 1325),
]
titles2 = [
    ("装配输入", "两个计划\n心音＋动机"),
    ("统一坐标", "PPQ、BPM\n拍号与长度"),
    ("渲染心音", "逐段拍点\nS1/S2 与力度"),
    ("放置动机", "目标轨道\n小节与重复"),
    ("保护策略", "锚点与\n可生成区域"),
    ("编译框架", "Scaffold、Score\nGenerationRequest"),
]
for box, (title, subtitle) in zip(boxes2, titles2):
    node(box, title, subtitle, "#b56a16", "#fff9f1")
for first, second in zip(boxes2, boxes2[1:]):
    arrow([right(first), left(second)], "#b56a16")
arrow([merge, (merge[0], 1165), (center(boxes2[0])[0], 1165), top(boxes2[0])], "#b56a16")

stage3 = (70, 1535, 1730, 1815)
group(stage3, "阶段三：MIDI-GPT 全局补全", "#0e7490")
boxes3 = [
    (140, 1645, 485, 1755),
    (555, 1645, 900, 1755),
    (970, 1645, 1315, 1755),
    (1385, 1645, 1660, 1755),
]
titles3 = [
    ("读取受控框架", "上下文＋填充策略"),
    ("补写音乐内容", "伴奏、和声、低音与过渡"),
    ("生成后验证", "保护事件、结构与范围"),
    ("完整多轨 MIDI", "通过后进入渲染"),
]
for box, (title, subtitle) in zip(boxes3, titles3):
    node(box, title, subtitle, "#0e7490", "#f0fafc")
for first, second in zip(boxes3, boxes3[1:]):
    arrow([right(first), left(second)], "#0e7490")
arrow([bottom(boxes2[-1]), (center(boxes2[-1])[0], 1500), (center(boxes3[0])[0], 1500), top(boxes3[0])], "#0e7490")

render_group = (70, 1885, 1730, 2135)
group(render_group, "阶段四：最终渲染", "#5f6b76")
render_boxes = [
    (235, 1980, 650, 2080),
    (700, 1980, 1115, 2080),
    (1165, 1980, 1580, 2080),
]
render_titles = [
    ("完整 MIDI", "结构化生成结果"),
    ("DAW / 软音源", "患者 SF2＋其他乐器"),
    ("最终音乐音频", "WAV / 其他交付格式"),
]
for box, (title, subtitle) in zip(render_boxes, render_titles):
    node(box, title, subtitle, "#5f6b76", "#f7f8fa")
for first, second in zip(render_boxes, render_boxes[1:]):
    arrow([right(first), left(second)], "#5f6b76")
arrow([bottom(boxes3[-1]), (center(boxes3[-1])[0], 1850), (center(render_boxes[0])[0], 1850), top(render_boxes[0])], "#5f6b76")

draw.text(
    (WIDTH / 2, 2170),
    "原则：一个规划 LLM；MuseCoco 只生成主题；第二阶段只做确定性装配；MIDI-GPT 只补全开放区域。",
    font=TINY_FONT,
    fill=MUTED,
    anchor="mm",
)

output = Path(__file__).with_name("legasynth_full_pipeline.png")
image.save(output, format="PNG", optimize=True)
print(output)
