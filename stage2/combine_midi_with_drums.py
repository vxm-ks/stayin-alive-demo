from __future__ import annotations

r"""
combine_midi_with_drums.py

功能：
    将两个音乐片段 A.mid 和 B.mid 按固定顺序拼接，
    同时自动生成一条持续的闭镲鼓点轨道。

固定结构：
    A → 空白 → B → 空白 → A → 空白

我们已经约定好的规则：
1. 当前测试状态下，A 和 B 都是 8 小节，程序不负责裁剪。
2. A、B 的所有音乐轨道都保留。
3. A 和 B 的 PPQ 必须一致。
4. 每段音乐后固定留空 8 小节，形成每段 8+8。
5. 复制输入素材前，检测并移除其所有 MIDI 通道 10 消息。
6. 再按 Stage 1 计划生成新的通道 10 心跳事件。
7. 鼓点贯穿整首 MIDI，包括音乐片段和空白部分。
8. 每一拍放置一个相同的闭镲鼓点。
9. 闭镲使用 General MIDI 音高 42。
10. 鼓轨使用 MIDI 通道 10；在 mido 中写作 channel=9。
11. 鼓点力度固定为 velocity=80。
12. 每个闭镲音符持续“一拍的八分之一”。
13. 默认输出文件名为 combined_with_drums.mid。

PowerShell 运行示例：
    python .\combine_midi_with_drums.py .\inputs\A.mid .\inputs\B.mid `
        --bpm 96 `
        --time-signature 4/4 `
        --output .\outputs\combined_with_drums.mid
"""

import argparse
import copy
import json
from pathlib import Path

import mido
from mido import Message, MetaMessage, MidiFile, MidiTrack


# 当前 Stage 2 测试状态：每个主题 8 小节，随后填充 8 小节。
SEGMENT_BARS = 8
GAP_BARS = 8

# General MIDI 中，42 表示 Closed Hi-Hat（闭镲）。
HI_HAT_NOTE = 42

# 人类习惯说“第 10 通道”，mido 从 0 开始编号，所以写 9。
DRUM_CHANNEL = 9

# 鼓点力度固定为 80。
HI_HAT_VELOCITY = 80

# 每个闭镲持续“一拍的八分之一”。
HI_HAT_DURATION_RATIO = 1 / 8


def parse_time_signature(value: str) -> tuple[int, int]:
    """
    把拍号字符串转换成两个整数。

    例如：
        "4/4" -> (4, 4)
        "3/4" -> (3, 4)
        "6/8" -> (6, 8)
    """
    try:
        numerator_text, denominator_text = value.split("/", maxsplit=1)
        numerator = int(numerator_text)
        denominator = int(denominator_text)
    except (ValueError, AttributeError) as exc:
        raise argparse.ArgumentTypeError(
            "拍号格式应为 4/4、3/4 或 6/8。"
        ) from exc

    if numerator <= 0 or denominator <= 0:
        raise argparse.ArgumentTypeError("拍号的分子和分母必须大于 0。")

    return numerator, denominator


def message_priority(message: Message | MetaMessage) -> int:
    """
    当多个事件位于同一个 tick 时，决定先后顺序。

    note_off 尽量排在 note_on 前面，避免旧音尚未结束，
    新音又在同一时刻开始。
    """
    if message.type == "note_off":
        return 0

    if message.type == "note_on" and message.velocity == 0:
        return 0

    if message.type == "note_on":
        return 1

    return 2


def absolute_events(
    track: MidiTrack,
) -> list[tuple[int, Message | MetaMessage]]:
    """
    把 MIDI 轨道中的相对时间转换为绝对 tick。

    MIDI 的 message.time 表示它距离上一个事件过了多少 tick。
    拼接片段时，我们需要知道每个事件从轨道开头算位于第多少 tick。
    """
    result: list[tuple[int, Message | MetaMessage]] = []
    absolute_tick = 0

    for message in track:
        absolute_tick += message.time
        result.append((absolute_tick, message))

    return result


def build_shifted_track(
    source_track: MidiTrack,
    offset_ticks: int,
    track_name: str,
) -> MidiTrack:
    """
    复制一条原始音乐轨道，并整体向后移动。

    source_track：
        A.mid 或 B.mid 中的一条轨道。

    offset_ticks：
        这段音乐在整首作品中的开始位置。

    track_name：
        输出 MIDI 中的新轨道名称。

    原音符、力度、乐器和通道等会保留。
    原片段中的 BPM 和拍号不复制，因为输出文件统一使用命令行参数。
    """
    events: list[tuple[int, Message | MetaMessage]] = []

    for absolute_tick, original in absolute_events(source_track):
        if original.type in {
            "end_of_track",
            "track_name",
            "set_tempo",
            "time_signature",
        }:
            continue
        # 输入素材中的通道 10 不是当前计划的权威。全部移除后，
        # 再由 build_heartbeat_track_from_plan 确定性填入 S1/S2。
        if getattr(original, "channel", None) == DRUM_CHANNEL:
            continue

        message = copy.copy(original)
        message.time = 0
        events.append((absolute_tick + offset_ticks, message))

    events.sort(key=lambda item: (item[0], message_priority(item[1])))

    output_track = MidiTrack()
    output_track.append(MetaMessage("track_name", name=track_name, time=0))

    # 保存 MIDI 时仍然要使用相对时间，因此这里再从绝对 tick 转回去。
    previous_tick = 0

    for absolute_tick, message in events:
        message.time = absolute_tick - previous_tick
        output_track.append(message)
        previous_tick = absolute_tick

    output_track.append(MetaMessage("end_of_track", time=0))
    return output_track


def add_fragment_tracks(
    output_midi: MidiFile,
    source_midi: MidiFile,
    fragment_name: str,
    occurrence: int,
    offset_ticks: int,
) -> None:
    """
    把一个音乐片段中的全部轨道复制进输出 MIDI。

    例如 A.mid 中有钢琴、弦乐和低音三条轨道，
    那么三条都会保留，不会只留下主旋律。
    """
    for track_index, source_track in enumerate(source_midi.tracks):
        track_name = f"{fragment_name}{occurrence}_track_{track_index}"

        shifted_track = build_shifted_track(
            source_track=source_track,
            offset_ticks=offset_ticks,
            track_name=track_name,
        )

        output_midi.tracks.append(shifted_track)


def build_hi_hat_track(
    total_bars: int,
    numerator: int,
    denominator: int,
    ticks_per_beat: int,
) -> MidiTrack:
    """
    自动生成贯穿整首作品的闭镲鼓轨。

    固定规则：
        每一拍一个闭镲。
        4/4 每小节 4 个。
        3/4 每小节 3 个。
        6/8 每小节 6 个。

    每个闭镲：
        音高 42
        通道 9（即 MIDI 第 10 通道）
        力度 80
        时长为一拍的八分之一
    """
    # PPQ 表示一个四分音符有多少 tick。
    # 当拍号分母不是 4 时，需要换算当前“一拍”有多少 tick。
    beat_ticks = round(ticks_per_beat * 4 / denominator)

    # 一个小节的长度。
    bar_ticks = numerator * beat_ticks

    # 每个闭镲的持续时间固定为一拍的八分之一。
    note_duration_ticks = max(
        1,
        round(beat_ticks * HI_HAT_DURATION_RATIO),
    )

    events: list[tuple[int, Message]] = []

    for bar_index in range(total_bars):
        bar_start_tick = bar_index * bar_ticks

        for beat_index in range(numerator):
            note_start_tick = bar_start_tick + beat_index * beat_ticks

            note_on = Message(
                "note_on",
                channel=DRUM_CHANNEL,
                note=HI_HAT_NOTE,
                velocity=HI_HAT_VELOCITY,
                time=0,
            )

            note_off = Message(
                "note_off",
                channel=DRUM_CHANNEL,
                note=HI_HAT_NOTE,
                velocity=0,
                time=0,
            )

            events.append((note_start_tick, note_on))
            events.append(
                (note_start_tick + note_duration_ticks, note_off)
            )

    events.sort(key=lambda item: (item[0], message_priority(item[1])))

    drum_track = MidiTrack()
    drum_track.append(
        MetaMessage(
            "track_name",
            name="Continuous Closed Hi-Hat",
            time=0,
        )
    )

    previous_tick = 0

    for absolute_tick, message in events:
        message.time = absolute_tick - previous_tick
        drum_track.append(message)
        previous_tick = absolute_tick

    drum_track.append(MetaMessage("end_of_track", time=0))
    return drum_track


def build_heartbeat_track_from_plan(
    plan_path: Path,
    total_bars: int,
    numerator: int,
    denominator: int,
    ticks_per_beat: int,
) -> MidiTrack:
    """Compile Stage 1 section drum instructions into protected S1/S2 events.

    Low-tension pulses use a complete S1/S2 pair. High-tension pulses use S1,
    matching Stage 1's heartbeat_processing_plan compatibility view.
    """
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    if data.get("total_bars") != total_bars:
        raise ValueError("stage2_plan total_bars differs from the assembled MIDI")
    if data.get("time_signature") != f"{numerator}/{denominator}":
        raise ValueError("stage2_plan time signature differs from the assembled MIDI")
    beat_ticks = round(ticks_per_beat * 4 / denominator)
    bar_ticks = numerator * beat_ticks
    duration = max(1, round(beat_ticks * HI_HAT_DURATION_RATIO))
    events: list[tuple[int, Message]] = []

    def add_note(tick: int, note: int, velocity: int) -> None:
        events.append((tick, Message("note_on", channel=DRUM_CHANNEL, note=note, velocity=velocity, time=0)))
        events.append((tick + duration, Message("note_off", channel=DRUM_CHANNEL, note=note, velocity=0, time=0)))

    for section in data["sections"]:
        pattern = section["drum_pattern"]
        trigger_beats = pattern.get("kick_beats") or (
            [float(value) for value in range(1, numerator + 1)]
            if pattern.get("pattern") == "pulse_each_beat" else [1.0]
        )
        high_tension = section.get("tension_level") == "high"
        velocity = max(1, min(127, round(64 + 32 * float(section.get("source_tension", 0.5)))))
        for bar_number in range(section["bar_start"], section["bar_end"] + 1):
            bar_tick = (bar_number - 1) * bar_ticks
            for beat in trigger_beats:
                if not 1.0 <= float(beat) <= numerator:
                    raise ValueError(f"heartbeat beat {beat} is outside {numerator}/{denominator}")
                s1_tick = bar_tick + round((float(beat) - 1.0) * beat_ticks)
                add_note(s1_tick, 36, velocity)
                if not high_tension:
                    s2_tick = s1_tick + round(0.5 * beat_ticks)
                    if s2_tick < bar_tick + bar_ticks:
                        add_note(s2_tick, 38, max(1, velocity - 8))
        for beat in pattern.get("snare_beats", []):
            for bar_number in range(section["bar_start"], section["bar_end"] + 1):
                add_note((bar_number - 1) * bar_ticks + round((float(beat) - 1) * beat_ticks), 38, velocity)

    events.sort(key=lambda item: (item[0], message_priority(item[1])))
    track = MidiTrack()
    track.append(MetaMessage("track_name", name="LegaSynth Heartbeat S1-S2", time=0))
    previous_tick = 0
    for tick, message in events:
        message.time = tick - previous_tick
        track.append(message)
        previous_tick = tick
    track.append(MetaMessage("end_of_track", time=0))
    return track


def combine_midi_with_drums(
    a_path: Path,
    b_path: Path,
    bpm: float,
    time_signature: tuple[int, int],
    output_path: Path,
    stage2_plan: Path | None = None,
) -> None:
    """
    完成整个拼接和鼓轨生成过程。

    固定结构：
        A → 空白 → B → 空白 → A → 空白
    """
    if bpm <= 0:
        raise ValueError("BPM 必须大于 0。")

    a_midi = MidiFile(a_path)
    b_midi = MidiFile(b_path)

    # 输出文件使用 A.mid 的 PPQ。
    ppq = a_midi.ticks_per_beat

    # 我们约定 A、B 的 PPQ 必须一致。
    if b_midi.ticks_per_beat != ppq:
        raise ValueError("A.mid 和 B.mid 的 PPQ 必须一致。")

    numerator, denominator = time_signature

    beat_ticks = round(ppq * 4 / denominator)
    bar_ticks = numerator * beat_ticks

    for label, midi in (("A.mid", a_midi), ("B.mid", b_midi)):
        end_tick = max((sum(message.time for message in track) for track in midi.tracks), default=0)
        if end_tick > SEGMENT_BARS * bar_ticks:
            raise ValueError(f"{label} exceeds the protected eight-bar motif boundary")

    # 当前测试状态固定为每段 8 小节主题 + 8 小节填充。
    gap_bars = GAP_BARS

    # 程序内部小节位置从 0 开始。
    a1_start_bar = 0
    b1_start_bar = SEGMENT_BARS + gap_bars
    a2_start_bar = 2 * SEGMENT_BARS + 2 * gap_bars

    # 3 个音乐片段，每个 8 小节；再加 3 段 8 小节空白。
    total_bars = 3 * SEGMENT_BARS + 3 * gap_bars

    # Type 1 表示多轨 MIDI。
    output_midi = MidiFile(type=1, ticks_per_beat=ppq)

    # 建立全局控制轨道，统一写入 BPM、拍号和总长度。
    conductor_track = MidiTrack()
    conductor_track.append(
        MetaMessage("track_name", name="Conductor", time=0)
    )
    conductor_track.append(
        MetaMessage(
            "set_tempo",
            tempo=mido.bpm2tempo(bpm),
            time=0,
        )
    )
    conductor_track.append(
        MetaMessage(
            "time_signature",
            numerator=numerator,
            denominator=denominator,
            time=0,
        )
    )
    conductor_track.append(
        MetaMessage(
            "end_of_track",
            time=total_bars * bar_ticks,
        )
    )

    output_midi.tracks.append(conductor_track)

    # 放置第一次出现的 A。
    add_fragment_tracks(
        output_midi=output_midi,
        source_midi=a_midi,
        fragment_name="A",
        occurrence=1,
        offset_ticks=a1_start_bar * bar_ticks,
    )

    # 放置 B。
    add_fragment_tracks(
        output_midi=output_midi,
        source_midi=b_midi,
        fragment_name="B",
        occurrence=1,
        offset_ticks=b1_start_bar * bar_ticks,
    )

    # 再次放置 A。
    add_fragment_tracks(
        output_midi=output_midi,
        source_midi=a_midi,
        fragment_name="A",
        occurrence=2,
        offset_ticks=a2_start_bar * bar_ticks,
    )

    # 自动生成持续到最后一小节的闭镲鼓轨。
    if stage2_plan is None:
        drum_track = build_hi_hat_track(
            total_bars=total_bars,
            numerator=numerator,
            denominator=denominator,
            ticks_per_beat=ppq,
        )
    else:
        drum_track = build_heartbeat_track_from_plan(
            stage2_plan, total_bars, numerator, denominator, ppq,
        )
    output_midi.tracks.append(drum_track)

    # 如果输出目录不存在，就自动创建。
    output_path.parent.mkdir(parents=True, exist_ok=True)

    output_midi.save(output_path)

    print("生成完成。")
    print(f"输出文件：{output_path}")
    print(f"BPM：{bpm}")
    print(f"拍号：{numerator}/{denominator}")
    print(f"每段空白：{gap_bars} 小节")
    print(f"总长度：{total_bars} 小节")
    print("结构：A - 空白 - B - 空白 - A - 空白")
    print("状态：TEST（每段 8 小节主题 + 8 小节填充）")
    print("通道 10：已移除输入原有消息，并按计划重新填充")
    print("闭镲时长：一拍的八分之一")


def main() -> None:
    """
    读取命令行参数，并启动 MIDI 合成。
    """
    parser = argparse.ArgumentParser(
        description=(
            "按 A-空白-B-空白-A-空白排列音乐，"
            "并自动添加持续的闭镲鼓轨。"
        )
    )

    parser.add_argument(
        "a_midi",
        type=Path,
        help="音乐片段 A 的 MIDI 文件",
    )

    parser.add_argument(
        "b_midi",
        type=Path,
        help="音乐片段 B 的 MIDI 文件",
    )

    parser.add_argument(
        "--bpm",
        type=float,
        required=True,
        help="全曲 BPM",
    )

    parser.add_argument(
        "--time-signature",
        type=parse_time_signature,
        required=True,
        help="拍号，例如 4/4",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("combined_with_drums.mid"),
        help="输出路径，默认 combined_with_drums.mid",
    )
    parser.add_argument(
        "--stage2-plan",
        type=Path,
        help="Stage 1 stage2_plan.json used to generate the protected heartbeat track",
    )

    args = parser.parse_args()

    combine_midi_with_drums(
        a_path=args.a_midi,
        b_path=args.b_midi,
        bpm=args.bpm,
        time_signature=args.time_signature,
        output_path=args.output,
        stage2_plan=args.stage2_plan,
    )


if __name__ == "__main__":
    main()
