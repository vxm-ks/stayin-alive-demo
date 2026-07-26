from __future__ import annotations

import argparse
import hashlib
import html
import json
import shutil
from pathlib import Path
from typing import Any

import mido


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_verified(source: Path, destination: Path, expected_sha256: str | None = None) -> None:
    if not source.is_file():
        raise FileNotFoundError(source)
    if expected_sha256 and sha256(source).lower() != expected_sha256.lower():
        raise RuntimeError(f"SHA-256 mismatch: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def find_one(root: Path, pattern: str) -> Path:
    matches = list(root.rglob(pattern))
    if len(matches) != 1:
        raise RuntimeError(f"Expected one match for {pattern!r} below {root}, got {len(matches)}")
    return matches[0]


def midi_notes(path: Path) -> tuple[list[dict[str, int]], int, int]:
    midi = mido.MidiFile(path)
    notes: list[dict[str, int]] = []
    end_tick = 0

    for track_index, track in enumerate(midi.tracks):
        tick = 0
        active: dict[tuple[int, int], list[tuple[int, int]]] = {}
        for message in track:
            tick += message.time
            end_tick = max(end_tick, tick)
            if message.type == "note_on" and message.velocity > 0:
                key = (message.channel, message.note)
                active.setdefault(key, []).append((tick, message.velocity))
            elif message.type in {"note_off", "note_on"}:
                key = (message.channel, message.note)
                starts = active.get(key)
                if starts:
                    start, velocity = starts.pop(0)
                    notes.append(
                        {
                            "start": start,
                            "end": max(start + 1, tick),
                            "pitch": message.note,
                            "velocity": velocity,
                            "channel": message.channel,
                            "track": track_index,
                        }
                    )

        for (channel, pitch), starts in active.items():
            for start, velocity in starts:
                notes.append(
                    {
                        "start": start,
                        "end": max(start + 1, end_tick),
                        "pitch": pitch,
                        "velocity": velocity,
                        "channel": channel,
                        "track": track_index,
                    }
                )

    return notes, midi.ticks_per_beat, max(end_tick, 1)


def render_piano_roll(
    midi_path: Path,
    output_path: Path,
    title: str,
    section_boundaries: list[tuple[int, str]] | None = None,
) -> None:
    notes, ppq, end_tick = midi_notes(midi_path)
    pitched = [note for note in notes if note["channel"] != 9]
    visible = pitched or notes
    min_pitch = max(0, min((note["pitch"] for note in visible), default=36) - 2)
    max_pitch = min(127, max((note["pitch"] for note in visible), default=84) + 2)

    width, height = 1600, 620
    left, right, top, bottom = 92, 34, 74, 58
    plot_width = width - left - right
    plot_height = height - top - bottom
    pitch_span = max(1, max_pitch - min_pitch + 1)

    def x_pos(tick: int) -> float:
        return left + plot_width * tick / end_tick

    def y_pos(pitch: int) -> float:
        return top + plot_height * (max_pitch - pitch) / pitch_span

    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">',
        "<style>",
        "text{font-family:Inter,Segoe UI,Arial,sans-serif}",
        ".title{font-size:25px;font-weight:700;fill:#263d47}",
        ".meta{font-size:13px;fill:#72858c}",
        ".axis{font-size:11px;fill:#819198}",
        "</style>",
        '<rect width="100%" height="100%" rx="24" fill="#fbfcfa"/>',
        f'<text class="title" x="{left}" y="38">{html.escape(title)}</text>',
        f'<text class="meta" x="{left}" y="59">{len(notes)} note events · PPQ {ppq}</text>',
        f'<rect x="{left}" y="{top}" width="{plot_width}" height="{plot_height}" '
        'rx="12" fill="#f4f8f7" stroke="#dce8e6"/>',
    ]

    beat_ticks = ppq
    bar_ticks = ppq * 4
    beat = 0
    while beat * beat_ticks <= end_tick:
        tick = beat * beat_ticks
        x = x_pos(tick)
        is_bar = tick % bar_ticks == 0
        color = "#ccdcd9" if is_bar else "#e7efed"
        stroke_width = 1.2 if is_bar else 0.6
        elements.append(
            f'<line x1="{x:.2f}" y1="{top}" x2="{x:.2f}" y2="{top + plot_height}" '
            f'stroke="{color}" stroke-width="{stroke_width}"/>'
        )
        if is_bar:
            bar_number = tick // bar_ticks + 1
            elements.append(
                f'<text class="axis" x="{x + 4:.2f}" y="{top + 15}">{bar_number}</text>'
            )
        beat += 1

    for pitch in range(min_pitch, max_pitch + 1):
        if pitch % 12 == 0:
            y = y_pos(pitch)
            elements.append(
                f'<line x1="{left}" y1="{y:.2f}" x2="{left + plot_width}" y2="{y:.2f}" '
                'stroke="#dfe9e7" stroke-width="0.8"/>'
            )
            elements.append(
                f'<text class="axis" x="{left - 12}" y="{y + 4:.2f}" text-anchor="end">'
                f'C{pitch // 12 - 1}</text>'
            )

    if section_boundaries:
        for bar, label in section_boundaries:
            tick = (bar - 1) * bar_ticks
            x = x_pos(tick)
            elements.append(
                f'<line x1="{x:.2f}" y1="{top}" x2="{x:.2f}" y2="{top + plot_height}" '
                'stroke="#e48673" stroke-width="2"/>'
            )
            elements.append(
                f'<text x="{x + 7:.2f}" y="{top - 12}" font-size="14" font-weight="700" '
                f'fill="#c56e5d">{html.escape(label)}</text>'
            )

    track_colors = ["#4d8f96", "#7886ba", "#c88b6f", "#8a9f78", "#9b78a8", "#5f9ba6"]
    note_height = max(2.4, plot_height / pitch_span * 0.72)
    for note in notes:
        x = x_pos(note["start"])
        note_width = max(1.3, x_pos(note["end"]) - x)
        y = y_pos(note["pitch"])
        if note["channel"] == 9:
            fill = "#e16f5a"
            opacity = 0.75
        else:
            fill = track_colors[note["track"] % len(track_colors)]
            opacity = 0.88
        elements.append(
            f'<rect x="{x:.2f}" y="{y:.2f}" width="{note_width:.2f}" '
            f'height="{note_height:.2f}" rx="1.4" fill="{fill}" opacity="{opacity}"/>'
        )

    elements.extend(
        [
            f'<text class="axis" x="{left + plot_width / 2:.2f}" y="{height - 18}" '
            'text-anchor="middle">Bars / musical time</text>',
            "</svg>",
        ]
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(elements), encoding="utf-8")


def compact_form_scale(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "strategy": data["strategy"],
        "section_count": data["section_count"],
        "total_bars": data["total_bars"],
        "form": "-".join(item["base_symbol"] + ("'" * item["variant_index"]) for item in data["form_blueprint"]),
        "theme_relations": [
            {
                "section": item["section_id"],
                "relation": item["relation"],
                "source": item["source_section_id"],
            }
            for item in data["form_blueprint"]
        ],
    }


def compact_content_plan(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "global": data["global"],
        "sections": [
            {
                "id": item["section_id"],
                "label": item["form_label"],
                "bars": [item["bar_start"], item["bar_end"]],
                "relation": item["relation"],
                "theme": item["theme_family_id"],
            }
            for item in data["form_plan"]["sections"]
        ],
    }


def compact_rhythm_plan(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "target_bpm": data.get("target_bpm"),
        "time_signature": data.get("time_signature"),
        "event_bank": data.get("event_bank"),
        "events": data.get("events"),
    }


def compact_stage2_plan(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "form_string": data["form_string"],
        "total_bars": data["total_bars"],
        "sections": [
            {
                "id": item["section_id"],
                "label": item["form_label"],
                "source": item["material_source"],
                "access": item["midigpt_access"],
                "editable": item["editable_bar_ranges"],
            }
            for item in data["sections"]
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Export one traceable LegaSynth job as a static demo case.")
    parser.add_argument("job_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--stage2-variant", default="stage2_soft_gate_test")
    parser.add_argument("--stage3-variant", default="stage3_soft_gate_test")
    args = parser.parse_args()

    job = args.job_dir.resolve()
    output = args.output_dir.resolve()
    stage2_dir = job / args.stage2_variant
    stage3_dir = job / args.stage3_variant

    root_job = read_json(job / "job.json")
    stage2_manifest = read_json(stage2_dir / "stage2_completion_manifest.json")
    stage3_manifest = read_json(stage3_dir / "stage3_render_manifest.json")
    if stage2_manifest.get("status") != "production":
        raise RuntimeError("The selected Stage 2 variant is not a completed production artifact.")
    if stage3_manifest.get("output", {}).get("final_mix") != "final_mix.wav":
        raise RuntimeError("The selected Stage 3 variant has no final mix.")

    story_request = read_json(job / "input" / "story_request.json")
    form_scale = read_json(job / "stage1" / "story-audit" / "form_scale_decision.json")
    content_plan = read_json(job / "stage1" / "story-audit" / "content_plan.json")
    rhythm_plan = read_json(job / "input" / "rhythm_plan.json")
    stage2_plan = read_json(job / "stage1" / "story-stage2" / "stage2_plan.json")

    heartbeat_root = find_one(job / "stage1" / "heartbeat", "heart_processing")
    if not heartbeat_root.is_dir():
        raise RuntimeError("Heartbeat processing directory is missing.")
    processing_dir = next(path for path in heartbeat_root.iterdir() if path.is_dir())
    heartbeat_package = find_one(job / "stage1" / "heartbeat", "heartbeat_package")

    regularized_wav = find_one(processing_dir, "*_regularized_events_enhanced.wav")
    regularized_plot = find_one(processing_dir, "*_regularized_events_enhanced_time_energy.png")
    heartbeat_bar_plot = heartbeat_package / "heartbeat_bar_time_energy.png"

    theme_paths = {
        theme: job / "stage1" / "story-musecoco" / "generated_themes" / theme / "final.mid"
        for theme in ("theme-A", "theme-B", "theme-C")
    }
    final_midi = stage2_dir / "final_completed.mid"
    final_mix = stage3_dir / "final_mix.wav"

    copy_verified(job / "input" / "heartbeat.wav", output / "audio" / "original_heartbeat.wav")
    copy_verified(regularized_wav, output / "audio" / "regularized_heartbeat.wav")
    copy_verified(
        final_mix,
        output / "audio" / "final_mix.wav",
        stage3_manifest["output"]["final_mix_sha256"],
    )
    copy_verified(regularized_plot, output / "figures" / "regularized_time_energy.png")
    copy_verified(heartbeat_bar_plot, output / "figures" / "heartbeat_bar_time_energy.png")
    for theme, path in theme_paths.items():
        copy_verified(path, output / "midi" / f"{theme}.mid")
        render_piano_roll(
            path,
            output / "figures" / f"{theme}.svg",
            f"{theme} · MuseCoco seed (8 bars)",
        )
    copy_verified(
        final_midi,
        output / "midi" / "final_completed_soft_gate.mid",
        stage2_manifest["output"]["sha256"],
    )
    render_piano_roll(
        final_midi,
        output / "figures" / "stage2_complete_soft_gate.svg",
        "Stage 2 complete MIDI · soft quality gate",
        [
            (section["bar_start"], section["form_label"])
            for section in stage2_plan["sections"]
        ],
    )

    json_sources = {
        "form_scale_decision.json": (
            job / "stage1" / "story-audit" / "form_scale_decision.json",
            compact_form_scale(form_scale),
        ),
        "content_plan.json": (
            job / "stage1" / "story-audit" / "content_plan.json",
            compact_content_plan(content_plan),
        ),
        "rhythm_plan.json": (
            job / "input" / "rhythm_plan.json",
            compact_rhythm_plan(rhythm_plan),
        ),
        "stage2_plan.json": (
            job / "stage1" / "story-stage2" / "stage2_plan.json",
            compact_stage2_plan(stage2_plan),
        ),
    }
    json_cards = []
    for name, (source, excerpt) in json_sources.items():
        copy_verified(source, output / "json" / name)
        json_cards.append(
            {
                "name": name,
                "src": f"demo/case-01/json/{name}",
                "excerpt": json.dumps(excerpt, ensure_ascii=False, indent=2),
            }
        )

    mix = stage3_manifest["mix"]
    manifest = {
        "schema_version": "legasynth-demo-case-v2",
        "status": "ready",
        "case_id": "case-01",
        "title": {
            "zh": "离乡、回忆与新的道路",
            "en": "Leaving Home, Remembering, and Moving Forward",
        },
        "subtitle": {
            "zh": "由真实心音与故事驱动的五段式生成案例，展示软质量门控重跑后的成功 MIDI 与最终混音。",
            "en": "A five-section case driven by a real heart recording and narrative, using the successful soft-gate MIDI rerun and final mix.",
        },
        "story": {"text": story_request["story_text"]},
        "audio": {
            "original_heartbeat": {
                "src": "demo/case-01/audio/original_heartbeat.wav",
                "label": {"zh": "原始心音", "en": "Original heart sound"},
            },
            "regularized_heartbeat": {
                "src": "demo/case-01/audio/regularized_heartbeat.wav",
                "label": {"zh": "规则化增强心音", "en": "Regularized enhanced heartbeat"},
            },
            "final_mix": {
                "src": "demo/case-01/audio/final_mix.wav",
                "label": {"zh": "最终混音（软校验支线）", "en": "Final mix (soft-gate branch)"},
            },
        },
        "figures": {
            "regularized_time_energy": {
                "src": "demo/case-01/figures/regularized_time_energy.png",
                "label": {"zh": "规则化增强心音：时间—能量图", "en": "Regularized heartbeat: time–energy plot"},
            },
            "heartbeat_bar_time_energy": {
                "src": "demo/case-01/figures/heartbeat_bar_time_energy.png",
                "label": {"zh": "单小节心跳：时间—能量图", "en": "One-bar heartbeat: time–energy plot"},
            },
            "stage1_midi_previews": [
                {
                    "src": f"demo/case-01/figures/{theme}.svg",
                    "label": {"zh": f"主题 {theme[-1]}", "en": f"Theme {theme[-1]}"},
                }
                for theme in theme_paths
            ],
            "stage2_midi_preview": {
                "src": "demo/case-01/figures/stage2_complete_soft_gate.svg",
                "label": {"zh": "软质量门控后的完整 MIDI", "en": "Complete MIDI after the soft quality gate"},
            },
        },
        "planning_artifacts": json_cards,
        "downloads": {
            "stage1_midis": [
                {"src": f"demo/case-01/midi/{theme}.mid", "label": f"{theme}.mid"}
                for theme in theme_paths
            ],
            "stage2_midi": {
                "src": "demo/case-01/midi/final_completed_soft_gate.mid",
                "label": "final_completed_soft_gate.mid",
            },
        },
        "metrics": {
            "form": stage2_manifest["settings"]["form_string"],
            "bpm": "80–100",
            "tonality": "C minor",
            "bars": stage2_manifest["settings"]["total_bars"],
            "final_lufs": round(mix["integrated_lufs_final"], 2),
            "true_peak_dbtp": round(mix["post_protection_true_peak_dbtp"], 2),
        },
        "provenance": {
            "source_job_id": root_job["job_id"],
            "source_job_status": root_job["status"],
            "selected_stage2_variant": args.stage2_variant,
            "selected_stage2_status": stage2_manifest["status"],
            "selected_stage2_sha256": stage2_manifest["output"]["sha256"],
            "selected_stage3_variant": args.stage3_variant,
            "selected_final_mix_sha256": stage3_manifest["output"]["final_mix_sha256"],
            "note": {
                "zh": "原始编排首次在 Stage 2 失败；本页明确采用同一任务目录内随后完成的软质量门控重跑产物，不使用失败支线的 MIDI 或音频。",
                "en": "The initial orchestration failed in Stage 2. This page explicitly uses the later successful soft-gate rerun from the same job directory and does not publish MIDI or audio from the failed branch.",
            },
        },
    }
    write_json(output / "demo_manifest.json", manifest)
    print(json.dumps({"status": "PASS", "manifest": str(output / "demo_manifest.json")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
