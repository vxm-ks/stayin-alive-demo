from __future__ import annotations

r"""
complete_all_gaps_v3.py

正式版功能
==========
读取已经制作好的 combined_with_drums.mid，使用 MIDI-GPT 补全其中的
三段空白区域，最后输出 completed_all_gaps.mid。

输入 MIDI 的固定结构
====================
    A → 空白1 → B → 空白2 → A → 空白3

已经确认的规则
==============
1. 本程序的输入只有 combined_with_drums.mid。
2. BPM、拍号、PPQ、总小节数都从输入 MIDI 自动读取。
3. A、B、第二次 A 都固定为 4 小节。
4. BPM <= 100 时，每段空白为 12 小节。
5. BPM > 100 时，每段空白为 28 小节。
6. BPM = 100 时按 12 小节处理。
7. 空白1按照前面的 A 续写。
8. 空白2按照前面的 B 续写。
9. 空白3按照前面的第二次 A 续写。
10. 每段空白只续写前一个片段中实际有音符的旋律轨道。
11. 不新增固定钢琴轨，也不改变原轨道的乐器编号。
12. 同一种乐器如果在输入 MIDI 中有多条轨道，就分别续写各自轨道。
13. 闭镲鼓轨和其他 drum 轨始终保持不变，只作为节奏上下文。
14. 每次生成 4 小节。
15. 每生成完一组 4 小节，就写回当前 Score，作为下一组的上下文。
16. 简单版只延续前一段，不专门优化与后一个片段的过渡。
17. MIDI-GPT 返回结果中的原始轨道版本不会直接写入最终输出；
    程序只提取本次目标小节中新生成的音符，避免原有内容被改动。
18. 允许单个乐器轨在某个 4 小节块中合理休止。
19. 只有当整个 4 小节块的所有目标旋律轨都没有音符时，才重新采样。
20. 每完成一个 4 小节块都会保存 checkpoint，可用 --resume 继续运行。
21. 每个参考片段会自动识别一条最像主旋律的轨道。
22. 每个 4 小节候选必须通过旋律性检查，否则自动重新采样。
23. 最后两小节会自动估计调性，并生成属功能到主和弦的终止式。

小节编号说明
============
MIDI-GPT 使用从 0 开始的小节编号。

当 BPM <= 100：
    A：       0–3
    空白1：   4–15
    B：      16–19
    空白2：  20–31
    A：      32–35
    空白3：  36–47

当 BPM > 100：
    A：       0–3
    空白1：   4–31
    B：      32–35
    空白2：  36–63
    A：      64–67
    空白3：  68–95

运行示例
========
    python .\complete_all_gaps_v3.py `
        .\outputs\combined_with_drums.mid `
        --output .\outputs\completed_all_gaps.mid
"""

import argparse
import copy
from dataclasses import dataclass
from pathlib import Path

from midigpt import Score, Track
from midigpt.inference import (
    GenerationRequest,
    InferenceConfig,
    InferenceEngine,
    TrackPrompt,
)


# ------------------------------------------------------------
# 固定参数
# ------------------------------------------------------------

# A、B、第二次 A 都固定为 4 小节。
SEGMENT_BARS = 4

# 每次让 MIDI-GPT 生成 4 小节。
BLOCK_BARS = 4

# 使用 8 小节模型窗口。
MODEL_DIM = 8

# 固定随机种子，方便重复测试。
DEFAULT_SEED = 42

# 简单版固定推理参数。
TEMPERATURE = 1.0
TOP_P = 0.95

# 一个 4 小节候选不合格时，最多重新采样的次数。
MAX_BLOCK_ATTEMPTS = 8

# 重试时只小幅提高温度，避免后续候选过于随机。
TEMPERATURE_ESCALATION = 1.04

MIN_MELODY_ONSETS = 6
MIN_MELODY_SOUNDED_BARS = 3
MIN_MELODY_DISTINCT_PITCHES = 3
MIN_MELODY_PITCH_CHANGES = 3
MAX_MELODY_CHORD_ONSET_RATIO = 0.50


@dataclass(frozen=True)
class GapPlan:
    """
    描述一段空白区域以及它前面的参考音乐片段。

    name：
        空白区域名称，例如 gap1。

    source_start/source_end：
        前一个音乐片段的起止小节，均为 0-based 闭区间。

    gap_start/gap_end：
        需要补全的空白区域起止小节，均为 0-based 闭区间。
    """

    name: str
    source_start: int
    source_end: int
    gap_start: int
    gap_end: int



@dataclass(frozen=True)
class MelodyStats:
    """一条轨道在指定小节范围内的旋律统计。"""

    note_count: int
    onset_count: int
    sounded_bars: int
    distinct_pitches: int
    pitch_changes: int
    chord_onset_ratio: float
    average_pitch: float

def detect_bpm(score: Score) -> float:
    """
    从 MIDI-GPT Score 的 tempo 字段自动计算 BPM。

    Score.tempo 的单位是：
        每个四分音符包含多少微秒。

    换算公式：
        BPM = 60,000,000 / tempo
    """
    if score.tempo <= 0:
        raise ValueError(f"输入 MIDI 的 tempo 非法：{score.tempo}")

    return 60_000_000 / score.tempo


def validate_constant_time_signature(score: Score) -> tuple[int, int]:
    """
    检查整首 MIDI 是否使用单一拍号，并返回该拍号。

    当前简单版不处理作品中途换拍号的情况。
    """
    if not score.tracks:
        raise ValueError("输入 MIDI 中没有轨道。")

    if not score.tracks[0].bars:
        raise ValueError("输入 MIDI 中没有小节。")

    first_bar = score.tracks[0].bars[0]
    expected = (first_bar.ts_numerator, first_bar.ts_denominator)

    for track_id, track in enumerate(score.tracks):
        for bar_id, bar in enumerate(track.bars):
            current = (bar.ts_numerator, bar.ts_denominator)

            if current != expected:
                raise ValueError(
                    "当前简单版只支持全曲单一拍号；"
                    f"轨道 {track_id} 第 {bar_id} 小节为 "
                    f"{current[0]}/{current[1]}，"
                    f"但开头拍号为 {expected[0]}/{expected[1]}。"
                )

    return expected


def validate_equal_bar_counts(score: Score) -> int:
    """
    检查所有轨道是否拥有相同的小节数，并返回总小节数。

    MIDI-GPT 要求所有轨道的小节数一致。
    """
    if not score.tracks:
        raise ValueError("输入 MIDI 中没有轨道。")

    total_bars = len(score.tracks[0].bars)

    if total_bars == 0:
        raise ValueError("输入 MIDI 中没有小节。")

    for track_id, track in enumerate(score.tracks):
        if len(track.bars) != total_bars:
            raise ValueError(
                f"轨道 {track_id} 有 {len(track.bars)} 小节，"
                f"但第一条轨道有 {total_bars} 小节。"
            )

    return total_bars


def build_gap_plans(gap_bars: int) -> list[GapPlan]:
    """
    根据固定结构计算三段空白和三个参考片段的位置。

    固定结构：
        A(4) → gap1 → B(4) → gap2 → A(4) → gap3
    """
    a1_start = 0
    a1_end = a1_start + SEGMENT_BARS - 1

    gap1_start = a1_end + 1
    gap1_end = gap1_start + gap_bars - 1

    b_start = gap1_end + 1
    b_end = b_start + SEGMENT_BARS - 1

    gap2_start = b_end + 1
    gap2_end = gap2_start + gap_bars - 1

    a2_start = gap2_end + 1
    a2_end = a2_start + SEGMENT_BARS - 1

    gap3_start = a2_end + 1
    gap3_end = gap3_start + gap_bars - 1

    return [
        GapPlan(
            name="gap1_after_A",
            source_start=a1_start,
            source_end=a1_end,
            gap_start=gap1_start,
            gap_end=gap1_end,
        ),
        GapPlan(
            name="gap2_after_B",
            source_start=b_start,
            source_end=b_end,
            gap_start=gap2_start,
            gap_end=gap2_end,
        ),
        GapPlan(
            name="gap3_after_A",
            source_start=a2_start,
            source_end=a2_end,
            gap_start=gap3_start,
            gap_end=gap3_end,
        ),
    ]


def track_has_notes_in_range(
    track: Track,
    start_bar: int,
    end_bar: int,
) -> bool:
    """
    判断一条轨道在指定小节范围中是否实际包含音符。
    """
    return any(
        track.bars[bar_id].notes
        for bar_id in range(start_bar, end_bar + 1)
    )


def find_active_melodic_tracks(
    score: Score,
    source_start: int,
    source_end: int,
) -> list[int]:
    """
    找出前一个音乐片段中真正使用的旋律轨道。

    筛选规则：
    1. track_type 必须不是 drum。
    2. 在参考片段的 4 小节中必须至少有一个音符。

    这些轨道就是对应空白区域需要续写的配器。
    """
    active_track_ids = []

    for track_id, track in enumerate(score.tracks):
        if track.track_type == "drum":
            continue

        if track_has_notes_in_range(track, source_start, source_end):
            active_track_ids.append(track_id)

    return active_track_ids


def find_drum_tracks(score: Score) -> list[int]:
    """
    找出所有鼓轨。

    鼓轨不参与生成，但会作为节奏上下文提供给模型。
    """
    return [
        track_id
        for track_id, track in enumerate(score.tracks)
        if track.track_type == "drum"
    ]



def analyze_melody_stats(
    track: Track,
    start_bar: int,
    end_bar: int,
) -> MelodyStats:
    """
    分析一条轨道在指定范围中的旋律性。

    每个不同的 (小节, onset_ticks) 被视为一个起奏事件。
    同一时刻出现的多个音符只算一个事件，并取最高音构成旋律轮廓。
    """
    onset_groups: dict[tuple[int, int], list[int]] = {}
    sounded_bar_ids: set[int] = set()
    all_pitches: list[int] = []

    for bar_id in range(start_bar, end_bar + 1):
        for note in track.bars[bar_id].notes:
            key = (bar_id, note.onset_ticks)
            onset_groups.setdefault(key, []).append(note.pitch)
            sounded_bar_ids.add(bar_id)
            all_pitches.append(note.pitch)

    if not onset_groups:
        return MelodyStats(
            note_count=0,
            onset_count=0,
            sounded_bars=0,
            distinct_pitches=0,
            pitch_changes=0,
            chord_onset_ratio=0.0,
            average_pitch=0.0,
        )

    ordered_groups = [
        onset_groups[key]
        for key in sorted(onset_groups)
    ]
    contour = [max(pitches) for pitches in ordered_groups]

    pitch_changes = sum(
        current != previous
        for previous, current in zip(contour, contour[1:])
    )
    chord_onsets = sum(
        len(pitches) >= 2
        for pitches in ordered_groups
    )

    return MelodyStats(
        note_count=len(all_pitches),
        onset_count=len(ordered_groups),
        sounded_bars=len(sounded_bar_ids),
        distinct_pitches=len(set(contour)),
        pitch_changes=pitch_changes,
        chord_onset_ratio=chord_onsets / len(ordered_groups),
        average_pitch=sum(contour) / len(contour),
    )


def choose_melody_track(
    score: Score,
    active_track_ids: list[int],
    source_start: int,
    source_end: int,
) -> tuple[int, MelodyStats]:
    """
    从参考片段的活跃轨道中，自动选择最像主旋律的一条。

    偏好单声部、音符事件较多、音高变化较丰富且音区较高的轨道。
    """
    best_track_id: int | None = None
    best_stats: MelodyStats | None = None
    best_score = float("-inf")

    for track_id in active_track_ids:
        stats = analyze_melody_stats(
            score.tracks[track_id],
            source_start,
            source_end,
        )

        if stats.onset_count == 0:
            continue

        monophony_score = 1.0 - stats.chord_onset_ratio
        event_score = min(stats.onset_count / 8.0, 1.0)
        variety_score = min(stats.distinct_pitches / 5.0, 1.0)
        motion_score = min(stats.pitch_changes / 5.0, 1.0)
        register_score = max(
            0.0,
            min((stats.average_pitch - 24.0) / 72.0, 1.0),
        )

        score_value = (
            3.0 * monophony_score
            + 2.0 * event_score
            + 2.0 * variety_score
            + 2.0 * motion_score
            + 1.0 * register_score
        )

        if score_value > best_score:
            best_score = score_value
            best_track_id = track_id
            best_stats = stats

    if best_track_id is None or best_stats is None:
        raise RuntimeError("无法从参考片段中识别主旋律轨。")

    return best_track_id, best_stats


def validate_melody_candidate(
    candidate_score: Score,
    melody_track_id: int,
    target_bars: list[int],
    source_stats: MelodyStats,
) -> tuple[bool, MelodyStats, list[str]]:
    """
    检查一个 4 小节候选是否有足够的旋律性。
    """
    stats = analyze_melody_stats(
        candidate_score.tracks[melody_track_id],
        target_bars[0],
        target_bars[-1],
    )

    min_onsets = max(
        MIN_MELODY_ONSETS,
        min(12, round(source_stats.onset_count * 0.50)),
    )
    min_distinct = max(
        MIN_MELODY_DISTINCT_PITCHES,
        min(6, round(source_stats.distinct_pitches * 0.50)),
    )
    min_changes = max(
        MIN_MELODY_PITCH_CHANGES,
        min(8, round(source_stats.pitch_changes * 0.45)),
    )
    max_chord_ratio = min(
        MAX_MELODY_CHORD_ONSET_RATIO,
        max(0.25, source_stats.chord_onset_ratio + 0.15),
    )

    reasons: list[str] = []

    if stats.onset_count < min_onsets:
        reasons.append(
            f"旋律事件太少：{stats.onset_count} < {min_onsets}"
        )

    if stats.sounded_bars < MIN_MELODY_SOUNDED_BARS:
        reasons.append(
            f"有旋律的小节太少："
            f"{stats.sounded_bars} < {MIN_MELODY_SOUNDED_BARS}"
        )

    if stats.distinct_pitches < min_distinct:
        reasons.append(
            f"不同音高太少：{stats.distinct_pitches} < {min_distinct}"
        )

    if stats.pitch_changes < min_changes:
        reasons.append(
            f"音高变化太少：{stats.pitch_changes} < {min_changes}"
        )

    if stats.chord_onset_ratio > max_chord_ratio:
        reasons.append(
            f"和弦式起奏比例过高："
            f"{stats.chord_onset_ratio:.2f} > {max_chord_ratio:.2f}"
        )

    return not reasons, stats, reasons


def split_into_blocks(
    start_bar: int,
    end_bar: int,
) -> list[list[int]]:
    """
    把一段空白拆成若干个连续的 4 小节块。

    例如：
        4–15
    拆成：
        [4,5,6,7]
        [8,9,10,11]
        [12,13,14,15]

    当前空白长度固定为 12 或 28，都能被 4 整除。
    """
    total = end_bar - start_bar + 1

    if total <= 0:
        raise ValueError("空白区域长度必须大于 0。")

    if total % BLOCK_BARS != 0:
        raise ValueError(
            f"空白长度 {total} 不能按每组 {BLOCK_BARS} 小节完整拆分。"
        )

    blocks = []

    for block_start in range(start_bar, end_bar + 1, BLOCK_BARS):
        blocks.append(
            list(range(block_start, block_start + BLOCK_BARS))
        )

    return blocks


def ensure_target_bars_empty(
    score: Score,
    track_ids: list[int],
    target_bars: list[int],
) -> None:
    """
    在生成前确认目标轨道的目标小节确实为空。

    正式版不允许静默覆盖已有音符。
    """
    for track_id in track_ids:
        for bar_id in target_bars:
            if score.tracks[track_id].bars[bar_id].notes:
                raise ValueError(
                    f"轨道 {track_id} 的第 {bar_id} 小节并非空白，"
                    "程序不会覆盖已有音符。"
                )


def build_generation_request(
    score: Score,
    active_track_ids: list[int],
    drum_track_ids: list[int],
    target_bars: list[int],
    seed: int,
    temperature: float,
) -> GenerationRequest:
    """
    为一个 4 小节块构造 MIDI-GPT 请求。

    三类轨道的处理方式：

    1. 本段需要续写的旋律轨：
       bars=target_bars、ignore=False
       表示在目标 4 小节中生成。

    2. 鼓轨：
       bars=[]、ignore=False
       表示不生成，但保留在模型上下文中提供节奏。

    3. 其他无关音乐轨：
       bars=[]、ignore=True
       表示本次生成时完全不让模型看见，
       从而减少 A/B/A 不同配器和风格之间的相互干扰。
    """
    active_set = set(active_track_ids)
    drum_set = set(drum_track_ids)
    prompts = []

    for track_id in range(len(score.tracks)):
        if track_id in active_set:
            prompts.append(
                TrackPrompt(
                    id=track_id,
                    bars=target_bars,
                    autoregressive=False,
                    ignore=False,
                )
            )
        elif track_id in drum_set:
            prompts.append(
                TrackPrompt(
                    id=track_id,
                    bars=[],
                    autoregressive=False,
                    ignore=False,
                )
            )
        else:
            prompts.append(
                TrackPrompt(
                    id=track_id,
                    bars=[],
                    autoregressive=False,
                    ignore=True,
                )
            )

    return GenerationRequest(
        tracks=prompts,
        config=InferenceConfig(
            temperature=temperature,
            top_p=TOP_P,
            seed=seed,
            # 单个配器轨在某个乐句中休止是合理的，不能因此中止整次生成。
            # 整个 4 小节块是否全静音，由外层统一判断并重试。
            max_attempts=1,
            novelty_check=False,
            silence_check=False,
            temperature_escalation=1.0,
            model_dim=MODEL_DIM,
            mask_mode="attention",
            bars_per_step=BLOCK_BARS,
            tracks_per_step=1,
            shuffle=False,
        ),
    )


def copy_generated_block(
    model_result: Score,
    working_score: Score,
    active_track_ids: list[int],
    target_bars: list[int],
) -> dict[int, int]:
    """
    只把本次目标轨道、目标小节中的新音符写回 working_score。

    这样做的目的：
    1. 原来的 A、B、A 和鼓轨不会采用模型返回版本。
    2. MIDI-GPT 即使对内部 Score 做了规范化，也不会影响原内容。
    3. 本次生成的 4 小节会成为下一组生成时的上下文。

    如果模型输出 resolution 与输入不同，会自动换算 tick。
    """
    if model_result.resolution <= 0 or working_score.resolution <= 0:
        raise RuntimeError("Score resolution 非法。")

    scale = working_score.resolution / model_result.resolution
    copied_counts: dict[int, int] = {}

    for track_id in active_track_ids:
        if track_id >= len(model_result.tracks):
            raise RuntimeError(
                f"模型输出中不存在轨道 {track_id}。"
            )

        copied_count = 0

        for bar_id in target_bars:
            source_bar = model_result.tracks[track_id].bars[bar_id]
            new_notes = []

            for source_note in source_bar.notes:
                note = copy.deepcopy(source_note)

                note.onset_ticks = round(
                    note.onset_ticks * scale
                )
                note.duration_ticks = max(
                    1,
                    round(note.duration_ticks * scale),
                )

                if hasattr(note, "delta"):
                    note.delta = round(note.delta * scale)

                new_notes.append(note)

            working_score.tracks[track_id].bars[bar_id].notes = new_notes
            copied_count += len(new_notes)

        copied_counts[track_id] = copied_count

    return copied_counts



MAJOR_KEY_PROFILE = (
    6.35, 2.23, 3.48, 2.33, 4.38, 4.09,
    2.52, 5.19, 2.39, 3.66, 2.29, 2.88,
)

MINOR_KEY_PROFILE = (
    6.33, 2.68, 3.52, 5.38, 2.60, 3.53,
    2.54, 4.75, 3.98, 2.69, 3.34, 3.17,
)

PITCH_CLASS_NAMES = (
    "C", "C#", "D", "D#", "E", "F",
    "F#", "G", "G#", "A", "A#", "B",
)


def estimate_key(
    score: Score,
    track_ids: list[int],
    start_bar: int,
    end_bar: int,
) -> tuple[int, str]:
    """
    根据音高类别和音符时值估计大调或小调。
    """
    histogram = [0.0] * 12

    for track_id in track_ids:
        track = score.tracks[track_id]

        for bar_id in range(start_bar, end_bar + 1):
            for note in track.bars[bar_id].notes:
                histogram[note.pitch % 12] += max(
                    1,
                    note.duration_ticks,
                )

    if sum(histogram) == 0:
        return 0, "major"

    best_tonic = 0
    best_mode = "major"
    best_score = float("-inf")

    for tonic_pc in range(12):
        major_score = sum(
            histogram[(tonic_pc + degree) % 12]
            * MAJOR_KEY_PROFILE[degree]
            for degree in range(12)
        )
        minor_score = sum(
            histogram[(tonic_pc + degree) % 12]
            * MINOR_KEY_PROFILE[degree]
            for degree in range(12)
        )

        if major_score > best_score:
            best_score = major_score
            best_tonic = tonic_pc
            best_mode = "major"

        if minor_score > best_score:
            best_score = minor_score
            best_tonic = tonic_pc
            best_mode = "minor"

    return best_tonic, best_mode


def average_pitch_in_range(
    track: Track,
    start_bar: int,
    end_bar: int,
    default: float,
) -> float:
    """
    计算一条轨道在指定范围内的平均音高。
    """
    pitches = [
        note.pitch
        for bar_id in range(start_bar, end_bar + 1)
        for note in track.bars[bar_id].notes
    ]

    if not pitches:
        return default

    return sum(pitches) / len(pitches)


def nearest_pitch_with_class(
    pitch_class: int,
    target_pitch: float,
) -> int:
    """
    找到最接近目标音区且属于指定 pitch class 的 MIDI 音高。
    """
    candidates = [
        pitch
        for pitch in range(12, 116)
        if pitch % 12 == pitch_class
    ]

    return min(
        candidates,
        key=lambda pitch: abs(pitch - target_pitch),
    )


def find_note_template(
    track: Track,
    preferred_start: int,
    preferred_end: int,
):
    """
    从原轨道复制一个 Note 对象作为终止式音符模板。
    """
    for bar_id in range(preferred_end, preferred_start - 1, -1):
        if track.bars[bar_id].notes:
            return copy.deepcopy(track.bars[bar_id].notes[-1])

    for bar in reversed(track.bars):
        if bar.notes:
            return copy.deepcopy(bar.notes[-1])

    raise RuntimeError("活跃轨道中找不到终止式音符模板。")


def make_cadence_note(
    template,
    pitch: int,
    onset_ticks: int,
    duration_ticks: int,
    velocity_scale: float = 1.0,
):
    """
    复制已有音符，并修改成终止式需要的音高和时值。
    """
    note = copy.deepcopy(template)
    note.pitch = int(max(0, min(127, pitch)))
    note.onset_ticks = int(max(0, onset_ticks))
    note.duration_ticks = int(max(1, duration_ticks))
    note.velocity = int(
        max(1, min(127, round(note.velocity * velocity_scale)))
    )

    if hasattr(note, "delta"):
        note.delta = 0

    return note


def apply_final_cadence(
    score: Score,
    active_track_ids: list[int],
    melody_track_id: int,
    source_start: int,
    source_end: int,
    gap_start: int,
    gap_end: int,
) -> tuple[str, int, int]:
    """
    将整首作品最后两小节改写为 V-I 或 V-i 终止式。

    主旋律：
        导音 -> 主音

    低音：
        属音 -> 主音

    其他声部：
        属和弦 -> 主和弦
    """
    if gap_end - gap_start + 1 < 2:
        raise ValueError("最后一段至少需要 2 小节才能生成终止式。")

    penultimate_bar = gap_end - 1
    final_bar = gap_end
    reference_end = max(source_end, penultimate_bar - 1)

    tonic_pc, mode = estimate_key(
        score,
        active_track_ids,
        source_start,
        reference_end,
    )

    possible_bass_tracks = [
        track_id
        for track_id in active_track_ids
        if track_id != melody_track_id
    ]

    bass_track_id: int | None = None

    if possible_bass_tracks:
        bass_track_id = min(
            possible_bass_tracks,
            key=lambda track_id: average_pitch_in_range(
                score.tracks[track_id],
                source_start,
                reference_end,
                default=60.0,
            ),
        )

    harmony_track_ids = [
        track_id
        for track_id in active_track_ids
        if track_id not in {melody_track_id, bass_track_id}
    ]

    reference_bar = score.tracks[melody_track_id].bars[final_bar]
    beat_ticks = round(
        score.resolution * 4 / reference_bar.ts_denominator
    )
    bar_ticks = reference_bar.ts_numerator * beat_ticks

    for track_id in active_track_ids:
        score.tracks[track_id].bars[penultimate_bar].notes = []
        score.tracks[track_id].bars[final_bar].notes = []

    # 主旋律：导音 -> 主音
    melody_track = score.tracks[melody_track_id]
    melody_template = find_note_template(
        melody_track,
        source_start,
        reference_end,
    )
    melody_register = average_pitch_in_range(
        melody_track,
        source_start,
        reference_end,
        default=72.0,
    )

    tonic_melody_pitch = nearest_pitch_with_class(
        tonic_pc,
        melody_register,
    )
    leading_tone_pitch = nearest_pitch_with_class(
        (tonic_pc - 1) % 12,
        tonic_melody_pitch - 1,
    )

    melody_track.bars[penultimate_bar].notes = [
        make_cadence_note(
            melody_template,
            pitch=leading_tone_pitch,
            onset_ticks=max(0, bar_ticks - beat_ticks),
            duration_ticks=beat_ticks,
            velocity_scale=0.92,
        )
    ]
    melody_track.bars[final_bar].notes = [
        make_cadence_note(
            melody_template,
            pitch=tonic_melody_pitch,
            onset_ticks=0,
            duration_ticks=bar_ticks,
            velocity_scale=1.0,
        )
    ]

    # 低音：属音 -> 主音
    if bass_track_id is not None:
        bass_track = score.tracks[bass_track_id]
        bass_template = find_note_template(
            bass_track,
            source_start,
            reference_end,
        )
        bass_register = average_pitch_in_range(
            bass_track,
            source_start,
            reference_end,
            default=43.0,
        )

        dominant_bass_pitch = nearest_pitch_with_class(
            (tonic_pc + 7) % 12,
            bass_register,
        )
        tonic_bass_pitch = nearest_pitch_with_class(
            tonic_pc,
            dominant_bass_pitch - 5,
        )

        bass_track.bars[penultimate_bar].notes = [
            make_cadence_note(
                bass_template,
                pitch=dominant_bass_pitch,
                onset_ticks=0,
                duration_ticks=bar_ticks,
                velocity_scale=0.95,
            )
        ]
        bass_track.bars[final_bar].notes = [
            make_cadence_note(
                bass_template,
                pitch=tonic_bass_pitch,
                onset_ticks=0,
                duration_ticks=bar_ticks,
                velocity_scale=1.0,
            )
        ]

    dominant_chord_pcs = (
        (tonic_pc + 7) % 12,
        (tonic_pc + 11) % 12,
        (tonic_pc + 2) % 12,
    )

    tonic_third = 4 if mode == "major" else 3
    tonic_chord_pcs = (
        tonic_pc,
        (tonic_pc + tonic_third) % 12,
        (tonic_pc + 7) % 12,
    )

    for index, track_id in enumerate(harmony_track_ids):
        track = score.tracks[track_id]
        template = find_note_template(
            track,
            source_start,
            reference_end,
        )
        register = average_pitch_in_range(
            track,
            source_start,
            reference_end,
            default=60.0,
        )

        dominant_pitch = nearest_pitch_with_class(
            dominant_chord_pcs[index % 3],
            register,
        )
        tonic_pitch = nearest_pitch_with_class(
            tonic_chord_pcs[index % 3],
            register,
        )

        track.bars[penultimate_bar].notes = [
            make_cadence_note(
                template,
                pitch=dominant_pitch,
                onset_ticks=0,
                duration_ticks=bar_ticks,
                velocity_scale=0.90,
            )
        ]
        track.bars[final_bar].notes = [
            make_cadence_note(
                template,
                pitch=tonic_pitch,
                onset_ticks=0,
                duration_ticks=bar_ticks,
                velocity_scale=0.95,
            )
        ]

    key_name = (
        f"{PITCH_CLASS_NAMES[tonic_pc]} "
        f"{'major' if mode == 'major' else 'minor'}"
    )

    return key_name, penultimate_bar, final_bar



def complete_all_gaps(
    input_path: Path,
    output_path: Path,
    model_name: str,
    base_seed: int,
    resume: bool,
) -> None:
    """
    执行正式的三段空白补全过程。
    """
    if not input_path.exists():
        raise FileNotFoundError(f"找不到输入文件：{input_path}")

    checkpoint_path = output_path.with_name(
        f"{output_path.stem}.v3.checkpoint{output_path.suffix}"
    )

    if resume:
        if not checkpoint_path.exists():
            raise FileNotFoundError(
                f"指定了 --resume，但找不到检查点：{checkpoint_path}"
            )

        print(f"正在从检查点继续：{checkpoint_path}")
        working_score = Score.from_midi(str(checkpoint_path))
    else:
        print("正在读取输入 MIDI……")
        working_score = Score.from_midi(str(input_path))

    total_bars = validate_equal_bar_counts(working_score)
    numerator, denominator = validate_constant_time_signature(
        working_score
    )
    bpm = detect_bpm(working_score)

    # BPM = 100 时使用 12 小节。
    gap_bars = 28 if bpm > 100 else 12
    expected_total_bars = (
        3 * SEGMENT_BARS + 3 * gap_bars
    )

    if total_bars != expected_total_bars:
        raise ValueError(
            f"根据 BPM={bpm:.3f}，程序预计总长度应为 "
            f"{expected_total_bars} 小节，"
            f"但输入 MIDI 实际为 {total_bars} 小节。"
        )

    gap_plans = build_gap_plans(gap_bars)
    drum_track_ids = find_drum_tracks(working_score)

    print()
    print("输入检查完成：")
    print(f"  BPM：{bpm:.3f}")
    print(f"  拍号：{numerator}/{denominator}")
    print(f"  PPQ：{working_score.resolution}")
    print(f"  总小节数：{total_bars}")
    print(f"  每段空白：{gap_bars} 小节")
    print(f"  总轨道数：{len(working_score.tracks)}")
    print(f"  鼓轨 ID：{drum_track_ids}")

    print()
    print(f"正在加载 MIDI-GPT 模型：{model_name}")
    engine = InferenceEngine.from_pretrained(model_name)

    generation_index = 0

    for gap_number, plan in enumerate(gap_plans, start=1):
        active_track_ids = find_active_melodic_tracks(
            working_score,
            plan.source_start,
            plan.source_end,
        )

        if not active_track_ids:
            raise RuntimeError(
                f"{plan.name} 前面的参考片段 "
                f"{plan.source_start}–{plan.source_end} "
                "没有找到任何有效旋律轨。"
            )

        melody_track_id, source_melody_stats = choose_melody_track(
            working_score,
            active_track_ids,
            plan.source_start,
            plan.source_end,
        )

        blocks = split_into_blocks(
            plan.gap_start,
            plan.gap_end,
        )

        print()
        print("=" * 60)
        print(f"开始补全空白 {gap_number}：{plan.name}")
        print(
            f"参考片段：{plan.source_start}–{plan.source_end}"
        )
        print(
            f"目标空白：{plan.gap_start}–{plan.gap_end}"
        )
        print(f"续写轨道：{active_track_ids}")
        print(f"自动识别的主旋律轨：{melody_track_id}")
        print(
            "参考旋律统计："
            f"事件={source_melody_stats.onset_count}，"
            f"不同音高={source_melody_stats.distinct_pitches}，"
            f"音高变化={source_melody_stats.pitch_changes}，"
            f"和弦比例={source_melody_stats.chord_onset_ratio:.2f}"
        )
        print(f"分块数量：{len(blocks)}")

        for block_number, target_bars in enumerate(
            blocks,
            start=1,
        ):
            existing_note_count = sum(
                len(working_score.tracks[track_id].bars[bar_id].notes)
                for track_id in active_track_ids
                for bar_id in target_bars
            )

            # checkpoint 只会在整块成功后保存，因此恢复时，
            # 目标块只要已有音符，就可以视为已经完整完成。
            if resume and existing_note_count > 0:
                print()
                print(
                    f"[空白{gap_number} / 块{block_number}/{len(blocks)}] "
                    f"小节 {target_bars} 已存在生成内容，跳过。"
                )
                generation_index += 1
                continue

            ensure_target_bars_empty(
                working_score,
                active_track_ids,
                target_bars,
            )

            accepted_counts = None

            for block_attempt in range(MAX_BLOCK_ATTEMPTS):
                current_seed = base_seed + generation_index
                generation_index += 1

                current_temperature = (
                    TEMPERATURE
                    * (TEMPERATURE_ESCALATION ** block_attempt)
                )

                print()
                print(
                    f"[空白{gap_number} / 块{block_number}/{len(blocks)}] "
                    f"生成小节 {target_bars}，"
                    f"整块尝试 {block_attempt + 1}/{MAX_BLOCK_ATTEMPTS}，"
                    f"seed={current_seed}，"
                    f"temperature={current_temperature:.3f}"
                )

                request = build_generation_request(
                    score=working_score,
                    active_track_ids=active_track_ids,
                    drum_track_ids=drum_track_ids,
                    target_bars=target_bars,
                    seed=current_seed,
                    temperature=current_temperature,
                )

                model_result = engine.session(
                    working_score,
                    request,
                ).run()

                # 先写入候选副本。只有整块至少有一个音符时，
                # 才把该副本正式设为 working_score。
                candidate_score = copy.deepcopy(working_score)

                copied_counts = copy_generated_block(
                    model_result=model_result,
                    working_score=candidate_score,
                    active_track_ids=active_track_ids,
                    target_bars=target_bars,
                )

                total_new_notes = sum(copied_counts.values())

                melody_ok, melody_stats, melody_reasons = (
                    validate_melody_candidate(
                        candidate_score=candidate_score,
                        melody_track_id=melody_track_id,
                        target_bars=target_bars,
                        source_stats=source_melody_stats,
                    )
                )

                if total_new_notes > 0 and melody_ok:
                    working_score = candidate_score
                    accepted_counts = copied_counts
                    print(
                        "主旋律检查通过："
                        f"事件={melody_stats.onset_count}，"
                        f"有效小节={melody_stats.sounded_bars}，"
                        f"不同音高={melody_stats.distinct_pitches}，"
                        f"音高变化={melody_stats.pitch_changes}，"
                        f"和弦比例={melody_stats.chord_onset_ratio:.2f}"
                    )
                    break

                if total_new_notes == 0:
                    print("候选被拒绝：整个 4 小节块均为静音。")
                else:
                    print(
                        "候选被拒绝：主旋律不合格；"
                        + "；".join(melody_reasons)
                    )

                print(
                    "将更换 seed 并调整 temperature 后重新生成。"
                )

            if accepted_counts is None:
                raise RuntimeError(
                    f"小节 {target_bars} 连续 "
                    f"{MAX_BLOCK_ATTEMPTS} 次都未生成合格主旋律。"
                )

            rest_tracks = [
                track_id
                for track_id, count in accepted_counts.items()
                if count == 0
            ]

            print(
                "写回完成："
                + ", ".join(
                    f"轨道{track_id}={count}个音符"
                    for track_id, count in accepted_counts.items()
                )
            )

            if rest_tracks:
                print(
                    f"允许合理休止的轨道：{rest_tracks}"
                )

            # 每完成一个 4 小节块就保存检查点。
            checkpoint_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            working_score.to_midi(str(checkpoint_path))
            print(f"检查点已保存：{checkpoint_path}")

    # 三段全部生成后，单独处理整首作品的最后两小节。
    final_plan = gap_plans[-1]
    final_active_track_ids = find_active_melodic_tracks(
        working_score,
        final_plan.source_start,
        final_plan.source_end,
    )
    final_melody_track_id, _ = choose_melody_track(
        working_score,
        final_active_track_ids,
        final_plan.source_start,
        final_plan.source_end,
    )

    key_name, cadence_start, cadence_end = apply_final_cadence(
        score=working_score,
        active_track_ids=final_active_track_ids,
        melody_track_id=final_melody_track_id,
        source_start=final_plan.source_start,
        source_end=final_plan.source_end,
        gap_start=final_plan.gap_start,
        gap_end=final_plan.gap_end,
    )

    print()
    print(
        f"终止式已写入小节 {cadence_start}–{cadence_end}，"
        f"估计调性：{key_name}"
    )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    working_score.to_midi(str(output_path))

    # 正式输出成功后，检查点已不再需要。
    if checkpoint_path.exists():
        checkpoint_path.unlink()

    print()
    print("=" * 60)
    print("全部空白补全完成。")
    print(f"输出文件：{output_path}")
    print("原始音乐片段和鼓轨未被模型返回版本覆盖。")
    print("三段空白分别按照前面的 A、B、A 配器续写。")
    print("所有 4 小节块均通过主旋律检查，结尾已加入终止式。")


def main() -> None:
    """
    读取命令行参数并运行正式补全程序。
    """
    parser = argparse.ArgumentParser(
        description=(
            "读取 combined_with_drums.mid，"
            "按照 A/B/A 的配器逐块补全三段空白。"
        )
    )

    parser.add_argument(
        "input_midi",
        type=Path,
        help="输入 combined_with_drums.mid",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("completed_all_gaps.mid"),
        help="输出 MIDI，默认 completed_all_gaps.mid",
    )

    parser.add_argument(
        "--model",
        default="yellow",
        help="MIDI-GPT 模型名称，默认 yellow",
    )

    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help="基础随机种子，默认 42",
    )

    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "从与输出文件同名的 .checkpoint.mid 检查点继续运行"
        ),
    )

    args = parser.parse_args()

    complete_all_gaps(
        input_path=args.input_midi,
        output_path=args.output,
        model_name=args.model,
        base_seed=args.seed,
        resume=args.resume,
    )


if __name__ == "__main__":
    main()
